"""Execution control CLI 的只读边界与 targeted recovery 回归。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from typer.testing import CliRunner

from entry.cli import app
from miclaw.core.agent.execution import AgentProviderFailure, run_agent_execution
import miclaw.core.agent.graph as agent_graph
from miclaw.core.agent.recovery import apply_checkpoint_correlation, latest_owned_checkpoint, new_checkpoint_run_id
from miclaw.core.agent.graph import create_agent_app
from miclaw.core.execution.failures import ExecutionFailure, ExecutionFailureCode, ExecutionFailureSource
from miclaw.core.execution.models import ExecutionStatus
from miclaw.core.execution.recovery import ExecutionAttemptRecord
from miclaw.core.execution.retry import RetryDecision, RetryDecisionReason, RetryEvaluation
from miclaw.core.execution.state import create_pending_execution, mark_execution_failed, start_execution
from miclaw.core.runtime.execution_store import ExecutionStore
from miclaw.core.observability.trace import TraceContext


runner = CliRunner()


def _running(store: ExecutionStore, execution_id: str, *, checkpoint_run_id: str | None = None):
    started = datetime.now(timezone.utc)
    pending = create_pending_execution(execution_id)
    running = start_execution(pending, run_id="trace-control", started_at=started)
    record = ExecutionAttemptRecord(
        pending,
        checkpoint_thread_id="CHECKPOINT_THREAD_SECRET",
        checkpoint_run_id=checkpoint_run_id or "CHECKPOINT_RUN_SECRET",
        checkpoint_id="CHECKPOINT_ID_SECRET",
    )
    store.create_attempt(record)
    store.transition(
        ExecutionAttemptRecord(
            running,
            checkpoint_thread_id=record.checkpoint_thread_id,
            checkpoint_run_id=record.checkpoint_run_id,
            checkpoint_id=record.checkpoint_id,
        ),
        expected_status=ExecutionStatus.PENDING,
    )
    return pending, running, started


def _make_list_fixture(workspace):
    store = ExecutionStore(workspace / "execution.sqlite3")
    pending_a = create_pending_execution("execution-a")
    store.create_attempt(ExecutionAttemptRecord(pending_a))
    _, running_b, started_b = _running(store, "execution-b")
    failed = ExecutionAttemptRecord(
        mark_execution_failed(running_b, finished_at=started_b),
        ExecutionFailure(ExecutionFailureSource.PROVIDER, ExecutionFailureCode.PROVIDER_TIMEOUT),
        RetryEvaluation(RetryDecision.RETRY, RetryDecisionReason.RETRYABLE_FAILURE),
    )
    store.fail_and_plan_next(failed, ExecutionAttemptRecord(create_pending_execution("execution-b", attempt=2)))
    store.close()


def test_execution_list_and_show_are_readonly_safe_views_without_provider(tmp_path, monkeypatch):
    """list/show 只经 readonly Store 解码，不构建 provider/graph，不输出内部 checkpoint。"""
    _make_list_fixture(tmp_path)
    database_path = tmp_path / "execution.sqlite3"
    before = database_path.read_bytes()
    monkeypatch.setattr(agent_graph, "get_provider", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no provider")))

    listed = runner.invoke(app, ["execution", "list", "--workspace", str(tmp_path)])
    shown = runner.invoke(app, ["execution", "show", "execution-b", "--workspace", str(tmp_path)])

    assert listed.exit_code == shown.exit_code == 0
    assert "execution-a" in listed.output and "execution-b" in listed.output
    assert "execution-b" in shown.output
    assert "failed" in shown.output and "pending" in shown.output
    assert "provider_timeout" in shown.output and "retry" in shown.output
    assert "CHECKPOINT_" not in listed.output + shown.output
    assert "EXECUTION_CONTROL_SECRET_PAYLOAD" not in listed.output + shown.output
    assert database_path.read_bytes() == before


def test_execution_read_commands_handle_empty_and_unknown_without_creating_db(tmp_path):
    """空 workspace list 是稳定空结果；show unknown 非零且不创建 execution DB。"""
    listed = runner.invoke(app, ["execution", "list", "--workspace", str(tmp_path)])
    shown = runner.invoke(app, ["execution", "show", "unknown", "--workspace", str(tmp_path)])

    assert listed.exit_code == 0 and "No executions found." in listed.output
    assert shown.exit_code != 0 and "execution_not_found" in shown.output
    assert not (tmp_path / "execution.sqlite3").exists()


def test_execution_control_help_and_limit_are_discoverable():
    """控制面子命令与有界 list limit 都可发现。"""
    for args in (["execution", "--help"], ["execution", "list", "--help"], ["execution", "show", "--help"], ["execution", "recover", "--help"]):
        assert runner.invoke(app, args).exit_code == 0
    invalid = runner.invoke(app, ["execution", "list", "--limit", "0"])
    assert invalid.exit_code != 0 and "invalid_limit" in invalid.output


def test_real_agent_execution_is_visible_through_execution_list_and_show(tmp_path, monkeypatch):
    """真实 graph + execution wrapper 落库后，CLI 仅展示安全 metadata。"""
    state_path = tmp_path / "state.sqlite3"
    store = ExecutionStore(tmp_path / "execution.sqlite3")

    class Model:
        def bind_tools(self, _tools):
            return self

        def invoke(self, _messages):
            return AIMessage(content="done")

    monkeypatch.setattr(agent_graph, "get_provider", lambda **_kwargs: Model())

    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(state_path)) as saver:
            graph = create_agent_app(tools=[], checkpointer=saver)
            checkpoint_run_id = new_checkpoint_run_id()
            config = apply_checkpoint_correlation({"configurable": {"thread_id": "control-thread"}}, checkpoint_run_id)

            async def invoke():
                return await graph.ainvoke(
                    {"messages": [HumanMessage(content="EXECUTION_CONTROL_SECRET_PAYLOAD")]},
                    config=config,
                    durability="sync",
                )

            await run_agent_execution(
                invoke,
                execution_id="execution-real-agent",
                trace_context=TraceContext(run_id="control-trace"),
                execution_store=store,
                checkpoint_thread_id="control-thread",
                checkpoint_run_id=checkpoint_run_id,
                checkpoint_id_provider=lambda: _checkpoint_id(graph, "control-thread", checkpoint_run_id),
            )

    asyncio.run(scenario())
    store.close()
    listed = runner.invoke(app, ["execution", "list", "--workspace", str(tmp_path)])
    shown = runner.invoke(app, ["execution", "show", "execution-real-agent", "--workspace", str(tmp_path)])
    assert listed.exit_code == shown.exit_code == 0
    assert "execution-real-agent" in listed.output + shown.output
    assert "succeeded" in shown.output
    assert "EXECUTION_CONTROL_SECRET_PAYLOAD" not in listed.output + shown.output


def test_execution_recover_cli_reconciles_completed_checkpoint_without_invocation(tmp_path, monkeypatch):
    """completed checkpoint 经 CLI 只 MARK_SUCCEEDED；没有 model/tool replay。"""
    state_path = tmp_path / "state.sqlite3"
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    _, running, _ = _running(store, "execution-complete", checkpoint_run_id="complete-run")
    store.close()
    calls = 0

    class Model:
        def bind_tools(self, _tools):
            return self

        def invoke(self, _messages):
            nonlocal calls
            calls += 1
            return AIMessage(content="done")

    async def setup():
        async with AsyncSqliteSaver.from_conn_string(str(state_path)) as saver:
            graph = create_agent_app(tools=[], checkpointer=saver)
            await graph.ainvoke(
                {"messages": [HumanMessage(content="EXECUTION_CONTROL_SECRET_PAYLOAD")]},
                config=apply_checkpoint_correlation({"configurable": {"thread_id": "CHECKPOINT_THREAD_SECRET"}}, "complete-run"),
                durability="sync",
            )

    monkeypatch.setattr(agent_graph, "get_provider", lambda **_kwargs: Model())
    asyncio.run(setup())
    assert calls == 1
    result = runner.invoke(app, ["execution", "recover", "execution-complete", "--attempt", "1", "--workspace", str(tmp_path)])

    assert result.exit_code == 0
    assert "mark_succeeded" in result.output and "graph_already_complete" in result.output
    assert "EXECUTION_CONTROL_SECRET_PAYLOAD" not in result.output
    assert calls == 1
    reopened = ExecutionStore(tmp_path / "execution.sqlite3")
    assert reopened.get_attempt("execution-complete", 1).state.status is ExecutionStatus.SUCCEEDED
    reopened.close()


def test_execution_recover_cli_plans_one_safe_next_attempt_without_replay(tmp_path, monkeypatch):
    """safe agent checkpoint 经 CLI 只产生 N+1 PENDING；重复命令不产生 N+2。"""
    state_path = tmp_path / "state.sqlite3"
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    checkpoint_run_id = new_checkpoint_run_id()
    _running(store, "execution-safe", checkpoint_run_id=checkpoint_run_id)
    store.close()
    tool_calls = 0
    model_calls = 0

    @tool
    def side_effect_tool(value: str) -> str:
        """只在首次准备 checkpoint 时执行；CLI recover 不得再次调用。"""
        nonlocal tool_calls
        tool_calls += 1
        return value

    class ToolThenTimeout:
        def bind_tools(self, _tools):
            return self

        def invoke(self, _messages):
            nonlocal model_calls
            model_calls += 1
            if model_calls == 1:
                return AIMessage(content="", tool_calls=[{"name": "side_effect_tool", "args": {"value": "once"}, "id": "tool-1"}])
            raise TimeoutError("RECOVERY_SECRET")

    monkeypatch.setattr(agent_graph, "get_provider", lambda **_kwargs: ToolThenTimeout())

    async def setup():
        async with AsyncSqliteSaver.from_conn_string(str(state_path)) as saver:
            graph = create_agent_app(tools=[side_effect_tool], checkpointer=saver)
            try:
                await graph.ainvoke(
                    {"messages": [HumanMessage(content="safe")]},
                    config=apply_checkpoint_correlation({"configurable": {"thread_id": "CHECKPOINT_THREAD_SECRET"}}, checkpoint_run_id),
                    durability="sync",
                )
            except AgentProviderFailure:
                pass

    asyncio.run(setup())
    assert model_calls == 2 and tool_calls == 1

    class NeverInvoke:
        def bind_tools(self, _tools):
            return self

        def invoke(self, _messages):
            raise AssertionError("recovery CLI must not invoke model")

    monkeypatch.setattr(agent_graph, "get_provider", lambda **_kwargs: NeverInvoke())
    first = runner.invoke(app, ["execution", "recover", "execution-safe", "--attempt", "1", "--workspace", str(tmp_path)])
    second = runner.invoke(app, ["execution", "recover", "execution-safe", "--attempt", "1", "--workspace", str(tmp_path)])

    assert first.exit_code == 0
    assert "resume_from_checkpoint" in first.output and "Attempt 2: pending" in first.output
    assert "has not been executed" in first.output
    assert second.exit_code != 0 and "status_not_recoverable" in second.output
    assert tool_calls == 1
    reopened = ExecutionStore(tmp_path / "execution.sqlite3")
    assert reopened.get_attempt("execution-safe", 1).state.status is ExecutionStatus.INTERRUPTED
    assert reopened.get_attempt("execution-safe", 2).state.status is ExecutionStatus.PENDING
    try:
        reopened.get_attempt("execution-safe", 3)
        raise AssertionError("recovery must not create N+2")
    except Exception as exc:
        assert str(exc) == "execution_record_not_found"
    finally:
        reopened.close()


def test_execution_recover_cli_refuses_toolnode_checkpoint_without_replay(tmp_path, monkeypatch):
    """ToolNode checkpoint 经 CLI fail closed，不能调用 Tool 或从 START 重放。"""
    state_path = tmp_path / "state.sqlite3"
    checkpoint_run_id = new_checkpoint_run_id()
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    _running(store, "execution-unsafe", checkpoint_run_id=checkpoint_run_id)
    store.close()
    tool_calls = 0
    model_calls = 0

    @tool
    def side_effect_tool(value: str) -> str:
        """interrupt_before tools 与 recovery 都不能触达此副作用。"""
        nonlocal tool_calls
        tool_calls += 1
        return value

    class ToolModel:
        def bind_tools(self, _tools):
            return self

        def invoke(self, _messages):
            nonlocal model_calls
            model_calls += 1
            return AIMessage(content="", tool_calls=[{"name": "side_effect_tool", "args": {"value": "x"}, "id": "tool-1"}])

    monkeypatch.setattr(agent_graph, "get_provider", lambda **_kwargs: ToolModel())

    async def setup():
        async with AsyncSqliteSaver.from_conn_string(str(state_path)) as saver:
            graph = create_agent_app(tools=[side_effect_tool], checkpointer=saver)
            await graph.ainvoke(
                {"messages": [HumanMessage(content="unsafe")]},
                config=apply_checkpoint_correlation({"configurable": {"thread_id": "CHECKPOINT_THREAD_SECRET"}}, checkpoint_run_id),
                interrupt_before=["tools"],
                durability="sync",
            )

    asyncio.run(setup())
    assert model_calls == 1 and tool_calls == 0

    class NeverInvoke:
        def bind_tools(self, _tools):
            return self

        def invoke(self, _messages):
            raise AssertionError("unsafe recovery must not invoke model")

    monkeypatch.setattr(agent_graph, "get_provider", lambda **_kwargs: NeverInvoke())
    result = runner.invoke(app, ["execution", "recover", "execution-unsafe", "--attempt", "1", "--workspace", str(tmp_path)])

    assert result.exit_code != 0
    assert "do_not_resume" in result.output and "unsafe_next_node" in result.output
    assert tool_calls == 0
    reopened = ExecutionStore(tmp_path / "execution.sqlite3")
    assert reopened.get_attempt("execution-unsafe", 1).state.status is ExecutionStatus.INTERRUPTED
    reopened.close()


async def _checkpoint_id(graph, thread_id: str, checkpoint_run_id: str) -> str | None:
    """读取真实 execution wrapper 所属的 latest checkpoint identity。"""
    checkpoint_ref, _ = await latest_owned_checkpoint(graph, thread_id, checkpoint_run_id)
    return checkpoint_ref.checkpoint_id if checkpoint_ref is not None else None
