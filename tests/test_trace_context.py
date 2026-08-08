import json

from miclaw.core.logger import JSONLEventLogger, build_permission_decision_event
from miclaw.core.permissions import PermissionCapability, PermissionRequest, RiskLevel, allow
from miclaw.core.trace import TraceContext, new_run_id, reset_trace_context, set_current_trace_context


def _read_events(log_file):
    return [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]


def test_new_run_id_is_non_empty_and_unique():
    first = new_run_id()
    second = new_run_id()

    assert isinstance(first, str)
    assert first
    assert isinstance(second, str)
    assert second
    assert first != second


def test_trace_context_step_id_increments():
    context = TraceContext(run_id="run-test")

    assert context.next_step_id() == 1
    assert context.next_step_id() == 2


def test_logger_writes_event_with_explicit_run_and_step(tmp_path):
    log_file = tmp_path / "trace.jsonl"
    logger = JSONLEventLogger(log_file=log_file)

    logger.log_event("thread", "system_action", content="hello", run_id="run-1", step_id=7)
    logger.log_queue.join()
    logger.shutdown()

    event = _read_events(log_file)[0]
    assert event["run_id"] == "run-1"
    assert event["step_id"] == 7


def test_logger_still_writes_event_without_trace_fields(tmp_path):
    log_file = tmp_path / "no-trace.jsonl"
    logger = JSONLEventLogger(log_file=log_file)

    logger.log_event("thread", "system_action", content="hello")
    logger.log_queue.join()
    logger.shutdown()

    event = _read_events(log_file)[0]
    assert event["event"] == "system_action"
    assert "run_id" not in event
    assert "step_id" not in event


def test_logger_adds_current_trace_context_fields(tmp_path):
    log_file = tmp_path / "context-trace.jsonl"
    logger = JSONLEventLogger(log_file=log_file)
    context = TraceContext(run_id="run-context")
    token = set_current_trace_context(context)
    try:
        logger.log_event("thread", "system_action", content="first")
        logger.log_event("thread", "system_action", content="second")
    finally:
        reset_trace_context(token)
    logger.log_queue.join()
    logger.shutdown()

    events = _read_events(log_file)
    assert [event["run_id"] for event in events] == ["run-context", "run-context"]
    assert [event["step_id"] for event in events] == [1, 2]


def test_permission_decision_event_includes_run_and_step_when_provided():
    request = PermissionRequest(
        capability=PermissionCapability.FILE_READ,
        operation="read",
        target="notes.txt",
        risk_level=RiskLevel.LOW,
        reason="test",
    )
    result = allow("allowed", RiskLevel.LOW)

    event = build_permission_decision_event(
        request,
        result,
        tool_name="read_office_file",
        run_id="run-1",
        step_id=3,
    )

    assert event["event_type"] == "permission_decision"
    assert event["run_id"] == "run-1"
    assert event["step_id"] == 3
