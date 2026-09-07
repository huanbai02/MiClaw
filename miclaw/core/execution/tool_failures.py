"""定义 structured Tool failure 是否终止当前 execution attempt 的保守策略。"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from collections.abc import Sequence

from .failures import ExecutionFailure, ExecutionFailureCode, ExecutionFailureSource


class ToolFailureDisposition(str, Enum):
    """Tool failure 在本轮 Agent graph 中的后续处理。"""

    MODEL_CONTINUE = "model_continue"
    ATTEMPT_FAIL = "attempt_fail"


class ToolFailureDispositionError(ValueError):
    """表示不回显 Tool content 的稳定 disposition validation error。"""


# 新增 ExecutionFailureCode 必须在此显式选择 disposition，避免默默改变 Tool terminalization。
TOOL_FAILURE_DISPOSITIONS = MappingProxyType({
    ExecutionFailureCode.TOOL_TIMEOUT: ToolFailureDisposition.MODEL_CONTINUE,
    ExecutionFailureCode.TOOL_TRANSIENT_ERROR: ToolFailureDisposition.MODEL_CONTINUE,
    ExecutionFailureCode.TOOL_EXECUTION_ERROR: ToolFailureDisposition.MODEL_CONTINUE,
    ExecutionFailureCode.PROVIDER_TIMEOUT: ToolFailureDisposition.MODEL_CONTINUE,
    ExecutionFailureCode.PROVIDER_TRANSIENT_ERROR: ToolFailureDisposition.MODEL_CONTINUE,
    ExecutionFailureCode.PROVIDER_ERROR: ToolFailureDisposition.MODEL_CONTINUE,
    ExecutionFailureCode.PERMISSION_DENIED: ToolFailureDisposition.MODEL_CONTINUE,
    ExecutionFailureCode.PERMISSION_REQUIRED: ToolFailureDisposition.MODEL_CONTINUE,
    ExecutionFailureCode.SAFETY_BLOCKED: ToolFailureDisposition.ATTEMPT_FAIL,
    ExecutionFailureCode.INVALID_INPUT: ToolFailureDisposition.MODEL_CONTINUE,
    ExecutionFailureCode.INVALID_TARGET: ToolFailureDisposition.MODEL_CONTINUE,
    ExecutionFailureCode.INVALID_CONFIGURATION: ToolFailureDisposition.ATTEMPT_FAIL,
    ExecutionFailureCode.RUNTIME_ERROR: ToolFailureDisposition.MODEL_CONTINUE,
    ExecutionFailureCode.EXECUTION_LIMIT_EXCEEDED: ToolFailureDisposition.MODEL_CONTINUE,
    ExecutionFailureCode.LOOP_GUARD_TRIGGERED: ToolFailureDisposition.MODEL_CONTINUE,
    ExecutionFailureCode.UNKNOWN_ERROR: ToolFailureDisposition.ATTEMPT_FAIL,
})

_ATTEMPT_FAIL_PRECEDENCE = MappingProxyType({
    ExecutionFailureCode.SAFETY_BLOCKED: 3,
    ExecutionFailureCode.INVALID_CONFIGURATION: 2,
    ExecutionFailureCode.UNKNOWN_ERROR: 1,
})


def get_tool_failure_disposition(failure: ExecutionFailure) -> ToolFailureDisposition:
    """返回已分类 Tool failure 的明确 graph disposition。"""
    if type(failure) is not ExecutionFailure or failure.source is not ExecutionFailureSource.TOOL:
        raise ToolFailureDispositionError("invalid_tool_failure")
    try:
        return TOOL_FAILURE_DISPOSITIONS[failure.code]
    except KeyError:
        raise ToolFailureDispositionError("unsupported_tool_failure_code") from None


def select_terminal_tool_failure(failures: Sequence[ExecutionFailure]) -> ExecutionFailure | None:
    """按固定 precedence 和原始 ToolMessage 顺序选出本 batch 的唯一 terminal failure。"""
    if not isinstance(failures, Sequence):
        raise ToolFailureDispositionError("invalid_tool_failures")
    candidates: list[tuple[int, ExecutionFailure]] = []
    for index, failure in enumerate(failures):
        if get_tool_failure_disposition(failure) is ToolFailureDisposition.ATTEMPT_FAIL:
            candidates.append((index, failure))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (_ATTEMPT_FAIL_PRECEDENCE[item[1].code], -item[0]))[1]
