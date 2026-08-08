import time
import json
import os
from pathlib import Path, PureWindowsPath
from rich.console import Console
from rich.theme import Theme
from rich.panel import Panel
from rich.text import Text
from rich.markup import escape
from rich.align import Align
from rich import box
from datetime import datetime

from miclaw.core.config import get_log_file_path


miclaw_theme = Theme({
    "info": "dim cyan",
    "warning": "color(141)",
    "error": "bold red",
    "llm_input": "dim white",
    "tool_call": "bold yellow",
    "tool_result": "bold green",
    "permission_pending": "bold color(214)",
    "ai_message": "bold bright_magenta",
    "timestamp": "dim white"
})

console = Console(theme=miclaw_theme)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEGACY_LOG_FILE = Path(PROJECT_ROOT) / "logs" / "local_geek_master.jsonl"


def resolve_monitor_log_file(log_file: str | Path | None = None) -> Path:
    """解析 monitor 应读取的 log 文件，并保留旧路径 fallback。"""
    if log_file is not None:
        return get_log_file_path(log_file=log_file)

    default_log_file = get_log_file_path()
    if default_log_file.exists() or not LEGACY_LOG_FILE.exists():
        return default_log_file
    return LEGACY_LOG_FILE


def print_header():
    """渲染 简约斜体版·MiClaw 监控面板"""
    
    monster = (
        "  ▄█▄▄█▄  \n"
        " ▀██████▀ \n"
        " ██▄██▄██ \n"
        "  ▀    ▀  "
    )
    

    content = Text(justify="center")
    content.append("\n  Live Stream  \n\n", style="bold white italic")
    content.append(monster + "\n\n", style="color(141)")
    content.append("   What is MiClaw doing?    \n", style="dim white italic") 

    panel = Panel(
        Align.center(content),  
        title="[bold color(141)] MiClaw [/bold color(141)]",
        title_align="left",
        border_style="color(141)",
        box=box.ROUNDED,
        width=42,               
        padding=0
    )

    console.print(Align.center(panel))
    console.print()

def tail_f(filepath):
    """文件末尾监听"""
    filepath = Path(filepath)
    if not filepath.exists():
        console.print(f"[warning]⏳ 等待日志文件生成...[/warning]")
        while not filepath.exists():
            time.sleep(0.5)
            
    with open(filepath, 'r', encoding='utf-8') as f:
        f.seek(0, 2)
        print_header()
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.1)
                continue
            yield line


def parse_event_line(line: str) -> dict | None:
    """解析单行 JSONL；坏行返回 parse_error event，避免 monitor 崩溃。"""
    try:
        text = line.strip()
        if not text:
            return None
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"event": "parse_error", "error": str(exc)}
    if not isinstance(data, dict):
        return {"event": "parse_error", "error": "log line is not an object"}
    return data


def read_jsonl_events(filepath: str | Path) -> list[dict]:
    """读取 JSONL event； malformed line 会被转换成 parse_error event。"""
    events = []
    path = Path(filepath)
    if not path.exists():
        return events
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            event = parse_event_line(line)
            if event is not None:
                events.append(event)
    return events


def tail_log_events(filepath: str | Path, lines: int = 20) -> list[dict]:
    """读取最后 N 条非空 JSONL event；坏行转换为 parse_error。"""
    path = Path(filepath)
    if not path.exists() or lines <= 0:
        return []

    with open(path, "r", encoding="utf-8") as f:
        raw_lines = [line for line in f if line.strip()]

    events = []
    for line in raw_lines[-lines:]:
        event = parse_event_line(line)
        if event is not None:
            events.append(event)
    return events


def _numeric_step_id(event: dict) -> int | None:
    """解析可排序的 numeric step_id；不可解析时返回 None。"""
    try:
        return int(event.get("step_id"))
    except (TypeError, ValueError):
        return None


def get_trace_events(events: list[dict], run_id: str) -> list[dict]:
    """筛选指定 run_id 的 event，并按 numeric step_id 稳定排序。"""
    requested_run_id = str(run_id)
    matched = [event for event in events if "run_id" in event and str(event.get("run_id")) == requested_run_id]

    indexed = list(enumerate(matched))

    def sort_key(item: tuple[int, dict]) -> tuple[int, int, int]:
        index, event = item
        step_id = _numeric_step_id(event)
        if step_id is None:
            return (1, index, index)
        return (0, step_id, index)

    return [event for _, event in sorted(indexed, key=sort_key)]


