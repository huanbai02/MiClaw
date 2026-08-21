import json
from io import StringIO

import pytest
from rich.console import Console

from entry import monitor


def _render_text(monkeypatch, event):
    output = StringIO()
    monkeypatch.setattr(
        monitor,
        "console",
        Console(file=output, force_terminal=False, color_system=None, width=120),
    )
    monitor.render_event(event)
    return output.getvalue()


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


@pytest.mark.parametrize(
    "result_summary",
    [
        "Authorization: Bearer SECRET_TOKEN",
        "api_key=SECRET_API_KEY password=SECRET_PASSWORD",
        "PRIVATE_FILE_CONTENT",
    ],
)
def test_legacy_tool_result_is_summarized_without_raw_content(result_summary):
    text = monitor.format_tool_result_event(
        {
            "event": "tool_result",
            "tool": "legacy_tool",
            "result_summary": result_summary,
        }
    )

    assert "<content omitted>" in text
    assert f"length={len(result_summary)}" in text
    assert result_summary not in text
    assert "SECRET_TOKEN" not in text
    assert "SECRET_API_KEY" not in text
    assert "SECRET_PASSWORD" not in text
    assert "PRIVATE_FILE_CONTENT" not in text


def test_legacy_tool_result_rendering_is_bounded_and_safe(monkeypatch):
    raw_result = "PRIVATE_FILE_CONTENT" * 10_000

    text = _render_text(
        monkeypatch,
        {
            "event": "tool_result",
            "tool": "legacy_tool",
            "result_summary": raw_result,
            "run_id": "abcdef123456",
            "step_id": 7,
        },
    )

    assert "PRIVATE_FILE_CONTENT" not in text
    assert "<content omitted>" in text
    assert "[run=abcdef12 step=7]" in text
    assert len(text) < 1_000


def test_legacy_ai_message_content_is_summarized(monkeypatch):
    content = "Authorization: Bearer SECRET_TOKEN"

    formatted = monitor.format_ai_message_event({"event": "ai_message", "content": content})
    rendered = _render_text(monkeypatch, {"event": "ai_message", "content": content})

    assert "<content omitted>" in formatted
    assert "<content omitted>" in rendered
    assert "SECRET_TOKEN" not in formatted
    assert "SECRET_TOKEN" not in rendered
    assert "Authorization" not in rendered
    assert "Bearer" not in rendered


def test_long_ai_message_content_is_bounded():
    content = "model output " * 10_000

    text = monitor.format_ai_message_event({"event": "ai_message", "content": content})

    assert content not in text
    assert f"length={len(content)}" in text
    assert len(text) < 200


@pytest.mark.parametrize(
    "args",
    [
        {
            "command": "SECRET_COMMAND",
            "command_present": "RAW_COLLISION_MARKER",
            "command_length": "RAW_COLLISION_MARKER",
            "queue_length": 5,
            "response_length": 89,
        },
        {
            "command_length": "RAW_COLLISION_MARKER",
            "command_present": "RAW_COLLISION_MARKER",
            "command": "SECRET_COMMAND",
            "queue_length": 5,
            "response_length": 89,
        },
    ],
)
def test_tool_call_uses_collision_safe_redaction_and_preserves_safe_metadata(args):
    text = monitor.format_tool_call_event({"event": "tool_call", "tool": "shell", "args": args})

    assert "command_present=true" in text
    assert f"command_length={len('SECRET_COMMAND')}" in text
    assert "queue_length=5" in text
    assert "response_length=89" in text
    assert "SECRET_COMMAND" not in text
    assert "RAW_COLLISION_MARKER" not in text


def test_unknown_event_does_not_dump_nested_payload_and_is_bounded(monkeypatch):
    event = {
        "event": "future_event",
        "payload": {"openai_api_key": "SECRET_TOKEN", "content": "PRIVATE_FILE_CONTENT" * 1_000},
    }

    cli_text = monitor.format_log_event_for_cli(event)
    rendered = _render_text(monkeypatch, event)

    assert cli_text == "EVENT future_event"
    assert "SECRET_TOKEN" not in rendered
    assert "PRIVATE_FILE_CONTENT" not in rendered
    assert len(rendered) < 500


def test_malformed_event_object_does_not_crash(monkeypatch):
    text = _render_text(monkeypatch, ["Authorization: Bearer SECRET_TOKEN"])

    assert "malformed JSONL line" in text
    assert "SECRET_TOKEN" not in text


def test_rich_markup_like_tool_name_is_rendered_literally(monkeypatch):
    tool_name = "[bold red]INJECTED[/bold red]"

    text = _render_text(monkeypatch, {"event": "tool_call", "tool": tool_name, "args": {}})

    assert tool_name in text


def test_unknown_event_fallback_still_works():
    event = monitor.parse_event_line(json.dumps({"event": "future_event", "value": 1}))

    assert event["event"] == "future_event"
    monitor.render_event(event)


def test_malformed_jsonl_handling_still_works():
    event = monitor.parse_event_line("not-json")

    assert event["event"] == "parse_error"
    monitor.render_event(event)


def test_trace_prefix_renders_short_run_and_step():
    text = monitor.format_trace_prefix({"run_id": "abcdef123456", "step_id": 3})

    assert text == "[run=abcdef12 step=3] "


def test_trace_prefix_omits_missing_trace_fields_for_old_events():
    assert monitor.format_trace_prefix({"event": "system_action"}) == ""


def test_render_event_tolerates_trace_fields_on_unknown_event():
    monitor.render_event({"event": "future_event", "run_id": "abcdef123456", "step_id": 9})


def test_trace_prefix_sanitizes_markup_like_values():
    text = monitor.format_trace_prefix({"run_id": "[red]abcdef[/red]", "step_id": "[bold]9[/bold]"})

    assert "[red]" not in text
    assert "[/red]" not in text
    assert "[bold]" not in text
    assert "[/bold]" not in text
    assert text.startswith("[run=redabcde step=bold9bold] ")


def test_trace_prefix_for_markup_renders_literal_brackets():
    from rich.text import Text

    markup = monitor.format_trace_prefix_for_markup({"run_id": "abcdef123456", "step_id": 3})
    plain = Text.from_markup(markup).plain

    assert plain == "[run=abcdef12 step=3] "
