import json
import os
from pathlib import Path
import sys
import time

import anyio
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
import pytest
from typer.testing import CliRunner

import entry.monitor as monitor
import miclaw.core.agent as agent
import miclaw.core.mcp_permissions as mcp_permissions
from entry.cli import app as cli_app
from miclaw.core.logger import JSONLEventLogger
from miclaw.core.mcp_adapter import MCPToolDescriptor
from miclaw.core.mcp_client import MCPClientError, MCPStdioClient, MCPStdioServerConfig
from miclaw.core.mcp_tools import (
    MCPAgentToolRuntime,
    MCPToolRegistrationError,
    build_mcp_agent_tool,
    mcp_agent_tool_name,
    merge_agent_tools,
)
from miclaw.core.permissions import (
    PermissionConfirmationChoice,
    reset_permission_confirmation_handler,
    reset_session_permission_grants,
    set_permission_confirmation_handler,
    set_session_permission_grants,
)
from miclaw.core.trace import TraceContext, reset_trace_context, set_current_trace_context


SERVER_SCRIPT = Path(__file__).parent / "fixtures" / "mcp_test_server.py"


@tool
def local_echo(text: str) -> str:
    """Local test tool。"""
    return text


class _SequentialModel:
    def __init__(self, responses):
        self.responses = list(responses)

    def invoke(self, _messages):
        return self.responses.pop(0)

    async def ainvoke(self, _messages):
        return self.responses.pop(0)


class _Provider:
    def __init__(self, model):
        self.model = model
        self.bound_tools = None

    def bind_tools(self, tools):
        self.bound_tools = list(tools)
        return self.model


def _config(tmp_path, server_id="server-a"):
    return MCPStdioServerConfig(
        server_id=server_id,
        command=sys.executable,
        args=(str(SERVER_SCRIPT),),
        env={"MCP_TEST_PID_FILE": str(tmp_path / f"{server_id}.pid")},
    )


def _wait_process_stopped(pid):
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


async def _invoke_graph(monkeypatch, tools, responses, *, thread_id="mcp-agent"):
    provider = _Provider(_SequentialModel(responses))
    monkeypatch.setattr(agent, "get_provider", lambda **_kwargs: provider)
    app = agent.create_agent_app(tools=list(tools))
    state = await app.ainvoke(
        {"messages": [HumanMessage(content="invoke MCP tool")], "summary": ""},
        config={"configurable": {"thread_id": thread_id}},
    )
    return state, provider


def _tool_call(tool_name, arguments, call_id="mcp-call"):
    return AIMessage(
        content="",
        tool_calls=[{"name": tool_name, "args": arguments, "id": call_id, "type": "tool_call"}],
    )


def test_agent_name_is_stable_distinct_and_registration_rejects_collisions():
    descriptor_a = MCPToolDescriptor("server-a", "search", "Search", {"type": "object"})
    descriptor_b = MCPToolDescriptor("server-b", "search", "Search", {"type": "object"})
    client_a = MCPStdioClient(MCPStdioServerConfig("server-a", "unused"))

    name_a = mcp_agent_tool_name(descriptor_a)
    name_b = mcp_agent_tool_name(descriptor_b)
    wrapped = build_mcp_agent_tool(client_a, descriptor_a)

    assert name_a == mcp_agent_tool_name(descriptor_a)
    assert name_a != name_b
    assert len(name_a) <= 64
    assert wrapped.metadata["mcp_qualified_name"] == "mcp::server-a::search"
    assert [item.name for item in merge_agent_tools([local_echo], [wrapped])] == ["local_echo", name_a]
    with pytest.raises(MCPToolRegistrationError, match="duplicate_agent_tool"):
        merge_agent_tools([local_echo], [local_echo])


def test_real_discovery_registers_local_and_same_name_multi_server_with_schema(tmp_path):
    async def scenario():
        runtime = MCPAgentToolRuntime(
            [_config(tmp_path, "server-a"), _config(tmp_path, "server-b")],
            local_tools=[local_echo],
        )
        async with runtime:
            echo_names = [
                name
                for name, descriptor in runtime.descriptors_by_agent_name.items()
                if descriptor.name == "echo"
            ]
            schema_name = next(
                name
                for name, descriptor in runtime.descriptors_by_agent_name.items()
                if descriptor.server_id == "server-a" and descriptor.name == "schema_echo"
            )
            schema_tool = next(tool for tool in runtime.mcp_tools if tool.name == schema_name)
            assert runtime.tools[0].name == "local_echo"
            assert len(echo_names) == 2
            assert len(set(echo_names)) == 2
            assert schema_tool.args_schema["required"] == ["query", "mode", "options"]
            assert schema_tool.args_schema["properties"]["mode"]["enum"] == ["fast", "safe"]
            assert schema_tool.args_schema["properties"]["options"]["$ref"]
            assert not {"command", "args", "cwd", "env", "server_id"} & set(schema_tool.args_schema["properties"])
            pids = [
                int((tmp_path / f"{server_id}.pid").read_text(encoding="utf-8"))
                for server_id in ("server-a", "server-b")
            ]
        assert runtime.tools == []
        return pids

    pids = anyio.run(scenario)
    assert all(_wait_process_stopped(pid) for pid in pids)


