import json
import os
import sys
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import entry.cli as cli
import miclaw.core.tools.sandbox_tools as sandbox_tools
from miclaw.core.logger import (
    JSONLEventLogger,
    build_permission_confirmation_event,
    build_permission_decision_event,
)
from miclaw.core.permissions import (
    PermissionConfirmationChoice,
    PermissionDecision,
    evaluate_permission,
    get_permission_confirmation_handler,
    get_session_permission_grants,
    reset_permission_confirmation_handler,
    reset_session_permission_grants,
    set_permission_confirmation_handler,
    set_session_permission_grants,
)
from miclaw.core.tools.sandbox_tools import (
    execute_office_shell,
    list_office_files,
    read_office_file,
    write_office_file,
)
from miclaw.core.trace import TraceContext, reset_trace_context, set_current_trace_context
from miclaw.core.workspace import (
    WorkspaceScope,
    get_active_project_root,
    reset_active_project_root,
    set_active_project_root,
)


runner = CliRunner()


@pytest.fixture()
def workspace_paths(tmp_path, monkeypatch):
    """创建相互隔离的 OFFICE 与 PROJECT root。"""
    office = tmp_path / "workspace" / "office"
    project = tmp_path / "project"
    office.mkdir(parents=True)
    project.mkdir()
    monkeypatch.setattr(sandbox_tools, "OFFICE_DIR", str(office))
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", evaluate_permission)
    return office, project


def _bind_audit_logger(monkeypatch, logger):
    """把 sandbox audit sink 接到临时 JSONL logger。"""

    def log_decision(request, result, *, tool_name=None, metadata=None, **kwargs):
        event = build_permission_decision_event(
            request,
            result,
            tool_name=tool_name,
            metadata=metadata,
        )
        logger.log_event("integration", event.pop("event_type"), **event)

    def log_confirmation(
        request,
        policy_result,
        final_result,
        *,
        tool_name=None,
        metadata=None,
        **kwargs,
    ):
        event = build_permission_confirmation_event(
            request,
            policy_result,
            final_result,
            tool_name=tool_name,
            metadata=metadata,
        )
        logger.log_event("integration", event.pop("event_type"), **event)

    monkeypatch.setattr(sandbox_tools, "_permission_audit_logger", log_decision)
    monkeypatch.setattr(sandbox_tools, "_permission_confirmation_audit_logger", log_confirmation)


