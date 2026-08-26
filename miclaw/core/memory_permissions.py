"""将 scoped user-profile 的逻辑 identity 接入现有 permission 流程。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .logger import log_permission_confirmation, log_permission_decision
from .memory import MemoryRecord, MemoryScopeKind
from .permissions import (
    PermissionCapability,
    PermissionDecision,
    PermissionRequest,
    PermissionResult,
    RiskLevel,
    evaluate_permission,
    get_permission_confirmation_handler,
    resolve_permission,
)
from .user_profile import UserProfileStore, derive_project_memory_id, get_user_profile_store
from .workspace import get_active_project_root


@dataclass(frozen=True)
class ResolvedUserProfileTarget:
    """绑定一次 Memory operation 的具体 Store 与逻辑 identity。"""

    store: UserProfileStore
    is_global_fallback: bool = False

    @property
    def memory_id(self) -> str:
        """返回 permission/audit 使用的安全逻辑 identity。"""
        return self.store.memory_id


@dataclass(frozen=True)
class MemoryAuthorization:
    """保存单次已绑定 target 的 policy 与最终 permission 结果。"""

    target: ResolvedUserProfileTarget
    request: PermissionRequest
    policy_result: PermissionResult
    final_result: PermissionResult


@dataclass(frozen=True)
class AuthorizedUserProfileReadOutcome:
    """保存授权读取的最终结果及不含正文的安全 outcome metadata。"""

    record: MemoryRecord | None
    blocked: bool
    block_reason_code: str | None
    used_global_fallback: bool


_permission_evaluator = evaluate_permission
_permission_audit_logger = log_permission_decision
_permission_confirmation_audit_logger = log_permission_confirmation


def resolve_user_profile_target(memory_dir: Path | str) -> ResolvedUserProfileTarget:
    """一次性解析 active workspace 对应的 user-profile persistence target。"""
    return ResolvedUserProfileTarget(get_user_profile_store(memory_dir))


def build_memory_permission_request(
    target: ResolvedUserProfileTarget,
    operation: str,
    tool_name: str,
) -> PermissionRequest:
    """在任何 profile content access 前构造 scope-aware permission request。"""
    capability, risk_level = _operation_permission(operation)
    store_scope = target.store.scope
    if not _target_matches_active_workspace(target):
        return PermissionRequest(
            capability=capability,
            operation=operation,
            target="invalid-memory-target",
            risk_level=risk_level,
            reason="Invalid user profile memory target",
            metadata={
                "tool_name": tool_name,
                "workspace_scope": "unknown",
                "memory_scope": "unknown",
                "memory_kind": "user_profile",
            },
        )

    metadata = {
        "tool_name": tool_name,
        "workspace_scope": store_scope.kind.value,
        "memory_scope": store_scope.kind.value,
        "memory_kind": "user_profile",
    }
    if store_scope.scope_id is not None:
        metadata["memory_scope_id"] = store_scope.scope_id
    return PermissionRequest(
        capability=capability,
        operation=operation,
        target=target.memory_id,
        risk_level=risk_level,
        reason=f"{operation.capitalize()} scoped user profile memory",
        metadata=metadata,
    )


def authorize_memory_access(
    target: ResolvedUserProfileTarget,
    operation: str,
    tool_name: str,
) -> MemoryAuthorization:
    """复用现有 policy、confirmation、session grant 与 audit 解析 Memory access。"""
    request = build_memory_permission_request(target, operation, tool_name)
    policy_result = _permission_evaluator(request)
    _permission_audit_logger(
        request,
        policy_result,
        tool_name=tool_name,
        metadata=request.metadata,
    )
    confirmation_handler = get_permission_confirmation_handler()
    final_result = resolve_permission(request, policy_result, confirmation_handler)
    confirmation_source = final_result.metadata.get("confirmation_source")
    if policy_result.decision is PermissionDecision.ASK and (
        confirmation_handler is not None or confirmation_source == "session_grant"
    ):
        _permission_confirmation_audit_logger(
            request,
            policy_result,
            final_result,
            tool_name=tool_name,
            metadata=request.metadata,
        )
    return MemoryAuthorization(target, request, policy_result, final_result)


def read_authorized_user_profile(memory_dir: Path | str) -> MemoryRecord | None:
    """读取当前 effective profile；PROJECT fallback 是独立的 GLOBAL read authorization。"""
    return read_authorized_user_profile_with_outcome(memory_dir).record


def read_authorized_user_profile_with_outcome(
    memory_dir: Path | str,
) -> AuthorizedUserProfileReadOutcome:
    """读取 current effective profile，并返回供 runtime observability 使用的安全结果。"""
    target = resolve_user_profile_target(memory_dir)
    authorization = authorize_memory_access(target, "read", "user_profile_context")
    if authorization.final_result.decision is not PermissionDecision.ALLOW:
        return AuthorizedUserProfileReadOutcome(
            record=None,
            blocked=True,
            block_reason_code=_read_block_reason_code(authorization.final_result),
            used_global_fallback=False,
        )

    record = target.store.read_primary_record()
    if record is not None or target.store.scope.kind is MemoryScopeKind.GLOBAL:
        return AuthorizedUserProfileReadOutcome(
            record=record,
            blocked=False,
            block_reason_code=None,
            used_global_fallback=False,
        )

    global_path = target.store.global_profile_path
    if global_path is None:
        return AuthorizedUserProfileReadOutcome(None, False, None, False)
    fallback_target = ResolvedUserProfileTarget(UserProfileStore(global_path), is_global_fallback=True)
    fallback_authorization = authorize_memory_access(fallback_target, "read", "user_profile_context")
    if fallback_authorization.final_result.decision is not PermissionDecision.ALLOW:
        return AuthorizedUserProfileReadOutcome(
            record=None,
            blocked=True,
            block_reason_code=_read_block_reason_code(fallback_authorization.final_result),
            used_global_fallback=False,
        )
    fallback_record = fallback_target.store.read_primary_record()
    return AuthorizedUserProfileReadOutcome(
        record=fallback_record,
        blocked=False,
        block_reason_code=None,
        used_global_fallback=fallback_record is not None,
    )


def authorize_user_profile_write(memory_dir: Path | str) -> MemoryAuthorization:
    """绑定当前 target 后解析一次 update permission，不执行写入。"""
    target = resolve_user_profile_target(memory_dir)
    return authorize_memory_access(target, "update", "save_user_profile")


def permission_block_message(result: PermissionResult) -> str:
    """把未允许的 permission result 转换为既有 ToolResult 可用的安全消息。"""
    if result.decision is PermissionDecision.ASK:
        return f"Permission required: {result.reason}"
    return f"Permission denied: {result.reason}"


def _operation_permission(operation: str) -> tuple[PermissionCapability, RiskLevel]:
    """将受限 Memory operation 映射到稳定 capability/risk。"""
    if operation == "read":
        return PermissionCapability.MEMORY_READ, RiskLevel.LOW
    return PermissionCapability.MEMORY_WRITE, RiskLevel.MEDIUM


def _read_block_reason_code(result: PermissionResult) -> str:
    """将已解析的 read block 映射为不暴露 request/detail 的稳定代码。"""
    if result.decision is PermissionDecision.ASK:
        return "permission_required"
    return "permission_denied"


def _target_matches_active_workspace(target: ResolvedUserProfileTarget) -> bool:
    """确保授权 target 与同一次 operation 的 active workspace 一致。"""
    scope = target.store.scope
    active_project_root = get_active_project_root()
    if scope.kind is MemoryScopeKind.GLOBAL:
        return active_project_root is None or target.is_global_fallback
    if scope.kind is not MemoryScopeKind.PROJECT or not scope.scope_id or active_project_root is None:
        return False
    try:
        return scope.scope_id == derive_project_memory_id(active_project_root.path)
    except Exception:
        return False
