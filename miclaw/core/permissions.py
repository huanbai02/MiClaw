"""MiClaw permission model skeleton。

本模块只定义未来工具共享的 permission 语言与保守默认策略，当前不接入
file、shell、network 或 MCP runtime。具体工具仍必须单独做 path、参数和
sandbox 边界校验。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class PermissionCapability(str, Enum):
    """工具能力类型，value 保持 JSON-friendly 且稳定。"""

    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    SHELL_EXEC = "shell_exec"
    NETWORK_ACCESS = "network_access"
    MCP_TOOL = "mcp_tool"
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    UNKNOWN = "unknown"


class PermissionDecision(str, Enum):
    """permission 评估结果。ASK 表示需要用户确认，不等同于 ALLOW。"""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PermissionConfirmationChoice(str, Enum):
    """用户对 ASK permission 的临时确认选择。"""

    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    DENY = "deny"


class RiskLevel(str, Enum):
    """请求风险等级。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class PermissionRequest:
    """描述一次未来工具调用需要评估的 permission request。"""

    capability: PermissionCapability | str = PermissionCapability.UNKNOWN
    operation: str = ""
    target: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    risk_level: RiskLevel | str = RiskLevel.LOW
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability", _coerce_capability(self.capability))
        object.__setattr__(self, "risk_level", _coerce_risk_level(self.risk_level))
        object.__setattr__(self, "operation", str(self.operation or ""))
        object.__setattr__(self, "target", str(self.target or ""))
        object.__setattr__(self, "reason", str(self.reason or ""))
        object.__setattr__(self, "arguments", dict(self.arguments or {}))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    def to_dict(self) -> dict[str, Any]:
        """返回只包含 JSON-friendly 值的 dict。"""
        return {
            "capability": self.capability.value,
            "operation": self.operation,
            "target": self.target,
            "arguments": dict(self.arguments),
            "reason": self.reason,
            "risk_level": self.risk_level.value,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class PermissionResult:
    """描述一次 permission evaluation 的结果。"""

    decision: PermissionDecision | str
    reason: str
    risk_level: RiskLevel | str = RiskLevel.LOW
    metadata: dict[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = field(init=False)

    def __post_init__(self) -> None:
        decision = _coerce_decision(self.decision)
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "reason", str(self.reason or ""))
        object.__setattr__(self, "risk_level", _coerce_risk_level(self.risk_level))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))
        object.__setattr__(self, "requires_confirmation", decision is PermissionDecision.ASK)

    def to_dict(self) -> dict[str, Any]:
        """返回只包含 JSON-friendly 值的 dict。"""
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "risk_level": self.risk_level.value,
            "requires_confirmation": self.requires_confirmation,
            "metadata": dict(self.metadata),
        }


class PermissionConfirmationHandler(Protocol):
    """定义 ASK permission 的确认回调协议。"""

    def __call__(
        self,
        request: PermissionRequest,
        result: PermissionResult,
    ) -> PermissionConfirmationChoice | PermissionDecision:
        """返回确认选择；旧 handler 的 ALLOW/DENY 仍保持兼容。"""
        ...


@dataclass(frozen=True)
class SessionPermissionGrant:
    """描述当前 interactive run 内可精确复用的 permission grant。"""

    capability: PermissionCapability
    operation: str
    tool_name: str
    target_scope: str
    workspace_scope: str
    risk_level: RiskLevel

    @classmethod
    def from_request(cls, request: PermissionRequest) -> SessionPermissionGrant:
        """从已校验的 PermissionRequest 构造精确 grant key。"""
        return cls(
            capability=request.capability,
            operation=request.operation,
            tool_name=str(request.metadata.get("tool_name") or ""),
            target_scope=request.target,
            workspace_scope=str(request.metadata.get("workspace_scope") or "office"),
            risk_level=request.risk_level,
        )


_current_confirmation_handler: ContextVar[PermissionConfirmationHandler | None] = ContextVar(
    "miclaw_permission_confirmation_handler",
    default=None,
)
_current_session_permission_grants: ContextVar[set[SessionPermissionGrant] | None] = ContextVar(
    "miclaw_session_permission_grants",
    default=None,
)


