import json

from miclaw.core.permissions import (
    PermissionCapability,
    PermissionDecision,
    PermissionRequest,
    PermissionResult,
    RiskLevel,
    allow,
    ask,
    deny,
    evaluate_permission,
)


def request_for(capability, risk_level=RiskLevel.LOW):
    return PermissionRequest(
        capability=capability,
        operation="unit_test",
        target="workspace/office/example.txt",
        arguments={"path": "example.txt"},
        reason="test request",
        risk_level=risk_level,
        metadata={"source": "test"},
    )


def test_file_read_low_risk_returns_allow():
    result = evaluate_permission(request_for(PermissionCapability.FILE_READ))

    assert result.decision is PermissionDecision.ALLOW
    assert result.requires_confirmation is False


def test_file_write_low_risk_returns_allow():
    result = evaluate_permission(request_for(PermissionCapability.FILE_WRITE))

    assert result.decision is PermissionDecision.ALLOW
    assert result.requires_confirmation is False


def test_shell_exec_returns_ask_by_default():
    result = evaluate_permission(request_for(PermissionCapability.SHELL_EXEC))

    assert result.decision is PermissionDecision.ASK
    assert result.requires_confirmation is True


def test_network_access_returns_deny_by_default():
    result = evaluate_permission(request_for(PermissionCapability.NETWORK_ACCESS))

    assert result.decision is PermissionDecision.DENY
    assert result.requires_confirmation is False


def test_mcp_tool_returns_deny_by_default():
    result = evaluate_permission(request_for(PermissionCapability.MCP_TOOL))

    assert result.decision is PermissionDecision.DENY
    assert result.requires_confirmation is False


def test_memory_write_returns_ask_by_default():
    result = evaluate_permission(request_for(PermissionCapability.MEMORY_WRITE))

    assert result.decision is PermissionDecision.ASK
    assert result.requires_confirmation is True


def test_unknown_returns_deny():
    result = evaluate_permission(request_for(PermissionCapability.UNKNOWN))

    assert result.decision is PermissionDecision.DENY
    assert result.requires_confirmation is False
    assert "Unknown capability" in result.reason


def test_ask_result_requires_confirmation_true():
    result = ask("needs user confirmation")

    assert result.decision is PermissionDecision.ASK
    assert result.requires_confirmation is True


def test_allow_and_deny_results_require_confirmation_false():
    assert allow("safe").requires_confirmation is False
    assert deny("blocked").requires_confirmation is False


def test_permission_request_serialization_returns_json_friendly_strings():
    request = request_for(PermissionCapability.FILE_READ)
    serialized = request.to_dict()

    assert serialized == {
        "capability": "file_read",
        "operation": "unit_test",
        "target": "workspace/office/example.txt",
        "arguments": {"path": "example.txt"},
        "reason": "test request",
        "risk_level": "low",
        "metadata": {"source": "test"},
    }
    json.dumps(serialized)


def test_permission_result_serialization_returns_json_friendly_strings():
    result = ask("shell needs approval", RiskLevel.MEDIUM, policy="default")
    serialized = result.to_dict()

    assert serialized == {
        "decision": "ask",
        "reason": "shell needs approval",
        "risk_level": "medium",
        "requires_confirmation": True,
        "metadata": {"policy": "default"},
    }
    json.dumps(serialized)


def test_unknown_or_invalid_capability_input_is_safe_and_does_not_fail_open():
    request = request_for("future_capability")
    result = evaluate_permission(request)

    assert request.capability is PermissionCapability.UNKNOWN
    assert result.decision is PermissionDecision.DENY
    assert result.requires_confirmation is False


def test_invalid_risk_level_does_not_fail_open():
    request = request_for(PermissionCapability.FILE_READ, risk_level="not_a_risk")
    result = evaluate_permission(request)

    assert request.risk_level is RiskLevel.CRITICAL
    assert result.decision is PermissionDecision.ASK
    assert result.requires_confirmation is True


def test_invalid_result_decision_defaults_to_deny():
    result = PermissionResult("future_decision", "invalid decision should not allow")

    assert result.decision is PermissionDecision.DENY
    assert result.requires_confirmation is False
