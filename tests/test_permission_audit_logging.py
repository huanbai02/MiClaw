import json
import os
import sys
from enum import Enum
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import miclaw.core.tools.sandbox_tools as sandbox_tools
from miclaw.core.logger import build_permission_decision_event
from miclaw.core.permissions import PermissionDecision, allow, ask, deny, evaluate_permission
from miclaw.core.tools.sandbox_tools import execute_office_shell, list_office_files, read_office_file, write_office_file


@pytest.fixture()
def office(tmp_path, monkeypatch):
    office_dir = tmp_path / "workspace" / "office"
    office_dir.mkdir(parents=True)
    monkeypatch.setattr(sandbox_tools, "OFFICE_DIR", str(office_dir))
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", evaluate_permission)
    return office_dir


@pytest.fixture()
def audit_events(monkeypatch):
    events = []

    def capture_event(request, result, *, tool_name=None, metadata=None, thread_id="system"):
        events.append(build_permission_decision_event(request, result, tool_name=tool_name, metadata=metadata))

    monkeypatch.setattr(sandbox_tools, "_permission_audit_logger", capture_event)
    return events


def deny_all_permissions(request):
    return deny("test policy denies", request.risk_level)


def ask_all_permissions(request):
    return ask("test policy asks", request.risk_level)


def allow_all_permissions(request):
    return allow("test policy allows", request.risk_level)


def test_file_read_allow_logs_one_permission_decision_event(office, audit_events):
    (office / "notes.txt").write_text("hello", encoding="utf-8")

    result = read_office_file.invoke({"filepath": "notes.txt"})

    assert result == "hello"
    assert len(audit_events) == 1
    event = audit_events[0]
    assert event["event_type"] == "permission_decision"
    assert event["tool_name"] == "read_office_file"
    assert event["capability"] == "file_read"
    assert event["operation"] == "read"
    assert event["target"] == "notes.txt"
    assert event["decision"] == "allow"
    assert event["risk_level"] == "low"
    assert event["requires_confirmation"] is False


def test_file_write_allow_logs_one_permission_decision_event(office, audit_events):
    result = write_office_file.invoke({"filepath": "notes.txt", "content": "hello", "mode": "w"})

    assert "成功以 覆盖/新建 模式写入文件" in result
    assert len(audit_events) == 1
    event = audit_events[0]
    assert event["tool_name"] == "write_office_file"
    assert event["capability"] == "file_write"
    assert event["operation"] == "write"
    assert event["target"] == "notes.txt"
    assert event["decision"] == "allow"


def test_file_list_allow_logs_one_permission_decision_event(office, audit_events):
    (office / "notes.txt").write_text("hello", encoding="utf-8")

    result = list_office_files.invoke({"sub_dir": ""})

    assert "📄 notes.txt" in result
    assert len(audit_events) == 1
    event = audit_events[0]
    assert event["event_type"] == "permission_decision"
    assert event["tool_name"] == "list_office_files"
    assert event["capability"] == "file_read"
    assert event["operation"] == "list"
    assert event["target"] == "."
    assert event["decision"] == "allow"
    assert isinstance(event["error_type"], str)
    assert event["error_type"] == ""


def test_permission_deny_logs_event_and_does_not_execute(office, audit_events, monkeypatch):
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", deny_all_permissions)

    result = write_office_file.invoke({"filepath": "blocked.txt", "content": "blocked", "mode": "w"})

    assert "Permission denied" in result
    assert not (office / "blocked.txt").exists()
    assert len(audit_events) == 1
    event = audit_events[0]
    assert event["decision"] == "deny"
    assert event["error_type"] == "permission_denied"
    assert event["requires_confirmation"] is False


def test_permission_ask_logs_event_and_does_not_execute(office, audit_events, monkeypatch):
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", ask_all_permissions)

    result = write_office_file.invoke({"filepath": "blocked.txt", "content": "blocked", "mode": "w"})

    assert "Permission required" in result
    assert not (office / "blocked.txt").exists()
    assert len(audit_events) == 1
    event = audit_events[0]
    assert event["decision"] == "ask"
    assert event["error_type"] == "permission_required"
    assert event["requires_confirmation"] is True


@patch("miclaw.core.tools.sandbox_tools.subprocess.run")
def test_shell_default_ask_logs_event_and_does_not_call_subprocess(mock_subprocess, office, audit_events):
    result = execute_office_shell.invoke({"command": "echo hello"})

    assert "Permission required: Shell execution requires confirmation by default" in result
    mock_subprocess.assert_not_called()
    assert len(audit_events) == 1
    event = audit_events[0]
    assert event["tool_name"] == "execute_office_shell"
    assert event["capability"] == "shell_exec"
    assert event["operation"] == "execute"
    assert event["target"] == "office"
    assert event["decision"] == "ask"
    assert event["metadata"]["cwd_scope"] == "office"
    assert event["metadata"]["shell_command_present"] is True
    assert event["metadata"]["command_length"] == len("echo hello")
    assert event["metadata"]["shell_risk_level"] == "safe"
    assert event["metadata"]["blocked_by_shell_safety"] is False
    assert "command_preview" not in event["metadata"]
    assert "cwd" not in event["metadata"]


