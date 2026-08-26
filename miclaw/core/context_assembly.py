"""以确定性 supplemental character budget 组装当前 Agent SystemMessage。"""

from __future__ import annotations

from dataclasses import dataclass

from .memory import MemoryKind, MemoryRecord


DEFAULT_SUPPLEMENTAL_CONTEXT_CHAR_BUDGET = 8_000
TRUNCATION_MARKER = "…[truncated]"
SUPPLEMENTAL_OMITTED_MARKER = "（内容因上下文预算未注入）"


@dataclass(frozen=True)
class ContextAssemblyRequest:
    """描述一次纯 context assembly 所需的 runtime 输入。"""

    base_system_prompt: str
    memory_records: tuple[MemoryRecord, ...]
    conversation_summary: str | None
    supplemental_char_budget: int = DEFAULT_SUPPLEMENTAL_CONTEXT_CHAR_BUDGET

    def __post_init__(self) -> None:
        """严格校验当前单一 USER_PROFILE retrieval contract。"""
        if type(self.base_system_prompt) is not str:
            raise ValueError("base system prompt must be a string")
        if type(self.memory_records) is not tuple:
            raise ValueError("memory records must be a tuple")
        if any(not isinstance(record, MemoryRecord) for record in self.memory_records):
            raise ValueError("invalid memory record")
        if len(self.memory_records) > 1:
            raise ValueError("multiple memory records are unsupported")
        if any(record.kind is not MemoryKind.USER_PROFILE for record in self.memory_records):
            raise ValueError("unsupported memory kind")
        if self.conversation_summary is not None and type(self.conversation_summary) is not str:
            raise ValueError("conversation summary must be a string or None")
        if type(self.supplemental_char_budget) is not int or self.supplemental_char_budget < 0:
            raise ValueError("invalid supplemental context character budget")


@dataclass(frozen=True)
class ContextAssemblyResult:
    """保存 assembled prompt 与不含正文的确定性预算统计。"""

    system_prompt: str
    used_dynamic_chars: int
    summary_chars_used: int
    memory_chars_used: int
    summary_truncated: bool
    memory_truncated: bool
    memory_omitted_due_to_budget: bool
    memory_record_count: int


def truncate_context_content(text: str, max_chars: int) -> str:
    """以固定 marker 截断动态 context，且 marker 计入字符上限。"""
    if type(text) is not str:
        raise ValueError("context content must be a string")
    if type(max_chars) is not int or max_chars < 0:
        raise ValueError("invalid context character limit")
    if len(text) <= max_chars:
        return text
    if max_chars == 0:
        return ""
    if max_chars <= len(TRUNCATION_MARKER):
        return TRUNCATION_MARKER[:max_chars]
    return text[: max_chars - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


class ContextAssembler:
    """纯组装 base rules、profile 与 conversation summary，不做读取或授权。"""

    def assemble(self, request: ContextAssemblyRequest) -> ContextAssemblyResult:
        """按 summary 优先的 supplemental budget 生成当前 SystemMessage 文本。"""
        if not isinstance(request, ContextAssemblyRequest):
            raise ValueError("invalid context assembly request")

        remaining = request.supplemental_char_budget
        summary = request.conversation_summary
        has_summary = summary is not None and summary != ""
        summary_content = ""
        summary_chars_used = 0
        summary_truncated = False
        if has_summary:
            summary_content = truncate_context_content(summary, remaining)
            summary_chars_used = len(summary_content)
            summary_truncated = len(summary) > remaining
            remaining -= summary_chars_used

        memory_content = "暂无记录"
        memory_chars_used = 0
        memory_truncated = False
        memory_omitted_due_to_budget = False
        if request.memory_records:
            profile_content = request.memory_records[0].content
            if remaining == 0:
                memory_content = SUPPLEMENTAL_OMITTED_MARKER
                memory_omitted_due_to_budget = True
            else:
                memory_content = truncate_context_content(profile_content, remaining)
                memory_chars_used = len(memory_content)
                memory_truncated = len(profile_content) > remaining

        system_prompt = (
            f"{request.base_system_prompt}\n\n"
            "=============================\n"
            "【用户长期画像 (静态偏好)】\n"
            f"{memory_content}\n"
            "=============================\n"
        )
        if has_summary:
            rendered_summary = summary_content or SUPPLEMENTAL_OMITTED_MARKER
            system_prompt += (
                f"\n\n[近期对话上下文]\n{rendered_summary}\n\n"
                "(注：这是系统自动生成的近期沟通摘要，请结合它来理解用户的最新问题)"
            )

        return ContextAssemblyResult(
            system_prompt=system_prompt,
            used_dynamic_chars=summary_chars_used + memory_chars_used,
            summary_chars_used=summary_chars_used,
            memory_chars_used=memory_chars_used,
            summary_truncated=summary_truncated,
            memory_truncated=memory_truncated,
            memory_omitted_due_to_budget=memory_omitted_due_to_budget,
            memory_record_count=len(request.memory_records),
        )
