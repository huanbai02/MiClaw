import time
import json
import os
from pathlib import Path
from rich.console import Console
from rich.theme import Theme
from rich.panel import Panel
from rich.text import Text
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


def _format_timestamp(data: dict) -> str:
    ts_str = str(data.get("ts") or data.get("timestamp") or "")
    try:
        if ts_str.endswith('Z'):
            ts_str = ts_str[:-1] + '+00:00'
        dt_local = datetime.fromisoformat(ts_str).astimezone()
        return dt_local.strftime("%H:%M:%S")
    except Exception:
        return ts_str.split("T")[-1][:8] if ts_str else "--:--:--"


def render_event(line: str | dict):
    """解析并渲染监控日志；未知 event 使用通用 fallback。"""
    data = parse_event_line(line) if isinstance(line, str) else line
    if not data:
        return

    event = data.get("event") or data.get("event_type") or "unknown"
    ts = _format_timestamp(data)
    prefix = f"[timestamp][ {ts} ][/timestamp] "

    if event == "llm_input":
        count = data.get("message_count", 0)
        console.print(f"{prefix}[llm_input]🧠 神经元唤醒：发送了 {count} 条上下文记忆...[/llm_input]")

    elif event == "tool_call":
        tool_name = data.get("tool", "unknown")
        args_str = json.dumps(data.get("args", {}), ensure_ascii=False, indent=2)
        content = f"[bold white] ● 使用工具: [/bold white][bold color(141)]{tool_name}[/bold color(141)]\n传入参数:\n{args_str}"
        console.print(Panel(content, title=f"✦ 意图决断 [ {ts} ]", title_align="left", border_style="color(141)", width=60))

    elif event == "tool_result":
        tool_name = data.get("tool", "unknown")
        result = str(data.get("result_summary", ""))
        display_result = result[:300] + "\n...[截断]..." if len(result) > 300 else result
        content = f"[bold white] ● 执行结果: [/bold white][bold cyan]{tool_name}[/bold cyan]\n{display_result}"
        console.print(Panel(content, title=f"✦ 环境回传 [ {ts} ]", title_align="left", border_style="cyan", width=60))

    elif event == "system_action":
        action = data.get("content", "")
        console.print(f"{prefix}[warning]✦ 底层状态机：{action}[/warning]")

    elif event == "permission_decision":
        tool_name = data.get("tool_name", "unknown")
        capability = data.get("capability", "unknown")
        decision = data.get("decision", "unknown")
        risk_level = data.get("risk_level", "unknown")
        console.print(
            f"{prefix}[warning]✦ Permission: {tool_name} {capability} -> {decision} ({risk_level})[/warning]"
        )

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
