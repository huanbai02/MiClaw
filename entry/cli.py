import os
import typer
import questionary
import logging
from pathlib import Path, PureWindowsPath
from typing import Annotated, Optional
from rich.console import Console
from rich.panel import Panel
from rich.status import Status
from dotenv import set_key, load_dotenv, unset_key
import sys

from miclaw.core.provider import get_provider
from miclaw.core.permissions import (
    PermissionCapability,
    PermissionConfirmationChoice,
    PermissionRequest,
    PermissionResult,
    reset_permission_confirmation_handler,
    reset_session_permission_grants,
    set_permission_confirmation_handler,
    set_session_permission_grants,
)
from miclaw.core.workspace import reset_active_project_root, set_active_project_root
from langchain_core.messages import HumanMessage

ENTRY_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(ENTRY_DIR) 

os.chdir(PROJECT_ROOT)

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

app = typer.Typer(help="MiClaw - 极客专属的赛博智能终端")
skills_app = typer.Typer(help="查看当前 workspace 中发现的 Skill。", no_args_is_help=True)
app.add_typer(skills_app, name="skills")
console = Console()

miclaw_style = questionary.Style([
    ('qmark', 'fg:#8d52ff bold'),       
    ('question', 'fg:#00ffff bold'),    
    ('answer', 'fg:#8d52ff bold'),      
    ('pointer', 'fg:#00ffff bold'),     
    ('highlighted', 'fg:#00ffff bold'), 
    ('selected', 'fg:#00ffff'),
    ('instruction', 'fg:#808080 dim'),  
])

ENV_PATH = os.path.join(PROJECT_ROOT, ".env")

