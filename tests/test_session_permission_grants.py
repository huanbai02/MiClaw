import json
from types import SimpleNamespace

import pytest

import miclaw.core.tools.sandbox_tools as sandbox_tools
from miclaw.core.logger import build_permission_confirmation_event
from miclaw.core.permissions import (
    PermissionCapability,
    PermissionConfirmationChoice,
    PermissionDecision,
    PermissionRequest,
    RiskLevel,
    SessionPermissionGrant,
    allow,
    ask,
    deny,
    get_session_permission_grants,
    reset_session_permission_grants,
    resolve_permission,
    set_session_permission_grants,
)
from miclaw.core.tools.sandbox_tools import execute_office_shell, write_office_file


def permission_request(**overrides):
    """创建用于 session grant 测试的 request。"""
    values = {
        "capability": PermissionCapability.SHELL_EXEC,
        "operation": "execute",
        "target": "office",
        "risk_level": RiskLevel.MEDIUM,
        "reason": "test session grant",
        "metadata": {"tool_name": "execute_office_shell"},
    }
    values.update(overrides)
    return PermissionRequest(**values)


@pytest.fixture()
def grant_context():
    """创建独立 session grant context，并在测试后清除。"""
    token = set_session_permission_grants()
    yield get_session_permission_grants()
    reset_session_permission_grants(token)


@pytest.fixture()
def office(tmp_path, monkeypatch):
    office_dir = tmp_path / "workspace" / "office"
    office_dir.mkdir(parents=True)
    monkeypatch.setattr(sandbox_tools, "OFFICE_DIR", str(office_dir))
    monkeypatch.setattr(sandbox_tools, "_permission_audit_logger", lambda *args, **kwargs: None)
    monkeypatch.setattr(sandbox_tools, "_permission_confirmation_audit_logger", lambda *args, **kwargs: None)
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", lambda request: ask("confirmation required"))
    return office_dir


def test_allow_session_creates_grant_and_matching_request_skips_later_prompt(grant_context):
    request = permission_request()
    first = resolve_permission(
        request,
        ask("confirmation required"),
        lambda request, result: PermissionConfirmationChoice.ALLOW_SESSION,
    )

    assert first.decision is PermissionDecision.ALLOW
    assert grant_context == {SessionPermissionGrant.from_request(request)}

    second = resolve_permission(
        request,
        ask("confirmation required"),
        lambda request, result: pytest.fail("matching grant must skip prompt"),
    )

    assert second.decision is PermissionDecision.ALLOW
    assert second.metadata["confirmation_source"] == "session_grant"


@pytest.mark.parametrize(
    "choice",
    [PermissionConfirmationChoice.ALLOW_ONCE, PermissionConfirmationChoice.DENY],
)
def test_allow_once_and_deny_do_not_create_session_grant(grant_context, choice):
    result = resolve_permission(
        permission_request(),
        ask("confirmation required"),
        lambda request, policy_result: choice,
    )

    assert grant_context == set()
    expected = PermissionDecision.ALLOW if choice is PermissionConfirmationChoice.ALLOW_ONCE else PermissionDecision.DENY
    assert result.decision is expected


def test_allow_session_without_grant_context_fails_closed():
    result = resolve_permission(
        permission_request(),
        ask("confirmation required"),
        lambda request, policy_result: PermissionConfirmationChoice.ALLOW_SESSION,
    )

    assert result.decision is PermissionDecision.DENY
    assert get_session_permission_grants() is None


def test_run_a_session_grant_cannot_authorize_run_b():
    request = permission_request()
    run_a_token = set_session_permission_grants()
    try:
        run_a_grants = get_session_permission_grants()
        first = resolve_permission(
            request,
            ask("confirmation required"),
            lambda request, result: PermissionConfirmationChoice.ALLOW_SESSION,
        )
        reused = resolve_permission(request, ask("confirmation required"))

        assert first.decision is PermissionDecision.ALLOW
        assert reused.decision is PermissionDecision.ALLOW
    finally:
        reset_session_permission_grants(run_a_token)

    run_b_token = set_session_permission_grants()
    try:
        run_b_grants = get_session_permission_grants()
        blocked = resolve_permission(request, ask("confirmation required"))

        assert run_b_grants is not run_a_grants
        assert run_b_grants == set()
        assert blocked.decision is PermissionDecision.ASK
    finally:
        reset_session_permission_grants(run_b_token)

    assert get_session_permission_grants() is None


