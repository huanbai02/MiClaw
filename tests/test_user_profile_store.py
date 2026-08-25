"""验证 UserProfileStore 保持 PR 32 已冻结的 profile filesystem 语义。"""

from pathlib import Path

from miclaw.core.tools import builtins
from miclaw.core.user_profile import UserProfileStore, get_user_profile_store


def test_store_owns_fixed_profile_path_under_memory_directory(tmp_path):
    """Store 由 memory root 推导唯一 profile 文件名。"""
    store = get_user_profile_store(tmp_path / "memory")

    assert store.profile_path == tmp_path / "memory" / "user_profile.md"


def test_store_reads_existing_profile_and_preserves_current_missing_empty_fallback(tmp_path):
    """缺失或空 profile 返回 None，交由 Agent 保持既有 prompt fallback。"""
    profile_path = tmp_path / "memory" / "user_profile.md"
    store = UserProfileStore(profile_path)

    assert store.read_profile() is None
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text("\n  PROFILE_A  \n", encoding="utf-8")
    assert store.read_profile() == "PROFILE_A"
    profile_path.write_text(" \n\t", encoding="utf-8")
    assert store.read_profile() is None


def test_store_ignores_invalid_utf8_and_overwrites_profile_without_appending(tmp_path):
    """保持 errors=ignore 与完整覆盖写入语义。"""
    profile_path = tmp_path / "missing-memory" / "user_profile.md"
    store = UserProfileStore(profile_path)

    store.write_profile("PROFILE_A\n")
    store.write_profile("PROFILE_B")
    assert profile_path.read_text(encoding="utf-8") == "PROFILE_B"

    profile_path.write_bytes(b"PROFILE_C\xff")
    assert store.read_profile() == "PROFILE_C"


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