def test_discovery_failure_registers_nothing_and_cleans_prior_server(tmp_path):
    runtime = MCPAgentToolRuntime(
        [
            _config(tmp_path),
            MCPStdioServerConfig("server-b", str(tmp_path / "missing-command")),
        ],
        local_tools=[local_echo],
    )

    async def scenario():
        with pytest.raises(MCPClientError):
            async with runtime:
                pytest.fail("partial MCP tool set must not be published")
        return int((tmp_path / "server-a.pid").read_text(encoding="utf-8"))

    pid = anyio.run(scenario)

    assert runtime.tools == []
    assert runtime.descriptors_by_agent_name == {}
    assert _wait_process_stopped(pid)


def test_real_agent_invokes_stdio_mcp_echo_and_returns_tool_message(tmp_path, monkeypatch):
    async def scenario():
        async with MCPAgentToolRuntime([_config(tmp_path)], local_tools=[local_echo]) as runtime:
            name = next(
                name for name, descriptor in runtime.descriptors_by_agent_name.items() if descriptor.name == "echo"
            )
            confirmation_token = set_permission_confirmation_handler(
                lambda *_: PermissionConfirmationChoice.ALLOW_ONCE
            )
            try:
                state, provider = await _invoke_graph(
                    monkeypatch,
                    runtime.tools,
                    [_tool_call(name, {"text": "hello MCP"}), AIMessage(content="done")],
                )
            finally:
                reset_permission_confirmation_handler(confirmation_token)
            pid = int((tmp_path / "server-a.pid").read_text(encoding="utf-8"))
            return state, provider, name, pid

    state, provider, name, pid = anyio.run(scenario)

    tool_messages = [message for message in state["messages"] if isinstance(message, ToolMessage)]
    assert [tool.name for tool in provider.bound_tools][0] == "local_echo"
    assert name in [tool.name for tool in provider.bound_tools]
    assert tool_messages[-1].content == "hello MCP"
    assert _wait_process_stopped(pid)


def test_agent_mcp_tool_error_returns_tool_message_without_crashing_graph(tmp_path, monkeypatch):
    async def scenario():
        async with MCPAgentToolRuntime([_config(tmp_path)]) as runtime:
            name = next(
                name
                for name, descriptor in runtime.descriptors_by_agent_name.items()
                if descriptor.name == "failing_tool"
            )
            token = set_permission_confirmation_handler(lambda *_: PermissionConfirmationChoice.ALLOW_ONCE)
            try:
                state, _ = await _invoke_graph(
                    monkeypatch,
                    runtime.tools,
                    [_tool_call(name, {}), AIMessage(content="recovered")],
                )
            finally:
                reset_permission_confirmation_handler(token)
            return state

    state = anyio.run(scenario)

    tool_message = next(message for message in state["messages"] if isinstance(message, ToolMessage))
    assert "expected tool failure" in tool_message.content
    assert state["messages"][-1].content == "recovered"


def test_sync_agent_fails_closed_without_calling_mcp_server(tmp_path, monkeypatch):
    marker = tmp_path / "sync-marker.txt"

    async def scenario():
        async with MCPAgentToolRuntime([_config(tmp_path)]) as runtime:
            name = next(
                name
                for name, descriptor in runtime.descriptors_by_agent_name.items()
                if descriptor.name == "side_effect_marker"
            )
            provider = _Provider(
                _SequentialModel(
                    [
                        _tool_call(name, {"path": str(marker)}),
                        AIMessage(content="sync blocked"),
                    ]
                )
            )
            monkeypatch.setattr(agent, "get_provider", lambda **_kwargs: provider)
            app = agent.create_agent_app(tools=runtime.tools)
            state = await anyio.to_thread.run_sync(
                lambda: app.invoke(
                    {"messages": [HumanMessage(content="sync MCP")], "summary": ""},
                    config={"configurable": {"thread_id": "sync-agent"}},
                )
            )
            return state

    state = anyio.run(scenario)

    tool_message = next(message for message in state["messages"] if isinstance(message, ToolMessage))
    assert tool_message.content == "MCP tools require async agent execution"
    assert not marker.exists()


