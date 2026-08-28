# Phase 5：Execution Runtime Integration & Retry Attempt Planning

PR49 把 PR47 的 `ExecutionState` 与 PR48 的 failure/retry policy 接入一次顶层 Agent graph invocation。

```text
PENDING → RUNNING → graph invocation exactly once → SUCCEEDED
                                      │
                                      └─ ordinary failure → FAILED
                                                              ↓
                                                       RetryEvaluation
                                                              ↓
                                              optional next PENDING attempt
```

## Identity

`execution_id` 表示 logical execution；`attempt` 表示其一次实际尝试；`run_id` 继续来自现有 `TraceContext`；`step_id` 继续由 TraceContext 管理。只有 `execution_id is None` 表示 runtime 应生成 UUID4；任何其他显式值都原样交给 PR47 validator，不会因 falsey 而替换。PR49 不替代 thread_id、run_id，也不引入 Scheduler task identity。

## 单次调用与失败边界

`agent.execution.run_agent_execution()` 在开始前创建 `PENDING`，使用当前 TraceContext 的 `run_id` 转为 `RUNNING`，并且只 await 一次 Agent graph invocation。

实际模型调用处只识别 `TimeoutError` 为 `provider_timeout`；其他 provider 异常为 `provider_error`，且不保存原始异常文本。模型调用边界外的普通 graph/orchestration 异常为 `runtime_error`。`asyncio.CancelledError` 与 LangGraph `GraphBubbleUp` 控制流原样传播，不进入 retry policy。

## Retry 只做规划

失败 attempt 会先结束为 `FAILED`，再执行 PR48 policy。若 decision 为 `RETRY`，仅创建同一 `execution_id`、`attempt + 1`、没有 run/timestamp 的 `PENDING` state；**不会自动再次调用 graph**。

ToolResult 是 graph 内部返回值。即使其 `error_type` 看似 timeout，PR49 也不重新执行 Tool；具有副作用的 file、shell、MCP、Memory 操作没有 automatic retry。

LangGraph native `RetryPolicy` 仍是 node/task-level primitive，不等同于 MiClaw logical execution attempt，PR49 不使用它实现 logical retry。未来只有在明确 side-effect-safe 的节点上，才能单独评估其适配。

## 后续路线

- **PR49**：Execution Runtime Integration & Retry Attempt Planning
- **PR50**：Loop Guards & Stop Conditions
- **PR51**：Checkpoint / Restart Recovery & Safe Attempt Resume
- **PR52**：Execution E2E Hardening（Phase 5 完成）

PR51 已建立独立 execution metadata、checkpoint correlation 与定向安全恢复；它仍不 replay whole graph。

## 当前非目标

- whole-graph 或 Tool automatic retry
- side-effect idempotency、backoff、jitter
- LangGraph/ LangChain retry middleware
- Scheduler retry
- loop guard、cancellation CLI、distributed execution
- execution observability event
