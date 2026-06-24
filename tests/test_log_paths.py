import json
from pathlib import Path

from entry import monitor
from miclaw.core.config import get_log_file_path
from miclaw.core.logger import JSONLEventLogger, build_permission_decision_event
from miclaw.core.permissions import PermissionCapability, PermissionRequest, RiskLevel, allow


def test_default_log_path_resolves_under_workspace(tmp_path):
    workspace = tmp_path / "workspace"

    result = get_log_file_path(workspace=workspace)

    assert result == workspace / "logs" / "miclaw.jsonl"


def test_explicit_log_file_override_is_respected(tmp_path):
    explicit = tmp_path / "custom" / "events.jsonl"

    result = get_log_file_path(log_file=explicit)

    assert result == explicit


def test_logger_writes_jsonl_to_temporary_log_path(tmp_path):
    log_file = tmp_path / "logs" / "events.jsonl"
    logger = JSONLEventLogger(log_file=log_file)

    logger.log_event(thread_id="test-thread", event="system_action", content="hello")
    logger.log_queue.join()
    logger.shutdown()

    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event"] == "system_action"
    assert event["thread_id"] == "test-thread"
    assert event["content"] == "hello"


def test_logger_preserves_legacy_log_dir_per_thread_files(tmp_path):
    log_dir = tmp_path / "legacy-logs"
    logger = JSONLEventLogger(log_dir)

    logger.log_event(thread_id="test-thread", event="system_action", content="legacy")
    logger.log_queue.join()
    logger.shutdown()

    log_file = log_dir / "test-thread.jsonl"
    event = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert event["event"] == "system_action"
    assert event["content"] == "legacy"


def test_logger_preserves_legacy_log_dir_keyword(tmp_path):
    log_dir = tmp_path / "legacy-keyword-logs"
    logger = JSONLEventLogger(log_dir=log_dir)

    logger.log_event(thread_id="test-thread", event="system_action", content="legacy-keyword")
    logger.log_queue.join()
    logger.shutdown()

    log_file = log_dir / "test-thread.jsonl"
    event = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert event["event"] == "system_action"
    assert event["content"] == "legacy-keyword"


def test_permission_decision_event_can_be_written_and_read_back(tmp_path):
    log_file = tmp_path / "permission.jsonl"
    logger = JSONLEventLogger(log_file=log_file)
    request = PermissionRequest(
        capability=PermissionCapability.FILE_READ,
        operation="read",
        target="notes.txt",
        risk_level=RiskLevel.LOW,
        reason="test",
    )
    result = allow("allowed", RiskLevel.LOW)
    event = build_permission_decision_event(request, result, tool_name="read_office_file")

    logger.log_event("test-thread", event["event_type"], **event)
    logger.log_queue.join()
    logger.shutdown()

    saved = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert saved["event"] == "permission_decision"
    assert saved["event_type"] == "permission_decision"
    assert saved["capability"] == "file_read"
    assert saved["decision"] == "allow"


def test_monitor_respects_explicit_log_file_override(tmp_path):
    explicit = tmp_path / "override.jsonl"

    result = monitor.resolve_monitor_log_file(explicit)

    assert result == explicit


def test_monitor_falls_back_to_legacy_log_file(tmp_path, monkeypatch):
    default = tmp_path / "workspace" / "logs" / "miclaw.jsonl"
    legacy = tmp_path / "logs" / "local_geek_master.jsonl"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("", encoding="utf-8")
    monkeypatch.setattr(monitor, "LEGACY_LOG_FILE", legacy)
    monkeypatch.setattr(monitor, "get_log_file_path", lambda **kwargs: default)

    result = monitor.resolve_monitor_log_file()

    assert result == legacy


def test_monitor_parser_tolerates_permission_decision_event():
    event = monitor.parse_event_line(
        json.dumps(
            {
                "event": "permission_decision",
                "tool_name": "read_office_file",
                "capability": "file_read",
                "operation": "read",
                "decision": "allow",
                "risk_level": "low",
                "requires_confirmation": False,
            }
        )
    )

    assert event["event"] == "permission_decision"
    monitor.render_event(event)


def test_monitor_parser_tolerates_unknown_event_type():
    event = monitor.parse_event_line(json.dumps({"event": "future_event", "value": 1}))

    assert event["event"] == "future_event"
    monitor.render_event(event)


def test_monitor_parser_tolerates_missing_optional_fields():
    event = monitor.parse_event_line(json.dumps({"event": "permission_decision"}))

    assert event["event"] == "permission_decision"
    monitor.render_event(event)


def test_malformed_jsonl_line_does_not_crash_reader(tmp_path):
    log_file = tmp_path / "bad.jsonl"
    log_file.write_text('{"event": "ok"}\nnot-json\n', encoding="utf-8")

    events = monitor.read_jsonl_events(log_file)

    assert events[0]["event"] == "ok"
    assert events[1]["event"] == "parse_error"
