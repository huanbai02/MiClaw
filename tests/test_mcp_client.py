import json
import os
from pathlib import Path
import sys
import time

import anyio
import pytest
from mcp import types as mcp_types

from entry.monitor import format_log_event_for_cli, get_trace_events
import miclaw.core.mcp_client as mcp_client
import miclaw.core.mcp_permissions as mcp_permissions
from miclaw.core.logger import (
    JSONLEventLogger,
    build_permission_confirmation_event,
    build_permission_decision_event,
)
from miclaw.core.mcp_adapter import MCPAdapterError, MCPToolDescriptor
from miclaw.core.mcp_client import (
    MCPClientError,
    MCPStdioClient,
    MCPStdioServerConfig,
)
from miclaw.core.permissions import (
    PermissionConfirmationChoice,
    PermissionDecision,
    PermissionResult,
    RiskLevel,
    reset_session_permission_grants,
    set_session_permission_grants,
)
from miclaw.core.trace import TraceContext, reset_trace_context, set_current_trace_context


SERVER_SCRIPT = Path(__file__).parent / "fixtures" / "mcp_test_server.py"


@pytest.fixture(autouse=True)
def disable_global_permission_audit(monkeypatch):
    monkeypatch.setattr(mcp_permissions, "_permission_decision_audit_logger", lambda *args, **kwargs: None)
    monkeypatch.setattr(mcp_permissions, "_permission_confirmation_audit_logger", lambda *args, **kwargs: None)


def _config(tmp_path, *, server_id="server-a", env=None):
    configured_env = {"MCP_TEST_PID_FILE": str(tmp_path / f"{server_id}.pid")}
    configured_env.update(env or {})
    return MCPStdioServerConfig(
        server_id=server_id,
        command=sys.executable,
        args=(str(SERVER_SCRIPT),),
        env=configured_env,
    )


async def _find_tool(client, name):
    return next(tool for tool in await client.list_tools() if tool.name == name)


def _allow_once(*_):
    return PermissionConfirmationChoice.ALLOW_ONCE


def _wait_process_stopped(pid):
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def test_real_stdio_lifecycle_lists_tools_through_adapter_and_stops_process(tmp_path, capsys):
    config = _config(tmp_path)

    async def scenario():
        async with MCPStdioClient(config) as client:
            tools = await client.list_tools()
            echo = next(tool for tool in tools if tool.name == "echo")
            assert echo.server_id == "server-a"
            assert echo.qualified_name == "mcp::server-a::echo"
            assert echo.input_schema["type"] == "object"
            assert echo.input_schema["properties"]["text"]["type"] == "string"
            pid = int((tmp_path / "server-a.pid").read_text(encoding="utf-8"))
        return pid

    pid = anyio.run(scenario)

    assert _wait_process_stopped(pid)
    captured = capsys.readouterr()
    assert "MCP_STDERR_SECRET_MARKER" not in captured.out + captured.err


def test_spawn_error_is_stable_and_does_not_leak_launch_details(tmp_path):
    secret_command = str(tmp_path / "MCP_COMMAND_SECRET_MARKER")
    config = MCPStdioServerConfig("server-a", secret_command)

    async def scenario():
        with pytest.raises(MCPClientError) as caught:
            async with MCPStdioClient(config, timeout_seconds=1):
                pytest.fail("invalid command must not connect")
        return caught.value

    error = anyio.run(scenario)

    assert error.code == "mcp_spawn_error"
    assert "MCP_COMMAND_SECRET_MARKER" not in str(error)
    assert str(tmp_path) not in str(error)


def test_permission_blocks_side_effect_until_explicit_allow(tmp_path, monkeypatch):
    marker = tmp_path / "marker.txt"

    async def scenario():
        async with MCPStdioClient(_config(tmp_path)) as client:
            tool = await _find_tool(client, "side_effect_marker")

            no_handler = await client.call_tool(tool, {"path": str(marker)})
            assert not no_handler.success
            assert no_handler.error_type == "permission_required"
            assert not marker.exists()

            denied = await client.call_tool(
                tool,
                {"path": str(marker)},
                confirmation_handler=lambda *_: PermissionConfirmationChoice.DENY,
            )
            assert not denied.success
            assert not marker.exists()

            def failed_confirmation(*_):
                raise RuntimeError("CONFIRMATION_SECRET")

            failed = await client.call_tool(
                tool,
                {"path": str(marker)},
                confirmation_handler=failed_confirmation,
            )
            assert not failed.success
            assert not marker.exists()

            monkeypatch.setattr(
                mcp_permissions,
                "_permission_evaluator",
                lambda request: PermissionResult(PermissionDecision.DENY, "Denied", RiskLevel.HIGH),
            )
            policy_denied = await client.call_tool(
                tool,
                {"path": str(marker)},
                confirmation_handler=_allow_once,
            )
            assert not policy_denied.success
            assert not marker.exists()

            monkeypatch.setattr(mcp_permissions, "_permission_evaluator", mcp_permissions.evaluate_permission)
            allowed = await client.call_tool(
                tool,
                {"path": str(marker), "value": "allowed"},
                confirmation_handler=_allow_once,
            )
            assert allowed.success
            assert marker.read_text(encoding="utf-8") == "allowed\n"

    anyio.run(scenario)


