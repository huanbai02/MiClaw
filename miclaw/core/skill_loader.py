import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from itertools import islice
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool
from functools import lru_cache

from .config import SKILLS_DIR
from .tools.sandbox_tools import execute_office_shell


_METADATA_PATTERNS = {
    "name": re.compile(r"^name:\s*(.+)$", re.MULTILINE),
    "description": re.compile(r"^description:\s*(.+)$", re.MULTILINE),
}
_EMPTY_METADATA_PATTERNS = {
    field: re.compile(rf"^{field}:[ \t]*(?:''|\"\")?[ \t]*$", re.MULTILINE)
    for field in _METADATA_PATTERNS
}


def _match_metadata_field(content: str, field: str) -> Optional[re.Match[str]]:
    """按 runtime 语法匹配一个 Skill metadata 字段。"""
    return _METADATA_PATTERNS[field].search(content)


def _is_empty_metadata_field(content: str, field: str) -> bool:
    """判断 runtime 语法下已声明但值为空的 metadata 字段。"""
    return _EMPTY_METADATA_PATTERNS[field].search(content) is not None


class SkillMetadataSeverity(str, Enum):
    """Skill metadata issue 的稳定 severity。"""

    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class SkillMetadataIssue:
    """描述一个不含路径或原始异常的 Skill metadata 问题。"""

    code: str
    severity: SkillMetadataSeverity
    field: Optional[str]
    message: str

    def to_dict(self) -> Dict[str, Optional[str]]:
        """返回 JSON-friendly issue。"""
        return {
            "code": self.code,
            "severity": self.severity.value,
            "field": self.field,
            "message": self.message,
        }


@dataclass(frozen=True)
class SkillMetadata:
    """保存 validator 按 runtime parser 识别的 metadata。"""

    name: Optional[str] = None
    description: Optional[str] = None

    def to_dict(self) -> Dict[str, Optional[str]]:
        """返回 JSON-friendly metadata。"""
        return {"name": self.name, "description": self.description}


@dataclass(frozen=True)
class SkillMetadataValidationResult:
    """保存单个 Skill 的确定性 validation 结果。"""

    skill: str
    metadata: SkillMetadata
    issues: tuple[SkillMetadataIssue, ...] = ()

    @property
    def valid(self) -> bool:
        """没有 ERROR 时返回 True；WARNING 不使结果失效。"""
        return not any(issue.severity is SkillMetadataSeverity.ERROR for issue in self.issues)

    @property
    def status(self) -> str:
        """返回 CLI 使用的 OK、WARNING 或 ERROR。"""
        if not self.valid:
            return "ERROR"
        return "WARNING" if self.issues else "OK"

    def to_dict(self) -> Dict[str, Any]:
        """返回不包含 filesystem path 的 JSON-friendly 结果。"""
        return {
            "skill": self.skill,
            "valid": self.valid,
            "metadata": self.metadata.to_dict(),
            "issues": [issue.to_dict() for issue in self.issues],
        }


_ISSUE_DEFINITIONS = {
    "missing_skill_md": (SkillMetadataSeverity.ERROR, None, "缺少 SKILL.md"),
    "invalid_skill_md": (SkillMetadataSeverity.ERROR, None, "SKILL.md 不是 regular file"),
    "unreadable_metadata": (SkillMetadataSeverity.ERROR, None, "metadata 不可读取"),
    "invalid_encoding": (SkillMetadataSeverity.ERROR, None, "metadata 不是有效 UTF-8"),
    "missing_name": (SkillMetadataSeverity.ERROR, "name", "缺少 name"),
    "empty_name": (SkillMetadataSeverity.ERROR, "name", "name 不能为空"),
    "missing_description": (SkillMetadataSeverity.WARNING, "description", "缺少 description"),
    "empty_description": (SkillMetadataSeverity.WARNING, "description", "description 不能为空"),
    "invalid_skills_directory": (SkillMetadataSeverity.ERROR, None, "skills path 不是 directory"),
    "unreadable_skills_directory": (SkillMetadataSeverity.ERROR, None, "skills directory 不可读取"),
}


def _metadata_issue(code: str) -> SkillMetadataIssue:
    """根据稳定 code 创建安全、确定的 issue。"""
    severity, field, message = _ISSUE_DEFINITIONS[code]
    return SkillMetadataIssue(code=code, severity=severity, field=field, message=message)


