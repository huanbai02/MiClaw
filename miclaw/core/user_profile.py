"""集中管理当前单一 Markdown 用户画像的 filesystem IO。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tempfile

from .memory import (
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemoryScopeKind,
    MemorySource,
)
from .workspace import WorkspaceRoot, WorkspaceScope, get_active_project_root


USER_PROFILE_FILENAME = "user_profile.md"
USER_PROFILE_MEMORY_ID = "user-profile"


class UserProfilePersistenceError(RuntimeError):
    """表示 profile 持久化失败，避免向调用方暴露路径或内容。"""


class UserProfileRoutingError(RuntimeError):
    """表示当前 workspace 无法安全映射为 profile persistence scope。"""


def derive_project_memory_id(project_root: Path | str) -> str:
    """从已授权的 canonical PROJECT root 派生稳定、不含路径的 namespace ID。

    Args:
        project_root: 已由 workspace 层校验的 existing project directory。

    Returns:
        基于 canonical path digest 的固定十六进制 ID。

    Raises:
        UserProfileRoutingError: root 无法作为有效 PROJECT directory 使用时抛出。
    """
    try:
        canonical_root = Path(project_root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise UserProfileRoutingError("invalid_project_memory_root") from None
    if not canonical_root.is_dir():
        raise UserProfileRoutingError("invalid_project_memory_root")
    return hashlib.sha256(str(canonical_root).encode("utf-8", "surrogatepass")).hexdigest()[:24]


@dataclass(frozen=True)
class UserProfileStore:
    """读写一个 scoped profile 文件，并为 PROJECT 提供 GLOBAL read fallback。"""

    profile_path: Path | str
    scope: MemoryScope = MemoryScope(MemoryScopeKind.GLOBAL)
    global_profile_path: Path | str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_path", Path(self.profile_path))
        if self.global_profile_path is not None:
            object.__setattr__(self, "global_profile_path", Path(self.global_profile_path))
        if self.scope.kind is MemoryScopeKind.PROJECT and self.global_profile_path is None:
            raise ValueError("project profile store requires global_profile_path")

    def read_profile(self) -> str | None:
        """读取当前完整 profile；缺失或空文件返回 None。

        Returns:
            去除首尾空白后的 profile；不存在或为空时返回 None。
        """
        record = self.read_record()
        return record.content if record else None

    def read_record(self) -> MemoryRecord | None:
        """读取当前 scoped profile；PROJECT 缺失或为空时回退 GLOBAL profile。

        Returns:
            存在且非空时返回对应 scope 的用户画像 record；否则返回 None。
        """
        content = self._read_content(self.profile_path)
        if content:
            return self._record(content, self.scope)
        if self.scope.kind is MemoryScopeKind.PROJECT and self.global_profile_path is not None:
            global_content = self._read_content(self.global_profile_path)
            if global_content:
                return self._record(global_content, MemoryScope(MemoryScopeKind.GLOBAL))
        return None

    @staticmethod
    def _read_content(profile_path: Path) -> str | None:
        """保持 legacy profile 的整文件、UTF-8 ignore 与空内容语义。"""
        if not profile_path.exists():
            return None
        content = profile_path.read_text(encoding="utf-8", errors="ignore").strip()
        return content or None

    @staticmethod
    def _record(content: str, scope: MemoryScope) -> MemoryRecord:
        """根据实际读取来源构造不包含路径的 profile record。"""
        memory_id = USER_PROFILE_MEMORY_ID
        if scope.kind is MemoryScopeKind.PROJECT:
            memory_id = f"{USER_PROFILE_MEMORY_ID}::{scope.scope_id}"
        return MemoryRecord(
            memory_id=memory_id,
            kind=MemoryKind.USER_PROFILE,
            scope=scope,
            source=MemorySource.USER_PROFILE_STORE,
            content=content,
        )

    def write_profile(self, content: str) -> None:
        """以同目录临时文件和原子 replace 覆盖写入 profile。

        Args:
            content: 要保存的完整 Markdown profile 文本。

        Raises:
            UserProfilePersistenceError: 创建、写入或替换 profile 失败时抛出。
        """
        temp_path: Path | None = None
        try:
            self.profile_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.profile_path.parent,
                prefix=f".{self.profile_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                temp_file.write(content)
                temp_file.flush()

            os.replace(temp_path, self.profile_path)
            temp_path = None
        except OSError:
            raise UserProfilePersistenceError("user_profile_write_failed") from None
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass


def get_user_profile_store(
    memory_dir: Path | str,
    workspace_root: WorkspaceRoot | None = None,
) -> UserProfileStore:
    """根据 active workspace 创建唯一的 GLOBAL 或 PROJECT profile store。

    Args:
        memory_dir: 当前 runtime 配置使用的 Memory root。
        workspace_root: 可选的已授权 workspace root；省略时读取当前 ContextVar。

    Raises:
        UserProfileRoutingError: scope 未支持或 PROJECT root 无效时抛出。
    """
    memory_root = Path(memory_dir)
    global_profile_path = memory_root / USER_PROFILE_FILENAME
    active_root = get_active_project_root() if workspace_root is None else workspace_root

    if active_root is None:
        return UserProfileStore(global_profile_path)
    if not isinstance(active_root, WorkspaceRoot):
        raise UserProfileRoutingError("unsupported_memory_scope")
    if active_root.scope is WorkspaceScope.OFFICE:
        return UserProfileStore(global_profile_path)
    if active_root.scope is not WorkspaceScope.PROJECT:
        raise UserProfileRoutingError("unsupported_memory_scope")

    project_id = derive_project_memory_id(active_root.path)
    project_profile_path = memory_root / "projects" / project_id / USER_PROFILE_FILENAME
    return UserProfileStore(
        profile_path=project_profile_path,
        scope=MemoryScope(MemoryScopeKind.PROJECT, project_id),
        global_profile_path=global_profile_path,
    )
