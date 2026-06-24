import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import miclaw.core.tools.sandbox_tools as sandbox_tools
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
    return office_dir


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
def test_execute_office_shell_safe_command_uses_resolved_office_cwd(mock_subprocess, office):
    mock_result = mock_subprocess.return_value
    mock_result.returncode = 0
    mock_result.stdout = "command output"
    mock_result.stderr = ""

    result = execute_office_shell.invoke({"command": "ls"})

    assert "ls" in result
    assert "command output" in result
    mock_subprocess.assert_called_once()
    assert mock_subprocess.call_args.kwargs["cwd"] == str(office.resolve())


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
