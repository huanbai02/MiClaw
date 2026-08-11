"""定义 sandbox 使用的最小 workspace root 数据模型。

本模块只描述 scope 与 canonical path，不负责注册、切换或持久化 root。
当前 runtime 仅由 sandbox tools 激活 OFFICE scope。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class WorkspaceScope(str, Enum):
    """Workspace root 的逻辑 scope；PROJECT/EXTERNAL 目前仅作类型预留。"""

    OFFICE = "office"
    PROJECT = "project"
    EXTERNAL = "external"


@dataclass(frozen=True)
class WorkspaceRoot:
    """保存一个 canonical workspace path 及其 scope。"""

    path: Path | str
    scope: WorkspaceScope

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path).expanduser().resolve())
        if not isinstance(self.scope, WorkspaceScope):
            object.__setattr__(self, "scope", WorkspaceScope(str(self.scope)))

    def to_dict(self) -> dict[str, str]:
        """返回 JSON-friendly 的 workspace root 描述。"""
        return {"path": str(self.path), "scope": self.scope.value}


_current_project_root: ContextVar[WorkspaceRoot | None] = ContextVar(
    "miclaw_project_workspace_root",
    default=None,
)


def get_active_project_root() -> WorkspaceRoot | None:
    """返回当前 run 显式绑定的 PROJECT root；未绑定时返回 None。"""
    return _current_project_root.get()


def set_active_project_root(project_path: Path | str) -> Token[WorkspaceRoot | None]:
    """校验并绑定当前 run 的 PROJECT root。

    Args:
        project_path: 用户显式提供的现有 directory path。

    Returns:
        可用于 reset 的 ContextVar token。

    Raises:
        ValueError: path 无效、不存在或不是 directory。
    """
    try:
        path = Path(project_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("Project workspace path must be an existing directory") from exc
    if not path.is_dir():
        raise ValueError("Project workspace path must be an existing directory")
    return _current_project_root.set(WorkspaceRoot(path=path, scope=WorkspaceScope.PROJECT))


def reset_active_project_root(token: Token[WorkspaceRoot | None]) -> None:
    """恢复当前 run 绑定前的 PROJECT root。"""
    _current_project_root.reset(token)
