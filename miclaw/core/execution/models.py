"""定义纯 Execution attempt 的不可变状态与一致性规则。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ExecutionStatus(str, Enum):
    """当前 execution attempt 的稳定生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
        ExecutionStatus.INTERRUPTED,
        ExecutionStatus.CANCELLED,
    }
)


class ExecutionStateValidationError(ValueError):
    """表示不含 execution identity/detail 的稳定状态校验失败。"""


@dataclass(frozen=True, slots=True)
class ExecutionState:
    """描述一个逻辑 execution 的单次 attempt。

    Args:
        execution_id: 逻辑 execution 的 runtime-controlled stable identity。
        attempt: 从 1 开始的实际执行尝试编号。
        status: 当前 attempt 的生命周期状态。
        run_id: 当前 attempt 对应的 TraceContext run identity。
        started_at: attempt 开始的 timezone-aware 时间。
        finished_at: attempt 结束的 timezone-aware 时间。
    """

    execution_id: str
    attempt: int
    status: ExecutionStatus
    run_id: str | None
    started_at: datetime | None
    finished_at: datetime | None

    def __post_init__(self) -> None:
        """拒绝任何绕过 transition helper 构造的不一致状态。"""
        validate_execution_state(self)


def is_terminal(status: ExecutionStatus) -> bool:
    """判断 status 是否表示已结束的 attempt。"""
    if type(status) is not ExecutionStatus:
        raise ExecutionStateValidationError("invalid_execution_status")
    return status in TERMINAL_EXECUTION_STATUSES


def validate_execution_state(state: ExecutionState) -> None:
    """严格验证 ExecutionState 的 identity、时间与状态组合。

    Args:
        state: 待验证的 execution attempt state。

    Raises:
        ExecutionStateValidationError: 任一字段或状态组合不符合第一版 contract 时抛出。
    """
    if type(state) is not ExecutionState:
        raise ExecutionStateValidationError("invalid_execution_state")
    _validate_identifier(state.execution_id, "invalid_execution_id")
    if type(state.attempt) is not int or state.attempt < 1:
        raise ExecutionStateValidationError("invalid_attempt")
    if type(state.status) is not ExecutionStatus:
        raise ExecutionStateValidationError("invalid_execution_status")
    if state.run_id is not None:
        _validate_identifier(state.run_id, "invalid_run_id")
    _validate_timestamp(state.started_at)
    _validate_timestamp(state.finished_at)

    if state.status is ExecutionStatus.PENDING:
        _validate_pending(state)
    elif state.status is ExecutionStatus.RUNNING:
        _validate_running(state)
    elif state.status in {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
        ExecutionStatus.INTERRUPTED,
    }:
        _validate_completed(state)
    else:
        _validate_cancelled(state)

    if state.started_at is not None and state.finished_at is not None and not _timestamps_are_ordered(
        state.started_at,
        state.finished_at,
    ):
        raise ExecutionStateValidationError("invalid_execution_timestamp")


def _validate_identifier(value: object, error_code: str) -> None:
    """验证 runtime identity 不接受隐式 coercion、空值或纯空白。"""
    if type(value) is not str or not value.strip():
        raise ExecutionStateValidationError(error_code)


def _validate_timestamp(value: datetime | None) -> None:
    """验证时间为 timezone-aware datetime，None 表示该阶段尚未发生。"""
    if value is None:
        return
    if type(value) is not datetime:
        raise ExecutionStateValidationError("invalid_execution_timestamp")
    try:
        is_aware = value.tzinfo is not None and value.utcoffset() is not None
    except Exception:
        raise ExecutionStateValidationError("invalid_execution_timestamp") from None
    if not is_aware:
        raise ExecutionStateValidationError("invalid_execution_timestamp")


def _timestamps_are_ordered(started_at: datetime, finished_at: datetime) -> bool:
    """安全比较两个已验证时间，隔离 tzinfo callback 的异常。"""
    try:
        return finished_at >= started_at
    except Exception:
        raise ExecutionStateValidationError("invalid_execution_timestamp") from None


def _validate_pending(state: ExecutionState) -> None:
    """验证未开始 attempt 不绑定 run 或时间。"""
    if state.run_id is not None or state.started_at is not None or state.finished_at is not None:
        raise ExecutionStateValidationError("invalid_execution_state")


def _validate_running(state: ExecutionState) -> None:
    """验证运行中 attempt 已绑定 run/start，尚未结束。"""
    if state.run_id is None or state.started_at is None or state.finished_at is not None:
        raise ExecutionStateValidationError("invalid_execution_state")


def _validate_completed(state: ExecutionState) -> None:
    """验证成功、失败或中断 attempt 具有完整运行时间范围。"""
    if state.run_id is None or state.started_at is None or state.finished_at is None:
        raise ExecutionStateValidationError("invalid_execution_state")


def _validate_cancelled(state: ExecutionState) -> None:
    """验证取消可发生在开始前或运行中，但必须有结束时间。"""
    if state.finished_at is None:
        raise ExecutionStateValidationError("invalid_execution_state")
    if state.started_at is None and state.run_id is not None:
        raise ExecutionStateValidationError("invalid_execution_state")
    if state.started_at is not None and state.run_id is None:
        raise ExecutionStateValidationError("invalid_execution_state")
