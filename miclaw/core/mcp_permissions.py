"""把已适配的 MCP Tool descriptor 接入现有 permission boundary。

本模块只构造、评估和审计 PermissionRequest，最终返回 PermissionResult；
不会连接 MCP server、执行 Tool 或处理 ToolResult。
"""

from __future__ import annotations

from typing import Any

from .logger import log_permission_confirmation, log_permission_decision
from .mcp_adapter import MCPToolDescriptor
from .permissions import (
    PermissionCapability,
    PermissionConfirmationHandler,
    PermissionDecision,
    PermissionRequest,
    PermissionResult,
    RiskLevel,
    evaluate_permission,
    get_permission_confirmation_handler,
    resolve_permission,
)


MCP_PERMISSION_OPERATION = "invoke"

_permission_evaluator = evaluate_permission
_permission_decision_audit_logger = log_permission_decision
_permission_confirmation_audit_logger = log_permission_confirmation


def build_mcp_permission_request(descriptor: MCPToolDescriptor) -> PermissionRequest:
    """从已验证 descriptor 构造不含 schema/description/arguments 的 request。

    Args:
        descriptor: PR28 产生的 MCPToolDescriptor。

    Returns:
        使用 MCP_TOOL、invoke 和 HIGH risk 的 PermissionRequest。

    Raises:
        TypeError: 输入不是 MCPToolDescriptor。
    """
    if not isinstance(descriptor, MCPToolDescriptor):
        raise TypeError("descriptor must be MCPToolDescriptor")
    qualified_name = descriptor.qualified_name
    return PermissionRequest(
        capability=PermissionCapability.MCP_TOOL,
        operation=MCP_PERMISSION_OPERATION,
        target=qualified_name,
        arguments={},
        reason="Invoke external MCP tool",
        risk_level=RiskLevel.HIGH,
        metadata={
            "tool_name": qualified_name,
            "mcp_server_id": descriptor.server_id,
            "mcp_tool_name": descriptor.name,
        },
    )


def authorize_mcp_tool(
    descriptor: Any,
    *,
    confirmation_handler: PermissionConfirmationHandler | None = None,
    thread_id: str = "system",
) -> PermissionResult:
    """评估并解析 MCP Tool permission，但不执行 Tool。

    Args:
        descriptor: 已验证 MCPToolDescriptor；其他输入按无效 identity fail closed。
        confirmation_handler: 可选确认 handler；未提供时读取当前 ContextVar。
        thread_id: 写入现有 permission audit 的 thread 标识。

    Returns:
        最终 ALLOW、DENY 或未确认 ASK PermissionResult。
    """
    request = _build_safe_request(descriptor)
    policy_result = _permission_evaluator(request)
    tool_name = str(request.metadata.get("tool_name") or "unknown")
    _permission_decision_audit_logger(
        request,
        policy_result,
        tool_name=tool_name,
        metadata={"tool_name": tool_name},
        thread_id=thread_id,
    )

    handler = (
        confirmation_handler
        if confirmation_handler is not None
        else get_permission_confirmation_handler()
    )
    final_result = resolve_permission(request, policy_result, handler)
    confirmation_source = final_result.metadata.get("confirmation_source")
    if policy_result.decision is PermissionDecision.ASK and (
        handler is not None or confirmation_source == "session_grant"
    ):
        _permission_confirmation_audit_logger(
            request,
            policy_result,
            final_result,
            tool_name=tool_name,
            metadata={"tool_name": tool_name},
            thread_id=thread_id,
        )
    return final_result


def _build_safe_request(descriptor: Any) -> PermissionRequest:
    """把 boundary 类型错误收敛为可审计且必定 DENY 的 request。"""
    if isinstance(descriptor, MCPToolDescriptor):
        return build_mcp_permission_request(descriptor)
    return PermissionRequest(
        capability=PermissionCapability.MCP_TOOL,
        operation=MCP_PERMISSION_OPERATION,
        target="invalid_mcp_tool",
        arguments={},
        reason="Invalid MCP tool descriptor",
        risk_level=RiskLevel.HIGH,
        metadata={"tool_name": "unknown"},
    )
