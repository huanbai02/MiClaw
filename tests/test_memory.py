"""验证当前用户画像的最小结构化 Memory 语义。"""

from dataclasses import FrozenInstanceError

import pytest

from miclaw.core.memory.models import (
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemoryScopeKind,
    MemorySource,
)


def _record(**overrides) -> MemoryRecord:
    """构造一个合法的当前 user-profile record，供不变量测试复用。"""
    values = {
        "memory_id": "user-profile",
        "kind": MemoryKind.USER_PROFILE,
        "scope": MemoryScope(MemoryScopeKind.GLOBAL),
        "source": MemorySource.USER_PROFILE_STORE,
        "content": "PROFILE_MARKER",
    }
    values.update(overrides)
    return MemoryRecord(**values)


def test_global_user_profile_record_is_valid_and_immutable():
    """当前 profile 的稳定语义为 GLOBAL、store source 的不可变 record。"""
    record = _record()

    assert record.memory_id == "user-profile"
    assert record.kind is MemoryKind.USER_PROFILE
    assert record.scope == MemoryScope(MemoryScopeKind.GLOBAL)
    assert record.source is MemorySource.USER_PROFILE_STORE
    with pytest.raises(FrozenInstanceError):
        record.content = "changed"


@pytest.mark.parametrize("memory_id", ["", "   ", None, 1])
def test_record_rejects_invalid_memory_id(memory_id):
    """Record id 必须为非空字符串。"""
    with pytest.raises(ValueError, match="memory_id"):
        _record(memory_id=memory_id)


def test_scope_enforces_global_and_project_identity_invariants():
    """GLOBAL 不带 id，PROJECT 必须带非空逻辑 id。"""
    assert MemoryScope(MemoryScopeKind.PROJECT, "project-a").scope_id == "project-a"

    with pytest.raises(ValueError, match="global"):
        MemoryScope(MemoryScopeKind.GLOBAL, "project-a")
    with pytest.raises(ValueError, match="project"):
        MemoryScope(MemoryScopeKind.PROJECT)
    with pytest.raises(ValueError, match="project"):
        MemoryScope(MemoryScopeKind.PROJECT, "  ")


def test_record_rejects_invalid_content_without_echoing_it():
    """内容必须是字符串，validation 不依赖内容值构造 diagnostic。"""
    with pytest.raises(ValueError, match="content"):
        _record(content=None)
