"""冻结 Phase 4C 长期画像写入到后续 context 的真实组合链路。"""

from __future__ import annotations

import json
from io import StringIO
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from rich.console import Console
from typer.testing import CliRunner

from entry import monitor
from entry.cli import app as cli_app
import miclaw.core.agent.graph as agent
import miclaw.core.memory.lifecycle as memory_lifecycle
import miclaw.core.memory.user_profile as user_profile
import miclaw.core.observability.logger as logger_module
from miclaw.core.observability.logger import JSONLEventLogger
from miclaw.core.memory.lifecycle import (
    MemoryWriteIntent,
    reset_memory_write_intent,
    set_memory_write_intent,
)
from miclaw.core.security.permissions import (
    PermissionConfirmationChoice,
    reset_permission_confirmation_handler,
    reset_session_permission_grants,
    set_permission_confirmation_handler,
    set_session_permission_grants,
)
from miclaw.core.tools import builtins
from miclaw.core.observability.trace import TraceContext, reset_trace_context, set_current_trace_context
from miclaw.core.memory.user_profile import UserProfilePersistenceError, UserProfileStore, get_user_profile_store
from miclaw.core.runtime.workspace import reset_active_project_root, set_active_project_root


class _SequenceModel:
    """按顺序返回响应并记录真实 Agent 的模型输入。"""

    def __init__(self, responses) -> None:
        self.inputs = []
        self._responses = list(responses)

    def invoke(self, messages):
        self.inputs.append(messages)
        return self._responses.pop(0)


class _Provider:
    """提供 create_agent_app 所需的最小 bind_tools 接口。"""

    def __init__(self, model: _SequenceModel) -> None:
        self._model = model

    def bind_tools(self, _tools):
        return self._model


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    """配置真实 lifecycle、Agent、JSONL 和临时 Memory root。"""
    memory_dir = tmp_path / "workspace" / "memory"
    log_file = tmp_path / "events.jsonl"
    event_logger = JSONLEventLogger(log_file=log_file)
    monkeypatch.setattr(agent, "MEMORY_DIR", str(memory_dir))
    monkeypatch.setattr(builtins, "MEMORY_DIR", str(memory_dir))
    monkeypatch.setattr(agent, "audit_logger", event_logger)
    monkeypatch.setattr(logger_module, "audit_logger", event_logger)
    yield SimpleNamespace(memory_dir=memory_dir, log_file=log_file, logger=event_logger)
    event_logger.shutdown()


def _global_store(runtime) -> UserProfileStore:
    """返回测试 GLOBAL profile 的 concrete store。"""
    return UserProfileStore(runtime.memory_dir / "user_profile.md")


def _invoke(app, model: _SequenceModel, run_id: str) -> str:
    """执行一次真实 Agent node，并返回该次模型输入中的 SystemMessage。"""
    trace_token = set_current_trace_context(TraceContext(run_id=run_id))
    try:
        app.invoke(
            {"messages": [HumanMessage(content="USER_QUERY_SECRET")], "summary": "SUMMARY_SECRET"},
            config={"configurable": {"thread_id": "memory-lifecycle-e2e"}},
        )
    finally:
        reset_trace_context(trace_token)
    messages = model.inputs[-1]
    return str(next(message.content for message in messages if isinstance(message, SystemMessage)))


def _events(runtime, run_id: str) -> list[dict]:
    """等待 JSONL 写入并读取当前 run 的事件。"""
    runtime.logger.log_queue.join()
    return [
        json.loads(line)
        for line in runtime.log_file.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("run_id") == run_id
    ]


