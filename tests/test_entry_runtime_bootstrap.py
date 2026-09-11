"""锁定默认 runtime bootstrap 的共享队列与无模型启动边界。"""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
import sys
from contextlib import nullcontext

import entry
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from miclaw.core.agent.request import AgentRequest, AgentRequestOrigin
import miclaw.core.agent.execution as execution_module
import miclaw.core.agent.graph as agent_graph
from miclaw.core.execution.models import ExecutionStatus, is_terminal
import miclaw.core.scheduler.heartbeat as heartbeat
import miclaw.core.memory.permissions as memory_permissions
from miclaw.core.memory.lifecycle import MemoryWriteIntent, get_memory_write_intent
from miclaw.core.memory.user_profile import UserProfileStore
from miclaw.core.security.permissions import (
    PermissionConfirmationChoice,
    get_session_permission_grants,
    reset_permission_confirmation_handler,
    reset_session_permission_grants,
    set_permission_confirmation_handler,
    set_session_permission_grants,
)
from miclaw.core.tools import builtins
import miclaw.core.tools.sandbox as sandbox_tools
from miclaw.core.tools.result import StructuredToolOutcome, extract_tool_outcome
from miclaw.core.observability.trace import TraceContext
from miclaw.core.runtime.workspace import reset_active_project_root, set_active_project_root


class _TrackingQueue(asyncio.Queue):
    """记录 input/worker 对同一 runtime queue 的收发。"""

    def __init__(self) -> None:
        super().__init__()
        self.put_items: list[object] = []
        self.get_items: list[object] = []
        self.get_cancelled = False

    async def put(self, item: object) -> None:
        self.put_items.append(item)
        await super().put(item)

    async def get(self) -> object:
        try:
            item = await super().get()
        except asyncio.CancelledError:
            self.get_cancelled = True
            raise
        self.get_items.append(item)
        return item


async def _wait_for_durable_status_counts(
    execution_db,
    expected: dict[ExecutionStatus, int],
    *,
    timeout: float = 1.0,
):
    """轮询 authoritative ExecutionStore，直到全部 logical execution 都处于预期 terminal 状态。"""
    from miclaw.core.runtime.execution_store import ExecutionStore, ExecutionStoreError

    deadline = asyncio.get_running_loop().time() + timeout
    observed: Counter[ExecutionStatus] = Counter()
    while asyncio.get_running_loop().time() < deadline:
        try:
            store = ExecutionStore(execution_db, readonly=True)
        except ExecutionStoreError:
            await asyncio.sleep(0.005)
            continue
        try:
            records = store.list_latest_attempts(limit=100)
        finally:
            store.close()
        observed = Counter(record.state.status for record in records)
        if observed == Counter(expected) and all(is_terminal(status) for status in observed):
            return records
        await asyncio.sleep(0.005)
    raise AssertionError(f"durable execution states did not converge: {dict(observed)}")


@pytest.fixture
def runtime_main():
    """按需加载 entry.main，并避免污染依赖 sys.modules 替换的 CLI tests。"""
    module = importlib.import_module("entry.main")
    try:
        yield module
    finally:
        if getattr(entry, "main", None) is module:
            delattr(entry, "main")
        sys.modules.pop("entry.main", None)


