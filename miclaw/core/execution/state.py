"""提供 ExecutionState 的纯创建与合法状态转换。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .models import ExecutionState, ExecutionStatus, ExecutionStateValidationError


class ExecutionTransitionError(ValueError):
    """表示不含 identity/detail 的稳定非法状态转换。"""


def create_pending_execution(
    execution_id: str,
    *,
    attempt: int = 1,
) -> ExecutionState:
    """创建尚未启动的 execution attempt。

    Args:
        execution_id: 逻辑 execution 的 stable runtime identity。
        attempt: 从 1 开始的 attempt number。
    """
    return ExecutionState(execution_id, attempt, ExecutionStatus.PENDING, None, None, None)


def start_execution(
    state: ExecutionState,
    *,
    run_id: str,
    started_at: datetime,
) -> ExecutionState:
    """将 PENDING attempt 转换为 RUNNING。

    Args:
        state: 当前 PENDING attempt。
        run_id: 本 attempt 对应的 existing trace run identity。
        started_at: timezone-aware 开始时间。
    """
    _require_status(state, ExecutionStatus.PENDING)
    if type(run_id) is not str or not run_id.strip():
        raise ExecutionStateValidationError("invalid_run_id")
    return replace(state, status=ExecutionStatus.RUNNING, run_id=run_id, started_at=started_at)


def mark_execution_succeeded(state: ExecutionState, *, finished_at: datetime) -> ExecutionState:
    """将 RUNNING attempt 标记为成功结束。"""
    return _finish_running_execution(state, ExecutionStatus.SUCCEEDED, finished_at)


def mark_execution_failed(state: ExecutionState, *, finished_at: datetime) -> ExecutionState:
    """将 RUNNING attempt 标记为失败结束。"""
    return _finish_running_execution(state, ExecutionStatus.FAILED, finished_at)


def mark_execution_interrupted(state: ExecutionState, *, finished_at: datetime) -> ExecutionState:
    """将 RUNNING attempt 标记为异常中断。"""
    return _finish_running_execution(state, ExecutionStatus.INTERRUPTED, finished_at)


def cancel_execution(state: ExecutionState, *, finished_at: datetime) -> ExecutionState:
    """取消 PENDING 或 RUNNING attempt，terminal attempt 不可重复取消。"""
    _require_state(state)
    if state.status not in {ExecutionStatus.PENDING, ExecutionStatus.RUNNING}:
        raise ExecutionTransitionError("invalid_execution_transition")
    return replace(state, status=ExecutionStatus.CANCELLED, finished_at=finished_at)


def _finish_running_execution(
    state: ExecutionState,
    status: ExecutionStatus,
    finished_at: datetime,
) -> ExecutionState:
    """完成 RUNNING attempt 的共享终结转换。"""
    _require_status(state, ExecutionStatus.RUNNING)
    return replace(state, status=status, finished_at=finished_at)


def _require_status(state: ExecutionState, expected: ExecutionStatus) -> None:
    """验证输入 state 后要求精确的 source status。"""
    _require_state(state)
    if state.status is not expected:
        raise ExecutionTransitionError("invalid_execution_transition")


def _require_state(state: ExecutionState) -> None:
    """将 runtime helper 的错误边界收敛为稳定 domain validation error。"""
    if type(state) is not ExecutionState:
        raise ExecutionStateValidationError("invalid_execution_state")