def test_complete_controlled_project_run_lifecycle(workspace_paths, tmp_path, monkeypatch):
    """覆盖 PROJECT read/write/shell、audit/trace 与 grant 生命周期。"""
    office, project = workspace_paths
    (project / "notes.txt").write_text("original", encoding="utf-8")
    log_file = tmp_path / "project-run.jsonl"
    logger = JSONLEventLogger(log_file=log_file)
    _bind_audit_logger(monkeypatch, logger)

    shell_calls = []

    def fake_run(*args, **kwargs):
        shell_calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="project shell", stderr="")

    monkeypatch.setattr(sandbox_tools.subprocess, "run", fake_run)

    canonical_input = project.parent / "nested" / ".." / project.name
    (project.parent / "nested").mkdir()
    project_token = set_active_project_root(canonical_input)
    grants_token = set_session_permission_grants()
    trace_token = set_current_trace_context(TraceContext(run_id="project-integration-run"))
    run_a_grants = get_session_permission_grants()
    try:
        assert get_active_project_root().path == project.resolve()
        assert get_active_project_root().scope is WorkspaceScope.PROJECT

        no_prompt_token = set_permission_confirmation_handler(
            lambda *args: pytest.fail("low-risk PROJECT read/list must not prompt")
        )
        try:
            assert "notes.txt" in list_office_files.invoke({"sub_dir": ""})
            assert read_office_file.invoke({"filepath": "notes.txt"}) == "original"
        finally:
            reset_permission_confirmation_handler(no_prompt_token)

        deny_token = set_permission_confirmation_handler(
            lambda request, result: PermissionConfirmationChoice.DENY
        )
        try:
            denied = write_office_file.invoke(
                {"filepath": "notes.txt", "content": "denied", "mode": "w"}
            )
        finally:
            reset_permission_confirmation_handler(deny_token)
        assert "Permission denied" in denied
        assert (project / "notes.txt").read_text(encoding="utf-8") == "original"

        once_token = set_permission_confirmation_handler(
            lambda request, result: PermissionConfirmationChoice.ALLOW_ONCE
        )
        try:
            allowed_once = write_office_file.invoke(
                {"filepath": "once.txt", "content": "once", "mode": "w"}
            )
        finally:
            reset_permission_confirmation_handler(once_token)
        assert "成功以 覆盖/新建 模式写入文件" in allowed_once
        blocked_repeat = write_office_file.invoke(
            {"filepath": "once.txt", "content": " twice", "mode": "a"}
        )
        assert "Permission required" in blocked_repeat
        assert (project / "once.txt").read_text(encoding="utf-8") == "once"

        write_prompts = []

        def allow_write_session(request, result):
            write_prompts.append(request)
            return PermissionConfirmationChoice.ALLOW_SESSION

        write_token = set_permission_confirmation_handler(allow_write_session)
        try:
            first_write = write_office_file.invoke(
                {"filepath": "session.txt", "content": "first", "mode": "w"}
            )
            second_write = write_office_file.invoke(
                {"filepath": "session.txt", "content": " second", "mode": "a"}
            )
        finally:
            reset_permission_confirmation_handler(write_token)
        assert "成功" in first_write
        assert "成功" in second_write
        assert len(write_prompts) == 1
        assert (project / "session.txt").read_text(encoding="utf-8") == "first\n second"

        shell_prompts = []

        def allow_shell_session(request, result):
            shell_prompts.append(request)
            return PermissionConfirmationChoice.ALLOW_SESSION

        shell_token = set_permission_confirmation_handler(allow_shell_session)
        try:
            first_shell = execute_office_shell.invoke({"command": "echo first"})
            second_shell = execute_office_shell.invoke({"command": "echo second"})
        finally:
            reset_permission_confirmation_handler(shell_token)
        assert "project shell" in first_shell
        assert "project shell" in second_shell
        assert len(shell_prompts) == 1
        assert [call["cwd"] for call in shell_calls] == [str(project.resolve())] * 2

        critical = execute_office_shell.invoke({"command": "rm -rf /"})
        assert "blocked by safety policy" in critical
        assert len(shell_calls) == 2
    finally:
        reset_trace_context(trace_token)
        reset_session_permission_grants(grants_token)
        reset_active_project_root(project_token)
        logger.log_queue.join()
        logger.shutdown()

    assert get_active_project_root() is None
    assert get_session_permission_grants() is None
    assert get_permission_confirmation_handler() is None
    assert sandbox_tools._get_active_workspace_root().path == office.resolve()

    monkeypatch.setattr(sandbox_tools, "_permission_audit_logger", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        sandbox_tools,
        "_permission_confirmation_audit_logger",
        lambda *args, **kwargs: None,
    )
    run_b_project_token = set_active_project_root(project)
    run_b_grants_token = set_session_permission_grants()
    try:
        run_b_grants = get_session_permission_grants()
        blocked_in_new_run = write_office_file.invoke(
            {"filepath": "session.txt", "content": " leaked", "mode": "a"}
        )
        assert run_b_grants is not run_a_grants
        assert run_b_grants == set()
        assert "Permission required" in blocked_in_new_run
        assert (project / "session.txt").read_text(encoding="utf-8") == "first\n second"
    finally:
        reset_session_permission_grants(run_b_grants_token)
        reset_active_project_root(run_b_project_token)

    events = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    project_events = [event for event in events if event.get("run_id") == "project-integration-run"]
    assert project_events
    assert [event["step_id"] for event in project_events] == list(
        range(1, len(project_events) + 1)
    )
    assert any(
        event["event"] == "permission_decision"
        and event["decision"] == "ask"
        and event["metadata"]["workspace_scope"] == "project"
        for event in project_events
    )
    assert any(
        event["event"] == "permission_confirmation"
        and event["source"] == "interactive"
        and event["final_decision"] == "allow"
        for event in project_events
    )
    assert any(
        event["event"] == "permission_confirmation"
        and event["source"] == "session_grant"
        for event in project_events
    )
    event_text = json.dumps(project_events)
    assert str(project.resolve()) not in event_text
    assert "echo first" not in event_text
    assert "echo second" not in event_text


