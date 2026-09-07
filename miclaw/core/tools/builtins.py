"""默认 MiClaw builtin Tool：统一返回 ToolResult，并保护 scheduler 持久化变更。"""

from __future__ import annotations

import ast
from datetime import datetime
import hashlib
import json
import os
import threading
import uuid

from ..memory.lifecycle import MemoryUpdateDisposition, write_user_profile_with_policy
from ..memory.permissions import permission_block_message
from ..observability.logger import log_permission_confirmation, log_permission_decision
from ..runtime.config import MEMORY_DIR, TASKS_FILE
from ..security.permissions import (
    PermissionCapability,
    PermissionDecision,
    PermissionRequest,
    PermissionResult,
    RiskLevel,
    evaluate_permission,
    get_permission_confirmation_handler,
    resolve_permission,
)
from .base import miclaw_tool
from .result import ToolResult, tool_error, tool_permission_blocked, tool_success
from .sandbox import (
    execute_office_shell,
    list_office_files,
    read_office_file,
    write_office_file,
)


tasks_lock = threading.Lock()
_permission_evaluator = evaluate_permission
_permission_audit_logger = log_permission_decision
_permission_confirmation_audit_logger = log_permission_confirmation
_SCHEDULER_COLLECTION_TARGET = "scheduled-tasks"
_ALLOWED_REPEAT_FREQUENCIES = frozenset({"hourly", "daily", "weekly", "monthly"})
_ALLOWED_CALCULATOR_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_ALLOWED_CALCULATOR_UNARYOPS = (ast.UAdd, ast.USub)


def _scheduler_task_target(task_id: str) -> str:
    """将 task id 投影为稳定且不泄漏原始值的 logical permission target。"""
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
    return f"scheduled-task::{digest}"


def _scheduler_permission_block_message(result: PermissionResult) -> str:
    """把 scheduler permission 的未允许结果转换为安全模型可见文本。"""
    if result.decision is PermissionDecision.ASK:
        return f"Permission required: {result.reason}"
    return f"Permission denied: {result.reason}"


def _authorize_scheduler(operation: str, target: str, tool_name: str) -> ToolResult | None:
    """对 scheduler 逻辑 target 执行统一 policy、confirmation 与 session grant 流程。"""
    request = PermissionRequest(
        capability=PermissionCapability.SCHEDULER,
        operation=operation,
        target=target,
        reason="Scheduler task operation",
        risk_level=RiskLevel.LOW if operation == "list" else RiskLevel.MEDIUM,
        metadata={"tool_name": tool_name},
    )
    policy_result = _permission_evaluator(request)
    _permission_audit_logger(request, policy_result, tool_name=tool_name, metadata=request.metadata)
    confirmation_handler = get_permission_confirmation_handler()
    final_result = resolve_permission(request, policy_result, confirmation_handler)
    confirmation_source = final_result.metadata.get("confirmation_source")
    if policy_result.decision is PermissionDecision.ASK and (
        confirmation_handler is not None or confirmation_source == "session_grant"
    ):
        _permission_confirmation_audit_logger(
            request,
            policy_result,
            final_result,
            tool_name=tool_name,
            metadata=request.metadata,
        )
    if final_result.decision is PermissionDecision.ALLOW:
        return None
    return tool_permission_blocked(
        _scheduler_permission_block_message(final_result),
        decision=final_result.decision.value,
        metadata={"tool_name": tool_name, "operation": operation, "target": target},
    )


def _load_tasks() -> list[dict] | None:
    """读取既有 tasks.json；缺失或空文件仍表示空列表，损坏内容 fail closed。"""
    if not os.path.exists(TASKS_FILE):
        return []
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as file:
            content = file.read().strip()
        tasks = json.loads(content) if content else []
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if type(tasks) is not list or any(type(task) is not dict for task in tasks):
        return None
    return tasks


def _save_tasks(tasks: list[dict]) -> bool:
    """使用既有 tasks.json 格式持久化任务列表。"""
    try:
        with open(TASKS_FILE, "w", encoding="utf-8") as file:
            json.dump(tasks, file, ensure_ascii=False, indent=2)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _validate_task_id(task_id: str) -> bool:
    """只接受非空 task id；既有 tasks.json 的历史 id 仍可使用。"""
    return type(task_id) is str and bool(task_id.strip())


def _parse_future_time(value: str) -> datetime | None:
    """验证 scheduler 时间格式及未来约束，不回显原始输入。"""
    if type(value) is not str:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed if parsed > datetime.now() else None


def _validate_schedule_input(
    target_time: str,
    description: str,
    repeat: str | None,
    repeat_count: int | None,
) -> bool:
    """在 permission 前验证创建任务的无副作用输入。"""
    if _parse_future_time(target_time) is None or type(description) is not str or not description.strip():
        return False
    if repeat is None:
        return repeat_count is None
    if type(repeat) is not str or repeat not in _ALLOWED_REPEAT_FREQUENCIES:
        return False
    return repeat_count is None or (type(repeat_count) is int and repeat_count > 0)


