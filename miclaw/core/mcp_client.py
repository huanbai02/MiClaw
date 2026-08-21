"""提供现代 MCP 2026-07-28 stdio client 的最小运行边界。

本模块只负责显式 host 配置的单一 stdio server：发现 Tool、在既有 permission
系统授权后调用 Tool，并转换为 ToolResult。它不注册 Agent Tool，也不管理 server
registry、HTTP transport 或认证。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
from typing import Any, TextIO

import anyio
from mcp import Client, StdioServerParameters, types as mcp_types
from mcp.client.stdio import stdio_client

from .mcp_adapter import (
    MCPAdapterError,
    MCPToolDescriptor,
    adapt_mcp_tools,
    validate_mcp_server_id,
)
from .mcp_permissions import authorize_mcp_tool
from .permissions import (
    PermissionConfirmationHandler,
    PermissionDecision,
)
from .tools.result import ToolResult, tool_error, tool_permission_blocked, tool_success


MCP_PROTOCOL_VERSION = "2026-07-28"
MCP_OPERATION_TIMEOUT_SECONDS = 10.0
MAX_TOOL_LIST_PAGES = 100
MAX_MCP_ARGUMENT_BYTES = 65_536
MAX_MCP_ARGUMENT_DEPTH = 16
MAX_MCP_ARGUMENT_ITEMS = 1_000
MAX_MCP_ARGUMENT_STRING_LENGTH = 16_384
MAX_MCP_ARGUMENT_INTEGER_BITS = 256
MCP_RESULT_TEXT_LIMIT = 4_000
MAX_MCP_RESULT_BLOCKS = 100


class MCPClientError(RuntimeError):
    """表示稳定且不含 command/env/path 的 MCP client lifecycle 错误。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class MCPStdioServerConfig:
    """描述一个由 host 显式提供的 MCP stdio server 启动配置。"""

    server_id: str
    command: str = field(repr=False)
    args: tuple[str, ...] = field(default_factory=tuple, repr=False)
    env: dict[str, str] | None = field(default=None, repr=False)
    cwd: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "server_id", validate_mcp_server_id(self.server_id))
        object.__setattr__(self, "command", _validate_launch_string(self.command, "command"))
        object.__setattr__(self, "args", _copy_launch_args(self.args))
        object.__setattr__(self, "env", _copy_launch_env(self.env))
        object.__setattr__(self, "cwd", _validate_cwd(self.cwd))


