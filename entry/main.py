import os
import sys
import time
import asyncio
import random
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.styles import Style
from prompt_toolkit.application import get_app

from miclaw.core.agent.active_execution import ActiveExecutionController
from miclaw.core.agent.execution import apply_graph_recursion_limit, new_execution_id, run_agent_execution
from miclaw.core.agent.recovery import apply_checkpoint_correlation, latest_owned_checkpoint, new_checkpoint_run_id
from miclaw.core.agent.graph import create_agent_app, default_agent_tools
from miclaw.core.agent.request import AgentRequest, AgentRequestOrigin
from miclaw.core.mcp.client import MCPClientError
from miclaw.core.mcp.runtime_config import MCPRuntimeConfigError, load_mcp_stdio_configs
from miclaw.core.mcp.tools import MCPAgentToolRuntime, MCPToolRegistrationError
from miclaw.core.memory.lifecycle import (
    MemoryWriteIntent,
    reset_memory_write_intent,
    set_memory_write_intent,
)
from miclaw.core.runtime.config import DB_PATH, EXECUTION_DB_PATH
from miclaw.core.security.permissions import reset_session_permission_grants, set_session_permission_grants
from miclaw.core.runtime.execution_store import ExecutionStore
from miclaw.core.scheduler.heartbeat import pacemaker_loop
from miclaw.core.observability.trace import TraceContext, new_run_id, reset_trace_context, set_current_trace_context

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def type_line(text: str, delay: float = 0.008):
    for ch in text:
        print(ch, end='', flush=True)
        time.sleep(delay)
    print()

def print_banner():
    clear_screen()

    CYAN = '\033[38;5;51m'
    PURPLE = '\033[38;5;141m'
    SILVER = '\033[38;5;250m'
    DIM = '\033[2m'
    BOLD = '\033[1m'
    RESET = '\033[0m'
    WHITE = '\033[37m'

    logo = f"""{CYAN}{BOLD}
███╗   ███╗██╗ ██████╗██╗      █████╗ ██╗    ██╗
████╗ ████║██║██╔════╝██║     ██╔══██╗██║    ██║
██╔████╔██║██║██║     ██║     ███████║██║ █╗ ██║
██║╚██╔╝██║██║██║     ██║     ██╔══██║██║███╗██║
██║ ╚═╝ ██║██║╚██████╗███████╗██║  ██║╚███╔███╔╝
╚═╝     ╚═╝╚═╝ ╚═════╝╚══════╝╚═╝  ╚═╝ ╚══╝╚══╝
{RESET}"""

    sub_title = f"{WHITE}{BOLD} 👾 Welcome to the {PURPLE}{BOLD}MiClaw{RESET}{WHITE}{BOLD} !  {RESET}"

    quotes = [
        "It works on my machine.",
        "It compiles! Ship it.",
        "Git commit, push, pray.",
        "There's no place like 127.0.0.1.",
        "sudo make me a sandwich.",
        "Works fine in dev.",
        "May the source be with you.",
        "Ctrl+C, Ctrl+V, Deploy.",
        "Hello, World."
    ]
    quote = random.choice(quotes)
    meta = f" {SILVER}✦{RESET} {CYAN}{quote}{RESET}"

    tip = (
        f"{PURPLE} ✦ {RESET}"
        f"{SILVER}{PURPLE}{BOLD}MiClaw{RESET} 已完成启动。输入命令开始，输入 {PURPLE}/exit{RESET}{SILVER} 退出。{RESET}\n"
    )

    print(logo)
    print(sub_title)
    print() 
    time.sleep(0.12)
    print(meta)
    print() 
    type_line(tip, delay=0.004)


def cprint(text="", end="\n"):
    print_formatted_text(ANSI(str(text)), end=end)


def _observe_critical_task(task: asyncio.Task, failure_signal: asyncio.Future, failure_code: str, *, must_run: bool = False):
    """将关键后台任务的非取消结束转换为稳定的 runtime failure signal。"""
    if task.cancelled():
        return

    exception = task.exception()
    if exception is None and not must_run:
        return
    if not failure_signal.done():
        failure_signal.set_result(failure_code)


