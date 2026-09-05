"""验证 Agent execution runtime 只运行一次并规划 retry attempt。"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.errors import GraphInterrupt

from miclaw.core.agent.execution import (
    AgentExecutionRuntimeError,
    AgentProviderFailure,
    DEFAULT_GRAPH_RECURSION_LIMIT,
    apply_graph_recursion_limit,
    invoke_provider,
    run_agent_execution,
)
from miclaw.core.agent.graph import create_agent_app
from miclaw.core.execution.failures import ExecutionFailureCode, ExecutionFailureSource
from miclaw.core.execution.models import ExecutionStateValidationError, ExecutionStatus
from miclaw.core.execution.retry import RetryDecision, RetryDecisionReason, RetryEvaluation, RetryPolicy
from miclaw.core.execution.state import (
    ExecutionTransitionError,
    cancel_execution,
    create_next_execution_attempt,
    create_pending_execution,
    mark_execution_failed,
    mark_execution_interrupted,
    mark_execution_succeeded,
    start_execution,
)
from miclaw.core.observability.trace import (
    TraceContext,
    reset_trace_context,
    set_current_trace_context,
)
from miclaw.core.runtime.execution_store import ExecutionStore, ExecutionStoreError
from miclaw.core.tools.result import tool_error


START = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)
FINISH = datetime(2026, 8, 27, 8, 1, tzinfo=timezone.utc)


class FixedClock:
    """为 state transition 提供确定性 timezone-aware timestamps。"""

    def __init__(self, *timestamps: datetime) -> None:
        self._timestamps = iter(timestamps)

    def __call__(self) -> datetime:
        return next(self._timestamps)


def _run(invoke, *, clock=None, policy=None, execution_id="execution-1"):
    """用真实 wrapper 与稳定 TraceContext 执行一个 fake graph invocation。"""
    return asyncio.run(
        run_agent_execution(
            invoke,
            execution_id=execution_id,
            trace_context=TraceContext(run_id="run-1"),
            retry_policy=policy,
            clock=clock or FixedClock(START, FINISH),
        )
    )


@pytest.mark.parametrize("execution_id", ["", 0, False, [], {}, 1.0, "   "])
def test_explicit_malformed_execution_id_is_not_replaced_or_invoked(execution_id):
    """只有 None 可触发生成；所有显式 malformed identity 必须在 graph 前失败。"""
    calls = 0

    async def invoke():
        nonlocal calls
        calls += 1
        return "unexpected"

    with patch("miclaw.core.agent.execution.new_execution_id", side_effect=AssertionError("must not generate")):
        with pytest.raises(ExecutionStateValidationError, match="^invalid_execution_id$"):
            _run(invoke, execution_id=execution_id)

    assert calls == 0


def test_none_execution_id_is_the_only_runtime_generation_sentinel():
    """None 生成 opaque id；显式有效 identity 则逐字保持不变。"""
    generated_calls = 0

    async def invoke():
        nonlocal generated_calls
        generated_calls += 1
        return "ok"

    with patch("miclaw.core.agent.execution.new_execution_id", return_value="generated-execution-id") as new_id:
        generated = _run(invoke, execution_id=None)

    explicit = _run(invoke, execution_id="exec-explicit-123")

    new_id.assert_called_once_with()
    assert generated_calls == 2
    assert generated.state.execution_id == "generated-execution-id"
    assert explicit.state.execution_id == "exec-explicit-123"


def test_successful_graph_invocation_runs_once_and_finishes_execution():
    """正常 graph output 不改变现有调用返回值，也不产生 retry planning。"""
    calls = 0

    async def invoke():
        nonlocal calls
        calls += 1
        return {"messages": ["answer"]}

    result = _run(invoke)

    assert calls == 1
    assert result.output == {"messages": ["answer"]}
    assert result.state.status is ExecutionStatus.SUCCEEDED
    assert result.state.run_id == "run-1"
    assert result.failure is result.retry_evaluation is result.next_attempt is None


def test_actual_agent_model_boundary_normalizes_timeout_without_raw_detail():
    """真实 Agent node 的 model.invoke 边界只向 wrapper 暴露 provider timeout 分类。"""

    class TimeoutModel:
        def invoke(self, _messages):
            raise TimeoutError("PROVIDER_SECRET_X")

    class Provider:
        def bind_tools(self, _tools):
            return TimeoutModel()

    with patch("miclaw.core.agent.graph.get_provider", return_value=Provider()):
        app = create_agent_app(tools=[])

    async def invoke():
        return await app.ainvoke({"messages": [HumanMessage(content="hello")]})

    result = _run(invoke)

    assert result.failure.source is ExecutionFailureSource.PROVIDER
    assert result.failure.code is ExecutionFailureCode.PROVIDER_TIMEOUT
    assert "PROVIDER_SECRET_X" not in repr(result.failure)
    assert result.next_attempt is not None


def test_execution_uses_current_trace_context_run_id_when_not_explicitly_passed():
    """实际 runtime 可复用既有 ContextVar，而不创建第二套 run identity。"""
    token = set_current_trace_context(TraceContext(run_id="current-run"))
    try:
        async def invoke():
            return "ok"

        result = asyncio.run(
            run_agent_execution(
                invoke,
                execution_id="execution-1",
                clock=FixedClock(START, FINISH),
            )
        )
    finally:
        reset_trace_context(token)

    assert result.state.run_id == "current-run"


def test_provider_timeout_plans_next_attempt_without_replaying_graph():
    """retry eligibility 只创建 PENDING plan，不能触发第二次 invocation。"""
    calls = 0

    async def invoke():
        nonlocal calls
        calls += 1
        return invoke_provider(lambda: (_ for _ in ()).throw(TimeoutError()))

    result = _run(invoke, policy=RetryPolicy(3))

    assert calls == 1
    assert result.output is None
    assert result.state.status is ExecutionStatus.FAILED
    assert result.failure.source is ExecutionFailureSource.PROVIDER
    assert result.failure.code is ExecutionFailureCode.PROVIDER_TIMEOUT
    assert result.retry_evaluation.decision is RetryDecision.RETRY
    assert result.retry_evaluation.reason is RetryDecisionReason.RETRYABLE_FAILURE
    assert result.next_attempt is not None
    assert result.next_attempt.execution_id == result.state.execution_id
    assert result.next_attempt.attempt == 2
    assert result.next_attempt.status is ExecutionStatus.PENDING
    assert result.next_attempt.run_id is result.next_attempt.started_at is result.next_attempt.finished_at is None


def test_generic_provider_error_is_safe_and_not_retryable():
    """provider raw exception detail 不进入 execution failure 或 retry result。"""

    async def invoke():
        return invoke_provider(lambda: (_ for _ in ()).throw(RuntimeError("PROVIDER_SECRET_X")))

    result = _run(invoke)

    assert result.failure.source is ExecutionFailureSource.PROVIDER
    assert result.failure.code is ExecutionFailureCode.PROVIDER_ERROR
    assert result.retry_evaluation == RetryEvaluation(
        RetryDecision.DO_NOT_RETRY,
        RetryDecisionReason.NON_RETRYABLE_FAILURE,
    )
    assert result.next_attempt is None
    assert "PROVIDER_SECRET_X" not in repr(result.failure)
    assert str(AgentProviderFailure(result.failure)) == "provider_error"


def test_runtime_failure_outside_provider_boundary_is_not_provider_failure():
    """graph orchestration 异常保持 RUNTIME source，且默认不 retry。"""

    async def invoke():
        raise RuntimeError("RUNTIME_SECRET_X")

    result = _run(invoke)

    assert result.failure.source is ExecutionFailureSource.RUNTIME
    assert result.failure.code is ExecutionFailureCode.RUNTIME_ERROR
    assert result.retry_evaluation.reason is RetryDecisionReason.NON_RETRYABLE_FAILURE
    assert result.next_attempt is None
    assert "RUNTIME_SECRET_X" not in repr(result.failure)


def test_cancellation_propagates_without_failure_or_retry_planning():
    """host cancellation 不能被收敛为普通 runtime failure。"""

    async def invoke():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        _run(invoke)


def test_cancellation_persists_running_attempt_without_retry_or_replay(tmp_path):
    """已 RUNNING 的 await 被取消时 durable 状态变 CANCELLED，且 wrapper 继续传播 control flow。"""
    async def scenario():
        store = ExecutionStore(tmp_path / "execution.sqlite3")
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def invoke():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()

        task = asyncio.create_task(
            run_agent_execution(
                invoke,
                execution_id="execution-cancelled",
                trace_context=TraceContext(run_id="cancel-trace"),
                execution_store=store,
                checkpoint_thread_id="cancel-thread",
                checkpoint_run_id="cancel-run",
                clock=FixedClock(START, FINISH),
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        record = store.get_attempt("execution-cancelled", 1)
        assert calls == 1
        assert record.state.status is ExecutionStatus.CANCELLED
        assert record.failure is record.retry_evaluation is None
        with pytest.raises(ExecutionStoreError, match="execution_record_not_found"):
            store.get_attempt("execution-cancelled", 2)
        store.close()

    asyncio.run(scenario())


def test_cancellation_before_task_starts_creates_no_phantom_execution_record(tmp_path):
    """child Task 尚未进入 wrapper 时被取消，不伪造 PENDING/RUNNING durable attempt。"""
    async def scenario():
        store = ExecutionStore(tmp_path / "execution.sqlite3")

        async def invoke():
            raise AssertionError("cancelled task must not invoke graph")

        task = asyncio.create_task(
            run_agent_execution(
                invoke,
                execution_id="execution-never-started",
                trace_context=TraceContext(run_id="early-cancel-trace"),
                execution_store=store,
                checkpoint_thread_id="cancel-thread",
                checkpoint_run_id="cancel-run",
            )
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ExecutionStoreError, match="execution_record_not_found"):
            store.get_attempt("execution-never-started", 1)
        store.close()

    asyncio.run(scenario())


def test_terminal_completion_and_cancel_race_keeps_one_legal_terminal_state(tmp_path):
    """success/cancel 边界竞争时由 CAS 决定唯一 terminal，不能重开或计划 retry。"""
    async def scenario():
        store = ExecutionStore(tmp_path / "execution.sqlite3")
        started = asyncio.Event()
        release = asyncio.Event()

        async def invoke():
            started.set()
            await release.wait()
            return "done"

        task = asyncio.create_task(
            run_agent_execution(
                invoke,
                execution_id="execution-terminal-race",
                trace_context=TraceContext(run_id="terminal-race-trace"),
                execution_store=store,
                checkpoint_thread_id="cancel-thread",
                checkpoint_run_id="cancel-run",
            )
        )
        await started.wait()
        release.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        record = store.get_attempt("execution-terminal-race", 1)
        assert record.state.status in {ExecutionStatus.SUCCEEDED, ExecutionStatus.CANCELLED}
        with pytest.raises(ExecutionStoreError, match="execution_record_not_found"):
            store.get_attempt("execution-terminal-race", 2)
        store.close()

    asyncio.run(scenario())


def test_cancellation_persistence_failure_never_replays_or_plans_retry(tmp_path, monkeypatch):
    """CANCELLED CAS 写入失败仍 re-raise cancellation，保留可恢复 RUNNING 而不执行任何 retry。"""
    async def scenario():
        store = ExecutionStore(tmp_path / "execution.sqlite3")
        started = asyncio.Event()
        calls = 0

        async def invoke():
            nonlocal calls
            calls += 1
            started.set()
            await asyncio.Event().wait()

        original_transition = store.transition

        def fail_cancel(record, *, expected_status):
            if record.state.status is ExecutionStatus.CANCELLED:
                raise ExecutionStoreError("execution_store_error")
            return original_transition(record, expected_status=expected_status)

        monkeypatch.setattr(store, "transition", fail_cancel)
        task = asyncio.create_task(
            run_agent_execution(
                invoke,
                execution_id="execution-cancel-persist-failure",
                trace_context=TraceContext(run_id="cancel-failure-trace"),
                execution_store=store,
                checkpoint_thread_id="cancel-thread",
                checkpoint_run_id="cancel-run",
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert calls == 1
        assert store.get_attempt("execution-cancel-persist-failure", 1).state.status is ExecutionStatus.RUNNING
        with pytest.raises(ExecutionStoreError, match="execution_record_not_found"):
            store.get_attempt("execution-cancel-persist-failure", 2)
        store.close()

    asyncio.run(scenario())


def test_langgraph_interrupt_control_flow_propagates_without_failure_or_retry_planning():
    """LangGraph control-flow exception 不得被 wrapper 吞成 runtime failure。"""

    async def invoke():
        raise GraphInterrupt(())

    with pytest.raises(GraphInterrupt):
        _run(invoke)


def test_graph_recursion_error_is_non_retryable_execution_limit_failure():
    """LangGraph hard bound 触发后由 runtime 归一化，且不会创建下一 attempt。"""
    model_calls = 0

    @tool
    def loop_tool(value: int) -> str:
        """执行一个有限测试 Tool。"""
        return str(value)

    class LoopModel:
        def bind_tools(self, _tools):
            return self

        def invoke(self, _messages):
            nonlocal model_calls
            model_calls += 1
            return AIMessage(
                content="",
                tool_calls=[{"name": "loop_tool", "args": {"value": model_calls}, "id": f"call-{model_calls}"}],
            )

    with patch("miclaw.core.agent.graph.get_provider", return_value=LoopModel()):
        app = create_agent_app(tools=[loop_tool])

    async def invoke():
        return await app.ainvoke(
            {"messages": [HumanMessage(content="loop")]},
            config=apply_graph_recursion_limit(
                {"configurable": {"thread_id": "thread-1", "run_id": "run-1"}}, recursion_limit=4
            ),
        )

    result = _run(invoke)

    assert model_calls >= 2
    assert result.state.status is ExecutionStatus.FAILED
    assert result.failure.source is ExecutionFailureSource.RUNTIME
    assert result.failure.code is ExecutionFailureCode.EXECUTION_LIMIT_EXCEEDED
    assert result.retry_evaluation == RetryEvaluation(
        RetryDecision.DO_NOT_RETRY,
        RetryDecisionReason.NON_RETRYABLE_FAILURE,
    )
    assert result.next_attempt is None


def test_graph_recursion_limit_config_is_explicit_and_preserves_existing_fields():
    """生产 config 增加 hard bound 时不覆盖 thread/checkpoint 等既有字段。"""
    original = {"configurable": {"thread_id": "thread-1", "run_id": "run-1"}, "callbacks": ["callback"]}

    resolved = apply_graph_recursion_limit(original)

    assert resolved["recursion_limit"] == DEFAULT_GRAPH_RECURSION_LIMIT
    assert resolved["configurable"] == original["configurable"]
    assert resolved["callbacks"] == original["callbacks"]
    assert "recursion_limit" not in original


@pytest.mark.parametrize("recursion_limit", [0, -1, True, False, 1.0, "4", None])
def test_graph_recursion_limit_requires_exact_positive_runtime_value(recursion_limit):
    """hard bound 不接受 bool/coercion 或无效值。"""
    with pytest.raises(AgentExecutionRuntimeError, match="^invalid_graph_recursion_limit$"):
        apply_graph_recursion_limit({}, recursion_limit=recursion_limit)


def test_repeated_tool_call_guard_blocks_real_toolnode_before_third_side_effect():
    """真实 Agent path 的第 3 次相同 Tool request 不会进入 ToolNode。"""
    model_calls = 0
    tool_calls = 0

    @tool
    def repeated_tool(value: str) -> str:
        """记录真实 ToolNode 侧副作用。"""
        nonlocal tool_calls
        tool_calls += 1
        return value

    class RepeatingModel:
        def bind_tools(self, _tools):
            return self

        def invoke(self, _messages):
            nonlocal model_calls
            model_calls += 1
            return AIMessage(
                content="",
                tool_calls=[{"name": "repeated_tool", "args": {"value": "same"}, "id": f"call-{model_calls}"}],
            )

    with patch("miclaw.core.agent.graph.get_provider", return_value=RepeatingModel()):
        app = create_agent_app(tools=[repeated_tool])

    async def invoke():
        return await app.ainvoke({"messages": [HumanMessage(content="repeat")]})

    result = _run(invoke)

    assert model_calls == 3
    assert tool_calls == 2
    assert result.state.status is ExecutionStatus.FAILED
    assert result.failure.code is ExecutionFailureCode.LOOP_GUARD_TRIGGERED
    assert result.retry_evaluation == RetryEvaluation(
        RetryDecision.DO_NOT_RETRY,
        RetryDecisionReason.NON_RETRYABLE_FAILURE,
    )
    assert result.next_attempt is None


def test_guard_blocks_entire_real_toolnode_batch_before_any_batch_side_effect():
    """触发阈值的 multi-tool batch 在 ToolNode 前整体阻断。"""
    model_calls = 0
    repeated_calls = 0
    other_calls = 0

    @tool
    def repeated_tool(value: str) -> str:
        """记录重复 Tool 副作用。"""
        nonlocal repeated_calls
        repeated_calls += 1
        return value

    @tool
    def other_tool(value: str) -> str:
        """记录 batch 内其他 Tool 副作用。"""
        nonlocal other_calls
        other_calls += 1
        return value

    class BatchModel:
        def bind_tools(self, _tools):
            return self

        def invoke(self, _messages):
            nonlocal model_calls
            model_calls += 1
            if model_calls < 3:
                return AIMessage(
                    content="",
                    tool_calls=[{"name": "repeated_tool", "args": {"value": "same"}, "id": f"call-{model_calls}"}],
                )
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "repeated_tool", "args": {"value": "same"}, "id": "call-3"},
                    {"name": "other_tool", "args": {"value": "other"}, "id": "other-1"},
                ],
            )

    with patch("miclaw.core.agent.graph.get_provider", return_value=BatchModel()):
        app = create_agent_app(tools=[repeated_tool, other_tool])

    async def invoke():
        return await app.ainvoke({"messages": [HumanMessage(content="repeat")]})

    result = _run(invoke)

    assert model_calls == 3
    assert repeated_calls == 2
    assert other_calls == 0
    assert result.failure.code is ExecutionFailureCode.LOOP_GUARD_TRIGGERED


def test_guard_context_is_reset_between_execution_attempts():
    """新的 top-level execution 从 fresh ContextVar guard state 开始。"""
    model_calls = 0
    tool_calls = 0

    @tool
    def repeated_tool(value: str) -> str:
        """记录跨 execution 的 Tool 调用。"""
        nonlocal tool_calls
        tool_calls += 1
        return value

    class ResetModel:
        def bind_tools(self, _tools):
            return self

        def invoke(self, _messages):
            nonlocal model_calls
            model_calls += 1
            if model_calls in {1, 2, 4}:
                return AIMessage(
                    content="",
                    tool_calls=[{"name": "repeated_tool", "args": {"value": "same"}, "id": f"call-{model_calls}"}],
                )
            return AIMessage(content="done")

    with patch("miclaw.core.agent.graph.get_provider", return_value=ResetModel()):
        app = create_agent_app(tools=[repeated_tool])

    async def first():
        return await app.ainvoke({"messages": [HumanMessage(content="first")]})

    async def second():
        return await app.ainvoke({"messages": [HumanMessage(content="second")]})

    first_result = _run(first, execution_id="execution-a")
    second_result = _run(second, execution_id="execution-b")

    assert first_result.state.status is ExecutionStatus.SUCCEEDED
    assert second_result.state.status is ExecutionStatus.SUCCEEDED
    assert tool_calls == 3


def test_real_agent_toolnode_side_effect_is_not_replayed_when_retry_is_planned():
    """真实 Agent → ToolNode → Tool → provider timeout 仍只执行一次 Tool side effect。"""
    model_calls = 0
    tool_calls = 0
    graph_calls = 0

    @tool
    def side_effect_tool(value: str) -> str:
        """记录一次测试副作用。"""
        nonlocal tool_calls
        tool_calls += 1
        return f"processed:{value}"

    class ToolThenTimeoutModel:
        def invoke(self, _messages):
            nonlocal model_calls
            model_calls += 1
            if model_calls == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "side_effect_tool", "args": {"value": "once"}, "id": "tool-call-1"}
                    ],
                )
            raise TimeoutError("PROVIDER_SECRET_SHOULD_NOT_LEAK")

        def bind_tools(self, _tools):
            return self

    with patch("miclaw.core.agent.graph.get_provider", return_value=ToolThenTimeoutModel()):
        app = create_agent_app(tools=[side_effect_tool])

    async def invoke():
        nonlocal graph_calls
        graph_calls += 1
        return await app.ainvoke({"messages": [HumanMessage(content="run tool once")]})

    result = _run(invoke, policy=RetryPolicy(3), execution_id="toolnode-execution")

    assert graph_calls == 1
    assert model_calls == 2
    assert tool_calls == 1
    assert result.state.status is ExecutionStatus.FAILED
    assert result.failure.source is ExecutionFailureSource.PROVIDER
    assert result.failure.code is ExecutionFailureCode.PROVIDER_TIMEOUT
    assert result.retry_evaluation == RetryEvaluation(RetryDecision.RETRY, RetryDecisionReason.RETRYABLE_FAILURE)
    assert result.next_attempt is not None
    assert result.next_attempt.execution_id == "toolnode-execution"
    assert result.next_attempt.attempt == 2
    assert result.next_attempt.status is ExecutionStatus.PENDING
    assert result.next_attempt.run_id is None
    assert "PROVIDER_SECRET_SHOULD_NOT_LEAK" not in repr(result.failure)
    assert "PROVIDER_SECRET_SHOULD_NOT_LEAK" not in repr(result.retry_evaluation)
    assert "PROVIDER_SECRET_SHOULD_NOT_LEAK" not in repr(result.next_attempt)


def test_retryable_looking_tool_result_is_not_reexecuted():
    """ToolResult 是 graph 内结果；即使 timeout-looking 也不自动重放 side effect。"""
    side_effect_calls = 0

    async def invoke():
        nonlocal side_effect_calls
        side_effect_calls += 1
        return tool_error("timeout", "tool timeout")

    result = _run(invoke)

    assert side_effect_calls == 1
    assert result.state.status is ExecutionStatus.SUCCEEDED
    assert result.output.error_type == "timeout"
    assert result.failure is result.retry_evaluation is result.next_attempt is None


def test_attempts_exhausted_keeps_failed_terminal_attempt_without_next_plan():
    """第三个 retryable failure 只能结束当前 attempt，不能创建第四个。"""

    async def invoke():
        return invoke_provider(lambda: (_ for _ in ()).throw(TimeoutError()))

    result = _run(invoke, policy=RetryPolicy(1))

    assert result.state.status is ExecutionStatus.FAILED
    assert result.retry_evaluation == RetryEvaluation(
        RetryDecision.DO_NOT_RETRY,
        RetryDecisionReason.ATTEMPTS_EXHAUSTED,
    )
    assert result.next_attempt is None


def test_next_attempt_creation_requires_failed_state_and_retry_decision():
    """纯 helper 永远不会重新打开 terminal attempt。"""
    pending = create_pending_execution("execution-1")
    running = start_execution(pending, run_id="run-1", started_at=START)
    failed = mark_execution_failed(running, finished_at=FINISH)
    retry = RetryEvaluation(RetryDecision.RETRY, RetryDecisionReason.RETRYABLE_FAILURE)

    next_attempt = create_next_execution_attempt(failed, retry)
    assert next_attempt.execution_id == "execution-1"
    assert next_attempt.attempt == 2
    assert next_attempt.status is ExecutionStatus.PENDING
    assert failed.status is ExecutionStatus.FAILED

    pending_second = create_pending_execution("execution-1", attempt=2)
    failed_second = mark_execution_failed(
        start_execution(pending_second, run_id="run-2", started_at=START), finished_at=FINISH
    )
    assert create_next_execution_attempt(failed_second, retry).attempt == 3

    succeeded = mark_execution_succeeded(running, finished_at=FINISH)
    interrupted = mark_execution_interrupted(running, finished_at=FINISH)
    cancelled = cancel_execution(pending, finished_at=FINISH)
    for terminal_state in (succeeded, interrupted, cancelled):
        with pytest.raises(ExecutionTransitionError, match="^invalid_execution_transition$"):
            create_next_execution_attempt(terminal_state, retry)
    with pytest.raises(ExecutionTransitionError, match="^invalid_execution_transition$"):
        create_next_execution_attempt(
            failed,
            RetryEvaluation(RetryDecision.DO_NOT_RETRY, RetryDecisionReason.NON_RETRYABLE_FAILURE),
        )
