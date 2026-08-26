"""验证长期 user-profile 写入的 lifecycle eligibility 与 permission 双 gate。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from miclaw.core import agent, memory_lifecycle, memory_permissions
from miclaw.core.memory import MemoryKind, MemoryScope, MemoryScopeKind
from miclaw.core.memory_lifecycle import (
    MemoryWriteIntent,
    MemoryWriteRequest,
    MemoryWriteSource,
    evaluate_memory_write_preflight,
    evaluate_memory_write_policy,
    reset_memory_write_intent,
    set_memory_write_intent,
    write_user_profile_with_policy,
)
from miclaw.core.memory_retrieval import MemoryRetrievalRequest, MemoryRetriever
from miclaw.core.permissions import (
    PermissionConfirmationChoice,
    PermissionDecision,
    reset_permission_confirmation_handler,
    reset_session_permission_grants,
    set_permission_confirmation_handler,
    set_session_permission_grants,
)
from miclaw.core.tools import builtins
from miclaw.core.user_profile import UserProfileRoutingError, UserProfileStore, get_user_profile_store
from miclaw.core.workspace import reset_active_project_root, set_active_project_root


class _SequentialModel:
    """按顺序生成 tool call 与最终回复，驱动真实 Agent ToolNode。"""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.inputs = []

    def invoke(self, messages):
        self.inputs.append(messages)
        return self.responses.pop(0)


class _Provider:
    """提供 create_agent_app 所需的最小 bind_tools 接口。"""

    def __init__(self, model: _SequentialModel) -> None:
        self.model = model

    def bind_tools(self, _tools):
        return self.model


class _NoopAuditLogger:
    """避免 policy tests 写入真实 JSONL。"""

    def log_event(self, **_kwargs) -> None:
        return None


def _request(
    *,
    kind=MemoryKind.USER_PROFILE,
    scope=MemoryScope(MemoryScopeKind.GLOBAL),
    source=MemoryWriteSource.AGENT_TOOL,
    content="profile",
    intent=MemoryWriteIntent.EXPLICIT_USER_REQUEST,
):
    """构造 policy unit test 使用的 write request。"""
    return MemoryWriteRequest(kind, scope, source, content, intent)


def _global_store(memory_dir) -> UserProfileStore:
    """构造 GLOBAL profile store。"""
    return UserProfileStore(memory_dir / "user_profile.md")


def test_policy_allows_only_explicit_user_profile_writes_and_empty_clear():
    """空内容可表达显式清空；缺失/伪造 intent 一律不具资格。"""
    explicit = evaluate_memory_write_policy(_request(content=""))
    missing = evaluate_memory_write_policy(_request(intent=None))
    forged = evaluate_memory_write_policy(_request(intent="explicit_user_request"))

    assert (explicit.eligible, explicit.reason_code) == (True, "explicit_user_request")
    assert (missing.eligible, missing.reason_code) == (False, "unsupported_write_intent")
    assert (forged.eligible, forged.reason_code) == (False, "unsupported_write_intent")


def test_tool_contract_exposes_policy_but_not_model_controlled_eligibility_fields():
    """模型可见 contract 说明 policy/permission，却不提供自证 intent 或 scope 参数。"""
    description = builtins.save_user_profile.description

    assert "显式用户请求" in description
    assert "权限确认" in description
    assert set(builtins.save_user_profile.args) == {"new_content"}
    for forbidden in ("intent", "explicit", "user_requested", "scope", "project_id", "path"):
        assert forbidden not in builtins.save_user_profile.args


def test_agent_and_tool_contract_only_allow_explicit_user_requested_writes():
    """模型可见 instruction 不再鼓励根据自主推断持久化长期画像。"""
    assert "只有当用户明确要求记住、保存、更新或清除长期画像时" in agent.BASE_SYSTEM_PROMPT
    assert "不得仅根据你自行推断" in agent.BASE_SYSTEM_PROMPT
    assert "仅当用户明确要求记住、保存、更新或清除长期画像时" in builtins.save_user_profile.description
    assert "不得仅根据模型自行推断" in builtins.save_user_profile.description


@pytest.mark.parametrize(
    ("write_request", "reason"),
    [
        (_request(kind="user_profile"), "unsupported_memory_kind"),
        (_request(scope=SimpleNamespace(kind=MemoryScopeKind.GLOBAL, scope_id=None)), "invalid_scope"),
        (_request(content=True), "invalid_content"),
        (_request(content=1), "invalid_content"),
        (_request(content=[]), "invalid_content"),
        (_request(source="agent_tool"), "invalid_source"),
        (SimpleNamespace(), "invalid_write_request"),
    ],
)
def test_policy_fails_closed_for_malformed_requests(write_request, reason):
    """policy 不执行隐式 coercion，异常 kind/scope/content/source 均稳定拒绝。"""
    result = evaluate_memory_write_policy(write_request)

    assert result.eligible is False
    assert result.reason_code == reason


def test_missing_intent_stops_before_permission_or_filesystem(tmp_path, monkeypatch):
    """默认无 ContextVar intent 时没有 confirmation、grant、mkdir 或写入。"""
    memory_dir = tmp_path / "memory"
    permission_calls = []
    monkeypatch.setattr(
        memory_lifecycle,
        "resolve_user_profile_target",
        lambda *_args: (_ for _ in ()).throw(AssertionError("resolver must not run")),
    )
    monkeypatch.setattr(
        memory_lifecycle,
        "authorize_memory_access",
        lambda *args: permission_calls.append(args) or (_ for _ in ()).throw(AssertionError("permission must not run")),
    )

    execution = write_user_profile_with_policy(memory_dir, "MODEL_GUESS")

    assert execution.policy_result.eligible is False
    assert execution.authorization is None
    assert permission_calls == []
    assert not memory_dir.exists()


@pytest.mark.parametrize(
    ("content", "intent", "reason"),
    [
        ("valid", None, "unsupported_write_intent"),
        ("valid", "invalid-intent", "unsupported_write_intent"),
        ([], MemoryWriteIntent.EXPLICIT_USER_REQUEST, "invalid_content"),
        ({}, MemoryWriteIntent.EXPLICIT_USER_REQUEST, "invalid_content"),
        (True, MemoryWriteIntent.EXPLICIT_USER_REQUEST, "invalid_content"),
        (1, MemoryWriteIntent.EXPLICIT_USER_REQUEST, "invalid_content"),
        (None, MemoryWriteIntent.EXPLICIT_USER_REQUEST, "invalid_content"),
    ],
)
def test_preflight_denial_never_resolves_target_or_authorizes(tmp_path, monkeypatch, content, intent, reason):
    """缺失/伪造 intent 或 malformed content 在 resolver、permission 前稳定拒绝。"""
    resolver_calls = []
    permission_calls = []
    monkeypatch.setattr(memory_lifecycle, "resolve_user_profile_target", lambda *_args: resolver_calls.append(1))
    monkeypatch.setattr(memory_lifecycle, "authorize_memory_access", lambda *_args: permission_calls.append(1))
    context_token = memory_lifecycle._current_memory_write_intent.set(intent)
    try:
        execution = write_user_profile_with_policy(tmp_path / "memory", content)
    finally:
        memory_lifecycle._current_memory_write_intent.reset(context_token)

    assert execution.policy_result == evaluate_memory_write_preflight(content, intent)
    assert execution.policy_result.reason_code == reason
    assert execution.authorization is None
    assert resolver_calls == []
    assert permission_calls == []
    assert not (tmp_path / "memory").exists()


def test_valid_preflight_resolves_once_before_full_policy_and_permission(tmp_path, monkeypatch):
    """合法 explicit intent + str content 才进入一次 target resolution 与 permission。"""
    target = memory_permissions.resolve_user_profile_target(tmp_path / "memory")
    resolver_calls = []
    permission_calls = []
    monkeypatch.setattr(
        memory_lifecycle,
        "resolve_user_profile_target",
        lambda *_args: resolver_calls.append(1) or target,
    )
    monkeypatch.setattr(
        memory_lifecycle,
        "authorize_memory_access",
        lambda *_args: permission_calls.append(1)
        or SimpleNamespace(final_result=SimpleNamespace(decision=PermissionDecision.ASK)),
    )
    intent_token = set_memory_write_intent(MemoryWriteIntent.EXPLICIT_USER_REQUEST)
    try:
        execution = write_user_profile_with_policy(tmp_path / "memory", "")
    finally:
        reset_memory_write_intent(intent_token)

    assert execution.policy_result.eligible is True
    assert resolver_calls == [1]
    assert permission_calls == [1]


def test_missing_intent_preempts_target_resolution_error(tmp_path, monkeypatch):
    """target/workspace resolution error 只会在 trusted preflight 通过后出现。"""
    monkeypatch.setattr(
        memory_lifecycle,
        "resolve_user_profile_target",
        lambda *_args: (_ for _ in ()).throw(UserProfileRoutingError("unsupported_memory_scope")),
    )

    denied = write_user_profile_with_policy(tmp_path / "memory", "content")
    assert denied.policy_result.reason_code == "unsupported_write_intent"

    intent_token = set_memory_write_intent(MemoryWriteIntent.EXPLICIT_USER_REQUEST)
    try:
        with pytest.raises(UserProfileRoutingError, match="unsupported_memory_scope"):
            write_user_profile_with_policy(tmp_path / "memory", "content")
    finally:
        reset_memory_write_intent(intent_token)


def test_explicit_policy_passes_but_permission_without_handler_still_blocks(tmp_path):
    """eligible write 不等于 permission ALLOW，ASK 无 handler 不产生写入。"""
    memory_dir = tmp_path / "memory"
    intent_token = set_memory_write_intent(MemoryWriteIntent.EXPLICIT_USER_REQUEST)
    try:
        execution = write_user_profile_with_policy(memory_dir, "EXPLICIT_BUT_UNCONFIRMED")
    finally:
        reset_memory_write_intent(intent_token)

    assert execution.policy_result.eligible is True
    assert execution.authorization is not None
    assert execution.authorization.final_result.decision.value == "ask"
    assert not memory_dir.exists()


def test_explicit_allow_once_writes_and_empty_project_reveals_existing_global_fallback(tmp_path, monkeypatch):
    """显式 clear 是合法写入；empty PROJECT 不是 tombstone，仍回退 GLOBAL。"""
    memory_dir = tmp_path / "memory"
    monkeypatch.setattr(builtins, "MEMORY_DIR", str(memory_dir))
    _global_store(memory_dir).write_profile("GLOBAL_FALLBACK")
    project_root = tmp_path / "project"
    project_root.mkdir()
    project_token = set_active_project_root(project_root)
    intent_token = set_memory_write_intent(MemoryWriteIntent.EXPLICIT_USER_REQUEST)
    confirmation_token = set_permission_confirmation_handler(
        lambda _request, _result: PermissionConfirmationChoice.ALLOW_ONCE
    )
    try:
        result = builtins.save_user_profile.invoke({"new_content": ""})
        project_store = get_user_profile_store(memory_dir)
        records = MemoryRetriever(memory_dir).retrieve(MemoryRetrievalRequest((MemoryKind.USER_PROFILE,)))
    finally:
        reset_permission_confirmation_handler(confirmation_token)
        reset_memory_write_intent(intent_token)
        reset_active_project_root(project_token)

    assert "成功覆写更新" in result
    assert project_store.profile_path.exists()
    assert project_store.profile_path.read_text(encoding="utf-8") == ""
    assert records[0].content == "GLOBAL_FALLBACK"
    assert records[0].scope.kind is MemoryScopeKind.GLOBAL


def test_session_grant_never_bypasses_repeated_write_policy(tmp_path, monkeypatch):
    """ALLOW_SESSION 只复用第二道 permission；无 explicit intent 的后续写入仍先被 policy 拦截。"""
    memory_dir = tmp_path / "memory"
    monkeypatch.setattr(builtins, "MEMORY_DIR", str(memory_dir))
    confirmations = []
    grants_token = set_session_permission_grants()
    confirmation_token = set_permission_confirmation_handler(
        lambda request, _result: confirmations.append(request.target) or PermissionConfirmationChoice.ALLOW_SESSION
    )
    intent_token = set_memory_write_intent(MemoryWriteIntent.EXPLICIT_USER_REQUEST)
    try:
        first = builtins.save_user_profile.invoke({"new_content": "FIRST_EXPLICIT"})
    finally:
        reset_memory_write_intent(intent_token)
    resolver_calls = []
    monkeypatch.setattr(
        memory_lifecycle,
        "resolve_user_profile_target",
        lambda *_args: resolver_calls.append(1) or (_ for _ in ()).throw(AssertionError("resolver must not run")),
    )
    try:
        second = builtins.save_user_profile.invoke({"new_content": "SECOND_MODEL_GUESS"})
    finally:
        reset_permission_confirmation_handler(confirmation_token)
        reset_session_permission_grants(grants_token)

    assert "成功覆写更新" in first
    assert second == "Memory write is not eligible under current policy."
    assert len(confirmations) == 1
    assert resolver_calls == []
    assert _global_store(memory_dir).read_profile() == "FIRST_EXPLICIT"


def test_agent_tool_call_without_intent_cannot_self_certify_or_prompt_for_permission(tmp_path, monkeypatch):
    """真实 model ToolNode 调用没有 runtime intent 时，policy 在 confirmation 前阻断。"""
    memory_dir = tmp_path / "memory"
    model = _SequentialModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "save_user_profile",
                        "args": {"new_content": "MODEL_SELF_CERTIFIED"},
                        "id": "write-call",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    confirmations = []
    monkeypatch.setattr(agent, "MEMORY_DIR", str(memory_dir))
    monkeypatch.setattr(builtins, "MEMORY_DIR", str(memory_dir))
    monkeypatch.setattr(agent, "audit_logger", _NoopAuditLogger())
    monkeypatch.setattr(agent, "get_provider", lambda **_kwargs: _Provider(model))
    monkeypatch.setattr(memory_permissions, "_permission_audit_logger", lambda *args, **kwargs: None)
    confirmation_token = set_permission_confirmation_handler(
        lambda request, _result: confirmations.append(request) or PermissionConfirmationChoice.ALLOW_ONCE
    )
    try:
        agent.create_agent_app(tools=[builtins.save_user_profile]).invoke(
            {"messages": [HumanMessage(content="ordinary task")], "summary": ""}
        )
    finally:
        reset_permission_confirmation_handler(confirmation_token)

    assert confirmations == []
    assert not memory_dir.exists()
    assert "Memory write is not eligible under current policy." in str(model.inputs[1])
    assert set(builtins.save_user_profile.args) == {"new_content"}


def test_explicit_runtime_intent_allows_agent_tool_then_next_agent_reads_profile(tmp_path, monkeypatch):
    """host 明确绑定 intent 后，真实 Agent ToolNode 才能经过 permission 写入并被下一 node 读取。"""
    memory_dir = tmp_path / "memory"
    writer_model = _SequentialModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "save_user_profile",
                        "args": {"new_content": "PYTHON_PREFERENCE"},
                        "id": "explicit-write",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="saved"),
        ]
    )
    reader_model = _SequentialModel([AIMessage(content="read")])
    monkeypatch.setattr(agent, "MEMORY_DIR", str(memory_dir))
    monkeypatch.setattr(builtins, "MEMORY_DIR", str(memory_dir))
    monkeypatch.setattr(agent, "audit_logger", _NoopAuditLogger())
    monkeypatch.setattr(memory_permissions, "_permission_audit_logger", lambda *args, **kwargs: None)
    confirmation_token = set_permission_confirmation_handler(
        lambda _request, _result: PermissionConfirmationChoice.ALLOW_ONCE
    )
    intent_token = set_memory_write_intent(MemoryWriteIntent.EXPLICIT_USER_REQUEST)
    try:
        monkeypatch.setattr(agent, "get_provider", lambda **_kwargs: _Provider(writer_model))
        agent.create_agent_app(tools=[builtins.save_user_profile]).invoke(
            {"messages": [HumanMessage(content="请记住以后代码默认使用 Python")], "summary": ""}
        )
    finally:
        reset_memory_write_intent(intent_token)
        reset_permission_confirmation_handler(confirmation_token)

    monkeypatch.setattr(agent, "get_provider", lambda **_kwargs: _Provider(reader_model))
    agent.create_agent_app(tools=[]).invoke({"messages": [HumanMessage(content="next turn")], "summary": ""})
    prompt = str(next(message.content for message in reader_model.inputs[0] if isinstance(message, SystemMessage)))
    assert _global_store(memory_dir).read_profile() == "PYTHON_PREFERENCE"
    assert "PYTHON_PREFERENCE" in prompt
