"""验证 scoped user-profile 复用既有 permission/confirmation/grant 边界。"""

import json
from pathlib import Path

import pytest

import entry.cli as cli
import miclaw.core.memory_permissions as memory_permissions
from miclaw.core import user_profile
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from miclaw.core.logger import (
    JSONLEventLogger,
    build_permission_confirmation_event,
    build_permission_decision_event,
)
from miclaw.core.memory import MemoryScopeKind
from miclaw.core.memory_permissions import (
    ResolvedUserProfileTarget,
    authorize_memory_access,
    authorize_user_profile_write,
    build_memory_permission_request,
    read_authorized_user_profile,
)
from miclaw.core.memory_lifecycle import (
    MemoryWriteIntent,
    reset_memory_write_intent,
    set_memory_write_intent,
)
from miclaw.core.permissions import (
    PermissionCapability,
    PermissionConfirmationChoice,
    PermissionDecision,
    PermissionRequest,
    RiskLevel,
    allow,
    deny,
    get_session_permission_grants,
    reset_permission_confirmation_handler,
    reset_session_permission_grants,
    set_permission_confirmation_handler,
    set_session_permission_grants,
)
from miclaw.core.tools import builtins
from miclaw.core.trace import TraceContext, reset_trace_context, set_current_trace_context
from miclaw.core.user_profile import UserProfileStore, get_user_profile_store
from miclaw.core.workspace import reset_active_project_root, set_active_project_root


class _CaptureModel:
    """捕获 Agent 的 SystemMessage，避免真实模型调用。"""

    def __init__(self) -> None:
        self.inputs = []

    def invoke(self, messages):
        self.inputs.append(messages)
        return AIMessage(content="memory permission test")


class _Provider:
    """提供最小 bind_tools 接口。"""

    def __init__(self, model: _CaptureModel) -> None:
        self.model = model

    def bind_tools(self, _tools):
        return self.model


