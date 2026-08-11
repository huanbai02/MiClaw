import json
import os
import sys
from types import SimpleNamespace

import pytest

import entry.cli as cli
import miclaw.core.tools.sandbox_tools as sandbox_tools
from miclaw.core.logger import build_permission_decision_event
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
    reset_permission_confirmation_handler,
    reset_session_permission_grants,
    resolve_permission,
    set_permission_confirmation_handler,
    set_session_permission_grants,
)
from miclaw.core.tools.sandbox_tools import (
    execute_office_shell,
    list_office_files,
    read_office_file,
    write_office_file,
)
from miclaw.core.workspace import (
    WorkspaceScope,
    get_active_project_root,
    reset_active_project_root,
    set_active_project_root,
)


def scoped_request(scope: WorkspaceScope) -> PermissionRequest:
    """创建用于验证 session grant workspace 隔离的 request。"""
    return PermissionRequest(
        capability=PermissionCapability.FILE_WRITE,
        operation="write",
        target="notes.txt",
        risk_level=RiskLevel.LOW,
        reason="test scoped grant",
        metadata={
            "tool_name": "write_office_file",
            "workspace_scope": scope.value,
        },
    )


def scoped_shell_request(scope: WorkspaceScope) -> PermissionRequest:
    """创建不包含 raw command 的 shell grant request。"""
    return PermissionRequest(
        capability=PermissionCapability.SHELL_EXEC,
        operation="execute",
        target=scope.value,
        risk_level=RiskLevel.MEDIUM,
        reason="test scoped shell grant",
        metadata={
            "tool_name": "execute_office_shell",
            "workspace_scope": scope.value,
        },
    )


@pytest.fixture()
def roots(tmp_path, monkeypatch):
    office = tmp_path / "workspace" / "office"
    project = tmp_path / "project"
    office.mkdir(parents=True)
    project.mkdir()
    monkeypatch.setattr(sandbox_tools, "OFFICE_DIR", str(office))
    monkeypatch.setattr(sandbox_tools, "_permission_audit_logger", lambda *args, **kwargs: None)
    monkeypatch.setattr(sandbox_tools, "_permission_confirmation_audit_logger", lambda *args, **kwargs: None)
    return office, project


@pytest.fixture()
def active_project(roots):
    _, project = roots
    token = set_active_project_root(project)
    yield project
    reset_active_project_root(token)


def test_existing_directory_can_become_canonical_project_root(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    token = set_active_project_root(project)
    try:
        root = get_active_project_root()
        assert root.path == project.resolve()
        assert root.scope is WorkspaceScope.PROJECT
    finally:
        reset_active_project_root(token)

    assert get_active_project_root() is None


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_invalid_project_root_is_rejected(tmp_path, kind):
    path = tmp_path / kind
    if kind == "file":
        path.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="existing directory"):
        set_active_project_root(path)

    assert get_active_project_root() is None


def test_default_workspace_remains_office(roots):
    office, _ = roots

    root = sandbox_tools._get_active_workspace_root()

    assert get_active_project_root() is None
    assert root.path == office.resolve()
    assert root.scope is WorkspaceScope.OFFICE


def test_project_relative_file_read_and_list_succeed(active_project):
    nested = active_project / "nested"
    nested.mkdir()
    (nested / "notes.txt").write_text("project", encoding="utf-8")

    assert read_office_file.invoke({"filepath": "nested/notes.txt"}) == "project"
    assert "notes.txt" in list_office_files.invoke({"sub_dir": "nested"})


def test_confirmed_project_write_can_execute(active_project):
    confirmation_token = set_permission_confirmation_handler(
        lambda request, result: PermissionDecision.ALLOW
    )
    try:
        result = write_office_file.invoke(
            {"filepath": "nested/notes.txt", "content": "project", "mode": "w"}
        )
    finally:
        reset_permission_confirmation_handler(confirmation_token)

    assert "成功以 覆盖/新建 模式写入文件" in result
    assert (active_project / "nested" / "notes.txt").read_text(encoding="utf-8") == "project"


