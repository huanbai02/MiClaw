"""验证 UserProfileStore 的 GLOBAL / PROJECT persistence routing。"""

from pathlib import Path

import pytest

from miclaw.core import user_profile
from miclaw.core.memory import MemoryScopeKind
from miclaw.core.permissions import (
    PermissionConfirmationChoice,
    reset_permission_confirmation_handler,
    set_permission_confirmation_handler,
)
from miclaw.core.tools import builtins
from miclaw.core.user_profile import (
    UserProfilePersistenceError,
    UserProfileRoutingError,
    UserProfileStore,
    derive_project_memory_id,
    get_user_profile_store,
)
from miclaw.core.workspace import WorkspaceRoot, WorkspaceScope


def _project_root(path: Path) -> WorkspaceRoot:
    """构造已存在且 canonical 的 PROJECT root。"""
    path.mkdir()
    return WorkspaceRoot(path=path, scope=WorkspaceScope.PROJECT)


def _global_store(memory_dir: Path) -> UserProfileStore:
    """返回测试用 GLOBAL store，避免依赖 ambient ContextVar。"""
    return UserProfileStore(memory_dir / "user_profile.md")


def test_office_scope_routes_to_global_profile(tmp_path):
    """无 active PROJECT 时，factory 始终返回 GLOBAL profile path。"""
    memory_dir = tmp_path / "memory"
    office_root = WorkspaceRoot(tmp_path / "office", WorkspaceScope.OFFICE)

    store = get_user_profile_store(memory_dir, office_root)

    assert store.scope.kind is MemoryScopeKind.GLOBAL
    assert store.profile_path == memory_dir / "user_profile.md"


def test_project_memory_id_is_stable_opaque_and_distinct_per_canonical_root(tmp_path):
    """PROJECT namespace 使用 canonical root digest，不含原始路径或名称。"""
    project_a = _project_root(tmp_path / "project-a")
    project_b = _project_root(tmp_path / "project-b")

    project_a_id = derive_project_memory_id(project_a.path)

    assert project_a_id == derive_project_memory_id(project_a.path)
    assert project_a_id != derive_project_memory_id(project_b.path)
    assert len(project_a_id) == 24
    assert project_a_id not in str(project_a.path)
    assert "project-a" not in project_a_id


def test_project_missing_or_empty_profile_falls_back_to_global_record(tmp_path):
    """PROJECT 没有有效文件时只读 GLOBAL，不创建或复制 project profile。"""
    memory_dir = tmp_path / "memory"
    global_store = _global_store(memory_dir)
    global_store.write_profile("GLOBAL_MARKER")
    project_store = get_user_profile_store(memory_dir, _project_root(tmp_path / "project-a"))

    fallback = project_store.read_record()
    assert fallback is not None
    assert fallback.content == "GLOBAL_MARKER"
    assert fallback.scope.kind is MemoryScopeKind.GLOBAL
    assert not project_store.profile_path.exists()

    project_store.profile_path.parent.mkdir(parents=True)
    project_store.profile_path.write_text(" \n", encoding="utf-8")
    empty_fallback = project_store.read_record()
    assert empty_fallback is not None
    assert empty_fallback.content == "GLOBAL_MARKER"
    assert empty_fallback.scope.kind is MemoryScopeKind.GLOBAL


def test_project_profile_overrides_global_and_write_never_mutates_global(tmp_path):
    """PROJECT 写入仅创建其 own profile，并在读取时优先于 GLOBAL fallback。"""
    memory_dir = tmp_path / "memory"
    global_store = _global_store(memory_dir)
    global_store.write_profile("GLOBAL_MARKER")
    project_store = get_user_profile_store(memory_dir, _project_root(tmp_path / "project-a"))

    project_store.write_profile("PROJECT_A_MARKER")
    record = project_store.read_record()

    assert global_store.read_profile() == "GLOBAL_MARKER"
    assert record is not None
    assert record.content == "PROJECT_A_MARKER"
    assert record.scope == project_store.scope
    assert record.memory_id == f"user-profile::{project_store.scope.scope_id}"
    assert project_store.profile_path.parent.parent == memory_dir / "projects"


def test_project_profiles_are_isolated_and_same_project_reconstructs_same_store(tmp_path):
    """Project A/B 不共享写入；相同 canonical root 可跨 Store reconstruction 读取。"""
    memory_dir = tmp_path / "memory"
    _global_store(memory_dir).write_profile("GLOBAL_MARKER")
    project_a = _project_root(tmp_path / "project-a")
    project_b = _project_root(tmp_path / "project-b")
    project_a_store = get_user_profile_store(memory_dir, project_a)
    project_a_store.write_profile("PROJECT_A_MARKER")

    project_b_record = get_user_profile_store(memory_dir, project_b).read_record()
    reconstructed_a_record = get_user_profile_store(memory_dir, project_a).read_record()

    assert project_b_record is not None
    assert project_b_record.content == "GLOBAL_MARKER"
    assert project_b_record.scope.kind is MemoryScopeKind.GLOBAL
    assert reconstructed_a_record is not None
    assert reconstructed_a_record.content == "PROJECT_A_MARKER"
    assert reconstructed_a_record.scope.kind is MemoryScopeKind.PROJECT


