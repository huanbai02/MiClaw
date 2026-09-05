"""锁定默认 runtime bootstrap 的共享队列与无模型启动边界。"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3
import sys
from contextlib import nullcontext

import entry
import pytest
from langchain_core.messages import AIMessage
from miclaw.core.observability.trace import TraceContext


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
    assert queue.put_items == ["/exit"]
    assert queue.get_items == ["/exit"]
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
