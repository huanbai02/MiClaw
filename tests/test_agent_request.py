"""锁定 host-owned AgentRequest 的严格 metadata 边界。"""

import pytest

from miclaw.core.agent.request import AgentRequest
from miclaw.core.memory.lifecycle import MemoryWriteIntent


def test_agent_request_accepts_only_explicit_memory_intent():
    """Memory intent 只接受现有 enum，不进行字符串或布尔值转换。"""
    assert AgentRequest("hello") == AgentRequest("hello", None)
    assert AgentRequest("remember", MemoryWriteIntent.EXPLICIT_USER_REQUEST).memory_write_intent is (
        MemoryWriteIntent.EXPLICIT_USER_REQUEST
    )
    for invalid in ("explicit_user_request", True, False, {}, []):
        with pytest.raises(ValueError, match="invalid_agent_request_memory_write_intent"):
            AgentRequest("hello", invalid)


def test_agent_request_requires_string_content():
    """queue content 不接受隐式字符串化。"""
    with pytest.raises(ValueError, match="invalid_agent_request_content"):
        AgentRequest(1)
