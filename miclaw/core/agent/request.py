"""定义由 host 投递给 Agent worker 的最小请求边界。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..memory.lifecycle import MemoryWriteIntent


class AgentRequestOrigin(str, Enum):
    """标记 host 建立的请求来源，避免内部 producer 获得交互授权范围。"""

    INTERACTIVE = "interactive"
    SCHEDULER = "scheduler"


@dataclass(frozen=True)
class AgentRequest:
    """承载 Agent 输入、host provenance 与仅 host 建立的 Memory 写入意图。"""

    content: str
    origin: AgentRequestOrigin
    memory_write_intent: MemoryWriteIntent | None = None

    def __post_init__(self) -> None:
        """拒绝 queue 边界的隐式类型转换。"""
        if type(self.content) is not str:
            raise ValueError("invalid_agent_request_content")
        if type(self.origin) is not AgentRequestOrigin:
            raise ValueError("invalid_agent_request_origin")
        if self.memory_write_intent is not None and self.memory_write_intent is not MemoryWriteIntent.EXPLICIT_USER_REQUEST:
            raise ValueError("invalid_agent_request_memory_write_intent")
