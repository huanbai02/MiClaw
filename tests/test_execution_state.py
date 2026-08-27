"""冻结 Phase 5 ExecutionState 的纯 attempt 生命周期语义。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

from miclaw.core.execution.models import (
    ExecutionState,
    ExecutionStateValidationError,
    ExecutionStatus,
    TERMINAL_EXECUTION_STATUSES,
    is_terminal,
)
from miclaw.core.execution.state import (
    ExecutionTransitionError,
    cancel_execution,
    create_pending_execution,
    mark_execution_failed,
    mark_execution_interrupted,
    mark_execution_succeeded,
    start_execution,
)


START = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)
FINISH = START + timedelta(seconds=1)


def _running() -> ExecutionState:
    """构造固定时间的合法 RUNNING attempt。"""
    return start_execution(create_pending_execution("exec-1"), run_id="run-1", started_at=START)


def test_create_pending_execution_has_fixed_initial_shape():
    """首次 attempt 以未绑定 run/timestamp 的 PENDING state 创建。"""
    state = create_pending_execution("exec-1")

    assert state == ExecutionState("exec-1", 1, ExecutionStatus.PENDING, None, None, None)


@pytest.mark.parametrize("attempt", [0, -1, True, False, 1.0, "1", None])
def test_pending_creation_rejects_invalid_attempts(attempt):
    """attempt 只接受从 1 开始的精确 int。"""
    with pytest.raises(ExecutionStateValidationError, match="^invalid_attempt$"):
        create_pending_execution("exec-1", attempt=attempt)


@pytest.mark.parametrize("execution_id", ["", "  ", [], {}, 1, True, None])
def test_pending_creation_rejects_invalid_execution_identity(execution_id):
    """execution identity 不允许隐式 coercion 或空白值。"""
    with pytest.raises(ExecutionStateValidationError, match="^invalid_execution_id$"):
        create_pending_execution(execution_id)


@pytest.mark.parametrize("run_id", ["", " ", [], {}, 1, True, None])
def test_start_rejects_invalid_run_identity(run_id):
    """RUNNING attempt 必须绑定精确、非空的 trace run identity。"""
    with pytest.raises(ExecutionStateValidationError, match="^invalid_run_id$"):
        start_execution(create_pending_execution("exec-1"), run_id=run_id, started_at=START)


def test_successful_lifecycle_returns_new_immutable_states():
    """PENDING → RUNNING → SUCCEEDED 不修改旧 state。"""
    pending = create_pending_execution("exec-1")
    running = start_execution(pending, run_id="run-1", started_at=START)
    succeeded = mark_execution_succeeded(running, finished_at=FINISH)

    assert pending.status is ExecutionStatus.PENDING
    assert running.status is ExecutionStatus.RUNNING
    assert succeeded.status is ExecutionStatus.SUCCEEDED
    assert succeeded.finished_at == FINISH
    assert pending is not running and running is not succeeded


@pytest.mark.parametrize(
    "finish",
    [mark_execution_succeeded, mark_execution_failed, mark_execution_interrupted],
)
def test_running_terminal_lifecycles_cannot_reopen(finish):
    """所有 RUNNING terminal transition 均结束 attempt；future retry 必须创建新 attempt。"""
    terminal = finish(_running(), finished_at=FINISH)

    assert is_terminal(terminal.status)
    with pytest.raises(ExecutionTransitionError, match="^invalid_execution_transition$"):
        start_execution(terminal, run_id="run-2", started_at=FINISH)
    with pytest.raises(ExecutionTransitionError, match="^invalid_execution_transition$"):
        cancel_execution(terminal, finished_at=FINISH)
    for transition in (mark_execution_succeeded, mark_execution_failed, mark_execution_interrupted):
        with pytest.raises(ExecutionTransitionError, match="^invalid_execution_transition$"):
            transition(terminal, finished_at=FINISH)


def test_cancel_supports_pending_and_running_without_reopening_terminal_state():
    """取消可发生在开始前或运行中，且两种结果都是 terminal。"""
    pending_cancelled = cancel_execution(create_pending_execution("pending"), finished_at=FINISH)
    running_cancelled = cancel_execution(_running(), finished_at=FINISH)

    assert (pending_cancelled.run_id, pending_cancelled.started_at, pending_cancelled.finished_at) == (None, None, FINISH)
    assert running_cancelled.run_id == "run-1"
    assert running_cancelled.started_at == START
    assert running_cancelled.finished_at == FINISH
    assert is_terminal(pending_cancelled.status)
    with pytest.raises(ExecutionTransitionError, match="^invalid_execution_transition$"):
        cancel_execution(running_cancelled, finished_at=FINISH)


def test_illegal_source_transitions_are_rejected():
    """PENDING 不能直接终结，RUNNING 也不能重复 start。"""
    pending = create_pending_execution("exec-1")
    with pytest.raises(ExecutionTransitionError, match="^invalid_execution_transition$"):
        mark_execution_succeeded(pending, finished_at=FINISH)
    with pytest.raises(ExecutionTransitionError, match="^invalid_execution_transition$"):
        mark_execution_failed(pending, finished_at=FINISH)
    with pytest.raises(ExecutionTransitionError, match="^invalid_execution_transition$"):
        mark_execution_interrupted(pending, finished_at=FINISH)
    with pytest.raises(ExecutionTransitionError, match="^invalid_execution_transition$"):
        start_execution(_running(), run_id="run-2", started_at=FINISH)


def test_timestamp_rules_reject_naive_or_reversed_values_and_allow_equality():
    """timestamps 必须 timezone-aware；同刻开始/结束合法，倒序非法。"""
    pending = create_pending_execution("exec-1")
    with pytest.raises(ExecutionStateValidationError, match="^invalid_execution_timestamp$"):
        start_execution(pending, run_id="run-1", started_at=datetime(2026, 8, 27, 8, 0))

    running = _running()
    assert mark_execution_succeeded(running, finished_at=START).finished_at == START
    with pytest.raises(ExecutionStateValidationError, match="^invalid_execution_timestamp$"):
        mark_execution_succeeded(running, finished_at=START - timedelta(microseconds=1))


@pytest.mark.parametrize(
    "error",
    [RuntimeError("UNTRUSTED_TIMEZONE_DETAIL"), ValueError("SECRET_TIMEZONE_DETAIL")],
)
def test_timezone_callback_errors_are_mapped_to_stable_timestamp_error(error):
    """timezone callback 不得把任意异常类型或详情带出 execution domain。"""

    class ExplodingTZ(tzinfo):
        def utcoffset(self, _dt):
            raise error

    timestamp = datetime(2026, 8, 27, 8, 0, tzinfo=ExplodingTZ())
    with pytest.raises(ExecutionStateValidationError, match="^invalid_execution_timestamp$") as raised:
        start_execution(create_pending_execution("exec-1"), run_id="run-1", started_at=timestamp)

    assert str(raised.value) == "invalid_execution_timestamp"
    assert str(error) not in str(raised.value)


def test_timestamp_ordering_callback_errors_are_mapped_to_stable_timestamp_error():
    """完成时间排序比较中的 tzinfo callback 异常同样不会逸出。"""

    class OrderingExplodingTZ(tzinfo):
        def __init__(self) -> None:
            self.calls = 0

        def utcoffset(self, _dt):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("ORDERING_TIMEZONE_DETAIL")
            return timedelta(0)

    started_at = datetime(2026, 8, 27, 8, 0, tzinfo=OrderingExplodingTZ())
    finished_at = datetime(2026, 8, 27, 8, 1, tzinfo=OrderingExplodingTZ())
    with pytest.raises(ExecutionStateValidationError, match="^invalid_execution_timestamp$") as raised:
        ExecutionState("exec-1", 1, ExecutionStatus.SUCCEEDED, "run-1", started_at, finished_at)

    assert str(raised.value) == "invalid_execution_timestamp"
    assert "ORDERING_TIMEZONE_DETAIL" not in str(raised.value)


def test_datetime_subclasses_are_rejected_without_executing_custom_behavior():
    """exact datetime policy 拒绝 subclass，避免 custom comparison 或 timezone surface。"""

    class CustomDatetime(datetime):
        pass

    timestamp = CustomDatetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)
    with pytest.raises(ExecutionStateValidationError, match="^invalid_execution_timestamp$"):
        start_execution(create_pending_execution("exec-1"), run_id="run-1", started_at=timestamp)


def test_fixed_offset_datetime_remains_valid():
    """exact datetime 仍接受合法 fixed-offset tzinfo。"""
    timestamp = datetime(2026, 8, 27, 8, 0, tzinfo=timezone(timedelta(hours=8)))

    assert start_execution(create_pending_execution("exec-1"), run_id="run-1", started_at=timestamp).started_at == timestamp


@pytest.mark.parametrize(
    "state",
    [
        ("exec", 1, ExecutionStatus.PENDING, "run", None, None),
        ("exec", 1, ExecutionStatus.PENDING, None, START, None),
        ("exec", 1, ExecutionStatus.RUNNING, None, START, None),
        ("exec", 1, ExecutionStatus.RUNNING, "run", START, FINISH),
        ("exec", 1, ExecutionStatus.SUCCEEDED, "run", None, FINISH),
        ("exec", 1, ExecutionStatus.FAILED, "run", START, None),
        ("exec", 1, ExecutionStatus.INTERRUPTED, None, START, FINISH),
        ("exec", 1, ExecutionStatus.CANCELLED, None, None, None),
    ],
)
def test_direct_inconsistent_states_are_rejected(state):
    """公开 dataclass 也不能绕过状态组合校验。"""
    with pytest.raises(ExecutionStateValidationError, match="^invalid_execution_state$"):
        ExecutionState(*state)


def test_direct_state_rejects_non_enum_status():
    """ExecutionStatus 不接受字符串等 persistence-deserialization coercion。"""
    with pytest.raises(ExecutionStateValidationError, match="^invalid_execution_status$"):
        ExecutionState("exec", 1, "pending", None, None, None)


def test_terminal_status_set_is_exactly_the_first_version_contract():
    """只有四个已结束 status 被定义为 terminal。"""
    assert not is_terminal(ExecutionStatus.PENDING)
    assert not is_terminal(ExecutionStatus.RUNNING)
    assert {status.value for status in TERMINAL_EXECUTION_STATUSES} == {
        "succeeded",
        "failed",
        "interrupted",
        "cancelled",
    }


def test_execution_package_has_no_runtime_domain_dependencies():
    """第一版 state model 只依赖标准库与同 package models。"""
    source = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            Path(__file__).parents[1] / "miclaw/core/execution/models.py",
            Path(__file__).parents[1] / "miclaw/core/execution/state.py",
        )
    )

    for forbidden in ("agent", "scheduler", "memory", "mcp", "logger", "permission", "config", "workspace"):
        assert f"core.{forbidden}" not in source
