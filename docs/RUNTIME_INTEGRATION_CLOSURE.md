# Runtime Integration Closure

## Scope

本文是 PR53–61 的当前运行时状态与 PR62 收口证据。`RUNTIME_INTEGRATION_AUDIT.md` 是历史审计基线，不作为当前能力说明维护。

PR62 只补 production-path 回归、日志隐私验证和文档；没有增加新的自动调度、传输协议或权限模型。

## Final Runtime Call Path

```text
miclaw run
  → bootstrap（可选 host-owned MCP stdio）
  → default builtins + workspace Dynamic Skills + configured MCP Tools
  → shared AgentRequest queue
  → origin-aware agent_worker
  → run_agent_execution（durable attempt lifecycle）
  → LangGraph Agent / Memory retrieval + context assembly
  → ToolNode
  → safe model-visible Tool content + structured Tool outcome
  → disposition（continue 或 typed TOOL failure）
  → ExecutionStore / checkpoint
```

控制面为 `miclaw execution list|show|recover|resume`。交互控制为 `/remember` 与 `/cancel`。heartbeat 产生 `SCHEDULER` origin request；它不继承 interactive session grants 或 Memory write intent。

## E2E Evidence Levels

| 级别 | 证据 |
| --- | --- |
| Tier 1 | deterministic production-path：真实 entry/graph/ToolNode/SQLite/permission/MCP subprocess，使用受控模型。CI 主证据。 |
| Tier 2 | 本地 Ollama 手工 smoke，不作为 CI 前置条件。 |
| Tier 3 | focused component 回归，补充而不替代 Tier 1。 |

## User-Reachable Journey Matrix

| Journey | 用户触发与真实路径 | 关键证据 | 成熟度 / 边界 |
| --- | --- | --- | --- |
| A 正常对话 | `miclaw run` → queue → worker → graph → `SUCCEEDED` | `test_entry_runtime_bootstrap.py::test_async_main_processes_one_message_into_execution_store` | L4；provider 由本地配置决定。 |
| B OFFICE read | Agent graph → ToolNode → contained office read | `test_tool_outcome_transport.py::test_real_sandbox_toolnode_transports_success_and_path_failure` | Focused graph/ToolNode evidence；低风险 read 不需不必要 ASK。 |
| C PROJECT write | interactive request → worker → ToolNode → PROJECT permission → durable attempt | `test_entry_runtime_bootstrap.py::test_project_write_runs_through_entry_agent_toolnode_and_permission` | Tier 1 / L4；ALLOW 写入一次，DENY 零副作用且模型继续。 |
| D Shell safety | shell Tool → safety classifier / permission | `tests/test_project_workspace_integration.py`、`tests/test_tool_failure_disposition.py` | L4；blocked command 不执行、无自动重试。 |
| E Dynamic Skill | startup discovery → default Tool set → ToolNode → existing shell permission path | `test_runtime_integration_closure.py::test_default_runtime_discovers_and_executes_workspace_dynamic_skill` | L4；无运行中 Tool hot reload、无 Skill ecosystem。 |
| F Memory read | interactive Agent → retriever → context assembler | `test_memory_context_e2e.py::test_agent_e2e_selects_global_project_or_global_fallback` | L4；只注入受控 profile context。 |
| G `/remember` | interactive origin + explicit intent → Memory Tool → permission/store | `test_entry_runtime_bootstrap.py::test_remember_entry_e2e_writes_profile_with_turn_local_intent` 及 deny/leak regressions | L4；不是自然语言自动记忆。 |
| H MCP stdio | host config → stdio lifecycle/tools/list → ToolNode → permission/tools/call | `test_mcp_runtime_entry.py::test_entry_mcp_config_discovers_and_invokes_real_stdio_tool_with_permission_allow`、deny/cleanup cases | L4；仅 stdio，host 控制启动参数。 |
| I retry/resume | provider timeout → FAILED + PENDING → explicit resume → safe continuation | `test_execution_cli.py::test_execution_resume_cli_runs_retry_created_pending_without_replaying_tool` | L5-like explicit closed loop；不自动消费 PENDING。 |
| J targeted recovery | explicit recover → completed reconciliation 或 safe PENDING planning | `test_execution_cli.py::test_execution_recover_cli_reconciles_completed_checkpoint_without_invocation`、safe/ToolNode cases | L5-like；exact owned checkpoint，绝不 START replay。 |
| K `/cancel` | interactive control → child Task cancel → durable `CANCELLED` → next turn | `test_entry_runtime_bootstrap.py::test_interactive_cancel_persists_active_execution_and_worker_handles_next_turn` | L4；仅当前进程，不回滚已发生副作用。 |
| L scheduled task | interactive ToolNode create → tasks.json → real pacemaker → shared queue → SCHEDULER worker/execution | `test_entry_runtime_bootstrap.py::test_scheduler_full_path_creates_due_task_and_runs_scheduler_execution` | Tier 1 / L4（基础 scheduler scope）；无 Scheduler 2.0。 |
| M 可纠正 Tool failure | structured failure → disposition `MODEL_CONTINUE` → second model response | `test_tool_failure_disposition.py::test_model_correctable_tool_failures_continue_without_whole_attempt_retry` | L4；timeout/transient 不触发 whole-graph retry。 |
| N terminal Tool failure | ToolNode → failure boundary → `AgentToolFailure` → durable TOOL `FAILED` | `test_tool_failure_disposition.py::test_terminal_structured_tool_failures_fail_attempt_without_second_model_call`、persistence/recovery regression | L4；不调用第二次模型、不可被 recovery 误标成功。 |
| O execution control | real turn → workspace-local list/show | `test_execution_cli.py::test_real_agent_execution_is_visible_through_execution_list_and_show` | L4；只展示安全 lifecycle metadata。 |

