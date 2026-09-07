"""验证默认 legacy builtin 收敛到 ToolResult、structured outcome 与 scheduler permission。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import miclaw.core.agent.graph as agent_graph
import miclaw.core.tools.builtins as builtins
from miclaw.core.agent.execution import run_agent_execution
from miclaw.core.execution.models import ExecutionStatus
from miclaw.core.observability.trace import TraceContext
from miclaw.core.security.permissions import (
    PermissionCapability,
    PermissionConfirmationChoice,
    reset_permission_confirmation_handler,
    reset_session_permission_grants,
    set_permission_confirmation_handler,
    set_session_permission_grants,
)
from miclaw.core.tools.base import _MiClawStructuredTool
from miclaw.core.tools.result import StructuredToolOutcome, extract_tool_outcome


class _SequentialModel:
    """提供确定性 Tool call 与最终回答。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = list(responses)
        self.inputs: list[list[object]] = []

    def bind_tools(self, _tools):
        return self

    def invoke(self, messages):
        self.inputs.append(list(messages))
        return self.responses.pop(0)


def _tool_call(name: str, args: dict[str, object], call_id: str) -> AIMessage:
    """构造真实 ToolNode 消费的 Tool call。"""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


def _tool_message(state: dict, call_id: str) -> ToolMessage:
    """取得一个 Tool call 对应的真实 ToolMessage。"""
    return next(
        message
        for message in state["messages"]
        if isinstance(message, ToolMessage) and message.tool_call_id == call_id
    )


@pytest.fixture()
def scheduler_file(tmp_path, monkeypatch):
    """隔离 tasks.json 与 scheduler permission audit，避免测试接触用户 runtime state。"""
    path = tmp_path / "tasks.json"
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(builtins, "TASKS_FILE", str(path))
    monkeypatch.setattr(builtins, "_permission_audit_logger", lambda *args, **kwargs: None)
    monkeypatch.setattr(builtins, "_permission_confirmation_audit_logger", lambda *args, **kwargs: None)
    return path


def _run_graph(monkeypatch, tools, responses):
    """通过真实 Agent graph/ToolNode 执行一个可控 Tool turn。"""
    async def scenario():
        model = _SequentialModel(responses)
        monkeypatch.setattr(agent_graph, "MEMORY_DIR", "memory-not-used")
        with patch.object(agent_graph, "get_provider", return_value=model):
            graph = agent_graph.create_agent_app(tools=tools)
        result = await run_agent_execution(
            lambda: graph.ainvoke({"messages": [HumanMessage(content="run")], "summary": ""}),
            trace_context=TraceContext(run_id="legacy-tool-run"),
        )
        return result, model

    return asyncio.run(scenario())


def _future_time() -> str:
    """返回 scheduler 当前语义接受的未来时间。"""
    return (datetime.now() + timedelta(days=1)).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def test_default_builtin_inventory_uses_miclaw_tool_wrapper():
    """默认 builtin inventory 完整且全部通过 MiClaw Tool wrapper；Dynamic Skills 不在此集合。"""
    expected_names = {
        "get_current_time",
        "calculator",
        "save_user_profile",
        "list_office_files",
        "read_office_file",
        "write_office_file",
        "execute_office_shell",
        "get_system_model_info",
        "schedule_task",
        "list_scheduled_tasks",
        "delete_scheduled_task",
        "modify_scheduled_task",
    }

    assert {tool.name for tool in builtins.BUILTIN_TOOLS} == expected_names
    assert all(isinstance(tool, _MiClawStructuredTool) for tool in builtins.BUILTIN_TOOLS)


def test_calculator_success_and_invalid_input_use_structured_tool_outcomes(monkeypatch):
    """计算成功与用户表达式错误均走真实 ToolNode；错误继续交给模型。"""
    success, success_model = _run_graph(
        monkeypatch,
        [builtins.calculator],
        [_tool_call("calculator", {"expression": "2 + 3"}, "calc-success"), AIMessage(content="done")],
    )
    success_message = _tool_message(success.output, "calc-success")
    assert success.state.status is ExecutionStatus.SUCCEEDED
    assert success_message.content.endswith("5")
    assert extract_tool_outcome(success_message) == StructuredToolOutcome(True, None)
    assert len(success_model.inputs) == 2

    for expression in ("__import__('os')", "(1).__class__"):
        invalid, invalid_model = _run_graph(
            monkeypatch,
            [builtins.calculator],
            [_tool_call("calculator", {"expression": expression}, "calc-invalid"), AIMessage(content="explained")],
        )
        invalid_message = _tool_message(invalid.output, "calc-invalid")
        assert invalid.state.status is ExecutionStatus.SUCCEEDED
        assert extract_tool_outcome(invalid_message) == StructuredToolOutcome(False, "invalid_input")
        assert len(invalid_model.inputs) == 2