@pytest.mark.parametrize("mode, content", [("w", "modified"), ("a", " appended")])
def test_denied_project_write_does_not_modify_existing_file(active_project, mode, content):
    existing = active_project / "existing.txt"
    existing.write_text("original", encoding="utf-8")
    confirmation_token = set_permission_confirmation_handler(
        lambda request, result: PermissionDecision.DENY
    )
    try:
        result = write_office_file.invoke(
            {"filepath": "existing.txt", "content": content, "mode": mode}
        )
    finally:
        reset_permission_confirmation_handler(confirmation_token)

    assert "Permission denied" in result
    assert existing.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize(
    "user_path, expected_message",
    [
        ("../outside.txt", "Path traversal outside project is not allowed"),
        ("../project_evil/file.txt", "Path traversal outside project is not allowed"),
        ("/etc/passwd", "Absolute paths are not allowed"),
    ],
)
def test_project_path_escape_is_rejected(active_project, user_path, expected_message):
    result = write_office_file.invoke({"filepath": user_path, "content": "blocked", "mode": "w"})

    assert expected_message in result
    assert not (active_project.parent / "outside.txt").exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink is not supported on this platform")
def test_project_symlink_escape_is_rejected(active_project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = active_project / "secret-link.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    result = read_office_file.invoke({"filepath": "secret-link.txt"})

    assert "Path is outside the project workspace" in result
    assert secret.read_text(encoding="utf-8") == "secret"


def test_project_write_parent_validation_precedes_side_effects(active_project, monkeypatch):
    monkeypatch.setattr(
        sandbox_tools,
        "_permission_evaluator",
        lambda request: pytest.fail("permission must not run before path validation"),
    )

    result = write_office_file.invoke({"filepath": "../outside/new.txt", "content": "blocked", "mode": "w"})

    assert "Path traversal outside project is not allowed" in result
    assert not (active_project.parent / "outside").exists()


def test_safe_shell_uses_project_root_as_cwd(active_project, monkeypatch):
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", lambda request: allow("test allows"))
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="project", stderr="")

    monkeypatch.setattr(sandbox_tools.subprocess, "run", fake_run)

    result = execute_office_shell.invoke({"command": "pwd"})

    assert "project" in result
    assert calls[0][1]["cwd"] == str(active_project.resolve())


def test_project_shell_asks_without_confirmation(active_project, monkeypatch):
    monkeypatch.setattr(
        sandbox_tools.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("ASK shell must not run without confirmation"),
    )

    result = execute_office_shell.invoke({"command": "echo hello"})

    assert "Permission required" in result


def test_project_shell_session_grant_is_reused_in_same_run(active_project, monkeypatch):
    prompt_calls = []
    run_calls = []

    def allow_session(request, result):
        prompt_calls.append(request)
        return PermissionConfirmationChoice.ALLOW_SESSION

    monkeypatch.setattr(
        sandbox_tools.subprocess,
        "run",
        lambda *args, **kwargs: (
            run_calls.append(kwargs) or SimpleNamespace(returncode=0, stdout="hello", stderr="")
        ),
    )
    grants_token = set_session_permission_grants()
    confirmation_token = set_permission_confirmation_handler(allow_session)
    try:
        first = execute_office_shell.invoke({"command": "echo first"})
        second = execute_office_shell.invoke({"command": "echo second"})
    finally:
        reset_permission_confirmation_handler(confirmation_token)
        reset_session_permission_grants(grants_token)

    assert "hello" in first
    assert "hello" in second
    assert len(prompt_calls) == 1
    assert len(run_calls) == 2


def test_critical_shell_command_is_blocked_with_project_grant(active_project, monkeypatch):
    grants_token = set_session_permission_grants()
    try:
        get_session_permission_grants().add(
            SessionPermissionGrant.from_request(scoped_shell_request(WorkspaceScope.PROJECT))
        )
        monkeypatch.setattr(
            sandbox_tools.subprocess,
            "run",
            lambda *args, **kwargs: pytest.fail("critical shell must not run"),
        )

        result = execute_office_shell.invoke({"command": "rm -rf /"})

        assert "blocked by safety policy" in result
    finally:
        reset_session_permission_grants(grants_token)