def allow(reason: str, risk_level: RiskLevel = RiskLevel.LOW, **metadata: Any) -> PermissionResult:
    """创建 ALLOW 结果。"""
    return PermissionResult(PermissionDecision.ALLOW, reason, risk_level, metadata)


def deny(reason: str, risk_level: RiskLevel = RiskLevel.HIGH, **metadata: Any) -> PermissionResult:
    """创建 DENY 结果。"""
    return PermissionResult(PermissionDecision.DENY, reason, risk_level, metadata)


def ask(reason: str, risk_level: RiskLevel = RiskLevel.MEDIUM, **metadata: Any) -> PermissionResult:
    """创建 ASK 结果；调用方不能把 ASK 当作 ALLOW。"""
    return PermissionResult(PermissionDecision.ASK, reason, risk_level, metadata)


def evaluate_permission(request: PermissionRequest) -> PermissionResult:
    """使用保守默认策略评估 permission request。

    这是 skeleton policy，不替代具体工具的 path validation、argument validation
    或 sandbox boundary 检查。
    """
    safe_request = request if isinstance(request, PermissionRequest) else PermissionRequest()
    capability = safe_request.capability
    risk_level = safe_request.risk_level

    if capability is PermissionCapability.FILE_READ:
        if risk_level is RiskLevel.LOW:
            return allow("Low-risk file read is allowed by default policy", risk_level)
        return ask("File read above low risk requires confirmation", risk_level)

    if capability is PermissionCapability.FILE_WRITE:
        if risk_level is RiskLevel.LOW:
            return allow("Low-risk file write is allowed by default policy", risk_level)
        return ask("File write above low risk requires confirmation", risk_level)

    if capability is PermissionCapability.SHELL_EXEC:
        return ask("Shell execution requires confirmation by default", risk_level)

    if capability is PermissionCapability.NETWORK_ACCESS:
        return deny("Network access is denied by default", risk_level)

    if capability is PermissionCapability.MCP_TOOL:
        return deny("MCP tool access is denied by default", risk_level)

    if capability is PermissionCapability.MEMORY_READ:
        if risk_level is RiskLevel.LOW:
            return allow("Low-risk memory read is allowed by default policy", risk_level)
        return ask("Memory read above low risk requires confirmation", risk_level)

    if capability is PermissionCapability.MEMORY_WRITE:
        return ask("Memory write requires confirmation by default", risk_level)

    return deny("Unknown capability is denied by default", RiskLevel.HIGH)


