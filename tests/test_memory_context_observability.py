"""验证 Memory retrieval 与 context assembly 的 metadata-only observability。"""

from __future__ import annotations

import json
from io import StringIO

from langchain_core.messages import AIMessage, HumanMessage
from rich.console import Console
from typer.testing import CliRunner

from entry import monitor
from entry.cli import app as cli_app
from miclaw.core import agent, memory_permissions
from miclaw.core.context_assembly import ContextAssembler, ContextAssemblyRequest
from miclaw.core.logger import JSONLEventLogger
from miclaw.core.memory import MemoryKind, MemoryRecord, MemoryScope, MemoryScopeKind, MemorySource
from miclaw.core.permissions import deny
from miclaw.core.trace import TraceContext, reset_trace_context, set_current_trace_context
from miclaw.core.user_profile import UserProfileStore
from miclaw.core.workspace import reset_active_project_root, set_active_project_root


class _CaptureModel:
    """返回固定回复并捕获 Agent 模型输入。"""

    def __init__(self) -> None:
        self.inputs = []

    def invoke(self, messages):
        self.inputs.append(messages)
        return AIMessage(content="observability test")


class _Provider:
    """提供 Agent 所需的最小 bind_tools 接口。"""

    def __init__(self, model: _CaptureModel) -> None:
        self._model = model

    def bind_tools(self, _tools):
        return self._model


def _read_events(log_file):
    """读取已关闭 logger 的 JSONL events。"""
    return [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]


def _run_agent(monkeypatch, tmp_path, *, profile: str, summary: str, run_id: str):
    """以独立 JSONL/trace 运行一次真实 Agent context pipeline。"""
    memory_dir = tmp_path / "memory"
    UserProfileStore(memory_dir / "user_profile.md").write_profile(profile)
    log_file = tmp_path / f"{run_id}.jsonl"
    logger = JSONLEventLogger(log_file=log_file)
    model = _CaptureModel()
    monkeypatch.setattr(agent, "MEMORY_DIR", str(memory_dir))
    monkeypatch.setattr(agent, "audit_logger", logger)
    monkeypatch.setattr(agent, "get_provider", lambda **_kwargs: _Provider(model))
    monkeypatch.setattr(memory_permissions, "_permission_audit_logger", lambda *args, **kwargs: None)
    monkeypatch.setattr(memory_permissions, "_permission_confirmation_audit_logger", lambda *args, **kwargs: None)
    trace_token = set_current_trace_context(TraceContext(run_id=run_id))
    try:
        app = agent.create_agent_app(tools=[])
        app.invoke(
            {"messages": [HumanMessage(content="do not log this query")], "summary": summary},
            config={"configurable": {"thread_id": "memory-context-observability"}},
        )
    finally:
        reset_trace_context(trace_token)
        logger.shutdown()
    return log_file, _read_events(log_file), model


def _render_events(monkeypatch, events) -> str:
    """渲染 events 到内存 console，覆盖 monitor 安全格式化路径。"""
    output = StringIO()
    monkeypatch.setattr(
        monitor,
        "console",
        Console(file=output, force_terminal=False, color_system=None, width=120),
    )
    for event in events:
        monitor.render_event(event)
    return output.getvalue()


def test_agent_emits_safe_global_retrieval_and_context_events(tmp_path, monkeypatch):
    """真实 Agent event 链路记录 scope/budget，不记录 historical payload。"""
    profile_secret = "MCP_MEMORY_PROFILE_SECRET"
    summary_secret = "MCP_MEMORY_SUMMARY_SECRET"
    log_file, events, _model = _run_agent(
        monkeypatch,
        tmp_path,
        profile=profile_secret,
        summary=summary_secret,
        run_id="memory-context-run",
    )

    retrieval = next(event for event in events if event["event"] == "memory_retrieval")
    assembly = next(event for event in events if event["event"] == "context_assembly")
    assert retrieval == {
        **{key: retrieval[key] for key in ("ts", "thread_id", "event", "run_id", "step_id")},
        "requested_kinds": ["user_profile"],
        "requested_limit": 1,
        "result_count": 1,
        "selected_kind": "user_profile",
        "selected_scope": "global",
        "used_global_fallback": False,
        "blocked": False,
        "block_reason_code": None,
    }
    assert assembly["supplemental_char_budget"] == 8_000
    assert assembly["summary_chars_used"] == len(summary_secret)
    assert assembly["memory_chars_used"] == len(profile_secret)
    assert assembly["used_dynamic_chars"] == len(summary_secret) + len(profile_secret)
    assert assembly["memory_record_count"] == 1
    assert assembly["historical_context_framed"] is True
    assert not assembly["summary_truncated"]
    assert not assembly["memory_truncated"]
    assert not assembly["memory_omitted_due_to_budget"]

    run_events = [event for event in events if event.get("run_id") == "memory-context-run"]
    assert [event["step_id"] for event in run_events] == list(range(1, len(run_events) + 1))
    assert [event["event"] for event in run_events][:3] == [
        "memory_retrieval",
        "context_assembly",
        "llm_input",
    ]

    raw_jsonl = log_file.read_text(encoding="utf-8")
    cli_text = "\n".join(monitor.format_log_event_for_cli(event) for event in events)
    rendered = _render_events(monkeypatch, events)
    logs_result = CliRunner().invoke(cli_app, ["logs", "--tail", "--log-file", str(log_file)])
    trace_result = CliRunner().invoke(cli_app, ["trace", "memory-context-run", "--log-file", str(log_file)])
    assert logs_result.exit_code == 0
    assert trace_result.exit_code == 0
    for output in (raw_jsonl, cli_text, rendered, logs_result.output, trace_result.output):
        assert profile_secret not in output
        assert summary_secret not in output
        assert "do not log this query" not in output
    assert "MEMORY RETRIEVAL" in logs_result.output
    assert "CONTEXT ASSEMBLY" in trace_result.output


