"""锁定 User Profile 的 exact-byte update 与 no-op 语义。"""

from __future__ import annotations

from contextlib import contextmanager

from miclaw.core import memory_lifecycle, memory_permissions
from miclaw.core.memory import MemoryKind
from miclaw.core.memory_lifecycle import (
    MemoryUpdateDisposition,
    MemoryWriteIntent,
    reset_memory_write_intent,
    set_memory_write_intent,
    write_user_profile_with_policy,
)
from miclaw.core.memory_retrieval import MemoryRetrievalRequest, MemoryRetriever
from miclaw.core.permissions import (
    PermissionCapability,
    PermissionConfirmationChoice,
    PermissionDecision,
    deny,
    reset_permission_confirmation_handler,
    reset_session_permission_grants,
    set_permission_confirmation_handler,
    set_session_permission_grants,
)
from miclaw.core.tools import builtins
from miclaw.core.user_profile import UserProfileStore, get_user_profile_store
from miclaw.core.workspace import reset_active_project_root, set_active_project_root


@contextmanager
def _explicit_write_intent():
    """为当前测试绑定且在退出时恢复 explicit lifecycle intent。"""
    token = set_memory_write_intent(MemoryWriteIntent.EXPLICIT_USER_REQUEST)
    try:
        yield
    finally:
        reset_memory_write_intent(token)


def _allow_once_handler(_request, _result):
    """为真实 write permission 返回单次允许。"""
    return PermissionConfirmationChoice.ALLOW_ONCE


def test_store_exact_content_comparison_is_raw_bytes_only(tmp_path):
    """比较不复用 read 的 strip/decode-ignore 语义，也不规范化换行或损坏 bytes。"""
    store = UserProfileStore(tmp_path / "memory" / "user_profile.md")

    assert store.matches_exact_content("A") is False
    store.profile_path.parent.mkdir(parents=True)
    store.profile_path.write_bytes(b"A\n")
    assert store.matches_exact_content("A\n") is True
    assert store.matches_exact_content("A") is False
    store.profile_path.write_bytes(b"A\xff")
    assert store.matches_exact_content("A") is False


def test_global_exact_match_is_noop_without_write_permission_or_persistence(tmp_path, monkeypatch):
    """exact GLOBAL content 仍需 explicit intent/read，但不会申请 write 或调用 Store writer。"""
    memory_dir = tmp_path / "memory"
    store = UserProfileStore(memory_dir / "user_profile.md")
    store.write_profile("EXACT")
    confirmations = []
    writes = []
    monkeypatch.setattr(
        UserProfileStore,
        "write_profile",
        lambda self, content: writes.append((self.profile_path, content)),
    )
    confirmation_token = set_permission_confirmation_handler(
        lambda request, result: confirmations.append(request) or _allow_once_handler(request, result)
    )
    try:
        with _explicit_write_intent():
            execution = write_user_profile_with_policy(memory_dir, "EXACT")
    finally:
        reset_permission_confirmation_handler(confirmation_token)

    assert execution.disposition is MemoryUpdateDisposition.NOOP_EXACT_MATCH
    assert execution.read_authorization is not None
    assert execution.read_authorization.final_result.decision is PermissionDecision.ALLOW
    assert execution.authorization is None
    assert confirmations == []
    assert writes == []
    assert store.profile_path.read_bytes() == b"EXACT"


def test_project_global_fallback_is_not_an_exact_target_duplicate(tmp_path):
    """PROJECT target 缺失时，即使 GLOBAL 相同也必须 materialize PROJECT profile。"""
    memory_dir = tmp_path / "memory"
    UserProfileStore(memory_dir / "user_profile.md").write_profile("SAME")
    project_root = tmp_path / "project-a"
    project_root.mkdir()
    project_token = set_active_project_root(project_root)
    confirmation_token = set_permission_confirmation_handler(_allow_once_handler)
    try:
        with _explicit_write_intent():
            execution = write_user_profile_with_policy(memory_dir, "SAME")
        project_store = get_user_profile_store(memory_dir)
        records = MemoryRetriever(memory_dir).retrieve(
            MemoryRetrievalRequest((MemoryKind.USER_PROFILE,))
        )
    finally:
        reset_permission_confirmation_handler(confirmation_token)
        reset_active_project_root(project_token)

    assert execution.disposition is MemoryUpdateDisposition.WRITE_REPLACE
    assert execution.authorization is not None
    assert execution.authorization.final_result.decision is PermissionDecision.ALLOW
    assert project_store.profile_path.read_bytes() == b"SAME"
    assert records[0].content == "SAME"
    assert records[0].scope == project_store.scope
    assert (memory_dir / "user_profile.md").read_bytes() == b"SAME"


