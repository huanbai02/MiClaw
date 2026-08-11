import os
import platform
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PureWindowsPath

from .base import miclaw_tool
from .result import (
    ToolResult,
    format_tool_result_for_model,
    tool_error,
    tool_permission_blocked,
    tool_success,
)
from ..config import OFFICE_DIR
from ..logger import log_permission_confirmation, log_permission_decision
from ..permissions import (
    PermissionCapability,
    PermissionDecision,
    PermissionRequest,
    PermissionResult,
    RiskLevel,
    evaluate_permission,
    get_permission_confirmation_handler,
    resolve_permission,
)

SYS_OS = platform.system()
_permission_evaluator = evaluate_permission
_permission_audit_logger = log_permission_decision
_permission_confirmation_audit_logger = log_permission_confirmation
SHELL_TIMEOUT_SECONDS = 10
SHELL_OUTPUT_LIMIT = 4000


class ShellCommandRisk(str, Enum):
    """Shell command 风险等级。"""

    SAFE = "safe"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class ShellCommandClassification:
    """描述 shell safety baseline 的分类结果。"""

    risk_level: ShellCommandRisk
    reason: str
    blocked: bool = False


def _get_office_root() -> Path:
    """返回 canonical 的 office workspace root。"""
    return Path(OFFICE_DIR).resolve()


def _reject_unsafe_path_input(user_path: str) -> None:
    """拒绝不应从模型输入中接受的 path 形式。"""
    raw_path = str(user_path or "")
    if PureWindowsPath(raw_path).drive:
        raise PermissionError("Windows drive paths are not allowed")
    if Path(raw_path).is_absolute() or raw_path.startswith(("/", "\\")):
        raise PermissionError("Absolute paths are not allowed")


def _ensure_path_inside_base(path: Path, base: Path) -> None:
    """如果 resolved path 不在 resolved base path 内，则抛出异常。"""
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise PermissionError("Path is outside the office workspace") from exc


def _resolve_candidate(user_path: str) -> tuple[Path, Path]:
    _reject_unsafe_path_input(user_path)
    base = _get_office_root()
    candidate = base / str(user_path or "")
    resolved = candidate.resolve(strict=False)
    try:
        _ensure_path_inside_base(resolved, base)
    except PermissionError as exc:
        if ".." in candidate.parts:
            raise PermissionError("Path traversal outside office is not allowed") from exc
        raise
    return resolved, base


def _resolve_existing_office_path(user_path: str) -> Path:
    """解析已存在的 office path，并确保 symlink 仍留在 office 内。"""
    resolved, base = _resolve_candidate(user_path)
    if not resolved.exists():
        return resolved
    resolved = resolved.resolve(strict=True)
    _ensure_path_inside_base(resolved, base)
    return resolved


def _resolve_new_office_path(user_path: str) -> Path:
    """解析写入目标，并安全校验 parent 与最终 target。"""
    target, base = _resolve_candidate(user_path)
    parent = target.parent.resolve(strict=False)
    _ensure_path_inside_base(parent, base)
    _ensure_path_inside_base(target, base)
    return target


def _get_safe_path(relative_path: str) -> str:
    """围绕安全 path resolver 的向后兼容字符串 path 包装器。"""
    return str(_resolve_new_office_path(relative_path))


def _ensure_no_symlink_escape_in_office() -> None:
    """防止 shell 命令通过逃逸 office 的 symlink 进行操作。"""
    base = _get_office_root()
    for entry in base.rglob("*"):
        if not entry.is_symlink():
            continue
        try:
            target = entry.resolve(strict=True)
        except OSError as exc:
            raise PermissionError("Path is outside the office workspace") from exc
        _ensure_path_inside_base(target, base)


def _relative_office_target(path: Path) -> str:
    """生成用于 permission request 的安全 office 相对 target。"""
    relative_path = path.relative_to(_get_office_root())
    return "." if not relative_path.parts else relative_path.as_posix()


def _format_result(result: ToolResult) -> str:
    """统一把内部 ToolResult 转成 model-facing string。"""
    return format_tool_result_for_model(result)


def _tool_metadata(tool_name: str, operation: str, target: str, **extra) -> dict:
    """生成 sandbox tool 的基础 metadata。"""
    metadata = {"tool_name": tool_name, "operation": operation, "target": target}
    metadata.update(extra)
    return metadata


