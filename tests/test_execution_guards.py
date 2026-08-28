"""验证纯 execution semantic Tool loop guard。"""

import math

import pytest

from miclaw.core.execution.guards import (
    MAX_GUARD_CANONICAL_ARG_BYTES,
    ExecutionGuardPolicy,
    ExecutionGuardState,
    ExecutionGuardValidationError,
    GuardDecision,
    GuardReason,
    observe_tool_call_batch,
)


def _call(name="tool", args=None, call_id="call-1"):
    """构造与 AIMessage.tool_calls 相同形状的测试调用。"""
    return {"name": name, "args": {} if args is None else args, "id": call_id}


def test_repeated_identical_calls_block_before_threshold_call_and_keep_state_uncommitted():
    """第 3 次相同调用被阻断，前两个观察结果才会成为已提交 state。"""
    policy = ExecutionGuardPolicy(3)
    first = observe_tool_call_batch(ExecutionGuardState(), [_call(args={"x": 1})], policy)
    second = observe_tool_call_batch(first.state, [_call(args={"x": 1}, call_id="call-2")], policy)
    third = observe_tool_call_batch(second.state, [_call(args={"x": 1}, call_id="call-3")], policy)

    assert first.state.consecutive_identical_count == 1
    assert second.state.consecutive_identical_count == 2
    assert third.decision is GuardDecision.BLOCK
    assert third.reason is GuardReason.REPEATED_IDENTICAL_TOOL_CALL
    assert third.state == second.state


def test_batch_preflight_blocks_entire_batch_without_partial_state_commit():
    """batch 内触发 guard 时，调用方应让整个 ToolNode batch 零执行。"""
    policy = ExecutionGuardPolicy(3)
    first = observe_tool_call_batch(ExecutionGuardState(), [_call(args={"x": 1})], policy)
    second = observe_tool_call_batch(first.state, [_call(args={"x": 1})], policy)
    blocked = observe_tool_call_batch(second.state, [_call(args={"x": 1}), _call(args={"x": 1}, call_id="call-2")], policy)

    assert blocked.decision is GuardDecision.BLOCK
    assert blocked.state == second.state


def test_different_arguments_reset_consecutive_chain():
    """相同 Tool 的不同 args 不能导致 false-positive。"""
    policy = ExecutionGuardPolicy(3)
    state = ExecutionGuardState()
    for args in ({"x": 1}, {"x": 1}, {"x": 2}, {"x": 1}, {"x": 1}):
        evaluation = observe_tool_call_batch(state, [_call(args=args)], policy)
        assert evaluation.decision is GuardDecision.ALLOW
        state = evaluation.state

    assert state.consecutive_identical_count == 2


def test_canonical_argument_order_and_call_id_do_not_bypass_identity():
    """dict key order 与 tool_call_id 不属于 semantic Tool identity。"""
    policy = ExecutionGuardPolicy(3)
    first = observe_tool_call_batch(ExecutionGuardState(), [_call(args={"x": 1, "y": 2}, call_id="a")], policy)
    second = observe_tool_call_batch(first.state, [_call(args={"y": 2, "x": 1}, call_id="b")], policy)
    third = observe_tool_call_batch(second.state, [_call(args={"x": 1, "y": 2}, call_id="c")], policy)

    assert second.state.consecutive_identical_count == 2
    assert third.decision is GuardDecision.BLOCK


def test_different_qualified_tool_names_do_not_collide():
    """Agent-facing qualified MCP identity 不会因裸 tool name 相同而碰撞。"""
    policy = ExecutionGuardPolicy(3)
    first = observe_tool_call_batch(ExecutionGuardState(), [_call("mcp__server_a__lookup", {"x": 1})], policy)
    second = observe_tool_call_batch(first.state, [_call("mcp__server_b__lookup", {"x": 1})], policy)

    assert second.decision is GuardDecision.ALLOW
    assert second.state.consecutive_identical_count == 1


def test_untrackable_or_oversized_arguments_reset_without_repr_or_crash():
    """非 JSON/超限 args 不做 repr、truncate 或 fingerprint，仍允许原 Tool schema 处理。"""
    policy = ExecutionGuardPolicy(3)
    tracked = observe_tool_call_batch(ExecutionGuardState(), [_call(args={"x": 1})], policy)

    class Unserializable:
        pass

    untrackable = observe_tool_call_batch(tracked.state, [_call(args={"x": Unserializable()})], policy)
    oversized = observe_tool_call_batch(
        tracked.state,
        [_call(args={"payload": "x" * (MAX_GUARD_CANONICAL_ARG_BYTES + 1)})],
        policy,
    )
    nan_value = observe_tool_call_batch(tracked.state, [_call(args={"x": math.nan})], policy)

    for evaluation in (untrackable, oversized, nan_value):
        assert evaluation.decision is GuardDecision.ALLOW
        assert evaluation.state == ExecutionGuardState()
        assert "Unserializable" not in repr(evaluation)


@pytest.mark.parametrize("threshold", [0, 1, -1, True, False, 3.0, "3", None])
def test_guard_policy_requires_exact_meaningful_threshold(threshold):
    """第一个 Tool call 不应被阈值配置误阻断。"""
    with pytest.raises(ExecutionGuardValidationError, match="^invalid_execution_guard_policy$"):
        ExecutionGuardPolicy(threshold)