@pytest.fixture(autouse=True)
def disable_global_memory_permission_audit(monkeypatch):
    """避免 focused tests 写入真实 audit log；审计测试会显式替换 logger。"""
    monkeypatch.setattr(memory_permissions, "_permission_audit_logger", lambda *args, **kwargs: None)
    monkeypatch.setattr(memory_permissions, "_permission_confirmation_audit_logger", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def bind_explicit_write_intent_for_permission_regressions():
    """既有写入 permission 测试只验证第二道 gate，显式绑定第一道 policy intent。"""
    token = set_memory_write_intent(MemoryWriteIntent.EXPLICIT_USER_REQUEST)
    try:
        yield
    finally:
        reset_memory_write_intent(token)


def _project_context(project_path: Path):
    """激活显式 PROJECT root，并返回 reset token。"""
    project_path.mkdir()
    return set_active_project_root(project_path)


def _global_store(memory_dir: Path) -> UserProfileStore:
    """构造直接 GLOBAL store，用于设置 fallback fixture 内容。"""
    return UserProfileStore(memory_dir / "user_profile.md")


def test_valid_global_and_project_memory_policy_matrix(tmp_path):
    """有效 read 默认 LOW ALLOW；有效 write 默认 MEDIUM ASK。"""
    memory_dir = tmp_path / "memory"
    global_target = ResolvedUserProfileTarget(_global_store(memory_dir))
    global_read = build_memory_permission_request(global_target, "read", "user_profile_context")
    global_write = build_memory_permission_request(global_target, "update", "save_user_profile")

    assert memory_permissions.evaluate_permission(global_read).decision is PermissionDecision.ALLOW
    assert memory_permissions.evaluate_permission(global_write).decision is PermissionDecision.ASK
    assert global_read.risk_level is RiskLevel.LOW
    assert global_write.risk_level is RiskLevel.MEDIUM

    token = _project_context(tmp_path / "project")
    try:
        project_target = ResolvedUserProfileTarget(get_user_profile_store(memory_dir))
        project_read = build_memory_permission_request(project_target, "read", "user_profile_context")
        project_write = build_memory_permission_request(project_target, "update", "save_user_profile")
    finally:
        reset_active_project_root(token)

    assert project_target.store.scope.kind is MemoryScopeKind.PROJECT
    assert memory_permissions.evaluate_permission(project_read).decision is PermissionDecision.ALLOW
    assert memory_permissions.evaluate_permission(project_write).decision is PermissionDecision.ASK


@pytest.mark.parametrize(
    "permission_request",
    [
        PermissionRequest(
            capability=PermissionCapability.MEMORY_READ,
            operation="read",
            target="user-profile",
            metadata={"memory_kind": "unknown", "memory_scope": "global", "workspace_scope": "global"},
        ),
        PermissionRequest(
            capability=PermissionCapability.MEMORY_WRITE,
            operation="update",
            target="user-profile::not-a-project-id",
            metadata={"memory_kind": "user_profile", "memory_scope": "project", "workspace_scope": "project"},
        ),
        PermissionRequest(
            capability=PermissionCapability.MEMORY_READ,
            operation="list",
            target="user-profile",
            metadata={"memory_kind": "user_profile", "memory_scope": "global", "workspace_scope": "global"},
        ),
        PermissionRequest(
            capability=PermissionCapability.MEMORY_READ,
            operation="read",
            target="user-profile",
            metadata={"memory_kind": "user_profile", "memory_scope": "external", "workspace_scope": "external"},
        ),
    ],
)
def test_invalid_memory_requests_are_denied(permission_request):
    """未知 kind/operation/scope 或 malformed identity 必须 fail closed。"""
    assert memory_permissions.evaluate_permission(permission_request).decision is PermissionDecision.DENY


@pytest.mark.parametrize("scope_id", [[], {}, 0, False, ["x"], {"x": "y"}, 1, True, 1.5, ()])
def test_global_memory_scope_id_malformed_types_are_denied(scope_id):
    """GLOBAL scope_id 只接受 None 或精确空字符串，且不对不可哈希值崩溃。"""
    request = PermissionRequest(
        capability=PermissionCapability.MEMORY_READ,
        operation="read",
        target="user-profile",
        risk_level=RiskLevel.LOW,
        metadata={
            "memory_kind": "user_profile",
            "memory_scope": "global",
            "memory_scope_id": scope_id,
            "workspace_scope": "global",
        },
    )

    assert memory_permissions.evaluate_permission(request).decision is PermissionDecision.DENY


@pytest.mark.parametrize("scope_id", [[], {}, 0, False, "", None])
def test_project_memory_scope_id_malformed_types_are_denied(scope_id):
    """PROJECT scope_id 必须是非空字符串，malformed metadata 一律 DENY。"""
    request = PermissionRequest(
        capability=PermissionCapability.MEMORY_READ,
        operation="read",
        target=f"user-profile::{'a' * 24}",
        risk_level=RiskLevel.LOW,
        metadata={
            "memory_kind": "user_profile",
            "memory_scope": "project",
            "memory_scope_id": scope_id,
            "workspace_scope": "project",
        },
    )

    assert memory_permissions.evaluate_permission(request).decision is PermissionDecision.DENY


def test_project_memory_non_empty_opaque_scope_id_remains_allowed():
    """合法 opaque PROJECT identity 继续遵循既有 read/write policy。"""
    scope_id = "a" * 24
    metadata = {
        "memory_kind": "user_profile",
        "memory_scope": "project",
        "memory_scope_id": scope_id,
        "workspace_scope": "project",
    }
    read_request = PermissionRequest(
        capability=PermissionCapability.MEMORY_READ,
        operation="read",
        target=f"user-profile::{scope_id}",
        risk_level=RiskLevel.LOW,
        metadata=metadata,
    )
    write_request = PermissionRequest(
        capability=PermissionCapability.MEMORY_WRITE,
        operation="update",
        target=f"user-profile::{scope_id}",
        risk_level=RiskLevel.MEDIUM,
        metadata=metadata,
    )

    assert memory_permissions.evaluate_permission(read_request).decision is PermissionDecision.ALLOW
    assert memory_permissions.evaluate_permission(write_request).decision is PermissionDecision.ASK


def test_project_read_authorizes_project_then_global_fallback_separately(tmp_path, monkeypatch):
    """PROJECT missing 时 GLOBAL fallback 是第二次独立 read permission。"""
    memory_dir = tmp_path / "memory"
    _global_store(memory_dir).write_profile("GLOBAL_MARKER")
    token = _project_context(tmp_path / "project")
    requests = []
    monkeypatch.setattr(
        memory_permissions,
        "_permission_evaluator",
        lambda request: requests.append(request) or allow("allowed", request.risk_level),
    )
    try:
        record = read_authorized_user_profile(memory_dir)
    finally:
        reset_active_project_root(token)

    assert record is not None
    assert record.content == "GLOBAL_MARKER"
    assert record.scope.kind is MemoryScopeKind.GLOBAL
    assert [request.target for request in requests] == [
        requests[0].target,
        "user-profile",
    ]
    assert requests[0].target.startswith("user-profile::")


def test_project_read_deny_does_not_read_or_fallback(tmp_path, monkeypatch):
    """PROJECT read 被拒绝时不能读取正文，也不能借 GLOBAL fallback 绕过。"""
    memory_dir = tmp_path / "memory"
    _global_store(memory_dir).write_profile("GLOBAL_SECRET_MARKER")
    token = _project_context(tmp_path / "project")
    read_calls = []
    original_read = UserProfileStore.read_primary_record
    monkeypatch.setattr(
        memory_permissions,
        "_permission_evaluator",
        lambda request: deny("blocked", request.risk_level),
    )
    monkeypatch.setattr(
        UserProfileStore,
        "read_primary_record",
        lambda store: read_calls.append(store.profile_path) or original_read(store),
    )
    try:
        record = read_authorized_user_profile(memory_dir)
    finally:
        reset_active_project_root(token)

    assert record is None
    assert read_calls == []


def test_project_existing_profile_requires_no_global_read_authorization(tmp_path, monkeypatch):
    """存在 PROJECT record 时只授权/读取 PROJECT 一次。"""
    memory_dir = tmp_path / "memory"
    token = _project_context(tmp_path / "project")
    try:
        project_store = get_user_profile_store(memory_dir)
        project_store.write_profile("PROJECT_MARKER")
        requests = []
        monkeypatch.setattr(
            memory_permissions,
            "_permission_evaluator",
            lambda request: requests.append(request) or allow("allowed", request.risk_level),
        )
        record = read_authorized_user_profile(memory_dir)
    finally:
        reset_active_project_root(token)

    assert record is not None
    assert record.content == "PROJECT_MARKER"
    assert len(requests) == 1
    assert requests[0].target.startswith("user-profile::")


def test_global_fallback_denial_returns_no_memory_after_project_missing(tmp_path, monkeypatch):
    """PROJECT 已允许但 GLOBAL fallback 被拒绝时不得读取 GLOBAL content。"""
    memory_dir = tmp_path / "memory"
    _global_store(memory_dir).write_profile("GLOBAL_SECRET_MARKER")
    token = _project_context(tmp_path / "project")
    read_paths = []
    original_read = UserProfileStore.read_primary_record

    def evaluate(request):
        return allow("allowed", request.risk_level) if request.target.startswith("user-profile::") else deny("blocked", request.risk_level)

    monkeypatch.setattr(memory_permissions, "_permission_evaluator", evaluate)
    monkeypatch.setattr(
        UserProfileStore,
        "read_primary_record",
        lambda store: read_paths.append(store.profile_path) or original_read(store),
    )
    try:
        record = read_authorized_user_profile(memory_dir)
    finally:
        reset_active_project_root(token)

    assert record is None
    assert len(read_paths) == 1


def test_project_target_mismatch_with_active_project_fails_closed(tmp_path):
    """已解析的 PROJECT A target 不能在 PROJECT B context 下复用。"""
    memory_dir = tmp_path / "memory"
    token_a = _project_context(tmp_path / "project-a")
    try:
        project_a_target = ResolvedUserProfileTarget(get_user_profile_store(memory_dir))
    finally:
        reset_active_project_root(token_a)
    token_b = _project_context(tmp_path / "project-b")
    try:
        request = build_memory_permission_request(project_a_target, "update", "save_user_profile")
    finally:
        reset_active_project_root(token_b)

    assert request.target == "invalid-memory-target"
    assert memory_permissions.evaluate_permission(request).decision is PermissionDecision.DENY


def test_write_ask_without_handler_has_zero_persistence_side_effect(tmp_path, monkeypatch):
    """PROJECT write 未确认时不得创建 profile 目录、temp 或 target。"""
    memory_dir = tmp_path / "memory"
    project_path = tmp_path / "project"
    project_path.mkdir()
    monkeypatch.setattr(builtins, "MEMORY_DIR", str(memory_dir))
    token = set_active_project_root(project_path)
    try:
        result = builtins.save_user_profile.invoke({"new_content": "PRIVATE_WRITE_MARKER"})
        project_store = get_user_profile_store(memory_dir)
    finally:
        reset_active_project_root(token)

    assert "Permission required" in result
    assert not project_store.profile_path.exists()
    assert not project_store.profile_path.parent.exists()
    assert not (memory_dir / "user_profile.md").exists()


def test_global_write_ask_without_handler_has_zero_persistence_side_effect(tmp_path, monkeypatch):
    """OFFICE/GLOBAL update 同样必须在 final ALLOW 前保持零 persistence side effect。"""
    memory_dir = tmp_path / "memory"
    monkeypatch.setattr(builtins, "MEMORY_DIR", str(memory_dir))

    result = builtins.save_user_profile.invoke({"new_content": "PRIVATE_GLOBAL_WRITE_MARKER"})

    assert "Permission required" in result
    assert not (memory_dir / "user_profile.md").exists()
    assert not memory_dir.exists()


def test_write_allow_once_persists_only_after_final_allow(tmp_path, monkeypatch):
    """ALLOW_ONCE 后才进入 PR35 atomic Store write。"""
    memory_dir = tmp_path / "memory"
    monkeypatch.setattr(builtins, "MEMORY_DIR", str(memory_dir))
    token = set_permission_confirmation_handler(
        lambda request, result: PermissionConfirmationChoice.ALLOW_ONCE
    )
    try:
        result = builtins.save_user_profile.invoke({"new_content": "GLOBAL_WRITE_MARKER"})
    finally:
        reset_permission_confirmation_handler(token)

    assert "成功覆写更新" in result
    assert _global_store(memory_dir).read_profile() == "GLOBAL_WRITE_MARKER"


def test_invalid_confirmation_blocks_write_without_creating_profile(tmp_path, monkeypatch):
    """ASK handler 返回无效值时 fail closed，写入保持零 side effect。"""
    memory_dir = tmp_path / "memory"
    monkeypatch.setattr(builtins, "MEMORY_DIR", str(memory_dir))
    token = set_permission_confirmation_handler(lambda request, result: PermissionDecision.ASK)
    try:
        result = builtins.save_user_profile.invoke({"new_content": "PRIVATE_WRITE_MARKER"})
    finally:
        reset_permission_confirmation_handler(token)

    assert "Permission denied" in result
    assert not (memory_dir / "user_profile.md").exists()


def test_session_grants_are_isolated_by_global_and_project_memory_identity(tmp_path, monkeypatch):
    """PROJECT A 的 ALLOW_SESSION 不得授权 PROJECT B 或 GLOBAL update。"""
    memory_dir = tmp_path / "memory"
    monkeypatch.setattr(builtins, "MEMORY_DIR", str(memory_dir))
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    grant_token = set_session_permission_grants()
    confirmations = []
    handler_token = set_permission_confirmation_handler(
        lambda request, result: confirmations.append(request.target) or PermissionConfirmationChoice.ALLOW_SESSION
    )
    try:
        token_a = set_active_project_root(project_a)
        try:
            first = builtins.save_user_profile.invoke({"new_content": "PROJECT_A_ONE"})
            reused = builtins.save_user_profile.invoke({"new_content": "PROJECT_A_TWO"})
        finally:
            reset_active_project_root(token_a)

        token_b = set_active_project_root(project_b)
        try:
            project_b_auth = authorize_user_profile_write(memory_dir)
        finally:
            reset_active_project_root(token_b)
        global_auth = authorize_user_profile_write(memory_dir)
        grant_targets = {grant.target_scope for grant in get_session_permission_grants() or set()}
    finally:
        reset_permission_confirmation_handler(handler_token)
        reset_session_permission_grants(grant_token)

    assert "成功覆写更新" in first
    assert "成功覆写更新" in reused
    assert len(confirmations) == 3
    assert project_b_auth.final_result.decision is PermissionDecision.ALLOW
    assert global_auth.final_result.decision is PermissionDecision.ALLOW
    assert confirmations[0] not in {confirmations[1], confirmations[2]}
    assert confirmations[1] != confirmations[2]
    assert grant_targets == set(confirmations)


def test_allow_once_requires_confirmation_again_for_same_project_target(tmp_path, monkeypatch):
    """ALLOW_ONCE 不创建 session grant。"""
    memory_dir = tmp_path / "memory"
    project_path = tmp_path / "project"
    project_path.mkdir()
    monkeypatch.setattr(builtins, "MEMORY_DIR", str(memory_dir))
    confirmations = []
    handler_token = set_permission_confirmation_handler(
        lambda request, result: confirmations.append(request.target) or PermissionConfirmationChoice.ALLOW_ONCE
    )
    token = set_active_project_root(project_path)
    try:
        builtins.save_user_profile.invoke({"new_content": "PROJECT_ONE"})
        builtins.save_user_profile.invoke({"new_content": "PROJECT_TWO"})
    finally:
        reset_active_project_root(token)
        reset_permission_confirmation_handler(handler_token)

    assert len(confirmations) == 2


def test_memory_permission_prompt_and_audit_are_content_and_path_safe(tmp_path, monkeypatch):
    """prompt/audit 仅包含 logical target、scope 与 trace，不含 profile body 或路径。"""
    log_file = tmp_path / "memory-permission.jsonl"
    logger = JSONLEventLogger(log_file=log_file)
    memory_dir = tmp_path / "workspace" / "memory"
    project_path = tmp_path / "project"
    project_path.mkdir()

    def log_decision(request, result, **kwargs):
        event = build_permission_decision_event(request, result, **kwargs)
        logger.log_event("system", event["event_type"], **event)

    def log_confirmation(request, policy_result, final_result, **kwargs):
        event = build_permission_confirmation_event(request, policy_result, final_result, **kwargs)
        logger.log_event("system", event["event_type"], **event)

    monkeypatch.setattr(memory_permissions, "_permission_audit_logger", log_decision)
    monkeypatch.setattr(memory_permissions, "_permission_confirmation_audit_logger", log_confirmation)
    monkeypatch.setattr(builtins, "MEMORY_DIR", str(memory_dir))
    trace_token = set_current_trace_context(TraceContext(run_id="memory-run"))
    project_token = set_active_project_root(project_path)
    confirmation_token = set_permission_confirmation_handler(
        lambda request, result: PermissionConfirmationChoice.ALLOW_ONCE
    )
    try:
        target = memory_permissions.resolve_user_profile_target(memory_dir)
        request = build_memory_permission_request(target, "update", "save_user_profile")
        prompt = cli.format_permission_confirmation_prompt(request, memory_permissions.evaluate_permission(request))
        result = builtins.save_user_profile.invoke({"new_content": "PRIVATE_PROFILE_MARKER"})
    finally:
        reset_permission_confirmation_handler(confirmation_token)
        reset_active_project_root(project_token)
        reset_trace_context(trace_token)
    logger.log_queue.join()
    logger.shutdown()

    events = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    serialized = json.dumps(events)
    assert "成功覆写更新" in result
    assert [event["event_type"] for event in events] == [
        "permission_decision",
        "permission_confirmation",
    ]
    assert [event["run_id"] for event in events] == ["memory-run", "memory-run"]
    assert [event["step_id"] for event in events] == [1, 2]
    assert request.target in prompt
    assert "memory_write" in prompt
    assert "project" in prompt
    for marker in ("PRIVATE_PROFILE_MARKER", str(project_path), str(memory_dir), "user_profile.md"):
        assert marker not in prompt
        assert marker not in serialized


def test_agent_denied_memory_read_uses_existing_empty_profile_fallback(tmp_path, monkeypatch):
    """Agent 在 MEMORY_READ DENY 时不读 profile content，继续使用“暂无记录”。"""
    from miclaw.core import agent

    memory_dir = tmp_path / "memory"
    _global_store(memory_dir).write_profile("PROFILE_SECRET_MARKER")
    model = _CaptureModel()
    monkeypatch.setattr(agent, "MEMORY_DIR", str(memory_dir))
    monkeypatch.setattr(agent, "audit_logger", type("_Audit", (), {"log_event": lambda *args, **kwargs: None})())
    monkeypatch.setattr(agent, "get_provider", lambda **_kwargs: _Provider(model))
    monkeypatch.setattr(memory_permissions, "_permission_evaluator", lambda request: deny("blocked", request.risk_level))

    app = agent.create_agent_app(tools=[])
    app.invoke({"messages": [HumanMessage(content="hello")], "summary": ""})
    prompt = next(message.content for message in model.inputs[0] if isinstance(message, SystemMessage))

    assert "暂无记录" in prompt
    assert "PROFILE_SECRET_MARKER" not in prompt
