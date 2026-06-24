import os
import sys
from pathlib import Path
from unittest.mock import mock_open, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import miclaw.core.tools.sandbox_tools as sandbox_tools
from miclaw.core.permissions import RiskLevel, allow, ask, deny, evaluate_permission
from miclaw.core.tools.sandbox_tools import (
    _get_safe_path,
    _resolve_existing_office_path,
    _resolve_new_office_path,
    execute_office_shell,
    list_office_files,
    read_office_file,
    write_office_file,
)


@pytest.fixture()
def office(tmp_path, monkeypatch):
    office_dir = tmp_path / "workspace" / "office"
    office_dir.mkdir(parents=True)
    monkeypatch.setattr(sandbox_tools, "OFFICE_DIR", str(office_dir))
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", evaluate_permission)
    return office_dir


def allow_all_permissions(request):
    return allow("test policy allows", request.risk_level)


def deny_all_permissions(request):
    return deny("test policy denies", request.risk_level)


def ask_all_permissions(request):
    return ask("test policy asks", request.risk_level)


def test_get_safe_path_normal(office):
    result = Path(_get_safe_path("subdir/file.txt"))
    assert result == (office / "subdir" / "file.txt").resolve()


def test_normal_relative_file_read_write_inside_office_succeeds(office):
    result = write_office_file.invoke({"filepath": "note.txt", "content": "hello", "mode": "w"})

    assert "成功以 覆盖/新建 模式写入文件" in result
    assert (office / "note.txt").read_text(encoding="utf-8") == "hello"
    assert read_office_file.invoke({"filepath": "note.txt"}) == "hello"


def test_nested_relative_path_inside_office_succeeds(office):
    result = write_office_file.invoke({"filepath": "nested/deep/note.txt", "content": "nested", "mode": "w"})

    assert "成功以 覆盖/新建 模式写入文件" in result
    assert read_office_file.invoke({"filepath": "nested/deep/note.txt"}) == "nested"
    assert "📁 deep" in list_office_files.invoke({"sub_dir": "nested"})


def test_list_office_files_succeeds_under_default_policy(office):
    (office / "file1.txt").write_text("content", encoding="utf-8")
    (office / "subdir").mkdir()

    result = list_office_files.invoke({"sub_dir": ""})

    assert "📄 file1.txt" in result
    assert "📁 subdir" in result


@pytest.mark.parametrize(
    "user_path, expected_message",
    [
        ("../memory/user_profile.md", "Path traversal outside office is not allowed"),
        ("/etc/passwd", "Absolute paths are not allowed"),
        ("../office_evil/file.txt", "Path traversal outside office is not allowed"),
        (r"C:\Users\x\secret.txt", "Windows drive paths are not allowed"),
    ],
)
def test_unsafe_paths_are_rejected_by_resolvers(office, user_path, expected_message):
    with pytest.raises(PermissionError, match=expected_message):
        _resolve_existing_office_path(user_path)

    result = read_office_file.invoke({"filepath": user_path})
    assert expected_message in result


def test_writing_new_file_inside_office_succeeds(office):
    result = write_office_file.invoke({"filepath": "new_dir/new_file.txt", "content": "new", "mode": "w"})

    assert "成功以 覆盖/新建 模式写入文件" in result
    assert (office / "new_dir" / "new_file.txt").read_text(encoding="utf-8") == "new"


def test_writing_new_file_through_parent_traversal_is_rejected(office):
    result = write_office_file.invoke({"filepath": "../outside.txt", "content": "nope", "mode": "w"})

    assert "Path traversal outside office is not allowed" in result
    assert not (office.parent / "outside.txt").exists()


def test_list_office_files_nonexistent_dir(office):
    result = list_office_files.invoke({"sub_dir": "nonexistent"})
    assert "目录不存在" in result


def test_read_office_file_nonexistent(office):
    result = read_office_file.invoke({"filepath": "nonexistent.txt"})
    assert "文件不存在" in result


def test_write_office_file_invalid_mode(office):
    result = write_office_file.invoke({"filepath": "test.txt", "content": "test content", "mode": "x"})
    assert "❌ 错误：mode 参数必须是" in result
    assert not (office / "test.txt").exists()


def test_file_read_deny_policy_does_not_read_file(office, monkeypatch):
    target = office / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", deny_all_permissions)
    blocked_open = mock_open(read_data="should not be read")

    with patch("builtins.open", blocked_open):
        result = read_office_file.invoke({"filepath": "secret.txt"})

    assert "Permission denied: test policy denies" in result
    blocked_open.assert_not_called()


def test_file_read_deny_policy_blocks_before_missing_file_result(office, monkeypatch):
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", deny_all_permissions)

    result = read_office_file.invoke({"filepath": "missing.txt"})

    assert "Permission denied: test policy denies" in result
    assert "文件不存在" not in result


def test_file_read_ask_policy_blocks_before_missing_file_result(office, monkeypatch):
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", ask_all_permissions)

    result = read_office_file.invoke({"filepath": "missing.txt"})

    assert "Permission required: test policy asks" in result
    assert "文件不存在" not in result


def test_file_write_deny_policy_does_not_create_or_modify(office, monkeypatch):
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", deny_all_permissions)

    result = write_office_file.invoke({"filepath": "new/blocked.txt", "content": "blocked", "mode": "w"})

    assert "Permission denied: test policy denies" in result
    assert not (office / "new").exists()


def test_file_write_ask_policy_does_not_create_or_modify(office, monkeypatch):
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", ask_all_permissions)

    result = write_office_file.invoke({"filepath": "new/blocked.txt", "content": "blocked", "mode": "w"})

    assert "Permission required: test policy asks" in result
    assert not (office / "new").exists()


