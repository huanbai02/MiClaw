"""锁定 structured Tool failure 到 execution attempt 的保守 disposition bridge。"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

import miclaw.core.agent.graph as agent_graph
from miclaw.core.agent.execution import AgentExecutionRuntimeError, apply_graph_recursion_limit, run_agent_execution
from miclaw.core.agent.recovery import apply_checkpoint_correlation, latest_owned_checkpoint, new_checkpoint_run_id, recover_execution
from miclaw.core.execution.failures import ExecutionFailure, ExecutionFailureCode, ExecutionFailureSource
from miclaw.core.execution.models import ExecutionStatus
from miclaw.core.execution.recovery import RecoveryDecision, RecoveryReason
from miclaw.core.execution.retry import RetryDecision, RetryPolicy, evaluate_retry
from miclaw.core.execution.tool_failures import (
    TOOL_FAILURE_DISPOSITIONS,
    ToolFailureDisposition,
    get_tool_failure_disposition,
)
from miclaw.core.observability.trace import TraceContext
from miclaw.core.runtime.execution_store import ExecutionStore, ExecutionStoreError
from miclaw.core.tools.base import miclaw_tool
from miclaw.core.tools.result import tool_error, tool_success


class _SequentialModel:
    """提供确定性 Tool call/最终回答，记录 model invocation 次数。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def bind_tools(self, _tools):
        return self

    def invoke(self, _messages):
        self.calls += 1
        return self.responses.pop(0)


def _tool_call(name: str, args: dict[str, object] | None = None, call_id: str = "tool-call") -> AIMessage:
    """构造真实 LangGraph ToolNode 消费的 model Tool call。"""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": call_id, "type": "tool_call"}])


def _run_with_store(tmp_path, monkeypatch, tools, responses, execution_id: str):
    """经真实 graph/ToolNode/wrapper/store 执行一轮，不引入 provider 或 replay。"""
    async def scenario():
        model = _SequentialModel(responses)
        store = ExecutionStore(tmp_path / f"{execution_id}.sqlite3")
        try:
            with patch.object(agent_graph, "get_provider", return_value=model):
                graph = agent_graph.create_agent_app(tools=list(tools))
            result = await run_agent_execution(
                lambda: graph.ainvoke({"messages": [HumanMessage(content="run")], "summary": ""}),
                execution_id=execution_id,
                trace_context=TraceContext(run_id=f"trace-{execution_id}"),
                execution_store=store,
                checkpoint_thread_id=f"thread-{execution_id}",
                checkpoint_run_id=f"checkpoint-{execution_id}",
                retry_policy=RetryPolicy(3),
            )
            return result, model, store
        except BaseException:
            store.close()
            raise

    result, model, store = asyncio.run(scenario())
    return result, model, store


def test_disposition_matrix_is_exhaustive_and_terminal_codes_are_non_retryable():
    """所有当前 failure code 都有显式 disposition；terminal 集合绝不进入 retry。"""
    assert set(TOOL_FAILURE_DISPOSITIONS) == set(ExecutionFailureCode)
    expected_terminal = {
        ExecutionFailureCode.SAFETY_BLOCKED,
        ExecutionFailureCode.INVALID_CONFIGURATION,
        ExecutionFailureCode.UNKNOWN_ERROR,
    }
    for code in ExecutionFailureCode:
        failure = ExecutionFailure(ExecutionFailureSource.TOOL, code)
        disposition = get_tool_failure_disposition(failure)
        if code in expected_terminal:
            assert disposition is ToolFailureDisposition.ATTEMPT_FAIL
            assert evaluate_retry(failure, current_attempt=1, policy=RetryPolicy(3)).decision is RetryDecision.DO_NOT_RETRY
        else:
            assert disposition is ToolFailureDisposition.MODEL_CONTINUE


