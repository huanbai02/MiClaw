"""锁定 host-owned AgentRequest 的严格 metadata 边界。"""

import pytest

from miclaw.core.agent.request import AgentRequest, AgentRequestOrigin
from miclaw.core.memory.lifecycle import MemoryWriteIntent


def test_agent_request_requires_explicit_host_origin():
    """请求来源必须由 host 明确给出，不能默认为 interactive。"""
    interactive = AgentRequest("hello", AgentRequestOrigin.INTERACTIVE)
    scheduler = AgentRequest("scheduled", AgentRequestOrigin.SCHEDULER)

    assert interactive.origin is AgentRequestOrigin.INTERACTIVE
    assert scheduler.origin is AgentRequestOrigin.SCHEDULER
    with pytest.raises(TypeError):
        AgentRequest("hello")
    for invalid in ("interactive", True, False, None, {}):
        with pytest.raises(ValueError, match="invalid_agent_request_origin"):
            AgentRequest("hello", invalid)


def test_agent_request_accepts_only_explicit_memory_intent():
    """Memory intent 与 provenance 独立，且不进行字符串或布尔值转换。"""
    request = AgentRequest(
        "remember",
        AgentRequestOrigin.INTERACTIVE,
        MemoryWriteIntent.EXPLICIT_USER_REQUEST,
    )
    scheduler_request = AgentRequest("scheduled", AgentRequestOrigin.SCHEDULER)

    assert request.memory_write_intent is MemoryWriteIntent.EXPLICIT_USER_REQUEST
    assert scheduler_request.memory_write_intent is None
    for invalid in ("explicit_user_request", True, False, {}, []):
        with pytest.raises(ValueError, match="invalid_agent_request_memory_write_intent"):
            AgentRequest("hello", AgentRequestOrigin.INTERACTIVE, invalid)


def test_agent_request_requires_string_content():
    """queue content 不接受隐式字符串化。"""
    with pytest.raises(ValueError, match="invalid_agent_request_content"):
        AgentRequest(1, AgentRequestOrigin.INTERACTIVE)
