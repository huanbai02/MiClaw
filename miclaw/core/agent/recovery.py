"""LangGraph checkpoint inspection and targeted safe recovery adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Callable
from uuid import uuid4

from ..execution.models import ExecutionStatus
from ..observability.logger import audit_logger
from ..observability.trace import TraceContext, get_current_trace_context
from ..execution.recovery import (
    CheckpointRef,
    ExecutionAttemptRecord,
    RecoveryAssessment,
    RecoveryDecision,
    RecoveryReason,
)
from ..execution.retry import RetryDecision
from ..execution.state import create_pending_execution, mark_execution_interrupted, mark_execution_succeeded
from ..runtime.execution_store import ExecutionStore, ExecutionStoreError


RECOVERY_SAFE_NEXT_NODES = frozenset({"agent"})


class AgentRecoveryError(RuntimeError):
    """不回显 checkpoint 内容、路径或 LangGraph 内部错误的稳定 recovery 错误。"""


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """定向恢复的安全计划；resume_record 存在时由调用方执行一次 checkpoint continuation。"""

    assessment: RecoveryAssessment
    record: ExecutionAttemptRecord
    resume_record: ExecutionAttemptRecord | None = None


def new_checkpoint_run_id() -> str:
    """生成只用于 LangGraph checkpoint 归属的 opaque correlation token。"""
    return uuid4().hex


def apply_checkpoint_correlation(config: dict[str, object], checkpoint_run_id: str) -> dict[str, object]:
    """合并 checkpoint run correlation，不覆盖 thread/checkpoint/callback 等既有配置。"""
    if type(config) is not dict or type(checkpoint_run_id) is not str or not checkpoint_run_id.strip():
        raise AgentRecoveryError("invalid_checkpoint_config")
    metadata = config.get("metadata", {})
    if type(metadata) is not dict:
        raise AgentRecoveryError("invalid_checkpoint_config")
    merged = dict(config)
    merged_metadata = dict(metadata)
    merged_metadata["run_id"] = checkpoint_run_id
    merged["metadata"] = merged_metadata
    # RunnableConfig.run_id 是本次 LangGraph 调用的 correlation token；TraceContext.run_id 不变。
    merged["run_id"] = checkpoint_run_id
    return merged


def apply_checkpoint_resume_config(
    config: dict[str, object],
    checkpoint_ref: CheckpointRef,
    checkpoint_run_id: str,
) -> dict[str, object]:
    """绑定同一 thread 的精确 checkpoint，并为新的 attempt 注入新 run correlation。"""
    if type(checkpoint_ref) is not CheckpointRef:
        raise AgentRecoveryError("invalid_checkpoint_config")
    merged = apply_checkpoint_correlation(config, checkpoint_run_id)
    configurable = merged.get("configurable", {})
    if type(configurable) is not dict:
        raise AgentRecoveryError("invalid_checkpoint_config")
    merged_configurable = dict(configurable)
    merged_configurable["thread_id"] = checkpoint_ref.thread_id
    merged_configurable["checkpoint_id"] = checkpoint_ref.checkpoint_id
    merged["configurable"] = merged_configurable
    return merged


async def latest_owned_checkpoint(
    graph: object,
    checkpoint_thread_id: str,
    checkpoint_run_id: str,
) -> tuple[CheckpointRef | None, tuple[str, ...] | None]:
    """只从 exact thread + metadata.run_id 找到该 attempt 最新 checkpoint。"""
    if (
        type(checkpoint_thread_id) is not str
        or not checkpoint_thread_id.strip()
        or type(checkpoint_run_id) is not str
        or not checkpoint_run_id.strip()
    ):
        raise AgentRecoveryError("invalid_checkpoint_config")
    try:
        history = graph.aget_state_history({"configurable": {"thread_id": checkpoint_thread_id}})
        async for snapshot in history:
            metadata = getattr(snapshot, "metadata", None)
            config = getattr(snapshot, "config", None)
            if type(metadata) is not dict or type(config) is not dict:
                continue
            if metadata.get("run_id") != checkpoint_run_id:
                continue
            configurable = config.get("configurable")
            if type(configurable) is not dict or configurable.get("thread_id") != checkpoint_thread_id:
                continue
            checkpoint_id = configurable.get("checkpoint_id")
            next_nodes = getattr(snapshot, "next", None)
            if type(checkpoint_id) is not str or not checkpoint_id.strip() or type(next_nodes) is not tuple:
                return None, None
            if any(type(node) is not str for node in next_nodes):
                return None, None
            return CheckpointRef(checkpoint_thread_id, checkpoint_run_id, checkpoint_id), next_nodes
    except AgentRecoveryError:
        raise
    except Exception:
        raise AgentRecoveryError("checkpoint_recovery_error") from None
    return None, ()


async def recover_execution(
    store: ExecutionStore,
    graph: object,
    execution_id: str,
    attempt: int,
    *,
    clock: Callable[[], datetime] | None = None,
    trace_context: TraceContext | None = None,
) -> RecoveryResult:
    """针对一个 known execution attempt 作 safe reconciliation 或 checkpoint continuation planning。"""
    if type(store) is not ExecutionStore:
        raise AgentRecoveryError("invalid_execution_store")
    if trace_context is not None and type(trace_context) is not TraceContext:
        raise AgentRecoveryError("invalid_trace_context")
    if clock is None:
        clock = _utc_now
    active_trace = trace_context if trace_context is not None else get_current_trace_context()

    def logged(result: RecoveryResult) -> RecoveryResult:
        return _log_recovery(result, active_trace)
    try:
        record = store.get_attempt(execution_id, attempt)
    except ExecutionStoreError as exc:
        raise AgentRecoveryError(str(exc)) from None

    resume_record: ExecutionAttemptRecord | None = None
    recovery_source = record
    if record.state.status is ExecutionStatus.FAILED:
        if record.retry_evaluation is None or record.retry_evaluation.decision is not RetryDecision.RETRY:
            return logged(RecoveryResult(
                RecoveryAssessment(RecoveryDecision.DO_NOT_RESUME, RecoveryReason.STATUS_NOT_RECOVERABLE), record
            ))
        try:
            resume_record = store.get_attempt(execution_id, attempt + 1)
        except ExecutionStoreError as exc:
            if str(exc) != "execution_record_not_found":
                raise AgentRecoveryError(str(exc)) from None
            return logged(RecoveryResult(
                RecoveryAssessment(RecoveryDecision.DO_NOT_RESUME, RecoveryReason.STATUS_NOT_RECOVERABLE), record
            ))
        if resume_record.state.status is not ExecutionStatus.PENDING:
            return logged(RecoveryResult(
                RecoveryAssessment(RecoveryDecision.DO_NOT_RESUME, RecoveryReason.STATUS_NOT_RECOVERABLE), record
            ))
    elif record.state.status is not ExecutionStatus.RUNNING:
        return logged(RecoveryResult(
            RecoveryAssessment(RecoveryDecision.DO_NOT_RESUME, RecoveryReason.STATUS_NOT_RECOVERABLE), record
        ))

    if recovery_source.checkpoint_thread_id is None or recovery_source.checkpoint_run_id is None:
        return logged(await _stop_unfinished(store, record, RecoveryReason.NO_MATCHING_CHECKPOINT, clock))
    checkpoint_ref, next_nodes = await latest_owned_checkpoint(
        graph,
        recovery_source.checkpoint_thread_id,
        recovery_source.checkpoint_run_id,
    )
    if checkpoint_ref is None:
        return logged(await _stop_unfinished(store, record, RecoveryReason.MALFORMED_CHECKPOINT if next_nodes is None else RecoveryReason.NO_MATCHING_CHECKPOINT, clock))
    if next_nodes == ():
        if record.state.status is ExecutionStatus.RUNNING:
            succeeded = mark_execution_succeeded(record.state, finished_at=clock())
            reconciled = ExecutionAttemptRecord(
                succeeded,
                checkpoint_thread_id=record.checkpoint_thread_id,
                checkpoint_run_id=record.checkpoint_run_id,
                checkpoint_id=checkpoint_ref.checkpoint_id,
            )
            try:
                store.transition(reconciled, expected_status=ExecutionStatus.RUNNING)
            except ExecutionStoreError as exc:
                raise AgentRecoveryError(str(exc)) from None
            return logged(RecoveryResult(
                RecoveryAssessment(RecoveryDecision.MARK_SUCCEEDED, RecoveryReason.GRAPH_ALREADY_COMPLETE, checkpoint_ref),
                reconciled,
            ))
        return logged(RecoveryResult(
            RecoveryAssessment(RecoveryDecision.DO_NOT_RESUME, RecoveryReason.STATUS_NOT_RECOVERABLE, checkpoint_ref), record
        ))
    if not all(node in RECOVERY_SAFE_NEXT_NODES for node in next_nodes):
        return logged(await _stop_unfinished(store, record, RecoveryReason.UNSAFE_NEXT_NODE, clock, checkpoint_ref))

    assessment = RecoveryAssessment(RecoveryDecision.RESUME_FROM_CHECKPOINT, RecoveryReason.SAFE_CHECKPOINT_AVAILABLE, checkpoint_ref)
    if record.state.status is ExecutionStatus.FAILED:
        assert resume_record is not None
        return logged(RecoveryResult(assessment, record, resume_record))

    interrupted = mark_execution_interrupted(record.state, finished_at=clock())
    next_state = create_pending_execution(record.state.execution_id, attempt=record.state.attempt + 1)
    interrupted_record = ExecutionAttemptRecord(
        interrupted,
        checkpoint_thread_id=record.checkpoint_thread_id,
        checkpoint_run_id=record.checkpoint_run_id,
        checkpoint_id=checkpoint_ref.checkpoint_id,
    )
    next_record = ExecutionAttemptRecord(next_state)
    try:
        store.interrupt_and_create_next(interrupted_record, next_record)
    except ExecutionStoreError as exc:
        raise AgentRecoveryError(str(exc)) from None
    return logged(RecoveryResult(assessment, interrupted_record, next_record))


def _log_recovery(result: RecoveryResult, trace_context: TraceContext | None) -> RecoveryResult:
    """写入不含 checkpoint identity/content 的 recovery JSONL metadata，并返回原结果。"""
    audit_logger.log_event(
        "execution",
        "execution_recovery",
        trace_context=trace_context,
        attempt=result.record.state.attempt,
        recovery_decision=result.assessment.decision.value,
        recovery_reason=result.assessment.reason.value,
    )
    return result


async def _stop_unfinished(
    store: ExecutionStore,
    record: ExecutionAttemptRecord,
    reason: RecoveryReason,
    clock: Callable[[], datetime],
    checkpoint_ref: CheckpointRef | None = None,
) -> RecoveryResult:
    """将 restart 前仍 RUNNING 的 attempt 终结为 INTERRUPTED；FAILED 记录保持终结。"""
    assessment = RecoveryAssessment(RecoveryDecision.DO_NOT_RESUME, reason, checkpoint_ref)
    if record.state.status is not ExecutionStatus.RUNNING:
        return RecoveryResult(assessment, record)
    interrupted = mark_execution_interrupted(record.state, finished_at=clock())
    updated = ExecutionAttemptRecord(
        interrupted,
        checkpoint_thread_id=record.checkpoint_thread_id,
        checkpoint_run_id=record.checkpoint_run_id,
        checkpoint_id=checkpoint_ref.checkpoint_id if checkpoint_ref else record.checkpoint_id,
    )
    try:
        store.transition(updated, expected_status=ExecutionStatus.RUNNING)
    except ExecutionStoreError as exc:
        raise AgentRecoveryError(str(exc)) from None
    return RecoveryResult(assessment, updated)


def _utc_now() -> datetime:
    """生成满足 ExecutionState 严格校验的 UTC-aware recovery timestamp。"""
    return datetime.now(timezone.utc)
