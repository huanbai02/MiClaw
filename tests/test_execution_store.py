"""验证 execution.sqlite3 只保存 lifecycle metadata，并以 CAS 保护状态转换。"""

from datetime import datetime, timezone
import sqlite3

import pytest

from miclaw.core.execution.failures import ExecutionFailure, ExecutionFailureCode, ExecutionFailureSource
from miclaw.core.execution.models import ExecutionStatus
from miclaw.core.execution.recovery import ExecutionAttemptRecord
from miclaw.core.execution.retry import RetryDecision, RetryDecisionReason, RetryEvaluation
from miclaw.core.execution.state import (
    cancel_execution,
    create_pending_execution,
    mark_execution_failed,
    mark_execution_interrupted,
    mark_execution_succeeded,
    start_execution,
)
from miclaw.core.runtime.execution_store import ExecutionStore, ExecutionStoreError


def _running_record() -> tuple[ExecutionAttemptRecord, datetime]:
    """构造一个已由领域 helper 验证的 RUNNING record。"""
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state = start_execution(create_pending_execution("execution-1"), run_id="trace-1", started_at=started_at)
    return ExecutionAttemptRecord(state, checkpoint_thread_id="thread-1", checkpoint_run_id="checkpoint-run-1"), started_at


def test_execution_store_round_trip_and_transactional_failure_plan(tmp_path):
    """FAILED 与下一 PENDING attempt 必须在一个 transaction 内持久化。"""
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    running, started_at = _running_record()
    pending = create_pending_execution("execution-1")
    store.create_attempt(ExecutionAttemptRecord(pending, checkpoint_thread_id="thread-1", checkpoint_run_id="checkpoint-run-1"))
    store.transition(running, expected_status=ExecutionStatus.PENDING)

    failed_state = mark_execution_failed(running.state, finished_at=started_at)
    failure = ExecutionFailure(ExecutionFailureSource.PROVIDER, ExecutionFailureCode.PROVIDER_TIMEOUT)
    evaluation = RetryEvaluation(RetryDecision.RETRY, RetryDecisionReason.RETRYABLE_FAILURE)
    failed = ExecutionAttemptRecord(
        failed_state,
        failure,
        evaluation,
        "thread-1",
        "checkpoint-run-1",
        "checkpoint-1",
    )
    next_attempt = ExecutionAttemptRecord(create_pending_execution("execution-1", attempt=2))
    store.fail_and_plan_next(failed, next_attempt)

    assert store.get_attempt("execution-1", 1) == failed
    assert store.get_attempt("execution-1", 2) == next_attempt
    with pytest.raises(ExecutionStoreError, match="^execution_record_conflict$"):
        store.transition(failed, expected_status=ExecutionStatus.RUNNING)
    store.close()


def test_execution_store_read_queries_decode_records_and_do_not_mutate(tmp_path):
    """控制面查询复用 Store 解码，并按 logical execution 最新 attempt 返回。"""
    path = tmp_path / "execution.sqlite3"
    store = ExecutionStore(path)
    pending_a = ExecutionAttemptRecord(create_pending_execution("execution-a"))
    store.create_attempt(pending_a)
    _, running, at = _persist_running(store, "execution-b")
    failed = ExecutionAttemptRecord(
        mark_execution_failed(running, finished_at=at),
        ExecutionFailure(ExecutionFailureSource.PROVIDER, ExecutionFailureCode.PROVIDER_TIMEOUT),
        RetryEvaluation(RetryDecision.RETRY, RetryDecisionReason.RETRYABLE_FAILURE),
    )
    store.fail_and_plan_next(failed, ExecutionAttemptRecord(create_pending_execution("execution-b", attempt=2)))
    store.close()

    readonly = ExecutionStore(path, readonly=True)
    assert [record.state.attempt for record in readonly.list_attempts("execution-b")] == [1, 2]
    assert [(record.state.execution_id, record.state.attempt) for record in readonly.list_latest_attempts(limit=20)] == [
        ("execution-b", 2),
        ("execution-a", 1),
    ]
    readonly.close()


