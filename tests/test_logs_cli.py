import json

from typer.testing import CliRunner

from entry.cli import app
from entry import monitor


runner = CliRunner()


def _write_jsonl(path, events):
    path.write_text("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n", encoding="utf-8")


def test_logs_tail_shows_only_last_two_events(tmp_path):
    log_file = tmp_path / "miclaw.jsonl"
    _write_jsonl(
        log_file,
        [
            {"event": "permission_decision", "decision": "allow", "capability": "file_read", "operation": "read", "target": "one.txt"},
            {"event": "permission_decision", "decision": "allow", "capability": "file_read", "operation": "read", "target": "two.txt"},
            {"event": "permission_decision", "decision": "allow", "capability": "file_read", "operation": "read", "target": "three.txt"},
        ],
    )

    result = runner.invoke(app, ["logs", "--tail", "--log-file", str(log_file), "--lines", "2"])

    assert result.exit_code == 0
    assert "one.txt" not in result.output
    assert "two.txt" in result.output
    assert "three.txt" in result.output


def test_logs_tail_missing_log_file_does_not_crash(tmp_path):
    missing = tmp_path / "missing.jsonl"

    result = runner.invoke(app, ["logs", "--tail", "--log-file", str(missing)])

    assert result.exit_code == 0
    assert "No log file found at" in result.output
    assert str(missing) in result.output


def test_logs_tail_malformed_jsonl_line_does_not_crash(tmp_path):
    log_file = tmp_path / "bad.jsonl"
    log_file.write_text('{"event": "llm_input", "message_count": 2}\nnot-json\n', encoding="utf-8")

    result = runner.invoke(app, ["logs", "--tail", "--log-file", str(log_file), "--lines", "2"])

    assert result.exit_code == 0
    assert "LLM INPUT message_count=2" in result.output
    assert "[parse_error] malformed JSONL line skipped" in result.output


def test_logs_tail_permission_decision_renders_safely(tmp_path):
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
                "metadata": {"command_preview": "Authorization: Bearer SECRET_TOKEN"},
            }
        ],
    )

    result = runner.invoke(app, ["logs", "--tail", "--log-file", str(log_file)])

    assert result.exit_code == 0
    assert "PERMISSION ask shell_exec execute" in result.output
    assert "status=blocked_pending_confirmation" in result.output
    assert "SECRET_TOKEN" not in result.output
    assert "Authorization" not in result.output
    assert "Bearer" not in result.output
    assert "command_preview" not in result.output


def test_logs_tail_tool_call_shell_command_args_are_not_rendered_raw(tmp_path):
    log_file = tmp_path / "tool-call.jsonl"
    command = 'curl -H "Authorization: Bearer SECRET_TOKEN" https://example.com'
    _write_jsonl(log_file, [{"event": "tool_call", "tool": "execute_office_shell", "args": {"command": command}}])

    result = runner.invoke(app, ["logs", "--tail", "--log-file", str(log_file)])

    assert result.exit_code == 0
    assert "TOOL CALL execute_office_shell" in result.output
    assert "command_present=true" in result.output
    assert f"command_length={len(command)}" in result.output
    assert command not in result.output
    assert "SECRET_TOKEN" not in result.output
    assert "Authorization" not in result.output
    assert "Bearer" not in result.output


def test_logs_tail_tool_call_does_not_dump_full_args_json(tmp_path):
    log_file = tmp_path / "args.jsonl"
    _write_jsonl(log_file, [{"event": "tool_call", "tool": "execute_office_shell", "args": {"command": "echo hello"}}])

    result = runner.invoke(app, ["logs", "--tail", "--log-file", str(log_file)])

    assert result.exit_code == 0
    assert '"command"' not in result.output
    assert "echo hello" not in result.output
    assert "{" not in result.output
    assert "}" not in result.output


def test_logs_tail_displays_run_id_and_step_id(tmp_path):
    log_file = tmp_path / "trace.jsonl"
    _write_jsonl(log_file, [{"event": "llm_input", "message_count": 1, "run_id": "abcdef123456", "step_id": 3}])

    result = runner.invoke(app, ["logs", "--tail", "--log-file", str(log_file)])

    assert result.exit_code == 0
    assert "[run=abcdef12 step=3] LLM INPUT message_count=1" in result.output


def test_logs_tail_old_event_without_trace_fields_still_renders(tmp_path):
    log_file = tmp_path / "old.jsonl"
    _write_jsonl(log_file, [{"event": "llm_input", "message_count": 4}])

    result = runner.invoke(app, ["logs", "--tail", "--log-file", str(log_file)])

    assert result.exit_code == 0
    assert "LLM INPUT message_count=4" in result.output
    assert "run=" not in result.output


def test_logs_tail_log_file_override_is_respected(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    _write_jsonl(first, [{"event": "permission_decision", "decision": "allow", "capability": "file_read", "target": "first.txt"}])
    _write_jsonl(second, [{"event": "permission_decision", "decision": "allow", "capability": "file_read", "target": "second.txt"}])

    result = runner.invoke(app, ["logs", "--tail", "--log-file", str(second)])

    assert result.exit_code == 0
    assert "second.txt" in result.output
    assert "first.txt" not in result.output


def test_logs_tail_default_lines_is_twenty(tmp_path):
    log_file = tmp_path / "default-lines.jsonl"
    events = [
        {"event": "permission_decision", "decision": "allow", "capability": "file_read", "target": f"note-{index}.txt"}
        for index in range(1, 22)
    ]
    _write_jsonl(log_file, events)

    result = runner.invoke(app, ["logs", "--tail", "--log-file", str(log_file)])

    assert result.exit_code == 0
    assert "note-1.txt" not in result.output
    assert "note-2.txt" in result.output
    assert "note-21.txt" in result.output


def test_tail_log_events_returns_last_events_as_dicts(tmp_path):
    log_file = tmp_path / "direct.jsonl"
    _write_jsonl(log_file, [{"event": "future", "value": 1}, {"event": "future", "value": 2}])

    events = monitor.tail_log_events(log_file, lines=1)

    assert events == [{"event": "future", "value": 2}]


def test_logs_tail_help_is_available():
    result = runner.invoke(app, ["logs", "--tail", "--help"])

    assert result.exit_code == 0
    assert "--lines" in result.output
    assert "--log-file" in result.output