def _read_metadata_prefix(md_path: str) -> str:
    """读取 Skill metadata 所需的前 50 行，不加载完整正文。"""
    with open(md_path, "r", encoding="utf-8") as f:
        return "\n".join(islice(f, 50))


def validate_skill_metadata(skill: str, md_path: str) -> SkillMetadataValidationResult:
    """按 runtime parser 语义校验一个 Skill 的结构和基础 metadata。

    Args:
        skill: 安全的 Skill folder 标识。
        md_path: 仅用于内部读取的 SKILL.md path，不写入结果。

    Returns:
        不含绝对路径或原始异常的结构化 validation 结果。
    """
    safe_skill = os.path.basename(os.path.normpath(skill)) or "unknown"
    empty_metadata = SkillMetadata()
    if not os.path.exists(md_path):
        return SkillMetadataValidationResult(safe_skill, empty_metadata, (_metadata_issue("missing_skill_md"),))
    if not os.path.isfile(md_path) or os.path.islink(md_path):
        return SkillMetadataValidationResult(safe_skill, empty_metadata, (_metadata_issue("invalid_skill_md"),))

    try:
        content = _read_metadata_prefix(md_path)
    except UnicodeDecodeError:
        return SkillMetadataValidationResult(safe_skill, empty_metadata, (_metadata_issue("invalid_encoding"),))
    except OSError:
        return SkillMetadataValidationResult(safe_skill, empty_metadata, (_metadata_issue("unreadable_metadata"),))

    name_match = _match_metadata_field(content, "name")
    description_match = _match_metadata_field(content, "description")
    name = name_match.group(1).strip() if name_match else None
    description = description_match.group(1).strip() if description_match else None
    if description and (
        (description.startswith('"') and description.endswith('"'))
        or (description.startswith("'") and description.endswith("'"))
    ):
        description = description[1:-1]

    issues = []
    if _is_empty_metadata_field(content, "name"):
        name = ""
        issues.append(_metadata_issue("empty_name"))
    elif name_match is None:
        issues.append(_metadata_issue("missing_name"))
    if _is_empty_metadata_field(content, "description"):
        description = ""
        issues.append(_metadata_issue("empty_description"))
    elif description_match is None:
        issues.append(_metadata_issue("missing_description"))

    return SkillMetadataValidationResult(
        skill=safe_skill,
        metadata=SkillMetadata(name=name, description=description),
        issues=tuple(issues),
    )


class DynamicSkillInput(BaseModel):
    mode: str = Field(
        description="必须是 'help' 或 'run'。第一次使用时强烈建议先传入 'help' 阅读说明书。"
    )
    command: Optional[str] = Field(
        default="",
        description="仅在 mode='run' 时需要。你要执行的完整命令，保留 {baseDir} 占位符。"
    )


