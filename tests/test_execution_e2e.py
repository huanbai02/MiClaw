"""Phase 5 跨层 E2E：真实 Agent、ToolNode、SQLite checkpoint/store 与安全 execution observability。"""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

import miclaw.core.agent.execution as execution_module
import miclaw.core.agent.recovery as recovery_module
from miclaw.core.agent.execution import AgentExecutionRuntimeError, run_agent_execution
from miclaw.core.agent.graph import create_agent_app
from miclaw.core.agent.recovery import apply_checkpoint_correlation, latest_owned_checkpoint, new_checkpoint_run_id, recover_execution
from miclaw.core.execution.failures import ExecutionFailureCode
from miclaw.core.execution.models import ExecutionStatus
from miclaw.core.execution.recovery import ExecutionAttemptRecord
from miclaw.core.execution.retry import RetryDecision, RetryPolicy
from miclaw.core.execution.state import create_pending_execution, start_execution
from miclaw.core.observability.logger import JSONLEventLogger
from miclaw.core.observability.trace import TraceContext, reset_trace_context, set_current_trace_context
from miclaw.core.runtime.execution_store import ExecutionStore, ExecutionStoreError


async def _checkpoint_id(graph, thread_id: str, checkpoint_run_id: str) -> str | None:
    """读取本次 invocation 精确 owned checkpoint id。"""
    checkpoint_ref, _ = await latest_owned_checkpoint(graph, thread_id, checkpoint_run_id)
    return checkpoint_ref.checkpoint_id if checkpoint_ref is not None else None


def _config(thread_id: str, checkpoint_run_id: str) -> dict[str, object]:
    """构造保留 callback/configurable 的 production-shape checkpoint config。"""
    return apply_checkpoint_correlation(
        {"configurable": {"thread_id": thread_id}, "callbacks": [], "recursion_limit": 25},
        checkpoint_run_id,
    )


def _events(path: Path) -> list[dict]:
    """读取已 shutdown 的 JSONL event 文件。"""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_fresh_success_is_durable_and_emits_metadata_only_execution_events(tmp_path, monkeypatch):
    """真实 graph 成功后 attempt durable，事件仅包含枚举/计数并保留 trace envelope。"""
    async def scenario():
        state_path = tmp_path / "state.sqlite3"
        execution_path = tmp_path / "execution.sqlite3"
        log_path = tmp_path / "events.jsonl"
        logger = JSONLEventLogger(log_file=log_path)
        monkeypatch.setattr(execution_module, "audit_logger", logger)
        calls = 0

        class Model:
            def bind_tools(self, _tools): return self
            def invoke(self, _messages):
                nonlocal calls
                calls += 1
                return AIMessage(content="done")

        store = ExecutionStore(execution_path)
        trace = TraceContext(run_id="trace-explicit")
        try:
            async with AsyncSqliteSaver.from_conn_string(str(state_path)) as saver:
                with patch("miclaw.core.agent.graph.get_provider", return_value=Model()):
                    graph = create_agent_app(tools=[], checkpointer=saver)
                checkpoint_run_id = new_checkpoint_run_id()
                config = _config("thread-e2e", checkpoint_run_id)

                async def invoke():
                    return await graph.ainvoke(
                        {"messages": [HumanMessage(content="PHASE5_EXECUTION_SECRET_PAYLOAD")]},
                        config=config,
                        durability="sync",
                    )

                result = await run_agent_execution(
                    invoke,
                    execution_id="execution-e2e-success",
                    trace_context=trace,
                    execution_store=store,
                    checkpoint_thread_id="thread-e2e",
                    checkpoint_run_id=checkpoint_run_id,
                    checkpoint_id_provider=lambda: _checkpoint_id(graph, "thread-e2e", checkpoint_run_id),
                )
                assert result.state.status is ExecutionStatus.SUCCEEDED
                assert result.failure is result.retry_evaluation is result.next_attempt is None
                assert calls == 1
                record = store.get_attempt("execution-e2e-success", 1)
                assert record.state.status is ExecutionStatus.SUCCEEDED
                assert record.checkpoint_run_id == checkpoint_run_id
                assert record.checkpoint_id is not None
                assert config["callbacks"] == [] and config["recursion_limit"] == 25
        finally:
            store.close()
            logger.shutdown()

        reopened = ExecutionStore(execution_path)
        assert reopened.get_attempt("execution-e2e-success", 1).state.status is ExecutionStatus.SUCCEEDED
        reopened.close()
        events = [event for event in _events(log_path) if event["event"].startswith("execution_")]
        assert [event["event"] for event in events] == ["execution_started", "execution_finished"]
        assert events[0] == {**events[0], "event": "execution_started", "attempt": 1, "status": "running"}
        assert events[1]["status"] == "succeeded"
        assert events[0]["run_id"] == events[1]["run_id"] == "trace-explicit"
        assert [event["step_id"] for event in events] == [1, 2]
        from entry.monitor import get_trace_events

        assert [event["event"] for event in get_trace_events(events, "trace-explicit")] == [
            "execution_started",
            "execution_finished",
        ]
        assert "PHASE5_EXECUTION_SECRET_PAYLOAD" not in log_path.read_text(encoding="utf-8")
        assert b"PHASE5_EXECUTION_SECRET_PAYLOAD" not in execution_path.read_bytes()
    asyncio.run(scenario())