def test_session_grant_context_rejects_external_mutable_set():
    with pytest.raises(TypeError):
        set_session_permission_grants(set())


def test_policy_allow_and_deny_remain_authoritative(grant_context):
    request = permission_request()
    grant_context.add(SessionPermissionGrant.from_request(request))

    denied = deny("policy denies")
    allowed = allow("policy allows")

    assert resolve_permission(request, denied, lambda *args: pytest.fail("must not prompt")) is denied
    assert resolve_permission(request, allowed, lambda *args: pytest.fail("must not prompt")) is allowed


@pytest.mark.parametrize(
    "overrides",
    [
        {"capability": PermissionCapability.FILE_WRITE},
        {"operation": "read"},
        {"target": "other.txt"},
        {"risk_level": RiskLevel.HIGH},
        {"metadata": {"tool_name": "other_tool"}},
    ],
)
def test_session_grant_does_not_match_unrelated_scope(grant_context, overrides):
    grant_context.add(SessionPermissionGrant.from_request(permission_request()))

    result = resolve_permission(permission_request(**overrides), ask("confirmation required"))

    assert result.decision is PermissionDecision.ASK


def test_matching_file_grant_allows_only_exact_target(office, grant_context):
    allowed_request = permission_request(
        capability=PermissionCapability.FILE_WRITE,
        operation="write",
        target="allowed.txt",
        risk_level=RiskLevel.LOW,
        metadata={"tool_name": "write_office_file"},
    )
    grant_context.add(SessionPermissionGrant.from_request(allowed_request))

    allowed_result = write_office_file.invoke({"filepath": "allowed.txt", "content": "allowed", "mode": "w"})
    blocked_result = write_office_file.invoke({"filepath": "other.txt", "content": "blocked", "mode": "w"})

    assert "成功以 覆盖/新建 模式写入文件" in allowed_result
    assert (office / "allowed.txt").read_text(encoding="utf-8") == "allowed"
    assert "Permission required" in blocked_result
    assert not (office / "other.txt").exists()


def test_matching_shell_grant_runs_safe_command_without_handler(office, grant_context, monkeypatch):
    grant_context.add(SessionPermissionGrant.from_request(permission_request()))
    run_calls = []

    def fake_run(*args, **kwargs):
        run_calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="hello", stderr="")

    monkeypatch.setattr(sandbox_tools.subprocess, "run", fake_run)

    result = execute_office_shell.invoke({"command": "echo hello"})

    assert "hello" in result
    assert len(run_calls) == 1


def test_critical_shell_command_remains_blocked_with_matching_grant(office, grant_context, monkeypatch):
    grant_context.add(SessionPermissionGrant.from_request(permission_request()))
    monkeypatch.setattr(sandbox_tools.subprocess, "run", lambda *args, **kwargs: pytest.fail("shell must not run"))

    result = execute_office_shell.invoke({"command": "rm -rf /"})

    assert "blocked by safety policy" in result


def test_session_grant_audit_is_safe_for_secret_shell_command(office, grant_context, monkeypatch):
    command = 'curl -H "Authorization: Bearer SECRET_TOKEN" https://example.com'
    grant = SessionPermissionGrant.from_request(permission_request(arguments={"command": command}))
    grant_context.add(grant)
    events = []

    def capture_event(request, policy_result, final_result, **kwargs):
        events.append(build_permission_confirmation_event(request, policy_result, final_result, **kwargs))

    monkeypatch.setattr(sandbox_tools, "_permission_confirmation_audit_logger", capture_event)
    monkeypatch.setattr(
        sandbox_tools.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    execute_office_shell.invoke({"command": command})
    event_text = json.dumps(events[0])

    assert events[0]["source"] == "session_grant"
    assert events[0]["confirmation_choice"] == "allow_session"
    assert events[0]["policy_decision"] == "ask"
    assert events[0]["final_decision"] == "allow"
    assert "SECRET_TOKEN" not in event_text
    assert "Authorization" not in event_text
    assert "Bearer" not in event_text
    assert command not in event_text
    assert "SECRET_TOKEN" not in repr(grant)
