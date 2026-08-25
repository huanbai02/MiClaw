"""定义当前显式用户画像使用的最小 Memory 语义模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MemoryKind(str, Enum):
    """当前已支持的 Memory 内容类别。"""

    USER_PROFILE = "user_profile"


class MemoryScopeKind(str, Enum):
    """Memory 的逻辑作用域类别。"""

    GLOBAL = "global"
    PROJECT = "project"


class MemorySource(str, Enum):
    """Memory record 的来源通道，不表示已验证的内容作者。"""

    USER_PROFILE_STORE = "user_profile_store"


@dataclass(frozen=True)
class MemoryScope:
    """表示 Memory 的逻辑作用域及其可选标识。"""

    kind: MemoryScopeKind
    scope_id: str | None = None

    def __post_init__(self) -> None:
        """校验 GLOBAL/PROJECT 的最小作用域不变量。"""
        if not isinstance(self.kind, MemoryScopeKind):
            raise ValueError("invalid memory scope kind")
        if self.kind is MemoryScopeKind.GLOBAL and self.scope_id is not None:
            raise ValueError("global memory scope must not have scope_id")
        if self.kind is MemoryScopeKind.PROJECT:
            if not isinstance(self.scope_id, str) or not self.scope_id.strip():
                raise ValueError("project memory scope requires non-empty scope_id")


@dataclass(frozen=True)
class MemoryRecord:
    """表示可注入 context 的不可变 Memory value object。"""

    memory_id: str
    kind: MemoryKind
    scope: MemoryScope
    source: MemorySource
    content: str

    def __post_init__(self) -> None:
        """校验 record 的最小语义字段，不回显 content 或路径。"""
        if not isinstance(self.memory_id, str) or not self.memory_id.strip():
            raise ValueError("memory_id must be a non-empty string")
        if not isinstance(self.kind, MemoryKind):
            raise ValueError("invalid memory kind")
        if not isinstance(self.scope, MemoryScope):
            raise ValueError("invalid memory scope")
        if not isinstance(self.source, MemorySource):
            raise ValueError("invalid memory source")
        if not isinstance(self.content, str):
            raise ValueError("memory content must be a string")