@pytest.mark.parametrize(
    "filepath, expected_message",
    [
        ("../outside.txt", "Path traversal outside project is not allowed"),
        ("../project_evil/file.txt", "Path traversal outside project is not allowed"),
        ("<absolute>", "Absolute paths are not allowed"),
    ],
)
def test_integrated_project_workflow_rejects_path_escape(
    workspace_paths,
    filepath,
    expected_message,
):
    """PROJECT workflow 必须在 permission 前拒绝 traversal、prefix 与 absolute escape。"""
    _, project = workspace_paths
    user_path = str(project.parent / "absolute.txt") if filepath == "<absolute>" else filepath
    token = set_active_project_root(project)
    try:
        result = write_office_file.invoke(
            {"filepath": user_path, "content": "blocked", "mode": "w"}
        )
    finally:
        reset_active_project_root(token)

    assert expected_message in result
    assert not (project.parent / "outside.txt").exists()
    assert not (project.parent / "absolute.txt").exists()
    assert not (project.parent / "project_evil").exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink is not supported")
def test_integrated_project_workflow_rejects_symlink_escape(workspace_paths, tmp_path):
    """PROJECT read workflow 不得跟随指向 root 外部的 symlink。"""
    _, project = workspace_paths
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = project / "secret-link.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    token = set_active_project_root(project)
    try:
        result = read_office_file.invoke({"filepath": "secret-link.txt"})
    finally:
        reset_active_project_root(token)

    assert "Path is outside the project workspace" in result
    assert secret.read_text(encoding="utf-8") == "secret"


def test_cli_project_workspace_smoke_and_reset(workspace_paths, monkeypatch):
    """CLI 显式 PROJECT path 可进入 run，完成后 context 必须恢复。"""
    _, project = workspace_paths
    observed = []
    monkeypatch.setattr(cli, "load_dotenv", lambda path: None)
    monkeypatch.setattr(
        cli.os,
        "getenv",
        lambda key, default=None: {"DEFAULT_PROVIDER": "ollama", "DEFAULT_MODEL": "test"}.get(
            key, default
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "entry.main",
        SimpleNamespace(main=lambda: observed.append(get_active_project_root())),
    )

    result = runner.invoke(cli.app, ["run", "--workspace", str(project)])

    assert result.exit_code == 0
    assert observed[0].path == project.resolve()
    assert observed[0].scope is WorkspaceScope.PROJECT
    assert get_active_project_root() is None
    assert get_session_permission_grants() is None
    assert get_permission_confirmation_handler() is None


def test_cli_project_workspace_resets_after_agent_error(workspace_paths, monkeypatch):
    """agent 异常退出时也必须恢复 PROJECT、confirmation 与 grants context。"""
    _, project = workspace_paths

    def fail_run():
        assert get_active_project_root().path == project.resolve()
        raise RuntimeError("agent failed")

    monkeypatch.setattr(cli, "load_dotenv", lambda path: None)
    monkeypatch.setattr(
        cli.os,
        "getenv",
        lambda key, default=None: {"DEFAULT_PROVIDER": "ollama", "DEFAULT_MODEL": "test"}.get(
            key, default
        ),
    )
    monkeypatch.setitem(sys.modules, "entry.main", SimpleNamespace(main=fail_run))

    result = runner.invoke(cli.app, ["run", "--workspace", str(project)])

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert get_active_project_root() is None
    assert get_session_permission_grants() is None
    assert get_permission_confirmation_handler() is None


def test_cli_invalid_project_fails_before_agent_execution(tmp_path, monkeypatch):
    """CLI invalid PROJECT path 不得执行 agent，也不得遗留 context。"""
    agent_called = []
    monkeypatch.setattr(cli, "load_dotenv", lambda path: None)
    monkeypatch.setattr(
        cli.os,
        "getenv",
        lambda key, default=None: {"DEFAULT_PROVIDER": "ollama", "DEFAULT_MODEL": "test"}.get(
            key, default
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "entry.main",
        SimpleNamespace(main=lambda: agent_called.append(True)),
    )

    result = runner.invoke(cli.app, ["run", "--workspace", str(tmp_path / "missing")])

    assert result.exit_code == 2
    assert "Invalid project workspace" in result.output
    assert agent_called == []
    assert get_active_project_root() is None
    assert get_session_permission_grants() is None
    assert get_permission_confirmation_handler() is None