def test_timeout_plans_one_durable_next_attempt_without_graph_replay(tmp_path, monkeypatch):
    """真实 provider timeout 只持久化 FAILED + N+1 PENDING，不自动第二次调用 graph。"""
    async def scenario():
        state_path = tmp_path / "state.sqlite3"
        store = ExecutionStore(tmp_path / "execution.sqlite3")
        logger = JSONLEventLogger(log_file=tmp_path / "events.jsonl")
        monkeypatch.setattr(execution_module, "audit_logger", logger)
        calls = 0

        class TimeoutModel:
            def bind_tools(self, _tools): return self
            def invoke(self, _messages):
                nonlocal calls
                calls += 1
                raise TimeoutError("PHASE5_TIMEOUT_SECRET")

        async with AsyncSqliteSaver.from_conn_string(str(state_path)) as saver:
            with patch("miclaw.core.agent.graph.get_provider", return_value=TimeoutModel()):
                graph = create_agent_app(tools=[], checkpointer=saver)
            checkpoint_run_id = new_checkpoint_run_id()
            config = _config("thread-timeout", checkpoint_run_id)

            async def invoke():
                return await graph.ainvoke({"messages": [HumanMessage(content="timeout")]}, config=config, durability="sync")

            result = await run_agent_execution(
                invoke,
                execution_id="execution-timeout",
                trace_context=TraceContext(run_id="trace-timeout"),
                execution_store=store,
                checkpoint_thread_id="thread-timeout",
                checkpoint_run_id=checkpoint_run_id,
                checkpoint_id_provider=lambda: _checkpoint_id(graph, "thread-timeout", checkpoint_run_id),
                retry_policy=RetryPolicy(3),
            )
            assert calls == 1
            assert result.failure.code is ExecutionFailureCode.PROVIDER_TIMEOUT
            assert result.retry_evaluation.decision is RetryDecision.RETRY
            assert store.get_attempt("execution-timeout", 1).state.status is ExecutionStatus.FAILED
            assert store.get_attempt("execution-timeout", 2).state.status is ExecutionStatus.PENDING
        store.close()
        logger.shutdown()
        event_text = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
        assert '"failure_code": "provider_timeout"' in event_text
        assert '"retry_decision": "retry"' in event_text
        assert "PHASE5_TIMEOUT_SECRET" not in event_text
    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (RuntimeError("VERY_SECRET_PROVIDER_DETAIL"), ExecutionFailureCode.PROVIDER_ERROR),
        (RuntimeError("VERY_SECRET_RUNTIME_DETAIL"), ExecutionFailureCode.RUNTIME_ERROR),
    ],
)
def test_non_retryable_failure_is_durable_without_raw_detail(tmp_path, error, expected_code):
    """provider 与 runtime 非 retryable failure 都不生成 next attempt 或持久化原始异常。"""
    async def scenario():
        store_path = tmp_path / f"{expected_code.value}.sqlite3"
        store = ExecutionStore(store_path)
        calls = 0

        async def invoke():
            nonlocal calls
            calls += 1
            if expected_code is ExecutionFailureCode.PROVIDER_ERROR:
                raise execution_module.AgentProviderFailure(
                    execution_module.provider_failure(ExecutionFailureCode.PROVIDER_ERROR)
                )
            raise error

        result = await run_agent_execution(
            invoke,
            execution_id=f"execution-{expected_code.value}",
            trace_context=TraceContext(run_id="trace-failure"),
            execution_store=store,
            checkpoint_thread_id="thread-failure",
            checkpoint_run_id="checkpoint-failure",
            retry_policy=RetryPolicy(3),
        )
        assert calls == 1
        assert result.state.status is ExecutionStatus.FAILED
        assert result.failure.code is expected_code
        assert result.retry_evaluation.decision is RetryDecision.DO_NOT_RETRY
        assert result.next_attempt is None
        with pytest.raises(ExecutionStoreError, match="^execution_record_not_found$"):
            store.get_attempt(f"execution-{expected_code.value}", 2)
        store.close()
        assert b"VERY_SECRET" not in store_path.read_bytes()
    asyncio.run(scenario())


