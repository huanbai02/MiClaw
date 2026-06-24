import json

from miclaw.core.permissions import PermissionDecision
from miclaw.core.tools.result import (
    format_tool_result_for_model,
    tool_error,
    tool_permission_blocked,
    tool_success,
)


def test_success_result_serializes_to_json_friendly_dict():
    result = tool_success(
        "ok",
        data={"count": 1},
        metadata={"decision": PermissionDecision.ALLOW},
    )

    serialized = result.to_dict()

    assert serialized == {
        "success": True,
        "content": "ok",
        "data": {"count": 1},
        "error_type": None,
        "error_message": None,
        "metadata": {"decision": "allow"},
    }
    json.dumps(serialized)


def test_error_result_serializes_with_error_fields():
    result = tool_error("file_not_found", "missing", metadata={"tool_name": "read_office_file"})

    serialized = result.to_dict()

    assert serialized["success"] is False
    assert serialized["error_type"] == "file_not_found"
    assert serialized["error_message"] == "missing"
    assert serialized["content"] == "missing"
    assert serialized["metadata"] == {"tool_name": "read_office_file"}
    json.dumps(serialized)


def test_permission_blocked_result_includes_permission_error_type():
    result = tool_permission_blocked("Permission required: review", decision="ask")

    serialized = result.to_dict()

    assert serialized["success"] is False
    assert serialized["error_type"] == "permission_required"
    assert serialized["error_message"] == "Permission required: review"
    assert serialized["metadata"] == {"permission_decision": "ask"}


def test_format_tool_result_for_model_returns_content_for_success():
    assert format_tool_result_for_model(tool_success("hello")) == "hello"


def test_format_tool_result_for_model_returns_clear_error_text_for_failure():
    result = tool_error("invalid_mode", "mode is invalid", content="❌ mode is invalid")

    assert format_tool_result_for_model(result) == "❌ mode is invalid"
