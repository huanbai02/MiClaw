"""SQLite durable storage for execution metadata; never stores graph state or payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from ..execution.failures import ExecutionFailure, ExecutionFailureCode, ExecutionFailureSource
from ..execution.models import ExecutionState, ExecutionStateValidationError, ExecutionStatus, validate_execution_state
from ..execution.recovery import ExecutionAttemptRecord
from ..execution.retry import RetryDecision, RetryDecisionReason, RetryEvaluation
from ..execution.state import is_legal_execution_transition


class ExecutionStoreError(RuntimeError):
    """Execution metadata storage 的稳定、安全错误边界。"""


class ExecutionStore:
    """持久化 attempt 元数据，并以 expected-status CAS 镜像领域状态机。"""

    def __init__(self, path: str | Path) -> None:
        """打开独立 execution SQLite 数据库并初始化 schema。"""
        if type(path) is not str and not isinstance(path, Path):
            raise ExecutionStoreError("execution_store_error")
        try:
            self._connection = sqlite3.connect(str(path))
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_attempts (
                    execution_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    run_id TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    failure_source TEXT,
                    failure_code TEXT,
                    retry_decision TEXT,
                    retry_reason TEXT,
                    checkpoint_thread_id TEXT,
                    checkpoint_run_id TEXT,
                    checkpoint_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (execution_id, attempt)
                )
                """
            )
            self._connection.commit()
        except (sqlite3.Error, OSError):
            raise ExecutionStoreError("execution_store_error") from None

    def close(self) -> None:
        """关闭数据库连接。"""
        self._connection.close()

    def __enter__(self) -> ExecutionStore:
        """支持同步 context manager。"""
        return self

    def __exit__(self, *_: object) -> None:
        """离开 context 时关闭连接。"""
        self.close()

    def create_attempt(self, record: ExecutionAttemptRecord) -> None:
        """插入唯一 PENDING attempt；重复 identity 必须稳定失败。"""
        if type(record) is not ExecutionAttemptRecord or record.state.status is not ExecutionStatus.PENDING:
            raise ExecutionStoreError("invalid_execution_record")
        try:
            with self._connection:
                self._insert(record)
        except sqlite3.IntegrityError:
            raise ExecutionStoreError("execution_record_conflict") from None
        except sqlite3.Error:
            raise ExecutionStoreError("execution_store_error") from None

    def transition(self, record: ExecutionAttemptRecord, *, expected_status: ExecutionStatus) -> None:
        """以 PR47 合法矩阵 + expected previous status CAS 持久化一个转换。"""
        _validate_store_transition(record, expected_status)
        try:
            with self._connection:
                if self._update(record, expected_status) != 1:
                    raise ExecutionStoreError("execution_record_conflict")
        except ExecutionStoreError:
            raise
        except sqlite3.Error:
            raise ExecutionStoreError("execution_store_error") from None

    def fail_and_plan_next(self, failed: ExecutionAttemptRecord, next_attempt: ExecutionAttemptRecord | None) -> None:
        """原子保存 FAILED attempt 与可选下一 PENDING attempt。"""
        _validate_store_transition(failed, ExecutionStatus.RUNNING)
        _validate_next_attempt(failed, next_attempt)
        try:
            with self._connection:
                if self._update(failed, ExecutionStatus.RUNNING) != 1:
                    raise ExecutionStoreError("execution_record_conflict")
                if next_attempt is not None:
                    self._insert(next_attempt)
        except ExecutionStoreError:
            raise
        except sqlite3.IntegrityError:
            raise ExecutionStoreError("execution_record_conflict") from None
        except sqlite3.Error:
            raise ExecutionStoreError("execution_store_error") from None

    def interrupt_and_create_next(self, interrupted: ExecutionAttemptRecord, next_attempt: ExecutionAttemptRecord | None) -> None:
        """原子中断 RUNNING attempt，并仅在安全恢复时创建下一 PENDING attempt。"""
        _validate_store_transition(interrupted, ExecutionStatus.RUNNING)
        _validate_next_attempt(interrupted, next_attempt)
        try:
            with self._connection:
                if self._update(interrupted, ExecutionStatus.RUNNING) != 1:
                    raise ExecutionStoreError("execution_record_conflict")
                if next_attempt is not None:
                    self._insert(next_attempt)
        except ExecutionStoreError:
            raise
        except sqlite3.IntegrityError:
            raise ExecutionStoreError("execution_record_conflict") from None
        except sqlite3.Error:
            raise ExecutionStoreError("execution_store_error") from None

    def get_attempt(self, execution_id: str, attempt: int) -> ExecutionAttemptRecord:
        """严格反序列化一个 persisted attempt。"""
        if type(execution_id) is not str or not execution_id.strip() or type(attempt) is not int or attempt < 1:
            raise ExecutionStoreError("invalid_execution_record")
        try:
            row = self._connection.execute(
                "SELECT execution_id, attempt, status, run_id, started_at, finished_at, "
                "failure_source, failure_code, retry_decision, retry_reason, "
                "checkpoint_thread_id, checkpoint_run_id, checkpoint_id "
                "FROM execution_attempts WHERE execution_id = ? AND attempt = ?",
                (execution_id, attempt),
            ).fetchone()
        except sqlite3.Error:
            raise ExecutionStoreError("execution_store_error") from None
        if row is None:
            raise ExecutionStoreError("execution_record_not_found")
        return _record_from_row(row)

    def _insert(self, record: ExecutionAttemptRecord) -> None:
        """执行不覆盖既有 attempt 的 INSERT。"""
        now = _timestamp()
        self._connection.execute(
            """
            INSERT INTO execution_attempts (
                execution_id, attempt, status, run_id, started_at, finished_at,
                failure_source, failure_code, retry_decision, retry_reason,
                checkpoint_thread_id, checkpoint_run_id, checkpoint_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _record_values(record, now, now),
        )

    def _update(self, record: ExecutionAttemptRecord, expected_status: ExecutionStatus) -> int:
        """按 `(execution_id, attempt, expected_status)` 更新并返回受影响行数。"""
        now = _timestamp()
        values = _record_values(record, None, now)
        cursor = self._connection.execute(
            """
            UPDATE execution_attempts SET
                status = ?, run_id = ?, started_at = ?, finished_at = ?,
                failure_source = ?, failure_code = ?, retry_decision = ?, retry_reason = ?,
                checkpoint_thread_id = ?, checkpoint_run_id = ?, checkpoint_id = ?, updated_at = ?
            WHERE execution_id = ? AND attempt = ? AND status = ?
            """,
            values[2:13] + (values[14], record.state.execution_id, record.state.attempt, expected_status.value),
        )
        return cursor.rowcount


def _validate_store_transition(record: ExecutionAttemptRecord, expected_status: ExecutionStatus) -> None:
    """复用 PR47 状态矩阵并确保 new state 本身仍满足完整领域校验。"""
    if type(record) is not ExecutionAttemptRecord or type(expected_status) is not ExecutionStatus:
        raise ExecutionStoreError("invalid_execution_record")
    try:
        validate_execution_state(record.state)
    except ExecutionStateValidationError:
        raise ExecutionStoreError("invalid_execution_record") from None
    if not is_legal_execution_transition(expected_status, record.state.status):
        raise ExecutionStoreError("invalid_execution_transition")


def _validate_next_attempt(current: ExecutionAttemptRecord, next_attempt: ExecutionAttemptRecord | None) -> None:
    """验证原子创建的下一 attempt 只能是同 execution 的连续 PENDING state。"""
    if next_attempt is None:
        return
    if type(next_attempt) is not ExecutionAttemptRecord:
        raise ExecutionStoreError("invalid_execution_record")
    try:
        validate_execution_state(next_attempt.state)
    except ExecutionStateValidationError:
        raise ExecutionStoreError("invalid_execution_record") from None
    current_state = current.state
    next_state = next_attempt.state
    if (
        next_state.status is not ExecutionStatus.PENDING
        or next_state.execution_id != current_state.execution_id
        or next_state.attempt != current_state.attempt + 1
    ):
        raise ExecutionStoreError("invalid_execution_transition")


def _record_values(record: ExecutionAttemptRecord, created_at: str | None, updated_at: str) -> tuple[object, ...]:
    """把安全领域 record 展开为 SQL 参数，不包含任何 graph payload。"""
    failure = record.failure
    evaluation = record.retry_evaluation
    state = record.state
    return (
        state.execution_id,
        state.attempt,
        state.status.value,
        state.run_id,
        _serialize_timestamp(state.started_at),
        _serialize_timestamp(state.finished_at),
        failure.source.value if failure else None,
        failure.code.value if failure else None,
        evaluation.decision.value if evaluation else None,
        evaluation.reason.value if evaluation else None,
        record.checkpoint_thread_id,
        record.checkpoint_run_id,
        record.checkpoint_id,
        created_at,
        updated_at,
    )


def _record_from_row(row: tuple[object, ...]) -> ExecutionAttemptRecord:
    """严格从 SQLite 基础值重建领域对象，未知值一律 fail closed。"""
    try:
        (
            execution_id, attempt, status, run_id, started_at, finished_at,
            failure_source, failure_code, retry_decision, retry_reason,
            checkpoint_thread_id, checkpoint_run_id, checkpoint_id,
        ) = row
        if type(status) is not str or type(attempt) is not int:
            raise ValueError
        state = ExecutionState(
            execution_id,
            attempt,
            ExecutionStatus(status),
            run_id,
            _parse_timestamp(started_at),
            _parse_timestamp(finished_at),
        )
        if (failure_source is None) != (failure_code is None) or (retry_decision is None) != (retry_reason is None):
            raise ValueError
        failure = None if failure_source is None else ExecutionFailure(
            ExecutionFailureSource(failure_source), ExecutionFailureCode(failure_code)
        )
        evaluation = None if retry_decision is None else RetryEvaluation(
            RetryDecision(retry_decision), RetryDecisionReason(retry_reason)
        )
        return ExecutionAttemptRecord(
            state,
            failure,
            evaluation,
            checkpoint_thread_id,
            checkpoint_run_id,
            checkpoint_id,
        )
    except (TypeError, ValueError):
        raise ExecutionStoreError("invalid_execution_record") from None


def _serialize_timestamp(value: datetime | None) -> str | None:
    """将已由 ExecutionState 验证的 aware timestamp 规范化为 UTC ISO-8601。"""
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _parse_timestamp(value: object) -> datetime | None:
    """严格解析 SQLite ISO timestamp，随后交由 ExecutionState 再验证。"""
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError
    return parsed


def _timestamp() -> str:
    """生成 metadata 行的 UTC timestamp。"""
    return datetime.now(timezone.utc).isoformat()