@app.command("config")
def config_wizard():
    console.clear()
    console.print(Panel(
        "👾 Welcome to [bold #8d52ff]MiClaw[/bold #8d52ff]...\n\n☁️[dim] 请完成模型配置，我们将把密钥安全固化在本地。[/dim]", 
        title="[bold white]✦  MiClaw Config[/bold white]", 
        border_style="#8d52ff"
    ))
    provider_raw = questionary.select(
        "选择你的模型提供商 (Provider):",
        choices=["openai", "anthropic", "aliyun (openai compatible)","tencent (openai compatible)", "z.ai (openai compatible)", "other (openai compatible)", "ollama"],
        style=miclaw_style,
        instruction="(按上下键选择，回车确认)"
    ).ask()

    if not provider_raw:
        console.print("[dim #8d52ff]✦   录入中断，MiClaw 配置已取消。[/dim #8d52ff]")
        return

    provider = provider_raw.split(" ")[0].strip()
    is_openai_compatible = "openai" in provider_raw.lower()

    model_name = questionary.text(
        "输入指定的模型型号 (如 gpt-4o-mini, qwen-max, glm-4 等):",
        style=miclaw_style
    ).ask()

    if model_name is None:
        console.print("[dim #8d52ff]✦   录入中断，MiClaw 配置已取消。[/dim #8d52ff]")
        return

    api_key = ""
    env_key = ""
    if provider != "ollama":
        if is_openai_compatible:
            env_key = "OPENAI_API_KEY"
        elif provider == "anthropic":
            env_key = "ANTHROPIC_API_KEY"

        api_key = questionary.password(
            f"输入你的 {env_key} (对应 {provider_raw}):",
            style=miclaw_style
        ).ask()

        if api_key is None:
            console.print("[dim #8d52ff]✦   录入中断，MiClaw 配置已取消。[/dim #8d52ff]")
            return

    base_url = ""
    if provider in ["openai", "anthropic"]:
        base_url = questionary.text(
            f"输入 {provider} 代理 Base URL (直连请直接回车跳过):",
            style=miclaw_style
        ).ask()
    elif provider == "ollama":
        base_url = questionary.text(
            "输入 Ollama Base URL (默认 http://localhost:11434，直接回车跳过):",
            style=miclaw_style
        ).ask()
    else:
        base_url = questionary.text(
            "输入兼容 Base URL (不填直接回车将使用官方默认地址):",
            style=miclaw_style
        ).ask()

    if base_url is None:
        console.print("[dim #8d52ff]✦   录入中断，MiClaw 配置已取消。[/dim #8d52ff]")
        return

    console.print("\n[dim]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/dim]")

    with Status(f"[bold #8d52ff]正在连接 {provider.upper()} 引擎并发送探测包...[/bold #8d52ff]", spinner="dots", spinner_style="#00ffff"):
        try:
            if env_key and api_key:
                os.environ[env_key] = api_key
            if base_url:
                if is_openai_compatible:
                    os.environ["OPENAI_API_BASE"] = base_url
                else:
                    os.environ[f"{provider.upper()}_BASE_URL"] = base_url

            llm = get_provider(provider_name=provider, model_name=model_name)
            response = llm.invoke([HumanMessage(content="回复我'收到'。")])

            console.print(" [bold #00ffff][ 配置成功!][/bold #00ffff]")
            
        except Exception as e:

            console.print(f" [bold #8d52ff][ 配置失败!][/bold #8d52ff]  无法连接到模型，请检查 Key、Base URL、模型型号 或 网络！\n[dim]错误信息: {str(e)}[/dim]")
            return


    if not os.path.exists(ENV_PATH):
        open(ENV_PATH, 'w').close()

    logging.getLogger("dotenv.main").setLevel(logging.ERROR)

    unset_key(ENV_PATH, "OPENAI_API_BASE")
    unset_key(ENV_PATH, "ANTHROPIC_BASE_URL")
    unset_key(ENV_PATH, "OLLAMA_BASE_URL")

    if env_key and api_key:
        set_key(ENV_PATH, env_key, api_key)
        
    if base_url:
        if is_openai_compatible:
            set_key(ENV_PATH, "OPENAI_API_BASE", base_url)
        else:
            set_key(ENV_PATH, f"{provider.upper()}_BASE_URL", base_url)
    
    set_key(ENV_PATH, "DEFAULT_PROVIDER", provider)
    set_key(ENV_PATH, "DEFAULT_MODEL", model_name)

    console.print(Panel(
        f"配置已保存至 [#8d52ff]{ENV_PATH}[/#8d52ff]\n"
        f"当前默认提供商: [#8d52ff]{provider}[/#8d52ff] | 模型: [#8d52ff]{model_name}[/#8d52ff]\n\n"
        f"👉 输入 [bold #00ffff]miclaw run[/bold #00ffff] 即可启动系统！",
        border_style="#00ffff"
    ))

def _show_boot_error():
    console.print(Panel(
        "[bold #00ffff]MiClaw未完成配置![/bold #00ffff]\n\n"
        "[#8d52ff]检测到 API Key、模型或Baseurl。请重新执行以下命令完成配置：[/#8d52ff]\n"
        "[bold #00ffff]miclaw config[/bold #00ffff]",
        title="[bold #8d52ff]⚠️ Boot Sequence Failed[/bold #8d52ff]",
        border_style="#8d52ff"
    ))


def _safe_prompt_text(value, limit: int = 160) -> str:
    """移除 terminal control character，并限制 prompt 字段长度。"""
    text = str(value or "")
    return "".join(char if char.isprintable() and char not in "\r\n" else "?" for char in text)[:limit]


def _safe_permission_target(request: PermissionRequest) -> str:
    """只展示已知 active workspace scope 内的相对 target。"""
    if request.capability is PermissionCapability.SHELL_EXEC:
        return _safe_workspace_scope(request)
    if request.capability not in {PermissionCapability.FILE_READ, PermissionCapability.FILE_WRITE}:
        return "hidden"

    target = str(request.target or "")
    path = Path(target)
    if path.is_absolute() or PureWindowsPath(target).drive or ".." in path.parts:
        return "hidden"
    return _safe_prompt_text(target or ".")