def test_session_grant_reuses_only_same_server_and_tool(tmp_path):
    marker = tmp_path / "session-marker.txt"

    async def scenario():
        token = set_session_permission_grants()
        try:
            async with MCPStdioClient(_config(tmp_path)) as client:
                marker_tool = await _find_tool(client, "side_effect_marker")
                echo_tool = await _find_tool(client, "echo")
                first = await client.call_tool(
                    marker_tool,
                    {"path": str(marker), "value": "first"},
                    confirmation_handler=lambda *_: PermissionConfirmationChoice.ALLOW_SESSION,
                )
                reused = await client.call_tool(
                    marker_tool,
                    {"path": str(marker), "value": "second"},
                )
                other_tool = await client.call_tool(echo_tool, {"text": "not called"})
                foreign = MCPToolDescriptor(
                    server_id="server-b",
                    name=marker_tool.name,
                    description=marker_tool.description,
                    input_schema=marker_tool.input_schema,
                )
                other_server = await client.call_tool(foreign, {"path": str(marker)})
        finally:
            reset_session_permission_grants(token)
        return first, reused, other_tool, other_server

    first, reused, other_tool, other_server = anyio.run(scenario)

    assert first.success and reused.success
    assert marker.read_text(encoding="utf-8") == "first\nsecond\n"
    assert other_tool.error_type == "permission_required"
    assert other_server.error_type == "mcp_server_mismatch"


def test_result_mapping_handles_success_error_bounds_non_text_and_timeout(tmp_path):
    async def scenario():
        async with MCPStdioClient(_config(tmp_path), timeout_seconds=1.0) as client:
            echo = await _find_tool(client, "echo")
            failing = await _find_tool(client, "failing_tool")
            long_text = await _find_tool(client, "long_text")
            image = await _find_tool(client, "image_result")
            slow = await _find_tool(client, "slow_tool")
            results = (
                await client.call_tool(echo, {"text": "hello"}, confirmation_handler=_allow_once),
                await client.call_tool(failing, {}, confirmation_handler=_allow_once),
                await client.call_tool(long_text, {"length": 5000}, confirmation_handler=_allow_once),
                await client.call_tool(image, {}, confirmation_handler=_allow_once),
                await client.call_tool(slow, {"delay": 2.0}, confirmation_handler=_allow_once),
            )
            pid = int((tmp_path / "server-a.pid").read_text(encoding="utf-8"))
        return (*results, pid)

    success, failure, long_result, image_result, timeout, pid = anyio.run(scenario)

    assert success.success and success.content == "hello"
    assert success.data == {"result": "hello"}
    assert not failure.success and failure.error_type == "mcp_tool_error"
    assert long_result.success and long_result.metadata["truncated"] is True
    assert long_result.metadata["original_length"] == 5000
    assert len(long_result.content) < 4100
    assert image_result.success
    assert image_result.content == "<image content omitted>"
    assert "RAW_BASE64_MARKER" not in json.dumps(image_result.to_dict())
    assert not timeout.success and timeout.error_type == "mcp_timeout"
    assert _wait_process_stopped(pid)


def test_parent_environment_is_not_inherited_unless_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("PARENT_ENV_SECRET", "PARENT_SECRET_MARKER")
    config = _config(tmp_path, env={"CONFIG_SECRET": "CONFIG_SECRET_MARKER"})
    assert "CONFIG_SECRET_MARKER" not in repr(config)
    assert str(SERVER_SCRIPT) not in repr(config)

    async def scenario():
        async with MCPStdioClient(config) as client:
            tool = await _find_tool(client, "read_environment")
            missing = await client.call_tool(
                tool,
                {"name": "PARENT_ENV_SECRET"},
                confirmation_handler=_allow_once,
            )
        async with MCPStdioClient(
            _config(tmp_path, server_id="server-b", env={"EXPLICIT_ENV": "explicit-value"})
        ) as client:
            tool = await _find_tool(client, "read_environment")
            explicit = await client.call_tool(
                tool,
                {"name": "EXPLICIT_ENV"},
                confirmation_handler=_allow_once,
            )
        return missing, explicit

    missing, explicit = anyio.run(scenario)

    assert missing.content == "<missing>"
    assert explicit.content == "explicit-value"