class MCPStdioClient:
    """管理单一现代 MCP stdio connection，并在 permission 后调用 Tool。"""

    def __init__(
        self,
        config: MCPStdioServerConfig,
        *,
        timeout_seconds: float = MCP_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(config, MCPStdioServerConfig):
            raise TypeError("config must be MCPStdioServerConfig")
        if not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")
        self.config = config
        self.timeout_seconds = float(timeout_seconds)
        self._sdk_client: Client | None = None
        self._errlog: TextIO | None = None
        self._lifecycle_scope: anyio.CancelScope | None = None

    async def __aenter__(self) -> MCPStdioClient:
        """启动 stdio server，并显式使用 MCP 2026-07-28 modern mode。"""
        if self._sdk_client is not None:
            raise MCPClientError("mcp_connection_error", "MCP client is already connected")
        self._errlog = open(os.devnull, "w", encoding="utf-8")
        parameters = StdioServerParameters(
            command=self.config.command,
            args=list(self.config.args),
            env=dict(self.config.env) if self.config.env is not None else None,
            cwd=self.config.cwd,
        )
        client = Client(
            stdio_client(parameters, errlog=self._errlog),
            mode=MCP_PROTOCOL_VERSION,
            read_timeout_seconds=self.timeout_seconds,
        )
        scope = anyio.CancelScope(deadline=anyio.current_time() + self.timeout_seconds)
        scope.__enter__()
        self._lifecycle_scope = scope
        try:
            await client.__aenter__()
        except BaseException as exc:
            timed_out = scope.cancel_called
            scope.__exit__(type(exc), exc, exc.__traceback__)
            self._lifecycle_scope = None
            self._close_errlog()
            if timed_out:
                raise MCPClientError("mcp_timeout", "MCP connection timed out") from None
            if isinstance(exc, anyio.get_cancelled_exc_class()):
                raise
            raise _safe_client_exception(exc, opening=True) from None
        scope.deadline = math.inf
        self._sdk_client = client
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        """交还 SDK lifecycle 管理并确保 stderr sink 被关闭。"""
        client, self._sdk_client = self._sdk_client, None
        scope, self._lifecycle_scope = self._lifecycle_scope, None
        close_error: BaseException | None = None
        try:
            if client is not None:
                if scope is not None:
                    scope.deadline = anyio.current_time() + self.timeout_seconds
                await client.__aexit__(exc_type, exc, traceback)
        except BaseException as caught:
            close_error = caught
        finally:
            timed_out = bool(scope and scope.cancel_called)
            if scope is not None:
                scope.__exit__(
                    type(close_error) if close_error is not None else None,
                    close_error,
                    close_error.__traceback__ if close_error is not None else None,
                )
            self._close_errlog()
        if exc is None and close_error is not None:
            if timed_out:
                raise MCPClientError("mcp_timeout", "MCP shutdown timed out") from None
            raise _safe_client_exception(close_error, opening=False) from None

    async def list_tools(self) -> list[MCPToolDescriptor]:
        """分页读取全部 MCP tools，并通过 PR28 adapter 校验和去重。"""
        client = self._require_client()
        descriptors: list[MCPToolDescriptor] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()

        try:
            for _ in range(MAX_TOOL_LIST_PAGES):
                with anyio.fail_after(self.timeout_seconds):
                    page = await client.list_tools(cursor=cursor, cache_mode="refresh")
                if getattr(page, "result_type", "complete") != "complete":
                    raise MCPClientError(
                        "unsupported_mcp_result_type",
                        "MCP tools/list returned an unsupported result type",
                    )
                for tool in page.tools:
                    wire = tool.model_dump(by_alias=True, mode="json")
                    descriptors.append(
                        MCPToolDescriptor.from_mapping(wire, server_id=self.config.server_id)
                    )
                next_cursor = page.next_cursor
                if next_cursor is None:
                    adapt_mcp_tools(descriptors)
                    return descriptors
                if next_cursor in seen_cursors:
                    raise MCPClientError("mcp_cursor_cycle", "MCP tools/list cursor repeated")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
        except (MCPAdapterError, MCPClientError):
            raise
        except Exception as exc:
            raise _safe_client_exception(exc, opening=False) from None
        raise MCPClientError("mcp_tool_list_limit", "MCP tools/list exceeded page limit")

    async def call_tool(
        self,
        descriptor: MCPToolDescriptor,
        arguments: Mapping[str, Any],
        *,
        confirmation_handler: PermissionConfirmationHandler | None = None,
        thread_id: str = "system",
    ) -> ToolResult:
        """授权后调用同 server Tool；任何非 ALLOW 结果都不会触达 server。"""
        client = self._sdk_client
        if client is None:
            return tool_error("mcp_connection_error", "MCP client is not connected")
        if not isinstance(descriptor, MCPToolDescriptor):
            return tool_error("invalid_mcp_tool", "Invalid MCP tool descriptor")
        if descriptor.server_id != self.config.server_id:
            return tool_error("mcp_server_mismatch", "MCP tool does not belong to this server")

        permission = authorize_mcp_tool(
            descriptor,
            confirmation_handler=confirmation_handler,
            thread_id=thread_id,
        )
        if permission.decision is not PermissionDecision.ALLOW:
            prefix = "Permission required" if permission.decision is PermissionDecision.ASK else "Permission denied"
            return tool_permission_blocked(
                f"{prefix}: {permission.reason}",
                decision=permission.decision.value,
                metadata=_result_metadata(descriptor, permission_decision=permission.decision.value),
            )
        try:
            safe_arguments = _copy_arguments(arguments)
        except MCPClientError as exc:
            return tool_error(
                exc.code,
                exc.message,
                metadata=_result_metadata(descriptor, permission_decision="allow"),
            )

        try:
            with anyio.fail_after(self.timeout_seconds):
                result = await client.session.call_tool(
                    descriptor.name,
                    safe_arguments,
                    read_timeout_seconds=self.timeout_seconds,
                    allow_input_required=True,
                    allow_claimed=True,
                )
        except Exception as exc:
            error = _safe_client_exception(exc, opening=False)
            return tool_error(
                error.code,
                error.message,
                metadata=_result_metadata(descriptor, permission_decision="allow"),
            )
        if not isinstance(result, mcp_types.CallToolResult) or result.result_type != "complete":
            return tool_error(
                "unsupported_mcp_result_type",
                "MCP tool returned an unsupported result type",
                metadata=_result_metadata(descriptor, permission_decision="allow"),
            )
        return _map_tool_result(descriptor, result)

    def _require_client(self) -> Client:
        """返回 active SDK client，否则抛出稳定 lifecycle error。"""
        if self._sdk_client is None:
            raise MCPClientError("mcp_connection_error", "MCP client is not connected")
        return self._sdk_client

    def _close_errlog(self) -> None:
        errlog, self._errlog = self._errlog, None
        if errlog is not None:
            errlog.close()


def _validate_launch_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _copy_launch_args(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("args must be a sequence of strings")
    return tuple(_validate_launch_string(item, "arg") for item in value)


def _copy_launch_env(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("env must be a mapping of strings")
    copied: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or "=" in key or "\x00" in key:
            raise ValueError("env contains an invalid key")
        if not isinstance(item, str) or "\x00" in item:
            raise ValueError("env contains an invalid value")
        copied[key] = item
    return copied


def _validate_cwd(value: Any) -> str | None:
    if value is None:
        return None
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, TypeError, ValueError):
        raise ValueError("cwd must be an existing directory") from None
    if not path.is_dir():
        raise ValueError("cwd must be an existing directory")
    return str(path)


def _copy_arguments(value: Any) -> dict[str, Any]:
    """复制有界 JSON object；失败时不回显原始 arguments。"""
    if not isinstance(value, Mapping):
        raise MCPClientError("invalid_mcp_arguments", "MCP tool arguments must be a mapping")
    try:
        item_count = [0]
        copied = _copy_json_value(value, depth=0, item_count=item_count)
        encoded = json.dumps(copied, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except MCPClientError:
        raise
    except Exception:
        raise MCPClientError("invalid_mcp_arguments", "MCP tool arguments are not JSON-compatible") from None
    if len(encoded.encode("utf-8")) > MAX_MCP_ARGUMENT_BYTES:
        raise MCPClientError("mcp_arguments_too_large", "MCP tool arguments exceed size limits")
    return copied


def _copy_json_value(value: Any, *, depth: int, item_count: list[int]) -> Any:
    if depth > MAX_MCP_ARGUMENT_DEPTH:
        raise MCPClientError("mcp_arguments_too_deep", "MCP tool arguments exceed depth limits")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value.bit_length() > MAX_MCP_ARGUMENT_INTEGER_BITS:
            raise MCPClientError("mcp_arguments_too_large", "MCP tool arguments contain an oversized integer")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MCPClientError("invalid_mcp_arguments", "MCP tool arguments contain a non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > MAX_MCP_ARGUMENT_STRING_LENGTH:
            raise MCPClientError("mcp_arguments_too_large", "MCP tool arguments contain an oversized string")
        return value
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            item_count[0] += 1
            if item_count[0] > MAX_MCP_ARGUMENT_ITEMS:
                raise MCPClientError("mcp_arguments_too_large", "MCP tool arguments contain too many items")
            if not isinstance(key, str):
                raise MCPClientError("invalid_mcp_arguments", "MCP tool argument keys must be strings")
            copied[key] = _copy_json_value(item, depth=depth + 1, item_count=item_count)
        return copied
    if isinstance(value, (list, tuple)):
        copied_items = []
        for item in value:
            item_count[0] += 1
            if item_count[0] > MAX_MCP_ARGUMENT_ITEMS:
                raise MCPClientError("mcp_arguments_too_large", "MCP tool arguments contain too many items")
            copied_items.append(_copy_json_value(item, depth=depth + 1, item_count=item_count))
        return copied_items
    raise MCPClientError("invalid_mcp_arguments", "MCP tool arguments contain an unsupported value")


def _map_tool_result(descriptor: MCPToolDescriptor, result: mcp_types.CallToolResult) -> ToolResult:
    content, truncated, original_length, content_types = _bounded_result_content(result.content)
    metadata = _result_metadata(
        descriptor,
        permission_decision="allow",
        is_error=result.is_error,
        truncated=truncated,
        original_length=original_length,
        content_types=content_types,
    )
    data = _copy_structured_content(result.structured_content)
    if result.is_error:
        return ToolResult(
            success=False,
            content=content,
            data=data,
            error_type="mcp_tool_error",
            error_message="MCP tool reported an error",
            metadata=metadata,
        )
    return tool_success(content, data=data, metadata=metadata)


def _bounded_result_content(content_blocks: Sequence[Any]) -> tuple[str, bool, int, list[str]]:
    parts: list[str] = []
    content_types: list[str] = []
    original_length = 0
    preview_length = 0
    omitted_blocks = max(0, len(content_blocks) - MAX_MCP_RESULT_BLOCKS)
    truncated = omitted_blocks > 0
    for index, block in enumerate(content_blocks[:MAX_MCP_RESULT_BLOCKS]):
        block_type = getattr(block, "type", "unknown")
        content_types.append(block_type if isinstance(block_type, str) else "unknown")
        if isinstance(block, mcp_types.TextContent):
            piece = block.text
        elif block_type == "image":
            piece = "<image content omitted>"
        elif block_type == "audio":
            piece = "<audio content omitted>"
        elif block_type == "resource_link":
            piece = "<resource link omitted>"
        elif block_type == "resource":
            piece = "<resource content omitted>"
        else:
            piece = "<unsupported content omitted>"
        rendered_piece = f"\n{piece}" if index else piece
        original_length += len(rendered_piece)
        remaining = MCP_RESULT_TEXT_LIMIT - preview_length
        if remaining > 0:
            parts.append(rendered_piece[:remaining])
            preview_length += min(len(rendered_piece), remaining)
        if len(rendered_piece) > remaining:
            truncated = True
    if not content_blocks:
        return "<no content>", False, len("<no content>"), content_types
    text = "".join(parts)
    if omitted_blocks:
        original_length += omitted_blocks
    if truncated:
        text = f"{text}\n... [truncated]"
    return text, truncated, original_length, content_types


def _copy_structured_content(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return _copy_arguments(value)
    except MCPClientError:
        return None


def _result_metadata(descriptor: MCPToolDescriptor, **extra: Any) -> dict[str, Any]:
    return {
        "server_id": descriptor.server_id,
        "qualified_tool": descriptor.qualified_name,
        **extra,
    }


def _safe_client_exception(exc: BaseException, *, opening: bool) -> MCPClientError:
    if _exception_contains(exc, TimeoutError):
        return MCPClientError("mcp_timeout", "MCP operation timed out")
    if opening and _exception_contains(exc, (FileNotFoundError, PermissionError, ValueError)):
        return MCPClientError("mcp_spawn_error", "MCP stdio server could not be started")
    if _exception_contains(exc, (anyio.BrokenResourceError, anyio.ClosedResourceError, anyio.EndOfStream)):
        return MCPClientError("mcp_connection_error", "MCP connection failed")
    return MCPClientError(
        "mcp_connection_error" if opening else "mcp_protocol_error",
        "MCP connection failed" if opening else "MCP operation failed",
    )


def _exception_contains(exc: BaseException, expected: type[BaseException] | tuple[type[BaseException], ...]) -> bool:
    if isinstance(exc, expected):
        return True
    children = getattr(exc, "exceptions", ())
    return isinstance(children, tuple) and any(
        isinstance(child, BaseException) and _exception_contains(child, expected)
        for child in children
    )