def _task_fields_are_valid(tasks: list[dict]) -> bool:
    """保证读取任务至少具备当前 scheduler/heartbeat 所需字段。"""
    return all(
        type(task.get("id")) is str
        and type(task.get("target_time")) is str
        and type(task.get("description")) is str
        for task in tasks
    )


def _find_task(tasks: list[dict], task_id: str) -> dict | None:
    """按既有 exact task id 查找单个任务。"""
    return next((task for task in tasks if task["id"] == task_id), None)


def _calculator_result(expression: str) -> int | float:
    """只计算 AST 白名单中的基础数值表达式。"""
    if type(expression) is not str or not expression.strip():
        raise ValueError("invalid expression")
    tree = ast.parse(expression, mode="eval")

    def validate(node: ast.AST) -> None:
        if isinstance(node, ast.Expression):
            validate(node.body)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_CALCULATOR_BINOPS):
            validate(node.left)
            validate(node.right)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, _ALLOWED_CALCULATOR_UNARYOPS):
            validate(node.operand)
        elif isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            return
        else:
            raise ValueError("unsupported expression")

    validate(tree)
    result = eval(compile(tree, "<calculator>", "eval"), {"__builtins__": {}}, {})
    if type(result) not in {int, float}:
        raise ValueError("invalid result")
    return result


@miclaw_tool
def get_system_model_info() -> ToolResult:
    """返回既有安全的 provider/model 配置摘要。"""
    provider = os.getenv("DEFAULT_PROVIDER", "unknown")
    model = os.getenv("DEFAULT_MODEL", "unknown")
    if provider == "unknown" or model == "unknown":
        return tool_success("无法获取当前的系统模型配置，可能是环境变量未正确加载。")
    return tool_success(f"当前使用的模型提供商(Provider)是: {provider}，具体型号(Model)是: {model}。")


@miclaw_tool
def save_user_profile(new_content: str) -> ToolResult:
    """保存或更新当前工作区对应的用户长期画像。

    默认 OFFICE 工作区写入全局画像；PROJECT 工作区写入当前项目范围的画像。
    画像范围由运行时当前工作区决定，不能通过参数自行选择。只有符合当前长期
    Memory 写入政策的显式用户请求才可执行，且持久化仍需要权限确认。
    仅当用户明确要求记住、保存、更新或清除长期画像时，才将完整 Markdown 档案
    作为 new_content 传入；不得仅根据模型自行推断的偏好或重要事实调用此工具。
    """
    execution = write_user_profile_with_policy(MEMORY_DIR, new_content)
    if not execution.policy_result.eligible:
        return tool_error(
            "memory_write_not_eligible",
            "Memory write is not eligible under current policy.",
            metadata={"memory_write_policy": execution.policy_result.reason_code},
        )
    if execution.disposition is MemoryUpdateDisposition.NOOP_EXACT_MATCH:
        return tool_success("记忆档案已成功覆写更新。新的人设画像已生效。")
    authorization = execution.authorization
    assert authorization is not None
    if authorization.final_result.decision is not PermissionDecision.ALLOW:
        return tool_permission_blocked(
            permission_block_message(authorization.final_result),
            decision=authorization.final_result.decision.value,
            metadata={"permission_decision": authorization.final_result.decision.value},
        )
    return tool_success("记忆档案已成功覆写更新。新的人设画像已生效。")


@miclaw_tool
def get_current_time() -> ToolResult:
    """返回当前本地系统时间。"""
    now = datetime.now()
    return tool_success(f"当前本地系统时间是: {now.strftime('%Y-%m-%d %H:%M:%S')}")


@miclaw_tool
def calculator(expression: str) -> ToolResult:
    """计算受限基础数学表达式，不接受 Python attribute、调用或 import。"""
    try:
        result = _calculator_result(expression)
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError, OverflowError):
        return tool_error("invalid_input", "计算出错，请检查表达式格式。")
    except Exception:
        return tool_error("tool_execution_error", "计算器执行失败。")
    return tool_success(f"表达式 '{expression}' 的计算结果是: {result}")


@miclaw_tool
def schedule_task(
    target_time: str,
    description: str,
    repeat: str | None = None,
    repeat_count: int | None = None,
) -> ToolResult:
    """创建任务；输入验证后才请求 persistent scheduler mutation permission。"""
    if _parse_future_time(target_time) is None:
        return tool_error("invalid_input", "设定失败：时间格式错误，必须严格遵循 'YYYY-MM-DD HH:MM:SS' 格式。")
    if not _validate_schedule_input(target_time, description, repeat, repeat_count):
        return tool_error("invalid_input", "设定失败：任务参数无效。")
    blocked = _authorize_scheduler("create", _SCHEDULER_COLLECTION_TARGET, "schedule_task")
    if blocked is not None:
        return blocked
    with tasks_lock:
        tasks = _load_tasks()
        if tasks is None or not _task_fields_are_valid(tasks):
            return tool_error("tool_execution_error", "设定失败：任务队列不可用。")
        new_task = {
            "id": str(uuid.uuid4())[:8],
            "target_time": target_time,
            "description": description,
            "repeat": repeat,
            "repeat_count": repeat_count,
        }
        tasks.append(new_task)
        if not _save_tasks(tasks):
            return tool_error("tool_execution_error", "设定失败：任务队列写入失败。")
    message = f" 任务已成功加入队列。首发时间：{target_time} | 任务：{description}"
    if repeat:
        message += f" | 循环模式：{repeat} (共 {repeat_count if repeat_count else '无限'} 次)"
    return tool_success(message)