def test_permission_audit_excludes_arguments_result_stderr_and_env(tmp_path, monkeypatch):
    log_file = tmp_path / "mcp-audit.jsonl"
    logger = JSONLEventLogger(log_file=log_file)

    def log_decision(request, result, **kwargs):
        thread_id = kwargs.pop("thread_id")
        event = build_permission_decision_event(request, result, **kwargs)
        logger.log_event(thread_id, event["event_type"], **event)

    def log_confirmation(request, policy_result, final_result, **kwargs):
        thread_id = kwargs.pop("thread_id")
        event = build_permission_confirmation_event(request, policy_result, final_result, **kwargs)
        logger.log_event(thread_id, event["event_type"], **event)

    monkeypatch.setattr(mcp_permissions, "_permission_decision_audit_logger", log_decision)
    monkeypatch.setattr(mcp_permissions, "_permission_confirmation_audit_logger", log_confirmation)

    async def scenario():
        trace_token = set_current_trace_context(TraceContext(run_id="mcp-client-run"))
        try:
            async with MCPStdioClient(_config(tmp_path, env={"EXPLICIT_SECRET": "ENV_SECRET_MARKER"})) as client:
                echo = await _find_tool(client, "echo")
                result = await client.call_tool(
                    echo,
                    {"text": "MCP_ARGUMENT_SECRET MCP_RESULT_SECRET"},
                    confirmation_handler=_allow_once,
                )
        finally:
            reset_trace_context(trace_token)
        return result

    result = anyio.run(scenario)
    logger.log_queue.join()
    logger.shutdown()
    serialized = log_file.read_text(encoding="utf-8")
    events = [json.loads(line) for line in serialized.splitlines()]

    rendered = "\n".join(format_log_event_for_cli(event) for event in events)
    traced = get_trace_events(events, "mcp-client-run")
    assert result.content == "MCP_ARGUMENT_SECRET MCP_RESULT_SECRET"
    assert [event["run_id"] for event in events] == ["mcp-client-run", "mcp-client-run"]
    assert [event["step_id"] for event in events] == [1, 2]
    assert len(traced) == 2
    for marker in (
        "MCP_ARGUMENT_SECRET",
        "MCP_RESULT_SECRET",
        "MCP_STDERR_SECRET_MARKER",
        "ENV_SECRET_MARKER",
        "inputSchema",
    ):
        assert marker not in serialized
        assert marker not in rendered


def test_list_tools_rejects_cursor_cycle_and_duplicate_identity(monkeypatch):
    schema = {"type": "object", "properties": {}}
    tool = mcp_types.Tool(name="echo", description="", inputSchema=schema)

    class FakeClient:
        def __init__(self, pages):
            self.pages = iter(pages)

        async def list_tools(self, **kwargs):
            return next(self.pages)

    async def cycle_scenario():
        client = MCPStdioClient(MCPStdioServerConfig("server-a", "unused"))
        client._sdk_client = FakeClient(
            [
                mcp_types.ListToolsResult(tools=[tool], nextCursor="again"),
                mcp_types.ListToolsResult(tools=[], nextCursor="again"),
            ]
        )
        with pytest.raises(MCPClientError) as caught:
            await client.list_tools()
        return caught.value

    async def duplicate_scenario():
        client = MCPStdioClient(MCPStdioServerConfig("server-a", "unused"))
        client._sdk_client = FakeClient(
            [
                mcp_types.ListToolsResult(tools=[tool], nextCursor="next"),
                mcp_types.ListToolsResult(tools=[tool]),
            ]
        )
        with pytest.raises(MCPAdapterError) as caught:
            await client.list_tools()
        return caught.value

    async def limit_scenario():
        client = MCPStdioClient(MCPStdioServerConfig("server-a", "unused"))
        client._sdk_client = FakeClient(
            [
                mcp_types.ListToolsResult(tools=[], nextCursor="page-2"),
                mcp_types.ListToolsResult(tools=[], nextCursor="page-3"),
            ]
        )
        with pytest.raises(MCPClientError) as caught:
            await client.list_tools()
        return caught.value

    assert anyio.run(cycle_scenario).code == "mcp_cursor_cycle"
    assert anyio.run(duplicate_scenario).code == "duplicate_tool_identity"
    monkeypatch.setattr(mcp_client, "MAX_TOOL_LIST_PAGES", 2)
    assert anyio.run(limit_scenario).code == "mcp_tool_list_limit"


def test_invalid_arguments_and_unsupported_result_fail_closed_without_call(tmp_path):
    descriptor = MCPToolDescriptor(
        server_id="server-a",
        name="echo",
        description="",
        input_schema={"type": "object", "properties": {}},
    )

    class FakeSession:
        def __init__(self):
            self.calls = 0

        async def call_tool(self, *args, **kwargs):
            self.calls += 1
            return mcp_types.InputRequiredResult(requestState="opaque")

    class FakeClient:
        def __init__(self):
            self.session = FakeSession()

    async def scenario():
        client = MCPStdioClient(MCPStdioServerConfig("server-a", "unused"))
        fake = FakeClient()
        client._sdk_client = fake
        invalid = await client.call_tool(descriptor, "not-a-mapping", confirmation_handler=_allow_once)
        unsupported = await client.call_tool(descriptor, {}, confirmation_handler=_allow_once)
        return invalid, unsupported, fake.session.calls

    invalid, unsupported, calls = anyio.run(scenario)

    assert invalid.error_type == "invalid_mcp_arguments"
    assert unsupported.error_type == "unsupported_mcp_result_type"
    assert calls == 1