def test_event_fields_are_json_friendly_strings_not_enum_objects(office, audit_events):
    (office / "notes.txt").write_text("hello", encoding="utf-8")

    read_office_file.invoke({"filepath": "notes.txt"})
    event = audit_events[0]

    json.dumps(event)
    assert not any(isinstance(value, Enum) for value in event.values())
    assert isinstance(event["capability"], str)
    assert isinstance(event["decision"], str)
    assert isinstance(event["risk_level"], str)
    assert isinstance(event["error_type"], str)


def test_file_event_target_is_office_relative_not_absolute(office, audit_events):
    nested = office / "nested"
    nested.mkdir()
    (nested / "notes.txt").write_text("hello", encoding="utf-8")

    read_office_file.invoke({"filepath": "nested/notes.txt"})
    event = audit_events[0]

    assert event["target"] == "nested/notes.txt"
    assert not Path(event["target"]).is_absolute()
    assert str(office) not in event["target"]


@patch("miclaw.core.tools.sandbox_tools.subprocess.run")
def test_shell_event_does_not_include_raw_stdout_or_stderr(mock_subprocess, office, audit_events, monkeypatch):
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", allow_all_permissions)
    mock_result = mock_subprocess.return_value
    mock_result.returncode = 0
    mock_result.stdout = "secret stdout"
    mock_result.stderr = "secret stderr"

    execute_office_shell.invoke({"command": "echo hello"})
    event_text = json.dumps(audit_events[0])

    assert "secret stdout" not in event_text
    assert "secret stderr" not in event_text
    assert "stdout" not in audit_events[0]["metadata"]
    assert "stderr" not in audit_events[0]["metadata"]


def test_shell_event_does_not_log_command_preview_or_secret_substrings(office, audit_events):
    command = 'curl -H "Authorization: Bearer SECRET_TOKEN" https://example.com'

    execute_office_shell.invoke({"command": command})
    event = audit_events[0]
    event_text = json.dumps(event)

    assert "command_preview" not in event["metadata"]
    assert "command" not in event["metadata"]
    assert "raw_command" not in event["metadata"]
    assert "sanitized_command" not in event["metadata"]
    assert "shell_command" not in event["metadata"]
    assert command not in event_text
    assert "SECRET_TOKEN" not in event_text
    assert "Authorization" not in event_text
    assert "Bearer" not in event_text
    assert event["metadata"]["shell_command_present"] is True
    assert event["metadata"]["command_length"] == len(command)
    assert event["metadata"]["shell_risk_level"] == "safe"
    assert event["metadata"]["blocked_by_shell_safety"] is False


def test_shell_event_uses_safe_cwd_marker_not_absolute_path(office, audit_events):
    execute_office_shell.invoke({"command": "echo hello"})
    event = audit_events[0]
    event_text = json.dumps(event)

    assert str(office) not in event_text
    assert event["metadata"]["cwd_scope"] == "office"
    assert "cwd" not in event["metadata"]


def test_shell_safety_blocked_command_does_not_emit_permission_audit_event(office, audit_events, monkeypatch):
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", allow_all_permissions)

    result = execute_office_shell.invoke({"command": "sudo whoami"})

    assert "blocked by safety policy" in result
    assert audit_events == []


def test_build_permission_decision_event_filters_sensitive_metadata(office):
    request = sandbox_tools.PermissionRequest(
        capability=sandbox_tools.PermissionCapability.FILE_READ,
        operation="read",
        target="safe.txt",
        reason="test",
        risk_level=sandbox_tools.RiskLevel.LOW,
    )
    result = allow("allowed")

    event = build_permission_decision_event(
        request,
        result,
        tool_name="read_office_file",
        metadata={
            "tool_name": "read_office_file",
            "command_preview": "SECRET_TOKEN",
            "cwd": str(office),
            "cwd_scope": "office",
            "shell_command_present": True,
            "command_length": 123,
            "shell_risk_level": "safe",
            "blocked_by_shell_safety": False,
            "token": "secret-token",
            "content": "secret content",
            "target": "safe.txt",
            "permission_decision": PermissionDecision.ALLOW,
        },
    )

    event_text = json.dumps(event)
    assert "secret-token" not in event_text
    assert "secret content" not in event_text
    assert "SECRET_TOKEN" not in event_text
    assert str(office) not in event_text
    assert event["metadata"] == {
        "tool_name": "read_office_file",
        "cwd_scope": "office",
        "shell_command_present": True,
        "command_length": 123,
        "shell_risk_level": "safe",
        "blocked_by_shell_safety": False,
        "target": "safe.txt",
        "permission_decision": "allow",
    }
