from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
import uuid


@dataclass
class TraceContext:
    """保存一次 MiClaw run 的最小 trace context。"""

    run_id: str
    step_counter: int = 0

    def __post_init__(self) -> None:
        """确保 run_id 始终是 JSON-friendly 的非空字符串。"""
        if not self.run_id:
            self.run_id = new_run_id()

    def next_step_id(self) -> int:
        """返回当前 run 内递增的 step_id。"""
        self.step_counter += 1
        return self.step_counter


_current_trace_context: ContextVar[TraceContext | None] = ContextVar("miclaw_trace_context", default=None)


def new_run_id() -> str:
    """生成 JSON-friendly 的 run_id。"""
    return uuid.uuid4().hex


def get_current_trace_context() -> TraceContext | None:
    """读取当前 contextvars 中绑定的 trace context。"""
    return _current_trace_context.get()


def set_current_trace_context(context: TraceContext) -> Token[TraceContext | None]:
    """绑定当前运行的 trace context，并返回可 reset 的 token。"""
    return _current_trace_context.set(context)


def reset_trace_context(token: Token[TraceContext | None]) -> None:
    """恢复绑定前的 trace context。"""
    _current_trace_context.reset(token)
