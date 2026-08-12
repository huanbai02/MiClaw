"""提供 transport-independent 的 MCP Tool descriptor 与 adapter。

本模块只复制、校验和限定外部 MCP tool metadata，不连接 server、不注册
可执行 Tool，也不参与 permission decision。真实 invocation 由后续执行层负责。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
import math
import re
from typing import Any

from .redaction import REDACTED, sanitize_value


MAX_IDENTITY_LENGTH = 80
MAX_DESCRIPTION_LENGTH = 200
MAX_SCHEMA_DEPTH = 16
MAX_SCHEMA_ITEMS = 1_000
MAX_SCHEMA_BYTES = 65_536
MAX_SCHEMA_STRING_LENGTH = 16_384
MAX_SCHEMA_INTEGER_BITS = 256

_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_POSIX_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![:/\w])/(?:[^\s/]+/)*[^\s/]+")
_WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(r"(?i)\b[A-Z]:\\(?:[^\\\s]+\\)*[^\\\s]+")


class MCPAdapterError(ValueError):
    """表示稳定、无敏感上下文的 MCP descriptor adapter 错误。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class MCPToolDescriptor:
    """描述 MCP server 提供的一个外部 Tool，不保存 connection state。"""

    server_id: str
    name: str
    description: str
    input_schema: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "server_id", _validate_identity_part(self.server_id, "server_id"))
        object.__setattr__(self, "name", _validate_identity_part(self.name, "tool_name"))
        object.__setattr__(self, "description", _sanitize_description(self.description))
        object.__setattr__(self, "input_schema", _copy_input_schema(self.input_schema))

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        server_id: str | None = None,
    ) -> MCPToolDescriptor:
        """从不可信 wire mapping 和显式 source 构造 descriptor。"""
        if not isinstance(value, Mapping):
            raise MCPAdapterError("invalid_descriptor", "MCP tool descriptor must be a mapping")
        try:
            mapped_server_id = value.get("server_id")
            if server_id is not None and mapped_server_id is not None and server_id != mapped_server_id:
                raise MCPAdapterError("conflicting_server_id", "server_id conflicts with descriptor source")
            return cls(
                server_id=server_id if server_id is not None else mapped_server_id,
                name=value.get("name"),
                description=value.get("description", ""),
                input_schema=_resolve_input_schema(value),
            )
        except MCPAdapterError:
            raise
        except Exception:
            raise MCPAdapterError("invalid_descriptor", "MCP tool descriptor cannot be read") from None

    @property
    def qualified_name(self) -> str:
        """返回 deterministic、包含来源的外部 Tool identity。"""
        return f"mcp::{self.server_id}::{self.name}"

    def to_dict(self) -> dict[str, Any]:
        """返回 JSON-friendly defensive copy。"""
        return {
            "server_id": self.server_id,
            "name": self.name,
            "description": self.description,
            "input_schema": _copy_input_schema(self.input_schema),
        }


@dataclass(frozen=True)
class MiClawToolDescriptor:
    """保存 agent/tool registration 后续可消费的非执行型 Tool descriptor。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", _copy_input_schema(self.input_schema))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        """返回 JSON-friendly defensive copy。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": _copy_input_schema(self.input_schema),
            "metadata": dict(self.metadata),
        }


def adapt_mcp_tool(
    descriptor: MCPToolDescriptor | Mapping[str, Any],
    *,
    server_id: str | None = None,
) -> MiClawToolDescriptor:
    """把 MCP descriptor 转换为 MiClaw-compatible 非执行型 descriptor。

    Args:
        descriptor: 已构造的 MCPToolDescriptor 或标准 MCP Tool mapping。
        server_id: wire mapping 未携带来源时必须显式提供的 server identity。

    Returns:
        保留 qualified identity、description 和 JSON Schema 的 defensive copy。

    Raises:
        MCPAdapterError: descriptor 无效或 schema 不安全/无界。
    """
    if isinstance(descriptor, MCPToolDescriptor):
        if server_id is not None and server_id != descriptor.server_id:
            raise MCPAdapterError("conflicting_server_id", "server_id conflicts with descriptor source")
        source = descriptor
    else:
        source = MCPToolDescriptor.from_mapping(descriptor, server_id=server_id)
    return MiClawToolDescriptor(
        name=source.qualified_name,
        description=source.description,
        input_schema=source.input_schema,
        metadata={
            "source": "mcp",
            "server_id": source.server_id,
            "external_name": source.name,
        },
    )


def adapt_mcp_tools(
    descriptors: Sequence[MCPToolDescriptor | Mapping[str, Any]],
    *,
    server_id: str | None = None,
) -> list[MiClawToolDescriptor]:
    """批量转换 MCP tools，并拒绝同一 qualified identity 的重复项。"""
    adapted = []
    identities = set()
    for descriptor in descriptors:
        tool = adapt_mcp_tool(descriptor, server_id=server_id)
        if tool.name in identities:
            raise MCPAdapterError("duplicate_tool_identity", "Duplicate MCP tool identity")
        identities.add(tool.name)
        adapted.append(tool)
    return adapted


