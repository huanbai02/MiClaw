"""定义 sandbox 使用的最小 workspace root 数据模型。

本模块只描述 scope 与 canonical path，不负责注册、切换或持久化 root。
当前 runtime 仅由 sandbox tools 激活 OFFICE scope。
"""

from __future__ import annotations

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