def _safe_outputs(runtime, events: list[dict], monkeypatch, run_id: str) -> tuple[str, ...]:
    """覆盖 JSONL、monitor、logs 与 trace 的安全展示链路。"""
    output = StringIO()
    monkeypatch.setattr(
        monitor,
        "console",
        Console(file=output, force_terminal=False, color_system=None, width=140),
    )
    for event in events:
        monitor.render_event(event)
    logs = CliRunner().invoke(cli_app, ["logs", "--tail", "--log-file", str(runtime.log_file), "--lines", "100"])
    trace = CliRunner().invoke(cli_app, ["trace", run_id, "--log-file", str(runtime.log_file)])
    assert logs.exit_code == 0
    assert trace.exit_code == 0
    return runtime.log_file.read_text(encoding="utf-8"), output.getvalue(), logs.output, trace.output


def test_explicit_agent_write_closes_through_context_and_safe_observability(runtime, monkeypatch):
    """真实 Agent ToolNode 写入后，同一 app 的后续 node 经 retrieval/assembly 看到新 profile。"""
    secret = "MEMORY_LIFECYCLE_SECRET_X"
    model = _SequenceModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "save_user_profile",
                        "args": {"new_content": secret},
                        "id": "explicit-write",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="saved"),
            AIMessage(content="read"),
        ]
    )
    monkeypatch.setattr(agent, "get_provider", lambda **_kwargs: _Provider(model))
    app = agent.create_agent_app(tools=[builtins.save_user_profile])
    confirmation_token = set_permission_confirmation_handler(
        lambda _request, _result: PermissionConfirmationChoice.ALLOW_ONCE
    )
    intent_token = set_memory_write_intent(MemoryWriteIntent.EXPLICIT_USER_REQUEST)
    try:
        _invoke(app, model, "explicit-write-run")
    finally:
        reset_memory_write_intent(intent_token)
        reset_permission_confirmation_handler(confirmation_token)
    prompt = _invoke(app, model, "explicit-read-run")

    write_events = _events(runtime, "explicit-write-run")
    events = _events(runtime, "explicit-read-run")
    retrieval = next(event for event in events if event["event"] == "memory_retrieval")
    assembly = next(event for event in events if event["event"] == "context_assembly")
    assert _global_store(runtime).profile_path.read_bytes() == secret.encode("utf-8")
    assert secret in prompt
    assert "来源通道: user_profile_store" in prompt
    assert retrieval["selected_scope"] == "global"
    assert retrieval["blocked"] is False
    assert assembly["memory_record_count"] == 1
    assert assembly["historical_context_framed"] is True
    assert set(builtins.save_user_profile.args) == {"new_content"}
    for run_id, run_events in (("explicit-write-run", write_events), ("explicit-read-run", events)):
        for output in _safe_outputs(runtime, run_events, monkeypatch, run_id):
            assert secret not in output
            assert "SUMMARY_SECRET" not in output
            assert "USER_QUERY_SECRET" not in output


def test_no_intent_agent_tool_does_no_lifecycle_work_and_preserves_context(runtime, monkeypatch):
    """模型自发 Tool call 在 preflight 停止，随后真实 context 仍只看到旧 profile。"""
    old_content = "OLD_PROFILE"
    rejected_content = "MEMORY_LIFECYCLE_SECRET_X"
    _global_store(runtime).write_profile(old_content)
    before_files = sorted(path.name for path in _global_store(runtime).profile_path.parent.iterdir())
    model = _SequenceModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "save_user_profile",
                        "args": {"new_content": rejected_content},
                        "id": "unauthorized-write",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="blocked"),
            AIMessage(content="read"),
        ]
    )
    resolver_calls = []
    authorization_calls = []
    confirmations = []
    monkeypatch.setattr(agent, "get_provider", lambda **_kwargs: _Provider(model))
    monkeypatch.setattr(
        memory_lifecycle,
        "resolve_user_profile_target",
        lambda *_args: resolver_calls.append(1) or (_ for _ in ()).throw(AssertionError("resolver must not run")),
    )
    monkeypatch.setattr(
        memory_lifecycle,
        "authorize_memory_access",
        lambda *_args: authorization_calls.append(1) or (_ for _ in ()).throw(AssertionError("authorization must not run")),
    )
    app = agent.create_agent_app(tools=[builtins.save_user_profile])
    confirmation_token = set_permission_confirmation_handler(
        lambda _request, _result: confirmations.append(1) or PermissionConfirmationChoice.ALLOW_SESSION
    )
    try:
        _invoke(app, model, "no-intent-write-run")
        prompt = _invoke(app, model, "no-intent-read-run")
    finally:
        reset_permission_confirmation_handler(confirmation_token)

    assert resolver_calls == []
    assert authorization_calls == []
    assert confirmations == []
    assert _global_store(runtime).profile_path.read_bytes() == old_content.encode("utf-8")
    assert sorted(path.name for path in _global_store(runtime).profile_path.parent.iterdir()) == before_files
    assert old_content in prompt
    assert rejected_content not in prompt
    for output in _safe_outputs(runtime, _events(runtime, "no-intent-read-run"), monkeypatch, "no-intent-read-run"):
        assert rejected_content not in output


