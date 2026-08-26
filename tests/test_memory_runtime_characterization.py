"""锁定当前 Markdown 用户画像与 Agent context 的实际行为。"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from miclaw.core import agent
from miclaw.core import config
from miclaw.core.tools import builtins
from miclaw.core.user_profile import UserProfileStore, get_user_profile_store
from miclaw.core.workspace import reset_active_project_root, set_active_project_root
from miclaw.core.permissions import (
    PermissionConfirmationChoice,
    reset_permission_confirmation_handler,
    set_permission_confirmation_handler,
)


class _CaptureModel:
    """记录 Agent 实际发送给模型的消息，并返回一个普通回复。"""

    def __init__(self) -> None:
        self.inputs = []

    def invoke(self, messages):
        self.inputs.append(messages)
        return AIMessage(content="characterized")


class _Provider:
    """提供 create_agent_app 所需的最小 bind_tools 接口。"""

    def __init__(self, model: _CaptureModel) -> None:
        self.model = model

    def bind_tools(self, _tools):
        return self.model


class _NoopAuditLogger:
    """避免 characterization 测试写入真实 JSONL。"""

    def log_event(self, **_kwargs) -> None:
        return None


def _build_capture_app(monkeypatch, memory_dir: Path):
    """创建一次用于捕获 system prompt 的最小 Agent app。"""
    model = _CaptureModel()
    monkeypatch.setattr(agent, "MEMORY_DIR", str(memory_dir))
    monkeypatch.setattr(agent, "audit_logger", _NoopAuditLogger())
    monkeypatch.setattr(agent, "get_provider", lambda **_kwargs: _Provider(model))
    return agent.create_agent_app(tools=[]), model


def _invoke_and_capture_system_prompt(app, model: _CaptureModel, *, summary: str = "") -> str:
    """对既有 app 执行一次调用，并返回该次实际 system prompt。"""
    input_count = len(model.inputs)
    app.invoke(
        {"messages": [HumanMessage(content="characterize memory")], "summary": summary},
        config={"configurable": {"thread_id": "memory-characterization"}},
    )

    system_message = next(
        message for message in model.inputs[input_count] if isinstance(message, SystemMessage)
    )
    return str(system_message.content)


def _capture_system_prompt(monkeypatch, memory_dir: Path, *, summary: str = "") -> str:
    """创建最小 app 后执行一次调用，供单次读取测试使用。"""
    app, model = _build_capture_app(monkeypatch, memory_dir)
    return _invoke_and_capture_system_prompt(app, model, summary=summary)


def test_memory_root_and_profile_filename_are_workspace_scoped_configuration():
    """当前显式 Memory root 固定为 MICLAW_WORKSPACE 下的 memory/。"""
    assert Path(config.MEMORY_DIR) == Path(config.WORKSPACE_DIR) / "memory"
    assert get_user_profile_store(config.MEMORY_DIR).profile_path == Path(config.MEMORY_DIR) / "user_profile.md"


def test_save_user_profile_creates_utf8_profile_and_overwrites_previous_content(tmp_path, monkeypatch):
    """正式写入口保存完整 Markdown，而不是 append 或多文件记录。"""
    memory_dir = tmp_path / "memory"
    profile_path = memory_dir / "user_profile.md"
    monkeypatch.setattr(builtins, "MEMORY_DIR", str(memory_dir))

    confirmation_token = set_permission_confirmation_handler(
        lambda request, result: PermissionConfirmationChoice.ALLOW_ONCE
    )
    try:
        assert "成功覆写更新" in builtins.save_user_profile.invoke({"new_content": "第一版\n"})
        assert "成功覆写更新" in builtins.save_user_profile.invoke({"new_content": "第二版：偏好"})
    finally:
        reset_permission_confirmation_handler(confirmation_token)

    assert profile_path.read_text(encoding="utf-8") == "第二版：偏好"


def test_agent_uses_missing_profile_fallback_and_ignores_other_memory_files(tmp_path, monkeypatch):
    """当前读取只看固定 user_profile.md，不枚举 memory/ 中其他文件。"""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "other.md").write_text("OTHER_MEMORY_MARKER", encoding="utf-8")

    prompt = _capture_system_prompt(monkeypatch, memory_dir)

    assert "暂无记录" in prompt
    assert "OTHER_MEMORY_MARKER" not in prompt


def test_agent_injects_full_profile_and_current_summary_into_system_prompt(tmp_path, monkeypatch):
    """用户画像与 LangGraph summary 都直接拼入 system prompt。"""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    profile = "PROFILE_CONTEXT_MARKER\n" + "x" * 12_000
    (memory_dir / "user_profile.md").write_text(profile, encoding="utf-8")

    prompt = _capture_system_prompt(monkeypatch, memory_dir, summary="SUMMARY_CONTEXT_MARKER")

    assert profile in prompt
    assert "SUMMARY_CONTEXT_MARKER" in prompt
    assert "【用户长期画像 (静态偏好)】" in prompt
    assert "[近期对话上下文]" in prompt
    assert "user-profile" not in prompt
    assert "user_profile_store" not in prompt
    assert "global" not in prompt


def test_agent_reads_profile_from_filesystem_each_invocation_and_ignores_invalid_utf8(tmp_path, monkeypatch):
    """同一 app 的后续 Agent node 会重读 profile；UTF-8 错误被忽略。"""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    profile_path = memory_dir / "user_profile.md"
    app, model = _build_capture_app(monkeypatch, memory_dir)

    profile_path.write_text("PROFILE_VERSION_A", encoding="utf-8")
    first_prompt = _invoke_and_capture_system_prompt(app, model)
    profile_path.write_bytes(b"PROFILE_VERSION_B\xff")
    second_prompt = _invoke_and_capture_system_prompt(app, model)

    assert "PROFILE_VERSION_A" in first_prompt
    assert "PROFILE_VERSION_B" not in first_prompt
    assert "PROFILE_VERSION_B" in second_prompt
    assert "PROFILE_VERSION_A" not in second_prompt


def test_agent_reads_successfully_replaced_profile_on_next_invocation(tmp_path, monkeypatch):
    """同一 app 在 Store 原子替换后仍从 filesystem 读取新内容。"""
    memory_dir = tmp_path / "memory"
    store = UserProfileStore(memory_dir / "user_profile.md")
    app, model = _build_capture_app(monkeypatch, memory_dir)

    store.write_profile("ATOMIC_PROFILE_VERSION_A")
    first_prompt = _invoke_and_capture_system_prompt(app, model)
    store.write_profile("ATOMIC_PROFILE_VERSION_B")
    second_prompt = _invoke_and_capture_system_prompt(app, model)

    assert "ATOMIC_PROFILE_VERSION_A" in first_prompt
    assert "ATOMIC_PROFILE_VERSION_B" not in first_prompt
    assert "ATOMIC_PROFILE_VERSION_B" in second_prompt
    assert "ATOMIC_PROFILE_VERSION_A" not in second_prompt


def test_project_workspace_uses_scoped_profile_with_global_fallback(tmp_path, monkeypatch):
    """PROJECT Agent 先读 GLOBAL fallback，写入 scoped profile 后改为读取该 profile。"""
    global_memory = tmp_path / "workspace" / "memory"
    global_memory.mkdir(parents=True)
    (global_memory / "user_profile.md").write_text("GLOBAL_PROFILE_MARKER", encoding="utf-8")
    project_root = tmp_path / "project"
    project_root.mkdir()

    token = set_active_project_root(project_root)
    try:
        fallback_prompt = _capture_system_prompt(monkeypatch, global_memory)
        get_user_profile_store(global_memory).write_profile("PROJECT_PROFILE_MARKER")
        project_prompt = _capture_system_prompt(monkeypatch, global_memory)
    finally:
        reset_active_project_root(token)

    assert "GLOBAL_PROFILE_MARKER" in fallback_prompt
    assert "PROJECT_PROFILE_MARKER" not in fallback_prompt
    assert "PROJECT_PROFILE_MARKER" in project_prompt
    assert "GLOBAL_PROFILE_MARKER" not in project_prompt
    assert (global_memory / "user_profile.md").read_text(encoding="utf-8") == "GLOBAL_PROFILE_MARKER"
