"""验证纯 execution retry eligibility policy。"""

from dataclasses import FrozenInstanceError

import pytest

from miclaw.core.execution.failures import (
    ExecutionFailure,
    ExecutionFailureCode,
    ExecutionFailureSource,
    classify_tool_error_type,
)
from miclaw.core.execution.retry import (
    DEFAULT_MAX_ATTEMPTS,
    NON_RETRYABLE_FAILURE_CODES,
    RETRYABLE_FAILURE_CODES,
    RetryDecision,
    RetryDecisionReason,
    RetryEvaluation,
    RetryPolicy,
    RetryPolicyValidationError,
    evaluate_retry,
)


def _failure(code: ExecutionFailureCode) -> ExecutionFailure:
    """为 policy tests 构造不含运行时正文的 failure。"""
    return ExecutionFailure(ExecutionFailureSource.TOOL, code)


def test_default_policy_is_three_total_attempts_not_three_extra_retries():
    """max_attempts 表示总 attempt 数。"""
    assert RetryPolicy().max_attempts == DEFAULT_MAX_ATTEMPTS == 3


@pytest.mark.parametrize("max_attempts", [0, -1, True, False, 1.0, "3", None])
def test_retry_policy_rejects_invalid_max_attempts(max_attempts):
    """policy budget 不接受 bool 或隐式数字转换。"""
    with pytest.raises(RetryPolicyValidationError, match="^invalid_retry_policy$"):
        RetryPolicy(max_attempts)


def test_retry_evaluation_is_frozen_and_contains_only_decision_and_reason():
    """policy 输出不携带异常、路径或其他运行时内容。"""
    evaluation = RetryEvaluation(RetryDecision.RETRY, RetryDecisionReason.RETRYABLE_FAILURE)

    assert tuple(evaluation.__dataclass_fields__) == ("decision", "reason")
    with pytest.raises(FrozenInstanceError):
        evaluation.decision = RetryDecision.DO_NOT_RETRY


@pytest.mark.parametrize("code", list(ExecutionFailureCode))
def test_every_failure_code_has_explicit_retryability_policy(code):
    """新增 code 时必须显式加入 retry 或 non-retry policy。"""
    assert RETRYABLE_FAILURE_CODES.isdisjoint(NON_RETRYABLE_FAILURE_CODES)
    assert RETRYABLE_FAILURE_CODES | NON_RETRYABLE_FAILURE_CODES == frozenset(ExecutionFailureCode)

    evaluation = evaluate_retry(_failure(code), current_attempt=1, policy=RetryPolicy(3))
    if code in RETRYABLE_FAILURE_CODES:
        assert evaluation == RetryEvaluation(RetryDecision.RETRY, RetryDecisionReason.RETRYABLE_FAILURE)
    else:
        assert evaluation == RetryEvaluation(RetryDecision.DO_NOT_RETRY, RetryDecisionReason.NON_RETRYABLE_FAILURE)


@pytest.mark.parametrize(
    "code",
    [
        ExecutionFailureCode.PERMISSION_DENIED,
        ExecutionFailureCode.PERMISSION_REQUIRED,
        ExecutionFailureCode.SAFETY_BLOCKED,
    ],
)
@pytest.mark.parametrize("attempt", [1, 2])
def test_permission_and_safety_failures_are_never_automatic_retry_candidates(code, attempt):
    """安全边界不能借由 retry policy 被反复尝试绕过。"""
    assert evaluate_retry(_failure(code), current_attempt=attempt, policy=RetryPolicy(3)) == RetryEvaluation(
        RetryDecision.DO_NOT_RETRY,
        RetryDecisionReason.NON_RETRYABLE_FAILURE,
    )


def test_unknown_failure_fails_closed():
    """未知分类默认不自动 retry。"""
    assert evaluate_retry(
        _failure(ExecutionFailureCode.UNKNOWN_ERROR), current_attempt=1, policy=RetryPolicy(3)
    ) == RetryEvaluation(RetryDecision.DO_NOT_RETRY, RetryDecisionReason.NON_RETRYABLE_FAILURE)


@pytest.mark.parametrize("error_type", ["mcp_spawn_error", "mcp_connection_error"])
def test_ambiguous_mcp_failures_are_not_automatic_retry_candidates(error_type):
    """MCP taxonomy 未证明 transient 的错误必须 fail closed。"""
    evaluation = evaluate_retry(
        classify_tool_error_type(error_type), current_attempt=1, policy=RetryPolicy(3)
    )

    assert evaluation == RetryEvaluation(RetryDecision.DO_NOT_RETRY, RetryDecisionReason.NON_RETRYABLE_FAILURE)


def test_retryable_failure_stops_at_total_attempt_limit():
    """max_attempts=3 时仅 attempt 1 与 2 可请求下一 attempt。"""
    policy = RetryPolicy(3)
    failure = _failure(ExecutionFailureCode.TOOL_TIMEOUT)

    assert evaluate_retry(failure, current_attempt=1, policy=policy) == RetryEvaluation(
        RetryDecision.RETRY,
        RetryDecisionReason.RETRYABLE_FAILURE,
    )
    assert evaluate_retry(failure, current_attempt=2, policy=policy) == RetryEvaluation(
        RetryDecision.RETRY,
        RetryDecisionReason.RETRYABLE_FAILURE,
    )
    assert evaluate_retry(failure, current_attempt=3, policy=policy) == RetryEvaluation(
        RetryDecision.DO_NOT_RETRY,
        RetryDecisionReason.ATTEMPTS_EXHAUSTED,
    )


def test_single_attempt_policy_exhausts_retryable_failure_immediately():
    """max_attempts=1 没有额外 retry budget。"""
    assert evaluate_retry(
        _failure(ExecutionFailureCode.PROVIDER_TIMEOUT), current_attempt=1, policy=RetryPolicy(1)
    ) == RetryEvaluation(RetryDecision.DO_NOT_RETRY, RetryDecisionReason.ATTEMPTS_EXHAUSTED)


@pytest.mark.parametrize("attempt", [0, -1, True, False, 1.0, "1", None])
def test_retry_evaluation_rejects_invalid_current_attempt(attempt):
    """current_attempt 与 ExecutionState 的编号语义保持一致。"""
    with pytest.raises(RetryPolicyValidationError, match="^invalid_retry_attempt$"):
        evaluate_retry(_failure(ExecutionFailureCode.TOOL_TIMEOUT), current_attempt=attempt, policy=RetryPolicy(3))


def test_retry_evaluation_rejects_attempt_outside_policy_envelope():
    """超过 max_attempts 是 runtime state 异常，而不是普通 exhausted result。"""
    with pytest.raises(RetryPolicyValidationError, match="^invalid_retry_attempt$"):
        evaluate_retry(_failure(ExecutionFailureCode.TOOL_TIMEOUT), current_attempt=4, policy=RetryPolicy(3))


@pytest.mark.parametrize("failure,policy,error", [
    ("failure", RetryPolicy(3), "invalid_execution_failure"),
    (_failure(ExecutionFailureCode.TOOL_TIMEOUT), "policy", "invalid_retry_policy"),
])
def test_retry_evaluation_rejects_malformed_domain_inputs(failure, policy, error):
    """policy 不对不可信对象做 coercion。"""
    with pytest.raises(RetryPolicyValidationError, match=f"^{error}$"):
        evaluate_retry(failure, current_attempt=1, policy=policy)