def test_session_grant_cannot_bypass_policy_and_noop_skips_write_side_effects(runtime, monkeypatch):
    """grant 只复用 changed write；无 intent 被前置拒绝，exact match 不申请 write permission。"""
    confirmations = []
    writes = []
    original_write = UserProfileStore.write_profile
    original_resolver = memory_lifecycle.resolve_user_profile_target
    allow_resolver = [True]
    monkeypatch.setattr(
        UserProfileStore,
        "write_profile",
        lambda store, content: writes.append(content) or original_write(store, content),
    )
    monkeypatch.setattr(
        memory_lifecycle,
        "resolve_user_profile_target",
        lambda *args: original_resolver(*args)
        if allow_resolver[0]
        else (_ for _ in ()).throw(AssertionError("resolver must not run")),
    )
    grants_token = set_session_permission_grants()
    confirmation_token = set_permission_confirmation_handler(
        lambda request, _result: confirmations.append(request.target) or PermissionConfirmationChoice.ALLOW_SESSION
    )
    try:
        first_intent = set_memory_write_intent(MemoryWriteIntent.EXPLICIT_USER_REQUEST)
        try:
            first = builtins.save_user_profile.invoke({"new_content": "ONE"})
        finally:
            reset_memory_write_intent(first_intent)

        allow_resolver[0] = False
        blocked = builtins.save_user_profile.invoke({"new_content": "TWO"})

        allow_resolver[0] = True
        exact_intent = set_memory_write_intent(MemoryWriteIntent.EXPLICIT_USER_REQUEST)
        try:
            exact = builtins.save_user_profile.invoke({"new_content": "ONE"})
            changed = builtins.save_user_profile.invoke({"new_content": "TWO"})
        finally:
            reset_memory_write_intent(exact_intent)
    finally:
        reset_permission_confirmation_handler(confirmation_token)
        reset_session_permission_grants(grants_token)

    assert first == exact == changed == "记忆档案已成功覆写更新。新的人设画像已生效。"
    assert blocked == "Memory write is not eligible under current policy."
    assert confirmations and len(confirmations) == 1
    assert writes == ["ONE", "TWO"]
    assert _global_store(runtime).profile_path.read_bytes() == b"TWO"


def test_write_intent_contextvar_does_not_leak_to_next_project(runtime, tmp_path):
    """Project A turn 的 explicit intent reset 后，Project B 默认仍必须在 preflight 被拒绝。"""
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    confirmation_token = set_permission_confirmation_handler(
        lambda _request, _result: PermissionConfirmationChoice.ALLOW_ONCE
    )
    token_a = set_active_project_root(project_a)
    try:
        intent_token = set_memory_write_intent(MemoryWriteIntent.EXPLICIT_USER_REQUEST)
        try:
            first = builtins.save_user_profile.invoke({"new_content": "PROJECT_A"})
        finally:
            reset_memory_write_intent(intent_token)
    finally:
        reset_active_project_root(token_a)

    token_b = set_active_project_root(project_b)
    try:
        second = builtins.save_user_profile.invoke({"new_content": "PROJECT_B"})
        project_b_store = get_user_profile_store(runtime.memory_dir)
    finally:
        reset_active_project_root(token_b)
        reset_permission_confirmation_handler(confirmation_token)

    assert "成功覆写更新" in first
    assert second == "Memory write is not eligible under current policy."
    assert not project_b_store.profile_path.parent.exists()