def _format_timestamp(data: dict) -> str:
    ts_str = str(data.get("ts") or data.get("timestamp") or "")
    try:
        if ts_str.endswith('Z'):
            ts_str = ts_str[:-1] + '+00:00'
        dt_local = datetime.fromisoformat(ts_str).astimezone()
        return dt_local.strftime("%H:%M:%S")
    except Exception:
        return ts_str.split("T")[-1][:8] if ts_str else "--:--:--"


def _safe_trace_text(value: object, max_length: int) -> str:
    """只保留 trace 展示所需的低风险字符，避免 Rich markup 注入。"""
    text = str(value or "")
    safe = "".join(ch for ch in text if ch.isalnum() or ch in "-_")
    return safe[:max_length]


def format_trace_prefix(event: dict) -> str:
    """生成短 trace 前缀，兼容没有 run_id/step_id 的旧日志。"""
    parts = []
    run_id = event.get("run_id")
    step_id = event.get("step_id")
    if run_id:
        safe_run_id = _safe_trace_text(run_id, 8)
        if safe_run_id:
            parts.append(f"run={safe_run_id}")
    if step_id is not None:
        safe_step_id = _safe_trace_text(step_id, 12)
        if safe_step_id:
            parts.append(f"step={safe_step_id}")
    return f"[{' '.join(parts)}] " if parts else ""


def format_trace_prefix_for_markup(event: dict) -> str:
    """生成可安全插入 Rich markup 字符串的 trace 前缀。"""
    return escape(format_trace_prefix(event))


def _safe_permission_target(target: object) -> str:
    """只显示明显安全的 office-relative permission target。"""
    target_text = str(target or "").strip()
    if not target_text:
        return ""
    path = Path(target_text)
    if path.is_absolute() or PureWindowsPath(target_text).drive:
        return ""
    if target_text.startswith(("/", "\\")):
        return ""
    if ".." in path.parts or ".." in PureWindowsPath(target_text).parts:
        return ""
    return target_text


def format_permission_decision_event(event: dict) -> str:
    """生成不包含 raw metadata 的 permission_decision 摘要。"""
    decision = str(event.get("decision") or "unknown").lower()
    capability = str(event.get("capability") or "unknown")
    operation = str(event.get("operation") or "").strip()
    tool_name = str(event.get("tool_name") or "unknown_tool")
    risk_level = str(event.get("risk_level") or "unknown")
    target = _safe_permission_target(event.get("target"))
    requires_confirmation = bool(event.get("requires_confirmation")) or decision == "ask"

    status_by_decision = {
        "allow": "allowed",
        "ask": "blocked_pending_confirmation",
        "deny": "blocked",
    }
    status = status_by_decision.get(decision, "unknown")

    action_parts = ["PERMISSION", decision, capability]
    if operation:
        action_parts.append(operation)
    if target:
        action_parts.append(target)

    detail_parts = [f"tool={tool_name}", f"risk={risk_level}", f"status={status}"]
    if requires_confirmation:
        detail_parts.append("requires_confirmation=true")
    if decision == "ask":
        detail_parts.append("currently blocked pending confirmation")

    return f"{' '.join(action_parts)} [{' '.join(detail_parts)}]"


def get_permission_decision_style(decision: object) -> str:
    """为 permission decision 选择明确语义样式，ASK 不走 generic warning。"""
    decision_text = str(decision or "").lower()
    if decision_text == "allow":
        return "tool_result"
    if decision_text == "deny":
        return "error"
    if decision_text == "ask":
        return "permission_pending"
    return "warning"


def _is_sensitive_arg_key(key: object) -> bool:
    """判断 tool arg key 是否只能显示 presence/length。"""
    key_text = str(key or "").lower()
    sensitive_markers = (
        "command",
        "content",
        "token",
        "api_key",
        "apikey",
        "password",
        "secret",
        "authorization",
        "bearer",
        "key",
    )
    return any(marker in key_text for marker in sensitive_markers)


def format_tool_args_summary(args: object) -> str:
    """生成 tool_call 参数摘要，不显示 raw value。"""
    if not isinstance(args, dict):
        return "args=unavailable"

    summary_parts = []
    sensitive_seen = False
    for key, value in args.items():
        key_text = str(key)
        key_lower = key_text.lower()
        value_text = "" if value is None else str(value)

        if "command" in key_lower:
            summary_parts.append(f"{key_text}_present=true")
            summary_parts.append(f"{key_text}_length={len(value_text)}")
        elif "content" in key_lower:
            summary_parts.append(f"{key_text}_present=true")
            summary_parts.append(f"{key_text}_length={len(value_text)}")
        elif key_lower == "input" or "input" in key_lower:
            summary_parts.append(f"{key_text}_present=true")
            summary_parts.append(f"{key_text}_length={len(value_text)}")
        elif _is_sensitive_arg_key(key_text):
            sensitive_seen = True
        else:
            summary_parts.append(key_text)

    if sensitive_seen:
        summary_parts.append("sensitive_value_present=true")
    return " ".join(summary_parts) if summary_parts else "none"


