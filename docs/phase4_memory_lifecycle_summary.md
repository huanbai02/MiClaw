# Phase 4C — Memory Lifecycle Completion Summary

Phase 4C 在 Phase 4A 的 scoped、permission-aware profile persistence，以及 Phase 4B 的 retrieval/context boundary 之上，收敛长期 User Profile 的写入生命周期。当前正式持久化对象仍只有 `USER_PROFILE`；本阶段没有把它扩展成通用 Memory database。

## 已完成能力

### PR 43：Explicit Memory Write Policy

`save_user_profile(new_content)` 的持久化写入必须先通过两道独立边界：

1. host/runtime 提供的可信 `EXPLICIT_USER_REQUEST` lifecycle intent；
2. 既有 `MEMORY_WRITE` permission 的最终 `ALLOW`。

缺失 intent、异常 intent 或非精确字符串内容会在 target/workspace resolution 前 fail closed。模型 Tool schema 仍只有 `new_content`，不能通过参数自证 intent、选择 scope、project identity 或文件路径。Store 仍是 policy/permission-agnostic 的 persistence primitive。

### PR 44：Exact Deduplication / Update Disposition

通过 lifecycle policy 后，runtime 对一次绑定的 concrete target 先执行 `MEMORY_READ` authorization，再比较当前 persisted bytes 与 `new_content.encode("utf-8")`：

```text
exact match        → NOOP_EXACT_MATCH
changed non-empty  → WRITE_REPLACE
changed empty      → WRITE_CLEAR
```

NOOP 不申请 `MEMORY_WRITE`、不确认、不创建 temporary file、不 replace；但仍需要可信 lifecycle intent，避免把 Tool 变成内容 equality oracle。比较严格使用 bytes，不做空白、Markdown、大小写或 Unicode normalization。PROJECT 比较只看 PROJECT concrete target；GLOBAL fallback 只服务读取，不参与 dedup。

### PR 45：Lifecycle E2E Hardening

端到端回归覆盖真实：

```text
explicit intent
→ lifecycle preflight/policy
→ scoped target
→ exact-target read + dedup
→ write permission
→ atomic persistence
→ retrieval
→ bounded、historical trust-framed SystemMessage
→ metadata-only JSONL / monitor / logs / trace
```

覆盖 explicit write 的后续 context 可见性、无 intent 的零 lifecycle work、`ALLOW_SESSION` 不能绕过 lifecycle、exact no-op、PROJECT materialization 与 clear 后 GLOBAL fallback，以及 atomic replace 失败后的旧 context continuity。Observability 不记录 profile 正文、摘要、new content、用户 query 或 project path。

## 冻结的安全不变量

1. 持久化写入需要可信 explicit lifecycle intent。
2. 模型不能经 Tool args 自证 intent 或控制 scope/path。
3. 不 eligible 的请求在 target resolution 前停止。
4. permission grant 不能绕过 lifecycle eligibility。
5. scope 完全由 runtime active workspace 决定。
6. exact dedup 只比较已授权 concrete target 的原始 bytes。
7. PROJECT → GLOBAL fallback 不参与 dedup。
8. NOOP 跳过 `MEMORY_WRITE` 的确认与持久化副作用。
9. changed write 仍必须获得最终 `MEMORY_WRITE` `ALLOW`。
10. 成功写入仍是同目录 temporary file + atomic replace 的完整覆盖。
11. NOOP 与真实成功写入对模型返回相同成功文案。
12. `UserProfileStore` 不依赖 lifecycle intent、permission 或 Agent runtime。

## 当前限制

尚未实现 fuzzy/semantic dedup、merge、字段级更新、revision/version、CAS、lost-update prevention、tombstone/global inheritance suppression、autonomous Memory candidate、importance scoring、LLM quality judge、compaction/consolidation、scheduler-specific Memory policy、semantic/vector Memory 或自动画像。

并发写入仍遵循“每次单独原子替换、最后成功完成的 writer 获胜”；因此仍可能发生 lost update。PROJECT empty profile 仍被读取路径视为 missing，随后回退 GLOBAL，不是“不继承 GLOBAL”的 tombstone。

## Completion

PR 45 完成后，Phase 4C 的显式长期画像写入生命周期已有端到端基线。后续 Memory 能力应单独评审，不应在本阶段顺带开启 compaction、scheduler integration 或新的自动写入机制。