def test_invalid_utf8_project_profile_uses_existing_decode_semantics_then_fallback(tmp_path):
    """PROJECT profile 延续 errors=ignore，解码后为空时才回退 GLOBAL。"""
    memory_dir = tmp_path / "memory"
    _global_store(memory_dir).write_profile("GLOBAL_MARKER")
    project_store = get_user_profile_store(memory_dir, _project_root(tmp_path / "project-a"))
    project_store.profile_path.parent.mkdir(parents=True)

    project_store.profile_path.write_bytes(b"PROJECT_MARKER\xff")
    record = project_store.read_record()
    assert record is not None
    assert record.content == "PROJECT_MARKER"
    assert record.scope.kind is MemoryScopeKind.PROJECT

    project_store.profile_path.write_bytes(b"\xff")
    fallback = project_store.read_record()
    assert fallback is not None
    assert fallback.content == "GLOBAL_MARKER"
    assert fallback.scope.kind is MemoryScopeKind.GLOBAL


def test_project_write_failure_preserves_project_and_other_scopes(tmp_path, monkeypatch):
    """PROJECT atomic failure 不会污染 GLOBAL 或其他 PROJECT namespace。"""
    memory_dir = tmp_path / "memory"
    global_store = _global_store(memory_dir)
    global_store.write_profile("GLOBAL_MARKER")
    project_a_store = get_user_profile_store(memory_dir, _project_root(tmp_path / "project-a"))
    project_b_store = get_user_profile_store(memory_dir, _project_root(tmp_path / "project-b"))
    project_a_store.write_profile("PROJECT_A_OLD")
    project_b_store.write_profile("PROJECT_B_MARKER")
    monkeypatch.setattr(user_profile.os, "replace", lambda _source, _target: (_ for _ in ()).throw(OSError()))

    with pytest.raises(UserProfilePersistenceError, match="^user_profile_write_failed$"):
        project_a_store.write_profile("PROJECT_A_NEW")

    assert global_store.read_profile() == "GLOBAL_MARKER"
    assert project_a_store.read_profile() == "PROJECT_A_OLD"
    assert project_b_store.read_profile() == "PROJECT_B_MARKER"


def test_unsupported_workspace_scope_fails_closed(tmp_path):
    """EXTERNAL 不能被静默映射到 GLOBAL profile。"""
    external_root = WorkspaceRoot(tmp_path, WorkspaceScope.EXTERNAL)

    with pytest.raises(UserProfileRoutingError, match="^unsupported_memory_scope$"):
        get_user_profile_store(tmp_path / "memory", external_root)


def test_builtin_uses_active_project_scope_without_scope_or_path_arguments(tmp_path, monkeypatch):
    """builtin scope 完全取自 host 的 active PROJECT context，而非模型参数。"""
    from miclaw.core.workspace import reset_active_project_root, set_active_project_root

    memory_dir = tmp_path / "memory"
    project_path = tmp_path / "project-a"
    project_path.mkdir()
    monkeypatch.setattr(builtins, "MEMORY_DIR", str(memory_dir))
    _global_store(memory_dir).write_profile("GLOBAL_MARKER")
    token = set_active_project_root(project_path)
    confirmation_token = set_permission_confirmation_handler(
        lambda request, result: PermissionConfirmationChoice.ALLOW_ONCE
    )
    try:
        result = builtins.save_user_profile.invoke({"new_content": "PROJECT_A_MARKER"})
        project_record = get_user_profile_store(memory_dir).read_record()
    finally:
        reset_permission_confirmation_handler(confirmation_token)
        reset_active_project_root(token)

    assert "成功覆写更新" in result
    assert set(builtins.save_user_profile.args) == {"new_content"}
    assert project_record is not None
    assert project_record.content == "PROJECT_A_MARKER"
    assert project_record.scope.kind is MemoryScopeKind.PROJECT
    assert _global_store(memory_dir).read_profile() == "GLOBAL_MARKER"


def test_save_user_profile_tool_contract_describes_runtime_scoped_routing():
    """模型看到的 Tool contract 准确描述 OFFICE/PROJECT 路由且不暴露实现。"""
    description = builtins.save_user_profile.description

    assert "OFFICE" in description
    assert "全局画像" in description
    assert "PROJECT" in description
    assert "当前项目范围" in description
    assert "运行时当前工作区决定" in description
    assert "全局显性记忆档案" not in description
    assert "MICLAW_WORKSPACE" not in description
    assert "memory/projects" not in description
    assert "sha256" not in description.lower()
    assert set(builtins.save_user_profile.args) == {"new_content"}
