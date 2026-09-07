"""锁定 ToolResult 经官方 ToolMessage artifact 的最小结构化传输。"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

import miclaw.core.agent.graph as agent_graph
import miclaw.core.tools.builtins as builtins
import miclaw.core.tools.sandbox as sandbox_tools
from miclaw.core.agent.execution import AgentToolFailure, run_agent_execution
from miclaw.core.execution.models import ExecutionStatus
from miclaw.core.observability.trace import TraceContext
from miclaw.core.tools.base import miclaw_tool
from miclaw.core.tools.result import (
    StructuredToolOutcome,
    extract_tool_outcome,
    tool_error,
    tool_success,
)


class _SequentialModel:
    """依次提供真实 Agent graph 所需的模型响应。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = list(responses)
        self.inputs: list[list[object]] = []

    def bind_tools(self, _tools):
        return self

    def invoke(self, messages):
        self.inputs.append(list(messages))
        return self.responses.pop(0)


def _tool_call(name: str, args: dict[str, object], call_id: str) -> AIMessage:
    """构造 LangGraph ToolNode 的真实 tool call 输入。"""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


def test_real_toolnode_transports_mixed_outcomes_without_terminalizing_execution(monkeypatch):
    """同一 ToolNode batch 的 success/failure artifact 独立，模型仍可继续完成 execution。"""
    async def scenario() -> None:
        @miclaw_tool
        def structured_success(value: str) -> object:
            """返回成功的 MiClaw ToolResult。"""
            return tool_success("MODEL_VISIBLE_SENTINEL", data={"internal": "INTERNAL_SECRET_SENTINEL"})

        @miclaw_tool
        def structured_failure(value: str) -> object:
            """返回失败的 MiClaw ToolResult。"""
            return tool_error("timeout", "MODEL_VISIBLE_FAILURE", metadata={"private": "INTERNAL_SECRET_SENTINEL"})

        @miclaw_tool
        def legacy_string(value: str) -> str:
            """保留尚未进入 ToolResult contract 的 legacy string Tool。"""
            return "LEGACY_MODEL_VISIBLE"

        model = _SequentialModel([
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "structured_success", "args": {"value": "a"}, "id": "call-success", "type": "tool_call"},
                    {"name": "structured_failure", "args": {"value": "b"}, "id": "call-failure", "type": "tool_call"},
                    {"name": "legacy_string", "args": {"value": "c"}, "id": "call-legacy", "type": "tool_call"},
                ],
            ),
            AIMessage(content="MODEL_CONTINUES"),
        ])
        with patch.object(agent_graph, "get_provider", return_value=model):
            graph = agent_graph.create_agent_app(tools=[structured_success, structured_failure, legacy_string])

        result = await run_agent_execution(
            lambda: graph.ainvoke({"messages": [HumanMessage(content="run tools")], "summary": ""}),
            trace_context=TraceContext(run_id="tool-outcome-run"),
        )
        assert result.state.status is ExecutionStatus.SUCCEEDED
        messages = result.output["messages"]
        tool_messages = {message.tool_call_id: message for message in messages if isinstance(message, ToolMessage)}
        assert tool_messages["call-success"].content == "MODEL_VISIBLE_SENTINEL"
        assert tool_messages["call-failure"].content == "MODEL_VISIBLE_FAILURE"
        assert tool_messages["call-legacy"].content == "LEGACY_MODEL_VISIBLE"
        assert tool_messages["call-success"].status == "success"
        assert tool_messages["call-failure"].status == "error"
        assert extract_tool_outcome(tool_messages["call-success"]) == StructuredToolOutcome(True, None)
        assert extract_tool_outcome(tool_messages["call-failure"]) == StructuredToolOutcome(False, "timeout")
        assert extract_tool_outcome(tool_messages["call-legacy"]) is None
        assert tool_messages["call-success"].artifact == {
            "miclaw_tool_outcome": {"version": 1, "ok": True, "error_type": None}
        }
        assert tool_messages["call-failure"].artifact == {
            "miclaw_tool_outcome": {"version": 1, "ok": False, "error_type": "timeout"}
        }
        assert result.output["messages"][-1].content == "MODEL_CONTINUES"
        assert "INTERNAL_SECRET_SENTINEL" not in repr(tool_messages["call-success"].artifact)
        assert "INTERNAL_SECRET_SENTINEL" not in repr(tool_messages["call-failure"].artifact)

    asyncio.run(scenario())


