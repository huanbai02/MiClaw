"""PR62 收口：默认 `miclaw run` 实际发现并执行 workspace Dynamic Skill。"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3
import sys
from contextlib import nullcontext

from langchain_core.messages import AIMessage, ToolMessage

import miclaw.core.agent.execution as execution_module
import miclaw.core.agent.graph as agent_graph
import miclaw.core.skills.loader as skill_loader
import miclaw.core.tools.sandbox as sandbox
from miclaw.core.observability.trace import TraceContext
from miclaw.core.security.permissions import (
    PermissionConfirmationChoice,
    reset_permission_confirmation_handler,
    set_permission_confirmation_handler,
)


class _SequentialModel:
    """以 Tool call 和最终回答驱动真实默认 Agent graph。"""

    def __init__(self) -> None:
        self.inputs: list[list[object]] = []
        self.bound_tool_names: set[str] = set()
        self._responses = [
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "closure_skill",
                    "args": {"mode": "run", "command": "echo CLOSURE_DYNAMIC_SKILL_OK"},
                    "id": "closure-skill-call",
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="skill complete"),
        ]

    def bind_tools(self, tools):
        """记录 default_agent_tools 实际注册的 Tool 名称。"""
        self.bound_tool_names = {tool.name for tool in tools}
        return self

    def invoke(self, messages):
        """返回下一个确定性模型响应。"""
        self.inputs.append(list(messages))
        return self._responses.pop(0)


class _NoopLogger:
    """避免 closure fixture 向真实 workspace 写入运行日志。"""

    def log_event(self, *_args, **_kwargs) -> None:
        """忽略仅测试使用的 audit event。"""


def test_default_runtime_discovers_and_executes_workspace_dynamic_skill(tmp_path, monkeypatch):
    """`miclaw run` 默认 Tool composition 真实扫描 SKILL.md，并经 ToolNode 执行 Skill。"""
    office = tmp_path / "office"
    skills_dir = office / "skills"
    skill_dir = skills_dir / "closure_skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "name: closure_skill\n"
        "description: Deterministic closure integration skill\n",
        encoding="utf-8",
    )

    model = _SequentialModel()
    monkeypatch.setattr(skill_loader, "SKILLS_DIR", str(skills_dir))
    monkeypatch.setattr(sandbox, "OFFICE_DIR", office)
    monkeypatch.setattr(agent_graph, "MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setattr(agent_graph, "get_provider", lambda **_kwargs: model)
    monkeypatch.setattr(agent_graph, "audit_logger", _NoopLogger())
    monkeypatch.setattr(execution_module, "audit_logger", _NoopLogger())
    monkeypatch.setattr(sandbox, "_permission_audit_logger", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sandbox, "_permission_confirmation_audit_logger", lambda *_args, **_kwargs: None)
    skill_loader.clear_skill_cache()

    confirmation_token = set_permission_confirmation_handler(
        lambda *_args: PermissionConfirmationChoice.ALLOW_ONCE
    )
    runtime_main = importlib.import_module("entry.main")
    try:
        prompts = iter(("run workspace skill", "/exit"))

        class _PromptSession:
            """按真实 interactive input contract 提供一个 Tool turn 和退出命令。"""

            def __init__(self, *_args, **_kwargs) -> None:
                pass

            async def prompt_async(self, *_args, **_kwargs) -> str:
                return next(prompts)

        async def _pacemaker(_queue, check_interval: int = 10) -> None:
            """保持后台任务存活，直到 entry shutdown 主动取消。"""
            await asyncio.Event().wait()

        monkeypatch.setattr(runtime_main, "print_banner", lambda: None)
        monkeypatch.setattr(runtime_main, "cprint", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(runtime_main, "patch_stdout", lambda: nullcontext())
        monkeypatch.setattr(runtime_main, "PromptSession", _PromptSession)
        monkeypatch.setattr(runtime_main, "pacemaker_loop", _pacemaker)
        monkeypatch.setattr(runtime_main, "DB_PATH", str(tmp_path / "state.sqlite3"))
        monkeypatch.setattr(runtime_main, "EXECUTION_DB_PATH", str(tmp_path / "execution.sqlite3"))
        asyncio.run(runtime_main.async_main(trace_context=TraceContext(run_id="closure-dynamic-skill")))
    finally:
        reset_permission_confirmation_handler(confirmation_token)
        skill_loader.clear_skill_cache()
        import entry

        if getattr(entry, "main", None) is runtime_main:
            delattr(entry, "main")
        sys.modules.pop("entry.main", None)

    with sqlite3.connect(tmp_path / "execution.sqlite3") as connection:
        statuses = connection.execute("SELECT status FROM execution_attempts").fetchall()
    tool_message = next(
        message
        for message in model.inputs[1]
        if isinstance(message, ToolMessage) and message.tool_call_id == "closure-skill-call"
    )
    assert statuses == [("succeeded",)]
    assert "closure_skill" in model.bound_tool_names
    assert "CLOSURE_DYNAMIC_SKILL_OK" in str(tool_message.content)
    assert len(model.inputs) == 2
