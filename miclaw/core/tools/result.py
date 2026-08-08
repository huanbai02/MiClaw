"""MiClaw tool result envelope。

本模块提供内部 ToolResult 结构，当前仍通过 formatter 转回既有的
model-facing string，后续 audit log、monitor、MCP adapter 可以复用结构化字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class ToolResult:
    """描述一次 tool 调用的结构化结果。"""

    success: bool
    content: str
    data: dict[str, Any] | None = None
    error_type: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", str(self.content or ""))
        object.__setattr__(self, "data", _dict_or_none(self.data))
        object.__setattr__(self, "error_type", _str_or_none(self.error_type))
        object.__setattr__(self, "error_message", _str_or_none(self.error_message))
        object.__setattr__(self, "metadata", _json_safe_dict(self.metadata or {}))

    def to_dict(self) -> dict[str, Any]:
        """返回 JSON-friendly dict，不泄漏 Python object。"""
        return {
            "success": self.success,
            "content": self.content,
            "data": _json_safe(self.data) if self.data is not None else None,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "metadata": _json_safe_dict(self.metadata),
        }


def tool_success(
    content: str,
    data: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToolResult:
    """创建成功 ToolResult。"""
    return ToolResult(success=True, content=content, data=data, metadata=metadata or {})


def tool_error(
    error_type: str,
    error_message: str,
    content: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToolResult:
    """创建失败 ToolResult。"""
    return ToolResult(
        success=False,
        content=content if content is not None else error_message,
        error_type=error_type,
        error_message=error_message,
        metadata=metadata or {},
    )


def tool_permission_blocked(
    message: str,
    decision: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToolResult:
    """创建 permission 阻断 ToolResult。"""
    error_type = "permission_required" if decision == "ask" else "permission_denied"
    result_metadata = dict(metadata or {})
    if decision is not None:
        result_metadata["permission_decision"] = decision
    return tool_error(error_type, message, content=message, metadata=result_metadata)


def format_tool_result_for_model(result: ToolResult) -> str:
    """把 ToolResult 转为当前 LangChain/LangGraph tool 需要的 string。"""
    if result.success:
        return result.content
    if result.content:
        return result.content
    if result.error_message:
        return result.error_message
    return result.error_type or "Tool execution failed"


def _dict_or_none(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return _json_safe_dict(value)


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _json_safe_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _json_safe(item) for key, item in dict(value).items()}


def _json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return _json_safe_dict(value)
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