@pytest.mark.parametrize(
    ("error_type", "expected_code"),
    [
        ("blocked_shell_command", ExecutionFailureCode.SAFETY_BLOCKED),
        ("mcp_spawn_error", ExecutionFailureCode.INVALID_CONFIGURATION),
        ("future_structured_error", ExecutionFailureCode.UNKNOWN_ERROR),
    ],
)
def test_terminal_structured_tool_failures_fail_attempt_without_second_model_call(tmp_path, monkeypatch, error_type, expected_code):
    """hard/config/unknown outcome 经 boundary 变为 durable TOOL FAILED，绝不再调用模型。"""
    tool_calls = 0

    @miclaw_tool
    def terminal_tool() -> object:
        """返回当前 case 的 structured failure。"""
        nonlocal tool_calls
        tool_calls += 1
        return tool_error(error_type, "MODEL_VISIBLE_TOOL_FAILURE")

    result, model, store = _run_with_store(
        tmp_path,
        monkeypatch,
        [terminal_tool],
        [_tool_call("terminal_tool"), AIMessage(content="must not be consumed")],
        f"terminal-{expected_code.value}",
    )
    try:
        assert tool_calls == 1
        assert model.calls == 1
        assert result.state.status is ExecutionStatus.FAILED
        assert result.failure == ExecutionFailure(ExecutionFailureSource.TOOL, expected_code)
        assert result.retry_evaluation.decision is RetryDecision.DO_NOT_RETRY
        assert result.next_attempt is None
        assert store.get_attempt(result.state.execution_id, 1).failure == result.failure
        with pytest.raises(ExecutionStoreError, match="^execution_record_not_found$"):
            store.get_attempt(result.state.execution_id, 2)
    finally:
        store.close()


def test_model_correctable_tool_failures_continue_without_whole_attempt_retry(tmp_path, monkeypatch):
    """invalid input、permission deny、timeout 继续到模型；timeout 不创建 PENDING retry。"""
    tool_calls: list[str] = []

    @miclaw_tool
    def invalid_input_tool() -> object:
        """首次参数错误由模型修正。"""
        tool_calls.append("invalid")
        return tool_error("invalid_mode", "invalid input")

    @miclaw_tool
    def permission_tool() -> object:
        """用户拒绝仍交给模型解释。"""
        tool_calls.append("permission")
        return tool_error("permission_denied", "permission denied")

    @miclaw_tool
    def timeout_tool() -> object:
        """Tool timeout 不触发 whole-attempt retry。"""
        tool_calls.append("timeout")
        return tool_error("timeout", "timeout")

    result, model, store = _run_with_store(
        tmp_path,
        monkeypatch,
        [invalid_input_tool, permission_tool, timeout_tool],
        [
            _tool_call("invalid_input_tool", call_id="invalid"),
            _tool_call("permission_tool", call_id="permission"),
            _tool_call("timeout_tool", call_id="timeout"),
            AIMessage(content="fallback complete"),
        ],
        "model-continue",
    )
    try:
        assert tool_calls == ["invalid", "permission", "timeout"]
        assert model.calls == 4
        assert result.state.status is ExecutionStatus.SUCCEEDED
        assert result.failure is result.retry_evaluation is result.next_attempt is None
        with pytest.raises(ExecutionStoreError, match="^execution_record_not_found$"):
            store.get_attempt("model-continue", 2)
    finally:
        store.close()


def test_mixed_batch_uses_safety_precedence_and_does_not_roll_back_prior_tool(tmp_path, monkeypatch):
    """batch 仍完整执行；success/invalid 后的 safety outcome 选择 SAFETY_BLOCKED 且不再调用模型。"""
    calls: list[str] = []

    @miclaw_tool
    def success_tool() -> object:
        """模拟先完成的副作用 Tool。"""
        calls.append("success")
        return tool_success("side effect completed")

    @miclaw_tool
    def invalid_tool() -> object:
        """模型可修正错误。"""
        calls.append("invalid")
        return tool_error("invalid_mode", "invalid input")

    @miclaw_tool
    def safety_tool() -> object:
        """hard safety block。"""
        calls.append("safety")
        return tool_error("blocked_shell_command", "blocked")

    result, model, store = _run_with_store(
        tmp_path,
        monkeypatch,
        [success_tool, invalid_tool, safety_tool],
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "success_tool", "args": {}, "id": "batch-success", "type": "tool_call"},
                    {"name": "invalid_tool", "args": {}, "id": "batch-invalid", "type": "tool_call"},
                    {"name": "safety_tool", "args": {}, "id": "batch-safety", "type": "tool_call"},
                ],
            ),
            AIMessage(content="must not be consumed"),
        ],
        "batch-safety",
    )
    try:
        assert calls == ["success", "invalid", "safety"]
        assert model.calls == 1
        assert result.failure == ExecutionFailure(ExecutionFailureSource.TOOL, ExecutionFailureCode.SAFETY_BLOCKED)
        assert result.retry_evaluation.decision is RetryDecision.DO_NOT_RETRY
        assert result.next_attempt is None
    finally:
        store.close()


