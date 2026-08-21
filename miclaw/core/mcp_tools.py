"""把已发现的 MCP descriptors 转换为 MiClaw agent-facing Tools。

本模块只负责稳定命名、wrapper 构造、tool set 合并与 run-scoped client
lifecycle；transport、permission 和执行仍分别由 PR28–30 模块负责。
"""

from __future__ import annotations

from contextlib import AsyncExitStack
import hashlib
import re
from collections.abc import Sequence
from typing import Any

from langchain_core.runnables import ensure_config
from langchain_core.tools import BaseTool

from .mcp_adapter import MCPToolDescriptor
from .mcp_client import MCPStdioClient, MCPStdioServerConfig
from .tools.result import format_tool_result_for_model, tool_error


MAX_AGENT_TOOL_NAME_LENGTH = 64
_AGENT_NAME_PART_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")


class MCPToolRegistrationError(ValueError):
    """表示稳定、不含 server 启动配置的 MCP Tool 注册错误。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class MCPAgentTool(BaseTool):
    """固定绑定一个 client/descriptor 的 agent-facing MCP Tool。"""

    client: MCPStdioClient
    descriptor: MCPToolDescriptor
    args_schema: dict[str, Any]

    def _run(self, **_arguments: object) -> str:
        """同步 graph 不跨 event loop 调用 MCP，返回稳定 fail-closed 结果。"""
        return format_tool_result_for_model(
            tool_error("mcp_async_required", "MCP tools require async agent execution")
        )

    async def _arun(self, **arguments: object) -> str:
        """通过 PR30 执行，并把 agent thread_id 传给 permission audit。"""
        config = ensure_config()
        thread_id = str(config.get("configurable", {}).get("thread_id") or "system")
        result = await self.client.call_tool(
            self.descriptor,
            arguments,
            thread_id=thread_id,
        )
        return format_tool_result_for_model(result)


def mcp_agent_tool_name(descriptor: MCPToolDescriptor) -> str:
    """生成符合常见模型 Tool name 约束的稳定、带 identity digest 的名称。"""
    if not isinstance(descriptor, MCPToolDescriptor):
        raise TypeError("descriptor must be MCPToolDescriptor")
    server = _agent_name_part(descriptor.server_id, 16)
    tool = _agent_name_part(descriptor.name, 20)
    digest = hashlib.sha256(descriptor.qualified_name.encode("utf-8")).hexdigest()[:12]
    return f"mcp__{server}__{tool}__{digest}"[:MAX_AGENT_TOOL_NAME_LENGTH]


def build_mcp_agent_tool(
    client: MCPStdioClient,
    descriptor: MCPToolDescriptor,
) -> MCPAgentTool:
    """构造固定绑定 client/descriptor 的 async agent Tool wrapper。"""
    if not isinstance(client, MCPStdioClient):
        raise TypeError("client must be MCPStdioClient")
    if not isinstance(descriptor, MCPToolDescriptor):
        raise TypeError("descriptor must be MCPToolDescriptor")
    if client.config.server_id != descriptor.server_id:
        raise MCPToolRegistrationError(
            "mcp_server_mismatch",
            "MCP descriptor does not belong to the client server",
        )

    return MCPAgentTool(
        client=client,
        descriptor=descriptor,
        name=mcp_agent_tool_name(descriptor),
        description=descriptor.description or "External MCP tool",
        args_schema=descriptor.to_dict()["input_schema"],
        metadata={
            "source": "mcp",
            "mcp_qualified_name": descriptor.qualified_name,
            "mcp_server_id": descriptor.server_id,
        },
    )


def merge_agent_tools(
    local_tools: Sequence[BaseTool],
    mcp_tools: Sequence[BaseTool],
) -> list[BaseTool]:
    """合并 local/MCP tools，并拒绝任何最终 agent-facing name 冲突。"""
    merged: list[BaseTool] = []
    names: set[str] = set()
    for tool in (*local_tools, *mcp_tools):
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or not name:
            raise MCPToolRegistrationError("invalid_agent_tool", "Agent tool must have a name")
        if name in names:
            raise MCPToolRegistrationError("duplicate_agent_tool", "Duplicate agent tool name")
        names.add(name)
        merged.append(tool)
    return merged


class MCPAgentToolRuntime:
    """在一次 host-controlled run 内启动 servers、发现并注册 MCP tools。"""

    def __init__(
        self,
        configs: Sequence[MCPStdioServerConfig],
        *,
        local_tools: Sequence[BaseTool] = (),
    ) -> None:
        self.configs = tuple(configs)
        self.local_tools = tuple(local_tools)
        self.tools: list[BaseTool] = []
        self.mcp_tools: list[BaseTool] = []
        self.descriptors_by_agent_name: dict[str, MCPToolDescriptor] = {}
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> MCPAgentToolRuntime:
        """全部 server discovery 成功后一次性发布完整 tool set。"""
        if self._stack is not None:
            raise MCPToolRegistrationError("mcp_runtime_active", "MCP tool runtime is already active")
        stack = AsyncExitStack()
        mcp_tools: list[BaseTool] = []
        mapping: dict[str, MCPToolDescriptor] = {}
        try:
            for config in self.configs:
                client = await stack.enter_async_context(MCPStdioClient(config))
                for descriptor in await client.list_tools():
                    tool = build_mcp_agent_tool(client, descriptor)
                    if tool.name in mapping:
                        raise MCPToolRegistrationError(
                            "duplicate_agent_tool",
                            "Duplicate MCP agent tool name",
                        )
                    mapping[tool.name] = descriptor
                    mcp_tools.append(tool)
            tools = merge_agent_tools(self.local_tools, mcp_tools)
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        self.mcp_tools = mcp_tools
        self.descriptors_by_agent_name = mapping
        self.tools = tools
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """关闭本 run 的全部 client，并清除不可继续调用的 wrappers。"""
        stack, self._stack = self._stack, None
        self.tools = []
        self.mcp_tools = []
        self.descriptors_by_agent_name = {}
        if stack is not None:
            await stack.__aexit__(exc_type, exc, traceback)


def _agent_name_part(value: str, limit: int) -> str:
    normalized = _AGENT_NAME_PART_PATTERN.sub("_", value).strip("_")
    return (normalized or "tool")[:limit]
