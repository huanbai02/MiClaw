# Phase 5 — Execution State Model

## 目标

PR47 定义纯 Execution State Model，为后续 recovery 或 task runtime 建立稳定语义；它不接入 Agent、Scheduler、filesystem、SQLite、permission、Memory 或 observability。

## Identity 关系

```text
execution_id
    ↓
attempt
    ↓
run_id
    ↓
step_id
```

* **Logical Execution**：由 runtime-controlled `execution_id` 标识，可跨未来多个 attempt 保持相同 identity。
* **Attempt**：一个 execution 的实际执行尝试，`attempt` 从 1 开始。
* **Run**：当前 attempt 对应既有 `TraceContext.run_id`。
* **Step**：继续使用既有 TraceContext 的 `step_id`。

`execution_id` 不替代 `run_id`，也不等同 Scheduler task identity；PR47 不引入 ScheduledTask 或修改 `tasks.json`。LangGraph checkpoint 与 ExecutionState 也仍是两条不同状态路径。

## 状态与转换

`ExecutionStatus` 只包含：`PENDING`、`RUNNING`、`SUCCEEDED`、`FAILED`、`INTERRUPTED`、`CANCELLED`。

```text
PENDING
  ├── RUNNING
  │     ├── SUCCEEDED
  │     ├── FAILED
  │     ├── INTERRUPTED
  │     └── CANCELLED
  │
  └── CANCELLED
```

`SUCCEEDED`、`FAILED`、`INTERRUPTED`、`CANCELLED` 都是 terminal attempt，不能继续转换。未来 retry 必须创建同一 `execution_id` 的新 attempt：`attempt N FAILED` → `attempt N+1 PENDING`，绝不能将 `FAILED` 重新打开为 `RUNNING`。

## 数据一致性

`ExecutionState` 是 frozen dataclass，字段为：

```text
execution_id, attempt, status, run_id, started_at, finished_at
```

`PENDING` 不绑定 run 或时间；`RUNNING` 必须有 non-empty `run_id` 和 timezone-aware `started_at`；成功、失败和中断必须另有 timezone-aware `finished_at`。取消允许发生在开始前或运行中，但总有 `finished_at`。若两者都存在，`finished_at >= started_at`。

所有 identity、attempt、status 与时间都严格校验：不进行字符串、整数或 enum coercion，拒绝 bool、naive datetime、`datetime` 子类和不一致的直接 dataclass 构造。时区回调或时间排序比较的异常同样收敛为 `invalid_execution_timestamp`。错误只使用稳定 code，如 `invalid_execution_id`、`invalid_attempt`、`invalid_run_id`、`invalid_execution_timestamp`、`invalid_execution_state` 与 `invalid_execution_transition`，不回显 execution 或 run identity。

## 当前非目标

PR47 不实现 retry policy/orchestration、failure taxonomy/code、持久化、restart recovery、pause/resume、Scheduler 2.0、Task Memory、Agent step limit、tool loop detection、execution observability event、cancellation CLI、distributed worker 或 replay。
