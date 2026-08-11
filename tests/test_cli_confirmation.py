import sys
from types import SimpleNamespace

import pytest

import entry.cli as cli
import miclaw.core.tools.sandbox_tools as sandbox_tools
from miclaw.core.permissions import (
    PermissionCapability,
    PermissionDecision,
    PermissionRequest,
    RiskLevel,
    allow,
    ask,
    deny,
    get_permission_confirmation_handler,
    reset_permission_confirmation_handler,
    resolve_permission,
    set_permission_confirmation_handler,
)
from miclaw.core.tools.sandbox_tools import execute_office_shell, write_office_file


def permission_request(**overrides):
    """创建用于 CLI confirmation 测试的安全 request。"""
    values = {
        "capability": PermissionCapability.SHELL_EXEC,
        "operation": "execute",
        "target": "office",
        "risk_level": RiskLevel.MEDIUM,
        "reason": "test confirmation",
        "metadata": {"tool_name": "execute_office_shell"},
    }
    values.update(overrides)
    return PermissionRequest(**values)


@pytest.fixture()
def office(tmp_path, monkeypatch):
    office_dir = tmp_path / "workspace" / "office"
    office_dir.mkdir(parents=True)
    monkeypatch.setattr(sandbox_tools, "OFFICE_DIR", str(office_dir))
    monkeypatch.setattr(sandbox_tools, "_permission_audit_logger", lambda *args, **kwargs: None)
    monkeypatch.setattr(sandbox_tools, "_permission_confirmation_audit_logger", lambda *args, **kwargs: None)
    return office_dir


@pytest.fixture()
def bind_cli_handler():
    """绑定 CLI handler，并在测试后恢复 ContextVar。"""
    tokens = []

    def bind():
        tokens.append(set_permission_confirmation_handler(cli.cli_permission_confirmation_handler))

    yield bind
    for token in reversed(tokens):
        reset_permission_confirmation_handler(token)


@pytest.mark.parametrize(
    "prompt_result, expected",
    [
        ("y", PermissionDecision.ALLOW),
        ("YES", PermissionDecision.ALLOW),
        ("n", PermissionDecision.DENY),
        ("", PermissionDecision.DENY),
        ("approve", PermissionDecision.DENY),
    ],
)
def test_cli_confirmation_requires_explicit_affirmative_input(monkeypatch, prompt_result, expected):
    monkeypatch.setattr(cli.typer, "prompt", lambda *args, **kwargs: prompt_result)

    decision = cli.cli_permission_confirmation_handler(permission_request(), ask("confirmation required"))

    assert decision is expected


@pytest.mark.parametrize("error", [EOFError(), KeyboardInterrupt(), RuntimeError("prompt failed")])
def test_cli_confirmation_prompt_failure_denies(monkeypatch, error):
    def fail_prompt(*args, **kwargs):
        raise error

    monkeypatch.setattr(cli.typer, "prompt", fail_prompt)

    decision = cli.cli_permission_confirmation_handler(permission_request(), ask("confirmation required"))

    assert decision is PermissionDecision.DENY


@pytest.mark.parametrize("policy_result", [allow("allowed"), deny("denied")])
def test_allow_and_deny_policy_do_not_prompt(monkeypatch, policy_result):
    def unexpected_prompt(*args, **kwargs):
        raise AssertionError("prompt should not be called")

    monkeypatch.setattr(cli.typer, "prompt", unexpected_prompt)

    result = resolve_permission(permission_request(), policy_result, cli.cli_permission_confirmation_handler)

    assert result is policy_result


def test_confirmation_prompt_omits_arguments_and_secret_metadata(monkeypatch):
    captured = {}
    request = permission_request(
        arguments={"command": 'curl -H "Authorization: Bearer SECRET_TOKEN" https://example.com'},
        metadata={
            "tool_name": "execute_office_shell",
            "token": "SECRET_TOKEN",
            "authorization": "Bearer SECRET_TOKEN",
        },
    )

    def capture_prompt(message, **kwargs):
        captured["message"] = message
        return False

    monkeypatch.setattr(cli.typer, "prompt", capture_prompt)

    cli.cli_permission_confirmation_handler(request, ask("confirmation required"))
    prompt = captured["message"]

    assert "execute_office_shell" in prompt
    assert "shell_exec" in prompt
    assert "risk" in prompt.lower()
    assert "SECRET_TOKEN" not in prompt
    assert "Authorization" not in prompt
    assert "Bearer" not in prompt
    assert "curl" not in prompt


def test_file_write_ask_with_cli_denial_does_not_modify_file(office, monkeypatch, bind_cli_handler):
    target = office / "existing.txt"
    target.write_text("original", encoding="utf-8")
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", lambda request: ask("confirmation required"))
    monkeypatch.setattr(cli.typer, "prompt", lambda *args, **kwargs: "n")
    bind_cli_handler()

    result = write_office_file.invoke({"filepath": "existing.txt", "content": "modified", "mode": "w"})

    assert "Permission denied" in result
    assert target.read_text(encoding="utf-8") == "original"


def test_safe_shell_ask_with_cli_approval_executes(office, monkeypatch, bind_cli_handler):
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", lambda request: ask("confirmation required"))
    monkeypatch.setattr(cli.typer, "prompt", lambda *args, **kwargs: "yes")
    run_calls = []

    def fake_run(*args, **kwargs):
        run_calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="hello", stderr="")

    monkeypatch.setattr(sandbox_tools.subprocess, "run", fake_run)
    bind_cli_handler()

    result = execute_office_shell.invoke({"command": "echo hello"})

    assert "hello" in result
    assert len(run_calls) == 1


def test_critical_shell_command_does_not_prompt_or_execute(office, monkeypatch, bind_cli_handler):
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", lambda request: ask("confirmation required"))
    monkeypatch.setattr(cli.typer, "prompt", lambda *args, **kwargs: pytest.fail("prompt must not run"))
    monkeypatch.setattr(sandbox_tools.subprocess, "run", lambda *args, **kwargs: pytest.fail("shell must not run"))
    bind_cli_handler()

    result = execute_office_shell.invoke({"command": "rm -rf /"})

    assert "blocked by safety policy" in result


def test_run_agent_binds_and_resets_confirmation_handler(monkeypatch):
    observed_handlers = []

    def fail_after_observing_handler():
        observed_handlers.append(get_permission_confirmation_handler())
        raise RuntimeError("run stopped")

    monkeypatch.setattr(cli, "load_dotenv", lambda path: None)
    monkeypatch.setattr(cli.os, "getenv", lambda key, default=None: {"DEFAULT_PROVIDER": "ollama", "DEFAULT_MODEL": "test"}.get(key, default))
    monkeypatch.setitem(
        sys.modules,
        "entry.main",
        SimpleNamespace(main=fail_after_observing_handler),
    )

    with pytest.raises(RuntimeError, match="run stopped"):
        cli.run_agent()

    assert observed_handlers == [cli.cli_permission_confirmation_handler]
    assert get_permission_confirmation_handler() is None