def test_targeted_safe_recovery_is_idempotent_and_emits_safe_metadata(tmp_path, monkeypatch):
    """safe checkpoint recovery 仅创建一次 N+1 attempt，JSONL 不含 checkpoint identity。"""
    async def scenario():
        state_path = tmp_path / "state.sqlite3"
        path = tmp_path / "execution.sqlite3"
        log_path = tmp_path / "events.jsonl"
        logger = JSONLEventLogger(log_file=log_path)
        monkeypatch.setattr(recovery_module, "audit_logger", logger)
        store = ExecutionStore(path)
        pending = create_pending_execution("execution-recovery-idempotent")
        running = start_execution(pending, run_id="trace", started_at=datetime.now(timezone.utc))
        checkpoint_run_id = new_checkpoint_run_id()
        store.create_attempt(ExecutionAttemptRecord(pending, checkpoint_thread_id="thread", checkpoint_run_id=checkpoint_run_id))
        store.transition(ExecutionAttemptRecord(running, checkpoint_thread_id="thread", checkpoint_run_id=checkpoint_run_id), expected_status=ExecutionStatus.PENDING)

        @tool
        def side_effect_tool(value: str) -> str:
            """构造 ToolNode 后的安全 agent checkpoint。"""
            return value

        class ToolThenTimeout:
            def __init__(self): self.calls = 0
            def bind_tools(self, _tools): return self
            def invoke(self, _messages):
                self.calls += 1
                if self.calls == 1:
                    return AIMessage(content="", tool_calls=[{"name": "side_effect_tool", "args": {"value": "once"}, "id": "tool-1"}])
                raise TimeoutError("RECOVERY_SECRET")

        async with AsyncSqliteSaver.from_conn_string(str(state_path)) as saver:
            with patch("miclaw.core.agent.graph.get_provider", return_value=ToolThenTimeout()):
                graph = create_agent_app(tools=[side_effect_tool], checkpointer=saver)
            with pytest.raises(execution_module.AgentProviderFailure):
                await graph.ainvoke(
                    {"messages": [HumanMessage(content="safe checkpoint")]},
                    config=_config("thread", checkpoint_run_id),
                    durability="sync",
                )
            first = await recover_execution(
                store,
                graph,
                "execution-recovery-idempotent",
                1,
                trace_context=TraceContext(run_id="trace-recovery-explicit"),
            )
            second = await recover_execution(
                store,
                graph,
                "execution-recovery-idempotent",
                1,
                trace_context=TraceContext(run_id="trace-recovery-explicit"),
            )
        assert first.assessment.decision.value == "resume_from_checkpoint"
        assert first.record.state.status is ExecutionStatus.INTERRUPTED
        assert first.resume_record is not None and first.resume_record.state.attempt == 2
        assert second.assessment.reason.value == "status_not_recoverable"
        assert second.record.state.status is ExecutionStatus.INTERRUPTED
        assert store.get_attempt("execution-recovery-idempotent", 2).state.status is ExecutionStatus.PENDING
        with pytest.raises(ExecutionStoreError, match="^execution_record_not_found$"):
            store.get_attempt("execution-recovery-idempotent", 3)
        store.close()
        logger.shutdown()
        text = log_path.read_text(encoding="utf-8")
        assert '"event": "execution_recovery"' in text
        assert "RECOVERY_SECRET" not in text and checkpoint_run_id not in text and '"thread_id": "thread"' not in text
        events = [event for event in _events(log_path) if event["event"] == "execution_recovery"]
        assert [event["run_id"] for event in events] == ["trace-recovery-explicit", "trace-recovery-explicit"]
    asyncio.run(scenario())


