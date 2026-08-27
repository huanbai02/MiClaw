# Phase 5：Task Execution & Recovery ✅

Phase 5 完成 MiClaw Execution Runtime 的第一版基础：每个顶层 Agent graph 调用都有明确 attempt 生命周期、受限 failure/retry 决策、循环保护、独立持久化元数据与定向安全恢复。详细设计仍保留在 [状态模型](phase5_execution_state_model.md)、[失败与 retry](phase5_failure_retry_policy.md)、[runtime 接入](phase5_execution_runtime_integration.md)、[loop guard](phase5_agent_loop_guards.md) 与 [恢复](phase5_execution_recovery.md)。

## 概念与状态

`execution_id → attempt → TraceContext.run_id → step_id`：execution 是逻辑身份，attempt 是一次实际运行，trace run/step 继续服务既有 observability。attempt 状态机为：

```text
PENDING → RUNNING → SUCCEEDED | FAILED | INTERRUPTED | CANCELLED
PENDING → CANCELLED
```

terminal attempt 不可 reopen；retry 或恢复只能创建同一 execution 的 `attempt + 1` PENDING state。

## Failure、retry 与 no replay

Failure 只保存稳定 source/code。仅明确 timeout/transient code 在 budget 内有 retry eligibility；permission、安全、unknown、loop 与 execution-limit failure 均 fail closed。`RetryDecision.RETRY` 只持久化下一 attempt，**不会重新调用当前 graph，也不会自动重试 Tool**。

## 有限 execution envelope

每次 production graph 调用显式设置 LangGraph `recursion_limit=25`。此外，attempt-local semantic guard 在 ToolNode 前比较 canonical tool identity 与参数 digest；连续第三次相同调用在副作用前阻断。Tool call id 不参与 identity；digest、参数与内容不会写入 event、checkpoint 或 execution store。

## 持久化与 checkpoint 恢复

- `workspace/execution.sqlite3` 只保存 attempt metadata、稳定 failure/retry 枚举与 checkpoint correlation；不保存 prompt、messages、Tool args/result、AgentState 或 checkpoint blob。
- `workspace/state.sqlite3` 继续由 LangGraph `AsyncSqliteSaver` 独占 graph progress。
- 每个 graph invocation 生成独立 `checkpoint_run_id`，以 RunnableConfig `run_id` 和 checkpoint `metadata.run_id` 精确关联；复用 thread_id 不可作为 ownership 证据。
- 调用使用 `durability="sync"`，但这不构成 exactly-once Tool 执行保证。
- 仅 checkpoint `next` 全部属于 `{agent}` 的 lineage 可安全恢复；`tools`/未知节点、缺失或模糊 checkpoint 一律不恢复，也不会从 START 回放。
- 完整 graph checkpoint 可将遗留 RUNNING metadata reconciliation 为 SUCCEEDED；安全恢复会先 terminalize 旧 attempt，再创建新 attempt。

## 最小 execution observability

现有 JSONL pipeline 额外记录三类 metadata-only event：

| Event | 字段 |
| --- | --- |
| `execution_started` | `attempt`, `status` |
| `execution_finished` | `attempt`, `status`, 可选 `failure_code`, `retry_decision` |
| `execution_recovery` | `attempt`, `recovery_decision`, `recovery_reason` |

执行 lifecycle event 显式使用该 attempt 的 authoritative `TraceContext`；显式 context 优先于 ambient logger ContextVar，因而 `ExecutionState.run_id`、JSONL `run_id` 与 `step_id` 同源。targeted recovery 可显式传入自己的 `TraceContext`，未传入时才沿用 ambient context。事件不记录 execution_id、thread/checkpoint identity、prompt、output、异常文本、Tool 参数、Memory 或 fingerprint。monitor 对新增 event 使用安全 unknown-event fallback，无需新 UI。

targeted recovery 对同一旧 attempt 重复调用不会持续创建 `N+1/N+2/N+3`：首次 safe assessment 最多创建一个下一 attempt，旧 attempt 随即保持 terminal。若 graph 已完成但 `SUCCEEDED` metadata 落库失败，重启后只在 exact-owned completed checkpoint 上 reconciliation 为 `SUCCEEDED`，不会重新调用 graph 或 Tool；这不是全局 exactly-once 保证。

## 最终不变量

1. 每个启用持久化的 attempt 都有明确 durable state。
2. terminal attempt 不可 reopen。
3. retry 分类、有上限且 fail closed。
4. retry eligibility 不会 replay 当前 graph。
5. permission/safety/unknown/loop failure 不能变成 automatic retry。
6. Agent graph 有明确 recursion envelope。
7. 重复 semantic Tool 调用在重复副作用前阻断。
8. checkpoint ownership 必须精确匹配。
9. ToolNode checkpoint 永不自动恢复。
10. 无 checkpoint 不从 START replay。
11. recovery 创建新 attempt，旧 interrupted attempt 保持 terminal。
12. execution metadata 不复制 graph/prompt/tool payload。

## Core Runtime v1 Foundation

以下基础已完成：安全与受控执行、JSONL observability baseline、Skill foundation、MCP core、Memory/Context core、Execution/Recovery core。

仍明确延期：Skill lifecycle ecosystem、Scheduler 2.0、advanced Memory、MCP HTTP/OAuth/resources/prompts、full replay、distributed execution、advanced runtime metrics 与 mypy。Phase 5 不表示 Scheduler 2.0、exactly-once 或完整任务回放已实现。
