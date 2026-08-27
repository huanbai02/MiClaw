# Phase 5：Agent Loop Guards & Stop Conditions

PR50 为单个 MiClaw execution attempt 增加两层有限执行 envelope。

## 两层保护

1. **LangGraph hard bound**：顶层 production invocation 显式合并 `recursion_limit=25`。这是 graph superstep 限制；触发 `GraphRecursionError` 时，MiClaw 归类为 `runtime/execution_limit_exceeded`。
2. **MiClaw semantic guard**：在 Agent 产出 `AIMessage.tool_calls` 后、ToolNode 执行前，检测连续相同的 semantic Tool call。触发时归类为 `runtime/loop_guard_triggered`。

`recursion_limit` 不是 `TraceContext.step_id`：前者由 LangGraph 管理 graph 执行边界，后者是 MiClaw observability identity。

两种 failure 均不可 automatic retry：attempt 结束为 `FAILED`，没有 next attempt。

## Repeated Tool call 语义

默认阈值为 3：前两次连续相同调用可以进入 ToolNode；第 3 次在执行副作用**之前**被阻断。

semantic identity 为 Agent-facing Tool identity 加 canonical JSON args：

- 忽略 Tool call id；
- dict key order 不影响 identity；
- MCP 使用当前 Agent-facing qualified Tool name，因此不同 server 的 Tool 不会碰撞；
- 仅在当前 attempt 内以 SHA-256 digest 比较，不保存原始 args、Tool name/args 组合或 digest 到 JSONL、monitor、trace、Memory、checkpoint 或 `ExecutionState`。

canonical args 使用稳定 JSON 编码且有 65,536 bytes 上限。非 JSON、NaN/Infinity、custom object 或超限参数视为 untrackable：不做 repr、truncate 或 hash，并重置连续链；LangGraph hard bound 仍提供最终限制。

一条 AIMessage 的多个 Tool calls 会先在临时 state 上完整预检。只要任一调用会触发 guard，整个 batch 都不会进入 ToolNode，也不会提交部分观察结果。

## 生命周期与限制

guard state 只存在 execution wrapper 建立的 attempt-local ContextVar runtime holder 中，完成、失败、取消或 LangGraph control-flow 后都会 reset；不跨 run、attempt、workspace 或 restart 继承。PR51 checkpoint/recovery 前不会恢复此状态。

当前不实现 generic consecutive Tool failure guard。虽然 LangChain `ToolMessage` 有 `status` 字段，但 MiClaw 尚未证明所有 local、MCP 与 Memory Tool wrapper 都一致提供可信 structured outcome；PR50 不解析 ToolMessage 内容猜测错误。

## 非目标

- whole-graph 或 Tool automatic retry
- LangGraph/ LangChain retry middleware
- guard persistence、checkpoint/recovery、Scheduler retry
- semantic similarity、LLM loop judge、Tool args 内容解析
- token/wall-clock budget、pause/resume