def test_real_sandbox_toolnode_transports_success_and_path_failure(monkeypatch, tmp_path):
    """sandbox ToolResult 经真实 ToolNode 逐 call 保留 success/path_error outcome。"""
    async def scenario() -> None:
        office = tmp_path / "office"
        office.mkdir()
        (office / "visible.txt").write_text("visible", encoding="utf-8")
        monkeypatch.setattr(sandbox_tools, "OFFICE_DIR", str(office))
        model = _SequentialModel([
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "list_office_files", "args": {"sub_dir": ""}, "id": "sandbox-success", "type": "tool_call"},
                    {"name": "list_office_files", "args": {"sub_dir": "/outside"}, "id": "sandbox-failure", "type": "tool_call"},
                ],
            ),
            AIMessage(content="sandbox handled"),
        ])
        with patch.object(agent_graph, "get_provider", return_value=model):
            graph = agent_graph.create_agent_app(tools=[sandbox_tools.list_office_files])
        state = await graph.ainvoke({"messages": [HumanMessage(content="list")], "summary": ""})
        outcomes = {
            message.tool_call_id: extract_tool_outcome(message)
            for message in state["messages"]
            if isinstance(message, ToolMessage)
        }
        assert outcomes == {
            "sandbox-success": StructuredToolOutcome(True, None),
            "sandbox-failure": StructuredToolOutcome(False, "path_error"),
        }
        assert {
            message.tool_call_id: message.status
            for message in state["messages"]
            if isinstance(message, ToolMessage)
        } == {"sandbox-success": "success", "sandbox-failure": "error"}
        assert state["messages"][-1].content == "sandbox handled"

    asyncio.run(scenario())


def test_memory_toolnode_failure_transports_stable_outcome(monkeypatch, tmp_path):
    """没有 trusted Memory intent 时保留 stable outcome，并按既有 SAFETY_BLOCKED 映射终止。"""
    async def scenario() -> None:
        model = _SequentialModel([
            _tool_call("save_user_profile", {"new_content": "PROFILE_PRIVATE"}, "memory-call"),
            AIMessage(content="memory failure handled"),
        ])
        monkeypatch.setattr(agent_graph, "MEMORY_DIR", str(tmp_path / "memory"))
        monkeypatch.setattr(builtins, "MEMORY_DIR", str(tmp_path / "memory"))
        with patch.object(agent_graph, "get_provider", return_value=model):
            graph = agent_graph.create_agent_app(tools=[builtins.save_user_profile])
        with pytest.raises(AgentToolFailure) as error:
            await graph.ainvoke({"messages": [HumanMessage(content="ordinary turn")], "summary": ""})
        assert error.value.failure.code.value == "safety_blocked"
        assert len(model.inputs) == 1
        assert not (tmp_path / "memory").exists()

    asyncio.run(scenario())


def test_tool_outcome_checkpoint_round_trip_and_malformed_metadata_are_neutral(monkeypatch, tmp_path):
    """artifact 经 SQLite checkpoint 重开保留，旧或损坏 metadata 不影响读取。"""
    async def scenario() -> None:
        @miclaw_tool
        def structured_failure() -> object:
            """构造可 checkpoint 的失败 ToolResult。"""
            return tool_error(
                "timeout",
                "MODEL_VISIBLE_SENTINEL",
                metadata={"private": "INTERNAL_SECRET_SENTINEL"},
            )

        model = _SequentialModel([
            _tool_call("structured_failure", {}, "checkpoint-call"),
            AIMessage(content="done"),
        ])
        config = {"configurable": {"thread_id": "tool-outcome-checkpoint"}}
        state_path = tmp_path / "state.sqlite3"
        async with AsyncSqliteSaver.from_conn_string(str(state_path)) as saver:
            with patch.object(agent_graph, "get_provider", return_value=model):
                graph = agent_graph.create_agent_app(tools=[structured_failure], checkpointer=saver)
            await graph.ainvoke({"messages": [HumanMessage(content="checkpoint")], "summary": ""}, config=config, durability="sync")

        async with AsyncSqliteSaver.from_conn_string(str(state_path)) as reopened_saver:
            with patch.object(agent_graph, "get_provider", return_value=_SequentialModel([])):
                reopened = agent_graph.create_agent_app(tools=[structured_failure], checkpointer=reopened_saver)
            snapshot = await reopened.aget_state(config)
        tool_message = next(message for message in snapshot.values["messages"] if isinstance(message, ToolMessage))
        assert tool_message.content == "MODEL_VISIBLE_SENTINEL"
        assert extract_tool_outcome(tool_message) == StructuredToolOutcome(False, "timeout")
        assert tool_message.artifact == {"miclaw_tool_outcome": {"version": 1, "ok": False, "error_type": "timeout"}}
        assert b"INTERNAL_SECRET_SENTINEL" not in state_path.read_bytes()
        assert extract_tool_outcome(ToolMessage(content="old", tool_call_id="old")) is None
        for artifact in (
            {"other": {}},
            {"miclaw_tool_outcome": {"version": 2, "ok": False, "error_type": "timeout"}},
            {"miclaw_tool_outcome": {"version": 1, "ok": "false", "error_type": "timeout"}},
            {"miclaw_tool_outcome": {"version": 1, "ok": False, "error_type": 123}},
        ):
            assert extract_tool_outcome(ToolMessage(content="safe", tool_call_id="invalid", artifact=artifact)) is None

    asyncio.run(scenario())
