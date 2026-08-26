# Core Domain Package Architecture

## 1. Phase 4.5 背景

Phase 4.5 将原先扁平的 `miclaw/core/` 按当前稳定领域重新归位。此变更只调整模块位置和 import 路径：不改变 Agent、Memory、MCP、Skill、Scheduler、Permission 或 observability 的运行时语义。

## 2. Core package tree

```text
miclaw/core/
├── agent/           # graph、AgentState、context assembly
├── runtime/         # config、workspace、bus
├── security/        # 通用 permission、confirmation、session grant
├── observability/   # JSONL logger、redaction、trace
├── memory/          # models、profile store、lifecycle、permission、retrieval
├── mcp/             # adapter、stdio client、permission、Agent-facing tools
├── skills/          # Skill discovery、validation、lazy loading
├── scheduler/       # heartbeat / scheduled triggering
├── llm/             # provider construction
└── tools/           # base、builtin、result、sandbox
```

各 package 的 `__init__.py` 只保留说明性 docstring，不承担 facade 或 barrel export。调用方显式导入 canonical module，例如 `miclaw.core.memory.retrieval`，避免隐藏真正的依赖关系。

## 3. Ownership 与依赖方向

`runtime` 不依赖 Agent、Memory、MCP 或 Scheduler；`security` 和 `observability` 不依赖 Agent orchestration；`memory`、`mcp`、`skills`、`llm` 不依赖 Agent graph。`agent` 可编排这些领域，`scheduler` 可触发 Agent，但 Agent 不反向依赖 Scheduler。

概念关系如下：

```text
entry
  ↓
scheduler
  ↓
agent ──→ memory / mcp / skills / tools / llm

shared foundations: runtime / security / observability
```

这是当前代码的领域方向，不是额外引入的严格层级检查器。迁移过程中不为消除所有历史耦合而进行业务逻辑重写。

## 4. 为什么不引入大杂烩模块

本次没有添加 `utils.py`、`common.py`、`helpers.py`、`services.py` 等泛化容器。每个已移动模块保留既有职责与实现，只有模块归属改变；若未来出现真正共享且无状态的基础类型，应在其明确领域内单独评审。

## 5. 模块级状态

Workspace ContextVar、TraceContext、permission session grants、Memory write intent 和 logger 等状态只保留在其 canonical module 中。旧 flat implementation 已移除，不设置永久 compatibility shim，以避免出现两份 ContextVar、enum 或 logger state 的 identity split。

## 6. Phase 5 接入方向

Phase 5 如需扩展 Task Runtime，可在真实职责稳定后新增 `core/task/` 或 `core/execution/`。本次不会提前创建空 package，也不会将 future scheduler、task 或 execution 设计混入现有领域。