def test_execution_events_prefer_explicit_trace_over_ambient_context(tmp_path, monkeypatch):
    """显式 execution TraceContext 必须覆盖无关 ambient ContextVar。"""
    async def scenario():
        log_path = tmp_path / "events.jsonl"
        logger = JSONLEventLogger(log_file=log_path)
        monkeypatch.setattr(execution_module, "audit_logger", logger)
        token = set_current_trace_context(TraceContext(run_id="ambient-wrong"))
        try:
            async def invoke():
                return "done"

            result = await run_agent_execution(
                invoke,
                execution_id="execution-explicit-trace",
                trace_context=TraceContext(run_id="trace-explicit"),
            )
            assert result.state.status is ExecutionStatus.SUCCEEDED
        finally:
            reset_trace_context(token)
            logger.shutdown()
        events = [event for event in _events(log_path) if event["event"].startswith("execution_")]
        assert [event["run_id"] for event in events] == ["trace-explicit", "trace-explicit"]
        assert all(event["run_id"] != "ambient-wrong" for event in events)
    asyncio.run(scenario())


def test_real_tool_loop_guard_persists_nonretryable_failure_and_safe_event(tmp_path, monkeypatch):
    """真实 ToolNode 第三次重复请求前被阻断，durable failure 与 JSONL 不暴露 Tool 参数。"""
    async def scenario():
        log_path = tmp_path / "events.jsonl"
        logger = JSONLEventLogger(log_file=log_path)
        monkeypatch.setattr(execution_module, "audit_logger", logger)
        store = ExecutionStore(tmp_path / "execution.sqlite3")
        model_calls = 0
        tool_calls = 0

        from langchain_core.tools import tool

        @tool
        def side_effect_tool(value: str) -> str:
            """记录实际 ToolNode 副作用。"""
            nonlocal tool_calls
            tool_calls += 1
            return value

        class RepeatingModel:
            def bind_tools(self, _tools): return self
            def invoke(self, _messages):
                nonlocal model_calls
                model_calls += 1
                return AIMessage(
                    content="",
                    tool_calls=[{"name": "side_effect_tool", "args": {"value": "PHASE5_TOOL_SECRET"}, "id": f"id-{model_calls}"}],
                )

        with patch("miclaw.core.agent.graph.get_provider", return_value=RepeatingModel()):
            graph = create_agent_app(tools=[side_effect_tool])

        async def invoke():
            return await graph.ainvoke({"messages": [HumanMessage(content="repeat")]})

        result = await run_agent_execution(
            invoke,
            execution_id="execution-loop",
            trace_context=TraceContext(run_id="trace-loop"),
            execution_store=store,
            checkpoint_thread_id="thread-loop",
            checkpoint_run_id="checkpoint-loop",
        )
        assert model_calls == 3 and tool_calls == 2
        assert result.failure.code is ExecutionFailureCode.LOOP_GUARD_TRIGGERED
        assert result.retry_evaluation.decision is RetryDecision.DO_NOT_RETRY
        assert result.next_attempt is None
        assert store.get_attempt("execution-loop", 1).state.status is ExecutionStatus.FAILED
        store.close()
        logger.shutdown()
        event_text = log_path.read_text(encoding="utf-8")
        assert '"failure_code": "loop_guard_triggered"' in event_text
        assert "PHASE5_TOOL_SECRET" not in event_text
    asyncio.run(scenario())


