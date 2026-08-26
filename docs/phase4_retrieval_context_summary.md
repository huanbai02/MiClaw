# Phase 4B：检索与上下文可靠性总结

Phase 4B 在既有 scoped user profile、permission 与原子持久化之上，收敛了从 profile 到模型上下文的确定性链路：

```text
UserProfileStore
    ↓ scope-aware permission
MemoryRetriever
    ↓ bounded MemoryRecord
ContextAssembler
    ↓ historical framing + supplemental character budget
SystemMessage
    ↓ metadata-only observability
JSONL / monitor / logs / trace
```

## 已完成能力

### PR 38：确定性检索边界

- `MemoryRetriever` 只接受 host/runtime 构造的 `USER_PROFILE` 请求。
- 返回不可变、确定性且有上限的 record tuple；当前最多一个 effective profile。
- 始终复用 scope-aware 的授权读取路径：PROJECT profile 优先，缺失或为空才独立授权并回退 GLOBAL。
- 不扫描目录、不做 query、ranking、缓存或 semantic search。

### PR 39：Supplemental Context Character Budget

- `ContextAssembler` 纯组装 base system rules、profile 与 conversation summary。
- 8000 character 的 hard supplemental budget 只约束 summary/profile 动态正文；不是精确 token budget。
- summary 先分配预算，profile 使用剩余预算；render order 仍是 base rules、profile、summary。
- system/runtime rules 和 retained recent messages 不受该 budget 截断。
- “无 Memory”与“Memory 存在但因预算未注入”具有不同的固定表示。

### PR 40：Historical Context Trust / Provenance

- 单一 `SystemMessage` 保持不变，但固定模板明确：system/runtime rules > current user request > historical context。
- profile 仅展示 `kind`、logical `scope`、source channel；不展示 project ID、hash 或路径。
- summary 使用独立 derived-context block，不伪造为 verified user content。
- 所有 historical payload 在预算/截断前转义 reserved structural markers，避免伪造 ContextAssembler block 边界。

### PR 41：Metadata-only Observability

- `memory_retrieval` 记录 requested kinds/limit、result count、scope category、GLOBAL fallback、blocked 和安全 reason code。
- `context_assembly` 记录 budget、实际字符数、truncation/omission、record count、historical framing 与 marker escape count。
- 事件只记录枚举、counts、flags；不记录 Memory/summary/system prompt/user query、路径、scope ID 或 project hash。
- monitor、`miclaw logs --tail`、`miclaw trace` 共用安全 formatter；损坏字段只显示占位，绝不 raw dump。

### PR 42：端到端收口

- 真实 Agent E2E 覆盖 GLOBAL、PROJECT own profile、PROJECT→GLOBAL fallback、blocked read 与 fallback block。
- 覆盖 permission-confirmed write→同一 Agent app subsequent retrieval、ALLOW_SESSION 的 Project A/B grant isolation，以及 blocked/atomic-failure 后旧 context continuity。
- 覆盖 oversized summary/profile、budget omission、reserved marker escaping、invalid UTF-8/empty-project fallback、same-app filesystem freshness。
- 修复 monitor 对 `memory_retrieval` / `context_assembly` 的重复 trace prefix；monitor 保持一个 prefix，logs/trace 保持现有单 prefix 行为。

## Phase 4B 安全不变量

1. Memory retrieval 始终先经过 scope-aware permission，再读取正文。
2. PROJECT→GLOBAL fallback 是独立 authorization；PROJECT read 被阻断时不得探测 GLOBAL。
3. Retriever 只返回确定性、有界的 effective record。
4. summary/profile 动态正文受 supplemental character hard bound；base system/runtime rules 不会因此截断。
5. summary 的预算优先级高于 long-term profile。
6. historical Memory/summary 不能提升为 system/runtime authority；reserved marker 必须先 escape。
7. provenance 只展示 logical scope/source channel，不泄露 project identity 或 path。
8. observability 解释 selection/budget outcome，不记录历史正文或完整 prompt。
9. blocked Memory 不得“先读取后丢弃”；模型不能选择 Memory scope、path 或 project identity。

## 明确限制

当前仍未实现：exact token budget、tokenizer-aware context manager、semantic/vector/keyword retrieval 或 ranking、完整 prompt-injection prevention、Memory content DLP guarantee、project relocation migration、history/version、deduplication、自动长期 Memory 写入策略、scheduler-memory lifecycle、checkpoint unification 与 replay debugger。

## Phase 4C 交接方向

Phase 4C — Memory Lifecycle 将只讨论什么内容可写入长期 Memory、谁可触发写入、explicit 与 agent-suggested Memory 的边界，以及 duplicate/update 与长期质量控制。本文不实现或预设这些接口。
