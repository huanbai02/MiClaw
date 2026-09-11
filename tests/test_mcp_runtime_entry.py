"""锁定 MCP stdio 从 host 配置到默认 entry ToolNode 的真实链路。"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import importlib
import json
import os
from pathlib import Path
import sys
import time

from langchain_core.messages import AIMessage
from langchain_core.tools import tool
import pytest

import entry
import miclaw.core.agent.graph as agent_graph
from miclaw.core.mcp.adapter import MCPToolDescriptor
from miclaw.core.mcp.tools import mcp_agent_tool_name
from miclaw.core.observability.trace import TraceContext
from miclaw.core.security.permissions import (
    PermissionConfirmationChoice,
    reset_permission_confirmation_handler,
    set_permission_confirmation_handler,
)


SERVER_SCRIPT = Path(__file__).parent / "fixtures" / "mcp_test_server.py"


class _SequenceModel:
    """让真实 Agent graph 获得可预测的 Tool call 与最终回答。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = list(responses)
        self.inputs = []

    def invoke(self, messages):
        self.inputs.append(messages)
        return self.responses.pop(0)


class _Provider:
    def __init__(self, model: _SequenceModel) -> None:
        self.model = model
        self.bound_tools = []

    def bind_tools(self, tools):
        self.bound_tools = list(tools)
        return self.model


def _wait_process_stopped(pid: int) -> bool:
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def _write_config(tmp_path, servers: list[dict]) -> str:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    return str(path)


def _write_raw_config(tmp_path, content: str) -> str:
    path = tmp_path / "mcp.json"
    path.write_text(content, encoding="utf-8")
    return str(path)


def _server_config(tmp_path, server_id: str = "server-a") -> dict:
    return {
        "id": server_id,
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(SERVER_SCRIPT)],
        "env": {
            "MCP_TEST_PID_FILE": str(tmp_path / f"{server_id}.pid"),
            "MCP_TEST_CALL_COUNT_FILE": str(tmp_path / f"{server_id}.calls"),
        },
    }


def _tool_call(name: str, args: dict, call_id: str = "mcp-call") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


