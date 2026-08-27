"""Execution checkpoint recovery 的纯模型，不访问数据库或 LangGraph。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .failures import ExecutionFailure
from .models import ExecutionState
from .retry import RetryEvaluation


class RecoveryDecision(str, Enum):
    """针对某个已持久化 attempt 的安全恢复动作。"""

    MARK_SUCCEEDED = "mark_succeeded"
    RESUME_FROM_CHECKPOINT = "resume_from_checkpoint"
    DO_NOT_RESUME = "do_not_resume"


class RecoveryReason(str, Enum):
    """RecoveryDecision 的稳定安全原因。"""

    GRAPH_ALREADY_COMPLETE = "graph_already_complete"
    SAFE_CHECKPOINT_AVAILABLE = "safe_checkpoint_available"
    NO_MATCHING_CHECKPOINT = "no_matching_checkpoint"
    CHECKPOINT_OWNERSHIP_MISMATCH = "checkpoint_ownership_mismatch"
    UNSAFE_NEXT_NODE = "unsafe_next_node"
    MALFORMED_CHECKPOINT = "malformed_checkpoint"
    STATUS_NOT_RECOVERABLE = "status_not_recoverable"


class RecoveryValidationError(ValueError):
    """表示不回显记录或 checkpoint 内容的稳定恢复验证错误。"""


@dataclass(frozen=True, slots=True)
class CheckpointRef:
    """仅保存 checkpoint 的 opaque 归属标识。"""

    thread_id: str
    run_id: str
    checkpoint_id: str

    def __post_init__(self) -> None:
        """拒绝缺失、空白或隐式转换的 checkpoint identity。"""
        for value in (self.thread_id, self.run_id, self.checkpoint_id):
            if type(value) is not str or not value.strip():
                raise RecoveryValidationError("invalid_recovery_record")


@dataclass(frozen=True, slots=True)
class ExecutionAttemptRecord:
    """ExecutionStore 保存的 attempt 元数据，不包含 graph payload。"""

    state: ExecutionState
    failure: ExecutionFailure | None = None
    retry_evaluation: RetryEvaluation | None = None
    checkpoint_thread_id: str | None = None
    checkpoint_run_id: str | None = None
    checkpoint_id: str | None = None

    def __post_init__(self) -> None:
        """验证记录只由既有稳定领域对象与 opaque checkpoint 标识组成。"""
        if type(self.state) is not ExecutionState:
            raise RecoveryValidationError("invalid_recovery_record")
        if self.failure is not None and type(self.failure) is not ExecutionFailure:
            raise RecoveryValidationError("invalid_recovery_record")
        if self.retry_evaluation is not None and type(self.retry_evaluation) is not RetryEvaluation:
            raise RecoveryValidationError("invalid_recovery_record")
        for value in (self.checkpoint_thread_id, self.checkpoint_run_id, self.checkpoint_id):
            if value is not None and (type(value) is not str or not value.strip()):
                raise RecoveryValidationError("invalid_recovery_record")
        if (self.failure is None) != (self.retry_evaluation is None):
            raise RecoveryValidationError("invalid_recovery_record")


@dataclass(frozen=True, slots=True)
class RecoveryAssessment:
    """不含 LangGraph state 的 recovery assessment 结果。"""

    decision: RecoveryDecision
    reason: RecoveryReason
    checkpoint_ref: CheckpointRef | None = None

    def __post_init__(self) -> None:
        """保持 assessment 只接受稳定 enum 与可选 opaque ref。"""
        if type(self.decision) is not RecoveryDecision or type(self.reason) is not RecoveryReason:
            raise RecoveryValidationError("invalid_recovery_record")
        if self.checkpoint_ref is not None and type(self.checkpoint_ref) is not CheckpointRef:
            raise RecoveryValidationError("invalid_recovery_record")
        if self.decision is RecoveryDecision.RESUME_FROM_CHECKPOINT and self.checkpoint_ref is None:
            raise RecoveryValidationError("invalid_recovery_record")