def classify_shell_command(command: str) -> ShellCommandClassification:
    """用保守 regex baseline 分类 shell command；不尝试做完整 shell parser。"""
    command_text = str(command or "")
    normalized = " ".join(command_text.strip().split())
    compact = re.sub(r"\s+", "", command_text)

    if not normalized:
        return ShellCommandClassification(ShellCommandRisk.MEDIUM, "Empty shell command", blocked=True)

    critical_patterns = [
        (r"(?:^|[;&|]\s*)rm\s+.*-(?:[^\s]*r[^\s]*f|[^\s]*f[^\s]*r)\s+(?:/|~|/\*)(?:\s|$)", "Recursive removal of root or home is blocked"),
        (r"(?:^|[;&|]\s*)sudo(?:\s|$)", "Privilege escalation with sudo is blocked"),
        (r"(?:^|[;&|]\s*)su(?:\s|$)", "Privilege escalation with su is blocked"),
        (r"(?:^|[;&|]\s*)chmod\s+-R\s+777\s+/", "Recursive world-writable chmod on root is blocked"),
        (r"(?:^|[;&|]\s*)chown\s+-R(?:\s|$)", "Recursive chown is blocked"),
        (r"(?:^|[;&|]\s*)mkfs(?:\.[\w-]+)?(?:\s|$)", "Filesystem formatting commands are blocked"),
        (r"(?:^|[;&|]\s*)dd\s+.*\bif=", "Raw disk copy commands using dd are blocked"),
        (r"(?:^|[;&|]\s*)(?:shutdown|reboot|poweroff|halt)(?:\s|$)", "System power commands are blocked"),
        (r"\b(?:curl|wget)\b[^|;\n]*\|\s*(?:sudo\s+)?(?:sh|bash)\b", "Piping remote downloads into a shell is blocked"),
        (r"\b(?:bash|sh)\s*<\s*\(\s*(?:curl|wget)\b", "Process substitution from remote download into shell is blocked"),
        (r"\b(?:nc|ncat)\b[^\n;|&]*\s-e(?:\s|$)", "Netcat exec mode is blocked"),
    ]
    for pattern, reason in critical_patterns:
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            return ShellCommandClassification(ShellCommandRisk.CRITICAL, reason, blocked=True)

    if ":(){:|:&};:" in compact:
        return ShellCommandClassification(ShellCommandRisk.CRITICAL, "Fork bomb pattern is blocked", blocked=True)

    high_patterns = [
        (r"\b(?:bash|sh)\s+-[a-zA-Z]*c[a-zA-Z]*(?:\s|$)", "Nested shell execution is blocked"),
        (r"\bpython3?\s+-c\b.*(?:\bos\.system\b|\bsubprocess\b|\bpopen\b|\bPopen\b|\bsystem\s*\(|\bimport\s+subprocess\b|\bfrom\s+os\s+import\s+system\b)", "Inline Python process execution is blocked"),
        (r"(?:^|[;&|]\s*)chmod\s+-R\s+777\s+\S+", "Recursive world-writable chmod is blocked"),
        (r"(?:^|[;&|]\s*)git\s+clean\s+-[^\s]*[fxd][^\s]*(?:\s|$)", "Destructive git clean command is blocked"),
        (r"(?:^|[;&|]\s*)rm\s+.*-(?:[^\s]*r[^\s]*f|[^\s]*f[^\s]*r)\s+\S+", "Recursive force removal is blocked"),
    ]
    for pattern, reason in high_patterns:
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            return ShellCommandClassification(ShellCommandRisk.HIGH, reason, blocked=True)

    medium_patterns = [
        (r"(?:^|[;&|]\s*)(?:pip|pip3|python\s+-m\s+pip)\s+install(?:\s|$)", "Package installation changes the environment"),
        (r"(?:^|[;&|]\s*)(?:npm|pnpm|yarn)\s+install(?:\s|$)", "Package installation changes the workspace"),
    ]
    for pattern, reason in medium_patterns:
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            return ShellCommandClassification(ShellCommandRisk.MEDIUM, reason, blocked=False)

    return ShellCommandClassification(ShellCommandRisk.SAFE, "No baseline shell risk pattern matched", blocked=False)


def _truncate_shell_output(text: str, limit: int = SHELL_OUTPUT_LIMIT) -> tuple[str, bool, int]:
    """限制 model-facing shell output 长度，metadata 只保留长度和是否截断。"""
    safe_text = str(text or "")
    original_length = len(safe_text)
    if original_length <= limit:
        return safe_text, False, original_length
    return f"{safe_text[:limit]}\n... [truncated]", True, original_length


