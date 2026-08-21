"""提供 observability 使用的保守 redaction 与摘要基础函数。

本模块只负责生成有界、JSON-friendly 的安全摘要，不是完整 DLP 或 secret
detection 系统。调用方不应把 sanitization 结果视为已覆盖所有敏感数据模式。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
import math
import re
from typing import Any


MAX_STRING_LENGTH = 200
MAX_COLLECTION_ITEMS = 20
MAX_RECURSION_DEPTH = 4
MAX_KEY_LENGTH = 80
MAX_INTEGER_BITS = 256

REDACTED = "<redacted>"
CONTENT_OMITTED = "<content omitted>"
DEPTH_LIMIT_REACHED = "<depth limit reached>"
COLLECTION_ITEMS_OMITTED = "<collection items omitted>"
UNSUPPORTED_VALUE = "<unsupported value>"
REDACTION_FAILED = "<redaction failed>"
LARGE_INTEGER_OMITTED = "<large integer omitted>"

_RESERVED_SUMMARY_KEY_NAMES = {
    "redaction_error",
    "redaction_limit",
    "redaction_omitted_items",
}

_SENSITIVE_KEY_NAMES = {
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "bearer",
    "credential",
    "private_key",
    "access_key",
    "client_secret",
    "access_token",
    "refresh_token",
}
_SENSITIVE_KEY_SUFFIXES = (
    "_api_key",
    "_access_key",
    "_private_key",
    "_client_secret",
    "_password",
    "_passwd",
    "_secret",
    "_token",
    "_credential",
    "_authorization",
)
_SENSITIVE_KEY_COMPACT_NAMES = {name.replace("_", "") for name in _SENSITIVE_KEY_NAMES}
_CONTENT_KEY_NAMES = {
    "command",
    "command_preview",
    "raw_command",
    "sanitized_command",
    "shell_command",
    "content",
    "file_content",
    "input",
    "input_text",
    "body",
    "request_body",
    "response_body",
    "payload",
    "stdout",
    "stderr",
    "result",
    "result_summary",
    "output",
    "output_text",
}
_CONTENT_KEY_SUFFIXES = tuple(f"_{name}" for name in ("command", "content", "body", "payload", "result", "output"))
_CONTENT_KEY_COMPACT_NAMES = {name.replace("_", "") for name in _CONTENT_KEY_NAMES}
_SECRET_CONTENT_PATTERNS = (
    re.compile(r"authorization\s*:\s*bearer\s+\S+", re.IGNORECASE),
    re.compile(r"\bbearer\s+[a-z]{8,}(?=\s|$)", re.IGNORECASE),
    re.compile(
        r"\bbearer\s+(?=\S{8,}(?:\s|$))(?=\S*(?:\d|_|[-+/=]|[a-z0-9]\.[a-z0-9]))\S+",
        re.IGNORECASE,
    ),
    re.compile(
        r"[\"']?(?:password|passwd|secret|token|api[_-]?key|apikey|authorization|auth|"
        r"credential|private[_-]?key|access[_-]?key|client[_-]?secret)[\"']?\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
)


def is_sensitive_key(key: str) -> bool:
    """判断 key 是否明显表示 credential 或 secret，匹配不区分大小写。

    Args:
        key: 待检查的字段名。

    Returns:
        明显敏感时返回 True。该规则是保守 baseline，不是完整 secret scanner。
    """
    try:
        normalized = _normalize_key(str(key))
        compact = normalized.replace("_", "")
        return (
            normalized in _SENSITIVE_KEY_NAMES
            or compact in _SENSITIVE_KEY_COMPACT_NAMES
            or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)
        )
    except Exception:
        return True


def summarize_content(content: Any) -> str:
    """省略 raw content，仅返回稳定且有界的 presence/length 摘要。

    Args:
        content: command、file content、stdout 或其他不应直接记录的值。

    Returns:
        不包含原始内容的摘要；无法安全计算长度时返回固定 placeholder。
    """
    try:
        if content is None:
            return f"{CONTENT_OMITTED}; present=false; length=0"
        length = _safe_length(content)
        if length is None:
            return CONTENT_OMITTED
        return f"{CONTENT_OMITTED}; present=true; length={length}"
    except Exception:
        return CONTENT_OMITTED


def sanitize_value(
    value: Any,
    *,
    max_string_length: int = MAX_STRING_LENGTH,
    max_collection_items: int = MAX_COLLECTION_ITEMS,
    max_depth: int = MAX_RECURSION_DEPTH,
) -> Any:
    """递归生成有界、JSON-friendly 的 value 表示。

    Args:
        value: 不可信或可能包含敏感数据的任意值。
        max_string_length: 普通字符串允许保留的最大字符数。
        max_collection_items: 每层 collection 最多保留的 item 数。
        max_depth: 最多递归层数。

    Returns:
        JSON-friendly scalar、dict 或 list。异常时返回安全 placeholder，绝不回退原值。

    Notes:
        普通低风险 scalar 会保留以支持诊断；明显 credential pattern 会整体 redacted。
    """
    try:
        limits = _validated_limits(max_string_length, max_collection_items, max_depth)
        return _sanitize_value(value, depth=0, **limits)
    except Exception:
        return REDACTION_FAILED


def redact_mapping(
    data: Mapping,
    *,
    max_string_length: int = MAX_STRING_LENGTH,
    max_collection_items: int = MAX_COLLECTION_ITEMS,
    max_depth: int = MAX_RECURSION_DEPTH,
) -> dict[str, Any]:
    """递归 redacts mapping，并把危险 content field 转换为 presence/length。

    Args:
        data: 待处理的 mapping。
        max_string_length: 普通字符串最大字符数。
        max_collection_items: 每层 mapping 最多处理的 item 数。
        max_depth: 最大递归层数。

    Returns:
        JSON-friendly dict。Nested secret key 同样会被 redacted；异常时 fail closed。
    """
    try:
        if not isinstance(data, Mapping):
            return {"_redaction_error": REDACTION_FAILED}
        limits = _validated_limits(max_string_length, max_collection_items, max_depth)
        return _redact_mapping(data, depth=0, **limits)
    except Exception:
        return {"_redaction_error": REDACTION_FAILED}


def summarize_tool_args(
    args: Mapping,
    *,
    max_string_length: int = MAX_STRING_LENGTH,
    max_collection_items: int = MAX_COLLECTION_ITEMS,
    max_depth: int = MAX_RECURSION_DEPTH,
) -> dict[str, Any]:
    """生成 tool arguments 的保守摘要，不直接返回 command/content 等 raw value。

    Args:
        args: Tool arguments mapping。
        max_string_length: 普通 metadata 字符串最大字符数。
        max_collection_items: 每层最多处理的 argument 数。
        max_depth: 最大递归层数。

    Returns:
        可直接 JSON serialize 的摘要 dict。
    """
    return redact_mapping(
        args,
        max_string_length=max_string_length,
        max_collection_items=max_collection_items,
        max_depth=max_depth,
    )


def _redact_mapping(
    data: Mapping,
    *,
    depth: int,
    max_string_length: int,
    max_collection_items: int,
    max_depth: int,
) -> dict[str, Any]:
    if depth >= max_depth:
        return {"_redaction_limit": DEPTH_LIMIT_REACHED}

    items = []
    has_omitted_items = False
    for key, value in data.items():
        if len(items) >= max_collection_items:
            has_omitted_items = True
            break
        items.append((key, value))

    generated_summary_keys = _generated_summary_keys(items)
    result: dict[str, Any] = {}
    for key, value in items:

        key_text = _safe_key(key, max_string_length=MAX_KEY_LENGTH)
        if key_text == "<unreadable key>":
            result[key_text] = REDACTED
            continue
        detection_key = key if isinstance(key, str) else key_text
        if _is_reserved_summary_key(detection_key, generated_summary_keys):
            result.setdefault(key_text, REDACTED)
            continue
        if is_sensitive_key(detection_key):
            result[key_text] = REDACTED
            continue
        if _is_content_key(detection_key):
            result[f"{key_text}_present"] = value is not None
            length = _safe_length(value)
            if length is not None:
                result[f"{key_text}_length"] = length
            continue
        result[key_text] = _sanitize_value(
            value,
            depth=depth + 1,
            max_string_length=max_string_length,
            max_collection_items=max_collection_items,
            max_depth=max_depth,
        )
    if has_omitted_items:
        result["_redaction_omitted_items"] = _omitted_item_count(data, len(items))
    return result


def _sanitize_value(
    value: Any,
    *,
    depth: int,
    max_string_length: int,
    max_collection_items: int,
    max_depth: int,
) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if value.bit_length() <= MAX_INTEGER_BITS else LARGE_INTEGER_OMITTED
    if isinstance(value, float):
        return value if math.isfinite(value) else UNSUPPORTED_VALUE
    if isinstance(value, Enum):
        return _sanitize_value(
            value.value,
            depth=depth,
            max_string_length=max_string_length,
            max_collection_items=max_collection_items,
            max_depth=max_depth,
        )
    if isinstance(value, str):
        return _sanitize_string(value, max_string_length)
    if isinstance(value, bytes):
        return f"<binary omitted; length={len(value)}>"
    if isinstance(value, Mapping):
        if depth >= max_depth:
            return DEPTH_LIMIT_REACHED
        return _redact_mapping(
            value,
            depth=depth,
            max_string_length=max_string_length,
            max_collection_items=max_collection_items,
            max_depth=max_depth,
        )
    if isinstance(value, (list, tuple)):
        if depth >= max_depth:
            return DEPTH_LIMIT_REACHED
        items = [
            _sanitize_value(
                item,
                depth=depth + 1,
                max_string_length=max_string_length,
                max_collection_items=max_collection_items,
                max_depth=max_depth,
            )
            for item in value[:max_collection_items]
        ]
        if len(value) > max_collection_items:
            items.append(COLLECTION_ITEMS_OMITTED)
        return items
    return UNSUPPORTED_VALUE


def _sanitize_string(value: str, max_length: int) -> str:
    if any(pattern.search(value) for pattern in _SECRET_CONTENT_PATTERNS):
        return REDACTED
    if len(value) <= max_length:
        return value
    return f"{value[:max_length]}...[truncated]"


def _is_content_key(key: str) -> bool:
    try:
        normalized = _normalize_key(key)
        compact = normalized.replace("_", "")
        return (
            normalized in _CONTENT_KEY_NAMES
            or compact in _CONTENT_KEY_COMPACT_NAMES
            or normalized.endswith(_CONTENT_KEY_SUFFIXES)
        )
    except Exception:
        return True


def _generated_summary_keys(items: list[tuple[Any, Any]]) -> set[str]:
    """派生本次 mapping 中 sanitizer 实际可能生成的摘要 key。"""
    generated = set()
    for key, _value in items:
        key_text = _safe_key(key, max_string_length=MAX_KEY_LENGTH)
        if key_text == "<unreadable key>":
            continue
        detection_key = key if isinstance(key, str) else key_text
        if _is_content_key(detection_key):
            generated.add(_normalize_key(f"{key_text}_present"))
            generated.add(_normalize_key(f"{key_text}_length"))
    return generated


def _is_reserved_summary_key(key: str, generated_summary_keys: set[str]) -> bool:
    """阻止不可信 key 覆盖 sanitizer 生成的摘要字段。"""
    try:
        normalized = _normalize_key(str(key))
        return normalized in _RESERVED_SUMMARY_KEY_NAMES or normalized in generated_summary_keys
    except Exception:
        return True


def _normalize_key(key: str) -> str:
    """把常见 camelCase、separator 和 mixed-case key 统一为 snake_case。"""
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return re.sub(r"[^a-z0-9]+", "_", snake_case.lower()).strip("_")


def _safe_key(key: Any, *, max_string_length: int) -> str:
    try:
        if isinstance(key, str):
            text = key
        elif key is None or isinstance(key, (bool, int, float)):
            text = str(key)
        else:
            return "<unreadable key>"
        return text[:max_string_length]
    except Exception:
        return "<unreadable key>"


def _safe_length(value: Any) -> int | None:
    try:
        if isinstance(value, (str, bytes, Mapping, list, tuple)):
            return len(value)
        if value is None:
            return 0
        if isinstance(value, (bool, int, float)):
            return len(str(value))
    except Exception:
        return None
    return None


def _omitted_item_count(data: Mapping, processed: int) -> int | str:
    try:
        return max(0, len(data) - processed)
    except Exception:
        return "unknown"


def _validated_limits(
    max_string_length: int,
    max_collection_items: int,
    max_depth: int,
) -> dict[str, int]:
    requested = {
        "max_string_length": int(max_string_length),
        "max_collection_items": int(max_collection_items),
        "max_depth": int(max_depth),
    }
    if any(value < 1 for value in requested.values()):
        raise ValueError("redaction limits must be positive")
    return {
        "max_string_length": min(requested["max_string_length"], MAX_STRING_LENGTH),
        "max_collection_items": min(requested["max_collection_items"], MAX_COLLECTION_ITEMS),
        "max_depth": min(requested["max_depth"], MAX_RECURSION_DEPTH),
    }
