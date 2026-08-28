"""定义 execution failure 的纯 retry eligibility policy，不执行重试。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .failures import ExecutionFailure, ExecutionFailureCode


DEFAULT_MAX_ATTEMPTS = 3


class RetryDecision(str, Enum):
    """当前 attempt 后是否可以由未来 runtime 创建下一 attempt。"""

    RETRY = "retry"
    DO_NOT_RETRY = "do_not_retry"


class RetryDecisionReason(str, Enum):
    """RetryDecision 的稳定安全原因。"""

    RETRYABLE_FAILURE = "retryable_failure"
    NON_RETRYABLE_FAILURE = "non_retryable_failure"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"


class RetryPolicyValidationError(ValueError):
    """表示不回显 runtime detail 的稳定 retry policy validation error。"""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """限制一个 logical execution 可拥有的总 attempt 数。

    Args:
        max_attempts: 总 attempt 上限；不是额外 retry 次数。
    """

    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    def __post_init__(self) -> None:
        """拒绝 bool、coercion 与无效 attempt budget。"""
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise RetryPolicyValidationError("invalid_retry_policy")


@dataclass(frozen=True, slots=True)
class RetryEvaluation:
    """纯 retry policy 的安全二元决策结果。

    Args:
        decision: 是否可在未来创建下一 attempt。
        reason: 此 decision 的稳定原因。
    """

    decision: RetryDecision
    reason: RetryDecisionReason

    def __post_init__(self) -> None:
        """保持结果对象只接受稳定枚举值。"""
        if type(self.decision) is not RetryDecision or type(self.reason) is not RetryDecisionReason:
            raise RetryPolicyValidationError("invalid_retry_evaluation")


RETRYABLE_FAILURE_CODES = frozenset(
    {
        ExecutionFailureCode.TOOL_TIMEOUT,
        ExecutionFailureCode.TOOL_TRANSIENT_ERROR,
        ExecutionFailureCode.PROVIDER_TIMEOUT,
        ExecutionFailureCode.PROVIDER_TRANSIENT_ERROR,
    }
)
NON_RETRYABLE_FAILURE_CODES = frozenset(set(ExecutionFailureCode) - RETRYABLE_FAILURE_CODES)


def evaluate_retry(
    failure: ExecutionFailure,
    *,
    current_attempt: int,
    policy: RetryPolicy,
) -> RetryEvaluation:
    """根据稳定 failure 分类与总 attempt 上限作出 retry 决策。

    Args:
        failure: 当前已分类 failure。
        current_attempt: 已结束的 attempt 编号。
        policy: 总 attempt 上限。

    Returns:
        只包含稳定枚举的 retry decision。

    Raises:
        RetryPolicyValidationError: 输入不是有效 policy envelope 时抛出。
    """
    if type(failure) is not ExecutionFailure:
        raise RetryPolicyValidationError("invalid_execution_failure")
    if type(policy) is not RetryPolicy:
        raise RetryPolicyValidationError("invalid_retry_policy")
    if type(current_attempt) is not int or current_attempt < 1 or current_attempt > policy.max_attempts:
        raise RetryPolicyValidationError("invalid_retry_attempt")
    if failure.code not in RETRYABLE_FAILURE_CODES:
        return RetryEvaluation(RetryDecision.DO_NOT_RETRY, RetryDecisionReason.NON_RETRYABLE_FAILURE)
    if current_attempt >= policy.max_attempts:
        return RetryEvaluation(RetryDecision.DO_NOT_RETRY, RetryDecisionReason.ATTEMPTS_EXHAUSTED)
    return RetryEvaluation(RetryDecision.RETRY, RetryDecisionReason.RETRYABLE_FAILURE)