def test_time_and_model_info_use_structured_success_outcomes(monkeypatch):
    """纯低风险时间/模型摘要 Tool 保持既有模型可见文本，并携带 success artifact。"""
    result, model = _run_graph(
        monkeypatch,
        [builtins.get_current_time, builtins.get_system_model_info],
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "get_current_time", "args": {}, "id": "time", "type": "tool_call"},
                    {"name": "get_system_model_info", "args": {}, "id": "info", "type": "tool_call"},
                ],
            ),
            AIMessage(content="done"),
        ],
    )

    assert result.state.status is ExecutionStatus.SUCCEEDED
    assert "当前本地系统时间是:" in _tool_message(result.output, "time").content
    assert extract_tool_outcome(_tool_message(result.output, "time")) == StructuredToolOutcome(True, None)
    assert extract_tool_outcome(_tool_message(result.output, "info")) == StructuredToolOutcome(True, None)
    assert len(model.inputs) == 2


def test_scheduler_create_allow_mutates_tasks_and_transports_success(monkeypatch, scheduler_file):
    """真实 ToolNode 的 scheduler create 在 ALLOW 后才写入 tasks.json。"""
    requests = []
    token = set_permission_confirmation_handler(
        lambda request, _result: requests.append(request) or PermissionConfirmationChoice.ALLOW_ONCE
    )
    try:
        result, model = _run_graph(
            monkeypatch,
            [builtins.schedule_task],
            [
                _tool_call(
                    "schedule_task",
                    {"target_time": _future_time(), "description": "SCHEDULED_SECRET", "repeat": None, "repeat_count": None},
                    "schedule-allow",
                ),
                AIMessage(content="created"),
            ],
        )
    finally:
        reset_permission_confirmation_handler(token)

    message = _tool_message(result.output, "schedule-allow")
    tasks = json.loads(scheduler_file.read_text(encoding="utf-8"))
    assert result.state.status is ExecutionStatus.SUCCEEDED
    assert extract_tool_outcome(message) == StructuredToolOutcome(True, None)
    assert len(tasks) == 1 and tasks[0]["description"] == "SCHEDULED_SECRET"
    assert "SCHEDULED_SECRET" not in repr(message.artifact)
    assert len(requests) == 1
    assert "SCHEDULED_SECRET" not in repr(requests[0])
    assert requests[0].capability is PermissionCapability.SCHEDULER
    assert requests[0].operation == "create"
    assert requests[0].target == "scheduled-tasks"
    assert len(model.inputs) == 2


def test_scheduler_create_deny_has_zero_mutation_and_model_continues(monkeypatch, scheduler_file):
    """DENY 发生在 scheduler 写入前，ToolResult 为 model-correctable permission failure。"""
    token = set_permission_confirmation_handler(
        lambda _request, _result: PermissionConfirmationChoice.DENY
    )
    try:
        result, model = _run_graph(
            monkeypatch,
            [builtins.schedule_task],
            [
                _tool_call(
                    "schedule_task",
                    {"target_time": _future_time(), "description": "DENIED_SECRET", "repeat": None, "repeat_count": None},
                    "schedule-deny",
                ),
                AIMessage(content="denied explained"),
            ],
        )
    finally:
        reset_permission_confirmation_handler(token)

    assert result.state.status is ExecutionStatus.SUCCEEDED
    assert extract_tool_outcome(_tool_message(result.output, "schedule-deny")) == StructuredToolOutcome(False, "permission_denied")
    assert json.loads(scheduler_file.read_text(encoding="utf-8")) == []
    assert len(model.inputs) == 2


def test_scheduler_invalid_input_skips_confirmation_and_continues(monkeypatch, scheduler_file):
    """格式错误在 ASK 前成为 INVALID_INPUT，且不会变更 tasks.json。"""
    token = set_permission_confirmation_handler(
        lambda *_args: pytest.fail("invalid scheduler input must not prompt")
    )
    try:
        result, model = _run_graph(
            monkeypatch,
            [builtins.schedule_task],
            [
                _tool_call(
                    "schedule_task",
                    {"target_time": "invalid", "description": "INVALID_SECRET", "repeat": None, "repeat_count": None},
                    "schedule-invalid",
                ),
                AIMessage(content="corrected"),
            ],
        )
    finally:
        reset_permission_confirmation_handler(token)

    assert result.state.status is ExecutionStatus.SUCCEEDED
    assert extract_tool_outcome(_tool_message(result.output, "schedule-invalid")) == StructuredToolOutcome(False, "invalid_input")
    assert json.loads(scheduler_file.read_text(encoding="utf-8")) == []
    assert len(model.inputs) == 2