class LazySkillLoader:
    """
    渐进式技能加载器 + 缓存机制
    
    特性：
    1. 启动时只扫描元数据（name, description），不加载完整内容
    2. 首次调用技能时才加载完整内容并缓存
    3. 支持热更新（修改技能文件后自动重新加载）
    4. LRU缓存策略，自动清理不常用的技能
    """
    
    def __init__(self, cache_size: int = 50):
        self._skill_registry: Optional[List[Dict[str, Any]]] = None
        self._cache_size = cache_size
        self._last_scan_time = 0
        self._scan_interval = 60  # 缓存元数据扫描结果60秒
    
    @lru_cache(maxsize=50)
    def _load_skill_content(self, md_path: str, mtime: float) -> str:
        """
        加载技能完整内容（带缓存）
        
        Args:
            md_path: 技能文件路径
            mtime: 文件修改时间（用于缓存失效检测）
        
        Returns:
            技能的完整 Markdown 内容
        """
        with open(md_path, "r", encoding="utf-8") as f:
            return f.read()

    def _list_skill_directories(self) -> List[tuple[str, str]]:
        """按名称返回现有 Skill directory，供 discovery 与 lint 共用。"""
        return [
            (item, os.path.join(SKILLS_DIR, item))
            for item in sorted(os.listdir(SKILLS_DIR))
            if os.path.isdir(os.path.join(SKILLS_DIR, item))
        ]
    
    def _scan_skills(self, force_rescan: bool = False) -> List[Dict[str, Any]]:
        """
        扫描技能目录，只提取元数据（轻量级操作）
        
        Args:
            force_rescan: 是否强制重新扫描（忽略缓存）
        
        Returns:
            技能元数据列表
        """
        current_time = time.time()
        
        # 缓存检查：如果最近扫描过且不强制刷新，直接返回缓存
        if (not force_rescan and 
            self._skill_registry is not None and 
            current_time - self._last_scan_time < self._scan_interval):
            return self._skill_registry
        
        skills = []
        
        if not os.path.exists(SKILLS_DIR):
            self._skill_registry = []
            self._last_scan_time = current_time
            return []
        
        for item, folder_path in self._list_skill_directories():
            md_path = os.path.join(folder_path, "SKILL.md")
            if not os.path.exists(md_path):
                md_path = os.path.join(folder_path, "README.md")
            
            if not os.path.exists(md_path):
                continue
            
            try:
                # 只读取前几行（name, description）
                metadata = self._extract_metadata(md_path)
                
                if metadata:
                    skills.append({
                        "folder": item,
                        "md_path": md_path,
                        "mtime": os.path.getmtime(md_path),
                        **metadata
                    })
            except Exception as e:
                print(f" [警告] 扫描 Skill 失败: {item} ({type(e).__name__})")
        
        skills.sort(key=lambda skill: (skill["name"], skill["folder"]))
        self._skill_registry = skills
        self._last_scan_time = current_time
        
        if skills:
            print(f" [OK] 扫描到 {len(skills)} 个技能（懒加载模式）")
        
        return skills
    
    def _extract_metadata(self, md_path: str) -> Optional[Dict[str, str]]:
        """
        从技能文件中提取元数据（只读取必要的部分）
        
        Args:
            md_path: 技能文件路径
        
        Returns:
            包含 name 和 description 的字典
        """
        try:
            content = _read_metadata_prefix(md_path)
            
            name_match = _match_metadata_field(content, "name")
            desc_match = _match_metadata_field(content, "description")
            
            raw_name = name_match.group(1).strip() if name_match else os.path.basename(os.path.dirname(md_path))
            tool_name = re.sub(r'[^a-zA-Z0-9_-]', '_', raw_name)
            
            raw_desc = desc_match.group(1).strip() if desc_match else f"提供 {raw_name} 相关功能"
            if (raw_desc.startswith('"') and raw_desc.endswith('"')) or (raw_desc.startswith("'") and raw_desc.endswith("'")):
                raw_desc = raw_desc[1:-1]
            
            return {
                "raw_name": raw_name,
                "name": tool_name,
                "description": raw_desc
            }
        except Exception as e:
            skill_name = os.path.basename(os.path.dirname(md_path)) or "unknown"
            print(f" [警告] 提取 Skill metadata 失败: {skill_name} ({type(e).__name__})")
            return None
    
    def _create_lazy_tool(self, skill_info: Dict[str, Any]) -> StructuredTool:
        """
        创建懒加载工具对象
        
        Args:
            skill_info: 技能元数据
        
        Returns:
            LangChain 工具对象
        """
        def lazy_runner(mode: str, command: str = "") -> str:
            """懒加载执行器：首次调用时才加载完整内容"""
            if mode == "help":
                # 懒加载：首次调用时才读取完整内容
                skill_content = self._load_skill_content(
                    skill_info["md_path"], 
                    skill_info["mtime"]
                )
                
                return (
                    f"========== 【{skill_info['raw_name']} 完整说明书】 ==========\n"
                    f"{skill_content[:3000]}\n"
                    f"====================================\n"
                    f"提示：请根据以上说明，如果觉得能解决问题，就将 mode 设为 'run'，"
                    f"并将拼装好的执行命令填入 command 重新调用。"
                )
            elif mode == "run":
                if not command:
                    return "错误：在 'run' 模式下，必须提供 command 参数！"
                
                actual_cmd = command.replace("{baseDir}", f"skills/{skill_info['folder']}")
                return execute_office_shell.invoke({"command": actual_cmd})
            else:
                return "错误：mode 参数只能是 'help' 或 'run'。"
        
        mini_description = (
            f"{skill_info['description']}\n\n"
            f"注意：这是一个外部扩展技能。首次使用请务必先传入 `mode='help'` 来阅读完整说明书，"
            f"之后再使用 `mode='run'` 配合 `command` 执行底层脚本。"
        )
        
        return StructuredTool.from_function(
            func=lazy_runner,
            name=skill_info["name"],
            description=mini_description,
            args_schema=DynamicSkillInput
        )
    
    def get_all_tools(self, force_rescan: bool = False) -> List[StructuredTool]:
        """
        获取所有工具（懒加载占位符）
        
        Args:
            force_rescan: 是否强制重新扫描技能目录
        
        Returns:
            工具对象列表
        """
        skill_infos = self._scan_skills(force_rescan=force_rescan)
        
        tools = []
        for skill_info in skill_infos:
            tools.append(self._create_lazy_tool(skill_info))
        
        return tools
    
    def get_tool_count(self) -> int:
        """获取技能数量（不触发加载）"""
        return len(self._scan_skills())

    def list_metadata(self, force_rescan: bool = False) -> List[Dict[str, str]]:
        """返回 Skill 列表所需的 metadata 副本，不加载完整正文。

        Args:
            force_rescan: 是否忽略 metadata TTL cache 并重新扫描。

        Returns:
            仅包含 name 和 description 的 metadata 列表。
        """
        return [
            {
                "name": str(skill["name"]),
                "description": str(skill["description"]),
            }
            for skill in self._scan_skills(force_rescan=force_rescan)
        ]

    def validate_skills(self) -> List[SkillMetadataValidationResult]:
        """校验 workspace Skills，不改变 runtime cache。"""
        if not os.path.exists(SKILLS_DIR):
            return []
        if not os.path.isdir(SKILLS_DIR):
            return [SkillMetadataValidationResult(
                skill="skills",
                metadata=SkillMetadata(),
                issues=(_metadata_issue("invalid_skills_directory"),),
            )]

        try:
            skill_directories = self._list_skill_directories()
        except OSError:
            return [SkillMetadataValidationResult(
                skill="skills",
                metadata=SkillMetadata(),
                issues=(_metadata_issue("unreadable_skills_directory"),),
            )]

        return [
            validate_skill_metadata(item, os.path.join(folder_path, "SKILL.md"))
            for item, folder_path in skill_directories
        ]
    
    def clear_cache(self):
        """清除所有缓存"""
        self._load_skill_content.cache_clear()
        self._skill_registry = None
        print(f" [OK] 技能缓存已清除")


