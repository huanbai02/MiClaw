"""验证纯 ContextAssembler 的字符预算与 Agent prompt 回归。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from miclaw.core import agent, memory_permissions
from miclaw.core.context_assembly import (
    DEFAULT_SUPPLEMENTAL_CONTEXT_CHAR_BUDGET,
    SUPPLEMENTAL_OMITTED_MARKER,
    TRUNCATION_MARKER,
    ContextAssembler,
    ContextAssemblyRequest,
    truncate_context_content,
)
from miclaw.core.memory import MemoryKind, MemoryRecord, MemoryScope, MemoryScopeKind, MemorySource
from miclaw.core.permissions import deny
from miclaw.core.user_profile import UserProfileStore


BASE_PROMPT = "BASE_SYSTEM_RULES"


def _record(content: str) -> MemoryRecord:
    """构造当前唯一支持的 GLOBAL user-profile record。"""
    return MemoryRecord(
        memory_id="user-profile",
        kind=MemoryKind.USER_PROFILE,
        scope=MemoryScope(MemoryScopeKind.GLOBAL),
        source=MemorySource.USER_PROFILE_STORE,
        content=content,
    )


def _assemble(
    *,
    memory_records: tuple[MemoryRecord, ...] = (),
    summary: str | None = None,
    budget: int = 8_000,
):
    """用短 base prompt 构造纯 assembly result。"""
    return ContextAssembler().assemble(
        ContextAssemblyRequest(BASE_PROMPT, memory_records, summary, budget)
    )


class _CaptureModel:
    """捕获 Agent 的实际 SystemMessage。"""

    def __init__(self) -> None:
        self.inputs = []

    def invoke(self, messages):
        self.inputs.append(messages)
        return AIMessage(content="context assembly test")


class _Provider:
    """提供 Agent 初始化所需的最小 bind_tools 接口。"""

    def __init__(self, model: _CaptureModel) -> None:
        self.model = model

    def bind_tools(self, _tools):
        return self.model


class _NoopAuditLogger:
    """避免 Agent 测试产生 JSONL。"""

    def log_event(self, **_kwargs) -> None:
        return None


@pytest.fixture(autouse=True)
def disable_memory_permission_audit(monkeypatch):
    """避免真实 authorized read 写入 permission audit。"""
    monkeypatch.setattr(memory_permissions, "_permission_audit_logger", lambda *args, **kwargs: None)
    monkeypatch.setattr(memory_permissions, "_permission_confirmation_audit_logger", lambda *args, **kwargs: None)


def _capture_agent_prompt(monkeypatch, memory_dir: Path, summary: str = "") -> str:
    """通过真实 Agent retrieve + assembly 路径得到 system prompt。"""
    model = _CaptureModel()
    monkeypatch.setattr(agent, "MEMORY_DIR", str(memory_dir))
    monkeypatch.setattr(agent, "audit_logger", _NoopAuditLogger())
    monkeypatch.setattr(agent, "get_provider", lambda **_kwargs: _Provider(model))
    app = agent.create_agent_app(tools=[])
    app.invoke(
        {"messages": [HumanMessage(content="assemble context")], "summary": summary},
        config={"configurable": {"thread_id": "context-assembly"}},
    )
    return str(next(message.content for message in model.inputs[0] if isinstance(message, SystemMessage)))


def test_normal_size_content_keeps_pr38_prompt_layout_exactly():
    """预算充足时 section wording、内容和渲染顺序保持原 Agent 格式。"""
    result = _assemble(memory_records=(_record("PROFILE_MARKER"),), summary="SUMMARY_MARKER")

    expected = (
        "BASE_SYSTEM_RULES\n\n"
        "=============================\n"
        "【用户长期画像 (静态偏好)】\n"
        "PROFILE_MARKER\n"
        "=============================\n"
        "\n\n[近期对话上下文]\nSUMMARY_MARKER\n\n"
        "(注：这是系统自动生成的近期沟通摘要，请结合它来理解用户的最新问题)"
    )
    assert result.system_prompt == expected
    assert result.used_dynamic_chars == len("PROFILE_MARKER") + len("SUMMARY_MARKER")
    assert not result.summary_truncated
    assert not result.memory_truncated
    assert not result.memory_omitted_due_to_budget


def test_default_supplemental_character_budget_is_stable():
    """默认 budget 是确定的 character baseline，而非模型 token limit。"""
    request = ContextAssemblyRequest(BASE_PROMPT, (), None)

    assert DEFAULT_SUPPLEMENTAL_CONTEXT_CHAR_BUDGET == 8_000
    assert request.supplemental_char_budget == 8_000


def test_missing_memory_and_summary_keep_existing_empty_layout():
    """无 record 显示暂无记录；空 summary 不新增 summary section。"""
    result = _assemble()

    assert "暂无记录" in result.system_prompt
    assert "[近期对话上下文]" not in result.system_prompt
    assert result.memory_record_count == 0
    assert result.used_dynamic_chars == 0


def test_summary_gets_budget_priority_before_profile():
    """超长 profile 不得挤掉完整 summary，剩余预算才分给 profile。"""
    summary = "SUMMARY"
    profile = "PROFILE_" + "x" * 100
    budget = len(summary) + 20
    result = _assemble(memory_records=(_record(profile),), summary=summary, budget=budget)

    assert summary in result.system_prompt
    assert "PROFILE_" in result.system_prompt
    assert TRUNCATION_MARKER in result.system_prompt
    assert result.summary_chars_used == len(summary)
    assert result.memory_chars_used == 20
    assert result.memory_truncated
    assert result.used_dynamic_chars == budget


def test_summary_exhaustion_omits_existing_memory_without_claiming_it_is_missing():
    """summary 占满预算时 profile 使用固定 omission marker，而非暂无记录。"""
    result = _assemble(memory_records=(_record("PROFILE_SECRET"),), summary="SUMMARY_TOO_LONG", budget=5)

    assert "PROFILE_SECRET" not in result.system_prompt
    assert SUPPLEMENTAL_OMITTED_MARKER in result.system_prompt
    assert "暂无记录" not in result.system_prompt
    assert result.summary_chars_used == 5
    assert result.memory_chars_used == 0
    assert result.memory_omitted_due_to_budget
    assert result.used_dynamic_chars == 5


def test_zero_budget_keeps_system_rules_and_distinguishes_omitted_dynamic_content():
    """0 budget 不截断 base rules，summary/profile 都使用固定 omission marker。"""
    result = _assemble(memory_records=(_record("PROFILE_SECRET"),), summary="SUMMARY_SECRET", budget=0)

    assert result.system_prompt.startswith(BASE_PROMPT)
    assert "PROFILE_SECRET" not in result.system_prompt
    assert "SUMMARY_SECRET" not in result.system_prompt
    assert result.system_prompt.count(SUPPLEMENTAL_OMITTED_MARKER) == 2
    assert result.used_dynamic_chars == 0
    assert result.summary_truncated
    assert result.memory_omitted_due_to_budget


@pytest.mark.parametrize(
    ("text", "max_chars", "expected"),
    [
        ("abcd", 4, "abcd"),
        ("abcdef", 0, ""),
        ("abcdef", 1, TRUNCATION_MARKER[:1]),
        ("abcdefghijklm", len(TRUNCATION_MARKER), TRUNCATION_MARKER),
        ("中文画像", 4, "中文画像"),
    ],
)
def test_truncation_is_deterministic_and_never_exceeds_unicode_character_budget(text, max_chars, expected):
    """截断使用 Python 字符数，包含 marker 也绝不越界。"""
    result = truncate_context_content(text, max_chars)

    assert result == expected
    assert len(result) <= max_chars


def test_exact_dynamic_budget_boundary_has_no_marker_overflow():
    """summary + profile 恰好等于 budget 时不发生 off-by-one 截断。"""
    result = _assemble(memory_records=(_record("de"),), summary="abc", budget=5)

    assert result.used_dynamic_chars == 5
    assert result.summary_chars_used == 3
    assert result.memory_chars_used == 2
    assert TRUNCATION_MARKER not in result.system_prompt


@pytest.mark.parametrize(
    ("base", "records", "summary", "budget", "message"),
    [
        (None, (), None, 1, "base"),
        (BASE_PROMPT, [], None, 1, "tuple"),
        (BASE_PROMPT, ("not-record",), None, 1, "record"),
        (BASE_PROMPT, (_record("A"), _record("B")), None, 1, "multiple"),
        (BASE_PROMPT, (), 1, 1, "summary"),
        (BASE_PROMPT, (), None, True, "budget"),
        (BASE_PROMPT, (), None, -1, "budget"),
        (BASE_PROMPT, (), None, 1.0, "budget"),
        (BASE_PROMPT, (), None, "1", "budget"),
    ],
)
def test_assembly_request_rejects_malformed_inputs(base, records, summary, budget, message):
    """Assembler boundary 不对 collection、summary 或 budget 做隐式 coercion。"""
    with pytest.raises(ValueError, match=message):
        ContextAssemblyRequest(base, records, summary, budget)


def test_result_is_immutable_and_does_not_duplicate_dynamic_content_fields():
    """结果仅暴露 system_prompt 与安全统计，不保存第二份 raw payload。"""
    result = _assemble(memory_records=(_record("PROFILE"),), summary="SUMMARY")

    with pytest.raises(FrozenInstanceError):
        result.used_dynamic_chars = 0
    assert "raw_summary" not in result.__dict__
    assert "raw_memory_contents" not in result.__dict__


def test_agent_uses_assembler_with_normal_size_pr38_compatible_prompt(tmp_path, monkeypatch):
    """真实 Agent 的小内容 prompt 与 PR38 手工拼接格式等价。"""
    memory_dir = tmp_path / "memory"
    UserProfileStore(memory_dir / "user_profile.md").write_profile("PROFILE_MARKER")

    prompt = _capture_agent_prompt(monkeypatch, memory_dir, "SUMMARY_MARKER")
    expected = (
        f"{agent.BASE_SYSTEM_PROMPT}\n\n"
        "=============================\n"
        "【用户长期画像 (静态偏好)】\n"
        "PROFILE_MARKER\n"
        "=============================\n"
        "\n\n[近期对话上下文]\nSUMMARY_MARKER\n\n"
        "(注：这是系统自动生成的近期沟通摘要，请结合它来理解用户的最新问题)"
    )

    assert prompt == expected


def test_agent_bounds_oversized_profile_without_changing_base_rules(tmp_path, monkeypatch):
    """真实 Agent 仅截断 supplemental profile，不截断 base system instructions。"""
    memory_dir = tmp_path / "memory"
    profile = "PROFILE_PREFIX_" + "x" * 8_100
    UserProfileStore(memory_dir / "user_profile.md").write_profile(profile)

    prompt = _capture_agent_prompt(monkeypatch, memory_dir)

    assert prompt.startswith(agent.BASE_SYSTEM_PROMPT)
    assert "PROFILE_PREFIX_" in prompt
    assert TRUNCATION_MARKER in prompt
    assert profile not in prompt


def test_agent_blocked_read_uses_no_record_fallback_without_exposing_profile(tmp_path, monkeypatch):
    """Assembler 只消费 retriever 结果；DENY 不会让 profile 内容进入 prompt。"""
    memory_dir = tmp_path / "memory"
    UserProfileStore(memory_dir / "user_profile.md").write_profile("BLOCKED_PROFILE_SECRET")
    monkeypatch.setattr(memory_permissions, "_permission_evaluator", lambda request: deny("blocked", request.risk_level))

    prompt = _capture_agent_prompt(monkeypatch, memory_dir)

    assert "BLOCKED_PROFILE_SECRET" not in prompt
    assert "暂无记录" in prompt