@miclaw_tool
def list_scheduled_tasks() -> ToolResult:
    """列出当前任务；低风险 scheduler read 仍经过 permission policy 但不要求确认。"""
    blocked = _authorize_scheduler("list", _SCHEDULER_COLLECTION_TARGET, "list_scheduled_tasks")
    if blocked is not None:
        return blocked
    with tasks_lock:
        tasks = _load_tasks()
        if tasks is None or not _task_fields_are_valid(tasks):
            return tool_error("tool_execution_error", "查询失败：任务队列不可用。")
        if not tasks:
            return tool_success("当前没有任何定时任务。")
        ordered_tasks = sorted(tasks, key=lambda task: task["target_time"])
    lines = [" 当前待执行任务列表："]
    lines.extend(
        f"- [ID: {task['id']}] 时间: {task['target_time']} | 任务: {task['description']}"
        for task in ordered_tasks
    )
    return tool_success("\n".join(lines) + "\n")


@miclaw_tool
def delete_scheduled_task(task_id: str) -> ToolResult:
    """删除 exact task id；目标存在后才请求 mutation permission。"""
    if not _validate_task_id(task_id):
        return tool_error("invalid_target", "删除失败：任务 ID 无效。")
    with tasks_lock:
        tasks = _load_tasks()
        if tasks is None or not _task_fields_are_valid(tasks):
            return tool_error("tool_execution_error", "删除失败：任务队列不可用。")
        if _find_task(tasks, task_id) is None:
            return tool_error("invalid_target", "删除失败：未找到指定任务。")
    blocked = _authorize_scheduler("delete", _scheduler_task_target(task_id), "delete_scheduled_task")
    if blocked is not None:
        return blocked
    with tasks_lock:
        tasks = _load_tasks()
        if tasks is None or not _task_fields_are_valid(tasks):
            return tool_error("tool_execution_error", "删除失败：任务队列不可用。")
        new_tasks = [task for task in tasks if task["id"] != task_id]
        if len(new_tasks) == len(tasks):
            return tool_error("invalid_target", "删除失败：未找到指定任务。")
        if not _save_tasks(new_tasks):
            return tool_error("tool_execution_error", "删除失败：任务队列写入失败。")
    return tool_success(f" 任务 [ID: {task_id}] 已成功取消。")


@miclaw_tool
def modify_scheduled_task(
    task_id: str,
    new_time: str | None = None,
    new_description: str | None = None,
) -> ToolResult:
    """修改 exact task id；无效输入或不存在 target 均不会触发 confirmation。"""
    if not _validate_task_id(task_id):
        return tool_error("invalid_target", "修改失败：任务 ID 无效。")
    if new_time is None and new_description is None:
        return tool_error("invalid_input", "修改失败：必须提供新的时间或任务内容。")
    if new_time is not None and _parse_future_time(new_time) is None:
        return tool_error("invalid_input", "修改失败：时间格式错误。")
    if new_description is not None and (type(new_description) is not str or not new_description.strip()):
        return tool_error("invalid_input", "修改失败：任务内容无效。")
    with tasks_lock:
        tasks = _load_tasks()
        if tasks is None or not _task_fields_are_valid(tasks):
            return tool_error("tool_execution_error", "修改失败：任务队列不可用。")
        if _find_task(tasks, task_id) is None:
            return tool_error("invalid_target", "修改失败：未找到指定任务。")
    blocked = _authorize_scheduler("modify", _scheduler_task_target(task_id), "modify_scheduled_task")
    if blocked is not None:
        return blocked
    with tasks_lock:
        tasks = _load_tasks()
        if tasks is None or not _task_fields_are_valid(tasks):
            return tool_error("tool_execution_error", "修改失败：任务队列不可用。")
        task = _find_task(tasks, task_id)
        if task is None:
            return tool_error("invalid_target", "修改失败：未找到指定任务。")
        if new_time is not None:
            task["target_time"] = new_time
        if new_description is not None:
            task["description"] = new_description
        if not _save_tasks(tasks):
            return tool_error("tool_execution_error", "修改失败：任务队列写入失败。")
    return tool_success(f" 任务 [ID: {task_id}] 已成功更新。")


BUILTIN_TOOLS = [
    get_current_time,
    calculator,
    save_user_profile,
    list_office_files,
    read_office_file,
    write_office_file,
    execute_office_shell,
    get_system_model_info,
    schedule_task,
    list_scheduled_tasks,
    delete_scheduled_task,
    modify_scheduled_task,
]
