"""定义 attempt 内 repeated Tool call 的纯语义 guard。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json


DEFAULT_MAX_CONSECUTIVE_IDENTICAL_TOOL_CALLS = 3
MAX_GUARD_CANONICAL_ARG_BYTES = 65_536


class GuardDecision(str, Enum):
    """Tool call batch 是否可以继续进入 ToolNode。"""

    ALLOW = "allow"
    BLOCK = "block"


class GuardReason(str, Enum):
    """当前 guard 的稳定、无内容原因。"""

    REPEATED_IDENTICAL_TOOL_CALL = "repeated_identical_tool_call"


class ExecutionGuardValidationError(ValueError):
    """表示不回显 Tool identity/args 的稳定 guard validation error。"""


class ExecutionGuardTriggered(RuntimeError):
    """表示 ToolNode 前已阻断的稳定 semantic loop guard failure。"""

    def __init__(self, reason: GuardReason) -> None:
        if type(reason) is not GuardReason:
            raise ExecutionGuardValidationError("invalid_execution_guard")
        self.reason = reason
        super().__init__("loop_guard_triggered")


@dataclass(frozen=True, slots=True)
class ExecutionGuardPolicy:
    """限制连续相同 semantic Tool call 的 attempt-local policy。

    Args:
        max_consecutive_identical_tool_calls: 第 N 次相同调用在 Tool 执行前被阻断。
    """

    max_consecutive_identical_tool_calls: int = DEFAULT_MAX_CONSECUTIVE_IDENTICAL_TOOL_CALLS

    def __post_init__(self) -> None:
        """拒绝无意义阈值与隐式类型转换。"""
        if type(self.max_consecutive_identical_tool_calls) is not int or self.max_consecutive_identical_tool_calls < 2:
            raise ExecutionGuardValidationError("invalid_execution_guard_policy")


@dataclass(frozen=True, slots=True)
class ExecutionGuardState:
    """仅保存 ephemeral fingerprint 与连续计数，不保存原始 Tool 内容。"""

    last_fingerprint: str | None = None
    consecutive_identical_count: int = 0

    def __post_init__(self) -> None:
        """保持 state 可安全地作为 ContextVar 内存值。"""
        if type(self.consecutive_identical_count) is not int or self.consecutive_identical_count < 0:
            raise ExecutionGuardValidationError("invalid_execution_guard_state")
        if self.last_fingerprint is None:
            if self.consecutive_identical_count != 0:
                raise ExecutionGuardValidationError("invalid_execution_guard_state")
        elif type(self.last_fingerprint) is not str or self.consecutive_identical_count < 1:
            raise ExecutionGuardValidationError("invalid_execution_guard_state")


@dataclass(frozen=True, slots=True)
class GuardEvaluation:
    """完整 Tool batch 的预检结果。

    Args:
        decision: 是否允许 ToolNode 执行整个 batch。
        reason: 阻断时的稳定原因；允许时为 None。
        state: 仅在允许时可提交的下一 attempt-local guard state。
    """

    decision: GuardDecision
    reason: GuardReason | None
    state: ExecutionGuardState

    def __post_init__(self) -> None:
        """拒绝含任意 Tool 原文的非稳定结果形状。"""
        if type(self.decision) is not GuardDecision or type(self.state) is not ExecutionGuardState:
            raise ExecutionGuardValidationError("invalid_execution_guard")
        if self.decision is GuardDecision.ALLOW and self.reason is not None:
            raise ExecutionGuardValidationError("invalid_execution_guard")
        if self.decision is GuardDecision.BLOCK and type(self.reason) is not GuardReason:
            raise ExecutionGuardValidationError("invalid_execution_guard")


def observe_tool_call_batch(
    state: ExecutionGuardState,
    tool_calls: object,
    policy: ExecutionGuardPolicy,
) -> GuardEvaluation:
    """原子预检一条 AIMessage 中的完整 Tool call batch。

    不可追踪的调用会重置连续链但不会阻断；若任一调用会触发阈值，则返回原 state，
    因而调用方不会提交 batch 的部分观察结果。

    Args:
        state: 当前 attempt-local guard state。
        tool_calls: Agent-facing AIMessage.tool_calls 的顺序列表。
        policy: 固定的 repeated-call threshold。

    Returns:
        可提交的 ALLOW state，或不会修改 state 的 BLOCK decision。
    """
    if type(state) is not ExecutionGuardState or type(policy) is not ExecutionGuardPolicy:
        raise ExecutionGuardValidationError("invalid_execution_guard")
    if type(tool_calls) is not list:
        return GuardEvaluation(GuardDecision.ALLOW, None, ExecutionGuardState())

    candidate = state
    for tool_call in tool_calls:
        fingerprint = _tool_call_fingerprint(tool_call)
        if fingerprint is None:
            candidate = ExecutionGuardState()
            continue
        count = candidate.consecutive_identical_count + 1 if fingerprint == candidate.last_fingerprint else 1
        if count >= policy.max_consecutive_identical_tool_calls:
            return GuardEvaluation(GuardDecision.BLOCK, GuardReason.REPEATED_IDENTICAL_TOOL_CALL, state)
        candidate = ExecutionGuardState(fingerprint, count)
    return GuardEvaluation(GuardDecision.ALLOW, None, candidate)


def _tool_call_fingerprint(tool_call: object) -> str | None:
    """生成不落盘的 name+canonical-args digest；无法安全编码时返回 None。"""
    if type(tool_call) is not dict:
        return None
    name = tool_call.get("name")
    arguments = tool_call.get("args")
    if type(name) is not str or type(arguments) is not dict:
        return None
    try:
        digest = sha256()
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        byte_count = 0
        encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        for chunk in encoder.iterencode(arguments):
            encoded = chunk.encode("utf-8")
            byte_count += len(encoded)
            if byte_count > MAX_GUARD_CANONICAL_ARG_BYTES:
                return None
            digest.update(encoded)
        return digest.hexdigest()
    except Exception:
        return None
