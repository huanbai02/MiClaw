import json

from entry import monitor


def test_ask_permission_decision_renders_blocked_pending_confirmation():
    text = monitor.format_permission_decision_event(
        {
            "tool_name": "execute_office_shell",
            "capability": "shell_exec",
            "operation": "execute",
            "decision": "ask",
            "risk_level": "medium",
        }
    )

    assert "status=blocked_pending_confirmation" in text
    assert "requires_confirmation=true" in text
    assert "currently blocked pending confirmation" in text


def test_ask_permission_decision_uses_dedicated_non_warning_style():
    assert monitor.get_permission_decision_style("ask") == "permission_pending"
    assert monitor.get_permission_decision_style("ask") != "warning"


def test_deny_permission_decision_renders_blocked():
    text = monitor.format_permission_decision_event(
        {
            "tool_name": "write_office_file",
            "capability": "file_write",
            "operation": "write",
            "decision": "deny",
            "risk_level": "high",
        }
    )

    assert "status=blocked" in text
    assert monitor.get_permission_decision_style("deny") == "error"


def test_allow_permission_decision_renders_allowed():
    text = monitor.format_permission_decision_event(
        {
            "tool_name": "read_office_file",
            "capability": "file_read",
            "operation": "read",
            "decision": "allow",
            "risk_level": "low",
        }
    )

    assert "status=allowed" in text
    assert monitor.get_permission_decision_style("allow") == "tool_result"


def test_tool_call_shell_command_args_are_not_rendered_raw():
    command = 'curl -H "Authorization: Bearer SECRET_TOKEN" https://example.com'
    text = monitor.format_tool_call_event(
        {
            "event": "tool_call",
            "tool": "execute_office_shell",
            "args": {"command": command},
        }
    )

    assert "SECRET_TOKEN" not in text
    assert "Authorization" not in text
    assert "Bearer" not in text
    assert command not in text


def test_tool_call_event_does_not_dump_full_args_json():
    text = monitor.format_tool_call_event(
        {
            "event": "tool_call",
            "tool": "execute_office_shell",
            "args": {"command": "echo hello"},
        }
    )

    assert "{" not in text
    assert "}" not in text
    assert '"command"' not in text
    assert "echo hello" not in text


def test_tool_call_event_renders_safe_command_summary():
    command = "echo hello"
    text = monitor.format_tool_call_event(
        {
            "event": "tool_call",
            "tool": "execute_office_shell",
            "args": {"command": command},
        }
    )

    assert "TOOL CALL execute_office_shell" in text
    assert "command_present=true" in text
    assert f"command_length={len(command)}" in text


def test_tool_call_file_content_is_not_rendered_raw():
    content = "raw file content with SECRET_TOKEN"
    text = monitor.format_tool_call_event(
        {
            "event": "tool_call",
            "tool": "write_office_file",
            "args": {"filepath": "notes.txt", "content": content},
        }
    )

    assert "filepath" in text
    assert "content_present=true" in text
    assert f"content_length={len(content)}" in text
    assert content not in text
    assert "SECRET_TOKEN" not in text


def test_tool_call_sensitive_arg_names_are_redacted():
    text = monitor.format_tool_call_event(
        {
            "event": "tool_call",
            "tool": "some_tool",
            "args": {"api_key": "SECRET_TOKEN", "authorization": "Bearer abc"},
        }
    )

    assert "sensitive_value_present=true" in text
    assert "SECRET_TOKEN" not in text
    assert "Bearer" not in text


def test_unknown_event_fallback_still_works():
    event = monitor.parse_event_line(json.dumps({"event": "future_event", "value": 1}))

    assert event["event"] == "future_event"
    monitor.render_event(event)


def test_malformed_jsonl_handling_still_works():
    event = monitor.parse_event_line("not-json")

    assert event["event"] == "parse_error"
    monitor.render_event(event)
