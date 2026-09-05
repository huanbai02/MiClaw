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
from miclaw.core.agent.request import AgentRequest
import miclaw.core.agent.graph as agent_graph
import miclaw.core.memory.permissions as memory_permissions
from miclaw.core.memory.lifecycle import MemoryWriteIntent, get_memory_write_intent
from miclaw.core.memory.user_profile import UserProfileStore
from miclaw.core.security.permissions import (
    PermissionConfirmationChoice,
    reset_permission_confirmation_handler,
    set_permission_confirmation_handler,
)
from miclaw.core.tools import builtins
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
    assert queue.put_items == [AgentRequest(content="/exit")]
    assert queue.get_items == [AgentRequest(content="/exit")]
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
        def log_event(self, **_kwargs) -> None:
            pass

    async def fake_pacemaker_loop(task_queue, check_interval: int = 10):
        for request in scheduled_requests:
            await task_queue.put(request)
        await asyncio.Event().wait()

    state_db = tmp_path / "state.sqlite3"
    execution_db = tmp_path / "execution.sqlite3"
    original_format_tool_result = builtins.format_tool_result_for_model

    def capture_tool_result(result):
        if result.error_type is not None:
            tool_error_types.append(result.error_type)
        return original_format_tool_result(result)

    monkeypatch.setattr(agent_graph, "get_provider", lambda **_kwargs: _SequenceProvider(model))
    monkeypatch.setattr(agent_graph, "MEMORY_DIR", str(memory_dir))
    monkeypatch.setattr(agent_graph, "audit_logger", _NoopLogger())
    monkeypatch.setattr(builtins, "MEMORY_DIR", str(memory_dir))
    monkeypatch.setattr(builtins, "format_tool_result_for_model", capture_tool_result)
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

    return model, memory_dir, execution_db, rendered, tool_error_types


def _model_contents(model: _SequenceModel) -> str:
    """提取模型所见 message 内容，用于验证 ToolNode 返回的稳定结果。"""
    return "\n".join(str(message.content) for batch in model.inputs for message in batch)


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
    assert "Memory write is not eligible" in _model_contents(model)
    assert tool_error_types == ["memory_write_not_eligible"]
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
    assert "Memory write is not eligible" in _model_contents(model)
    assert tool_error_types == ["memory_write_not_eligible"]
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
    assert "Memory write is not eligible" in _model_contents(model)
    assert tool_error_types == ["memory_write_not_eligible"]
    assert get_memory_write_intent() is None


def test_scheduler_request_cannot_gain_remember_intent(runtime_main, monkeypatch, tmp_path):
    """scheduler 只投递普通 AgentRequest，内容伪造 /remember 也不能提升权限。"""

    scheduled_request = AgentRequest(content="/remember scheduler-secret")
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
    assert "Memory write is not eligible" in _model_contents(model)
    assert tool_error_types == ["memory_write_not_eligible"]
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
        MemoryWriteIntent.EXPLICIT_USER_REQUEST,
    )
    assert runtime_main._parse_user_request("/remembered preference") == AgentRequest("/remembered preference")