def _permission_block_message(result: PermissionResult) -> str:
    """把 DENY/ASK permission result 转换为用户可见阻断消息。"""
    if result.decision is PermissionDecision.ASK:
        return f"Permission required: {result.reason}"
    return f"Permission denied: {result.reason}"


def _evaluate_office_permission(request: PermissionRequest) -> PermissionResult:
    """调用当前 permission evaluator，便于测试用 monkeypatch 注入策略。"""
    return _permission_evaluator(request)


def _require_allowed_permission(request: PermissionRequest, metadata: dict) -> ToolResult | None:
    """评估并解析 permission；只有最终 ALLOW 可继续执行。"""
    policy_result = _evaluate_office_permission(request)
    metadata["permission_decision"] = policy_result.decision.value
    _permission_audit_logger(
        request,
        policy_result,
        tool_name=str(metadata.get("tool_name") or "unknown"),
        metadata=metadata,
    )
    confirmation_handler = get_permission_confirmation_handler()
    final_result = resolve_permission(request, policy_result, confirmation_handler)
    if policy_result.decision is PermissionDecision.ASK and confirmation_handler is not None:
        _permission_confirmation_audit_logger(
            request,
            policy_result,
            final_result,
            tool_name=str(metadata.get("tool_name") or "unknown"),
            metadata=metadata,
        )
    if final_result.decision is PermissionDecision.ALLOW:
        return None
    return tool_permission_blocked(
        _permission_block_message(final_result),
        decision=final_result.decision.value,
        metadata=metadata,
    )


@miclaw_tool
def list_office_files(sub_dir: str = "") -> str:
    """
    查看你的 office 工位里有哪些文件和文件夹。
    如果 sub_dir 为空，则查看工位根目录。
    """
    try:
        target_dir = _resolve_existing_office_path(sub_dir)
        target = _relative_office_target(target_dir)
        metadata = _tool_metadata("list_office_files", "list", target)
        block_result = _require_allowed_permission(
            PermissionRequest(
                capability=PermissionCapability.FILE_READ,
                operation="list",
                target=target,
                risk_level=RiskLevel.LOW,
                reason="List files in office workspace",
            ),
            metadata,
        )
        if block_result:
            return _format_result(block_result)

        if not target_dir.exists():
            message = f"目录不存在：{sub_dir}"
            return _format_result(tool_error("file_not_found", message, content=message, metadata=metadata))

        items = os.listdir(target_dir)
        if not items:
            message = f"[{sub_dir if sub_dir else 'office 根目录'}] 是空的。"
            return _format_result(tool_success(message, data={"items": []}, metadata=metadata))

        # 格式化输出，标注是文件还是文件夹
        result = []
        for item in items:
            item_path = target_dir / item
            item_type = "📁" if item_path.is_dir() else "📄"
            result.append(f"{item_type} {item}")

        return _format_result(tool_success("\n".join(result), data={"items": items}, metadata=metadata))
    except PermissionError as e:
        return _format_result(
            tool_error(
                "path_error",
                str(e),
                content=str(e),
                metadata=_tool_metadata("list_office_files", "list", str(sub_dir or ".")),
            )
        )
    except Exception as e:
        return _format_result(
            tool_error(
                "unexpected_error",
                str(e),
                content=str(e),
                metadata=_tool_metadata("list_office_files", "list", str(sub_dir or ".")),
            )
        )


@miclaw_tool
def read_office_file(filepath: str) -> str:
    """
    读取 office 工位里指定文件的内容。
    filepath 参数应该是相对于 office 的路径，例如 "test.py" 或 "skills/my_skill.py"。
    """
    try:
        target_path = _resolve_existing_office_path(filepath)
        target = _relative_office_target(target_path)
        metadata = _tool_metadata("read_office_file", "read", target)
        block_result = _require_allowed_permission(
            PermissionRequest(
                capability=PermissionCapability.FILE_READ,
                operation="read",
                target=target,
                risk_level=RiskLevel.LOW,
                reason="Read file from office workspace",
            ),
            metadata,
        )
        if block_result:
            return _format_result(block_result)

        if not target_path.exists():
            message = f"文件不存在：{filepath}"
            return _format_result(tool_error("file_not_found", message, content=message, metadata=metadata))

        with open(target_path, "r", encoding="utf-8") as f:
            content = f.read()
            original_length = len(content)
            truncated = original_length > 10000
            if truncated:
                content = content[:10000] + "\n\n...[内容过长，已被安全截断]..."
            return _format_result(
                tool_success(
                    content,
                    data={"content_length": original_length, "truncated": truncated},
                    metadata=metadata,
                )
            )
    except PermissionError as e:
        return _format_result(
            tool_error(
                "path_error",
                str(e),
                content=str(e),
                metadata=_tool_metadata("read_office_file", "read", str(filepath or "")),
            )
        )
    except Exception as e:
        return _format_result(
            tool_error(
                "unexpected_error",
                str(e),
                content=str(e),
                metadata=_tool_metadata("read_office_file", "read", str(filepath or "")),
            )
        )


