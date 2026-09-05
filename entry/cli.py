import os
import asyncio
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

from miclaw.core.llm.provider import get_provider
from miclaw.core.security.permissions import (
    PermissionCapability,
    PermissionConfirmationChoice,
    PermissionRequest,
    PermissionResult,
    reset_permission_confirmation_handler,
    reset_session_permission_grants,
    set_permission_confirmation_handler,
    set_session_permission_grants,
)
from miclaw.core.runtime.workspace import reset_active_project_root, set_active_project_root
from langchain_core.messages import HumanMessage

ENTRY_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(ENTRY_DIR) 

os.chdir(PROJECT_ROOT)

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

app = typer.Typer(help="MiClaw - 极客专属的赛博智能终端")
skills_app = typer.Typer(help="查看当前 workspace 中发现的 Skill。", no_args_is_help=True)
execution_app = typer.Typer(help="查看 execution attempt，并显式请求 targeted recovery。", no_args_is_help=True)
app.add_typer(skills_app, name="skills")
app.add_typer(execution_app, name="execution")
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


def _execution_paths(workspace: str | None) -> tuple[Path, Path]:
    """解析 execution control 使用的同一 runtime workspace 内两份 SQLite 状态。"""
    try:
        root = (
            Path(workspace).expanduser().resolve(strict=True)
            if workspace is not None
            else Path(os.getenv("MICLAW_WORKSPACE", os.path.join(PROJECT_ROOT, "workspace"))).expanduser().resolve()
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        _execution_cli_error("invalid_workspace")
    if not root.is_dir():
        _execution_cli_error("invalid_workspace")
    return root / "execution.sqlite3", root / "state.sqlite3"


def _execution_cli_error(code: str) -> None:
    """输出不含路径、SQL 或 checkpoint detail 的稳定 execution control 错误。"""
    console.print(code, markup=False)
    raise typer.Exit(code=2)


def _validate_execution_cli_id(execution_id: str) -> None:
    """在 Store 前拒绝空白 execution identity，保持 exact-match control surface。"""
    if type(execution_id) is not str or not execution_id.strip():
        _execution_cli_error("invalid_execution_id")


def _open_execution_store_readonly(path: Path):
    """以只读 Store 打开已有 execution DB；缺失数据库不触发 SQLite 创建。"""
    from miclaw.core.runtime.execution_store import ExecutionStore, ExecutionStoreError

    if not path.is_file():
        return None
    try:
        return ExecutionStore(path, readonly=True)
    except ExecutionStoreError as exc:
        _execution_cli_error(str(exc))


def _safe_time(value: object) -> str:
    """只渲染领域 state 已验证的时间字段。"""
    return value.isoformat() if value is not None else "-"


def _safe_failure(record: object) -> str:
    """从稳定 ExecutionAttemptRecord 读取 failure code，不暴露异常 detail。"""
    failure = getattr(record, "failure", None)
    return failure.code.value if failure is not None else "-"


def _safe_retry(record: object) -> str:
    """从稳定 ExecutionAttemptRecord 读取 retry decision，不暗示自动执行。"""
    evaluation = getattr(record, "retry_evaluation", None)
    return evaluation.decision.value if evaluation is not None else "-"


@execution_app.command("list")
def execution_list(
    workspace: Annotated[Optional[str], typer.Option(help="指定包含 execution.sqlite3 的 runtime workspace。")] = None,
    limit: Annotated[int, typer.Option(help="最多显示的 logical execution 数量（1-100）。")] = 20,
):
    """列出每个 logical execution 的最新 attempt；该命令只读。"""
    if type(limit) is not int or not 1 <= limit <= 100:
        _execution_cli_error("invalid_limit")
    execution_path, _ = _execution_paths(workspace)
    store = _open_execution_store_readonly(execution_path)
    if store is None:
        console.print("No executions found.", markup=False)
        return
    try:
        records = store.list_latest_attempts(limit=limit)
    except Exception as exc:
        _execution_cli_error(str(exc) if str(exc) in {"execution_store_error", "invalid_execution_record"} else "execution_store_error")
    finally:
        store.close()
    if not records:
        console.print("No executions found.", markup=False)
        return
    for record in records:
        state = record.state
        console.print(
            f"{state.execution_id} attempt={state.attempt} status={state.status.value} "
            f"failure={_safe_failure(record)} retry={_safe_retry(record)} finished={_safe_time(state.finished_at)}",
            markup=False,
        )


@execution_app.command("show")
def execution_show(
    execution_id: str,
    workspace: Annotated[Optional[str], typer.Option(help="指定包含 execution.sqlite3 的 runtime workspace。")] = None,
):
    """展示一个 logical execution 的全部 attempts；该命令只读。"""
    _validate_execution_cli_id(execution_id)
    execution_path, _ = _execution_paths(workspace)
    store = _open_execution_store_readonly(execution_path)
    if store is None:
        _execution_cli_error("execution_not_found")
    try:
        records = store.list_attempts(execution_id)
    except Exception as exc:
        _execution_cli_error("execution_not_found" if str(exc) == "execution_record_not_found" else "execution_store_error")
    finally:
        store.close()
    console.print(f"Execution: {execution_id}", markup=False)
    for record in records:
        state = record.state
        evaluation = record.retry_evaluation
        console.print(
            f"attempt={state.attempt} status={state.status.value} started={_safe_time(state.started_at)} "
            f"finished={_safe_time(state.finished_at)} failure={_safe_failure(record)} retry={_safe_retry(record)} "
            f"retry_reason={evaluation.reason.value if evaluation is not None else '-'}",
            markup=False,
        )


@execution_app.command("recover")
def execution_recover(
    execution_id: str,
    attempt: Annotated[int, typer.Option("--attempt", help="要恢复的明确 attempt（>=1）。")],
    workspace: Annotated[Optional[str], typer.Option(help="指定同时包含 execution.sqlite3/state.sqlite3 的 runtime workspace。")] = None,
):
    """对单个 attempt 执行 checkpoint assessment/reconciliation/planning，绝不执行 graph continuation。"""
    _validate_execution_cli_id(execution_id)
    if type(attempt) is not int or attempt < 1:
        _execution_cli_error("invalid_attempt")
    execution_path, state_path = _execution_paths(workspace)
    if not execution_path.is_file() or not state_path.is_file():
        _execution_cli_error("recovery_not_available")

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from miclaw.core.agent.graph import create_agent_app
    from miclaw.core.agent.recovery import AgentRecoveryError, recover_execution
    from miclaw.core.runtime.execution_store import ExecutionStore, ExecutionStoreError

    async def recover_once():
        store = ExecutionStore(execution_path)
        try:
            async with AsyncSqliteSaver.from_conn_string(str(state_path)) as checkpointer:
                graph = create_agent_app(
                    provider_name=os.getenv("DEFAULT_PROVIDER", "aliyun"),
                    model_name=os.getenv("DEFAULT_MODEL", "glm-5"),
                    tools=[],
                    checkpointer=checkpointer,
                )
                return await recover_execution(store, graph, execution_id, attempt)
        finally:
            store.close()

    try:
        result = asyncio.run(recover_once())
    except AgentRecoveryError as exc:
        _execution_cli_error("execution_not_found" if str(exc) == "execution_record_not_found" else "recovery_not_available")
    except ExecutionStoreError as exc:
        _execution_cli_error("execution_not_found" if str(exc) == "execution_record_not_found" else "execution_store_error")
    except Exception:
        _execution_cli_error("recovery_not_available")

    record = result.record.state
    console.print(f"Recovery decision: {result.assessment.decision.value}", markup=False)
    console.print(f"Recovery reason: {result.assessment.reason.value}", markup=False)
    console.print(f"Attempt {record.attempt}: {record.status.value}", markup=False)
    if result.resume_record is not None:
        console.print("Recovery planned.", markup=False)
        console.print(f"Attempt {result.resume_record.state.attempt}: pending", markup=False)
        console.print("Attempt has not been executed.", markup=False)
    if result.assessment.decision.value == "do_not_resume":
        raise typer.Exit(code=2)

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
    if request.capability is PermissionCapability.MCP_TOOL:
        target = str(request.target or "")
        return _safe_prompt_text(target) if _mcp_prompt_identity_parts(target) else "hidden"
    if request.capability in {PermissionCapability.MEMORY_READ, PermissionCapability.MEMORY_WRITE}:
        target = str(request.target or "")
        return _safe_prompt_text(target) if _is_safe_memory_identity(target) else "hidden"
    if request.capability not in {PermissionCapability.FILE_READ, PermissionCapability.FILE_WRITE}:
        return "hidden"

    target = str(request.target or "")
    path = Path(target)
    if path.is_absolute() or PureWindowsPath(target).drive or ".." in path.parts:
        return "hidden"
    return _safe_prompt_text(target or ".")


def _safe_workspace_scope(request: PermissionRequest) -> str:
    """只返回当前 CLI 支持展示的 workspace scope。"""
    if request.capability is PermissionCapability.MCP_TOOL:
        identity = _mcp_prompt_identity_parts(str(request.target or ""))
        return f"mcp:{identity[0]}" if identity else "hidden"
    scope = str(request.metadata.get("workspace_scope") or "office")
    return scope if scope in {"office", "project", "global"} else "hidden"


def _mcp_prompt_identity_parts(target: str) -> tuple[str, str] | None:
    """只接受 permission policy 同形的 MCP qualified identity。"""
    parts = target.split("::")
    if len(parts) != 3 or parts[0] != "mcp":
        return None
    server_id, tool_name = parts[1:]
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if not all(value and len(value) <= 80 and set(value) <= allowed for value in (server_id, tool_name)):
        return None
    return server_id, tool_name


def _is_safe_memory_identity(target: str) -> bool:
    """只展示 GLOBAL 或 opaque PROJECT user-profile identity。"""
    if target == "user-profile":
        return True
    prefix, separator, project_id = target.partition("::")
    return (
        prefix == "user-profile"
        and separator == "::"
        and len(project_id) == 24
        and all(char in "0123456789abcdef" for char in project_id)
    )


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
    mcp_config: Annotated[
        Optional[str],
        typer.Option("--mcp-config", help="指定 host 控制的本地 MCP stdio JSON 配置文件。"),
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

        if mcp_config is None:
            miclaw_main.main()
        else:
            miclaw_main.main(mcp_config_path=mcp_config)
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
        from miclaw.core.skills.loader import list_skill_metadata

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
        from miclaw.core.skills.loader import validate_skills

    results = validate_skills()
    if not results:
        console.print("No skills found.", markup=False)
        return

    console.print("Skill lint results", markup=False)
    console.print("", markup=False)
    for result in results:
        skill = result.skill[:40]
        status = result.status
        issues = ", ".join(issue.code for issue in result.issues)
        console.print(f"{skill:<24} {status:<7} {issues}", markup=False)

    valid = sum(result.status == "OK" for result in results)
    warnings = sum(result.status == "WARNING" for result in results)
    errors = sum(result.status == "ERROR" for result in results)
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
