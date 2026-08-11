import json
from io import StringIO

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from rich.console import Console
from typer.testing import CliRunner

from entry import monitor
from entry.cli import app as cli_app
from miclaw.core import agent
from miclaw.core.logger import JSONLEventLogger
from miclaw.core.redaction import DEPTH_LIMIT_REACHED, LARGE_INTEGER_OMITTED, REDACTED
from miclaw.core.trace import TraceContext, reset_trace_context, set_current_trace_context


FORBIDDEN_MARKERS = (
    "SECRET_TOKEN",
    "abcdefgh",
    "OPENAI_SECRET_VALUE",
    "AWS_SECRET_VALUE",
    "DATABASE_SECRET_VALUE",
    "SECRET_COMMAND",
    "PRIVATE_FILE_CONTENT",
    "STDOUT_SECRET",
    "STDERR_SECRET",
    "NESTED_SECRET",
    "RAW_COLLISION_MARKER",
    "AI_MESSAGE_SECRET",
    "DEEP_SECRET_MARKER",
    "LEGACY_RESULT_SECRET",
    "OTHER_RUN_SECRET",
)


class _SequentialModel:
    """按顺序返回预设 AIMessage，驱动真实 agent graph。"""

    def __init__(self, responses):
        self.responses = list(responses)

    def invoke(self, _messages):
        return self.responses.pop(0)


class _Provider:
    """提供 create_agent_app 所需的最小 bind_tools 接口。"""

    def __init__(self, model):
        self.model = model

    def bind_tools(self, _tools):
        return self.model


class _ObservabilityFixtureTool(BaseTool):
    """执行真实 ToolNode 调度，但避免测试工具提前 stringify 巨型整数。"""

    name: str = "observability_fixture_tool"
    description: str = "Observability integration fixture"
    result: str

    def invoke(self, _input, config=None, **_kwargs):
        tool_call_id = _input.get("id", "fixture-call") if isinstance(_input, dict) else "fixture-call"
        return ToolMessage(content=self.result, name=self.name, tool_call_id=tool_call_id)

    def _run(self, **_kwargs):
        return self.result


def _assert_forbidden_absent(text: str) -> None:
    for marker in FORBIDDEN_MARKERS:
        assert marker not in text


def _render_events(monkeypatch, events) -> str:
    output = StringIO()
    monkeypatch.setattr(
        monitor,
        "console",
        Console(file=output, force_terminal=False, color_system=None, width=120),
    )
    for event in events:
        monitor.render_event(event)
    return output.getvalue()


def _invoke_logs(log_file, lines=100):
    return CliRunner().invoke(
        cli_app,
        ["logs", "--tail", "--log-file", str(log_file), "--lines", str(lines)],
    )


def _invoke_trace(log_file, run_id):
    return CliRunner().invoke(cli_app, ["trace", run_id, "--log-file", str(log_file)])