def test_execution_store_rejects_duplicate_insert_and_invalid_db_enum_without_payload_leak(tmp_path):
    """禁止覆盖 attempt；未知 DB enum 必须稳定 fail closed 且不回显 row。"""
    path = tmp_path / "execution.sqlite3"
    store = ExecutionStore(path)
    pending = ExecutionAttemptRecord(create_pending_execution("execution-1"))
    store.create_attempt(pending)
    with pytest.raises(ExecutionStoreError, match="^execution_record_conflict$"):
        store.create_attempt(pending)
    store.close()

    connection = sqlite3.connect(path)
    connection.execute("UPDATE execution_attempts SET status = ?", ("SECRET_UNKNOWN_STATUS",))
    connection.commit()
    connection.close()
    reopened = ExecutionStore(path)
    with pytest.raises(ExecutionStoreError, match="^invalid_execution_record$") as exc_info:
        reopened.get_attempt("execution-1", 1)
    assert "SECRET_UNKNOWN_STATUS" not in str(exc_info.value)
    reopened.close()


def test_execution_database_never_contains_graph_payload_sentinel(tmp_path):
    """schema 与 record values 不提供写入 prompt/message/tool content 的通道。"""
    path = tmp_path / "execution.sqlite3"
    store = ExecutionStore(path)
    store.create_attempt(ExecutionAttemptRecord(create_pending_execution("execution-1")))
    store.close()
    assert b"EXECUTION_DB_SECRET_PAYLOAD" not in path.read_bytes()


def _persist_running(store: ExecutionStore, execution_id: str = "execution-a"):
    """写入并启动一个 attempt，供 durable transition 矩阵测试复用。"""
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pending = create_pending_execution(execution_id)
    running = start_execution(pending, run_id="trace-1", started_at=started_at)
    store.create_attempt(ExecutionAttemptRecord(pending))
    store.transition(ExecutionAttemptRecord(running), expected_status=ExecutionStatus.PENDING)
    return pending, running, started_at


@pytest.mark.parametrize(
    ("terminal_status", "finish"),
    [
        (ExecutionStatus.SUCCEEDED, lambda state, at: mark_execution_succeeded(state, finished_at=at)),
        (ExecutionStatus.FAILED, lambda state, at: mark_execution_failed(state, finished_at=at)),
        (ExecutionStatus.INTERRUPTED, lambda state, at: mark_execution_interrupted(state, finished_at=at)),
        (ExecutionStatus.CANCELLED, lambda state, at: cancel_execution(state, finished_at=at)),
    ],
)
def test_durable_terminal_attempt_cannot_reopen_or_change_terminal_status(tmp_path, terminal_status, finish):
    """terminal attempt 的 durable 行不能被 CAS 伪装成新的 lifecycle。"""
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    pending, running, at = _persist_running(store)
    terminal = finish(running, at)
    expected = ExecutionStatus.RUNNING
    store.transition(ExecutionAttemptRecord(terminal), expected_status=expected)

    with pytest.raises(ExecutionStoreError, match="^invalid_execution_transition$"):
        store.transition(ExecutionAttemptRecord(running), expected_status=terminal_status)
    alternate = mark_execution_failed(running, finished_at=at)
    if terminal_status is ExecutionStatus.FAILED:
        alternate = mark_execution_succeeded(running, finished_at=at)
    with pytest.raises(ExecutionStoreError, match="^invalid_execution_transition$"):
        store.transition(ExecutionAttemptRecord(alternate), expected_status=terminal_status)
    assert store.get_attempt("execution-a", 1).state.status is terminal_status
    store.close()


