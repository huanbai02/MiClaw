"""MiClaw tool result envelope。

本模块提供内部 ToolResult 结构：模型继续只看到 formatter 生成的安全文本，
runtime 则通过 ToolMessage artifact 接收最小结构化 outcome。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from langchain_core.messages import ToolMessage


_TOOL_OUTCOME_ARTIFACT_KEY = "miclaw_tool_outcome"
_TOOL_OUTCOME_VERSION = 1


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


@dataclass(frozen=True, slots=True)
class StructuredToolOutcome:
    """保存在 ToolMessage artifact 中的最小 runtime outcome。"""

    ok: bool
    error_type: str | None

    def __post_init__(self) -> None:
        """拒绝 checkpoint artifact 的隐式类型转换。"""
        if type(self.ok) is not bool or (self.error_type is not None and type(self.error_type) is not str):
            raise ValueError("invalid_structured_tool_outcome")


def encode_tool_outcome(result: ToolResult) -> dict[str, dict[str, bool | int | str | None]]:
    """投影 ToolResult 的 runtime decision 字段，绝不复制 content/data/metadata。"""
    if type(result) is not ToolResult:
        raise TypeError("invalid_tool_result")
    return {
        _TOOL_OUTCOME_ARTIFACT_KEY: {
            "version": _TOOL_OUTCOME_VERSION,
            "ok": result.success,
            "error_type": result.error_type,
        }
    }


def extract_tool_outcome(message: ToolMessage) -> StructuredToolOutcome | None:
    """从已知 artifact schema 读取 outcome；旧或损坏消息保持 fail-neutral。"""
    if not isinstance(message, ToolMessage) or type(message.artifact) is not dict:
        return None
    if set(message.artifact) != {_TOOL_OUTCOME_ARTIFACT_KEY}:
        return None
    payload = message.artifact.get(_TOOL_OUTCOME_ARTIFACT_KEY)
    if type(payload) is not dict or set(payload) != {"version", "ok", "error_type"}:
        return None
    if type(payload["version"]) is not int or payload["version"] != _TOOL_OUTCOME_VERSION:
        return None
    try:
        return StructuredToolOutcome(ok=payload["ok"], error_type=payload["error_type"])
    except ValueError:
        return None


def apply_tool_outcome_status(output: object) -> object:
    """让官方 ToolMessage.status 与已验证的 MiClaw outcome 保持一致。"""
    if not isinstance(output, ToolMessage):
        return output
    outcome = extract_tool_outcome(output)
    if outcome is None:
        return output
    return output.model_copy(update={"status": "success" if outcome.ok else "error"})


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