def test_agent_jsonl_to_monitor_logs_and_trace_stays_redacted(tmp_path, monkeypatch):
    command = 'curl -H "Authorization: Bearer SECRET_TOKEN" https://example.com'
    content = "PRIVATE_FILE_CONTENT"
    collision = "RAW_COLLISION_MARKER"
    huge_integer = 10**10_000
    raw_huge_integer_text = "1" + "0" * 10_000
    large_nested = [{"value": index} for index in range(100)]
    tool_output = "stdout=STDOUT_SECRET stderr=STDERR_SECRET Bearer abcdefgh"
    ai_content = "Authorization: Bearer AI_MESSAGE_SECRET " + "long output " * 10_000

    args = dict(
        [
            ("command", command),
            ("command_present", collision),
            ("command_length", collision),
            ("content", content),
            ("content_present", collision),
            ("content_length", collision),
            ("input", "Bearer abcdefgh"),
            ("openai_api_key", "OPENAI_SECRET_VALUE"),
            ("aws_access_key", "AWS_SECRET_VALUE"),
            ("database_password", "DATABASE_SECRET_VALUE"),
            (
                "deep_nested",
                {
                    "client_secret": "NESTED_SECRET",
                    "level1": {"level2": [{"level3": {"level4": "DEEP_SECRET_MARKER"}}]},
                },
            ),
            ("huge_integer", huge_integer),
            ("large_nested", large_nested),
            ("token_count", 17),
            ("output_tokens", 23),
            ("queue_length", 5),
            ("response_length", 89),
            ("latency_ms", 34),
            ("tool_name", "observability_fixture_tool"),
            ("risk_level", "medium"),
        ]
    )
    model = _SequentialModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "observability_fixture_tool",
                        "args": args,
                        "id": "call-forward",
                        "type": "tool_call",
                    },
                    {
                        "name": "observability_fixture_tool",
                        "args": dict(reversed(list(args.items()))),
                        "id": "call-reversed",
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(content=ai_content),
        ]
    )
    monkeypatch.setattr(agent, "get_provider", lambda **_kwargs: _Provider(model))

    log_file = tmp_path / "agent.jsonl"
    logger = JSONLEventLogger(log_file=log_file)
    monkeypatch.setattr(agent, "audit_logger", logger)
    trace_token = set_current_trace_context(TraceContext(run_id="redaction-run"))
    try:
        app = agent.create_agent_app(tools=[_ObservabilityFixtureTool(result=tool_output)])
        app.invoke(
            {"messages": [HumanMessage(content="exercise observability")], "summary": ""},
            config={"configurable": {"thread_id": "integration-thread"}},
        )
    finally:
        reset_trace_context(trace_token)
        logger.shutdown()

    with log_file.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "event": "tool_result",
                    "tool": "other_tool",
                    "result_summary": "OTHER_RUN_SECRET",
                    "run_id": "other-run",
                    "step_id": 1,
                }
            )
            + "\n"
        )

    raw_text = log_file.read_text(encoding="utf-8")
    generated_events = monitor.read_jsonl_events(log_file)
    redaction_events = [event for event in generated_events if event.get("run_id") == "redaction-run"]
    tool_calls = [
        event
        for event in redaction_events
        if event.get("event") == "tool_call" and event.get("tool") == "observability_fixture_tool"
    ]

    _assert_forbidden_absent("\n".join(line for line in raw_text.splitlines() if "other-run" not in line))
    assert raw_huge_integer_text not in raw_text
    assert DEPTH_LIMIT_REACHED in raw_text
    assert LARGE_INTEGER_OMITTED in raw_text
    assert len(tool_calls) == 2
    assert all(event["args"]["command_present"] is True for event in tool_calls)
    assert all(event["args"]["command_length"] == len(command) for event in tool_calls)
    assert all(event["args"]["content_present"] is True for event in tool_calls)
    assert all(event["args"]["content_length"] == len(content) for event in tool_calls)
    assert all(event["args"]["openai_api_key"] == REDACTED for event in tool_calls)
    assert all(event["args"]["aws_access_key"] == REDACTED for event in tool_calls)
    assert all(event["args"]["database_password"] == REDACTED for event in tool_calls)
    assert all(event["args"]["huge_integer"] == LARGE_INTEGER_OMITTED for event in tool_calls)
    assert all(DEPTH_LIMIT_REACHED in json.dumps(event["args"]["deep_nested"]) for event in tool_calls)
    assert all(len(event["args"]["large_nested"]) <= 21 for event in tool_calls)
    assert all(event["args"]["token_count"] == 17 for event in tool_calls)
    assert all(event["args"]["output_tokens"] == 23 for event in tool_calls)
    assert all(event["args"]["queue_length"] == 5 for event in tool_calls)
    assert all(event["args"]["response_length"] == 89 for event in tool_calls)
    assert all(event["args"]["latency_ms"] == 34 for event in tool_calls)
    assert all(event["run_id"] == "redaction-run" for event in redaction_events)
    assert [event["step_id"] for event in redaction_events] == list(range(1, len(redaction_events) + 1))
    assert len(raw_text) < 25_000

    monitor_cli_text = "\n".join(monitor.format_log_event_for_cli(event) for event in generated_events)
    rendered_text = _render_events(monkeypatch, generated_events)
    logs_result = _invoke_logs(log_file)
    trace_result = _invoke_trace(log_file, "redaction-run")

    assert logs_result.exit_code == 0
    assert trace_result.exit_code == 0
    for output in (monitor_cli_text, rendered_text, logs_result.output, trace_result.output):
        _assert_forbidden_absent(output)
        assert len(output) < 25_000
    assert "queue_length=5" in monitor_cli_text
    assert "response_length=89" in monitor_cli_text
    assert "queue_length=5" in logs_result.output
    assert "Trace run=redaction-run" in trace_result.output
    assert "other-run" not in trace_result.output
    assert "OTHER_RUN_SECRET" not in trace_result.output
    assert trace_result.output.index("step=1") < trace_result.output.index("step=2")


