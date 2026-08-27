# Phase 5：Execution 持久化与定向重启恢复（PR51）

PR51 将 Execution attempt 生命周期元数据持久化到独立数据库，并仅在 checkpoint 所有权和下一节点安全性都可证明时恢复。它不是任务重放引擎。

## 当前运行时特征

- Agent graph 在 `miclaw.core.agent.graph.create_agent_app()` 中以 `workflow.compile(checkpointer=...)` 编译。
- CLI 使用 LangGraph 1.2.4 的 `AsyncSqliteSaver`，checkpoint 路径为 `<WORKSPACE_DIR>/state.sqlite3`。
- CLI 的 `thread_id` 当前固定为 `local_geek_master`，会跨多个对话 turn 复用，因此不能作为 execution identity。
- 既有 `TraceContext.run_id` 是 MiClaw trace run；PR51 不改变它。
- 每次 graph invocation 新建一个 opaque `checkpoint_run_id`，作为 RunnableConfig 的 `run_id`，同时写入 checkpoint `metadata.run_id`，用于 attempt ↔ checkpoint 归属关联。
- production graph 节点为 `agent` 与 `tools`；`tools` 是 LangGraph `ToolNode`，可能产生外部副作用。
- 恢复启用的调用显式传入 `durability="sync"`。这会在进入下一 graph step 前同步提交 checkpoint，但不构成 Tool exactly-once 保证。

当前 `AsyncSqliteSaver` 使用 LangGraph 默认 `JsonPlusSerializer`（`pickle_fallback=False`）；PR51 未修改 serializer、未增加 pickle fallback，也不把外部 checkpoint DB 当作可信 payload。

同步 durability 仅保证已完成的 graph step checkpoint 在继续前落盘；若进程位于 Tool side effect 与 ToolNode checkpoint 之间退出，可能仍存在未 checkpoint 的 pending writes。PR51 不解释或重放这类不确定状态，而是将 ToolNode/未知 next 视为不可恢复。

## 两份持久化数据的职责

| 存储 | 内容 | 权威性 |
| --- | --- | --- |
| `state.sqlite3` | LangGraph checkpoint、消息、下一节点与 graph progress | graph progress 的唯一权威 |
| `execution.sqlite3` | execution/attempt 状态、稳定 failure/retry 枚举、checkpoint opaque correlation | MiClaw lifecycle 的唯一权威 |

`execution.sqlite3` 的 `execution_attempts` 表包含：

- `(execution_id, attempt)` 主键；
- `status`、trace `run_id`、开始/结束时间；
- `failure_source/code` 与 `retry_decision/reason`；
- `checkpoint_thread_id/run_id/id`；
- 创建/更新时间。

它不保存用户输入、`AgentState`、messages、Tool args/result、Memory、模型输出、异常或 traceback。

## 持久化状态转换

运行顺序为：

```text
INSERT PENDING
  -> CAS PENDING -> RUNNING
  -> graph invocation
  -> CAS RUNNING -> terminal
```

SQLite 创建使用 `INSERT`，不使用 `INSERT OR REPLACE`。状态转换以 `(execution_id, attempt, expected_status)` CAS 更新；行数不是 1 时稳定报冲突。失败 attempt 与其下一 `PENDING` attempt 在同一 transaction 中写入，因此不会出现半写入 retry plan。

PENDING/RUNNING 持久化失败时不会调用 graph。图已经结束但 terminal metadata 持久化失败时，不会重跑图；遗留的 RUNNING attempt 可由 terminal checkpoint 后续 reconciliation。

## 定向恢复

恢复 API 是针对已知 `(execution_id, attempt)` 的 targeted operation；没有启动时全局 sweep，避免误伤仍在运行的其他进程。

恢复时按精确 `thread_id + metadata.run_id` 枚举 checkpoint history，取该 run lineage 最新 checkpoint。不会使用同一 thread 的 latest checkpoint，更不会依据时间猜测所有权。缺失、归属不匹配或 malformed checkpoint 一律 `DO_NOT_RESUME`。

恢复决策：

- `MARK_SUCCEEDED / graph_already_complete`：RUNNING metadata 对应 checkpoint 的 `next == ()`。只将 metadata 变为 SUCCEEDED，不调用 graph。
- `RESUME_FROM_CHECKPOINT / safe_checkpoint_available`：所有 `snapshot.next` 节点都在当前明确 allowlist `{agent}` 中。旧 RUNNING attempt 先变为 INTERRUPTED，再创建同一 execution_id 的新 PENDING attempt。
- `DO_NOT_RESUME`：没有归属 checkpoint、下一节点为 `tools` 或任何未知节点、状态不允许恢复，均停止自动恢复。

checkpoint continuation 必须使用同一 `thread_id` 加精确 `checkpoint_id`，并以新的 `checkpoint_run_id` 运行新的 attempt。不会使用 `thread_id` 的 latest checkpoint，也不会补一条 HumanMessage 伪造 resume。

`tools` / ToolNode 永远不在恢复安全 allowlist 中：崩溃时无法证明 Tool 未执行，故不能自动重放。checkpoint 缺失也不会从 START 重新执行；不会选择更早 checkpoint 回放。

## 已知边界

- `durability="sync"` 不能保证 exactly-once：Tool 副作用后、ToolNode checkpoint 前崩溃仍是不可判定状态，故 fail closed。
- 安全 resume 创建新 attempt；绝不将 `INTERRUPTED` 或 `FAILED` 旧 attempt 重新打开。
- PR51 不实现全局 stale-run 扫描、Scheduler 2.0、分布式 lease、Tool compensation、任意 checkpoint replay、持久化 loop fingerprint 或 PostgreSQL。
- LangGraph checkpoint 保存 graph state；PR51 不复制它，也不实现 full task replay。

Phase 5 后续将以 PR52 统一 execution observability 与 E2E closure；PR51 本身不扩展 execution event schema。

## Durable attempt 不变量（PR51 Follow-up）

`ExecutionStore` 不只做 expected-status CAS。它复用 PR47 的固定转换矩阵：仅允许 `PENDING→RUNNING/CANCELLED` 与 `RUNNING→SUCCEEDED/FAILED/INTERRUPTED/CANCELLED`。因此已 durable 的 terminal attempt 不能通过 Store API reopen 或改写为另一 terminal status。

两条原子 plan-next 路径（failure 与 recovery interrupt）还要求下一记录严格为同一 `execution_id` 的 `attempt + 1` PENDING state。跨 execution、同号、跳号或无效 PENDING record 会在任何 UPDATE/INSERT 前被拒绝，当前 attempt 保持原状。