@miclaw_tool
def write_office_file(filepath: str, content: str, mode: str = "w") -> str:
    """
    在 office 工位里操作文件内容。

    参数说明:
    - filepath: 相对路径，例如 "spider.py" 或 "docs/readme.md"。
    - content: 要写入的具体文本或代码内容。
    - mode: 写入模式。
        - "w" (默认): 【覆盖/新建】模式。如果文件已存在，将彻底清空原内容并写入新内容！
        - "a": 【追加】模式。保留原内容，将新内容追加到文件最末尾（常用于写日志或在文件末尾新增函数）。

    ⚠️ 智能体操作规范：
    1. 如果你要修改一个长文件中间的某几行，目前最安全的做法是：读取原文件，在你的内存中完成替换，然后用 "w" 模式把【完整的最新代码】重写进去。
    2. 如果你需要重命名文件或删除文件，请直接使用 execute_office_shell 工具执行 `mv` 或 `rm` 命令。
    3. 禁止编写 与 跳出office工位 相关的任何语言脚本！
    """
    try:
        target_path = _resolve_new_office_path(filepath)
        target = _relative_office_target(target_path)
        metadata = _tool_metadata("write_office_file", "write", target)
        block_result = _require_allowed_permission(
            PermissionRequest(
                capability=PermissionCapability.FILE_WRITE,
                operation="write",
                target=target,
                risk_level=RiskLevel.LOW,
                reason="Write file in office workspace",
            ),
            metadata,
        )
        if block_result:
            return _format_result(block_result)

        # 严格校验传入的 mode
        if mode not in ["w", "a"]:
            message = "❌ 错误：mode 参数必须是 'w' (覆盖) 或 'a' (追加)。"
            return _format_result(tool_error("invalid_mode", message, content=message, metadata=metadata))

        # 如果模型想在子目录里写文件，确保子目录存在
        target_path.parent.mkdir(parents=True, exist_ok=True)

        with open(target_path, mode, encoding="utf-8") as f:
            # 如果是追加模式，且内容不是以换行符开头，自动补一个换行，防止代码粘连
            if mode == "a" and not content.startswith("\n"):
                f.write("\n" + content)
            else:
                f.write(content)

        action = "覆盖/新建" if mode == "w" else "追加"
        message = f" ● 成功以 {action} 模式写入文件：{filepath} (共 {len(content)} 字符)"
        return _format_result(
            tool_success(
                message,
                data={"bytes_written": len(content), "mode": mode},
                metadata=metadata,
            )
        )
    except PermissionError as e:
        return _format_result(
            tool_error(
                "path_error",
                str(e),
                content=str(e),
                metadata=_tool_metadata("write_office_file", "write", str(filepath or "")),
            )
        )
    except Exception as e:
        return _format_result(
            tool_error(
                "unexpected_error",
                str(e),
                content=str(e),
                metadata=_tool_metadata("write_office_file", "write", str(filepath or "")),
            )
        )