def test_legacy_unsafe_jsonl_is_sanitized_across_monitor_logs_and_trace(tmp_path, monkeypatch):
    log_file = tmp_path / "legacy.jsonl"
    legacy_events = [
        {
            "event": "permission_decision",
            "tool_name": "execute_office_shell",
            "capability": "shell_exec",
            "operation": "execute",
            "decision": "ask",
            "risk_level": "medium",
            "run_id": "legacy-run",
            "step_id": 1,
        },
        {
            "event": "tool_call",
            "tool": "execute_office_shell",
            "args": {
                "command": 'curl -H "Authorization: Bearer SECRET_TOKEN" https://example.com',
                "command_present": "RAW_COLLISION_MARKER",
                "command_length": "RAW_COLLISION_MARKER",
                "content": "PRIVATE_FILE_CONTENT",
                "content_present": "RAW_COLLISION_MARKER",
                "content_length": "RAW_COLLISION_MARKER",
                "nested": {
                    "openai_api_key": "OPENAI_SECRET_VALUE",
                    "aws_access_key": "AWS_SECRET_VALUE",
                    "database_password": "DATABASE_SECRET_VALUE",
                },
                "queue_length": 5,
                "response_length": 89,
            },
            "run_id": "legacy-run",
            "step_id": 2,
        },
        {
            "event": "tool_result",
            "tool": "execute_office_shell",
            "result_summary": "LEGACY_RESULT_SECRET stdout=STDOUT_SECRET stderr=STDERR_SECRET Bearer abcdefgh",
            "run_id": "legacy-run",
            "step_id": 3,
        },
        {
            "event": "ai_message",
            "content": "Authorization: Bearer AI_MESSAGE_SECRET " + "long output " * 10_000,
            "run_id": "legacy-run",
            "step_id": 4,
        },
        {
            "event": "future_event",
            "payload": {
                "client_secret": "NESTED_SECRET",
                "huge_integer": "9" * 10_000,
                "content": "PRIVATE_FILE_CONTENT" * 1_000,
            },
            "run_id": "legacy-run",
            "step_id": 5,
        },
        {
            "event": "tool_result",
            "result_summary": "OTHER_RUN_SECRET",
            "run_id": "other-run",
            "step_id": 1,
        },
    ]
    lines = [json.dumps(event, ensure_ascii=False) for event in legacy_events]
    lines.insert(5, "not-json Authorization: Bearer SECRET_TOKEN")
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    parsed_events = monitor.read_jsonl_events(log_file)
    cli_formatted = "\n".join(monitor.format_log_event_for_cli(event) for event in parsed_events)
    rendered = _render_events(monkeypatch, parsed_events)
    logs_result = _invoke_logs(log_file)
    trace_result = _invoke_trace(log_file, "legacy-run")

    assert logs_result.exit_code == 0
    assert trace_result.exit_code == 0
    for output in (cli_formatted, rendered, logs_result.output, trace_result.output):
        _assert_forbidden_absent(output)
        assert len(output) < 25_000
    assert "PERMISSION ask shell_exec execute" in logs_result.output
    assert "status=blocked_pending_confirmation" in logs_result.output
    assert "command_present=true" in logs_result.output
    assert "queue_length=5" in logs_result.output
    assert "response_length=89" in logs_result.output
    assert "[parse_error] malformed JSONL line skipped" in logs_result.output
    assert "Trace run=legacy-run" in trace_result.output
    assert "other-run" not in trace_result.output
    assert trace_result.output.index("step=1") < trace_result.output.index("step=5")
