# Phase 5：失败分类与 Retry Policy

PR48 为纯 execution domain 增加失败分类与 retry eligibility；它不执行 retry，也不改变 Agent、Tool、Provider 或 Scheduler runtime。

## 分层

```text
raw runtime/tool/provider failure
        ↓
ExecutionFailure(source, code)
        ↓
RetryPolicy(max_attempts)
        ↓
RetryEvaluation(decision, reason)
```

`ExecutionFailure` 只保存稳定 `source` 与 `code`，不保存异常、message、traceback、Tool 参数、路径、用户文本或 provider response。

## Failure 分类

来源为 `tool`、`provider`、`runtime`。当前 ToolResult 通过精确 `error_type` 映射分类：例如 `timeout`/`mcp_timeout` 为 `tool_timeout`，permission 的 `permission_denied`/`permission_required` 分别保留为安全分类，未知字符串统一为 `unknown_error`。

`mcp_spawn_error` 当前覆盖可执行文件、权限、参数与其他 spawn 配置问题，因此映射为 `invalid_configuration`；`mcp_connection_error` 同时表示未连接和未细分的连接/初始化失败，因此映射为 `tool_execution_error`。两者都不是可自动 retry 的证据：**语义混合或模糊的 failure 不等于 transient failure**。未来只有 MCP subsystem 新增经 review 的明确稳定类型（例如 timeout 或 transient transport error）后，才可映射为 retryable 分类。

当前 provider 未提供标准化异常接口；PR48 仅提供 provider 分类 vocabulary，不导入 SDK exception 或按异常文本猜测。

## Retry 规则

默认 `max_attempts=3`，表示总共最多三次 attempt，而不是三次额外 retry。

仅以下 code 在未耗尽 budget 时可 retry：

- `tool_timeout`
- `tool_transient_error`
- `provider_timeout`
- `provider_transient_error`

其余全部不可自动 retry。尤其 `permission_denied`、`permission_required`、`safety_blocked`、`execution_limit_exceeded` 与 `loop_guard_triggered` 永不自动 retry；重复执行不能替代新的用户授权或安全决策。`unknown_error` 同样 fail closed。

先判断 failure 是否 retryable，再判断 budget。因此不可 retry 的 failure 即使已到上限，也返回 `non_retryable_failure`；retryable failure 在 `current_attempt >= max_attempts` 时返回 `attempts_exhausted`。超过 policy 上限的 attempt 属于无效 runtime state，稳定拒绝而非静默当作耗尽。

## 与 PR47 的关系

PR47 terminal attempt 不能重新打开。PR49 在 `RetryDecision.RETRY` 时只使用相同 `execution_id` 规划 `attempt + 1` 的新 `PENDING` attempt；不是让 `FAILED → RUNNING`，也不会自动 replay graph。

## 当前非目标

- 不执行 retry 或创建下一 attempt
- 不做 Tool/Agent/Provider/Scheduler integration
- 不捕获 provider 原始异常
- 不做 backoff、sleep、jitter 或 retry_at
- 不做 checkpoint、restart recovery、loop guard、observability event
- 不扩展 `ExecutionState` schema，也不开放 retryable code 配置
