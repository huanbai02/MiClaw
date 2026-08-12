import json
from collections.abc import Mapping

import pytest

from miclaw.core.mcp_adapter import (
    MAX_DESCRIPTION_LENGTH,
    MCPAdapterError,
    MCPToolDescriptor,
    adapt_mcp_tool,
    adapt_mcp_tools,
)


def _descriptor(**overrides):
    value = {
        "server_id": "server-a",
        "name": "search",
        "description": "Search indexed documents",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
    value.update(overrides)
    return value


def test_adapt_valid_descriptor_preserves_tool_contract():
    tool = adapt_mcp_tool(_descriptor())

    assert tool.name == "mcp::server-a::search"
    assert tool.description == "Search indexed documents"
    assert tool.input_schema == _descriptor()["input_schema"]
    assert tool.metadata == {
        "source": "mcp",
        "server_id": "server-a",
        "external_name": "search",
    }
    json.dumps(tool.to_dict(), ensure_ascii=False)


def test_same_name_from_different_servers_can_coexist():
    tools = adapt_mcp_tools([
        _descriptor(server_id="server-a"),
        _descriptor(server_id="server-b"),
    ])

    assert [tool.name for tool in tools] == [
        "mcp::server-a::search",
        "mcp::server-b::search",
    ]


def test_duplicate_qualified_identity_is_rejected():
    with pytest.raises(MCPAdapterError) as caught:
        adapt_mcp_tools([_descriptor(), _descriptor()])

    assert caught.value.code == "duplicate_tool_identity"
    assert "server-a" not in str(caught.value)


def test_adapter_defensively_copies_nested_schema():
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["fast", "safe"]},
            "options": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
        },
        "required": ["mode"],
    }
    descriptor = MCPToolDescriptor(**_descriptor(input_schema=schema))
    tool = adapt_mcp_tool(descriptor)
    schema["properties"]["mode"]["enum"].append("mutated")
    descriptor.input_schema["required"].append("mutated")

    assert tool.input_schema["properties"]["mode"]["enum"] == ["fast", "safe"]
    assert tool.input_schema["required"] == ["mode"]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"name": None}, "invalid_tool_name"),
        ({"name": ""}, "invalid_tool_name"),
        ({"name": 12}, "invalid_tool_name"),
        ({"name": "x" * 81}, "invalid_tool_name"),
        ({"server_id": ""}, "invalid_server_id"),
        ({"input_schema": []}, "invalid_input_schema"),
        ({"input_schema": {"type": object()}}, "invalid_input_schema"),
    ],
)
def test_invalid_descriptors_fail_with_stable_error(overrides, code):
    with pytest.raises(MCPAdapterError) as caught:
        adapt_mcp_tool(_descriptor(**overrides))

    assert caught.value.code == code
    assert "/home/" not in str(caught.value)


def test_missing_name_fails_safely():
    descriptor = _descriptor()
    descriptor.pop("name")

    with pytest.raises(MCPAdapterError) as caught:
        adapt_mcp_tool(descriptor)

    assert caught.value.code == "invalid_tool_name"


def test_unexpected_mapping_failure_remains_fail_safe():
    class BrokenMapping(Mapping):
        def __getitem__(self, key):
            raise RuntimeError("/home/user/private")

        def __iter__(self):
            raise RuntimeError("/home/user/private")

        def __len__(self):
            return 1

    with pytest.raises(MCPAdapterError) as caught:
        adapt_mcp_tool(_descriptor(input_schema=BrokenMapping()))

    assert caught.value.code == "invalid_input_schema"
    assert "/home/user" not in str(caught.value)


def test_description_is_bounded_and_sensitive_values_are_not_preserved():
    long_description = "x" * (MAX_DESCRIPTION_LENGTH + 100)
    bounded = adapt_mcp_tool(_descriptor(description=long_description))
    sensitive = adapt_mcp_tool(
        _descriptor(description="Authorization: Bearer SECRET_TOKEN at /home/user/private")
    )

    assert len(bounded.description) <= MAX_DESCRIPTION_LENGTH + len("...[truncated]")
    assert bounded.description.endswith("...[truncated]")
    assert sensitive.description == "<redacted>"
    assert "SECRET_TOKEN" not in sensitive.description
    assert "/home/user" not in sensitive.description


def test_absolute_paths_in_description_are_omitted():
    tool = adapt_mcp_tool(_descriptor(description="Read /home/user/private and C:\\Users\\x\\secret.txt"))

    assert "/home/user" not in tool.description
    assert "C:\\Users" not in tool.description
    assert tool.description.count("<path omitted>") == 2


def test_huge_schema_is_rejected_with_bounded_error():
    schema = {"type": "object", "description": "x" * 20_000}

    with pytest.raises(MCPAdapterError) as caught:
        adapt_mcp_tool(_descriptor(input_schema=schema))

    assert caught.value.code == "schema_too_large"
    assert len(str(caught.value)) < 100


def test_adapter_has_no_execution_or_permission_surface():
    tool = adapt_mcp_tool(_descriptor())

    assert not hasattr(tool, "invoke")
    assert not hasattr(tool, "run")
    assert set(tool.to_dict()) == {"name", "description", "input_schema", "metadata"}
