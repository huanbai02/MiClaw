import json
import socket
import subprocess

import pytest

import entry.cli as cli
import miclaw.core.mcp_permissions as mcp_permissions
from miclaw.core.logger import (
    JSONLEventLogger,
    build_permission_confirmation_event,
    build_permission_decision_event,
)
from miclaw.core.mcp_adapter import MCPToolDescriptor
from miclaw.core.mcp_permissions import authorize_mcp_tool, build_mcp_permission_request
from miclaw.core.permissions import (
    PermissionCapability,
    PermissionConfirmationChoice,
    PermissionDecision,
    PermissionRequest,
    RiskLevel,
    evaluate_permission,
    get_session_permission_grants,
    reset_session_permission_grants,
    resolve_permission,
    set_session_permission_grants,
)
from miclaw.core.trace import TraceContext, reset_trace_context, set_current_trace_context


def _descriptor(server_id="server-a", name="search", *, secret_description=False):
    description = (
        "Authorization: Bearer SECRET_TOKEN PRIVATE_CONTENT_MARKER"
        if secret_description
        else "Search documents"
    )
    return MCPToolDescriptor(
        server_id=server_id,
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "openai_api_key": {"type": "string", "description": "PRIVATE_SCHEMA_MARKER"},
            },
        },
    )


@pytest.fixture(autouse=True)
def disable_global_permission_audit(monkeypatch):
    monkeypatch.setattr(mcp_permissions, "_permission_decision_audit_logger", lambda *args, **kwargs: None)
    monkeypatch.setattr(mcp_permissions, "_permission_confirmation_audit_logger", lambda *args, **kwargs: None)


def test_build_request_preserves_qualified_identity_without_arguments_or_schema():
    request = build_mcp_permission_request(_descriptor())

    assert request.capability is PermissionCapability.MCP_TOOL
    assert request.operation == "invoke"
    assert request.target == "mcp::server-a::search"
    assert request.risk_level is RiskLevel.HIGH
    assert request.arguments == {}
    assert request.metadata == {
        "tool_name": "mcp::server-a::search",
        "mcp_server_id": "server-a",
        "mcp_tool_name": "search",
    }
    serialized = json.dumps(request.to_dict())
    assert "input_schema" not in serialized
    assert "Search documents" not in serialized


def test_valid_mcp_invoke_policy_is_ask_by_default():
    result = evaluate_permission(build_mcp_permission_request(_descriptor()))

    assert result.decision is PermissionDecision.ASK
    assert result.risk_level is RiskLevel.HIGH
    assert result.requires_confirmation is True


def test_ask_without_handler_remains_blocked():
    result = authorize_mcp_tool(_descriptor())

    assert result.decision is PermissionDecision.ASK


def test_allow_once_confirmation_returns_allow():
    result = authorize_mcp_tool(
        _descriptor(),
        confirmation_handler=lambda request, policy: PermissionConfirmationChoice.ALLOW_ONCE,
    )

    assert result.decision is PermissionDecision.ALLOW


def test_confirmation_deny_returns_deny():
    result = authorize_mcp_tool(
        _descriptor(),
        confirmation_handler=lambda request, policy: PermissionConfirmationChoice.DENY,
    )

    assert result.decision is PermissionDecision.DENY


@pytest.mark.parametrize(
    "handler",
    [
        lambda request, policy: None,
        lambda request, policy: PermissionDecision.ASK,
        lambda request, policy: "allow",
    ],
)
def test_invalid_confirmation_results_fail_closed(handler):
    assert authorize_mcp_tool(_descriptor(), confirmation_handler=handler).decision is PermissionDecision.DENY


def test_confirmation_exception_fails_closed():
    def fail_confirmation(request, policy):
        raise RuntimeError("SECRET_TOKEN")

    result = authorize_mcp_tool(_descriptor(), confirmation_handler=fail_confirmation)

    assert result.decision is PermissionDecision.DENY


def test_policy_denies_unsupported_operation_and_invalid_identity_without_prompt():
    requests = [
        PermissionRequest(
            capability=PermissionCapability.MCP_TOOL,
            operation="execute",
            target="mcp::server-a::search",
            risk_level=RiskLevel.HIGH,
        ),
        PermissionRequest(
            capability=PermissionCapability.MCP_TOOL,
            operation="invoke",
            target="search",
            risk_level=RiskLevel.HIGH,
        ),
    ]

    for request in requests:
        policy = evaluate_permission(request)
        final = resolve_permission(
            request,
            policy,
            lambda *args: pytest.fail("DENY must not prompt"),
        )
        assert policy.decision is PermissionDecision.DENY
        assert final is policy