def test_exact_project_match_is_noop_without_touching_global_profile(tmp_path, monkeypatch):
    """只有 PROJECT concrete target 的相同 bytes 才能成为 PROJECT no-op。"""
    memory_dir = tmp_path / "memory"
    global_store = UserProfileStore(memory_dir / "user_profile.md")
    global_store.write_profile("GLOBAL")
    project_root = tmp_path / "project-a"
    project_root.mkdir()
    project_token = set_active_project_root(project_root)
    confirmation_token = set_permission_confirmation_handler(_allow_once_handler)
    try:
        project_store = get_user_profile_store(memory_dir)
        project_store.write_profile("PROJECT")
        compared_paths = []
        original_matches = UserProfileStore.matches_exact_content
        monkeypatch.setattr(
            UserProfileStore,
            "matches_exact_content",
            lambda store, content: compared_paths.append(store.profile_path)
            or original_matches(store, content),
        )
        with _explicit_write_intent():
            execution = write_user_profile_with_policy(memory_dir, "PROJECT")
    finally:
        reset_permission_confirmation_handler(confirmation_token)
        reset_active_project_root(project_token)

    assert execution.disposition is MemoryUpdateDisposition.NOOP_EXACT_MATCH
    assert execution.authorization is None
    assert compared_paths == [project_store.profile_path]
    assert project_store.profile_path.read_bytes() == b"PROJECT"
    assert global_store.profile_path.read_bytes() == b"GLOBAL"


def test_read_block_cannot_prove_duplicate_and_falls_through_to_write_permission(tmp_path, monkeypatch):
    """exact-target READ 被拒绝时既不读 bytes，也不会误判为 no-op。"""
    memory_dir = tmp_path / "memory"
    store = UserProfileStore(memory_dir / "user_profile.md")
    store.write_profile("EXACT")
    comparisons = []
    monkeypatch.setattr(
        UserProfileStore,
        "matches_exact_content",
        lambda _self, _content: comparisons.append(1) or True,
    )
    monkeypatch.setattr(
        memory_permissions,
        "_permission_evaluator",
        lambda request: deny("blocked", request.risk_level),
    )
    with _explicit_write_intent():
        execution = write_user_profile_with_policy(memory_dir, "EXACT")

    assert execution.read_authorization is not None
    assert execution.read_authorization.final_result.decision is PermissionDecision.DENY
    assert execution.disposition is MemoryUpdateDisposition.WRITE_REPLACE
    assert execution.authorization is not None
    assert execution.authorization.final_result.decision is PermissionDecision.DENY
    assert comparisons == []
    assert store.profile_path.read_bytes() == b"EXACT"


def test_clear_classifies_empty_targets_and_changed_targets_exactly(tmp_path):
    """empty exact file 是 no-op；missing/非空文件的显式 clear 仍为受权 overwrite。"""
    empty_dir = tmp_path / "empty"
    missing_dir = tmp_path / "missing"
    changed_dir = tmp_path / "changed"
    UserProfileStore(empty_dir / "user_profile.md").write_profile("")
    UserProfileStore(changed_dir / "user_profile.md").write_profile("OLD")
    confirmation_token = set_permission_confirmation_handler(_allow_once_handler)
    try:
        with _explicit_write_intent():
            empty = write_user_profile_with_policy(empty_dir, "")
        with _explicit_write_intent():
            missing = write_user_profile_with_policy(missing_dir, "")
        with _explicit_write_intent():
            changed = write_user_profile_with_policy(changed_dir, "")
    finally:
        reset_permission_confirmation_handler(confirmation_token)

    assert empty.disposition is MemoryUpdateDisposition.NOOP_EXACT_MATCH
    assert empty.authorization is None
    assert missing.disposition is MemoryUpdateDisposition.WRITE_CLEAR
    assert changed.disposition is MemoryUpdateDisposition.WRITE_CLEAR
    assert (missing_dir / "user_profile.md").read_bytes() == b""
    assert (changed_dir / "user_profile.md").read_bytes() == b""


def test_noop_result_hides_disposition_from_tool_and_session_grant_still_applies_to_changes(tmp_path, monkeypatch):
    """Tool 对 no-op 与真实写入返回同一成功文案；grant 不替代每次 exact comparison。"""
    memory_dir = tmp_path / "memory"
    monkeypatch.setattr(builtins, "MEMORY_DIR", str(memory_dir))
    confirmations = []
    grants_token = set_session_permission_grants()
    confirmation_token = set_permission_confirmation_handler(
        lambda request, _result: confirmations.append(request) or PermissionConfirmationChoice.ALLOW_SESSION
    )
    try:
        with _explicit_write_intent():
            first = builtins.save_user_profile.invoke({"new_content": "ONE"})
        with _explicit_write_intent():
            same = builtins.save_user_profile.invoke({"new_content": "ONE"})
        with _explicit_write_intent():
            changed = builtins.save_user_profile.invoke({"new_content": "TWO"})
    finally:
        reset_permission_confirmation_handler(confirmation_token)
        reset_session_permission_grants(grants_token)

    assert first == same == changed == "记忆档案已成功覆写更新。新的人设画像已生效。"
    assert len(confirmations) == 1
    assert UserProfileStore(memory_dir / "user_profile.md").profile_path.read_bytes() == b"TWO"


def test_missing_intent_stops_before_exact_comparison(tmp_path, monkeypatch):
    """no-op 没有副作用也不能绕过 lifecycle preflight 变成内容比较 oracle。"""
    memory_dir = tmp_path / "memory"
    UserProfileStore(memory_dir / "user_profile.md").write_profile("EXACT")
    comparisons = []
    monkeypatch.setattr(
        memory_lifecycle,
        "resolve_user_profile_target",
        lambda *_args: (_ for _ in ()).throw(AssertionError("resolver must not run")),
    )
    monkeypatch.setattr(
        UserProfileStore,
        "matches_exact_content",
        lambda *_args: comparisons.append(1) or True,
    )

    execution = write_user_profile_with_policy(memory_dir, "EXACT")

    assert execution.policy_result.eligible is False
    assert execution.authorization is None
    assert comparisons == []
