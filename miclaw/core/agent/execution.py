"""把单次 Agent graph invocation 连接到纯 execution attempt 领域模型。"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from collections.abc import Awaitable, Callable
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
from ..execution.retry import RetryDecision, RetryEvaluation, RetryPolicy, evaluate_retry
from ..execution.state import (
    create_next_execution_attempt,
    create_pending_execution,
    mark_execution_failed,
    mark_execution_succeeded,
    start_execution,
)
from ..observability.trace import TraceContext, get_current_trace_context


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
    """表示不回显 graph/provider detail 的稳定 Agent execution preflight error。"""


class AgentProviderFailure(RuntimeError):
    """把已分类 provider failure 从模型调用点安全传递到 execution wrapper。"""

    def __init__(self, failure: ExecutionFailure) -> None:
        if failure.source is not ExecutionFailureSource.PROVIDER:
            raise AgentExecutionRuntimeError("invalid_provider_failure")
        self.failure = failure
        super().__init__(failure.code.value)


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    """单次 Agent invocation 的内存执行结果，不记录原始异常。

    Args:
        output: graph invocation 的原始返回值，仅供调用方继续处理。
        state: 已成功或失败结束的当前 attempt。
        failure: 失败时的稳定分类；成功时为 None。
        retry_evaluation: 失败时的 retry eligibility；成功时为 None。
        next_attempt: 仅在 retry eligibility 允许时准备的下一 PENDING attempt。
    """

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
    """合并 MiClaw-owned LangGraph hard bound，不覆盖既有 config 字段。

    Args:
        config: 现有 graph invocation config。
        recursion_limit: runtime-owned LangGraph hard bound；生产调用使用默认值。

    Returns:
        保留原字段并显式带有 recursion_limit 的新 config。
    """
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
    """在实际模型调用边界将原始异常正规化为稳定 provider failure。

    Args:
        call: 一次同步 provider/model invocation。

    Returns:
        provider 的原始成功结果。

    Raises:
        AgentProviderFailure: provider 调用失败时抛出安全分类。
    """
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
    retry_policy: RetryPolicy | None = None,
    trace_context: TraceContext | None = None,
    clock: Callable[[], datetime] | None = None,
) -> AgentExecutionResult:
    """执行一次 Agent graph invocation，并仅规划可能的下一 attempt。

    此函数绝不自动调用第二次 graph invocation；next_attempt 只是未来 runtime 可消费的
    PENDING state。

    Args:
        invoke: 恰好执行一次 graph invocation 的 async callable。
        execution_id: 可测试地显式提供的 opaque logical execution identity。
        retry_policy: 当前 logical execution 的总 attempt 上限。
        trace_context: 当前 attempt 必须复用的 TraceContext；省略时读取当前 ContextVar。
        clock: 返回 timezone-aware 时间的可注入时钟。

    Returns:
        成功或失败后的 immutable execution result。

    Raises:
        asyncio.CancelledError: host cancellation 必须原样传播。
        GraphBubbleUp: LangGraph control-flow 必须原样传播。
        AgentExecutionRuntimeError: 缺少 trace context 或 policy 无效时抛出。
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

    resolved_execution_id = new_execution_id() if execution_id is None else execution_id
    pending = create_pending_execution(resolved_execution_id)
    running = start_execution(pending, run_id=active_trace.run_id, started_at=clock())
    guard_token = _execution_guard_runtime.set(_ExecutionGuardRuntime(ExecutionGuardState(), ExecutionGuardPolicy()))
    try:
        output = await invoke()
    except asyncio.CancelledError:
        raise
    except GraphBubbleUp:
        raise
    except GraphRecursionError:
        failure = ExecutionFailure(ExecutionFailureSource.RUNTIME, ExecutionFailureCode.EXECUTION_LIMIT_EXCEEDED)
    except ExecutionGuardTriggered:
        failure = ExecutionFailure(ExecutionFailureSource.RUNTIME, ExecutionFailureCode.LOOP_GUARD_TRIGGERED)
    except AgentProviderFailure as exc:
        failure = exc.failure
    except Exception:
        failure = ExecutionFailure(ExecutionFailureSource.RUNTIME, ExecutionFailureCode.RUNTIME_ERROR)
    else:
        succeeded = mark_execution_succeeded(running, finished_at=clock())
        return AgentExecutionResult(output, succeeded, None, None, None)
    finally:
        _execution_guard_runtime.reset(guard_token)

    failed = mark_execution_failed(running, finished_at=clock())
    evaluation = evaluate_retry(failure, current_attempt=failed.attempt, policy=retry_policy)
    next_attempt = (
        create_next_execution_attempt(failed, evaluation)
        if evaluation.decision is RetryDecision.RETRY
        else None
    )
    return AgentExecutionResult(None, failed, failure, evaluation, next_attempt)


def _utc_now() -> datetime:
    """返回满足 ExecutionState 严格校验的 UTC-aware 当前时间。"""
    return datetime.now(timezone.utc)