def _safe_workspace_scope(request: PermissionRequest) -> str:
    """只返回当前 CLI 支持展示的 workspace scope。"""
    scope = str(request.metadata.get("workspace_scope") or "office")
    return scope if scope in {"office", "project"} else "hidden"


def format_permission_confirmation_prompt(request: PermissionRequest, result: PermissionResult) -> str:
    """使用固定安全字段构造 CLI confirmation prompt。"""
    tool_name = _safe_prompt_text(request.metadata.get("tool_name") or "unknown_tool", limit=80)
    return (
        "Permission confirmation required\n"
        f"Tool: {tool_name}\n"
        f"Capability: {request.capability.value}\n"
        f"Operation: {_safe_prompt_text(request.operation, limit=80)}\n"
        f"Risk: {result.risk_level.value}\n"
        f"Workspace: {_safe_workspace_scope(request)}\n"
        f"Target: {_safe_permission_target(request)}\n"
        "Status: currently blocked pending confirmation\n"
        "Choose: [a] Allow once, [s] Allow for this session, [d] Deny"
    )


def cli_permission_confirmation_handler(
    request: PermissionRequest,
    result: PermissionResult,
) -> PermissionConfirmationChoice:
    """请求一次显式 CLI 确认；任何非明确同意或 prompt 异常都返回 DENY。"""
    try:
        answer = typer.prompt(
            format_permission_confirmation_prompt(request, result),
            default="d",
            show_default=True,
        )
    except (EOFError, KeyboardInterrupt):
        return PermissionConfirmationChoice.DENY
    except Exception:
        return PermissionConfirmationChoice.DENY
    normalized = str(answer).strip().lower()
    if normalized in {"a", "allow", "once", "y", "yes"}:
        return PermissionConfirmationChoice.ALLOW_ONCE
    if normalized in {"s", "session"}:
        return PermissionConfirmationChoice.ALLOW_SESSION
    return PermissionConfirmationChoice.DENY


@app.command("run")
def run_agent(
    workspace: Annotated[
        Optional[str],
        typer.Option(help="显式指定当前 run 使用的现有 PROJECT workspace directory。"),
    ] = None,
):
    load_dotenv(ENV_PATH)
    provider = os.getenv("DEFAULT_PROVIDER")
    model = os.getenv("DEFAULT_MODEL")
    if not provider or not model:
        _show_boot_error()
        raise typer.Exit()
    if provider != "ollama":
        if provider in ["openai", "aliyun", "z.ai", "tencent", "other"]: 
            if not os.getenv("OPENAI_API_KEY"):
                _show_boot_error()
                raise typer.Exit()
                
        elif provider == "anthropic":
            if not os.getenv("ANTHROPIC_API_KEY"):
                _show_boot_error()
                raise typer.Exit()
        
    project_token = None
    if workspace is not None:
        try:
            project_token = set_active_project_root(workspace)
        except ValueError as exc:
            console.print(f"Invalid project workspace: {exc}", markup=False)
            raise typer.Exit(code=2) from exc

    grants_token = set_session_permission_grants()
    confirmation_token = set_permission_confirmation_handler(cli_permission_confirmation_handler)
    try:
        import entry.main as miclaw_main

        miclaw_main.main()
    finally:
        reset_permission_confirmation_handler(confirmation_token)
        reset_session_permission_grants(grants_token)
        if project_token is not None:
            reset_active_project_root(project_token)

@app.command("monitor")
def run_monitor(
    log_file: Optional[str] = typer.Option(None, "--log-file", help="指定要读取的 JSONL log 文件。")
):
        
    try:
        import entry.monitor as miclaw_monitor
        miclaw_monitor.main(log_file=log_file)
    except ImportError as e:
        console.print(f"[bold red]启动失败：找不到监视器模块！[/bold red]\n[dim]请确保 monitor.py 和 cli.py 在同一目录下。\n报错信息: {e}[/dim]")