def test_legacy_tool_message_text_never_drives_disposition(tmp_path, monkeypatch):
    """artifact 缺失时即使 content 含 safety 字样，也不解析文本并终止 attempt。"""
    @miclaw_tool
    def legacy_tool() -> str:
        """返回非 ToolResult 的 legacy string。"""
        return "SAFETY_BLOCKED timeout permission denied"

    result, model, store = _run_with_store(
        tmp_path,
        monkeypatch,
        [legacy_tool],
        [_tool_call("legacy_tool"), AIMessage(content="legacy handled")],
        "legacy-neutral",
    )
    try:
        assert model.calls == 2
        assert result.state.status is ExecutionStatus.SUCCEEDED
    finally:
        store.close()



def test_terminal_tool_checkpoint_remains_unrecoverable_when_failed_persistence_fails(tmp_path, monkeypatch):
    """terminal Tool failure 的 checkpoint 指向 failure boundary，重启 recovery 不会误标成功或重放 Tool。"""
    async def scenario():
        checkpoint_path = tmp_path / "state.sqlite3"
        execution_path = tmp_path / "execution.sqlite3"
        tool_calls = 0
        model_calls = 0

        @miclaw_tool
        def blocked_tool() -> object:
            """模拟已经执行一次的 hard safety Tool outcome。"""
            nonlocal tool_calls
            tool_calls += 1
            return tool_error("blocked_shell_command", "blocked")

        class BlockedToolModel:
            """第一轮只发 Tool call；failure boundary 前不得再次调用。"""

            def bind_tools(self, _tools):
                return self

            def invoke(self, _messages):
                nonlocal model_calls
                model_calls += 1
                return _tool_call("blocked_tool", call_id="blocked-call")

        store = ExecutionStore(execution_path)
        thread_id = "terminal-tool-thread"
        checkpoint_run_id = new_checkpoint_run_id()
        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            with patch.object(agent_graph, "get_provider", return_value=BlockedToolModel()):
                app = agent_graph.create_agent_app(tools=[blocked_tool], checkpointer=saver)
            config = apply_checkpoint_correlation(
                apply_graph_recursion_limit({"configurable": {"thread_id": thread_id}, "callbacks": []}),
                checkpoint_run_id,
            )

            async def checkpoint_id() -> str | None:
                ref, _ = await latest_owned_checkpoint(app, thread_id, checkpoint_run_id)
                return ref.checkpoint_id if ref is not None else None

            async def invoke():
                return await app.ainvoke(
                    {"messages": [HumanMessage(content="run")], "summary": ""},
                    config=config,
                    durability="sync",
                )

            def fail_failed_persistence(_failed, _next_attempt):
                raise ExecutionStoreError("execution_store_error")

            monkeypatch.setattr(store, "fail_and_plan_next", fail_failed_persistence)
            with pytest.raises(AgentExecutionRuntimeError, match="^execution_persistence_failed$"):
                await run_agent_execution(
                    invoke,
                    execution_id="terminal-tool-persistence",
                    trace_context=TraceContext(run_id="trace-terminal-tool"),
                    execution_store=store,
                    checkpoint_thread_id=thread_id,
                    checkpoint_run_id=checkpoint_run_id,
                    checkpoint_id_provider=checkpoint_id,
                )
            ref, next_nodes = await latest_owned_checkpoint(app, thread_id, checkpoint_run_id)
            assert ref is not None
            assert next_nodes == ("tool_failure_boundary",)
            assert tool_calls == 1
            assert model_calls == 1
            assert store.get_attempt("terminal-tool-persistence", 1).state.status is ExecutionStatus.RUNNING
        store.close()

        reopened_store = ExecutionStore(execution_path)
        recovery_model_calls = 0

        class NeverInvokeModel:
            """recovery 只应读取 checkpoint，绝不执行 graph。"""

            def bind_tools(self, _tools):
                return self

            def invoke(self, _messages):
                nonlocal recovery_model_calls
                recovery_model_calls += 1
                raise AssertionError("recovery must not invoke graph")

        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as reopened_saver:
            with patch.object(agent_graph, "get_provider", return_value=NeverInvokeModel()):
                reopened_app = agent_graph.create_agent_app(tools=[blocked_tool], checkpointer=reopened_saver)
            recovery = await recover_execution(reopened_store, reopened_app, "terminal-tool-persistence", 1)
            assert recovery.assessment.decision is RecoveryDecision.DO_NOT_RESUME
            assert recovery.assessment.reason is RecoveryReason.UNSAFE_NEXT_NODE
            assert recovery.record.state.status is ExecutionStatus.INTERRUPTED
        assert tool_calls == 1
        assert recovery_model_calls == 0
        with pytest.raises(ExecutionStoreError, match="^execution_record_not_found$"):
            reopened_store.get_attempt("terminal-tool-persistence", 2)
        reopened_store.close()

    asyncio.run(scenario())
