"""验证 LangGraph checkpoint correlation、safe recovery 与 no-replay 边界。"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from miclaw.core.agent.execution import AgentExecutionRuntimeError, apply_graph_recursion_limit, run_agent_execution
from miclaw.core.agent.graph import create_agent_app
from miclaw.core.agent.recovery import (
    AgentRecoveryError,
    apply_checkpoint_correlation,
    apply_checkpoint_resume_config,
    latest_owned_checkpoint,
    new_checkpoint_run_id,
    recover_execution,
)
from miclaw.core.execution.models import ExecutionStatus
from miclaw.core.execution.recovery import ExecutionAttemptRecord, RecoveryDecision, RecoveryReason
from miclaw.core.execution.retry import RetryDecision as RetryPolicyDecision
from miclaw.core.execution.state import create_pending_execution, start_execution
from miclaw.core.observability.trace import TraceContext
from miclaw.core.runtime.execution_store import ExecutionStore, ExecutionStoreError


async def _checkpoint_id(app, thread_id, run_id):
    """取得 exact metadata.run_id 归属的最新 opaque checkpoint id。"""
    ref, _ = await latest_owned_checkpoint(app, thread_id, run_id)
    return ref.checkpoint_id if ref else None


def _base_config(thread_id="thread-1"):
    """构造生产调用同形的 config，并锁定 recursion_limit merge。"""
    return apply_graph_recursion_limit({"configurable": {"thread_id": thread_id}, "callbacks": []})


def test_checkpoint_correlation_preserves_existing_config_and_uses_sync_disk_checkpoint(tmp_path):
    """run correlation 写入 checkpoint metadata，既有 config 不被 recursion/durability 接入覆盖。"""
    async def scenario():
        checkpoint_path = tmp_path / "state.sqlite3"
        class Model:
            def bind_tools(self, _tools): return self
            def invoke(self, _messages): return AIMessage(content="ok")

        with patch("miclaw.core.agent.graph.get_provider", return_value=Model()):
            async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
                app = create_agent_app(tools=[], checkpointer=saver)
                run_id = new_checkpoint_run_id()
                config = apply_checkpoint_correlation(_base_config(), run_id)
                await app.ainvoke({"messages": [HumanMessage(content="hello")]}, config=config, durability="sync")
                ref, next_nodes = await latest_owned_checkpoint(app, "thread-1", run_id)
                assert ref is not None and next_nodes == ()
                assert config["recursion_limit"] == 25
                assert config["callbacks"] == []
                assert config["metadata"]["run_id"] == run_id
                assert config["run_id"] == run_id
        assert checkpoint_path.exists()
    asyncio.run(scenario())


def test_safe_provider_timeout_recovery_resumes_disk_checkpoint_without_replaying_tool(tmp_path):
    """Tool 已 checkpoint 后 provider timeout：新 attempt 从 agent checkpoint 恢复，Tool side effect 仅一次。"""
    async def scenario():
        checkpoint_path = tmp_path / "state.sqlite3"
        execution_path = tmp_path / "execution.sqlite3"
        tool_calls = 0

        @tool
        def side_effect_tool(value: str) -> str:
            """记录真实 ToolNode 调用次数。"""
            nonlocal tool_calls
            tool_calls += 1
            return value

        class ToolThenTimeout:
            def __init__(self): self.calls = 0
            def bind_tools(self, _tools): return self
            def invoke(self, _messages):
                self.calls += 1
                if self.calls == 1:
                    return AIMessage(content="", tool_calls=[{"name": "side_effect_tool", "args": {"value": "once"}, "id": "tool-1"}])
                raise TimeoutError("RECOVERY_PROVIDER_SECRET")

        store = ExecutionStore(execution_path)
        first_model = ToolThenTimeout()
        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            with patch("miclaw.core.agent.graph.get_provider", return_value=first_model):
                app = create_agent_app(tools=[side_effect_tool], checkpointer=saver)
            thread_id, checkpoint_run_id = "thread-1", new_checkpoint_run_id()
            config = apply_checkpoint_correlation(_base_config(thread_id), checkpoint_run_id)

            async def invoke():
                return await app.ainvoke({"messages": [HumanMessage(content="EXECUTION_DB_SECRET_PAYLOAD")]}, config=config, durability="sync")

            result = await run_agent_execution(
                invoke,
                execution_id="execution-1",
                trace_context=TraceContext(run_id="trace-1"),
                execution_store=store,
                checkpoint_thread_id=thread_id,
                checkpoint_run_id=checkpoint_run_id,
                checkpoint_id_provider=lambda: _checkpoint_id(app, thread_id, checkpoint_run_id),
            )
            assert result.state.status is ExecutionStatus.FAILED
            assert result.next_attempt is not None
            assert tool_calls == 1
        store.close()

        # 真实磁盘 close/reopen 后，只从 exact owned checkpoint 继续。
        reopened_store = ExecutionStore(execution_path)
        class SuccessModel:
            def bind_tools(self, _tools): return self
            def invoke(self, _messages): return AIMessage(content="recovered")

        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as reopened_saver:
            with patch("miclaw.core.agent.graph.get_provider", return_value=SuccessModel()):
                resumed_app = create_agent_app(tools=[side_effect_tool], checkpointer=reopened_saver)
            recovery = await recover_execution(reopened_store, resumed_app, "execution-1", 1)
            assert recovery.assessment.decision is RecoveryDecision.RESUME_FROM_CHECKPOINT
            assert recovery.assessment.reason is RecoveryReason.SAFE_CHECKPOINT_AVAILABLE
            assert recovery.resume_record is not None
            assert recovery.resume_record.state.attempt == 2
            next_run_id = new_checkpoint_run_id()
            resume_config = apply_checkpoint_resume_config(_base_config(thread_id), recovery.assessment.checkpoint_ref, next_run_id)

            async def resume():
                return await resumed_app.ainvoke(None, config=resume_config, durability="sync")

            resumed = await run_agent_execution(
                resume,
                pending_state=recovery.resume_record.state,
                trace_context=TraceContext(run_id="trace-2"),
                execution_store=reopened_store,
                checkpoint_thread_id=thread_id,
                checkpoint_run_id=next_run_id,
                checkpoint_id_provider=lambda: _checkpoint_id(resumed_app, thread_id, next_run_id),
            )
            assert resumed.state.status is ExecutionStatus.SUCCEEDED
            assert resumed.state.execution_id == "execution-1"
            assert resumed.state.attempt == 2
            assert resumed.state.run_id == "trace-2"
            assert tool_calls == 1
        reopened_store.close()
        database_bytes = execution_path.read_bytes()
        assert b"RECOVERY_PROVIDER_SECRET" not in database_bytes
        assert b"EXECUTION_DB_SECRET_PAYLOAD" not in database_bytes
    asyncio.run(scenario())


def test_unsafe_tool_checkpoint_is_interrupted_without_tool_replay(tmp_path):
    """next=ToolNode 的 checkpoint 不会恢复，RUNNING attempt 只会被标记 INTERRUPTED。"""
    async def scenario():
        checkpoint_path = tmp_path / "state.sqlite3"
        store = ExecutionStore(tmp_path / "execution.sqlite3")
        tool_calls = 0

        @tool
        def side_effect_tool(value: str) -> str:
            """不应在 recovery 中执行。"""
            nonlocal tool_calls
            tool_calls += 1
            return value

        class ToolModel:
            def bind_tools(self, _tools): return self
            def invoke(self, _messages):
                return AIMessage(content="", tool_calls=[{"name": "side_effect_tool", "args": {"value": "x"}, "id": "tool-1"}])

        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            with patch("miclaw.core.agent.graph.get_provider", return_value=ToolModel()):
                app = create_agent_app(tools=[side_effect_tool], checkpointer=saver)
            run_id = new_checkpoint_run_id()
            config = apply_checkpoint_correlation(_base_config(), run_id)
            pending = create_pending_execution("execution-unsafe")
            running = start_execution(pending, run_id="trace-1", started_at=datetime.now(timezone.utc))
            store.create_attempt(ExecutionAttemptRecord(pending, checkpoint_thread_id="thread-1", checkpoint_run_id=run_id))
            store.transition(ExecutionAttemptRecord(running, checkpoint_thread_id="thread-1", checkpoint_run_id=run_id), expected_status=ExecutionStatus.PENDING)
            await app.ainvoke({"messages": [HumanMessage(content="tool")]}, config=config, interrupt_before=["tools"], durability="sync")
            recovery = await recover_execution(store, app, "execution-unsafe", 1)
            assert recovery.assessment.decision is RecoveryDecision.DO_NOT_RESUME
            assert recovery.assessment.reason is RecoveryReason.UNSAFE_NEXT_NODE
            assert recovery.record.state.status is ExecutionStatus.INTERRUPTED
            assert tool_calls == 0
        store.close()
    asyncio.run(scenario())


def test_completed_checkpoint_reconciles_running_attempt_without_graph_replay(tmp_path):
    """图已 terminal、metadata 仍 RUNNING 时只 MARK_SUCCEEDED，不再调用 graph。"""
    async def scenario():
        checkpoint_path = tmp_path / "state.sqlite3"
        store = ExecutionStore(tmp_path / "execution.sqlite3")
        model_calls = 0
        class Model:
            def bind_tools(self, _tools): return self
            def invoke(self, _messages):
                nonlocal model_calls
                model_calls += 1
                return AIMessage(content="done")

        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            with patch("miclaw.core.agent.graph.get_provider", return_value=Model()):
                app = create_agent_app(tools=[], checkpointer=saver)
            run_id = new_checkpoint_run_id()
            config = apply_checkpoint_correlation(_base_config(), run_id)
            pending = create_pending_execution("execution-complete")
            running = start_execution(pending, run_id="trace-1", started_at=datetime.now(timezone.utc))
            store.create_attempt(ExecutionAttemptRecord(pending, checkpoint_thread_id="thread-1", checkpoint_run_id=run_id))
            store.transition(ExecutionAttemptRecord(running, checkpoint_thread_id="thread-1", checkpoint_run_id=run_id), expected_status=ExecutionStatus.PENDING)
            await app.ainvoke({"messages": [HumanMessage(content="done")]}, config=config, durability="sync")
            assert model_calls == 1
            recovery = await recover_execution(store, app, "execution-complete", 1)
            assert recovery.assessment.decision is RecoveryDecision.MARK_SUCCEEDED
            assert recovery.record.state.status is ExecutionStatus.SUCCEEDED
            assert model_calls == 1
        store.close()
    asyncio.run(scenario())


def test_missing_checkpoint_interrupts_without_replay_or_new_attempt(tmp_path):
    """没有 exact owned checkpoint 时 fail closed，不从 START 重放。"""
    async def scenario():
        store = ExecutionStore(tmp_path / "execution.sqlite3")
        pending = create_pending_execution("execution-missing")
        running = start_execution(pending, run_id="trace-1", started_at=datetime.now(timezone.utc))
        store.create_attempt(ExecutionAttemptRecord(pending, checkpoint_thread_id="thread-1", checkpoint_run_id="run-missing"))
        store.transition(ExecutionAttemptRecord(running, checkpoint_thread_id="thread-1", checkpoint_run_id="run-missing"), expected_status=ExecutionStatus.PENDING)

        class NoCheckpointGraph:
            async def aget_state_history(self, _config):
                if False: yield None

        recovery = await recover_execution(store, NoCheckpointGraph(), "execution-missing", 1)
        assert recovery.assessment.decision is RecoveryDecision.DO_NOT_RESUME
        assert recovery.assessment.reason is RecoveryReason.NO_MATCHING_CHECKPOINT
        assert recovery.record.state.status is ExecutionStatus.INTERRUPTED
        with pytest.raises(Exception):
            store.get_attempt("execution-missing", 2)
        store.close()
    asyncio.run(scenario())


def test_checkpoint_lookup_uses_run_ownership_not_thread_latest(tmp_path):
    """同一 thread 的较晚 run 不能夺取旧 attempt 的 checkpoint ownership。"""
    async def scenario():
        checkpoint_path = tmp_path / "state.sqlite3"
        class Model:
            def bind_tools(self, _tools): return self
            def invoke(self, _messages): return AIMessage(content="ok")

        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            with patch("miclaw.core.agent.graph.get_provider", return_value=Model()):
                app = create_agent_app(tools=[], checkpointer=saver)
            run_a, run_b = new_checkpoint_run_id(), new_checkpoint_run_id()
            await app.ainvoke({"messages": [HumanMessage(content="A")]}, config=apply_checkpoint_correlation(_base_config(), run_a), durability="sync")
            ref_a, _ = await latest_owned_checkpoint(app, "thread-1", run_a)
            await app.ainvoke({"messages": [HumanMessage(content="B")]}, config=apply_checkpoint_correlation(_base_config(), run_b), durability="sync")
            ref_b, _ = await latest_owned_checkpoint(app, "thread-1", run_b)
            selected_a, _ = await latest_owned_checkpoint(app, "thread-1", run_a)
            assert ref_a is not None and ref_b is not None and selected_a is not None
            assert selected_a.checkpoint_id == ref_a.checkpoint_id
            assert selected_a.checkpoint_id != ref_b.checkpoint_id
    asyncio.run(scenario())


def test_persistence_failure_before_invoke_blocks_graph_and_terminal_failure_never_replays(tmp_path, monkeypatch):
    """无法持久化 RUNNING 时不执行图；terminal 持久化失败后也不会重新调用图。"""
    async def scenario():
        from miclaw.core.agent.execution import AgentExecutionRuntimeError
        from miclaw.core.runtime.execution_store import ExecutionStoreError

        store = ExecutionStore(tmp_path / "execution.sqlite3")
        calls = 0

        async def invoke():
            nonlocal calls
            calls += 1
            return "done"

        def fail_transition(*_args, **_kwargs):
            raise ExecutionStoreError("execution_store_error")

        monkeypatch.setattr(store, "transition", fail_transition)
        with pytest.raises(AgentExecutionRuntimeError, match="^execution_persistence_failed$"):
            await run_agent_execution(
                invoke,
                execution_id="execution-store-preflight",
                trace_context=TraceContext(run_id="trace-1"),
                execution_store=store,
                checkpoint_thread_id="thread-1",
                checkpoint_run_id="checkpoint-run-1",
            )
        assert calls == 0
        monkeypatch.undo()

        transition_calls = 0
        original_transition = store.transition

        def fail_terminal(record, *, expected_status):
            nonlocal transition_calls
            transition_calls += 1
            if transition_calls == 2:
                raise ExecutionStoreError("execution_store_error")
            return original_transition(record, expected_status=expected_status)

        monkeypatch.setattr(store, "transition", fail_terminal)
        with pytest.raises(AgentExecutionRuntimeError, match="^execution_persistence_failed$"):
            await run_agent_execution(
                invoke,
                execution_id="execution-store-terminal",
                trace_context=TraceContext(run_id="trace-2"),
                execution_store=store,
                checkpoint_thread_id="thread-2",
                checkpoint_run_id="checkpoint-run-2",
            )
        assert calls == 1
        assert store.get_attempt("execution-store-terminal", 1).state.status is ExecutionStatus.RUNNING
        store.close()
    asyncio.run(scenario())


def test_terminal_persistence_failure_reconciles_completed_disk_checkpoint_without_tool_replay(tmp_path, monkeypatch):
    """真实 ToolNode 完成后终态落库失败，重启仅 reconciliation，不重放副作用。"""
    async def scenario():
        checkpoint_path = tmp_path / "state.sqlite3"
        execution_path = tmp_path / "execution.sqlite3"
        tool_calls = 0
        model_calls = 0

        @tool
        def side_effect_tool(value: str) -> str:
            """记录真实 ToolNode 的唯一副作用。"""
            nonlocal tool_calls
            tool_calls += 1
            return value

        class ToolThenFinish:
            def bind_tools(self, _tools): return self
            def invoke(self, _messages):
                nonlocal model_calls
                model_calls += 1
                if model_calls == 1:
                    return AIMessage(content="", tool_calls=[{"name": "side_effect_tool", "args": {"value": "once"}, "id": "tool-1"}])
                return AIMessage(content="done")

        store = ExecutionStore(execution_path)
        checkpoint_run_id = new_checkpoint_run_id()
        thread_id = "terminal-persistence-thread"
        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            with patch("miclaw.core.agent.graph.get_provider", return_value=ToolThenFinish()):
                app = create_agent_app(tools=[side_effect_tool], checkpointer=saver)
            original_transition = store.transition

            def fail_succeeded(record, *, expected_status):
                if expected_status is ExecutionStatus.RUNNING and record.state.status is ExecutionStatus.SUCCEEDED:
                    raise ExecutionStoreError("execution_store_error")
                return original_transition(record, expected_status=expected_status)

            monkeypatch.setattr(store, "transition", fail_succeeded)
            config = apply_checkpoint_correlation(_base_config(thread_id), checkpoint_run_id)

            async def invoke():
                return await app.ainvoke(
                    {"messages": [HumanMessage(content="terminal persistence")]},
                    config=config,
                    durability="sync",
                )

            with pytest.raises(AgentExecutionRuntimeError, match="^execution_persistence_failed$"):
                await run_agent_execution(
                    invoke,
                    execution_id="execution-terminal-persistence",
                    trace_context=TraceContext(run_id="trace-terminal-persistence"),
                    execution_store=store,
                    checkpoint_thread_id=thread_id,
                    checkpoint_run_id=checkpoint_run_id,
                    checkpoint_id_provider=lambda: _checkpoint_id(app, thread_id, checkpoint_run_id),
                )
            checkpoint_ref, next_nodes = await latest_owned_checkpoint(app, thread_id, checkpoint_run_id)
            assert checkpoint_ref is not None and next_nodes == ()
            assert model_calls == 2 and tool_calls == 1
            assert store.get_attempt("execution-terminal-persistence", 1).state.status is ExecutionStatus.RUNNING
        store.close()

        reopened_store = ExecutionStore(execution_path)
        recovery_model_calls = 0

        class NeverInvokeModel:
            def bind_tools(self, _tools): return self
            def invoke(self, _messages):
                nonlocal recovery_model_calls
                recovery_model_calls += 1
                raise AssertionError("recovery must not invoke graph")

        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as reopened_saver:
            with patch("miclaw.core.agent.graph.get_provider", return_value=NeverInvokeModel()):
                reopened_app = create_agent_app(tools=[side_effect_tool], checkpointer=reopened_saver)
            recovery = await recover_execution(reopened_store, reopened_app, "execution-terminal-persistence", 1)
            assert recovery.assessment.decision is RecoveryDecision.MARK_SUCCEEDED
            assert recovery.assessment.reason is RecoveryReason.GRAPH_ALREADY_COMPLETE
            assert recovery.record.state.status is ExecutionStatus.SUCCEEDED
        assert recovery_model_calls == 0
        assert tool_calls == 1
        assert reopened_store.get_attempt("execution-terminal-persistence", 1).state.status is ExecutionStatus.SUCCEEDED
        reopened_store.close()
    asyncio.run(scenario())