def _validate_identity_part(value: Any, field_name: str) -> str:
    """校验 server/tool identity 的最小安全字符集和长度。"""
    if not isinstance(value, str):
        raise MCPAdapterError(f"invalid_{field_name}", f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise MCPAdapterError(f"invalid_{field_name}", f"{field_name} must not be empty")
    if len(normalized) > MAX_IDENTITY_LENGTH:
        raise MCPAdapterError(f"invalid_{field_name}", f"{field_name} is too long")
    if _IDENTITY_PATTERN.fullmatch(normalized) is None:
        raise MCPAdapterError(f"invalid_{field_name}", f"{field_name} contains unsupported characters")
    return normalized


def _sanitize_description(value: Any) -> str:
    """生成有界纯文本 description，并移除明显 credential/path。"""
    if not isinstance(value, str):
        raise MCPAdapterError("invalid_description", "description must be a string")
    sanitized = sanitize_value(value, max_string_length=MAX_DESCRIPTION_LENGTH)
    if not isinstance(sanitized, str):
        return REDACTED
    sanitized = _WINDOWS_ABSOLUTE_PATH_PATTERN.sub("<path omitted>", sanitized)
    sanitized = _POSIX_ABSOLUTE_PATH_PATTERN.sub("<path omitted>", sanitized)
    return "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in sanitized)


def _resolve_input_schema(value: Mapping[str, Any]) -> Any:
    """解析 MCP wire inputSchema 与内部 input_schema alias，并拒绝冲突。"""
    has_wire_schema = "inputSchema" in value
    has_alias_schema = "input_schema" in value
    if has_wire_schema and has_alias_schema:
        # 标准 wire field 与兼容 alias 同时出现时语义不明确，统一 fail closed。
        raise MCPAdapterError("conflicting_input_schema", "inputSchema conflicts with input_schema")
    if has_wire_schema:
        return value["inputSchema"]
    if has_alias_schema:
        return value["input_schema"]
    raise MCPAdapterError("invalid_input_schema", "MCP tool descriptor is missing inputSchema")


def _copy_input_schema(value: Any) -> dict[str, Any]:
    """复制并限定 JSON Schema；超界时拒绝而不截断业务语义。"""
    if not isinstance(value, Mapping):
        raise MCPAdapterError("invalid_input_schema", "input_schema must be a mapping")

    try:
        item_count = [0]
        copied = _copy_json_value(value, depth=0, item_count=item_count)
        encoded = json.dumps(copied, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except MCPAdapterError:
        raise
    except Exception:
        raise MCPAdapterError("invalid_input_schema", "input_schema cannot be safely adapted") from None
    if len(encoded.encode("utf-8")) > MAX_SCHEMA_BYTES:
        raise MCPAdapterError("schema_too_large", "input_schema exceeds adapter bounds")
    _validate_input_schema_shape(copied)
    return copied


def _validate_input_schema_shape(schema: dict[str, Any]) -> None:
    """执行 MCP Tool input schema 所需的最小 root boundary validation。"""
    if schema.get("type") != "object":
        raise MCPAdapterError("invalid_input_schema", "input_schema root type must be object")
    properties = schema.get("properties")
    if properties is not None and not isinstance(properties, dict):
        raise MCPAdapterError("invalid_input_schema", "input_schema properties must be an object")
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list)
        or any(not isinstance(field, str) for field in required)
    ):
        raise MCPAdapterError("invalid_input_schema", "input_schema required must be a string array")


def _copy_json_value(value: Any, *, depth: int, item_count: list[int]) -> Any:
    if depth > MAX_SCHEMA_DEPTH:
        raise MCPAdapterError("schema_too_deep", "input_schema exceeds adapter depth")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value.bit_length() > MAX_SCHEMA_INTEGER_BITS:
            raise MCPAdapterError("schema_too_large", "input_schema contains an oversized integer")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MCPAdapterError("invalid_input_schema", "input_schema contains a non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > MAX_SCHEMA_STRING_LENGTH:
            raise MCPAdapterError("schema_too_large", "input_schema contains an oversized string")
        return value
    if isinstance(value, Mapping):
        copied = {}
        for key, item in value.items():
            item_count[0] += 1
            if item_count[0] > MAX_SCHEMA_ITEMS:
                raise MCPAdapterError("schema_too_large", "input_schema contains too many items")
            if not isinstance(key, str):
                raise MCPAdapterError("invalid_input_schema", "input_schema keys must be strings")
            copied[key] = _copy_json_value(item, depth=depth + 1, item_count=item_count)
        return copied
    if isinstance(value, (list, tuple)):
        copied = []
        for item in value:
            item_count[0] += 1
            if item_count[0] > MAX_SCHEMA_ITEMS:
                raise MCPAdapterError("schema_too_large", "input_schema contains too many items")
            copied.append(_copy_json_value(item, depth=depth + 1, item_count=item_count))
        return copied
    raise MCPAdapterError("invalid_input_schema", "input_schema contains an unsupported value")