@skills_app.command("list")
def list_skills_command():
    """列出当前 workspace 中已发现的 Skill metadata。"""
    from contextlib import redirect_stdout
    from io import StringIO

    # config 初始化仍会输出绝对 workspace path，此处仅隔离 import side effect。
    with redirect_stdout(StringIO()):
        from miclaw.core.skill_loader import list_skill_metadata

    skills = list_skill_metadata()
    if not skills:
        console.print("No skills found.", markup=False)
        return

    console.print(f"Available skills: {len(skills)}", markup=False)
    console.print("", markup=False)
    console.print(f"{'NAME':<24} DESCRIPTION", markup=False)
    for skill in skills:
        name = str(skill.get("name") or "unknown")[:40]
        description = " ".join(str(skill.get("description") or "unknown").split())[:160]
        console.print(f"{name:<24} {description}", markup=False)


@skills_app.command("lint")
def lint_skills_command():
    """静态检查当前 workspace 中 Skill 的结构和基础 metadata。"""
    from contextlib import redirect_stdout
    from io import StringIO

    with redirect_stdout(StringIO()):
        from miclaw.core.skill_loader import lint_skills

    results = lint_skills()
    if not results:
        console.print("No skills found.", markup=False)
        return

    console.print("Skill lint results", markup=False)
    console.print("", markup=False)
    for result in results:
        skill = str(result["skill"])[:40]
        status = str(result["status"])
        issues = ", ".join(result["issues"])
        console.print(f"{skill:<24} {status:<7} {issues}", markup=False)

    valid = sum(result["status"] == "OK" for result in results)
    warnings = sum(result["status"] == "WARNING" for result in results)
    errors = sum(result["status"] == "ERROR" for result in results)
    console.print("", markup=False)
    console.print(f"{valid} valid, {warnings} warning, {errors} error", markup=False)
    if errors:
        raise typer.Exit(code=1)

@app.command("logs")
def logs_command(
    tail: bool = typer.Option(False, "--tail", help="显示最近的 JSONL log event。"),
    lines: int = typer.Option(20, "--lines", min=1, help="显示最近 N 条非空 log event。"),
    log_file: Optional[str] = typer.Option(None, "--log-file", help="指定要读取的 JSONL log 文件。"),
):
    """查看最近的 MiClaw JSONL log event。"""
    if not tail:
        console.print("请使用 `miclaw logs --tail` 查看最近日志。", markup=False)
        return

    import entry.monitor as miclaw_monitor

    resolved_log_file = miclaw_monitor.resolve_monitor_log_file(log_file)
    if not resolved_log_file.exists():
        console.print(f"No log file found at {resolved_log_file}", markup=False)
        return

    events = miclaw_monitor.tail_log_events(resolved_log_file, lines=lines)
    if not events:
        console.print(f"No log events found at {resolved_log_file}", markup=False)
        return

    for event in events:
        console.print(miclaw_monitor.format_log_event_for_cli(event), markup=False)

@app.command("trace")
def trace_command(
    run_id: str = typer.Argument(..., help="要查看的 run_id。"),
    log_file: Optional[str] = typer.Option(None, "--log-file", help="指定要读取的 JSONL log 文件。"),
):
    """查看指定 run_id 的 MiClaw trace event。"""
    import entry.monitor as miclaw_monitor

    resolved_log_file = miclaw_monitor.resolve_monitor_log_file(log_file)
    if not resolved_log_file.exists():
        console.print(f"No log file found at {resolved_log_file}", markup=False)
        return

    events = miclaw_monitor.read_jsonl_events(resolved_log_file)
    trace_events = miclaw_monitor.get_trace_events(events, run_id)
    if not trace_events:
        console.print(f"No events found for run_id {run_id}", markup=False)
        return

    console.print(f"Trace run={run_id}", markup=False)
    for event in trace_events:
        console.print(miclaw_monitor.format_log_event_for_cli(event), markup=False)

def main():
    app()

if __name__ == "__main__":
    main()