def test_async_main_reaches_input_boundary_with_one_shared_queue(runtime_main, monkeypatch, tmp_path):
    """bootstrap 不调用模型，并把 input、worker、heartbeat 连接到同一 queue。"""

    queue = _TrackingQueue()
    observed: dict[str, object] = {"model_invoked": False, "input_reached": False, "heartbeat_cancelled": False}

    class _AsyncioProxy:
        """只替换 entry 模块的 Queue factory，不影响 SQLite 依赖自身的 asyncio。"""

        Queue = staticmethod(lambda: queue)

        def __getattr__(self, name: str):
            return getattr(asyncio, name)

    class _FakeApp:
        """任何 graph invocation 都意味着 bootstrap test 越过了边界。"""

        async def astream(self, *_args, **_kwargs):
            observed["model_invoked"] = True
            raise AssertionError("startup regression must not invoke the graph")
            yield  # pragma: no cover

    class _FakePromptSession:
        """第一次 prompt 即验证 input boundary，然后走既有 /exit contract。"""

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def prompt_async(self, *_args, **_kwargs) -> str:
            observed["input_reached"] = True
            return "/exit"

    async def fake_pacemaker_loop(task_queue, check_interval: int = 10):
        """保持 production signature，并等待 shutdown cancellation。"""
        observed["heartbeat_queue"] = task_queue
        observed["heartbeat_interval"] = check_interval
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            observed["heartbeat_cancelled"] = True
            raise

    monkeypatch.setattr(runtime_main, "asyncio", _AsyncioProxy())
    monkeypatch.setattr(runtime_main, "print_banner", lambda: None)
    monkeypatch.setattr(runtime_main, "cprint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_main, "patch_stdout", lambda: nullcontext())
    monkeypatch.setattr(runtime_main, "PromptSession", _FakePromptSession)
    monkeypatch.setattr(runtime_main, "pacemaker_loop", fake_pacemaker_loop)
    monkeypatch.setattr(runtime_main, "create_agent_app", lambda **_kwargs: _FakeApp())
    monkeypatch.setattr(runtime_main, "DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(runtime_main, "EXECUTION_DB_PATH", str(tmp_path / "execution.sqlite3"))

    asyncio.run(runtime_main.async_main(trace_context=TraceContext(run_id="bootstrap-run")))

    assert observed["input_reached"] is True
    assert observed["heartbeat_queue"] is queue
    assert observed["heartbeat_interval"] == 10
    assert queue.put_items == [AgentRequest(content="/exit", origin=AgentRequestOrigin.INTERACTIVE)]
    assert queue.get_items == [AgentRequest(content="/exit", origin=AgentRequestOrigin.INTERACTIVE)]
    assert observed["heartbeat_cancelled"] is True
    assert observed["model_invoked"] is False


def test_async_main_surfaces_heartbeat_failure_without_raw_detail(runtime_main, monkeypatch, tmp_path):
    """heartbeat 崩溃会终止 runtime，而非在 shutdown 的 gather 中静默消失。"""

    queue = _TrackingQueue()
    observed = {"input_cancelled": False, "model_invoked": False}

    class _AsyncioProxy:
        Queue = staticmethod(lambda: queue)

        def __getattr__(self, name: str):
            return getattr(asyncio, name)

    class _FakeApp:
        async def astream(self, *_args, **_kwargs):
            observed["model_invoked"] = True
            raise AssertionError("heartbeat failure must stop before graph invocation")
            yield  # pragma: no cover

    class _FakePromptSession:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def prompt_async(self, *_args, **_kwargs) -> str:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                observed["input_cancelled"] = True
                raise

    async def fake_pacemaker_loop(task_queue, check_interval: int = 10):
        assert task_queue is queue
        raise RuntimeError("SENSITIVE_HEARTBEAT_DETAIL")

    monkeypatch.setattr(runtime_main, "asyncio", _AsyncioProxy())
    monkeypatch.setattr(runtime_main, "print_banner", lambda: None)
    monkeypatch.setattr(runtime_main, "cprint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_main, "patch_stdout", lambda: nullcontext())
    monkeypatch.setattr(runtime_main, "PromptSession", _FakePromptSession)
    monkeypatch.setattr(runtime_main, "pacemaker_loop", fake_pacemaker_loop)
    monkeypatch.setattr(runtime_main, "create_agent_app", lambda **_kwargs: _FakeApp())
    monkeypatch.setattr(runtime_main, "DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(runtime_main, "EXECUTION_DB_PATH", str(tmp_path / "execution.sqlite3"))

    with pytest.raises(RuntimeError, match="scheduler_heartbeat_failed") as error:
        asyncio.run(runtime_main.async_main(trace_context=TraceContext(run_id="heartbeat-failure-run")))

    assert "SENSITIVE_HEARTBEAT_DETAIL" not in str(error.value)
    assert observed["input_cancelled"] is True
    assert queue.get_cancelled is True
    assert observed["model_invoked"] is False


def test_async_main_surfaces_agent_worker_failure_without_raw_detail(runtime_main, monkeypatch, tmp_path):
    """agent worker 的未捕获异常也不能被 shutdown gather 静默吞掉。"""

    observed = {"input_cancelled": False}
    responses = iter(("trigger worker failure",))

    class _FakeApp:
        pass

    class _FakePromptSession:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def prompt_async(self, *_args, **_kwargs) -> str:
            try:
                return next(responses)
            except StopIteration:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    observed["input_cancelled"] = True
                    raise

    async def fake_pacemaker_loop(_task_queue, check_interval: int = 10):
        await asyncio.Event().wait()

    async def fake_run_agent_execution(*_args, **_kwargs):
        raise RuntimeError("SENSITIVE_WORKER_DETAIL")

    monkeypatch.setattr(runtime_main, "print_banner", lambda: None)
    monkeypatch.setattr(runtime_main, "cprint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_main, "patch_stdout", lambda: nullcontext())
    monkeypatch.setattr(runtime_main, "PromptSession", _FakePromptSession)
    monkeypatch.setattr(runtime_main, "pacemaker_loop", fake_pacemaker_loop)
    monkeypatch.setattr(runtime_main, "create_agent_app", lambda **_kwargs: _FakeApp())
    monkeypatch.setattr(runtime_main, "run_agent_execution", fake_run_agent_execution)
    monkeypatch.setattr(runtime_main, "DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(runtime_main, "EXECUTION_DB_PATH", str(tmp_path / "execution.sqlite3"))

    with pytest.raises(RuntimeError, match="agent_worker_failed") as error:
        asyncio.run(runtime_main.async_main(trace_context=TraceContext(run_id="worker-failure-run")))

    assert "SENSITIVE_WORKER_DETAIL" not in str(error.value)
    assert observed["input_cancelled"] is True


def test_async_main_processes_one_message_into_execution_store(runtime_main, monkeypatch, tmp_path):
    """entry worker 通过既有 execution wrapper 持久化一条非网络 fake graph result。"""

    observed: dict[str, object] = {"graph_calls": 0, "heartbeat_cancelled": False}
    responses = iter(("bootstrap message", "/exit"))

    class _FakeApp:
        async def astream(self, _inputs, *, config, stream_mode, durability):
            observed["graph_calls"] = int(observed["graph_calls"]) + 1
            observed["config"] = config
            observed["stream_mode"] = stream_mode
            observed["durability"] = durability
            yield {"agent": {"messages": [AIMessage(content="bootstrap complete")]}}

        async def aget_state_history(self, _config):
            if False:  # pragma: no cover - 保持 async generator 合约。
                yield None

    class _FakePromptSession:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def prompt_async(self, *_args, **_kwargs) -> str:
            return next(responses)

    async def fake_pacemaker_loop(task_queue, check_interval: int = 10):
        observed["heartbeat_queue"] = task_queue
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            observed["heartbeat_cancelled"] = True
            raise

    state_db = tmp_path / "state.sqlite3"
    execution_db = tmp_path / "execution.sqlite3"
    monkeypatch.setattr(runtime_main, "print_banner", lambda: None)
    monkeypatch.setattr(runtime_main, "cprint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_main, "patch_stdout", lambda: nullcontext())
    monkeypatch.setattr(runtime_main, "PromptSession", _FakePromptSession)
    monkeypatch.setattr(runtime_main, "pacemaker_loop", fake_pacemaker_loop)
    monkeypatch.setattr(runtime_main, "create_agent_app", lambda **_kwargs: _FakeApp())
    monkeypatch.setattr(runtime_main, "DB_PATH", str(state_db))
    monkeypatch.setattr(runtime_main, "EXECUTION_DB_PATH", str(execution_db))

    asyncio.run(runtime_main.async_main(trace_context=TraceContext(run_id="execution-bootstrap-run")))

    assert observed["graph_calls"] == 1
    assert observed["stream_mode"] == "updates"
    assert observed["durability"] == "sync"
    assert observed["config"]["recursion_limit"] == 25
    assert observed["heartbeat_cancelled"] is True
    with sqlite3.connect(execution_db) as connection:
        rows = connection.execute("SELECT status FROM execution_attempts").fetchall()
    assert rows == [("succeeded",)]


def test_interactive_cancel_persists_active_execution_and_worker_handles_next_turn(runtime_main, monkeypatch, tmp_path):
    """/cancel 经真实 input control boundary 取消 child task，持久化后 worker 仍可完成下一 turn。"""
    started = asyncio.Event()
    cancelled = asyncio.Event()
    second_done = asyncio.Event()
    rendered: list[str] = []

    class _FakeApp:
        async def astream(self, inputs, *, config, stream_mode, durability):
            content = inputs["messages"][0].content
            assert stream_mode == "updates" and durability == "sync"
            if content == "long running":
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            else:
                second_done.set()
                yield {"agent": {"messages": [AIMessage(content="second complete")]}}

        async def aget_state_history(self, _config):
            if False:  # pragma: no cover
                yield None

    class _FakePromptSession:
        def __init__(self, *_args, **_kwargs) -> None:
            self.step = 0

        async def prompt_async(self, *_args, **_kwargs) -> str:
            self.step += 1
            if self.step == 1:
                return "long running"
            if self.step == 2:
                await started.wait()
                return "/cancel"
            if self.step == 3:
                return "/cancel"
            if self.step == 4:
                await cancelled.wait()
                return "second turn"
            await second_done.wait()
            return "/exit"

    async def fake_pacemaker_loop(_task_queue, check_interval: int = 10):
        await asyncio.Event().wait()

    execution_db = tmp_path / "execution.sqlite3"
    monkeypatch.setattr(runtime_main, "print_banner", lambda: None)
    monkeypatch.setattr(runtime_main, "cprint", lambda *args, **_kwargs: rendered.append(str(args[0]) if args else ""))
    monkeypatch.setattr(runtime_main, "patch_stdout", lambda: nullcontext())
    monkeypatch.setattr(runtime_main, "PromptSession", _FakePromptSession)
    monkeypatch.setattr(runtime_main, "pacemaker_loop", fake_pacemaker_loop)
    monkeypatch.setattr(runtime_main, "create_agent_app", lambda **_kwargs: _FakeApp())
    monkeypatch.setattr(runtime_main, "DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(runtime_main, "EXECUTION_DB_PATH", str(execution_db))

    asyncio.run(runtime_main.async_main(trace_context=TraceContext(run_id="interactive-cancel-run")))

    with sqlite3.connect(execution_db) as connection:
        rows = connection.execute("SELECT status FROM execution_attempts ORDER BY created_at").fetchall()
    assert rows == [("cancelled",), ("succeeded",)]
    assert "Cancellation requested." in rendered
    assert "Cancellation already requested." in rendered
    assert any("Execution cancelled." in line for line in rendered)


def test_interactive_cancel_when_idle_does_not_mutate_execution_store(runtime_main, monkeypatch, tmp_path):
    """idle /cancel 只给稳定反馈，不投递模型请求或改变 Store。"""
    rendered: list[str] = []
    prompts = iter(("/cancel", "/exit"))

    class _FakeApp:
        async def astream(self, *_args, **_kwargs):
            raise AssertionError("idle cancel must not invoke graph")
            yield  # pragma: no cover

    class _FakePromptSession:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def prompt_async(self, *_args, **_kwargs) -> str:
            return next(prompts)

    async def fake_pacemaker_loop(_task_queue, check_interval: int = 10):
        await asyncio.Event().wait()

    execution_db = tmp_path / "execution.sqlite3"
    monkeypatch.setattr(runtime_main, "print_banner", lambda: None)
    monkeypatch.setattr(runtime_main, "cprint", lambda *args, **_kwargs: rendered.append(str(args[0]) if args else ""))
    monkeypatch.setattr(runtime_main, "patch_stdout", lambda: nullcontext())
    monkeypatch.setattr(runtime_main, "PromptSession", _FakePromptSession)
    monkeypatch.setattr(runtime_main, "pacemaker_loop", fake_pacemaker_loop)
    monkeypatch.setattr(runtime_main, "create_agent_app", lambda **_kwargs: _FakeApp())
    monkeypatch.setattr(runtime_main, "DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(runtime_main, "EXECUTION_DB_PATH", str(execution_db))

    asyncio.run(runtime_main.async_main(trace_context=TraceContext(run_id="idle-cancel-run")))

    assert rendered[0] == "No active execution."
    with sqlite3.connect(execution_db) as connection:
        assert connection.execute("SELECT status FROM execution_attempts").fetchall() == []


def test_cancel_text_is_not_privileged_outside_interactive_input_boundary(runtime_main):
    """scheduler/MCP 等 producer 的普通 AgentRequest content 不能伪造 host /cancel control。"""
    assert runtime_main._parse_user_request("/cancel") == AgentRequest(content="/cancel", origin=AgentRequestOrigin.INTERACTIVE)


def test_exit_cancels_active_execution_before_worker_shutdown(runtime_main, monkeypatch, tmp_path):
    """/exit 在 active execution 中先取消 child task，避免 shutdown 留下 RUNNING attempt。"""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class _FakeApp:
        async def astream(self, _inputs, *, config, stream_mode, durability):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            yield  # pragma: no cover

        async def aget_state_history(self, _config):
            if False:  # pragma: no cover
                yield None

    class _FakePromptSession:
        def __init__(self, *_args, **_kwargs) -> None:
            self.step = 0

        async def prompt_async(self, *_args, **_kwargs) -> str:
            self.step += 1
            if self.step == 1:
                return "long running"
            await started.wait()
            return "/exit"

    async def fake_pacemaker_loop(_task_queue, check_interval: int = 10):
        await asyncio.Event().wait()

    execution_db = tmp_path / "execution.sqlite3"
    monkeypatch.setattr(runtime_main, "print_banner", lambda: None)
    monkeypatch.setattr(runtime_main, "cprint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_main, "patch_stdout", lambda: nullcontext())
    monkeypatch.setattr(runtime_main, "PromptSession", _FakePromptSession)
    monkeypatch.setattr(runtime_main, "pacemaker_loop", fake_pacemaker_loop)
    monkeypatch.setattr(runtime_main, "create_agent_app", lambda **_kwargs: _FakeApp())
    monkeypatch.setattr(runtime_main, "DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(runtime_main, "EXECUTION_DB_PATH", str(execution_db))

    asyncio.run(runtime_main.async_main(trace_context=TraceContext(run_id="exit-cancel-run")))

    assert cancelled.is_set()
    with sqlite3.connect(execution_db) as connection:
        assert connection.execute("SELECT status FROM execution_attempts").fetchall() == [("cancelled",)]


@pytest.mark.parametrize("explicit_user_cancel", (False, True), ids=("runtime_shutdown", "user_cancel_then_shutdown"))
def test_active_execution_shutdown_does_not_swallow_worker_cancellation(
    runtime_main,
    monkeypatch,
    tmp_path,
    explicit_user_cancel,
):
    """worker 自身被取消时优先退出，即使 child 已有 /cancel 标记。"""
    started = asyncio.Event()
    child_cancelled = asyncio.Event()
    rendered: list[str] = []

    class _FakeApp:
        async def astream(self, _inputs, *, config, stream_mode, durability):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                child_cancelled.set()
                if explicit_user_cancel:
                    # 等待 runtime shutdown 的第二次取消，锁定原先 worker 吞取消的竞态。
                    await asyncio.Event().wait()
                raise
            yield  # pragma: no cover

        async def aget_state_history(self, _config):
            if False:  # pragma: no cover
                yield None

    class _FakePromptSession:
        def __init__(self, *_args, **_kwargs) -> None:
            self.step = 0

        async def prompt_async(self, *_args, **_kwargs) -> str:
            self.step += 1
            if self.step == 1:
                return "long running"
            await started.wait()
            if explicit_user_cancel and self.step == 2:
                return "/cancel"
            await asyncio.Event().wait()
            raise AssertionError("unreachable")  # pragma: no cover

    async def fake_pacemaker_loop(_task_queue, check_interval: int = 10):
        await (child_cancelled if explicit_user_cancel else started).wait()
        raise RuntimeError("SENSITIVE_HEARTBEAT_DETAIL")

    execution_db = tmp_path / "execution.sqlite3"
    monkeypatch.setattr(runtime_main, "print_banner", lambda: None)
    monkeypatch.setattr(runtime_main, "cprint", lambda *args, **_kwargs: rendered.append(str(args[0]) if args else ""))
    monkeypatch.setattr(runtime_main, "patch_stdout", lambda: nullcontext())
    monkeypatch.setattr(runtime_main, "PromptSession", _FakePromptSession)
    monkeypatch.setattr(runtime_main, "pacemaker_loop", fake_pacemaker_loop)
    monkeypatch.setattr(runtime_main, "create_agent_app", lambda **_kwargs: _FakeApp())
    monkeypatch.setattr(runtime_main, "DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(runtime_main, "EXECUTION_DB_PATH", str(execution_db))

    async def run_runtime() -> None:
        with pytest.raises(RuntimeError, match="scheduler_heartbeat_failed") as error:
            await asyncio.wait_for(
                runtime_main.async_main(trace_context=TraceContext(run_id="active-shutdown-race-run")),
                timeout=1,
            )
        assert "SENSITIVE_HEARTBEAT_DETAIL" not in str(error.value)

    asyncio.run(run_runtime())

    assert child_cancelled.is_set()
    with sqlite3.connect(execution_db) as connection:
        assert connection.execute("SELECT status FROM execution_attempts").fetchall() == [("cancelled",)]
    assert not any("agent_worker_failed" in line for line in rendered)


def test_eof_cancels_active_execution_before_worker_shutdown(runtime_main, monkeypatch, tmp_path):
    """EOF 与 /exit 共享受控 child cancellation，不留下 RUNNING attempt。"""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class _FakeApp:
        async def astream(self, _inputs, *, config, stream_mode, durability):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            yield  # pragma: no cover

        async def aget_state_history(self, _config):
            if False:  # pragma: no cover
                yield None

    class _FakePromptSession:
        def __init__(self, *_args, **_kwargs) -> None:
            self.step = 0

        async def prompt_async(self, *_args, **_kwargs) -> str:
            self.step += 1
            if self.step == 1:
                return "long running"
            await started.wait()
            raise EOFError

    async def fake_pacemaker_loop(_task_queue, check_interval: int = 10):
        await asyncio.Event().wait()

    execution_db = tmp_path / "execution.sqlite3"
    monkeypatch.setattr(runtime_main, "print_banner", lambda: None)
    monkeypatch.setattr(runtime_main, "cprint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_main, "patch_stdout", lambda: nullcontext())
    monkeypatch.setattr(runtime_main, "PromptSession", _FakePromptSession)
    monkeypatch.setattr(runtime_main, "pacemaker_loop", fake_pacemaker_loop)
    monkeypatch.setattr(runtime_main, "create_agent_app", lambda **_kwargs: _FakeApp())
    monkeypatch.setattr(runtime_main, "DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(runtime_main, "EXECUTION_DB_PATH", str(execution_db))

    asyncio.run(runtime_main.async_main(trace_context=TraceContext(run_id="eof-cancel-run")))

    assert cancelled.is_set()
    with sqlite3.connect(execution_db) as connection:
        assert connection.execute("SELECT status FROM execution_attempts").fetchall() == [("cancelled",)]


class _SequenceModel:
    """为真实 graph/ToolNode 提供可控的同步模型响应。"""

    def __init__(self, responses):
        self.inputs = []
        self._responses = list(responses)

    def invoke(self, messages):
        self.inputs.append(messages)
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _SequenceProvider:
    """复用 production graph 所需的最小 provider 表面。"""

    def __init__(self, model: _SequenceModel):
        self._model = model

    def bind_tools(self, _tools):
        return self._model


def _profile_tool_call(content: str, call_id: str) -> AIMessage:
    """构造真实 ToolNode 可消费的 save_user_profile call。"""
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "save_user_profile",
            "args": {"new_content": content},
            "id": call_id,
            "type": "tool_call",
        }],
    )


def _scheduler_create_tool_call(description: str, call_id: str) -> AIMessage:
    """构造真实 ToolNode 可消费的 scheduler create call。"""
    target_time = (datetime.now() + timedelta(days=1)).replace(microsecond=0)
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "schedule_task",
            "args": {
                "target_time": target_time.strftime("%Y-%m-%d %H:%M:%S"),
                "description": description,
                "repeat": None,
                "repeat_count": None,
            },
            "id": call_id,
            "type": "tool_call",
        }],
    )


def _run_memory_entry(
    runtime_main,
    monkeypatch,
    tmp_path,
    *,
    prompts: tuple[str, ...],
    responses: list[AIMessage | BaseException],
    permission_choice: PermissionConfirmationChoice = PermissionConfirmationChoice.ALLOW_ONCE,
    scheduled_requests: tuple[AgentRequest, ...] = (),
):
    """经真实 entry worker、graph、ToolNode 和 profile Store 执行受控 turn。"""

    memory_dir = tmp_path / "workspace" / "memory"
    model = _SequenceModel(responses)
    prompt_values = iter(prompts)
    rendered = []
    tool_error_types = []

    class _FakePromptSession:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def prompt_async(self, *_args, **_kwargs) -> str:
            return next(prompt_values)

    class _NoopLogger:
        def log_event(self, *_args, **_kwargs) -> None:
            pass

    async def fake_pacemaker_loop(task_queue, check_interval: int = 10):
        for request in scheduled_requests:
            await task_queue.put(request)
        await asyncio.Event().wait()

    state_db = tmp_path / "state.sqlite3"
    execution_db = tmp_path / "execution.sqlite3"
    monkeypatch.setattr(agent_graph, "get_provider", lambda **_kwargs: _SequenceProvider(model))
    monkeypatch.setattr(agent_graph, "MEMORY_DIR", str(memory_dir))
    monkeypatch.setattr(agent_graph, "audit_logger", _NoopLogger())
    monkeypatch.setattr(builtins, "MEMORY_DIR", str(memory_dir))
    monkeypatch.setattr(memory_permissions, "_permission_audit_logger", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(memory_permissions, "_permission_confirmation_audit_logger", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_main, "print_banner", lambda: None)
    monkeypatch.setattr(runtime_main, "cprint", lambda *args, **_kwargs: rendered.append(str(args[0]) if args else ""))
    monkeypatch.setattr(runtime_main, "patch_stdout", lambda: nullcontext())
    monkeypatch.setattr(runtime_main, "PromptSession", _FakePromptSession)
    monkeypatch.setattr(runtime_main, "pacemaker_loop", fake_pacemaker_loop)
    monkeypatch.setattr(runtime_main, "DB_PATH", str(state_db))
    monkeypatch.setattr(runtime_main, "EXECUTION_DB_PATH", str(execution_db))

    permission_token = set_permission_confirmation_handler(lambda _request, _result: permission_choice)
    try:
        asyncio.run(runtime_main.async_main(trace_context=TraceContext(run_id="memory-entry-run")))
    finally:
        reset_permission_confirmation_handler(permission_token)

    outcomes_by_call_id = {
        message.tool_call_id: extract_tool_outcome(message)
        for batch in model.inputs
        for message in batch
        if isinstance(message, ToolMessage)
    }
    tool_error_types.extend(
        outcome.error_type
        for outcome in outcomes_by_call_id.values()
        if outcome is not None and outcome.error_type is not None
    )

    return model, memory_dir, execution_db, rendered, tool_error_types


def _model_contents(model: _SequenceModel) -> str:
    """提取模型所见 message 内容，用于验证 ToolNode 返回的稳定结果。"""
    return "\n".join(str(message.content) for batch in model.inputs for message in batch)


def test_scheduler_requests_isolate_session_grants_and_restore_interactive_context(runtime_main, monkeypatch, tmp_path):
    """scheduler request 使用独立 grants，拒绝不写入，且不会破坏 interactive grant。"""
    first_confirmation = asyncio.Event()
    first_scheduled_confirmation = asyncio.Event()
    second_scheduled_confirmation = asyncio.Event()
    interactive_third_done = asyncio.Event()
    scheduled_failure_done = asyncio.Event()
    interactive_fourth_done = asyncio.Event()
    task_file = tmp_path / "tasks.json"
    task_file.write_text("[]", encoding="utf-8")
    state_db = tmp_path / "state.sqlite3"
    execution_db = tmp_path / "execution.sqlite3"
    confirmation_contexts: list[set] = []
    confirmation_grant_counts: list[int] = []
    confirmations = []

    class _GrantSequenceModel(_SequenceModel):
        """在既有受控模型上暴露 turn 间同步点。"""

        def invoke(self, messages):
            self.inputs.append(messages)
            response = self._responses.pop(0)
            call_number = len(self.inputs)
            if call_number == 6:
                interactive_third_done.set()
            elif call_number == 10:
                scheduled_failure_done.set()
            elif call_number == 12:
                interactive_fourth_done.set()
            if isinstance(response, BaseException):
                raise response
            return response

    responses = [
        _scheduler_create_tool_call("interactive-one", "interactive-one"),
        AIMessage(content="interactive one done"),
        _scheduler_create_tool_call("scheduled-denied", "scheduled-denied"),
        AIMessage(content="scheduled denied"),
        _scheduler_create_tool_call("interactive-three", "interactive-three"),
        AIMessage(content="interactive three done"),
        _scheduler_create_tool_call("scheduled-allowed", "scheduled-allowed"),
        AIMessage(content="scheduled allowed"),
        _scheduler_create_tool_call("scheduled-failure", "scheduled-failure"),
        RuntimeError("SENSITIVE_SCHEDULED_PROVIDER_DETAIL"),
        _scheduler_create_tool_call("interactive-four", "interactive-four"),
        AIMessage(content="interactive four done"),
    ]
    model = _GrantSequenceModel(responses)

    class _FakePromptSession:
        def __init__(self, *_args, **_kwargs) -> None:
            self.step = 0

        async def prompt_async(self, *_args, **_kwargs) -> str:
            self.step += 1
            if self.step == 1:
                return "interactive one"
            if self.step == 2:
                await first_scheduled_confirmation.wait()
                return "interactive three"
            if self.step == 3:
                await scheduled_failure_done.wait()
                return "interactive four"
            await interactive_fourth_done.wait()
            await _wait_for_durable_status_counts(
                execution_db,
                {ExecutionStatus.SUCCEEDED: 5, ExecutionStatus.FAILED: 1},
            )
            return "/exit"

    async def fake_pacemaker_loop(task_queue, check_interval: int = 10):
        await first_confirmation.wait()
        await task_queue.put(
            AgentRequest(content="scheduled one", origin=AgentRequestOrigin.SCHEDULER)
        )
        await interactive_third_done.wait()
        await task_queue.put(
            AgentRequest(content="scheduled two", origin=AgentRequestOrigin.SCHEDULER)
        )
        await second_scheduled_confirmation.wait()
        await task_queue.put(
            AgentRequest(content="scheduled three", origin=AgentRequestOrigin.SCHEDULER)
        )
        await asyncio.Event().wait()

    def confirm(request, _result):
        grants = get_session_permission_grants()
        assert grants is not None
        confirmations.append(request)
        confirmation_contexts.append(grants)
        confirmation_grant_counts.append(len(grants))
        index = len(confirmations)
        if index == 1:
            first_confirmation.set()
            return PermissionConfirmationChoice.ALLOW_SESSION
        if index == 2:
            first_scheduled_confirmation.set()
            return PermissionConfirmationChoice.DENY
        if index == 3:
            second_scheduled_confirmation.set()
            return PermissionConfirmationChoice.ALLOW_SESSION
        assert index == 4
        return PermissionConfirmationChoice.DENY

    class _NoopLogger:
        def log_event(self, *_args, **_kwargs) -> None:
            pass

    interactive_grants_token = set_session_permission_grants()
    interactive_grants = get_session_permission_grants()
    permission_token = set_permission_confirmation_handler(confirm)
    try:
        monkeypatch.setattr(agent_graph, "get_provider", lambda **_kwargs: _SequenceProvider(model))
        monkeypatch.setattr(agent_graph, "MEMORY_DIR", str(tmp_path / "memory"))
        monkeypatch.setattr(agent_graph, "audit_logger", _NoopLogger())
        monkeypatch.setattr(builtins, "TASKS_FILE", str(task_file))
        monkeypatch.setattr(builtins, "_permission_audit_logger", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(builtins, "_permission_confirmation_audit_logger", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(runtime_main, "print_banner", lambda: None)
        monkeypatch.setattr(runtime_main, "cprint", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(runtime_main, "patch_stdout", lambda: nullcontext())
        monkeypatch.setattr(runtime_main, "PromptSession", _FakePromptSession)
        monkeypatch.setattr(runtime_main, "pacemaker_loop", fake_pacemaker_loop)
        monkeypatch.setattr(runtime_main, "DB_PATH", str(state_db))
        monkeypatch.setattr(runtime_main, "EXECUTION_DB_PATH", str(execution_db))

        asyncio.run(runtime_main.async_main(trace_context=TraceContext(run_id="scheduler-grant-run")))
    finally:
        reset_permission_confirmation_handler(permission_token)
        reset_session_permission_grants(interactive_grants_token)

    tasks = json.loads(task_file.read_text(encoding="utf-8"))
    assert [task["description"] for task in tasks] == [
        "interactive-one",
        "interactive-three",
        "scheduled-allowed",
        "interactive-four",
    ]
    assert len(confirmations) == 4
    assert confirmation_contexts[0] is interactive_grants
    assert confirmation_grant_counts == [0, 0, 0, 0]
    assert len({id(grants) for grants in confirmation_contexts}) == 4
    assert len(interactive_grants) == 1
    with sqlite3.connect(execution_db) as connection:
        statuses = Counter(
            ExecutionStatus(status)
            for (status,) in connection.execute("SELECT status FROM execution_attempts")
        )
    assert statuses == Counter({ExecutionStatus.SUCCEEDED: 5, ExecutionStatus.FAILED: 1})


@pytest.mark.parametrize(
    ("permission", "expected_outcome", "expected_file"),
    [
        (PermissionConfirmationChoice.ALLOW_ONCE, StructuredToolOutcome(True, None), "allowed"),
        (PermissionConfirmationChoice.DENY, StructuredToolOutcome(False, "permission_denied"), None),
    ],
)
def test_project_write_runs_through_entry_agent_toolnode_and_permission(
    runtime_main,
    monkeypatch,
    tmp_path,
    permission,
    expected_outcome,
    expected_file,
):
    """PROJECT write 经真实 entry→worker→ToolNode；ALLOW/ DENY 都保留模型继续路径。"""
    project = tmp_path / "project"
    project.mkdir()
    state_db = tmp_path / "state.sqlite3"
    execution_db = tmp_path / "execution.sqlite3"
    model_finished = asyncio.Event()
    confirmations = []

    class _ProjectModel(_SequenceModel):
        """在最终模型调用时释放 input loop，随后等待 durable terminal state。"""

        def invoke(self, messages):
            response = super().invoke(messages)
            if len(self.inputs) == 2:
                model_finished.set()
            return response

    model = _ProjectModel([
        AIMessage(
            content="",
            tool_calls=[{
                "name": "write_office_file",
                "args": {"filepath": "result.txt", "content": "allowed", "mode": "w"},
                "id": "project-write",
                "type": "tool_call",
            }],
        ),
        AIMessage(content="project write complete"),
    ])

    class _PromptSession:
        def __init__(self, *_args, **_kwargs) -> None:
            self.step = 0

        async def prompt_async(self, *_args, **_kwargs) -> str:
            self.step += 1
            if self.step == 1:
                return "write project file"
            await model_finished.wait()
            await _wait_for_durable_status_counts(
                execution_db, {ExecutionStatus.SUCCEEDED: 1}
            )
            return "/exit"

    async def _idle_pacemaker(_queue, check_interval: int = 10) -> None:
        await asyncio.Event().wait()

    class _NoopLogger:
        def log_event(self, *_args, **_kwargs) -> None:
            pass

    project_token = set_active_project_root(project)
    grants_token = set_session_permission_grants()
    confirmation_token = set_permission_confirmation_handler(
        lambda request, _result: confirmations.append(request) or permission
    )
    try:
        monkeypatch.setattr(agent_graph, "get_provider", lambda **_kwargs: _SequenceProvider(model))
        monkeypatch.setattr(agent_graph, "MEMORY_DIR", str(tmp_path / "memory"))
        monkeypatch.setattr(agent_graph, "audit_logger", _NoopLogger())
        monkeypatch.setattr(execution_module, "audit_logger", _NoopLogger())
        monkeypatch.setattr(sandbox_tools, "_permission_audit_logger", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            sandbox_tools, "_permission_confirmation_audit_logger", lambda *_args, **_kwargs: None
        )
        monkeypatch.setattr(runtime_main, "print_banner", lambda: None)
        monkeypatch.setattr(runtime_main, "cprint", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(runtime_main, "patch_stdout", lambda: nullcontext())
        monkeypatch.setattr(runtime_main, "PromptSession", _PromptSession)
        monkeypatch.setattr(runtime_main, "pacemaker_loop", _idle_pacemaker)
        monkeypatch.setattr(runtime_main, "DB_PATH", str(state_db))
        monkeypatch.setattr(runtime_main, "EXECUTION_DB_PATH", str(execution_db))
        asyncio.run(runtime_main.async_main(trace_context=TraceContext(run_id="project-entry-toolnode")))
    finally:
        reset_permission_confirmation_handler(confirmation_token)
        reset_session_permission_grants(grants_token)
        reset_active_project_root(project_token)

    target = project / "result.txt"
    tool_message = next(
        message
        for message in model.inputs[1]
        if isinstance(message, ToolMessage) and message.tool_call_id == "project-write"
    )
    assert extract_tool_outcome(tool_message) == expected_outcome
    assert len(model.inputs) == 2
    assert len(confirmations) == 1
    assert confirmations[0].metadata["workspace_scope"] == "project"
    if expected_file is None:
        assert not target.exists()
    else:
        assert target.read_text(encoding="utf-8") == expected_file
    assert state_db.exists()
    with sqlite3.connect(state_db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] > 0


def test_scheduler_full_path_creates_due_task_and_runs_scheduler_execution(runtime_main, monkeypatch, tmp_path):
    """真实 ToolNode 创建 tasks.json 后，真实 pacemaker 将其投递给同一 worker 并持久化 scheduled turn。"""
    task_file = tmp_path / "tasks.json"
    task_file.write_text("[]", encoding="utf-8")
    state_db = tmp_path / "state.sqlite3"
    execution_db = tmp_path / "execution.sqlite3"
    queue = _TrackingQueue()
    interactive_tool_finished = asyncio.Event()
    scheduled_tool_finished = asyncio.Event()
    confirmation_contexts: list[set] = []
    confirmations = []
    future_time = (datetime.now() + timedelta(days=1)).replace(microsecond=0)

    class _SchedulerModel(_SequenceModel):
        """在 interactive ToolNode 成功后仅把真实创建出的 one-shot task 调整为已到期。"""

        def invoke(self, messages):
            response = super().invoke(messages)
            if len(self.inputs) == 2:
                with builtins.tasks_lock:
                    tasks = json.loads(task_file.read_text(encoding="utf-8"))
                    assert [task["description"] for task in tasks] == ["closure due task"]
                    tasks[0]["target_time"] = (datetime.now() - timedelta(minutes=1)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    task_file.write_text(json.dumps(tasks), encoding="utf-8")
                interactive_tool_finished.set()
            elif len(self.inputs) == 4:
                scheduled_tool_finished.set()
            return response

    model = _SchedulerModel([
        AIMessage(
            content="",
            tool_calls=[{
                "name": "schedule_task",
                "args": {
                    "target_time": future_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "description": "closure due task",
                    "repeat": None,
                    "repeat_count": None,
                },
                "id": "interactive-schedule",
                "type": "tool_call",
            }],
        ),
        AIMessage(content="scheduled"),
        AIMessage(
            content="",
            tool_calls=[{
                "name": "schedule_task",
                "args": {
                    "target_time": future_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "description": "scheduled mutation denied",
                    "repeat": None,
                    "repeat_count": None,
                },
                "id": "scheduled-denied",
                "type": "tool_call",
            }],
        ),
        AIMessage(content="scheduled denial explained"),
    ])

    class _AsyncioProxy:
        Queue = staticmethod(lambda: queue)

        def __getattr__(self, name: str):
            return getattr(asyncio, name)

    class _PromptSession:
        def __init__(self, *_args, **_kwargs) -> None:
            self.step = 0

        async def prompt_async(self, *_args, **_kwargs) -> str:
            self.step += 1
            if self.step == 1:
                return "create due task"
            await interactive_tool_finished.wait()
            await scheduled_tool_finished.wait()
            await _wait_for_durable_status_counts(
                execution_db, {ExecutionStatus.SUCCEEDED: 2}
            )
            return "/exit"

    observed: dict[str, object] = {}

    async def _fast_real_pacemaker(task_queue, check_interval: int = 10) -> None:
        observed["heartbeat_queue"] = task_queue
        await heartbeat.pacemaker_loop(task_queue, check_interval=0.001)

    class _NoopLogger:
        def log_event(self, *_args, **_kwargs) -> None:
            pass

    interactive_grants_token = set_session_permission_grants()
    interactive_grants = get_session_permission_grants()

    def confirm(request, _result):
        grants = get_session_permission_grants()
        assert grants is not None
        confirmations.append(request)
        confirmation_contexts.append(grants)
        if len(confirmations) == 1:
            return PermissionConfirmationChoice.ALLOW_SESSION
        assert len(confirmations) == 2
        assert grants == set()
        return PermissionConfirmationChoice.DENY

    confirmation_token = set_permission_confirmation_handler(confirm)
    try:
        monkeypatch.setattr(runtime_main, "asyncio", _AsyncioProxy())
        monkeypatch.setattr(agent_graph, "get_provider", lambda **_kwargs: _SequenceProvider(model))
        monkeypatch.setattr(agent_graph, "MEMORY_DIR", str(tmp_path / "memory"))
        monkeypatch.setattr(agent_graph, "audit_logger", _NoopLogger())
        monkeypatch.setattr(execution_module, "audit_logger", _NoopLogger())
        monkeypatch.setattr(builtins, "TASKS_FILE", str(task_file))
        monkeypatch.setattr(heartbeat, "TASKS_FILE", str(task_file))
        monkeypatch.setattr(builtins, "_permission_audit_logger", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            builtins, "_permission_confirmation_audit_logger", lambda *_args, **_kwargs: None
        )
        monkeypatch.setattr(runtime_main, "print_banner", lambda: None)
        monkeypatch.setattr(runtime_main, "cprint", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(runtime_main, "patch_stdout", lambda: nullcontext())
        monkeypatch.setattr(runtime_main, "PromptSession", _PromptSession)
        monkeypatch.setattr(runtime_main, "pacemaker_loop", _fast_real_pacemaker)
        monkeypatch.setattr(runtime_main, "DB_PATH", str(state_db))
        monkeypatch.setattr(runtime_main, "EXECUTION_DB_PATH", str(execution_db))
        asyncio.run(runtime_main.async_main(trace_context=TraceContext(run_id="scheduler-full-path")))
    finally:
        reset_permission_confirmation_handler(confirmation_token)
        reset_session_permission_grants(interactive_grants_token)

    scheduled_requests = [
        request
        for request in queue.put_items
        if isinstance(request, AgentRequest) and request.origin is AgentRequestOrigin.SCHEDULER
    ]
    outcomes = {
        message.tool_call_id: extract_tool_outcome(message)
        for batch in model.inputs
        for message in batch
        if isinstance(message, ToolMessage)
    }
    from miclaw.core.runtime.execution_store import ExecutionStore

    with ExecutionStore(execution_db, readonly=True) as store:
        records = store.list_latest_attempts(limit=100)
    assert observed["heartbeat_queue"] is queue
    assert len(scheduled_requests) == 1
    assert scheduled_requests[0].memory_write_intent is None
    assert "closure due task" in scheduled_requests[0].content
    assert outcomes["interactive-schedule"] == StructuredToolOutcome(True, None)
    assert outcomes["scheduled-denied"] == StructuredToolOutcome(False, "permission_denied")
    assert json.loads(task_file.read_text(encoding="utf-8")) == []
    assert len(confirmations) == 2
    assert confirmation_contexts[0] is interactive_grants
    assert confirmation_contexts[1] is not interactive_grants
    assert len({record.state.execution_id for record in records}) == 2
    assert Counter(record.state.status for record in records) == Counter({ExecutionStatus.SUCCEEDED: 2})
    with sqlite3.connect(state_db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] > 0


def test_scheduler_cancellation_restores_interactive_session_grants(runtime_main, monkeypatch, tmp_path):
    """取消 scheduler child 后，worker 的 finally 必须恢复原 interactive grants。"""
    scheduler_started = asyncio.Event()
    scheduler_cancelled = asyncio.Event()
    interactive_checked = asyncio.Event()
    observed: dict[str, object] = {}

    class _FakeApp:
        async def astream(self, inputs, *, config, stream_mode, durability):
            content = inputs["messages"][0].content
            grants = get_session_permission_grants()
            if content == "scheduled cancellation":
                observed["scheduler_grants"] = grants
                scheduler_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    scheduler_cancelled.set()
                    raise
            assert content == "interactive after scheduler cancel"
            observed["interactive_grants"] = grants
            interactive_checked.set()
            yield {"agent": {"messages": [AIMessage(content="healthy")]}}

        async def aget_state_history(self, _config):
            if False:  # pragma: no cover
                yield None

    class _FakePromptSession:
        def __init__(self, *_args, **_kwargs) -> None:
            self.step = 0

        async def prompt_async(self, *_args, **_kwargs) -> str:
            self.step += 1
            if self.step == 1:
                await scheduler_started.wait()
                return "/cancel"
            if self.step == 2:
                await scheduler_cancelled.wait()
                return "interactive after scheduler cancel"
            await interactive_checked.wait()
            return "/exit"

    async def fake_pacemaker_loop(task_queue, check_interval: int = 10):
        await task_queue.put(
            AgentRequest(content="scheduled cancellation", origin=AgentRequestOrigin.SCHEDULER)
        )
        await asyncio.Event().wait()

    state_db = tmp_path / "state.sqlite3"
    execution_db = tmp_path / "execution.sqlite3"
    interactive_grants_token = set_session_permission_grants()
    interactive_grants = get_session_permission_grants()
    try:
        monkeypatch.setattr(runtime_main, "print_banner", lambda: None)
        monkeypatch.setattr(runtime_main, "cprint", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(runtime_main, "patch_stdout", lambda: nullcontext())
        monkeypatch.setattr(runtime_main, "PromptSession", _FakePromptSession)
        monkeypatch.setattr(runtime_main, "pacemaker_loop", fake_pacemaker_loop)
        monkeypatch.setattr(runtime_main, "create_agent_app", lambda **_kwargs: _FakeApp())
        monkeypatch.setattr(runtime_main, "DB_PATH", str(state_db))
        monkeypatch.setattr(runtime_main, "EXECUTION_DB_PATH", str(execution_db))

        asyncio.run(runtime_main.async_main(trace_context=TraceContext(run_id="scheduler-cancel-grants")))
    finally:
        reset_session_permission_grants(interactive_grants_token)

    assert scheduler_cancelled.is_set()
    assert observed["scheduler_grants"] is not interactive_grants
    assert observed["scheduler_grants"] == set()
    assert observed["interactive_grants"] is interactive_grants
    with sqlite3.connect(execution_db) as connection:
        assert connection.execute("SELECT status FROM execution_attempts ORDER BY created_at").fetchall() == [
            ("cancelled",),
            ("succeeded",),
        ]


def test_remember_entry_e2e_writes_profile_with_turn_local_intent(runtime_main, monkeypatch, tmp_path):
    """/remember 经 entry→ToolNode→真实 Store 成功写入，metadata 不进入模型消息。"""

    profile_content = "I prefer Python for interview coding."
    model, memory_dir, execution_db, _, _ = _run_memory_entry(
        runtime_main,
        monkeypatch,
        tmp_path,
        prompts=("/remember I prefer Python for interview coding.", "/exit"),
        responses=[_profile_tool_call(profile_content, "remember-write"), AIMessage(content="已记住")],
    )

    profile = UserProfileStore(memory_dir / "user_profile.md")
    assert profile.profile_path.read_text(encoding="utf-8") == profile_content
    assert get_memory_write_intent() is None
    assert "memory_write_intent" not in _model_contents(model)
    assert "explicit_user_request" not in _model_contents(model)
    with sqlite3.connect(execution_db) as connection:
        assert connection.execute("SELECT status FROM execution_attempts").fetchall() == [("succeeded",)]


def test_remember_entry_e2e_permission_deny_keeps_profile_unchanged(runtime_main, monkeypatch, tmp_path):
    """/remember 只建立 eligibility，MEMORY_WRITE deny 仍阻止真实持久化。"""

    model, memory_dir, _, _, tool_error_types = _run_memory_entry(
        runtime_main,
        monkeypatch,
        tmp_path,
        prompts=("/remember denied content", "ordinary after deny", "/exit"),
        responses=[
            _profile_tool_call("DENIED_PROFILE", "remember-deny"),
            AIMessage(content="无法写入"),
            AIMessage(content="runtime remains healthy"),
        ],
        permission_choice=PermissionConfirmationChoice.DENY,
    )

    assert not (memory_dir / "user_profile.md").exists()
    assert "Permission denied" in _model_contents(model)
    assert tool_error_types == ["permission_denied"]
    assert len(model.inputs) == 3
    assert get_memory_write_intent() is None


def test_normal_entry_turn_cannot_self_authorize_memory_write(runtime_main, monkeypatch, tmp_path):
    """普通输入中模型即使主动调用 Tool，也没有 trusted write intent。"""

    model, memory_dir, _, _, tool_error_types = _run_memory_entry(
        runtime_main,
        monkeypatch,
        tmp_path,
        prompts=("hello", "/exit"),
        responses=[_profile_tool_call("MODEL_ESCALATION", "normal-write"), AIMessage(content="done")],
    )

    assert not (memory_dir / "user_profile.md").exists()
    assert len(model.inputs) == 1
    assert tool_error_types == []
    assert get_memory_write_intent() is None


def test_remember_intent_does_not_leak_to_next_turn(runtime_main, monkeypatch, tmp_path):
    """首轮 /remember 的 token 在 finally reset，下一普通 turn 仍被 lifecycle 拒绝。"""

    model, memory_dir, _, _, tool_error_types = _run_memory_entry(
        runtime_main,
        monkeypatch,
        tmp_path,
        prompts=("/remember first", "ordinary second turn", "/exit"),
        responses=[
            _profile_tool_call("FIRST_PROFILE", "first-write"),
            AIMessage(content="first done"),
            _profile_tool_call("SECOND_PROFILE", "second-write"),
            AIMessage(content="second done"),
        ],
    )

    assert (memory_dir / "user_profile.md").read_text(encoding="utf-8") == "FIRST_PROFILE"
    assert len(model.inputs) == 3
    assert tool_error_types == []
    assert get_memory_write_intent() is None


def test_remember_failure_cleanup_does_not_grant_next_turn(runtime_main, monkeypatch, tmp_path):
    """显式 turn 的 provider failure 后，下一普通 turn 仍不能继承 intent。"""

    model, memory_dir, _, _, tool_error_types = _run_memory_entry(
        runtime_main,
        monkeypatch,
        tmp_path,
        prompts=("/remember failed first", "ordinary second turn", "/exit"),
        responses=[
            RuntimeError("PROVIDER_SECRET"),
            _profile_tool_call("AFTER_FAILURE", "failure-followup"),
            AIMessage(content="second done"),
        ],
    )

    assert not (memory_dir / "user_profile.md").exists()
    assert len(model.inputs) == 2
    assert tool_error_types == []
    assert get_memory_write_intent() is None


def test_scheduler_request_cannot_gain_remember_intent(runtime_main, monkeypatch, tmp_path):
    """scheduler 只投递普通 AgentRequest，内容伪造 /remember 也不能提升权限。"""

    scheduled_request = AgentRequest(
        content="/remember scheduler-secret", origin=AgentRequestOrigin.SCHEDULER
    )
    model, memory_dir, _, _, tool_error_types = _run_memory_entry(
        runtime_main,
        monkeypatch,
        tmp_path,
        prompts=("/exit",),
        scheduled_requests=(scheduled_request,),
        responses=[_profile_tool_call("SCHEDULER_PROFILE", "scheduled-write"), AIMessage(content="done")],
    )

    assert scheduled_request.memory_write_intent is None
    assert not (memory_dir / "user_profile.md").exists()
    assert len(model.inputs) == 1
    assert tool_error_types == []
    assert get_memory_write_intent() is None


def test_remember_empty_command_does_not_enqueue_agent_request(runtime_main, monkeypatch):
    """空 /remember 只给 usage，不创建 request 或调用模型。"""

    output = []
    monkeypatch.setattr(runtime_main, "cprint", lambda text="", **_kwargs: output.append(text))
    assert runtime_main._parse_user_request("/remember") is None
    assert output == ["Usage: /remember <content>"]


def test_remember_parser_requires_exact_control_command(runtime_main):
    """只有完整 command token 后的空白可触发 trusted intent。"""
    assert runtime_main._parse_user_request("/remember\tPython") == AgentRequest(
        "请记住以下信息：Python",
        AgentRequestOrigin.INTERACTIVE,
        MemoryWriteIntent.EXPLICIT_USER_REQUEST,
    )
    assert runtime_main._parse_user_request("/remembered preference") == AgentRequest("/remembered preference", AgentRequestOrigin.INTERACTIVE)
