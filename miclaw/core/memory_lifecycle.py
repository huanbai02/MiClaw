"""定义长期 user-profile 写入的显式 intent eligibility 边界。"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .memory import MemoryKind, MemoryScope, MemoryScopeKind
from .memory_permissions import MemoryAuthorization, ResolvedUserProfileTarget, authorize_memory_access, resolve_user_profile_target
from .permissions import PermissionDecision


class MemoryWriteIntent(str, Enum):
    """当前可由 host/runtime 明确证明的长期写入意图。"""

    EXPLICIT_USER_REQUEST = "explicit_user_request"


class MemoryWriteSource(str, Enum):
    """写入来源通道；不表示 profile 正文具有已验证作者身份。"""

    AGENT_TOOL = "agent_tool"


@dataclass(frozen=True)
class MemoryWriteRequest:
    """描述一次不含路径或正文日志的 scoped profile 写入候选。"""

    kind: object
    scope: object
    source: object
    content: object
    write_intent: object


@dataclass(frozen=True)
class MemoryWritePolicyResult:
    """描述 lifecycle eligibility；它不是 filesystem permission decision。"""

    eligible: bool
    reason_code: str


@dataclass(frozen=True)
class MemoryWriteExecutionResult:
    """保存 policy 与（仅 eligible 时）既有 permission 解析结果。"""

    policy_result: MemoryWritePolicyResult
    authorization: MemoryAuthorization | None


_current_memory_write_intent: ContextVar[MemoryWriteIntent | None] = ContextVar(
    "miclaw_memory_write_intent",
    default=None,
)


def set_memory_write_intent(intent: MemoryWriteIntent) -> Token[MemoryWriteIntent | None]:
    """为当前 host-controlled turn 绑定显式写入 intent。

    Args:
        intent: 当前唯一支持的可信 lifecycle intent。
    """
    if intent is not MemoryWriteIntent.EXPLICIT_USER_REQUEST:
        raise ValueError("invalid_memory_write_intent")
    return _current_memory_write_intent.set(intent)


def reset_memory_write_intent(token: Token[MemoryWriteIntent | None]) -> None:
    """恢复外层 turn 的 write-intent ContextVar。"""
    _current_memory_write_intent.reset(token)


def get_memory_write_intent() -> MemoryWriteIntent | None:
    """返回当前可信 intent；默认 None，供 policy fail-closed。"""
    return _current_memory_write_intent.get()


def evaluate_memory_write_policy(request: object) -> MemoryWritePolicyResult:
    """评估长期 Memory 是否有资格申请持久化 permission。

    Args:
        request: 仅由 runtime service 构造的候选写入请求。
    """
    if not isinstance(request, MemoryWriteRequest):
        return MemoryWritePolicyResult(False, "invalid_write_request")
    if request.kind is not MemoryKind.USER_PROFILE:
        return MemoryWritePolicyResult(False, "unsupported_memory_kind")
    if not _valid_scope(request.scope):
        return MemoryWritePolicyResult(False, "invalid_scope")
    if request.source is not MemoryWriteSource.AGENT_TOOL:
        return MemoryWritePolicyResult(False, "invalid_source")
    if type(request.content) is not str:
        return MemoryWritePolicyResult(False, "invalid_content")
    if request.write_intent is not MemoryWriteIntent.EXPLICIT_USER_REQUEST:
        return MemoryWritePolicyResult(False, "unsupported_write_intent")
    return MemoryWritePolicyResult(True, "explicit_user_request")


def evaluate_memory_write_preflight(
    content: object,
    write_intent: object,
) -> MemoryWritePolicyResult:
    """在 target resolution 前校验不依赖 scope 的 lifecycle 条件。"""
    if type(content) is not str:
        return MemoryWritePolicyResult(False, "invalid_content")
    if write_intent is not MemoryWriteIntent.EXPLICIT_USER_REQUEST:
        return MemoryWritePolicyResult(False, "unsupported_write_intent")
    return MemoryWritePolicyResult(True, "explicit_user_request")


def write_user_profile_with_policy(
    memory_dir: Path | str,
    content: object,
) -> MemoryWriteExecutionResult:
    """按 lifecycle policy → permission → Store 的顺序写入 scoped user profile。

    Args:
        memory_dir: 当前 runtime 配置使用的 Memory root。
        content: Tool 传入的完整 profile Markdown；不做隐式类型转换。
    """
    write_intent = get_memory_write_intent()
    preflight_result = evaluate_memory_write_preflight(content, write_intent)
    if not preflight_result.eligible:
        return MemoryWriteExecutionResult(preflight_result, None)

    target = resolve_user_profile_target(memory_dir)
    request = MemoryWriteRequest(
        kind=MemoryKind.USER_PROFILE,
        scope=target.store.scope,
        source=MemoryWriteSource.AGENT_TOOL,
        content=content,
        write_intent=write_intent,
    )
    policy_result = evaluate_memory_write_policy(request)
    if not policy_result.eligible:
        return MemoryWriteExecutionResult(policy_result, None)

    authorization = authorize_memory_access(target, "update", "save_user_profile")
    if authorization.final_result.decision is PermissionDecision.ALLOW:
        authorization.target.store.write_profile(content)
    return MemoryWriteExecutionResult(policy_result, authorization)


def _valid_scope(scope: object) -> bool:
    """严格校验 GLOBAL/PROJECT 的既有逻辑 scope 结构。"""
    if not isinstance(scope, MemoryScope):
        return False
    if scope.kind is MemoryScopeKind.GLOBAL:
        return scope.scope_id is None
    return (
        scope.kind is MemoryScopeKind.PROJECT
        and type(scope.scope_id) is str
        and bool(scope.scope_id.strip())
    )