# 全局懒加载器实例
_lazy_loader = LazySkillLoader(cache_size=50)


def load_dynamic_skills(force_rescan: bool = False) -> List[StructuredTool]:
    """
    加载动态技能（懒加载 + 缓存版本）
    
    Args:
        force_rescan: 是否强制重新扫描技能目录（默认 False）
    
    Returns:
        工具对象列表（懒加载占位符）
    
    Note:
        - 启动时只扫描元数据，不加载完整内容
        - 首次调用技能时才加载完整内容
        - 支持热更新（修改技能文件后自动重新加载）
        - 使用 LRU 缓存策略
    """
    return _lazy_loader.get_all_tools(force_rescan=force_rescan)


def reload_skills() -> List[StructuredTool]:
    """
    强制重新扫描技能目录并清除缓存
    
    Returns:
        更新后的工具列表
    """
    return _lazy_loader.get_all_tools(force_rescan=True)


def get_skill_count() -> int:
    """
    获取当前技能数量（不触发加载）
    
    Returns:
        技能总数
    """
    return _lazy_loader.get_tool_count()


def list_skill_metadata(force_rescan: bool = False) -> List[Dict[str, str]]:
    """列出已发现 Skill 的轻量 metadata，不触发完整正文加载。

    Args:
        force_rescan: 是否忽略 metadata TTL cache 并重新扫描。

    Returns:
        按 Skill name 排序的 metadata 列表。
    """
    return _lazy_loader.list_metadata(force_rescan=force_rescan)


def validate_skills() -> List[SkillMetadataValidationResult]:
    """校验当前 workspace Skills，不触发完整正文加载。"""
    return _lazy_loader.validate_skills()


def lint_skills() -> List[SkillMetadataValidationResult]:
    """兼容 PR25 的 lint API，返回结构化 validation 结果。"""
    return validate_skills()


def clear_skill_cache():
    """清除技能内容缓存"""
    _lazy_loader.clear_cache()