def test_malformed_descriptor_fails_closed_without_traceback():
    result = authorize_mcp_tool({"server_id": "server-a", "name": "search"})

    assert result.decision is PermissionDecision.DENY
    assert result.risk_level is RiskLevel.HIGH


def test_same_tool_different_server_and_same_server_different_tool_have_distinct_identity():
    requests = [
        build_mcp_permission_request(_descriptor("server-a", "search")),
        build_mcp_permission_request(_descriptor("server-b", "search")),
        build_mcp_permission_request(_descriptor("server-a", "delete")),
    ]

    assert [request.target for request in requests] == [
        "mcp::server-a::search",
        "mcp::server-b::search",
        "mcp::server-a::delete",
    ]
    assert len({request.target for request in requests}) == 3


def test_session_grant_isolated_by_server_and_tool():
    token = set_session_permission_grants()
    try:
        first = authorize_mcp_tool(
            _descriptor("server-a", "search"),
            confirmation_handler=lambda request, policy: PermissionConfirmationChoice.ALLOW_SESSION,
        )
        reused = authorize_mcp_tool(_descriptor("server-a", "search"))
        other_server = authorize_mcp_tool(_descriptor("server-b", "search"))
        other_tool = authorize_mcp_tool(_descriptor("server-a", "delete"))

        assert first.decision is PermissionDecision.ALLOW
        assert reused.decision is PermissionDecision.ALLOW
        assert reused.metadata["confirmation_source"] == "session_grant"
        assert other_server.decision is PermissionDecision.ASK
        assert other_tool.decision is PermissionDecision.ASK
        assert len(get_session_permission_grants()) == 1
    finally:
        reset_session_permission_grants(token)


def test_confirmation_prompt_uses_only_safe_mcp_identity_fields():
    request = build_mcp_permission_request(_descriptor(secret_description=True))
    prompt = cli.format_permission_confirmation_prompt(
        request,
        evaluate_permission(request),
    )

    assert "mcp::server-a::search" in prompt
    assert "mcp:server-a" in prompt
    assert "mcp_tool" in prompt
    assert "invoke" in prompt
    assert "high" in prompt
    for marker in (
        "SECRET_TOKEN",
        "PRIVATE_CONTENT_MARKER",
        "PRIVATE_SCHEMA_MARKER",
        "openai_api_key",
        "input_schema",
        "Authorization",
        "Bearer",
    ):
        assert marker not in prompt


def test_permission_audit_is_safe_and_preserves_trace(tmp_path, monkeypatch):
    log_file = tmp_path / "mcp-permission.jsonl"
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
    trace_token = set_current_trace_context(TraceContext(run_id="mcp-run"))
    try:
        result = authorize_mcp_tool(
            _descriptor(secret_description=True),
            confirmation_handler=lambda request, policy: PermissionConfirmationChoice.ALLOW_ONCE,
        )
    finally:
        reset_trace_context(trace_token)
    logger.log_queue.join()
    logger.shutdown()

    events = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    serialized = json.dumps(events)
    assert result.decision is PermissionDecision.ALLOW
    assert [event["event_type"] for event in events] == [
        "permission_decision",
        "permission_confirmation",
    ]
    assert [event["run_id"] for event in events] == ["mcp-run", "mcp-run"]
    assert [event["step_id"] for event in events] == [1, 2]
    assert all(event["target"] == "mcp::server-a::search" for event in events)
    for marker in (
        "SECRET_TOKEN",
        "PRIVATE_CONTENT_MARKER",
        "PRIVATE_SCHEMA_MARKER",
        "openai_api_key",
        "input_schema",
        "Authorization",
        "Bearer",
    ):
        assert marker not in serialized


def test_final_allow_has_no_process_network_or_workspace_side_effect(tmp_path, monkeypatch):
    marker = tmp_path / "must-not-exist"
    process_calls = []
    network_calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: process_calls.append((args, kwargs)))
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: network_calls.append((args, kwargs)))

    result = authorize_mcp_tool(
        _descriptor(),
        confirmation_handler=lambda request, policy: PermissionConfirmationChoice.ALLOW_ONCE,
    )

    assert result.decision is PermissionDecision.ALLOW
    assert process_calls == []
    assert network_calls == []
    assert not marker.exists()
