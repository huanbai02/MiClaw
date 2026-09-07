"""验证纯 execution failure 分类边界。"""

from dataclasses import FrozenInstanceError

import pytest

from miclaw.core.execution.failures import (
    ExecutionFailure,
    ExecutionFailureCode,
    ExecutionFailureSource,
    ExecutionFailureValidationError,
    TOOL_ERROR_TYPE_MAPPING,
    classify_tool_error_type,
    provider_failure,
)


KNOWN_TOOL_ERROR_MAPPINGS = {
    "timeout": ExecutionFailureCode.TOOL_TIMEOUT,
    "mcp_timeout": ExecutionFailureCode.TOOL_TIMEOUT,
    "mcp_connection_error": ExecutionFailureCode.TOOL_EXECUTION_ERROR,
    "mcp_spawn_error": ExecutionFailureCode.INVALID_CONFIGURATION,
    "blocked_shell_command": ExecutionFailureCode.SAFETY_BLOCKED,
    "memory_write_not_eligible": ExecutionFailureCode.SAFETY_BLOCKED,
    "permission_denied": ExecutionFailureCode.PERMISSION_DENIED,
    "permission_required": ExecutionFailureCode.PERMISSION_REQUIRED,
    "invalid_mode": ExecutionFailureCode.INVALID_INPUT,
    "invalid_input": ExecutionFailureCode.INVALID_INPUT,
    "invalid_target": ExecutionFailureCode.INVALID_TARGET,
    "tool_execution_error": ExecutionFailureCode.TOOL_EXECUTION_ERROR,
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
}


def test_execution_failure_is_frozen_and_only_contains_stable_enums():
    """分类对象不保存原始异常或可变运行时内容。"""
    failure = ExecutionFailure(ExecutionFailureSource.TOOL, ExecutionFailureCode.TOOL_TIMEOUT)

    assert failure.source is ExecutionFailureSource.TOOL
    assert failure.code is ExecutionFailureCode.TOOL_TIMEOUT
    assert tuple(failure.__dataclass_fields__) == ("source", "code")
    with pytest.raises(FrozenInstanceError):
        failure.code = ExecutionFailureCode.UNKNOWN_ERROR


@pytest.mark.parametrize("source,code", [
    ("tool", ExecutionFailureCode.TOOL_TIMEOUT),
    (ExecutionFailureSource.TOOL, "tool_timeout"),
    (None, None),
    ([], {}),
])
def test_execution_failure_rejects_malformed_enums_without_echoing_values(source, code):
    """failure model 不做字符串 coercion 或泄露原始输入。"""
    with pytest.raises(ExecutionFailureValidationError, match="^invalid_execution_failure$") as raised:
        ExecutionFailure(source, code)

    assert str(raised.value) == "invalid_execution_failure"


def test_known_tool_error_types_have_exact_stable_mappings():
    """当前 ToolResult taxonomy 全部通过精确字典映射。"""
    assert dict(TOOL_ERROR_TYPE_MAPPING) == KNOWN_TOOL_ERROR_MAPPINGS
    for error_type, expected_code in KNOWN_TOOL_ERROR_MAPPINGS.items():
        assert classify_tool_error_type(error_type) == ExecutionFailure(ExecutionFailureSource.TOOL, expected_code)


def test_unknown_or_empty_tool_error_type_fails_closed_without_retaining_raw_value():
    """未来 error_type 不得默认成为 retry candidate 或泄露原始字符串。"""
    failure = classify_tool_error_type("NEW_SECRET_ERROR_TYPE")

    assert failure == ExecutionFailure(ExecutionFailureSource.TOOL, ExecutionFailureCode.UNKNOWN_ERROR)
    assert "NEW_SECRET_ERROR_TYPE" not in repr(failure)
    assert classify_tool_error_type("").code is ExecutionFailureCode.UNKNOWN_ERROR


@pytest.mark.parametrize("error_type", [None, True, 1, [], {}])
def test_tool_error_mapping_requires_exact_string(error_type):
    """ToolResult taxonomy adapter 拒绝不可信的非字符串值。"""
    with pytest.raises(ExecutionFailureValidationError, match="^invalid_execution_failure$"):
        classify_tool_error_type(error_type)


@pytest.mark.parametrize(
    "code",
    [
        ExecutionFailureCode.PROVIDER_TIMEOUT,
        ExecutionFailureCode.PROVIDER_TRANSIENT_ERROR,
        ExecutionFailureCode.PROVIDER_ERROR,
    ],
)
def test_provider_failure_accepts_only_explicit_provider_codes(code):
    """provider adapter 只能构造 provider vocabulary 内的 failure。"""
    assert provider_failure(code) == ExecutionFailure(ExecutionFailureSource.PROVIDER, code)


@pytest.mark.parametrize("code", [ExecutionFailureCode.TOOL_TIMEOUT, "provider_timeout", None])
def test_provider_failure_rejects_non_provider_or_malformed_codes(code):
    """provider helper 不接受跨 source code 或 coercion。"""
    with pytest.raises(ExecutionFailureValidationError, match="^invalid_execution_failure$"):
        provider_failure(code)