def test_store_accepts_complete_pr47_legal_transition_matrix(tmp_path):
    """Store hardening 不得阻断 PR47 的全部 six legal transition。"""
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    pending = create_pending_execution("pending-running")
    store.create_attempt(ExecutionAttemptRecord(pending))
    running = start_execution(pending, run_id="trace", started_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    store.transition(ExecutionAttemptRecord(running), expected_status=ExecutionStatus.PENDING)
    assert store.get_attempt("pending-running", 1).state.status is ExecutionStatus.RUNNING

    cancelled_pending = create_pending_execution("pending-cancelled")
    store.create_attempt(ExecutionAttemptRecord(cancelled_pending))
    store.transition(
        ExecutionAttemptRecord(cancel_execution(cancelled_pending, finished_at=datetime(2026, 1, 1, tzinfo=timezone.utc))),
        expected_status=ExecutionStatus.PENDING,
    )

    finishes = (mark_execution_succeeded, mark_execution_failed, mark_execution_interrupted, cancel_execution)
    for index, finish in enumerate(finishes, start=1):
        _, active, at = _persist_running(store, f"running-{index}")
        store.transition(ExecutionAttemptRecord(finish(active, finished_at=at)), expected_status=ExecutionStatus.RUNNING)
    store.close()


def test_store_rejects_illegal_nonterminal_transition_pairs_without_mutation(tmp_path):
    """CAS expected-status 不能放宽 PENDING/RUNNING 的非法 domain transition。"""
    from miclaw.core.execution.models import ExecutionState

    store = ExecutionStore(tmp_path / "execution.sqlite3")
    pending = create_pending_execution("pending")
    store.create_attempt(ExecutionAttemptRecord(pending))
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for status in (ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.INTERRUPTED):
        invalid = ExecutionState("pending", 1, status, "trace", timestamp, timestamp)
        with pytest.raises(ExecutionStoreError, match="^invalid_execution_transition$"):
            store.transition(ExecutionAttemptRecord(invalid), expected_status=ExecutionStatus.PENDING)
    assert store.get_attempt("pending", 1).state.status is ExecutionStatus.PENDING

    _, running, _ = _persist_running(store, "running")
    with pytest.raises(ExecutionStoreError, match="^invalid_execution_transition$"):
        store.transition(ExecutionAttemptRecord(running), expected_status=ExecutionStatus.RUNNING)
    assert store.get_attempt("running", 1).state.status is ExecutionStatus.RUNNING
    store.close()


@pytest.mark.parametrize("method_name", ["fail_and_plan_next", "interrupt_and_create_next"])
@pytest.mark.parametrize(
    "next_state",
    [
        create_pending_execution("execution-b", attempt=2),
        create_pending_execution("execution-a", attempt=1),
        create_pending_execution("execution-a", attempt=3),
        create_pending_execution("execution-a", attempt=99),
    ],
)
def test_atomic_plan_next_rejects_wrong_identity_or_sequence_and_rolls_back(tmp_path, method_name, next_state):
    """两条 atomic plan API 都必须拒绝跨 execution、同号或跳号 next attempt。"""
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    _, running, at = _persist_running(store)
    if method_name == "fail_and_plan_next":
        terminal = mark_execution_failed(running, finished_at=at)
    else:
        terminal = mark_execution_interrupted(running, finished_at=at)
    with pytest.raises(ExecutionStoreError, match="^invalid_execution_transition$"):
        getattr(store, method_name)(ExecutionAttemptRecord(terminal), ExecutionAttemptRecord(next_state))
    assert store.get_attempt("execution-a", 1).state.status is ExecutionStatus.RUNNING
    if (next_state.execution_id, next_state.attempt) != ("execution-a", 1):
        with pytest.raises(ExecutionStoreError, match="^execution_record_not_found$"):
            store.get_attempt(next_state.execution_id, next_state.attempt)
    store.close()


@pytest.mark.parametrize("method_name", ["fail_and_plan_next", "interrupt_and_create_next"])
def test_atomic_plan_next_accepts_exact_same_execution_next_attempt(tmp_path, method_name):
    """两条 atomic plan API 都接受同 execution 的严格 N+1 PENDING attempt。"""
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    _, running, at = _persist_running(store)
    terminal = (
        mark_execution_failed(running, finished_at=at)
        if method_name == "fail_and_plan_next"
        else mark_execution_interrupted(running, finished_at=at)
    )
    next_attempt = ExecutionAttemptRecord(create_pending_execution("execution-a", attempt=2))
    getattr(store, method_name)(ExecutionAttemptRecord(terminal), next_attempt)
    assert store.get_attempt("execution-a", 1).state.status is terminal.status
    assert store.get_attempt("execution-a", 2) == next_attempt
    store.close()
