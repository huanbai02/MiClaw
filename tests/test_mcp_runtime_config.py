"""锁定 CLI MCP stdio JSON 配置的严格 host-owned 边界。"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from entry.cli import app
from miclaw.core.mcp.runtime_config import MCPRuntimeConfigError, load_mcp_stdio_configs


def _write_config(tmp_path, payload) -> str:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _write_raw_config(tmp_path, content: str) -> str:
    path = tmp_path / "mcp.json"
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_load_mcp_stdio_configs_accepts_only_enabled_stdio_servers(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "servers": [
                {
                    "id": "enabled",
                    "transport": "stdio",
                    "command": "python",
                    "args": ["server.py"],
                    "env": {"TOKEN": "value"},
                    "cwd": str(tmp_path),
                },
                {"id": "disabled", "transport": "stdio", "command": "python", "args": [], "enabled": False},
            ]
        },
    )

    configs = load_mcp_stdio_configs(path)

    assert [config.server_id for config in configs] == ["enabled"]
    assert configs[0].args == ("server.py",)
    assert configs[0].env == {"TOKEN": "value"}
    assert configs[0].cwd == str(tmp_path.resolve())


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"servers": {},},
        {"servers": [{"id": "x", "transport": "stdio", "command": "python", "args": [], "extra": {}}]},
        {"servers": [{"id": "x", "transport": "http", "command": "python", "args": []}]},
        {"servers": [{"id": "x", "transport": "stdio", "command": "python", "args": "server.py"}]},
        {"servers": [{"id": "x", "transport": "stdio", "command": "   ", "args": []}]},
        {"servers": [{"id": "x", "transport": "stdio", "command": "python", "args": [], "enabled": 1}]},
        {
            "servers": [
                {"id": "x", "transport": "stdio", "command": "python", "args": []},
                {"id": "x", "transport": "stdio", "command": "python", "args": []},
            ]
        },
    ],
)
def test_load_mcp_stdio_configs_rejects_malformed_or_ambiguous_config(tmp_path, payload):
    with pytest.raises(MCPRuntimeConfigError, match="invalid_mcp_config"):
        load_mcp_stdio_configs(_write_config(tmp_path, payload))


def test_no_mcp_config_is_an_empty_runtime_configuration():
    assert load_mcp_stdio_configs(None) == ()


@pytest.mark.parametrize(
    "content",
    [
        '{"servers": [], "servers": []}',
        '{"servers": [{"id": "demo", "transport": "stdio", "command": "python-a", "command": "python-b", "args": []}]}',
        '{"servers": [{"id": "demo", "transport": "stdio", "command": "python", "args": [], "env": {"TOKEN": "a", "TOKEN": "b"}}]}',
    ],
    ids=["root", "server-field", "nested-env"],
)
def test_load_mcp_stdio_configs_rejects_duplicate_keys_at_any_object_level(tmp_path, content):
    with pytest.raises(MCPRuntimeConfigError, match="invalid_mcp_config"):
        load_mcp_stdio_configs(_write_raw_config(tmp_path, content))


def test_load_mcp_stdio_configs_keeps_invalid_json_normalized(tmp_path):
    with pytest.raises(MCPRuntimeConfigError, match="invalid_mcp_config"):
        load_mcp_stdio_configs(_write_raw_config(tmp_path, '{"servers": ['))


def test_run_help_exposes_host_controlled_mcp_config_option():
    result = CliRunner().invoke(app, ["run", "--help"])

    assert result.exit_code == 0
    assert "--mcp-config" in result.output
