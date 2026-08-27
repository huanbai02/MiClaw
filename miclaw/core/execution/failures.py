"""定义 execution failure 的安全分类，不保存原始运行时内容。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType


class ExecutionFailureSource(str, Enum):
    """失败发生的执行边界。"""

    TOOL = "tool"
    PROVIDER = "provider"
    RUNTIME = "runtime"


class ExecutionFailureCode(str, Enum):
    """第一版稳定 failure 分类 code。"""

    TOOL_TIMEOUT = "tool_timeout"
    TOOL_TRANSIENT_ERROR = "tool_transient_error"
    TOOL_EXECUTION_ERROR = "tool_execution_error"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_TRANSIENT_ERROR = "provider_transient_error"
    PROVIDER_ERROR = "provider_error"
    PERMISSION_DENIED = "permission_denied"
    PERMISSION_REQUIRED = "permission_required"
    SAFETY_BLOCKED = "safety_blocked"
    INVALID_INPUT = "invalid_input"
    INVALID_TARGET = "invalid_target"
    INVALID_CONFIGURATION = "invalid_configuration"
    RUNTIME_ERROR = "runtime_error"
    UNKNOWN_ERROR = "unknown_error"


class ExecutionFailureValidationError(ValueError):
    """表示不回显原始失败值的稳定 failure validation error。"""


@dataclass(frozen=True, slots=True)
class ExecutionFailure:
    """只携带安全稳定分类的 execution failure。

    Args:
        source: 失败发生的 execution boundary。
        code: 失败的稳定分类 code。
    """

    source: ExecutionFailureSource
    code: ExecutionFailureCode

    def __post_init__(self) -> None:
        """拒绝字符串等未验证分类值。"""
        if type(self.source) is not ExecutionFailureSource or type(self.code) is not ExecutionFailureCode:
            raise ExecutionFailureValidationError("invalid_execution_failure")


# 仅适配当前 ToolResult 的稳定 error_type；未知值必须落入 UNKNOWN_ERROR。
TOOL_ERROR_TYPE_MAPPING = MappingProxyType({
    "timeout": ExecutionFailureCode.TOOL_TIMEOUT,
    "mcp_timeout": ExecutionFailureCode.TOOL_TIMEOUT,
    "mcp_connection_error": ExecutionFailureCode.TOOL_EXECUTION_ERROR,
    "mcp_spawn_error": ExecutionFailureCode.INVALID_CONFIGURATION,
    "blocked_shell_command": ExecutionFailureCode.SAFETY_BLOCKED,
    "memory_write_not_eligible": ExecutionFailureCode.SAFETY_BLOCKED,
    "permission_denied": ExecutionFailureCode.PERMISSION_DENIED,
    "permission_required": ExecutionFailureCode.PERMISSION_REQUIRED,
    "invalid_mode": ExecutionFailureCode.INVALID_INPUT,
    "invalid_mcp_tool": ExecutionFailureCode.INVALID_INPUT,
    "invalid_mcp_arguments": ExecutionFailureCode.INVALID_INPUT,
    "mcp_arguments_too_large": ExecutionFailureCode.INVALID_INPUT,
    "mcp_arguments_too_deep": ExecutionFailureCode.INVALID_INPUT,
    "path_error": ExecutionFailureCode.INVALID_TARGET,
    "file_not_found": ExecutionFailureCode.INVALID_TARGET,
    "mcp_server_mismatch": ExecutionFailureCode.INVALID_TARGET,
    "shell_error": ExecutionFailureCode.TOOL_EXECUTION_ERROR,
    "mcp_protocol_error": ExecutionFailureCode.TOOL_EXECUTION_ERROR,
    "mcp_tool_error": ExecutionFailureCode.TOOL_EXECUTION_ERROR,
    "unsupported_mcp_result_type": ExecutionFailureCode.TOOL_EXECUTION_ERROR,
    "mcp_cursor_cycle": ExecutionFailureCode.TOOL_EXECUTION_ERROR,
    "mcp_tool_list_limit": ExecutionFailureCode.TOOL_EXECUTION_ERROR,
    "mcp_async_required": ExecutionFailureCode.RUNTIME_ERROR,
    "unexpected_error": ExecutionFailureCode.UNKNOWN_ERROR,
})

_PROVIDER_FAILURE_CODES = frozenset(
    {
        ExecutionFailureCode.PROVIDER_TIMEOUT,
        ExecutionFailureCode.PROVIDER_TRANSIENT_ERROR,
        ExecutionFailureCode.PROVIDER_ERROR,
    }
)


def classify_tool_error_type(error_type: str) -> ExecutionFailure:
    """将既有 ToolResult error_type 映射为安全、稳定的 failure 分类。

    Args:
        error_type: ToolResult 产生的稳定 error_type。

    Returns:
        对应的 TOOL source failure；未知字符串统一为 UNKNOWN_ERROR。

    Raises:
        ExecutionFailureValidationError: error_type 不是 exact str 时抛出。
    """
    if type(error_type) is not str:
        raise ExecutionFailureValidationError("invalid_execution_failure")
    return ExecutionFailure(
        ExecutionFailureSource.TOOL,
        TOOL_ERROR_TYPE_MAPPING.get(error_type, ExecutionFailureCode.UNKNOWN_ERROR),
    )


def provider_failure(code: ExecutionFailureCode) -> ExecutionFailure:
    """构造已由未来 provider adapter 正规化的 provider failure。

    Args:
        code: 仅允许 provider source 的稳定 failure code。

    Raises:
        ExecutionFailureValidationError: code 不属于 provider 分类时抛出。
    """
    if type(code) is not ExecutionFailureCode or code not in _PROVIDER_FAILURE_CODES:
        raise ExecutionFailureValidationError("invalid_execution_failure")
    return ExecutionFailure(ExecutionFailureSource.PROVIDER, code)
