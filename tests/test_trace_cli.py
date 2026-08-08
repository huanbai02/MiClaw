import json

from typer.testing import CliRunner

from entry.cli import app
from entry import monitor


runner = CliRunner()


def _write_jsonl(path, events_or_lines):
    lines = []
    for item in events_or_lines:
        if isinstance(item, str):
            lines.append(item)
        else:
            lines.append(json.dumps(item, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_trace_displays_only_events_with_requested_run_id(tmp_path):
    log_file = tmp_path / "trace.jsonl"
    _write_jsonl(
        log_file,
        [
            {"event": "llm_input", "message_count": 1, "run_id": "run-a", "step_id": 1},
            {"event": "llm_input", "message_count": 2, "run_id": "run-b", "step_id": 1},
            {"event": "permission_decision", "decision": "allow", "capability": "file_read", "target": "a.txt", "run_id": "run-a", "step_id": 2},
        ],
    )

    result = runner.invoke(app, ["trace", "run-a", "--log-file", str(log_file)])

    assert result.exit_code == 0
    assert "Trace run=run-a" in result.output
    assert "message_count=1" in result.output
    assert "a.txt" in result.output
    assert "message_count=2" not in result.output
    assert "run-b" not in result.output


def test_trace_orders_numeric_step_id(tmp_path):
    log_file = tmp_path / "ordered.jsonl"
    _write_jsonl(
        log_file,
        [
            {"event": "permission_decision", "decision": "allow", "capability": "file_read", "target": "third.txt", "run_id": "run-a", "step_id": 3},
            {"event": "permission_decision", "decision": "allow", "capability": "file_read", "target": "first.txt", "run_id": "run-a", "step_id": 1},
            {"event": "permission_decision", "decision": "allow", "capability": "file_read", "target": "second.txt", "run_id": "run-a", "step_id": "2"},
        ],
    )

    result = runner.invoke(app, ["trace", "run-a", "--log-file", str(log_file)])

    assert result.exit_code == 0
    assert result.output.index("first.txt") < result.output.index("second.txt") < result.output.index("third.txt")


def test_trace_events_without_step_id_do_not_crash(tmp_path):
    log_file = tmp_path / "missing-step.jsonl"
    _write_jsonl(
        log_file,
        [
            {"event": "llm_input", "message_count": 1, "run_id": "run-a"},
            {"event": "permission_decision", "decision": "allow", "capability": "file_read", "target": "has-step.txt", "run_id": "run-a", "step_id": 1},
        ],
    )

    result = runner.invoke(app, ["trace", "run-a", "--log-file", str(log_file)])

    assert result.exit_code == 0
    assert "has-step.txt" in result.output
    assert "LLM INPUT message_count=1" in result.output


def test_trace_missing_log_file_does_not_crash(tmp_path):
    missing = tmp_path / "missing.jsonl"

    result = runner.invoke(app, ["trace", "run-a", "--log-file", str(missing)])

    assert result.exit_code == 0
    assert "No log file found at" in result.output
    assert str(missing) in result.output


def test_trace_no_matching_run_id_produces_clear_message(tmp_path):
    log_file = tmp_path / "no-match.jsonl"
    _write_jsonl(log_file, [{"event": "llm_input", "message_count": 1, "run_id": "other"}])

    result = runner.invoke(app, ["trace", "run-a", "--log-file", str(log_file)])

    assert result.exit_code == 0
    assert "No events found for run_id run-a" in result.output


def test_trace_malformed_jsonl_line_does_not_crash(tmp_path):
    log_file = tmp_path / "bad.jsonl"
    _write_jsonl(log_file, ['{"event": "llm_input", "message_count": 1, "run_id": "run-a", "step_id": 1}', "not-json"])

    result = runner.invoke(app, ["trace", "run-a", "--log-file", str(log_file)])

    assert result.exit_code == 0
    assert "LLM INPUT message_count=1" in result.output


def test_trace_tool_call_shell_command_args_are_not_rendered_raw(tmp_path):
    log_file = tmp_path / "tool-call.jsonl"
    command = 'curl -H "Authorization: Bearer SECRET_TOKEN" https://example.com'
    _write_jsonl(log_file, [{"event": "tool_call", "tool": "execute_office_shell", "args": {"command": command}, "run_id": "run-a", "step_id": 1}])

    result = runner.invoke(app, ["trace", "run-a", "--log-file", str(log_file)])

    assert result.exit_code == 0
    assert "TOOL CALL execute_office_shell" in result.output
    assert "command_present=true" in result.output
    assert f"command_length={len(command)}" in result.output
    assert command not in result.output
    assert "SECRET_TOKEN" not in result.output
    assert "Authorization" not in result.output
    assert "Bearer" not in result.output


def test_trace_permission_decision_renders_safely(tmp_path):
    log_file = tmp_path / "permission.jsonl"
    _write_jsonl(
        log_file,
        [
            {
                "event": "permission_decision",
                "tool_name": "execute_office_shell",
                "capability": "shell_exec",
                "operation": "execute",
                "decision": "ask",
                "risk_level": "medium",
                "requires_confirmation": True,
                "run_id": "run-a",
                "step_id": 2,
                "metadata": {"command_preview": "Authorization: Bearer SECRET_TOKEN"},
            }
        ],
    )

    result = runner.invoke(app, ["trace", "run-a", "--log-file", str(log_file)])

    assert result.exit_code == 0
    assert "PERMISSION ask shell_exec execute" in result.output
    assert "status=blocked_pending_confirmation" in result.output
    assert "SECRET_TOKEN" not in result.output
    assert "Authorization" not in result.output
    assert "Bearer" not in result.output
    assert "command_preview" not in result.output


def test_trace_help_is_available():
    result = runner.invoke(app, ["trace", "--help"])

    assert result.exit_code == 0
    assert "run_id" in result.output
    assert "--log-file" in result.output


def test_get_trace_events_filters_and_sorts_stably():
    events = [
        {"event": "future", "run_id": "1", "step_id": "bad", "value": "no-step-a"},
        {"event": "future", "run_id": "1", "step_id": 2, "value": "two"},
        {"event": "future", "run_id": "2", "step_id": 1, "value": "other"},
        {"event": "future", "run_id": "1", "step_id": 1, "value": "one"},
        {"event": "future", "run_id": "1", "value": "no-step-b"},
    ]

    result = monitor.get_trace_events(events, "1")

    assert [event["value"] for event in result] == ["one", "two", "no-step-a", "no-step-b"]


def test_trace_ignores_events_without_run_id_even_when_requested_text_is_none(tmp_path):
    log_file = tmp_path / "missing-run-id.jsonl"
    _write_jsonl(log_file, [{"event": "llm_input", "message_count": 1}])

    result = runner.invoke(app, ["trace", "None", "--log-file", str(log_file)])

    assert result.exit_code == 0
    assert "No events found for run_id None" in result.output
    assert "LLM INPUT" not in result.output