@pytest.mark.parametrize("execution_id", ["", False, 0, "   "])
def test_malformed_execution_id_fails_before_graph_or_store_mutation(tmp_path, execution_id):
    """显式 malformed ID 不会被 UUID 替换，也不会写入 durable record。"""
    async def scenario():
        store_path = tmp_path / "execution.sqlite3"
        store = ExecutionStore(store_path)
        calls = 0

        async def invoke():
            nonlocal calls
            calls += 1
            return "unexpected"

        from miclaw.core.execution.models import ExecutionStateValidationError
        with pytest.raises(ExecutionStateValidationError, match="^invalid_execution_id$"):
            await run_agent_execution(
                invoke,
                execution_id=execution_id,
                trace_context=TraceContext(run_id="trace-invalid"),
                execution_store=store,
                checkpoint_thread_id="thread-invalid",
                checkpoint_run_id="checkpoint-invalid",
            )
        assert calls == 0
        with pytest.raises(ExecutionStoreError, match="^execution_record_not_found$"):
            store.get_attempt("execution-invalid", 1)
        store.close()
    asyncio.run(scenario())


def test_real_provider_error_is_nonretryable_and_never_leaks_to_execution_event(tmp_path, monkeypatch):
    """真实 Agent provider RuntimeError 仅留下 provider_error enum，绝不写原始 detail。"""
    async def scenario():
        state_path = tmp_path / "state.sqlite3"
        execution_path = tmp_path / "execution.sqlite3"
        log_path = tmp_path / "events.jsonl"
        logger = JSONLEventLogger(log_file=log_path)
        monkeypatch.setattr(execution_module, "audit_logger", logger)
        store = ExecutionStore(execution_path)

        class ErrorModel:
            def bind_tools(self, _tools): return self
            def invoke(self, _messages): raise RuntimeError("VERY_SECRET_PROVIDER_DETAIL")

        async with AsyncSqliteSaver.from_conn_string(str(state_path)) as saver:
            with patch("miclaw.core.agent.graph.get_provider", return_value=ErrorModel()):
                graph = create_agent_app(tools=[], checkpointer=saver)
            checkpoint_run_id = new_checkpoint_run_id()
            config = _config("thread-provider-error", checkpoint_run_id)

            async def invoke():
                return await graph.ainvoke({"messages": [HumanMessage(content="safe")]}, config=config, durability="sync")

            result = await run_agent_execution(
                invoke,
                execution_id="execution-provider-error",
                trace_context=TraceContext(run_id="trace-provider-error"),
                execution_store=store,
                checkpoint_thread_id="thread-provider-error",
                checkpoint_run_id=checkpoint_run_id,
                checkpoint_id_provider=lambda: _checkpoint_id(graph, "thread-provider-error", checkpoint_run_id),
            )
            assert result.failure.code is ExecutionFailureCode.PROVIDER_ERROR
            assert result.retry_evaluation.decision is RetryDecision.DO_NOT_RETRY
            assert result.next_attempt is None
            assert store.get_attempt("execution-provider-error", 1).failure.code is ExecutionFailureCode.PROVIDER_ERROR
        store.close()
        logger.shutdown()
        assert "VERY_SECRET_PROVIDER_DETAIL" not in log_path.read_text(encoding="utf-8")
        assert b"VERY_SECRET_PROVIDER_DETAIL" not in execution_path.read_bytes()
    asyncio.run(scenario())


def test_reopening_execution_store_does_not_perform_global_stale_run_sweep(tmp_path):
    """普通 runtime/store 启动仅打开数据库，不能隐式中断其他 RUNNING attempt。"""
    path = tmp_path / "execution.sqlite3"
    store = ExecutionStore(path)
    pending = create_pending_execution("execution-still-running")
    running = start_execution(pending, run_id="trace-running", started_at=datetime.now(timezone.utc))
    store.create_attempt(ExecutionAttemptRecord(pending))
    store.transition(ExecutionAttemptRecord(running), expected_status=ExecutionStatus.PENDING)
    store.close()

    reopened = ExecutionStore(path)
    assert reopened.get_attempt("execution-still-running", 1).state.status is ExecutionStatus.RUNNING
    reopened.close()