def test_project_operation_passes_relative_scope_to_permission_and_audit(active_project, monkeypatch):
    captured = {}
    events = []
    (active_project / "notes.txt").write_text("project", encoding="utf-8")

    def capture_and_deny(request):
        captured["request"] = request
        return deny("test denies", request.risk_level)

    def capture_event(request, result, **kwargs):
        events.append(build_permission_decision_event(request, result, **kwargs))

    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", capture_and_deny)
    monkeypatch.setattr(sandbox_tools, "_permission_audit_logger", capture_event)

    result = read_office_file.invoke({"filepath": "notes.txt"})
    request = captured["request"]
    event_text = json.dumps(events[0])

    assert "Permission denied" in result
    assert request.target == "notes.txt"
    assert request.metadata["workspace_scope"] == "project"
    assert events[0]["target"] == "notes.txt"
    assert events[0]["metadata"]["workspace_scope"] == "project"
    assert str(active_project) not in event_text


def test_project_ask_stays_blocked_without_confirmation(active_project):
    result = write_office_file.invoke({"filepath": "blocked.txt", "content": "blocked", "mode": "w"})

    assert "Permission required" in result
    assert not (active_project / "blocked.txt").exists()


def test_office_and_project_session_grants_are_isolated():
    office_request = scoped_request(WorkspaceScope.OFFICE)
    project_request = scoped_request(WorkspaceScope.PROJECT)
    grants_token = set_session_permission_grants()
    try:
        office_allowed = resolve_permission(
            office_request,
            ask("confirmation required"),
            lambda request, result: PermissionConfirmationChoice.ALLOW_SESSION,
        )
        project_blocked = resolve_permission(project_request, ask("confirmation required"))

        get_session_permission_grants().clear()
        project_allowed = resolve_permission(
            project_request,
            ask("confirmation required"),
            lambda request, result: PermissionConfirmationChoice.ALLOW_SESSION,
        )
        office_blocked = resolve_permission(office_request, ask("confirmation required"))

        assert office_allowed.decision is PermissionDecision.ALLOW
        assert project_blocked.decision is PermissionDecision.ASK
        assert project_allowed.decision is PermissionDecision.ALLOW
        assert office_blocked.decision is PermissionDecision.ASK
    finally:
        reset_session_permission_grants(grants_token)


def test_cli_explicit_workspace_binds_project_for_run_and_resets(roots, monkeypatch):
    _, project = roots
    observed = []
    monkeypatch.setattr(cli, "load_dotenv", lambda path: None)
    monkeypatch.setattr(
        cli.os,
        "getenv",
        lambda key, default=None: {"DEFAULT_PROVIDER": "ollama", "DEFAULT_MODEL": "test"}.get(key, default),
    )
    monkeypatch.setitem(
        sys.modules,
        "entry.main",
        SimpleNamespace(main=lambda: observed.append(sandbox_tools._get_active_workspace_root())),
    )

    cli.run_agent(workspace=str(project))

    assert observed[0].path == project.resolve()
    assert observed[0].scope is WorkspaceScope.PROJECT
    assert get_active_project_root() is None


def test_cli_without_workspace_keeps_office_and_does_not_use_cwd(roots, monkeypatch):
    office, _ = roots
    observed = []
    monkeypatch.chdir(office.parent.parent)
    monkeypatch.setattr(cli, "load_dotenv", lambda path: None)
    monkeypatch.setattr(
        cli.os,
        "getenv",
        lambda key, default=None: {"DEFAULT_PROVIDER": "ollama", "DEFAULT_MODEL": "test"}.get(key, default),
    )
    monkeypatch.setitem(
        sys.modules,
        "entry.main",
        SimpleNamespace(main=lambda: observed.append(sandbox_tools._get_active_workspace_root())),
    )

    cli.run_agent(workspace=None)

    assert observed[0].path == office.resolve()
    assert observed[0].scope is WorkspaceScope.OFFICE


def test_cli_invalid_workspace_fails_without_fallback(tmp_path, monkeypatch):
    messages = []
    monkeypatch.setattr(cli, "load_dotenv", lambda path: None)
    monkeypatch.setattr(
        cli.os,
        "getenv",
        lambda key, default=None: {"DEFAULT_PROVIDER": "ollama", "DEFAULT_MODEL": "test"}.get(key, default),
    )
    monkeypatch.setattr(cli.console, "print", lambda message, **kwargs: messages.append(str(message)))

    with pytest.raises(cli.typer.Exit):
        cli.run_agent(workspace=str(tmp_path / "missing"))

    assert "Invalid project workspace" in messages[0]
    assert get_active_project_root() is None
