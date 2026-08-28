# Phase 4C：Explicit Memory Write Policy

PR 43 为当前唯一可持久化的 `USER_PROFILE` 写入增加两道独立 gate：

```text
host/runtime explicit intent + content shape preflight
    ↓ eligible
resolve runtime target/scope
    ↓
MemoryWriteRequest
    ↓
MemoryWritePolicy eligibility
    ↓
existing MEMORY_WRITE permission
    ↓ final ALLOW
UserProfileStore atomic write
```

## 当前写入政策

唯一支持的 lifecycle intent 是 `EXPLICIT_USER_REQUEST`。它必须由 host/runtime 通过当前 turn 的 `ContextVar` 显式绑定；ContextVar 只保存稳定 enum，不保存用户原话。开始 target/workspace resolution 前，preflight 会先校验该 intent 与 `new_content` 的精确字符串类型；没有/异常 intent 或 malformed content 时，policy 默认拒绝，且不会解析 target、构造 permission confirmation、查询 session grant、创建目录或 temporary file。空字符串仍是合法的 explicit clear 内容。

preflight 通过后才解析既有 OFFICE/PROJECT runtime target，并构造完整 `MemoryWriteRequest`。该 request 包含 kind、resolved runtime scope、write source channel、content 和 intent；完整 policy 随后仍校验 kind/scope/source 等 target-dependent 不变量。source channel 当前为 `AGENT_TOOL`，只说明调用经过 Agent Tool，不证明 profile 正文由用户本人创作或内容真实。模型参数不能选择 scope、project ID、path 或 memory ID。

`MemoryWritePolicy` 只回答“是否有资格申请持久化副作用”：仅 `USER_PROFILE`、合法 GLOBAL/PROJECT scope、精确字符串 content（包括空字符串）、`AGENT_TOOL` 和 `EXPLICIT_USER_REQUEST` 可 eligible。它不做内容质量、相关性、敏感类别、事实性或 LLM judge。

## Policy 与 Permission 的分工

`eligible write != authorized write`：

1. lifecycle policy eligible 后，仍必须经过既有 `MEMORY_WRITE` MEDIUM-risk `ASK` permission；
2. 无 confirmation handler、DENY 或无效确认仍不会写入；
3. `ALLOW_ONCE` 与 `ALLOW_SESSION` 继续只属于 permission；每次写入仍先重新评估 lifecycle policy；
4. 已有 session grant 不能让缺少 explicit intent 的后续 Agent/model 写入绕过 policy；
5. 只有两道 gate 均通过，才会进入同目录 temporary file + atomic `os.replace()`。

`UserProfileStore` 仍是 policy/permission-agnostic persistence primitive；直接 Store API 的内部测试或迁移代码不由本政策拦截。正式 runtime Tool 链路才固定使用 policy → permission → Store。

## Tool 与运行时边界

`save_user_profile` 仍只有 `new_content` 参数。它的 schema 中没有 `intent`、`explicit`、`user_requested`、scope、project ID 或 path，因此模型无法自行证明 eligibility。普通 Agent 推断、Tool output、scheduler/internal context 都默认没有 explicit write intent，不会自动持久化。

## PR 44：精确更新与去重

在 lifecycle policy 通过并一次性绑定 runtime target 后，写入链路增加 exact-target comparison：

```text
explicit lifecycle policy
    ↓
resolved concrete target
    ↓
MEMORY_READ authorization
    ↓
current target bytes == new_content.encode("utf-8")?
    ├─ yes → NOOP_EXACT_MATCH（不申请 MEMORY_WRITE）
    └─ no / read blocked / comparison unavailable
          ↓
       MEMORY_WRITE permission
          ↓ final ALLOW
       atomic complete overwrite
```

比较只读取当前 concrete target，不调用 PROJECT → GLOBAL effective-profile fallback。它比较原始 persisted bytes 与 `new_content` 的 UTF-8 bytes；不会 `.strip()`、规范化空白/Markdown/大小写/Unicode，也不会做 fuzzy、semantic 或 LLM 比较。因此 `"Python\n"` 与 `"Python"` 是不同更新，legacy invalid UTF-8 bytes 也不会因 `errors="ignore"` 的逻辑读取结果而被误判为重复。

当前 disposition 为：

* `NOOP_EXACT_MATCH`：目标文件存在且 bytes 完全相同；仍要求可信 explicit intent 和 exact-target `MEMORY_READ`，但不触发 `MEMORY_WRITE` confirmation、临时文件或 replace。
* `WRITE_REPLACE`：非空且与 concrete target 不同（包括 target 缺失）。
* `WRITE_CLEAR`：空字符串且 target 不是 exact empty file（包括 target 缺失）；仍通过 atomic overwrite 创建或清空文件，不会删除文件。

`MEMORY_READ` 被阻断或比较不可用时，系统不能证明重复，因而保守进入既有 `MEMORY_WRITE` permission；它绝不将比较失败视为 no-op。Tool 对 no-op 与真实写入保持相同成功文案，避免把 `save_user_profile` 变成模型可用的 profile equality oracle。

PROJECT 去重严格 target-local：PROJECT profile 缺失、但 GLOBAL fallback 内容恰好相同，仍不是重复；在获得 `MEMORY_WRITE` ALLOW 后会 materialize PROJECT profile。反之，已存在且 bytes 相同的 PROJECT profile 才是 no-op。每次操作继续使用同一个 resolved target，既有 session grant 只能复用 changed write 的 permission，不能跳过 lifecycle 或 exact comparison。

完整 overwrite 语义保持：即使用户明确要求“记住 X”，模型仍需给出完整新 profile 后覆盖写入；本阶段不加入 append、merge、revision、CAS 或 autonomous candidate queue。

## 空内容限制

空字符串是合法的显式 clear 请求，但仍需要 policy eligible 与 final permission ALLOW。对 PROJECT profile，现有 routing 将 empty 文件视为 missing，后续读取会回退 GLOBAL profile。因此 empty PROJECT write **不是** 不继承 GLOBAL 的 tombstone；project-specific suppression/global opt-out 尚未实现。

## 非目标与后续方向

本阶段未实现 autonomous memory、scheduler memory policy、candidate queue、content moderation、LLM write judge、importance scoring、fuzzy/semantic dedup、merge/version/history、CAS/locking、tombstone 或 compaction。并发更新仍是 last successfully completed atomic replace wins，可能产生 lost update；PR 44 不解决该问题。

后续生命周期工作只需讨论什么内容可写、谁能触发写入、explicit 与 agent-suggested 的产品流程，以及 duplicate/update 质量控制；不应把这些能力倒灌为模型自我认证字段。