def test_permission_audit_receives_real_agent_thread_id(tmp_path, monkeypatch):
    observed_thread_ids = []
    monkeypatch.setattr(
        mcp_permissions,
        "_permission_decision_audit_logger",
        lambda *args, **kwargs: observed_thread_ids.append(kwargs["thread_id"]),
    )
    monkeypatch.setattr(
        mcp_permissions,
        "_permission_confirmation_audit_logger",
        lambda *args, **kwargs: observed_thread_ids.append(kwargs["thread_id"]),
    )

    async def scenario():
        async with MCPAgentToolRuntime([_config(tmp_path)]) as runtime:
            name = next(
                name
                for name, descriptor in runtime.descriptors_by_agent_name.items()
                if descriptor.name == "schema_echo"
            )
            token = set_permission_confirmation_handler(lambda *_: PermissionConfirmationChoice.ALLOW_ONCE)
            try:
                state, _ = await _invoke_graph(
                    monkeypatch,
                    runtime.tools,
                    [
                        _tool_call(
                            name,
                            {
                                "query": "hello",
                                "mode": "safe",
                                "options": {"limit": 3},
                                "config": "MODEL_CONFIG_VALUE",
                            },
                        ),
                        AIMessage(content="done"),
                    ],
                    thread_id="real-agent-thread",
                )
            finally:
                reset_permission_confirmation_handler(token)
            return state

    state = anyio.run(scenario)

    assert observed_thread_ids == ["real-agent-thread", "real-agent-thread"]
    tool_message = next(message for message in state["messages"] if isinstance(message, ToolMessage))
    assert tool_message.content == "hello:safe:3::MODEL_CONFIG_VALUE"


@pytest.mark.parametrize(
    ("handler", "expected_text"),
    [
        (None, "Permission required"),
        (lambda *_: PermissionConfirmationChoice.DENY, "Permission denied"),
    ],
)
def test_agent_side_effect_is_blocked_without_final_allow(tmp_path, monkeypatch, handler, expected_text):
    marker = tmp_path / "blocked-marker.txt"

    async def scenario():
        async with MCPAgentToolRuntime([_config(tmp_path)]) as runtime:
            name = next(
                name
                for name, descriptor in runtime.descriptors_by_agent_name.items()
                if descriptor.name == "side_effect_marker"
            )
            token = set_permission_confirmation_handler(handler) if handler is not None else None
            try:
                state, _ = await _invoke_graph(
                    monkeypatch,
                    runtime.tools,
                    [_tool_call(name, {"path": str(marker)}), AIMessage(content="done")],
                )
            finally:
                if token is not None:
                    reset_permission_confirmation_handler(token)
            return state

    state = anyio.run(scenario)

    tool_message = next(message for message in state["messages"] if isinstance(message, ToolMessage))
    assert expected_text in tool_message.content
    assert not marker.exists()


def test_agent_allow_once_executes_side_effect(tmp_path, monkeypatch):
    marker = tmp_path / "allowed-marker.txt"

    async def scenario():
        async with MCPAgentToolRuntime([_config(tmp_path)]) as runtime:
            name = next(
                name
                for name, descriptor in runtime.descriptors_by_agent_name.items()
                if descriptor.name == "side_effect_marker"
            )
            token = set_permission_confirmation_handler(lambda *_: PermissionConfirmationChoice.ALLOW_ONCE)
            try:
                state, _ = await _invoke_graph(
                    monkeypatch,
                    runtime.tools,
                    [
                        _tool_call(name, {"path": str(marker), "value": "allowed"}),
                        AIMessage(content="done"),
                    ],
                )
            finally:
                reset_permission_confirmation_handler(token)
            return state

    state = anyio.run(scenario)

    tool_message = next(message for message in state["messages"] if isinstance(message, ToolMessage))
    assert tool_message.content == "marker written"
    assert marker.read_text(encoding="utf-8") == "allowed\n"


