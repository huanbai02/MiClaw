import json

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from miclaw.core import agent
from miclaw.core.logger import JSONLEventLogger
from miclaw.core.redaction import CONTENT_OMITTED, REDACTED, REDACTION_FAILED, summarize_tool_args
from miclaw.core.trace import TraceContext, reset_trace_context, set_current_trace_context


class _SequentialModel:
    """按顺序返回预设 AIMessage，供 agent graph 集成测试使用。"""

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


def test_agent_jsonl_events_sanitize_tool_args_results_and_ai_content(tmp_path, monkeypatch):
    command = 'curl -H "Authorization: Bearer SECRET_TOKEN" https://example.com'
    file_content = "PRIVATE_FILE_CONTENT"
    collision_marker = "RAW_COLLISION_MARKER"
    tool_result = "stdout Bearer abcdefgh SECRET_TOOL_RESULT"
    ai_content = "Bearer abcdefghij " + "LONG_AI_SECRET" * 100

    @tool
    def audit_fixture_tool(
        command: str,
        command_present: str,
        command_length: str,
        content: str,
        content_present: str,
        content_length: str,
        token_count: int,
        queue_length: int,
        response_length: int,
        nested: dict,
    ) -> str:
        """返回包含敏感数据的测试结果。"""
        return tool_result

    forward_args = dict(
        [
            ("command", command),
            ("command_present", collision_marker),
            ("command_length", collision_marker),
            ("content", file_content),
            ("content_present", collision_marker),
            ("content_length", collision_marker),
            ("token_count", 17),
            ("queue_length", 5),
            ("response_length", 89),
            ("nested", {"openai_api_key": "OPENAI_SECRET", "safe": "visible"}),
        ]
    )
    reversed_args = dict(reversed(list(forward_args.items())))
    model = _SequentialModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "audit_fixture_tool",
                        "args": forward_args,
                        "id": "call-1",
                        "type": "tool_call",
                    },
                    {
                        "name": "audit_fixture_tool",
                        "args": reversed_args,
                        "id": "call-2",
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
    trace_token = set_current_trace_context(TraceContext(run_id="audit-run"))
    try:
        app = agent.create_agent_app(tools=[audit_fixture_tool])
        app.invoke(
            {"messages": [HumanMessage(content="run audit test")], "summary": ""},
            config={"configurable": {"thread_id": "audit-thread"}},
        )
    finally:
        reset_trace_context(trace_token)
        logger.shutdown()

    events = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    tool_call_events = [event for event in events if event["event"] == "tool_call"]
    tool_result_events = [event for event in events if event["event"] == "tool_result"]
    ai_message_event = next(event for event in events if event["event"] == "ai_message")
    serialized = json.dumps(events, ensure_ascii=False)

    expected_args = {
        "command_present": True,
        "command_length": len(command),
        "content_present": True,
        "content_length": len(file_content),
        "token_count": 17,
        "queue_length": 5,
        "response_length": 89,
        "nested": {"openai_api_key": REDACTED, "safe": "visible"},
    }
    assert len(tool_call_events) == 2
    assert all(event["args"] == expected_args for event in tool_call_events)
    assert all(event["result_summary"].startswith(CONTENT_OMITTED) for event in tool_result_events)
    assert ai_message_event["content"].startswith(CONTENT_OMITTED)
    assert all(len(event["result_summary"]) < 100 for event in tool_result_events)
    assert len(ai_message_event["content"]) < 100
    assert all(event["run_id"] == "audit-run" for event in events)
    assert [event["step_id"] for event in events] == list(range(1, len(events) + 1))
    assert not any(
        secret in serialized
        for secret in (
            "SECRET_TOKEN",
            "PRIVATE_FILE_CONTENT",
            "OPENAI_SECRET",
            "SECRET_TOOL_RESULT",
            "LONG_AI_SECRET",
            collision_marker,
            command,
            tool_result,
            ai_content,
        )
    )


def test_malformed_tool_args_fail_closed_and_remain_json_serializable():
    class BrokenMapping(dict):
        def items(self):
            raise RuntimeError("RAW_SECRET")

    result = summarize_tool_args(BrokenMapping({"token": "RAW_SECRET"}))
    serialized = json.dumps(result)

    assert result == {"_redaction_error": REDACTION_FAILED}
    assert "RAW_SECRET" not in serialized