def _parse_user_request(user_input: str) -> AgentRequest | None:
    """只在交互输入边界解析 /remember 并建立可信 turn metadata。"""
    if user_input == "/remember" or (
        user_input.startswith("/remember")
        and len(user_input) > len("/remember")
        and user_input[len("/remember")].isspace()
    ):
        content = user_input[len("/remember"):].strip()
        if not content:
            cprint("Usage: /remember <content>")
            return None
        return AgentRequest(
            content=f"请记住以下信息：{content}",
            origin=AgentRequestOrigin.INTERACTIVE,
            memory_write_intent=MemoryWriteIntent.EXPLICIT_USER_REQUEST,
        )
    return AgentRequest(content=user_input, origin=AgentRequestOrigin.INTERACTIVE)


async def async_main(trace_context: TraceContext | None = None, mcp_config_path: str | None = None):
    print_banner()
    
    from dotenv import load_dotenv
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    load_dotenv(env_path)
    
    current_provider = os.getenv("DEFAULT_PROVIDER", "aliyun")
    current_model = os.getenv("DEFAULT_MODEL", "glm-5")

    try:
        mcp_configs = load_mcp_stdio_configs(mcp_config_path)
    except MCPRuntimeConfigError:
        raise RuntimeError("invalid_mcp_config") from None

    async with AsyncSqliteSaver.from_conn_string(DB_PATH) as memory:
        execution_store = ExecutionStore(EXECUTION_DB_PATH)
        mcp_runtime = MCPAgentToolRuntime(mcp_configs, local_tools=default_agent_tools())
        try:
            await mcp_runtime.__aenter__()
        except (MCPClientError, MCPToolRegistrationError, ValueError):
            execution_store.close()
            raise RuntimeError("mcp_runtime_start_failed") from None
        try:
            app = create_agent_app(
                provider_name=current_provider,
                model_name=current_model,
                tools=mcp_runtime.tools,
                checkpointer=memory,
            )
        except BaseException as exc:
            try:
                await mcp_runtime.__aexit__(type(exc), exc, exc.__traceback__)
            finally:
                execution_store.close()
            raise
        config = {"configurable": {"thread_id": "local_geek_master"}}
        if trace_context is not None:
            config["configurable"]["run_id"] = trace_context.run_id
        config = apply_graph_recursion_limit(config)
        runtime_task_queue = asyncio.Queue()
        active_execution = ActiveExecutionController()

        class SpinnerState:
            action_words = [
                "Thinking...",              
                "Working...",               
                "Beep boop...",             
                "Eating bugs...",           
                "Charging battery...",      
                "Brewing coffee...",        
                "Blinking lights...",       
                "Polishing pixels...",      
                "Scanning matrix...",       
                "Warming up circuits...",   
                "Syncing data...",          
                "Pinging server..."         
            ]
            current_words = [] 
            is_spinning = False
            start_time = 0
            frames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
            is_tool_calling = False 
            tool_msg = ""           

        spinner = SpinnerState()


        def get_bottom_toolbar():
            if not spinner.is_spinning:
                return ANSI("") 
            
            elapsed = time.time() - spinner.start_time
            if spinner.is_tool_calling:
                display_msg = spinner.tool_msg
            else:
                idx_word = int(elapsed) % len(spinner.current_words)
                display_msg = f"👾 {spinner.current_words[idx_word]}"

            idx_frame = int(elapsed * 12) % len(spinner.frames)
            frame = spinner.frames[idx_frame]
            

            return ANSI(f"  \033[38;5;51m{frame}\033[0m \033[38;5;250m{display_msg}\033[0m \033[38;5;141m[{elapsed:.1f}s]\033[0m")

        prompt_message = ANSI("  \033[38;5;51m❯\033[0m ")
        placeholder_text = ANSI("\033[3m\033[38;5;242minput...\033[0m")

        async def agent_worker(current_task_queue, controller: ActiveExecutionController):
            while True:
                request = await current_task_queue.get()
                if not isinstance(request, AgentRequest):
                    current_task_queue.task_done()
                    raise RuntimeError("invalid_agent_request")
                if request.origin is AgentRequestOrigin.INTERACTIVE and request.content.lower() in ["/exit", "/quit"]:
                    current_task_queue.task_done()
                    break

                intent_token = None
                grant_token = None
                if request.origin is AgentRequestOrigin.SCHEDULER:
                    grant_token = set_session_permission_grants()
                if request.memory_write_intent is not None:
                    intent_token = set_memory_write_intent(request.memory_write_intent)
                try:
                    spinner.current_words = spinner.action_words.copy()
                    random.shuffle(spinner.current_words)

                    spinner.start_time = time.time()
                    spinner.is_spinning = True
                    spinner.is_tool_calling = False

                    inputs = {"messages": [HumanMessage(content=request.content)]}
                    checkpoint_thread_id = config["configurable"]["thread_id"]
                    checkpoint_run_id = new_checkpoint_run_id()
                    invocation_config = apply_checkpoint_correlation(config, checkpoint_run_id)

                    async def invoke_graph_once():
                        async for event in app.astream(
                            inputs,
                            config=invocation_config,
                            stream_mode="updates",
                            durability="sync",
                        ):
                            for node_name, node_data in event.items():
                                if node_name == "agent":
                                    last_msg = node_data["messages"][-1]

                                    if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                                        for tc in last_msg.tool_calls:
                                            spinner.is_tool_calling = True
                                            spinner.tool_msg = f"唤醒内置工具 : {tc['name']}..."
                                            cprint(f"  ●\033[38;5;51m Tool Call: \033[0m{tc['name']}")
                                            cprint('')

                                    elif last_msg.content:
                                        spinner.is_spinning = False

                                        lines = last_msg.content.strip().split('\n')
                                        if lines:
                                            formatted_out = f"  \033[38;5;141m❯\033[0m \033[38;5;250m{lines[0]}"
                                            for line in lines[1:]:
                                                formatted_out += f"\n    {line}"
                                            formatted_out += "\033[0m"
                                            cprint(formatted_out)

                                elif node_name != "agent":
                                    spinner.is_tool_calling = False

                    async def checkpoint_id_provider():
                        checkpoint_ref, _ = await latest_owned_checkpoint(
                            app,
                            checkpoint_thread_id,
                            checkpoint_run_id,
                        )
                        return checkpoint_ref.checkpoint_id if checkpoint_ref is not None else None

                    execution_id = new_execution_id()
                    execution_task = asyncio.create_task(
                        run_agent_execution(
                            invoke_graph_once,
                            execution_id=execution_id,
                            trace_context=trace_context,
                            execution_store=execution_store,
                            checkpoint_thread_id=checkpoint_thread_id,
                            checkpoint_run_id=checkpoint_run_id,
                            checkpoint_id_provider=checkpoint_id_provider,
                        )
                    )
                    controller.register(execution_task, execution_id, 1)
                    try:
                        execution = await execution_task
                    except asyncio.CancelledError:
                        worker_task = asyncio.current_task()
                        if worker_task is not None and worker_task.cancelling() > 0:
                            raise
                        if not controller.cancellation_requested_for(execution_task):
                            raise
                        cprint("  \033[33m[ Execution cancelled. ]\033[0m")
                        continue
                    finally:
                        controller.clear_if_current(execution_task)
                    if execution.failure is not None:
                        spinner.is_spinning = False
                        cprint(f"  \033[31m[ ⚠️ 引擎执行失败 : {execution.failure.code.value} ]\033[0m")
                finally:
                    if intent_token is not None:
                        reset_memory_write_intent(intent_token)
                    if grant_token is not None:
                        reset_session_permission_grants(grant_token)
                    spinner.is_spinning = False
                    cprint() # 空出舒适的行距
                    current_task_queue.task_done()

        async def user_input_loop(current_task_queue, controller: ActiveExecutionController):
            custom_style = Style.from_dict({
                'bottom-toolbar': 'bg:default fg:default noreverse',
            })
            
            session = PromptSession(
                bottom_toolbar=get_bottom_toolbar,
                style=custom_style,
                erase_when_done=True,
                reserve_space_for_menu=0  
            )
            
            async def redraw_timer():
                while True:
                    if spinner.is_spinning:
                        try:
                            get_app().invalidate()
                        except Exception:
                            pass
                    await asyncio.sleep(0.08)
                    
            redraw_task = asyncio.create_task(redraw_timer())
            
            try:
                while True:
                    try:
                        user_input = await session.prompt_async(prompt_message, placeholder=placeholder_text)

                        user_input = user_input.strip()
                        if not user_input:
                            continue

                        if user_input == "/cancel":
                            outcome = controller.cancel_active()
                            if outcome == "cancellation_requested":
                                cprint("Cancellation requested.")
                            elif outcome == "cancellation_already_requested":
                                cprint("Cancellation already requested.")
                            else:
                                cprint("No active execution.")
                            continue
                    

                        request = _parse_user_request(user_input)
                        if request is None:
                            continue
                        padded_bubble = f"  ❯ {user_input}    "
                        cprint(f"\033[48;2;38;38;38m\033[38;5;255m{padded_bubble}\033[0m\n")
                    
                        await current_task_queue.put(request)
                        if user_input.lower() in ["/exit", "/quit"]:
                            controller.cancel_active()
                            cprint("  \033[38;5;141m✦ 记忆已固化，MiClaw 进入休眠。\033[0m")
                            break

                    except (KeyboardInterrupt, EOFError):
                        cprint("\n  \033[38;5;141m✦ 强制中断，MiClaw 进入休眠。\033[0m")
                        controller.cancel_active()
                        await current_task_queue.put(
                            AgentRequest(content="/exit", origin=AgentRequestOrigin.INTERACTIVE)
                        )
                        break
            finally:
                redraw_task.cancel()
                await asyncio.gather(redraw_task, return_exceptions=True)

        worker = None
        heartbeat_worker = None
        input_worker = None
        queue_join_worker = None
        try:
            with patch_stdout():
                worker = asyncio.create_task(agent_worker(runtime_task_queue, active_execution))
                heartbeat_worker = asyncio.create_task(pacemaker_loop(runtime_task_queue, check_interval=10))
                failure_signal = asyncio.get_running_loop().create_future()
                worker.add_done_callback(
                    lambda task: _observe_critical_task(task, failure_signal, "agent_worker_failed")
                )
                heartbeat_worker.add_done_callback(
                    lambda task: _observe_critical_task(
                        task,
                        failure_signal,
                        "scheduler_heartbeat_failed",
                        must_run=True,
                    )
                )

                input_worker = asyncio.create_task(user_input_loop(runtime_task_queue, active_execution))
                done, _ = await asyncio.wait(
                    (input_worker, failure_signal),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if failure_signal in done:
                    raise RuntimeError(failure_signal.result())
                await input_worker

                queue_join_worker = asyncio.create_task(runtime_task_queue.join())
                done, _ = await asyncio.wait(
                    (queue_join_worker, failure_signal),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if failure_signal in done:
                    raise RuntimeError(failure_signal.result())
                await queue_join_worker
        finally:
            for task in (input_worker, queue_join_worker, worker, heartbeat_worker):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (input_worker, queue_join_worker, worker, heartbeat_worker) if task is not None),
                return_exceptions=True,
            )
            execution_store.close()
            try:
                await mcp_runtime.__aexit__(None, None, None)
            except MCPClientError:
                raise RuntimeError("mcp_runtime_shutdown_failed") from None

def main(mcp_config_path: str | None = None):
    trace_context = TraceContext(run_id=new_run_id())
    trace_token = set_current_trace_context(trace_context)
    try:
        asyncio.run(async_main(trace_context=trace_context, mcp_config_path=mcp_config_path))
    finally:
        reset_trace_context(trace_token)

if __name__ == "__main__":
    main()
