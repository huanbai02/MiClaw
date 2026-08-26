"""验证 UserProfileStore 保持 PR 32 已冻结的 profile filesystem 语义。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from miclaw.core import user_profile
from miclaw.core.tools import builtins
from miclaw.core.memory import MemoryKind, MemoryScopeKind, MemorySource
from miclaw.core.user_profile import (
    UserProfilePersistenceError,
    UserProfileStore,
    get_user_profile_store,
)
from miclaw.core.workspace import reset_active_project_root, set_active_project_root
from miclaw.core.permissions import allow


def test_store_owns_fixed_profile_path_under_memory_directory(tmp_path):
    """Store 由 memory root 推导唯一 profile 文件名。"""
    store = get_user_profile_store(tmp_path / "memory")

    assert store.profile_path == tmp_path / "memory" / "user_profile.md"


def test_store_reads_existing_profile_and_preserves_current_missing_empty_fallback(tmp_path):
    """缺失或空 profile 返回 None，交由 Agent 保持既有 prompt fallback。"""
    profile_path = tmp_path / "memory" / "user_profile.md"
    store = UserProfileStore(profile_path)

    assert store.read_profile() is None
    assert store.read_record() is None
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text("\n  PROFILE_A  \n", encoding="utf-8")
    assert store.read_profile() == "PROFILE_A"
    profile_path.write_text(" \n\t", encoding="utf-8")
    assert store.read_profile() is None
    assert store.read_record() is None


def test_store_reads_existing_profile_as_fixed_global_record(tmp_path):
    """现有 Markdown 文件映射为稳定的 GLOBAL user-profile record。"""
    profile_path = tmp_path / "memory" / "user_profile.md"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text("PROFILE_MARKER", encoding="utf-8")

    record = UserProfileStore(profile_path).read_record()

    assert record is not None
    assert record.memory_id == "user-profile"
    assert record.kind is MemoryKind.USER_PROFILE
    assert record.scope.kind is MemoryScopeKind.GLOBAL
    assert record.scope.scope_id is None
    assert record.source is MemorySource.USER_PROFILE_STORE
    assert record.content == "PROFILE_MARKER"


def test_legacy_read_profile_delegates_to_structured_record(tmp_path):
    """旧字符串 API 与 read_record 使用同一文件读取语义。"""
    profile_path = tmp_path / "memory" / "user_profile.md"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text("PROFILE_MARKER", encoding="utf-8")
    store = UserProfileStore(profile_path)

    record = store.read_record()

    assert record is not None
    assert store.read_profile() == record.content


def test_store_profile_remains_global_while_project_workspace_is_active(tmp_path):
    """PROJECT workspace 不会激活 project-scoped profile storage。"""
    profile_path = tmp_path / "workspace" / "memory" / "user_profile.md"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text("GLOBAL_PROFILE_MARKER", encoding="utf-8")
    project_root = tmp_path / "project"
    project_root.mkdir()

    token = set_active_project_root(project_root)
    try:
        record = UserProfileStore(profile_path).read_record()
    finally:
        reset_active_project_root(token)

    assert record is not None
    assert record.scope.kind is MemoryScopeKind.GLOBAL
    assert record.content == "GLOBAL_PROFILE_MARKER"


def test_store_ignores_invalid_utf8_and_overwrites_profile_without_appending(tmp_path):
    """保持 errors=ignore 与完整覆盖写入语义。"""
    profile_path = tmp_path / "missing-memory" / "user_profile.md"
    store = UserProfileStore(profile_path)

    store.write_profile("PROFILE_A\n")
    store.write_profile("PROFILE_B")
    assert profile_path.read_text(encoding="utf-8") == "PROFILE_B"

    profile_path.write_bytes(b"PROFILE_C\xff")
    assert store.read_profile() == "PROFILE_C"
    assert store.read_record() is not None
    assert store.read_record().content == "PROFILE_C"


def test_atomic_write_replaces_profile_and_leaves_no_temporary_artifacts(tmp_path):
    """成功写入在同一 profile 路径完成覆盖，临时文件不会遗留。"""
    profile_path = tmp_path / "memory" / "user_profile.md"
    store = UserProfileStore(profile_path)

    store.write_profile("PROFILE_VERSION_A")
    store.write_profile("PROFILE_VERSION_B")

    assert profile_path.read_text(encoding="utf-8") == "PROFILE_VERSION_B"
    assert list(profile_path.parent.iterdir()) == [profile_path]


def test_write_failure_preserves_old_profile_cleans_temp_and_hides_details(tmp_path, monkeypatch):
    """准备临时内容失败时，旧 profile 与外部安全错误语义均保持稳定。"""
    profile_path = tmp_path / "memory" / "user_profile.md"
    store = UserProfileStore(profile_path)
    store.write_profile("PROFILE_VERSION_A")
    temp_path = profile_path.parent / ".write-failure.tmp"

    class _FailingTemporaryFile:
        name = str(temp_path)

        def __enter__(self):
            temp_path.touch()
            return self

        def __exit__(self, _type, _value, _traceback):
            return False

        def write(self, _content):
            raise OSError("PRIVATE_WRITE_FAILURE")

    monkeypatch.setattr(user_profile.tempfile, "NamedTemporaryFile", lambda **_kwargs: _FailingTemporaryFile())

    with pytest.raises(UserProfilePersistenceError) as error:
        store.write_profile("PROFILE_VERSION_B_PRIVATE")

    assert str(error.value) == "user_profile_write_failed"
    assert str(profile_path) not in str(error.value)
    assert "PROFILE_VERSION_B_PRIVATE" not in str(error.value)
    assert profile_path.read_text(encoding="utf-8") == "PROFILE_VERSION_A"
    assert not temp_path.exists()


def test_replace_failure_preserves_old_profile_and_cleans_temp(tmp_path, monkeypatch):
    """replace 失败不破坏旧文件，也不暴露路径或临时文件信息。"""
    profile_path = tmp_path / "memory" / "user_profile.md"
    store = UserProfileStore(profile_path)
    store.write_profile("PROFILE_VERSION_A")

    def _fail_replace(_source, _target):
        raise OSError("PRIVATE_REPLACE_FAILURE")

    monkeypatch.setattr(user_profile.os, "replace", _fail_replace)

    with pytest.raises(UserProfilePersistenceError) as error:
        store.write_profile("PROFILE_VERSION_B_PRIVATE")

    assert str(error.value) == "user_profile_write_failed"
    assert str(profile_path) not in str(error.value)
    assert "PROFILE_VERSION_B_PRIVATE" not in str(error.value)
    assert profile_path.read_text(encoding="utf-8") == "PROFILE_VERSION_A"
    assert list(profile_path.parent.iterdir()) == [profile_path]


def test_initial_replace_failure_creates_no_profile_or_temp_artifact(tmp_path, monkeypatch):
    """初始写入在 replace 前失败时不产生正式或临时文件。"""
    profile_path = tmp_path / "memory" / "user_profile.md"
    store = UserProfileStore(profile_path)
    monkeypatch.setattr(user_profile.os, "replace", lambda _source, _target: (_ for _ in ()).throw(OSError()))

    with pytest.raises(UserProfilePersistenceError, match="^user_profile_write_failed$"):
        store.write_profile("PROFILE_VERSION_B_PRIVATE")

    assert not profile_path.exists()
    assert list(profile_path.parent.iterdir()) == []


def test_atomic_writes_use_distinct_same_directory_temporary_files(tmp_path, monkeypatch):
    """每次写入使用独立且位于 profile 父目录的临时文件。"""
    profile_path = tmp_path / "memory" / "user_profile.md"
    store = UserProfileStore(profile_path)
    original_named_temporary_file = user_profile.tempfile.NamedTemporaryFile
    temp_names = []

    def _record_temp_name(**kwargs):
        temp_file = original_named_temporary_file(**kwargs)
        temp_names.append(Path(temp_file.name))
        return temp_file

    monkeypatch.setattr(user_profile.tempfile, "NamedTemporaryFile", _record_temp_name)

    store.write_profile("PROFILE_VERSION_A")
    store.write_profile("PROFILE_VERSION_B")

    assert len(temp_names) == 2
    assert temp_names[0] != temp_names[1]
    assert all(path.parent == profile_path.parent for path in temp_names)


def test_builtin_does_not_report_success_when_store_persistence_fails(monkeypatch):
    """保存 Tool 沿用 Store 的安全失败，不把失败伪装成成功。"""
    class _FailingStore:
        def write_profile(self, _content: str) -> None:
            raise UserProfilePersistenceError("user_profile_write_failed")

    monkeypatch.setattr(
        builtins,
        "authorize_user_profile_write",
        lambda _memory_dir: SimpleNamespace(
            final_result=allow("allowed"),
            target=SimpleNamespace(store=_FailingStore()),
        ),
    )

    with pytest.raises(UserProfilePersistenceError, match="^user_profile_write_failed$"):
        builtins.save_user_profile.invoke({"new_content": "PROFILE_PRIVATE"})


def test_save_user_profile_delegates_write_to_profile_store(monkeypatch):
    """内置 Tool 通过 store 写入，不再自行操作 profile filesystem。"""
    written = []

    class _RecordingStore:
        def write_profile(self, content: str) -> None:
            written.append(content)

    monkeypatch.setattr(
        builtins,
        "authorize_user_profile_write",
        lambda _memory_dir: SimpleNamespace(
            final_result=allow("allowed"),
            target=SimpleNamespace(store=_RecordingStore()),
        ),
    )

    result = builtins.save_user_profile.invoke({"new_content": "PROFILE_FROM_TOOL"})

    assert "成功覆写更新" in result
    assert written == ["PROFILE_FROM_TOOL"]
