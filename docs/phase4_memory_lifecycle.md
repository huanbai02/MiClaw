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

完整 overwrite 语义保持：即使用户明确要求“记住 X”，模型仍需给出完整新 profile 后覆盖写入；PR 43 不加入 append、merge、dedup、history、revision 或 autonomous candidate queue。

## 空内容限制

空字符串是合法的显式 clear 请求，但仍需要 policy eligible 与 final permission ALLOW。对 PROJECT profile，现有 routing 将 empty 文件视为 missing，后续读取会回退 GLOBAL profile。因此 empty PROJECT write **不是** 不继承 GLOBAL 的 tombstone；project-specific suppression/global opt-out 尚未实现。

## 非目标与后续方向

本 PR 未实现 autonomous memory、scheduler memory policy、candidate queue、content moderation、LLM write judge、importance scoring、dedup/merge/version/history、tombstone 或 compaction。

后续生命周期工作只需讨论什么内容可写、谁能触发写入、explicit 与 agent-suggested 的产品流程，以及 duplicate/update 质量控制；不应把这些能力倒灌为模型自我认证字段。
