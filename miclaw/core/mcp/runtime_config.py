"""加载 host-owned MCP stdio runtime 配置，不向 Agent 暴露启动细节。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .client import MCPStdioServerConfig


MAX_MCP_CONFIG_BYTES = 65_536
_ROOT_KEYS = {"servers"}
_SERVER_KEYS = {"id", "transport", "command", "args", "env", "cwd", "enabled"}


class _DuplicateJSONKeyError(ValueError):
    """阻止 JSON parser 静默覆盖同一 object 内较早的成员。"""


class MCPRuntimeConfigError(ValueError):
    """表示不回显配置内容或路径的稳定 MCP runtime 配置错误。"""

    def __init__(self, code: str = "invalid_mcp_config") -> None:
        self.code = code
        super().__init__(code)


def load_mcp_stdio_configs(path: str | Path | None) -> tuple[MCPStdioServerConfig, ...]:
    """严格读取用户指定的 JSON MCP stdio server 配置。

    Args:
        path: host 在 CLI 提供的配置文件；None 表示不启用 MCP server。

    Returns:
        已启用且经验证的 MCP stdio server 配置。

    Raises:
        MCPRuntimeConfigError: 文件、JSON 或 schema 不符合受控配置契约。
    """
    if path is None:
        return ()
    if not isinstance(path, (str, Path)):
        raise MCPRuntimeConfigError()
    try:
        config_path = Path(path).expanduser().resolve(strict=True)
        if not config_path.is_file() or config_path.stat().st_size > MAX_MCP_CONFIG_BYTES:
            raise MCPRuntimeConfigError()
        payload = json.loads(
            config_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except MCPRuntimeConfigError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise MCPRuntimeConfigError() from None

    if type(payload) is not dict or set(payload) != _ROOT_KEYS or type(payload["servers"]) is not list:
        raise MCPRuntimeConfigError()

    configs: list[MCPStdioServerConfig] = []
    server_ids: set[str] = set()
    for server in payload["servers"]:
        if type(server) is not dict or not set(server) <= _SERVER_KEYS:
            raise MCPRuntimeConfigError()
        if not {"id", "transport", "command", "args"} <= set(server):
            raise MCPRuntimeConfigError()
        server_id = server["id"]
        if type(server_id) is not str or not server_id.strip() or server_id in server_ids:
            raise MCPRuntimeConfigError()
        if (
            server["transport"] != "stdio"
            or type(server["command"]) is not str
            or not server["command"].strip()
        ):
            raise MCPRuntimeConfigError()
        if type(server["args"]) is not list or any(type(arg) is not str for arg in server["args"]):
            raise MCPRuntimeConfigError()
        enabled = server.get("enabled", True)
        if type(enabled) is not bool:
            raise MCPRuntimeConfigError()
        env = server.get("env")
        if env is not None and (
            type(env) is not dict
            or any(type(key) is not str or type(value) is not str for key, value in env.items())
        ):
            raise MCPRuntimeConfigError()
        cwd = server.get("cwd")
        if cwd is not None and type(cwd) is not str:
            raise MCPRuntimeConfigError()
        try:
            config = MCPStdioServerConfig(
                server_id=server_id,
                command=server["command"],
                args=tuple(server["args"]),
                env=env,
                cwd=cwd,
            )
        except (TypeError, ValueError):
            raise MCPRuntimeConfigError() from None
        server_ids.add(server_id)
        if enabled:
            configs.append(config)
    return tuple(configs)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """构造 JSON object 时按 exact key 拒绝重复成员。"""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError()
        result[key] = value
    return result