def test_agent_allow_session_reuses_exact_tool_but_not_other_identity(tmp_path, monkeypatch):
    marker = tmp_path / "session-marker.txt"
    confirmation_requests = []

    def confirmation(request, _result):
        confirmation_requests.append(request.target)
        if request.target == "mcp::server-a::side_effect_marker":
            return PermissionConfirmationChoice.ALLOW_SESSION
        return PermissionConfirmationChoice.DENY

    async def scenario():
        async with MCPAgentToolRuntime(
            [_config(tmp_path, "server-a"), _config(tmp_path, "server-b")]
        ) as runtime:
            marker_name = next(
                name
                for name, descriptor in runtime.descriptors_by_agent_name.items()
                if descriptor.server_id == "server-a" and descriptor.name == "side_effect_marker"
            )
            other_server_name = next(
                name
                for name, descriptor in runtime.descriptors_by_agent_name.items()
                if descriptor.server_id == "server-b" and descriptor.name == "side_effect_marker"
            )
            echo_name = next(
                name
                for name, descriptor in runtime.descriptors_by_agent_name.items()
                if descriptor.server_id == "server-a" and descriptor.name == "echo"
            )
            grant_token = set_session_permission_grants()
            confirmation_token = set_permission_confirmation_handler(confirmation)
            try:
                state, _ = await _invoke_graph(
                    monkeypatch,
                    runtime.tools,
                    [
                        _tool_call(marker_name, {"path": str(marker), "value": "one"}, "call-1"),
                        _tool_call(marker_name, {"path": str(marker), "value": "two"}, "call-2"),
                        _tool_call(
                            other_server_name,
                            {"path": str(marker), "value": "other-server"},
                            "call-3",
                        ),
                        _tool_call(echo_name, {"text": "must remain blocked"}, "call-4"),
                        AIMessage(content="done"),
                    ],
                )
            finally:
                reset_permission_confirmation_handler(confirmation_token)
                reset_session_permission_grants(grant_token)
            return state

    state = anyio.run(scenario)

    tool_messages = [message.content for message in state["messages"] if isinstance(message, ToolMessage)]
    assert marker.read_text(encoding="utf-8") == "one\ntwo\n"
    assert len(confirmation_requests) == 3
    assert confirmation_requests[0].endswith("::side_effect_marker")
    assert confirmation_requests[1] == "mcp::server-b::side_effect_marker"
    assert confirmation_requests[2].endswith("::echo")
    assert "Permission denied" in tool_messages[-1]


def test_agent_mcp_observability_redacts_arguments_and_results(tmp_path, monkeypatch):
    log_file = tmp_path / "mcp-agent.jsonl"
    logger = JSONLEventLogger(log_file=log_file)
    monkeypatch.setattr(agent, "audit_logger", logger)

    async def scenario():
        async with MCPAgentToolRuntime([_config(tmp_path)]) as runtime:
            name = next(
                name
                for name, descriptor in runtime.descriptors_by_agent_name.items()
                if descriptor.name == "secret_result"
            )
            confirmation_token = set_permission_confirmation_handler(
                lambda *_: PermissionConfirmationChoice.ALLOW_ONCE
            )
            trace_token = set_current_trace_context(TraceContext(run_id="mcp-agent-run"))
            try:
                state, _ = await _invoke_graph(
                    monkeypatch,
                    runtime.tools,
                    [
                        _tool_call(
                            name,
                            {"value": "Authorization: Bearer MCP_AGENT_ARGUMENT_SECRET"},
                        ),
                        AIMessage(content="done"),
                    ],
                    thread_id="mcp-agent-audit",
                )
            finally:
                reset_trace_context(trace_token)
                reset_permission_confirmation_handler(confirmation_token)
            return state

    state = anyio.run(scenario)
    logger.log_queue.join()
    logger.shutdown()
    raw_log = log_file.read_text(encoding="utf-8")
    events = monitor.read_jsonl_events(log_file)
    rendered = "\n".join(monitor.format_log_event_for_cli(event) for event in events)
    logs_output = CliRunner().invoke(
        cli_app,
        ["logs", "--tail", "--log-file", str(log_file), "--lines", "100"],
    )
    trace_output = CliRunner().invoke(
        cli_app,
        ["trace", "mcp-agent-run", "--log-file", str(log_file)],
    )
    tool_message = next(message for message in state["messages"] if isinstance(message, ToolMessage))

    assert "MCP_AGENT_ARGUMENT_SECRET" in tool_message.content
    assert "MCP_AGENT_RESULT_SECRET" in tool_message.content
    assert "MCP_AGENT_ARGUMENT_SECRET" not in raw_log
    assert "MCP_AGENT_RESULT_SECRET" not in raw_log
    assert "MCP_AGENT_ARGUMENT_SECRET" not in rendered
    assert "MCP_AGENT_RESULT_SECRET" not in rendered
    assert logs_output.exit_code == 0
    assert trace_output.exit_code == 0
    assert "MCP_AGENT_ARGUMENT_SECRET" not in logs_output.output
    assert "MCP_AGENT_RESULT_SECRET" not in logs_output.output
    assert "MCP_AGENT_ARGUMENT_SECRET" not in trace_output.output
    assert "MCP_AGENT_RESULT_SECRET" not in trace_output.output
    assert all(event.get("run_id") == "mcp-agent-run" for event in events)
    step_ids = [event.get("step_id") for event in events]
    assert step_ids == sorted(step_ids)
    assert len(step_ids) == len(set(step_ids))
