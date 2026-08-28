"""验证确定性、permission-aware 的 user-profile retrieval 边界。"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import miclaw.core.agent.graph as agent
import miclaw.core.memory.permissions as memory_permissions
import miclaw.core.memory.retrieval as memory_retrieval
from miclaw.core.memory.models import MemoryKind, MemoryScopeKind
from miclaw.core.memory.retrieval import MemoryRetrievalRequest, MemoryRetriever
from miclaw.core.security.permissions import deny
from miclaw.core.memory.user_profile import UserProfileStore, get_user_profile_store
from miclaw.core.runtime.workspace import reset_active_project_root, set_active_project_root


class _CaptureModel:
    """捕获 Agent 发送给模型的 system prompt。"""

    def __init__(self) -> None:
        self.inputs = []

    def invoke(self, messages):
        self.inputs.append(messages)
        return AIMessage(content="retrieval test")


class _Provider:
    """提供 create_agent_app 所需的最小 bind_tools 接口。"""

    def __init__(self, model: _CaptureModel) -> None:
        self.model = model

    def bind_tools(self, _tools):
        return self.model


class _NoopAuditLogger:
    """避免 Agent integration test 写入真实 JSONL。"""

    def log_event(self, **_kwargs) -> None:
        return None


@pytest.fixture(autouse=True)
def disable_memory_permission_audit(monkeypatch):
    """避免 retriever tests 写入真实 permission audit。"""
    monkeypatch.setattr(memory_permissions, "_permission_audit_logger", lambda *args, **kwargs: None)
    monkeypatch.setattr(memory_permissions, "_permission_confirmation_audit_logger", lambda *args, **kwargs: None)


def _global_store(memory_dir: Path) -> UserProfileStore:
    """构造测试 GLOBAL profile store。"""
    return UserProfileStore(memory_dir / "user_profile.md")


def _profile_request(limit: int = 1) -> MemoryRetrievalRequest:
    """构造当前唯一有效的 profile retrieval request。"""
    return MemoryRetrievalRequest((MemoryKind.USER_PROFILE,), limit)


def _capture_agent_prompt(monkeypatch, memory_dir: Path) -> str:
    """经真实 Agent retrieval 路径捕获一次 system prompt。"""
    model = _CaptureModel()
    monkeypatch.setattr(agent, "MEMORY_DIR", str(memory_dir))
    monkeypatch.setattr(agent, "audit_logger", _NoopAuditLogger())
    monkeypatch.setattr(agent, "get_provider", lambda **_kwargs: _Provider(model))
    app = agent.create_agent_app(tools=[])
    app.invoke(
        {"messages": [HumanMessage(content="retrieve profile")], "summary": ""},
        config={"configurable": {"thread_id": "memory-retrieval"}},
    )
    return str(next(message.content for message in model.inputs[0] if isinstance(message, SystemMessage)))


def test_empty_request_returns_without_authorization_or_filesystem_read(monkeypatch, tmp_path):
    """空请求是无 Memory 查询，不触发既有授权读取边界。"""
    called = False

    def unexpected_read(_memory_dir):
        nonlocal called
        called = True
        raise AssertionError("empty retrieval must not read memory")

    monkeypatch.setattr(memory_retrieval, "read_authorized_user_profile", unexpected_read)

    assert MemoryRetriever(tmp_path / "memory").retrieve(MemoryRetrievalRequest(())) == ()
    assert not called


def test_retrieval_returns_one_authorized_profile_and_refreshes_from_filesystem(tmp_path):
    """同一 retriever 不缓存，USER_PROFILE 始终最多返回一个 immutable record。"""
    memory_dir = tmp_path / "memory"
    store = _global_store(memory_dir)
    retriever = MemoryRetriever(memory_dir)
    store.write_profile("PROFILE_A")

    first = retriever.retrieve(_profile_request(limit=32))
    repeated = retriever.retrieve(_profile_request())
    store.write_profile("PROFILE_B")
    second = retriever.retrieve(_profile_request())

    assert isinstance(first, tuple)
    assert len(first) == 1
    assert first == repeated
    assert first[0].content == "PROFILE_A"
    assert second[0].content == "PROFILE_B"
    with pytest.raises(AttributeError):
        first.append(first[0])


def test_missing_user_profile_returns_no_records(tmp_path):
    """缺失 profile 保持既有 no-record 语义。"""
    assert MemoryRetriever(tmp_path / "memory").retrieve(_profile_request()) == ()


@pytest.mark.parametrize(
    ("kinds", "limit", "message"),
    [
        ([MemoryKind.USER_PROFILE], 1, "tuple"),
        ((MemoryKind.USER_PROFILE, MemoryKind.USER_PROFILE), 1, "duplicate"),
        (("user_profile",), 1, "invalid"),
        ((MemoryKind.USER_PROFILE,), 0, "limit"),
        ((MemoryKind.USER_PROFILE,), 33, "limit"),
        ((MemoryKind.USER_PROFILE,), True, "limit"),
        ((MemoryKind.USER_PROFILE,), 1.0, "limit"),
        ((MemoryKind.USER_PROFILE,), None, "limit"),
    ],
)
def test_retrieval_request_rejects_malformed_or_unbounded_input(kinds, limit, message):
    """kind/limit 仅接受明确、有限的 host-controlled 输入。"""
    with pytest.raises(ValueError, match=message):
        MemoryRetrievalRequest(kinds, limit)


def test_retriever_reuses_authorized_read_boundary(monkeypatch, tmp_path):
    """Retriever 只委托既有授权读取服务，不直接读取 Store。"""
    expected = _global_store(tmp_path / "memory")
    expected.write_profile("AUTHORIZED_PROFILE")
    record = expected.read_record()
    assert record is not None
    calls = []
    monkeypatch.setattr(
        memory_retrieval,
        "read_authorized_user_profile",
        lambda memory_dir: calls.append(Path(memory_dir)) or record,
    )

    assert MemoryRetriever(tmp_path / "memory").retrieve(_profile_request()) == (record,)
    assert calls == [tmp_path / "memory"]


def test_denied_retrieval_does_not_read_profile_content(tmp_path, monkeypatch):
    """MEMORY_READ DENY 时 retriever 返回空 tuple，Store 读取不发生。"""
    memory_dir = tmp_path / "memory"
    _global_store(memory_dir).write_profile("DENIED_PROFILE_SECRET")
    read_calls = []
    original_read = UserProfileStore.read_primary_record
    monkeypatch.setattr(memory_permissions, "_permission_evaluator", lambda request: deny("blocked", request.risk_level))
    monkeypatch.setattr(
        UserProfileStore,
        "read_primary_record",
        lambda store: read_calls.append(store.profile_path) or original_read(store),
    )

    assert MemoryRetriever(memory_dir).retrieve(_profile_request()) == ()
    assert read_calls == []


def test_retriever_preserves_project_profile_and_global_fallback(tmp_path):
    """PROJECT record 优先；缺失 PROJECT 时返回唯一已授权 GLOBAL fallback。"""
    memory_dir = tmp_path / "memory"
    _global_store(memory_dir).write_profile("GLOBAL_PROFILE")
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    retriever = MemoryRetriever(memory_dir)

    token_a = set_active_project_root(project_a)
    try:
        get_user_profile_store(memory_dir).write_profile("PROJECT_A_PROFILE")
        project_a_records = retriever.retrieve(_profile_request())
    finally:
        reset_active_project_root(token_a)

    token_b = set_active_project_root(project_b)
    try:
        project_b_records = retriever.retrieve(_profile_request())
    finally:
        reset_active_project_root(token_b)

    assert project_a_records[0].content == "PROJECT_A_PROFILE"
    assert project_a_records[0].scope.kind is MemoryScopeKind.PROJECT
    assert project_b_records[0].content == "GLOBAL_PROFILE"
    assert project_b_records[0].scope.kind is MemoryScopeKind.GLOBAL


def test_agent_uses_retrieval_and_keeps_denied_profile_out_of_system_prompt(tmp_path, monkeypatch):
    """真实 Agent 经 retriever 注入授权内容；DENY 时仍使用既有暂无记录 fallback。"""
    memory_dir = tmp_path / "memory"
    _global_store(memory_dir).write_profile("AGENT_RETRIEVAL_PROFILE")

    allowed_prompt = _capture_agent_prompt(monkeypatch, memory_dir)
    monkeypatch.setattr(memory_permissions, "_permission_evaluator", lambda request: deny("blocked", request.risk_level))
    denied_prompt = _capture_agent_prompt(monkeypatch, memory_dir)

    assert "AGENT_RETRIEVAL_PROFILE" in allowed_prompt
    assert "AGENT_RETRIEVAL_PROFILE" not in denied_prompt
    assert "暂无记录" in denied_prompt