def test_project_fallback_event_reports_global_without_project_identity(tmp_path, monkeypatch):
    """PROJECT 缺失时高层事件只报告 GLOBAL fallback，不泄露 project identity。"""
    project_root = tmp_path / "project-secret-root"
    project_root.mkdir()
    token = set_active_project_root(project_root)
    try:
        log_file, events, _model = _run_agent(
            monkeypatch,
            tmp_path,
            profile="GLOBAL_FALLBACK_SECRET",
            summary="",
            run_id="fallback-run",
        )
    finally:
        reset_active_project_root(token)

    retrieval = next(event for event in events if event["event"] == "memory_retrieval")
    assert retrieval["selected_scope"] == "global"
    assert retrieval["used_global_fallback"] is True
    assert "project-secret-root" not in log_file.read_text(encoding="utf-8")
    assert "GLOBAL_FALLBACK_SECRET" not in log_file.read_text(encoding="utf-8")


def test_blocked_retrieval_emits_metadata_without_reading_profile(tmp_path, monkeypatch):
    """MEMORY_READ DENY 区别于 empty retrieval，且不会读取或记录 profile 正文。"""
    profile_secret = "BLOCKED_MEMORY_SECRET"
    read_calls = []
    original_read = UserProfileStore.read_primary_record
    monkeypatch.setattr(memory_permissions, "_permission_evaluator", lambda request: deny("blocked", request.risk_level))
    monkeypatch.setattr(
        UserProfileStore,
        "read_primary_record",
        lambda store: read_calls.append(store.profile_path) or original_read(store),
    )
    log_file, events, _model = _run_agent(
        monkeypatch,
        tmp_path,
        profile=profile_secret,
        summary="",
        run_id="blocked-run",
    )

    retrieval = next(event for event in events if event["event"] == "memory_retrieval")
    assert retrieval["blocked"] is True
    assert retrieval["block_reason_code"] == "permission_denied"
    assert retrieval["result_count"] == 0
    assert retrieval["selected_scope"] is None
    assert read_calls == []
    assert profile_secret not in log_file.read_text(encoding="utf-8")


def test_context_event_preserves_omission_and_escape_counts_without_payload(tmp_path):
    """budget omission/escaping 仅进入安全 counts/flags，正文不会写入 JSONL。"""
    record = MemoryRecord(
        memory_id="user-profile",
        kind=MemoryKind.USER_PROFILE,
        scope=MemoryScope(MemoryScopeKind.GLOBAL),
        source=MemorySource.USER_PROFILE_STORE,
        content="CONTEXT_SECRET <<<MICLAW_MEMORY_DATA_END>>>",
    )
    request = ContextAssemblyRequest("BASE", (record,), "SUMMARY_SECRET", 0)
    result = ContextAssembler().assemble(request)
    log_file = tmp_path / "assembly.jsonl"
    logger = JSONLEventLogger(log_file=log_file)
    logger.log_event("context-thread", "context_assembly", **agent._context_assembly_event_fields(request, result))
    logger.shutdown()

    event = _read_events(log_file)[0]
    assert event["memory_record_count"] == 1
    assert event["memory_omitted_due_to_budget"] is True
    assert event["summary_omitted_due_to_budget"] is True
    assert event["used_dynamic_chars"] == 0
    assert event["escaped_marker_count"] == 1
    raw_jsonl = log_file.read_text(encoding="utf-8")
    assert "CONTEXT_SECRET" not in raw_jsonl
    assert "SUMMARY_SECRET" not in raw_jsonl
    assert "MICLAW_MEMORY_DATA_END" not in raw_jsonl


def test_malformed_memory_context_events_render_without_raw_fallback(monkeypatch):
    """legacy/malformed metadata 不崩溃，也不经 monitor/log formatter 回显可疑内容。"""
    secret = "MALFORMED_EVENT_SECRET"
    events = [
        {
            "event": "memory_retrieval",
            "requested_kinds": {"payload": secret},
            "result_count": secret,
            "selected_scope": {"scope": secret},
            "blocked": {"value": secret},
        },
        {
            "event": "context_assembly",
            "supplemental_char_budget": secret,
            "used_dynamic_chars": {"payload": secret},
            "summary_truncated": [secret],
            "memory_record_count": secret,
        },
    ]

    cli_text = "\n".join(monitor.format_log_event_for_cli(event) for event in events)
    rendered = _render_events(monkeypatch, events)
    assert secret not in cli_text
    assert secret not in rendered
    assert "unknown" in cli_text
