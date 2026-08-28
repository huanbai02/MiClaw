"""提供当前 user-profile 的确定性、permission-aware Memory retrieval 边界。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .models import MemoryKind, MemoryRecord
from .permissions import (
    read_authorized_user_profile,
    read_authorized_user_profile_with_outcome,
)


DEFAULT_MEMORY_RETRIEVAL_LIMIT = 1
MAX_MEMORY_RETRIEVAL_LIMIT = 32


@dataclass(frozen=True)
class MemoryRetrievalRequest:
    """描述 host-controlled 的 Memory retrieval 请求。"""

    kinds: tuple[MemoryKind, ...]
    limit: int = DEFAULT_MEMORY_RETRIEVAL_LIMIT

    def __post_init__(self) -> None:
        """校验当前唯一支持的 Memory kind 与有界 result limit。"""
        if type(self.kinds) is not tuple:
            raise ValueError("memory retrieval kinds must be a tuple")
        if any(not isinstance(kind, MemoryKind) for kind in self.kinds):
            raise ValueError("invalid memory retrieval kind")
        if len(set(self.kinds)) != len(self.kinds):
            raise ValueError("duplicate memory retrieval kind")
        if any(kind is not MemoryKind.USER_PROFILE for kind in self.kinds):
            raise ValueError("unsupported memory kind")
        if type(self.limit) is not int or not 1 <= self.limit <= MAX_MEMORY_RETRIEVAL_LIMIT:
            raise ValueError("invalid memory retrieval limit")


@dataclass(frozen=True)
class MemoryRetrievalOutcome:
    """保存 retrieval records 与不含 identity/content 的安全结果 metadata。"""

    records: tuple[MemoryRecord, ...]
    blocked: bool
    block_reason_code: str | None
    used_global_fallback: bool


class MemoryRetriever:
    """通过既有授权读取边界返回当前 effective user-profile record。"""

    def __init__(self, memory_dir: Path | str) -> None:
        """保存 configured Memory root；不缓存 profile 内容。"""
        self._memory_dir = Path(memory_dir)

    def retrieve(self, request: MemoryRetrievalRequest) -> tuple[MemoryRecord, ...]:
        """返回确定性、有界且不可变的授权 Memory records。

        Args:
            request: 仅由 host/runtime 构造的 kind 与 result-limit 请求。

        Returns:
            当前唯一支持的 effective USER_PROFILE record；无可访问 record 时为空 tuple。
        """
        if not isinstance(request, MemoryRetrievalRequest):
            raise ValueError("invalid memory retrieval request")
        if not request.kinds:
            return ()

        record = read_authorized_user_profile(self._memory_dir)
        return (record,) if record is not None else ()

    def retrieve_with_outcome(self, request: MemoryRetrievalRequest) -> MemoryRetrievalOutcome:
        """返回 records 及供 orchestration 层记录的安全 retrieval outcome。"""
        if not isinstance(request, MemoryRetrievalRequest):
            raise ValueError("invalid memory retrieval request")
        if not request.kinds:
            return MemoryRetrievalOutcome((), False, None, False)

        outcome = read_authorized_user_profile_with_outcome(self._memory_dir)
        records = (outcome.record,) if outcome.record is not None else ()
        return MemoryRetrievalOutcome(
            records=records,
            blocked=outcome.blocked,
            block_reason_code=outcome.block_reason_code,
            used_global_fallback=outcome.used_global_fallback,
        )
