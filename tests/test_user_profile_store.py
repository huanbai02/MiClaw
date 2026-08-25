"""验证 UserProfileStore 保持 PR 32 已冻结的 profile filesystem 语义。"""

from pathlib import Path

from miclaw.core.tools import builtins
from miclaw.core.memory import MemoryKind, MemoryScopeKind, MemorySource
from miclaw.core.user_profile import UserProfileStore, get_user_profile_store
from miclaw.core.workspace import reset_active_project_root, set_active_project_root


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


def test_save_user_profile_delegates_write_to_profile_store(monkeypatch):
    """内置 Tool 通过 store 写入，不再自行操作 profile filesystem。"""
    written = []

    class _RecordingStore:
        def write_profile(self, content: str) -> None:
            written.append(content)

    monkeypatch.setattr(builtins, "get_user_profile_store", lambda _memory_dir: _RecordingStore())

    result = builtins.save_user_profile.invoke({"new_content": "PROFILE_FROM_TOOL"})

    assert "成功覆写更新" in result
    assert written == ["PROFILE_FROM_TOOL"]