def _run_entry(runtime_main, monkeypatch, tmp_path, *, config_path: str, responses: list[AIMessage], prompts=("use MCP", "/exit"), permission=PermissionConfirmationChoice.ALLOW_ONCE):
    """经真实 entry、stdio client、graph 与 ToolNode 执行受控 MCP turn。"""
    model = _SequenceModel(responses)
    provider = _Provider(model)
    prompt_values = iter(prompts)

    @tool
    def skill_probe(text: str) -> str:
        """证明 dynamic Skill Tool 不会被 MCP 覆盖。"""
        return text

    class _FakePromptSession:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def prompt_async(self, *_args, **_kwargs) -> str:
            return next(prompt_values)

    async def fake_pacemaker_loop(_task_queue, check_interval: int = 10):
        await asyncio.Event().wait()

    class _NoopLogger:
        def log_event(self, **_kwargs) -> None:
            pass

    monkeypatch.setattr(agent_graph, "get_provider", lambda **_kwargs: provider)
    monkeypatch.setattr(agent_graph, "load_dynamic_skills", lambda: [skill_probe])
    monkeypatch.setattr(agent_graph, "audit_logger", _NoopLogger())
    monkeypatch.setattr(runtime_main, "print_banner", lambda: None)
    monkeypatch.setattr(runtime_main, "cprint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_main, "patch_stdout", lambda: nullcontext())
    monkeypatch.setattr(runtime_main, "PromptSession", _FakePromptSession)
    monkeypatch.setattr(runtime_main, "pacemaker_loop", fake_pacemaker_loop)
    monkeypatch.setattr(runtime_main, "DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(runtime_main, "EXECUTION_DB_PATH", str(tmp_path / "execution.sqlite3"))

    token = set_permission_confirmation_handler(lambda *_args: permission)
    try:
        asyncio.run(runtime_main.async_main(TraceContext(run_id="mcp-entry-run"), config_path))
    finally:
        reset_permission_confirmation_handler(token)
    return provider, model


@pytest.fixture
def runtime_main():
    module = importlib.import_module("entry.main")
    try:
        yield module
    finally:
        if getattr(entry, "main", None) is module:
            delattr(entry, "main")
        sys.modules.pop("entry.main", None)


def test_entry_mcp_config_discovers_and_invokes_real_stdio_tool_with_permission_allow(runtime_main, monkeypatch, tmp_path):
    """host JSON 配置启动 server，ToolNode 经 permission 后实际调用 MCP tools/call。"""
    marker = tmp_path / "allowed-marker.txt"
    config_path = _write_config(tmp_path, [_server_config(tmp_path)])
    name = mcp_agent_tool_name(MCPToolDescriptor("server-a", "side_effect_marker", "", {"type": "object"}))

    provider, model = _run_entry(
        runtime_main,
        monkeypatch,
        tmp_path,
        config_path=config_path,
        responses=[_tool_call(name, {"path": str(marker), "value": "allowed"}), AIMessage(content="done")],
    )

    assert marker.read_text(encoding="utf-8") == "allowed\n"
    assert (tmp_path / "server-a.calls").read_text(encoding="utf-8") == "1\n"
    names = [tool.name for tool in provider.bound_tools]
    assert {"calculator", "skill_probe", name} <= set(names)
    assert next(tool for tool in provider.bound_tools if tool.name == name).metadata["mcp_qualified_name"] == "mcp::server-a::side_effect_marker"
    seen = "\n".join(str(message.content) for batch in model.inputs for message in batch)
    assert sys.executable not in seen
    assert "MCP_TEST_PID_FILE" not in seen
    pid = int((tmp_path / "server-a.pid").read_text(encoding="utf-8"))
    assert _wait_process_stopped(pid)


def test_entry_mcp_permission_deny_does_not_call_stdio_tool(runtime_main, monkeypatch, tmp_path):
    """配置 server 不等于模型已获调用权；DENY 时 side effect 不发生。"""
    marker = tmp_path / "denied-marker.txt"
    config_path = _write_config(tmp_path, [_server_config(tmp_path)])
    name = mcp_agent_tool_name(MCPToolDescriptor("server-a", "side_effect_marker", "", {"type": "object"}))

    _run_entry(
        runtime_main,
        monkeypatch,
        tmp_path,
        config_path=config_path,
        responses=[_tool_call(name, {"path": str(marker)}), AIMessage(content="denied")],
        permission=PermissionConfirmationChoice.DENY,
    )

    assert not marker.exists()
    assert not (tmp_path / "server-a.calls").exists()
    pid = int((tmp_path / "server-a.pid").read_text(encoding="utf-8"))
    assert _wait_process_stopped(pid)


def test_entry_mcp_start_failure_is_stable_and_rolls_back_prior_server(runtime_main, monkeypatch, tmp_path):
    """多 server 启动部分失败时不进入 Agent，已启动的子进程必须被关闭。"""
    config_path = _write_config(
        tmp_path,
        [_server_config(tmp_path, "server-a"), {"id": "server-b", "transport": "stdio", "command": str(tmp_path / "SECRET_COMMAND"), "args": []}],
    )
    monkeypatch.setattr(runtime_main, "print_banner", lambda: None)
    monkeypatch.setattr(runtime_main, "cprint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_main, "DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(runtime_main, "EXECUTION_DB_PATH", str(tmp_path / "execution.sqlite3"))

    with pytest.raises(RuntimeError, match="mcp_runtime_start_failed") as error:
        asyncio.run(runtime_main.async_main(TraceContext(run_id="mcp-start-failure"), config_path))

    assert "SECRET_COMMAND" not in str(error.value)
    pid = int((tmp_path / "server-a.pid").read_text(encoding="utf-8"))
    assert _wait_process_stopped(pid)


def test_entry_mcp_malformed_config_fails_before_agent_start(runtime_main, monkeypatch, tmp_path):
    """无效 host config 不启动 MCP 或 Agent，也不降级为未知 Tool 集。"""
    config_path = _write_config(tmp_path, [{"id": "server-a", "transport": "http", "command": "ignored", "args": []}])
    monkeypatch.setattr(runtime_main, "print_banner", lambda: None)
    monkeypatch.setattr(runtime_main, "create_agent_app", lambda **_kwargs: pytest.fail("agent must not start"))

    with pytest.raises(RuntimeError, match="invalid_mcp_config"):
        asyncio.run(runtime_main.async_main(TraceContext(run_id="invalid-mcp-config"), config_path))


def test_entry_mcp_duplicate_key_config_fails_before_subprocess_start(runtime_main, monkeypatch, tmp_path):
    """严格 parser 在普通 dict 覆盖发生前终止 bootstrap，不能启动后出现的 command。"""
    server = _server_config(tmp_path)
    config_path = _write_raw_config(
        tmp_path,
        "{" 
        '"servers": [{'
        '"id": "server-a", '
        '"transport": "stdio", '
        '"command": "SECRET_EXECUTABLE_NAME", '
        f'"command": {json.dumps(server["command"])}, '
        f'"args": {json.dumps(server["args"])}, '
        f'"env": {json.dumps(server["env"])}'
        "}]"
        "}",
    )
    monkeypatch.setattr(runtime_main, "print_banner", lambda: None)

    with pytest.raises(RuntimeError, match="invalid_mcp_config") as error:
        asyncio.run(runtime_main.async_main(TraceContext(run_id="duplicate-mcp-config"), config_path))

    assert "SECRET_EXECUTABLE_NAME" not in str(error.value)
    assert not (tmp_path / "server-a.pid").exists()
