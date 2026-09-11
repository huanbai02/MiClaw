"""把单次 Agent graph invocation 连接到 execution attempt 与可选 durable metadata。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TypeVar
from uuid import uuid4

from langgraph.errors import GraphBubbleUp, GraphRecursionError

from ..execution.failures import ExecutionFailure, ExecutionFailureCode, ExecutionFailureSource, provider_failure
from ..execution.guards import (
    ExecutionGuardPolicy,
    ExecutionGuardState,
    ExecutionGuardTriggered,
    GuardDecision,
    observe_tool_call_batch,
)
from ..execution.models import ExecutionState, ExecutionStatus
from ..execution.recovery import ExecutionAttemptRecord
from ..execution.retry import RetryDecision, RetryEvaluation, RetryPolicy, evaluate_retry
from ..execution.state import (
    create_next_execution_attempt,
    cancel_execution,
    create_pending_execution,
    mark_execution_failed,
    mark_execution_succeeded,
    start_execution,
)
from ..observability.logger import audit_logger
from ..observability.trace import TraceContext, get_current_trace_context
from ..runtime.execution_store import ExecutionStore, ExecutionStoreError


ResultT = TypeVar("ResultT")

DEFAULT_GRAPH_RECURSION_LIMIT = 25


@dataclass
class _ExecutionGuardRuntime:
    """跨 LangGraph node task 共享的单 attempt guard holder，仅保存安全 state。"""

    state: ExecutionGuardState
    policy: ExecutionGuardPolicy


_execution_guard_runtime: ContextVar[_ExecutionGuardRuntime | None] = ContextVar(
    "miclaw_execution_guard_runtime", default=None
)


class AgentExecutionRuntimeError(RuntimeError):
    """表示不回显 graph/provider/detail 的稳定 Agent execution 错误。"""


class AgentProviderFailure(RuntimeError):
    """把已分类 provider failure 从模型调用点安全传递到 execution wrapper。"""

    def __init__(self, failure: ExecutionFailure) -> None:
        if failure.source is not ExecutionFailureSource.PROVIDER:
            raise AgentExecutionRuntimeError("invalid_provider_failure")
        self.failure = failure
        super().__init__(failure.code.value)


class AgentToolFailure(RuntimeError):
    """把 post-ToolNode 已分类的 terminal Tool failure 传递给 execution wrapper。"""

    def __init__(self, failure: ExecutionFailure) -> None:
        if type(failure) is not ExecutionFailure or failure.source is not ExecutionFailureSource.TOOL:
            raise AgentExecutionRuntimeError("invalid_tool_failure")
        self.failure = failure
        super().__init__(failure.code.value)


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    """单次 Agent invocation 的内存执行结果，不记录原始异常。"""

    output: object | None
    state: ExecutionState
    failure: ExecutionFailure | None
    retry_evaluation: RetryEvaluation | None
    next_attempt: ExecutionState | None


def apply_graph_recursion_limit(
    config: dict[str, object],
    *,
    recursion_limit: int = DEFAULT_GRAPH_RECURSION_LIMIT,
) -> dict[str, object]:
    """合并 MiClaw-owned LangGraph hard bound，不覆盖既有 config 字段。"""
    if type(config) is not dict:
        raise AgentExecutionRuntimeError("invalid_graph_config")
    if type(recursion_limit) is not int or recursion_limit < 1:
        raise AgentExecutionRuntimeError("invalid_graph_recursion_limit")
    merged = dict(config)
    merged["recursion_limit"] = recursion_limit
    return merged


def preflight_tool_call_batch(tool_calls: object) -> None:
    """在 ToolNode 前原子预检当前 attempt 的 AIMessage Tool batch。"""
    runtime = _execution_guard_runtime.get()
    if runtime is None:
        return
    evaluation = observe_tool_call_batch(runtime.state, tool_calls, runtime.policy)
    if evaluation.decision is GuardDecision.BLOCK:
        assert evaluation.reason is not None
        raise ExecutionGuardTriggered(evaluation.reason)
    runtime.state = evaluation.state


def new_execution_id() -> str:
    """生成不编码用户输入、路径或 trace identity 的 opaque execution id。"""
    return uuid4().hex


def invoke_provider(call: Callable[[], ResultT]) -> ResultT:
    """在模型调用边界把原始异常正规化为稳定 provider failure。"""
    try:
        return call()
    except GraphBubbleUp:
        raise
    except TimeoutError:
        raise AgentProviderFailure(provider_failure(ExecutionFailureCode.PROVIDER_TIMEOUT)) from None
    except Exception:
        raise AgentProviderFailure(provider_failure(ExecutionFailureCode.PROVIDER_ERROR)) from None


async def run_agent_execution(
    invoke: Callable[[], Awaitable[ResultT]],
    *,
    execution_id: str | None = None,
    pending_state: ExecutionState | None = None,
    retry_policy: RetryPolicy | None = None,
    trace_context: TraceContext | None = None,
    execution_store: ExecutionStore | None = None,
    checkpoint_thread_id: str | None = None,
    checkpoint_run_id: str | None = None,
    checkpoint_id_provider: Callable[[], Awaitable[str | None]] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> AgentExecutionResult:
    """执行一次 graph invocation；开启 store 时先 durable PENDING/RUNNING，绝不自动重放。

    Args:
        invoke: 恰好执行一次 graph invocation 的 async callable。
        execution_id: None 时由 runtime 生成；其他显式值由 ExecutionState 严格校验。
        pending_state: safe recovery 已创建的 PENDING attempt；不能替代 terminal attempt。
        retry_policy: 当前 logical execution 的总 attempt 上限。
        trace_context: 当前 attempt 的既有 TraceContext；其 run_id 不作 checkpoint correlation。
        execution_store: 可选独立 execution metadata store。
        checkpoint_thread_id: LangGraph checkpoint thread lineage。
        checkpoint_run_id: 本次 invocation 的 checkpoint metadata correlation token。
        checkpoint_id_provider: 调用完成后读取当前 owned opaque checkpoint id 的 async provider。
        clock: 可注入的 UTC-aware 时间来源。

    Raises:
        AgentExecutionRuntimeError: metadata 无法在 graph 前后安全持久化或 checkpoint 无法安全关联时抛出。
        asyncio.CancelledError: host cancellation 原样传播。
        GraphBubbleUp: LangGraph control flow 原样传播。
    """
    active_trace = trace_context or get_current_trace_context()
    if type(active_trace) is not TraceContext or type(active_trace.run_id) is not str or not active_trace.run_id.strip():
        raise AgentExecutionRuntimeError("missing_trace_context")
    if retry_policy is None:
        retry_policy = RetryPolicy()
    if type(retry_policy) is not RetryPolicy:
        raise AgentExecutionRuntimeError("invalid_retry_policy")
    if clock is None:
        clock = _utc_now

    pending, is_existing_pending = _resolve_pending_state(execution_id, pending_state)
    _validate_persistence_inputs(execution_store, checkpoint_thread_id, checkpoint_run_id, checkpoint_id_provider)
    if execution_store is not None and not is_existing_pending:
        _persist_create(execution_store, _record(pending, checkpoint_thread_id, checkpoint_run_id))

    running = start_execution(pending, run_id=active_trace.run_id, started_at=clock())
    if execution_store is not None:
        _persist_transition(
            execution_store,
            _record(running, checkpoint_thread_id, checkpoint_run_id),
            ExecutionStatus.PENDING,
        )
    _log_execution_started(running, checkpoint_thread_id, active_trace)

    guard_token = _execution_guard_runtime.set(_ExecutionGuardRuntime(ExecutionGuardState(), ExecutionGuardPolicy()))
    try:
        try:
            output = await invoke()
        except GraphBubbleUp:
            raise
        except GraphRecursionError:
            failure = ExecutionFailure(ExecutionFailureSource.RUNTIME, ExecutionFailureCode.EXECUTION_LIMIT_EXCEEDED)
        except ExecutionGuardTriggered:
            failure = ExecutionFailure(ExecutionFailureSource.RUNTIME, ExecutionFailureCode.LOOP_GUARD_TRIGGERED)
        except AgentToolFailure as exc:
            failure = exc.failure
        except AgentProviderFailure as exc:
            failure = exc.failure
        except Exception:
            failure = ExecutionFailure(ExecutionFailureSource.RUNTIME, ExecutionFailureCode.RUNTIME_ERROR)
        else:
            succeeded = mark_execution_succeeded(running, finished_at=clock())
            if execution_store is not None:
                _persist_transition(
                    execution_store,
                    _record(
                        succeeded,
                        checkpoint_thread_id,
                        checkpoint_run_id,
                        checkpoint_id=await _checkpoint_id(checkpoint_id_provider),
                    ),
                    ExecutionStatus.RUNNING,
                )
            _log_execution_finished(succeeded, checkpoint_thread_id, active_trace)
            return AgentExecutionResult(output, succeeded, None, None, None)

        failed = mark_execution_failed(running, finished_at=clock())
        evaluation = evaluate_retry(failure, current_attempt=failed.attempt, policy=retry_policy)
        next_attempt = create_next_execution_attempt(failed, evaluation) if evaluation.decision is RetryDecision.RETRY else None
        if execution_store is not None:
            try:
                execution_store.fail_and_plan_next(
                    _record(
                        failed,
                        checkpoint_thread_id,
                        checkpoint_run_id,
                        checkpoint_id=await _checkpoint_id(checkpoint_id_provider),
                        failure=failure,
                        retry_evaluation=evaluation,
                    ),
                    ExecutionAttemptRecord(next_attempt) if next_attempt is not None else None,
                )
            except ExecutionStoreError:
                raise AgentExecutionRuntimeError("execution_persistence_failed") from None
        _log_execution_finished(
            failed,
            checkpoint_thread_id,
            active_trace,
            failure_code=failure.code.value,
            retry_decision=evaluation.decision.value,
        )
        return AgentExecutionResult(None, failed, failure, evaluation, next_attempt)
    except asyncio.CancelledError:
        _persist_cancellation(execution_store, running, checkpoint_thread_id, checkpoint_run_id, active_trace, clock)
        raise
    finally:
        _execution_guard_runtime.reset(guard_token)


def _log_execution_started(
    state: ExecutionState,
    checkpoint_thread_id: str | None,
    trace_context: TraceContext,
) -> None:
    """写入不含 identity/content 的最小 execution started JSONL metadata。"""
    audit_logger.log_event(
        _safe_event_thread_id(checkpoint_thread_id),
        "execution_started",
        trace_context=trace_context,
        attempt=state.attempt,
        status=state.status.value,
    )


def _log_execution_finished(
    state: ExecutionState,
    checkpoint_thread_id: str | None,
    trace_context: TraceContext,
    *,
    failure_code: str | None = None,
    retry_decision: str | None = None,
) -> None:
    """写入不含异常文本、payload 或 checkpoint identity 的 terminal execution metadata。"""
    metadata: dict[str, object] = {"attempt": state.attempt, "status": state.status.value}
    if failure_code is not None:
        metadata["failure_code"] = failure_code
    if retry_decision is not None:
        metadata["retry_decision"] = retry_decision
    audit_logger.log_event(
        _safe_event_thread_id(checkpoint_thread_id),
        "execution_finished",
        trace_context=trace_context,
        **metadata,
    )


def _safe_event_thread_id(_value: str | None) -> str:
    """不把 checkpoint thread identity 写入 execution JSONL，统一使用固定安全 envelope。"""
    return "execution"


def _resolve_pending_state(
    execution_id: str | None,
    pending_state: ExecutionState | None,
) -> tuple[ExecutionState, bool]:
    """仅区分 None 自动 ID 与显式值；复用 ExecutionState 作为唯一验证边界。"""
    if pending_state is None:
        resolved_execution_id = new_execution_id() if execution_id is None else execution_id
        return create_pending_execution(resolved_execution_id), False
    if type(pending_state) is not ExecutionState or pending_state.status is not ExecutionStatus.PENDING:
        raise AgentExecutionRuntimeError("invalid_pending_execution")
    if execution_id is not None and execution_id != pending_state.execution_id:
        raise AgentExecutionRuntimeError("invalid_pending_execution")
    return pending_state, True


def _validate_persistence_inputs(
    execution_store: ExecutionStore | None,
    checkpoint_thread_id: str | None,
    checkpoint_run_id: str | None,
    checkpoint_id_provider: Callable[[], Awaitable[str | None]] | None,
) -> None:
    """确保启用持久化时已具备最小 checkpoint ownership metadata。"""
    if execution_store is None:
        if checkpoint_thread_id is not None or checkpoint_run_id is not None or checkpoint_id_provider is not None:
            raise AgentExecutionRuntimeError("invalid_execution_store")
        return
    if type(execution_store) is not ExecutionStore:
        raise AgentExecutionRuntimeError("invalid_execution_store")
    if type(checkpoint_thread_id) is not str or not checkpoint_thread_id.strip():
        raise AgentExecutionRuntimeError("invalid_checkpoint_config")
    if type(checkpoint_run_id) is not str or not checkpoint_run_id.strip():
        raise AgentExecutionRuntimeError("invalid_checkpoint_config")
    if checkpoint_id_provider is not None and not callable(checkpoint_id_provider):
        raise AgentExecutionRuntimeError("invalid_checkpoint_config")


def _record(
    state: ExecutionState,
    checkpoint_thread_id: str | None,
    checkpoint_run_id: str | None,
    *,
    checkpoint_id: str | None = None,
    failure: ExecutionFailure | None = None,
    retry_evaluation: RetryEvaluation | None = None,
) -> ExecutionAttemptRecord:
    """构造不含 graph payload 的 durable attempt record。"""
    return ExecutionAttemptRecord(
        state,
        failure,
        retry_evaluation,
        checkpoint_thread_id,
        checkpoint_run_id,
        checkpoint_id,
    )


def _persist_create(store: ExecutionStore, record: ExecutionAttemptRecord) -> None:
    """将 persistence error 收敛为 execution preflight error，调用方不会触发 graph。"""
    try:
        store.create_attempt(record)
    except ExecutionStoreError:
        raise AgentExecutionRuntimeError("execution_persistence_failed") from None


def _persist_transition(store: ExecutionStore, record: ExecutionAttemptRecord, expected: ExecutionStatus) -> None:
    """将 terminal 或 start persistence failure 转成稳定 uncertainty error。"""
    try:
        store.transition(record, expected_status=expected)
    except ExecutionStoreError as exc:
        if str(exc) == "execution_record_conflict":
            raise AgentExecutionRuntimeError("execution_record_conflict") from None
        raise AgentExecutionRuntimeError("execution_persistence_failed") from None


def _persist_cancellation(
    store: ExecutionStore | None,
    running: ExecutionState,
    checkpoint_thread_id: str | None,
    checkpoint_run_id: str | None,
    trace_context: TraceContext,
    clock: Callable[[], datetime],
) -> None:
    """在 cancellation control-flow 传播前尽力持久化 CANCELLED，失败只记录稳定 metadata。"""
    cancelled = cancel_execution(running, finished_at=clock())
    if store is not None:
        try:
            store.transition(
                _record(cancelled, checkpoint_thread_id, checkpoint_run_id),
                expected_status=ExecutionStatus.RUNNING,
            )
        except ExecutionStoreError:
            audit_logger.log_event(
                _safe_event_thread_id(checkpoint_thread_id),
                "execution_cancel_persistence_failed",
                trace_context=trace_context,
                attempt=running.attempt,
            )
            return
    _log_execution_finished(cancelled, checkpoint_thread_id, trace_context)


async def _checkpoint_id(provider: Callable[[], Awaitable[str | None]] | None) -> str | None:
    """读取 opaque checkpoint id；查询失败会形成稳定 uncertainty，而不会隐藏错误或重放图。"""
    if provider is None:
        return None
    try:
        checkpoint_id = await provider()
    except Exception:
        raise AgentExecutionRuntimeError("checkpoint_recovery_unavailable") from None
    return checkpoint_id if type(checkpoint_id) is str and checkpoint_id.strip() else None


def _utc_now() -> datetime:
    """返回满足 ExecutionState 严格校验的 UTC-aware 当前时间。"""
    return datetime.now(timezone.utc)
