"""定义由 host 投递给 Agent worker 的最小请求边界。"""

from __future__ import annotations

from dataclasses import dataclass

from ..memory.lifecycle import MemoryWriteIntent


@dataclass(frozen=True)
class AgentRequest:
    """承载一条 Agent 输入及仅由 host 建立的本轮 Memory 写入意图。"""

    content: str
    memory_write_intent: MemoryWriteIntent | None = None

    def __post_init__(self) -> None:
        """拒绝 queue 边界的隐式类型转换。"""
        if type(self.content) is not str:
            raise ValueError("invalid_agent_request_content")
        if self.memory_write_intent is not None and self.memory_write_intent is not MemoryWriteIntent.EXPLICIT_USER_REQUEST:
            raise ValueError("invalid_agent_request_memory_write_intent")
