"""验证显式 PENDING dispatcher 的 fail-closed 与单消费者边界。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from miclaw.core.agent.execution import AgentExecutionRuntimeError, AgentProviderFailure
from miclaw.core.agent.recovery import resume_pending_execution
from miclaw.core.execution.failures import ExecutionFailure, ExecutionFailureCode, ExecutionFailureSource
from miclaw.core.execution.models import ExecutionStatus
from miclaw.core.execution.recovery import ExecutionAttemptRecord, RecoveryDecision, RecoveryReason
from miclaw.core.execution.retry import RetryDecision, RetryDecisionReason, RetryEvaluation, RetryPolicy
from miclaw.core.execution.state import create_pending_execution, mark_execution_failed, start_execution
from miclaw.core.observability.trace import TraceContext
from miclaw.core.runtime.execution_store import ExecutionStore


class _Snapshot:
    """最小 LangGraph snapshot 同形对象；只承载 recovery adapter 允许读取的 metadata/config/next。"""

    def __init__(self, run_id: str, next_nodes: tuple[str, ...], checkpoint_id: str = "checkpoint-1") -> None:
        self.metadata = {"run_id": run_id}
        self.config = {"configurable": {"thread_id": "resume-thread", "checkpoint_id": checkpoint_id}}
        self.next = next_nodes


class _CheckpointGraph:
    """用于 dispatcher 控制流测试的最小 checkpoint graph，避免引入 provider/network。"""

    def __init__(self, run_id: str, next_nodes: tuple[str, ...], checkpoint_id: str = "checkpoint-1") -> None:
        self.snapshot = _Snapshot(run_id, next_nodes, checkpoint_id)
        self.invocations = 0

    async def aget_state_history(self, _config):
        yield self.snapshot

    async def ainvoke(self, _input, *, config, durability):
        self.invocations += 1
        assert durability == "sync"
        return {"config": config}


def _plan_retry(store: ExecutionStore, execution_id: str, *, attempt: int = 1, checkpoint_id: str = "checkpoint-1") -> None:
    """构造已分类 timeout 失败并原子创建后续 PENDING 的真实 Store 记录。"""
    pending = create_pending_execution(execution_id, attempt=attempt)
    running = start_execution(pending, run_id=f"trace-{attempt}", started_at=datetime.now(timezone.utc))
    store.create_attempt(
        ExecutionAttemptRecord(
            pending,
            checkpoint_thread_id="resume-thread",
            checkpoint_run_id="predecessor-run",
            checkpoint_id=checkpoint_id,
        )
    )
    store.transition(
        ExecutionAttemptRecord(
            running,
            checkpoint_thread_id="resume-thread",
            checkpoint_run_id="predecessor-run",
            checkpoint_id=checkpoint_id,
        ),
        expected_status=ExecutionStatus.PENDING,
    )
    failed = ExecutionAttemptRecord(
        mark_execution_failed(running, finished_at=datetime.now(timezone.utc)),
        ExecutionFailure(ExecutionFailureSource.PROVIDER, ExecutionFailureCode.PROVIDER_TIMEOUT),
        RetryEvaluation(RetryDecision.RETRY, RetryDecisionReason.RETRYABLE_FAILURE),
        "resume-thread",
        "predecessor-run",
        checkpoint_id,
    )
    store.fail_and_plan_next(failed, ExecutionAttemptRecord(create_pending_execution(execution_id, attempt=attempt + 1)))


def test_resume_refuses_unsafe_or_missing_checkpoint_without_mutating_pending(tmp_path):
    """不安全或缺失 predecessor checkpoint 时 target 仍 PENDING，且绝不调用 graph。"""
    async def scenario():
        for name, graph, reason in (
            ("unsafe", _CheckpointGraph("predecessor-run", ("tools",)), RecoveryReason.UNSAFE_NEXT_NODE),
            ("missing", _CheckpointGraph("other-run", ("agent",)), RecoveryReason.NO_MATCHING_CHECKPOINT),
            ("mismatch", _CheckpointGraph("predecessor-run", ("agent",), checkpoint_id="other-checkpoint"), RecoveryReason.CHECKPOINT_OWNERSHIP_MISMATCH),
        ):
            store = ExecutionStore(tmp_path / f"{name}.sqlite3")
            _plan_retry(store, f"execution-{name}")
            result = await resume_pending_execution(
                store,
                graph,
                f"execution-{name}",
                2,
                config={"configurable": {"thread_id": "resume-thread"}},
                trace_context=TraceContext(run_id=f"{name}-trace"),
            )
            assert result.execution is None
            assert result.assessment.decision is RecoveryDecision.DO_NOT_RESUME
            assert result.assessment.reason is reason
            assert graph.invocations == 0
            assert store.get_attempt(f"execution-{name}", 2).state.status is ExecutionStatus.PENDING
            store.close()

    asyncio.run(scenario())


def test_resume_pending_cas_allows_one_graph_continuation(tmp_path):
    """两个 dispatcher 并发消费同一 PENDING 时，只有一个 CAS 成功并进入 graph。"""
    async def scenario():
        store = ExecutionStore(tmp_path / "execution.sqlite3")
        _plan_retry(store, "execution-race")
        graph = _CheckpointGraph("predecessor-run", ("agent",))
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_ainvoke(_input, *, config, durability):
            graph.invocations += 1
            assert durability == "sync"
            entered.set()
            await release.wait()
            return {"config": config}

        graph.ainvoke = slow_ainvoke
        first = asyncio.create_task(
            resume_pending_execution(
                store,
                graph,
                "execution-race",
                2,
                config={"configurable": {"thread_id": "resume-thread"}},
                trace_context=TraceContext(run_id="race-one"),
            )
        )
        await entered.wait()
        second = asyncio.create_task(
            resume_pending_execution(
                store,
                graph,
                "execution-race",
                2,
                config={"configurable": {"thread_id": "resume-thread"}},
                trace_context=TraceContext(run_id="race-two"),
            )
        )
        await asyncio.sleep(0)
        release.set()
        first_result, second_result = await asyncio.gather(first, second, return_exceptions=True)

        assert graph.invocations == 1
        assert sum(not isinstance(result, Exception) for result in (first_result, second_result)) == 1
        assert any(
            (isinstance(result, AgentExecutionRuntimeError) and str(result) == "execution_record_conflict")
            or (isinstance(result, Exception) and str(result) == "execution_attempt_not_pending")
            for result in (first_result, second_result)
        )
        assert store.get_attempt("execution-race", 2).state.status is ExecutionStatus.SUCCEEDED
        store.close()

    asyncio.run(scenario())


def test_resumed_timeout_plans_next_attempt_and_respects_total_attempt_limit(tmp_path):
    """dispatcher 只消费当前 PENDING；新的 timeout 仍由既有 RetryPolicy 计划下一 attempt。"""
    async def scenario():
        store = ExecutionStore(tmp_path / "execution.sqlite3")
        _plan_retry(store, "execution-timeout")

        class TimeoutGraph(_CheckpointGraph):
            async def ainvoke(self, _input, *, config, durability):
                self.invocations += 1
                self.snapshot = _Snapshot(config["run_id"], ("agent",), f"checkpoint-{self.invocations + 1}")
                raise AgentProviderFailure(
                    ExecutionFailure(ExecutionFailureSource.PROVIDER, ExecutionFailureCode.PROVIDER_TIMEOUT)
                )

        graph = TimeoutGraph("predecessor-run", ("agent",))
        result = await resume_pending_execution(
            store,
            graph,
            "execution-timeout",
            2,
            config={"configurable": {"thread_id": "resume-thread"}},
            trace_context=TraceContext(run_id="timeout-two"),
            retry_policy=RetryPolicy(max_attempts=3),
        )
        assert result.record.state.status is ExecutionStatus.FAILED
        assert result.execution is not None and result.execution.next_attempt is not None
        assert store.get_attempt("execution-timeout", 3).state.status is ExecutionStatus.PENDING
        exhausted = await resume_pending_execution(
            store,
            graph,
            "execution-timeout",
            3,
            config={"configurable": {"thread_id": "resume-thread"}},
            trace_context=TraceContext(run_id="timeout-three"),
            retry_policy=RetryPolicy(max_attempts=3),
        )
        assert exhausted.record.state.status is ExecutionStatus.FAILED
        assert exhausted.execution is not None and exhausted.execution.next_attempt is None
        try:
            store.get_attempt("execution-timeout", 4)
            raise AssertionError("max_attempts must not create attempt 4")
        except Exception as exc:
            assert str(exc) == "execution_record_not_found"
        store.close()

    asyncio.run(scenario())