@miclaw_tool
def execute_office_shell(command: str) -> str:
    """
    在 office 工位中执行 Shell 命令。

    ⚠️ 【极其重要的环境限制】：
    1. 💻 跨平台注意：当前宿主机可能是 Windows、Linux 或 Mac。请根据你得到的环境反馈，使用对应的原生 Shell 命令（例如 Win 用 dir/del，Linux 用 ls/rm）。如果命令报错，请自行调整重试！
    2. 这是一个非交互式终端！所有命令必须携带免确认参数（如 -y, --quiet）。
    3. 禁止使用 cd 命令跳出当前目录，你的活动范围仅限 office。
    4. [无状态警告] 每次执行都是独立的终端进程！需要进入子目录请使用“命令链”或相对路径。
    5. 禁止一切形式跳出office工位!!! 例如运行跳出或查看office路径的任何脚本以及其他高危操作。
    """
    metadata = _tool_metadata("execute_office_shell", "execute", "office")
    try:
        classification = classify_shell_command(command)
        metadata = _tool_metadata(
            "execute_office_shell",
            "execute",
            "office",
            shell_risk_level=classification.risk_level.value,
            blocked_by_shell_safety=classification.blocked,
        )
        if classification.blocked:
            message = f"❌ 权限拒绝：Shell command blocked by safety policy: {classification.reason}"
            return _format_result(
                tool_error(
                    "blocked_shell_command",
                    message,
                    content=message,
                    metadata=metadata,
                )
            )

        dangerous_patterns = [
            r"\.\.",                        # 杀招1：拦截所有相对路径越权 (如 ../)
            r"(?:^|\s|[<>|&;])/",           # 杀招2：Unix 拦截绝对路径 (连 cat </etc/passwd 这种黑客写法也防了)
            r"(?:^|\s|[<>|&;])~",           # 杀招3：Unix 拦截用户主目录 (防 ~/.ssh/)
            r"(?:^|\s|[<>|&;])\\",          # 杀招4：Win 拦截根目录 (防 dir \)
            r"(?i)(?:^|\s|[<>|&;])[a-z]:",  # 杀招5：Win 拦截直接跳盘符及绝对路径 (防 D:, type C:\...)
        ]
        for pattern in dangerous_patterns:
            if re.search(pattern, command):
                message = "❌ 权限拒绝：检测到危险的目录跳转指令。你被禁止离开 office 工位！"
                return _format_result(tool_error("shell_error", message, content=message, metadata=metadata))

        office_root = _get_office_root()
        safe_shell_metadata = {
            "cwd_scope": "office",
            "shell_command_present": bool(command),
            "command_length": len(command or ""),
            "shell_risk_level": classification.risk_level.value,
            "blocked_by_shell_safety": False,
        }
        metadata = _tool_metadata("execute_office_shell", "execute", "office", **safe_shell_metadata)
        _ensure_no_symlink_escape_in_office()
        block_result = _require_allowed_permission(
            PermissionRequest(
                capability=PermissionCapability.SHELL_EXEC,
                operation="execute",
                target="office",
                arguments=safe_shell_metadata,
                risk_level=RiskLevel.MEDIUM,
                reason="Execute shell command in office workspace",
            ),
            metadata,
        )
        if block_result:
            return _format_result(block_result)

        result = subprocess.run(
            command,
            shell=True,
            cwd=str(office_root),
            capture_output=True,
            encoding='utf-8',
            errors='replace',
            timeout=SHELL_TIMEOUT_SECONDS
        )

        output = f" ● 当前系统: {SYS_OS}\n"
        output += f" ● 执行命令: `{command}`\n"
        output += f" ● 退出码 (Exit Code): {result.returncode}\n"

        stdout_raw = result.stdout.strip()
        stderr_raw = result.stderr.strip()
        stdout, stdout_truncated, stdout_length = _truncate_shell_output(stdout_raw)
        stderr, stderr_truncated, stderr_length = _truncate_shell_output(stderr_raw)

        if result.returncode != 0 and ("prompt" in stderr.lower() or "y/n" in stdout.lower()):
            output += "\n💡 系统提示：命令可能由于交互式等待而失败。请重试并添加 -y 参数！"

        if stdout:
            output += f"\n[STDOUT]\n{stdout}"
        if stderr:
            output += f"\n[STDERR]\n{stderr}"

        if not stdout and not stderr:
            if result.returncode == 0:
                output += "\n(静默执行完毕：无终端输出)"
            else:
                output += "\n(异常退出：Exit Code 非 0，无错误日志输出)"

        return _format_result(
            tool_success(
                output,
                data={
                    "stdout_length": stdout_length,
                    "stderr_length": stderr_length,
                    "stdout_truncated": stdout_truncated,
                    "stderr_truncated": stderr_truncated,
                },
                metadata={**metadata, "exit_code": result.returncode},
            )
        )

    except subprocess.TimeoutExpired:
        message = f"❌ 严重错误：命令执行超时（{SHELL_TIMEOUT_SECONDS}s）被熔断！请检查是否有阻塞式交互。"
        return _format_result(
            tool_error("timeout", message, content=message, metadata={**metadata, "timeout": SHELL_TIMEOUT_SECONDS})
        )
    except Exception as e:
        message = f"❌ 执行异常：{str(e)}"
        return _format_result(tool_error("unexpected_error", str(e), content=message, metadata=metadata))