def test_project_materialization_clear_fallback_and_atomic_failure_preserve_context(runtime, monkeypatch, tmp_path):
    """PROJECT fallback 不参与 dedup；clear 回退 GLOBAL，replace failure 后仍保留旧 context。"""
    global_marker = "GLOBAL_MARKER"
    failed_content = "MEMORY_LIFECYCLE_SECRET_X"
    _global_store(runtime).write_profile(global_marker)
    project_root = tmp_path / "SECRET_PROJECT_PATH_Z"
    project_root.mkdir()
    project_token = set_active_project_root(project_root)
    confirmation_token = set_permission_confirmation_handler(
        lambda _request, _result: PermissionConfirmationChoice.ALLOW_ONCE
    )
    try:
        intent_token = set_memory_write_intent(MemoryWriteIntent.EXPLICIT_USER_REQUEST)
        try:
            materialized = builtins.save_user_profile.invoke({"new_content": global_marker})
        finally:
            reset_memory_write_intent(intent_token)
        project_store = get_user_profile_store(runtime.memory_dir)
        assert project_store.profile_path.read_bytes() == global_marker.encode("utf-8")

        clear_intent = set_memory_write_intent(MemoryWriteIntent.EXPLICIT_USER_REQUEST)
        try:
            cleared = builtins.save_user_profile.invoke({"new_content": ""})
        finally:
            reset_memory_write_intent(clear_intent)
        model = _SequenceModel([AIMessage(content="read")])
        monkeypatch.setattr(agent, "get_provider", lambda **_kwargs: _Provider(model))
        app = agent.create_agent_app(tools=[])
        fallback_prompt = _invoke(app, model, "project-clear-run")

        project_store.write_profile("OLD_CONTEXT")
        original_replace = user_profile.os.replace
        monkeypatch.setattr(
            user_profile.os,
            "replace",
            lambda _source, _target: (_ for _ in ()).throw(OSError("replace failed")),
        )
        failed_intent = set_memory_write_intent(MemoryWriteIntent.EXPLICIT_USER_REQUEST)
        try:
            with pytest.raises(UserProfilePersistenceError, match="^user_profile_write_failed$"):
                builtins.save_user_profile.invoke({"new_content": failed_content})
        finally:
            reset_memory_write_intent(failed_intent)
        monkeypatch.setattr(user_profile.os, "replace", original_replace)
        model = _SequenceModel([AIMessage(content="read")])
        monkeypatch.setattr(agent, "get_provider", lambda **_kwargs: _Provider(model))
        continuity_app = agent.create_agent_app(tools=[])
        continuity_prompt = _invoke(continuity_app, model, "atomic-failure-run")
    finally:
        reset_permission_confirmation_handler(confirmation_token)
        reset_active_project_root(project_token)

    assert "成功覆写更新" in materialized and "成功覆写更新" in cleared
    assert _global_store(runtime).profile_path.read_bytes() == global_marker.encode("utf-8")
    assert project_store.profile_path.read_bytes() == b"OLD_CONTEXT"
    assert global_marker in fallback_prompt
    assert "范围: global" in fallback_prompt
    assert "OLD_CONTEXT" in continuity_prompt
    assert failed_content not in continuity_prompt
    fallback = next(event for event in _events(runtime, "project-clear-run") if event["event"] == "memory_retrieval")
    assert fallback["selected_scope"] == "global"
    assert fallback["used_global_fallback"] is True
    for output in _safe_outputs(runtime, _events(runtime, "atomic-failure-run"), monkeypatch, "atomic-failure-run"):
        assert failed_content not in output
        assert "SECRET_PROJECT_PATH_Z" not in output