## Execution Lifecycle and Safety

真实生产路径覆盖 `SUCCEEDED`、`FAILED`、`INTERRUPTED`、`CANCELLED` 与 retry/recovery 创建的 `PENDING`（见 `tests/test_execution_e2e.py`、`tests/test_execution_resume.py`、`tests/test_execution_cli.py`）。

- retry eligibility 仅创建 durable `PENDING`；用户必须明确 `execution resume <id> --attempt N`。
- `recover` 只 assessment/reconciliation/planning：completed checkpoint 可标为成功，安全 `agent` checkpoint 可计划 N+1；ToolNode、unknown、missing checkpoint fail closed。
- resume 使用 predecessor 的 exact checkpoint lineage，CAS `PENDING → RUNNING` 后才继续；不会重放已执行 Tool。
- cancellation 是 terminal `CANCELLED`，不进入 RetryPolicy；worker shutdown 优先于 child expected-cancel marker，避免 shutdown hang。

## Tool Runtime and Permission Boundaries

MiClaw-owned 默认 builtins 使用：

```text
ToolResult → safe model content + structured outcome → classifier/disposition
```

`INVALID_INPUT`、permission deny、timeout/transient 等模型可处理的错误继续交给模型；`SAFETY_BLOCKED`、`INVALID_CONFIGURATION`、未知结构化错误终止当前 attempt，且 terminal set 被锁定为 `DO_NOT_RETRY`。Dynamic Skills 是现有 `StructuredTool` compatibility path，不被伪造为 structured success。

所有持久 scheduler mutation 先经过 `SCHEDULER` permission；interactive `ALLOW_SESSION` 在 scheduler request 中被 ContextVar token 替换为新的空 grant set，结束后恢复原 interactive grants。scheduled request 同样不获得 `/remember` intent；scheduler/model/Tool/MCP 返回文本也不能触发 `/cancel`。

`test_scheduler_requests_isolate_session_grants_and_restore_interactive_context` 在 input `/exit` 前轮询 `ExecutionStore` 的 terminal status counts，而不是依据模型 producer event 或 SQLite row 顺序猜测完成状态；它验证 interactive grant 恢复、scheduled fresh grants、失败清理和 durable completion。

MCP server identity、command、args、env、cwd 和生命周期仅来自启动时的 host config。模型只见已注册 Tool 的安全 name/description/schema，调用仍逐次接受 MCP permission。不会从 checkpoint/ExecutionStore 恢复 MCP config 或旧 session grants。

## Observability Privacy

实际 JSONL → monitor/logs/trace 的链路由 `tests/test_observability_redaction_integration.py::test_agent_jsonl_to_monitor_logs_and_trace_stays_redacted` 覆盖：tool args、tool output、AI content、token/API/password fields、嵌套数据和大 payload 均以安全摘要或 redaction 输出。`tests/test_memory_context_e2e.py` 证明 prompt/profile/project identity 不进入 JSONL/monitor/logs/trace；`tests/test_execution_e2e.py` 覆盖 provider exception 与 execution payload 不泄露。

checkpoint `state.sqlite3` 是 LangGraph durability state，可能依其语义保存必要 messages；它不是 observability log。`execution list/show` 只显示 allowlist lifecycle 字段，不显示 prompt、Tool args、Memory 或 checkpoint blobs。

## Known Non-Goals / Backlog

- 无 Scheduler 2.0：没有 scheduler execution history、lease、pause/resume、自动 retry/recovery。
- 无自动 PENDING 扫描/dispatcher、全局 stale sweep、或 Tool auto-retry。
- 无 HTTP/SSE/streamable HTTP MCP、OAuth、resources/prompts、registry、auto restart 或 hot reload。
- 无跨进程 cancellation、IPC 或 daemon；`/cancel` 不回滚完成的外部副作用。
- 无 exactly-once guarantee、full replay/time travel、advanced/semantic Memory、Skill install/version ecosystem。

## Done Criteria and Final Status

PR62 的 blocking 条件（default startup、Agent invocation、PROJECT write permission、scheduler full path 与 grant/Memory intent isolation、MCP cleanup、retry/recovery no-replay、cancellation durability、terminal Tool recovery、JSONL privacy、execution view privacy）均由上述对应等级的回归覆盖；表中 Focused evidence 不被表述为 Tier 1。

**Final status: Runtime Integration Closure COMPLETE，前提是本文件列出的 focused 与 full regression 保持通过。** 本结论不把上述非目标包装为已实现能力。