def test_file_write_deny_policy_does_not_overwrite_existing_file(office, monkeypatch):
    target = office / "existing.txt"
    target.write_text("original", encoding="utf-8")
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", deny_all_permissions)

    result = write_office_file.invoke({"filepath": "existing.txt", "content": "modified", "mode": "w"})

    assert "Permission denied" in result
    assert target.read_text(encoding="utf-8") == "original"


def test_file_write_ask_policy_does_not_overwrite_existing_file(office, monkeypatch):
    target = office / "existing.txt"
    target.write_text("original", encoding="utf-8")
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", ask_all_permissions)

    result = write_office_file.invoke({"filepath": "existing.txt", "content": "modified", "mode": "w"})

    assert "Permission required" in result
    assert target.read_text(encoding="utf-8") == "original"


def test_file_write_deny_policy_does_not_append_to_existing_file(office, monkeypatch):
    target = office / "existing.txt"
    target.write_text("original", encoding="utf-8")
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", deny_all_permissions)

    result = write_office_file.invoke({"filepath": "existing.txt", "content": " appended", "mode": "a"})

    assert "Permission denied" in result
    assert target.read_text(encoding="utf-8") == "original"


def test_file_write_ask_policy_does_not_append_to_existing_file(office, monkeypatch):
    target = office / "existing.txt"
    target.write_text("original", encoding="utf-8")
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", ask_all_permissions)

    result = write_office_file.invoke({"filepath": "existing.txt", "content": " appended", "mode": "a"})

    assert "Permission required" in result
    assert target.read_text(encoding="utf-8") == "original"


def test_permission_checks_do_not_bypass_path_validation_for_read(office, monkeypatch):
    def fail_if_called(request):
        raise AssertionError("permission evaluator should not run before path validation")

    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", fail_if_called)

    result = read_office_file.invoke({"filepath": "../memory/user_profile.md"})

    assert "Path traversal outside office is not allowed" in result


def test_permission_checks_do_not_bypass_path_validation_for_write(office, monkeypatch):
    def fail_if_called(request):
        raise AssertionError("permission evaluator should not run before path validation")

    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", fail_if_called)

    result = write_office_file.invoke({"filepath": "../outside.txt", "content": "blocked", "mode": "w"})

    assert "Path traversal outside office is not allowed" in result
    assert not (office.parent / "outside.txt").exists()


def test_shell_permission_request_uses_medium_risk_and_command_preview(office, monkeypatch):
    captured = {}

    def capture_and_deny(request):
        captured["request"] = request
        return deny("captured", request.risk_level)

    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", capture_and_deny)

    result = execute_office_shell.invoke({"command": "echo hello"})

    request = captured["request"]
    assert "Permission denied: captured" in result
    assert request.operation == "execute"
    assert request.target == "office"
    assert request.arguments == {"command_preview": "echo hello"}
    assert request.risk_level is RiskLevel.MEDIUM


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink is not supported on this platform")
def test_symlink_inside_office_pointing_outside_is_rejected(office, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = office / "secret-link.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    assert "Path is outside the office workspace" in read_office_file.invoke({"filepath": "secret-link.txt"})
    assert "Path is outside the office workspace" in write_office_file.invoke(
        {"filepath": "secret-link.txt", "content": "overwrite", "mode": "w"}
    )
    assert secret.read_text(encoding="utf-8") == "secret"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink is not supported on this platform")
def test_shell_rejects_office_with_symlink_escape(office, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (office / "outside-link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    result = execute_office_shell.invoke({"command": "ls"})
    assert "Path is outside the office workspace" in result


@patch("miclaw.core.tools.sandbox_tools.subprocess.run")
def test_execute_office_shell_is_blocked_by_default_ask_policy(mock_subprocess, office):
    result = execute_office_shell.invoke({"command": "ls"})

    assert "Permission required: Shell execution requires confirmation by default" in result
    mock_subprocess.assert_not_called()


@patch("miclaw.core.tools.sandbox_tools.subprocess.run")
def test_execute_office_shell_safe_command_uses_resolved_office_cwd_when_allowed(mock_subprocess, office, monkeypatch):
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", allow_all_permissions)
    mock_result = mock_subprocess.return_value
    mock_result.returncode = 0
    mock_result.stdout = "command output"
    mock_result.stderr = ""

    result = execute_office_shell.invoke({"command": "ls"})

    assert "ls" in result
    assert "command output" in result
    mock_subprocess.assert_called_once()
    assert mock_subprocess.call_args.kwargs["cwd"] == str(office.resolve())


@patch("miclaw.core.tools.sandbox_tools.subprocess.run")
def test_execute_office_shell_deny_policy_does_not_run(mock_subprocess, office, monkeypatch):
    monkeypatch.setattr(sandbox_tools, "_permission_evaluator", deny_all_permissions)

    result = execute_office_shell.invoke({"command": "ls"})

    assert "Permission denied: test policy denies" in result
    mock_subprocess.assert_not_called()


@pytest.mark.parametrize(
    "cmd",
    [
        "cd ../",
        "cat /etc/passwd",
        "ls ~",
        "dir \\",
        r"type C:\windows\system32\config\sam",
    ],
)
def test_execute_office_shell_dangerous_commands(cmd, office):
    result = execute_office_shell.invoke({"command": cmd})
    assert "❌ 权限拒绝" in result


def test_resolve_new_office_path_rejects_parent_symlink_escape(office, tmp_path):
    outside = tmp_path / "outside-parent"
    outside.mkdir()
    link = office / "link-parent"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(PermissionError, match="Path is outside the office workspace"):
        _resolve_new_office_path("link-parent/new-file.txt")