def resolve_permission(
    request: PermissionRequest,
    result: PermissionResult,
    confirmation_handler: PermissionConfirmationHandler | None = None,
) -> PermissionResult:
    """解析 ASK permission；任何非明确 ALLOW 的确认结果都 fail closed。

    Args:
        request: 原始 permission request。
        result: permission policy 的原始结果。
        confirmation_handler: 可选确认回调，仅在 policy 返回 ASK 时调用。

    Returns:
        最终用于决定是否执行的 PermissionResult。
    """
    if result.decision is not PermissionDecision.ASK:
        return result

    grants = get_session_permission_grants()
    if grants is not None and SessionPermissionGrant.from_request(request) in grants:
        return allow(
            "Permission allowed by session grant",
            result.risk_level,
            policy_decision=PermissionDecision.ASK.value,
            confirmation_decision=PermissionDecision.ALLOW.value,
            confirmation_choice=PermissionConfirmationChoice.ALLOW_SESSION.value,
            confirmation_source="session_grant",
        )
    if confirmation_handler is None:
        return result

    try:
        confirmation_choice = confirmation_handler(request, result)
    except Exception:
        return deny(
            "Permission confirmation failed closed",
            result.risk_level,
            policy_decision=PermissionDecision.ASK.value,
            confirmation_decision=PermissionDecision.DENY.value,
        )

    if confirmation_choice is PermissionDecision.ALLOW:
        return allow(
            "Permission explicitly confirmed",
            result.risk_level,
            policy_decision=PermissionDecision.ASK.value,
            confirmation_decision=PermissionDecision.ALLOW.value,
        )

    if confirmation_choice is PermissionConfirmationChoice.ALLOW_ONCE:
        return allow(
            "Permission explicitly allowed once",
            result.risk_level,
            policy_decision=PermissionDecision.ASK.value,
            confirmation_decision=PermissionDecision.ALLOW.value,
            confirmation_choice=confirmation_choice.value,
            confirmation_source="interactive",
        )

    if confirmation_choice is PermissionConfirmationChoice.ALLOW_SESSION:
        if grants is None:
            return deny(
                "Session permission grant context is unavailable",
                result.risk_level,
                policy_decision=PermissionDecision.ASK.value,
                confirmation_decision=PermissionDecision.DENY.value,
                confirmation_choice=confirmation_choice.value,
                confirmation_source="interactive",
            )
        grants.add(SessionPermissionGrant.from_request(request))
        return allow(
            "Permission allowed for this session",
            result.risk_level,
            policy_decision=PermissionDecision.ASK.value,
            confirmation_decision=PermissionDecision.ALLOW.value,
            confirmation_choice=confirmation_choice.value,
            confirmation_source="interactive",
        )

    reason = (
        "Permission confirmation denied"
        if confirmation_choice in {PermissionDecision.DENY, PermissionConfirmationChoice.DENY}
        else "Permission confirmation did not explicitly allow"
    )
    confirmation_metadata = {}
    if isinstance(confirmation_choice, PermissionConfirmationChoice):
        confirmation_metadata = {
            "confirmation_choice": confirmation_choice.value,
            "confirmation_source": "interactive",
        }
    return deny(
        reason,
        result.risk_level,
        policy_decision=PermissionDecision.ASK.value,
        confirmation_decision=PermissionDecision.DENY.value,
        **confirmation_metadata,
    )


def get_permission_confirmation_handler() -> PermissionConfirmationHandler | None:
    """返回当前 execution context 绑定的 confirmation handler。"""
    return _current_confirmation_handler.get()


def set_permission_confirmation_handler(
    handler: PermissionConfirmationHandler,
) -> Token[PermissionConfirmationHandler | None]:
    """为当前 execution context 绑定 handler，并返回可 reset 的 token。"""
    return _current_confirmation_handler.set(handler)


def reset_permission_confirmation_handler(token: Token[PermissionConfirmationHandler | None]) -> None:
    """恢复绑定前的 confirmation handler。"""
    _current_confirmation_handler.reset(token)


def get_session_permission_grants() -> set[SessionPermissionGrant] | None:
    """返回当前 interactive run 的内存 grant set；未绑定时返回 None。"""
    return _current_session_permission_grants.get()


def set_session_permission_grants() -> Token[set[SessionPermissionGrant] | None]:
    """为当前 execution context 创建并绑定全新的空 session grant set。"""
    return _current_session_permission_grants.set(set())


def reset_session_permission_grants(token: Token[set[SessionPermissionGrant] | None]) -> None:
    """清除当前 run 的 grants，并恢复先前 execution context。"""
    _current_session_permission_grants.reset(token)


def _coerce_capability(value: Any) -> PermissionCapability:
    """把不可信 capability 输入转换为安全 enum；未知值收敛到 UNKNOWN。"""
    if isinstance(value, PermissionCapability):
        return value
    try:
        return PermissionCapability(str(value))
    except ValueError:
        return PermissionCapability.UNKNOWN


def _coerce_decision(value: Any) -> PermissionDecision:
    """把不可信 decision 输入转换为安全 enum；未知值收敛到 DENY。"""
    if isinstance(value, PermissionDecision):
        return value
    try:
        return PermissionDecision(str(value))
    except ValueError:
        return PermissionDecision.DENY


def _coerce_risk_level(value: Any) -> RiskLevel:
    """把不可信 risk level 输入转换为安全 enum；未知值按 CRITICAL 处理。"""
    if isinstance(value, RiskLevel):
        return value
    try:
        return RiskLevel(str(value))
    except ValueError:
        return RiskLevel.CRITICAL