def test_scheduler_modify_delete_targets_and_legacy_tasks_file_remain_compatible(scheduler_file):
    """既有 tasks.json 可修改/删除；DENY 和不存在 target 均不会写入。"""
    original = [{"id": "legacy task/id", "target_time": _future_time(), "description": "old", "repeat": None, "repeat_count": None}]
    scheduler_file.write_text(json.dumps(original), encoding="utf-8")
    denied_requests = []
    token = set_permission_confirmation_handler(
        lambda request, _result: denied_requests.append(request) or PermissionConfirmationChoice.DENY
    )
    try:
        assert "Permission denied" in builtins.modify_scheduled_task.invoke(
            {"task_id": "legacy task/id", "new_description": "denied"}
        )
    finally:
        reset_permission_confirmation_handler(token)
    assert json.loads(scheduler_file.read_text(encoding="utf-8")) == original
    assert denied_requests[0].target.startswith("scheduled-task::")
    assert "legacy task/id" not in denied_requests[0].target

    token = set_permission_confirmation_handler(
        lambda _request, _result: PermissionConfirmationChoice.ALLOW_ONCE
    )
    try:
        assert "已成功更新" in builtins.modify_scheduled_task.invoke(
            {"task_id": "legacy task/id", "new_description": "updated"}
        )
        assert "已成功取消" in builtins.delete_scheduled_task.invoke({"task_id": "legacy task/id"})
    finally:
        reset_permission_confirmation_handler(token)
    assert json.loads(scheduler_file.read_text(encoding="utf-8")) == []

    token = set_permission_confirmation_handler(
        lambda *_args: pytest.fail("missing target must not prompt")
    )
    try:
        assert "未找到指定任务" in builtins.delete_scheduled_task.invoke({"task_id": "missing"})
    finally:
        reset_permission_confirmation_handler(token)



def test_scheduler_missing_target_is_structured_invalid_target_and_model_continues(monkeypatch, scheduler_file):
    """不存在 task 在 permission 前返回 INVALID_TARGET，不会将用户错误升级为 terminal failure。"""
    token = set_permission_confirmation_handler(
        lambda *_args: pytest.fail("missing target must not prompt")
    )
    try:
        result, model = _run_graph(
            monkeypatch,
            [builtins.delete_scheduled_task],
            [
                _tool_call("delete_scheduled_task", {"task_id": "missing"}, "missing-task"),
                AIMessage(content="not found explained"),
            ],
        )
    finally:
        reset_permission_confirmation_handler(token)

    assert result.state.status is ExecutionStatus.SUCCEEDED
    assert extract_tool_outcome(_tool_message(result.output, "missing-task")) == StructuredToolOutcome(False, "invalid_target")
    assert len(model.inputs) == 2
    assert json.loads(scheduler_file.read_text(encoding="utf-8")) == []


def test_scheduler_storage_failure_is_structured_execution_error_without_raw_content(monkeypatch, scheduler_file):
    """损坏 tasks.json 不泄漏解析内容，返回 model-correctable TOOL_EXECUTION_ERROR。"""
    scheduler_file.write_text('{"SCHEDULER_PARSE_SECRET": true}', encoding="utf-8")
    token = set_permission_confirmation_handler(
        lambda _request, _result: PermissionConfirmationChoice.ALLOW_ONCE
    )
    try:
        result, model = _run_graph(
            monkeypatch,
            [builtins.schedule_task],
            [
                _tool_call(
                    "schedule_task",
                    {"target_time": _future_time(), "description": "normal", "repeat": None, "repeat_count": None},
                    "storage-error",
                ),
                AIMessage(content="fallback"),
            ],
        )
    finally:
        reset_permission_confirmation_handler(token)

    message = _tool_message(result.output, "storage-error")
    assert result.state.status is ExecutionStatus.SUCCEEDED
    assert extract_tool_outcome(message) == StructuredToolOutcome(False, "tool_execution_error")
    assert "SCHEDULER_PARSE_SECRET" not in message.content
    assert len(model.inputs) == 2
    assert scheduler_file.read_text(encoding="utf-8") == '{"SCHEDULER_PARSE_SECRET": true}'


def test_scheduler_session_grant_is_scoped_to_create_collection_target(scheduler_file):
    """ALLOW_SESSION 仅复用同一 create collection request，不能授权 delete target。"""
    grant_token = set_session_permission_grants()
    confirmations = []
    def confirm(request, _result):
        confirmations.append(request)
        return (
            PermissionConfirmationChoice.ALLOW_SESSION
            if request.operation == "create"
            else PermissionConfirmationChoice.DENY
        )

    permission_token = set_permission_confirmation_handler(confirm)
    try:
        first = builtins.schedule_task.invoke({"target_time": _future_time(), "description": "one"})
        second = builtins.schedule_task.invoke({"target_time": _future_time(), "description": "two"})
        task_id = json.loads(scheduler_file.read_text(encoding="utf-8"))[0]["id"]
        denied_delete = builtins.delete_scheduled_task.invoke({"task_id": task_id})
    finally:
        reset_permission_confirmation_handler(permission_token)
        reset_session_permission_grants(grant_token)

    assert "任务已成功加入队列" in first
    assert "任务已成功加入队列" in second
    assert len(confirmations) == 2
    assert confirmations[0].operation == "create"
    assert confirmations[1].operation == "delete"
    assert "Permission denied" in denied_delete