def format_tool_call_event(event: dict) -> str:
    """生成不泄漏 raw args 的 tool_call 摘要。"""
    tool_name = str(event.get("tool") or "unknown")
    return f"TOOL CALL {tool_name} [args={format_tool_args_summary(event.get('args', {}))}]"


def format_log_event_for_cli(event: dict) -> str:
    """生成适合 `miclaw logs --tail` 的安全单行摘要。"""
    event_type = str(event.get("event") or event.get("event_type") or "unknown")
    trace_prefix = format_trace_prefix(event)

    if event_type == "permission_decision":
        return f"{trace_prefix}{format_permission_decision_event(event)}"
    if event_type == "tool_call":
        return f"{trace_prefix}{format_tool_call_event(event)}"
    if event_type == "tool_result":
        tool_name = str(event.get("tool") or "unknown")
        return f"{trace_prefix}TOOL RESULT {tool_name}"
    if event_type == "llm_input":
        return f"{trace_prefix}LLM INPUT message_count={event.get('message_count', 0)}"
    if event_type == "ai_message":
        content = str(event.get("content") or "")
        return f"{trace_prefix}AI MESSAGE content_present={bool(content)} content_length={len(content)}"
    if event_type == "system_action":
        content = str(event.get("content") or "")
        return f"{trace_prefix}SYSTEM ACTION content_present={bool(content)} content_length={len(content)}"
    if event_type == "parse_error":
        return f"{trace_prefix}[parse_error] malformed JSONL line skipped"
    return f"{trace_prefix}EVENT {event_type}"


def render_event(line: str | dict):
    """解析并渲染监控日志；未知 event 使用通用 fallback。"""
    data = parse_event_line(line) if isinstance(line, str) else line
    if not data:
        return

    event = data.get("event") or data.get("event_type") or "unknown"
    ts = _format_timestamp(data)
    safe_ts = escape(ts)
    prefix = f"[timestamp][ {safe_ts} ][/timestamp] {format_trace_prefix_for_markup(data)}"

    if event == "llm_input":
        count = data.get("message_count", 0)
        console.print(f"{prefix}[llm_input]🧠 神经元唤醒：发送了 {count} 条上下文记忆...[/llm_input]")

    elif event == "tool_call":
        tool_name = data.get("tool", "unknown")
        content = (
            f"[bold white] ● 使用工具: [/bold white][bold color(141)]{tool_name}[/bold color(141)]\n"
            f"{format_tool_call_event(data)}"
        )
        console.print(Panel(content, title=f"✦ 意图决断 [ {safe_ts} ]", title_align="left", border_style="color(141)", width=60))

    elif event == "tool_result":
        tool_name = data.get("tool", "unknown")
        result = str(data.get("result_summary", ""))
        display_result = result[:300] + "\n...[截断]..." if len(result) > 300 else result
        content = f"[bold white] ● 执行结果: [/bold white][bold cyan]{tool_name}[/bold cyan]\n{display_result}"
        console.print(Panel(content, title=f"✦ 环境回传 [ {safe_ts} ]", title_align="left", border_style="cyan", width=60))

    elif event == "system_action":
        action = data.get("content", "")
        console.print(f"{prefix}[warning]✦ 底层状态机：{action}[/warning]")

    elif event == "permission_decision":
        decision = str(data.get("decision") or "").lower()
        style = get_permission_decision_style(decision)
        console.print(f"{prefix}[{style}]✦ {format_permission_decision_event(data)}[/{style}]")

    elif event == "parse_error":
        console.print(f"{prefix}[error]✦ 日志解析失败：{data.get('error', 'unknown error')}[/error]")

    else:
        console.print(f"{prefix}[info]✦ Event: {event}[/info]")


def main(log_file: str | Path | None = None):
    try:
        console.clear()
        for line in tail_f(resolve_monitor_log_file(log_file)):
            render_event(line)
    except KeyboardInterrupt:
        console.print("\n[warning]✦ 监控网络已断开。[/warning]")

if __name__ == "__main__":
    main()
