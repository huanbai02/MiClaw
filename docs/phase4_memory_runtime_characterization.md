# MiClaw Phase 4：Memory Runtime Characterization

本文记录 PR 32 建立、由 PR 33–39 演进后的 Memory runtime 行为。PR 33 将固定 profile 的 filesystem IO 收敛到 `UserProfileStore`；PR 34 补充最小结构化语义；PR 36 为 profile 启用明确的 GLOBAL / PROJECT persistence routing；PR 38 增加确定性、permission-aware retrieval boundary；PR 39 增加有界的 context assembly。本文仍不定义通用 `MemoryStore`、semantic retrieval 或完整 context API。

## 1. 当前 Memory 概览

当前存在两条不同的“记忆”路径，不能混为一个 subsystem：

1. **显式用户画像**：`workspace/memory/user_profile.md`。这是当前唯一正式的 Markdown 持久化 Memory 对象；Agent 每次进入 `agent_node` 时读取它，并直接放入 system prompt。
2. **LangGraph conversation state/checkpoint**：`workspace/state.sqlite3`。`entry/main.py` 用 `AsyncSqliteSaver` 打开它，保存 `AgentState.messages` 与 `summary` 等 graph checkpoint。它是对话状态路径，不是 `memory/` 目录的 profile API。

下文中“显式 Memory”默认指第一条路径；第二条会在 context 与 persistence 章节单独说明。

## 2. 文件布局

### 根路径与自动创建

`miclaw/core/config.py` 在 import 时解析：

```text
WORKSPACE_DIR = $MICLAW_WORKSPACE 或 <project>/workspace
MEMORY_DIR    = <WORKSPACE_DIR>/memory
GLOBAL profile = <MEMORY_DIR>/user_profile.md
PROJECT profile = <MEMORY_DIR>/projects/<opaque-project-id>/user_profile.md
```

Config import 会通过 `os.makedirs(..., exist_ok=True)` 创建 `WORKSPACE_DIR`、`memory/`、`office/`、`office/skills/` 等目录；它不会创建 profile 文件或 `projects/` 子目录。当前没有 daily memory、agent-specific memory、task memory 或 metadata index。

### 格式与范围

每个 `user_profile.md` 都是任意 UTF-8 Markdown 文本，没有 front matter、record schema、version、分段约定或文件大小限制。Agent 每次只选择一个 effective profile：OFFICE 为 GLOBAL；PROJECT 为非空 PROJECT profile，缺失时只读回退 GLOBAL。其他 `.md` 文件不会自动进入 prompt。

正常运行中 workspace 由环境变量在 import 前决定；当前没有 runtime workspace switching API。`agent.py` 与 `builtins.py` 仍在 import 时取得 `MEMORY_DIR`，因此若进程内显式 reload `miclaw.core.config`，这些已导入模块不会自动切换到新的 Memory root。这是当前 module-level config binding 的限制；`UserProfileStore` 本身不缓存内容。

## 3. Read Path

显式 Memory 的正式 read entry point 不是一个独立 Tool/API，而是 `miclaw.core.agent.create_agent_app()` 内部的 `agent_node`，它通过 `MemoryRetriever` 读取：

```text
effective GLOBAL/PROJECT user_profile.md
    ↓ MemoryRetriever.retrieve(USER_PROFILE, limit=1)
    ↓ scope-aware MEMORY_READ permission
    ↓ UserProfileStore.read_primary_record()
    ↓ Path.exists + read_text(encoding="utf-8", errors="ignore").strip()
    ↓ profile_content（空/缺失时为“暂无记录”）
    ↓ system prompt 的“用户长期画像（静态偏好）”段
    ↓ model invocation
```

读取发生在每次 `agent_node` 执行时，不在 app construction、CLI startup 或 Tool registration 时预加载。当前没有 profile cache、TTL、memoization、排序、目录扫描或 relevance retrieval；文件变化会在下一次 Agent node 读取时可见。

有效 GLOBAL 或 current authorized PROJECT profile 的 `MEMORY_READ` 使用 LOW risk，默认 ALLOW；它仍生成现有 `permission_decision` audit。PROJECT primary 缺失/空时，GLOBAL fallback 是第二次独立 `MEMORY_READ` authorization：PROJECT read 被拒绝时不会尝试 GLOBAL；PROJECT read 允许但 GLOBAL fallback 被拒绝时，Agent 使用既有 `暂无记录` fallback，不读取正文。

### Missing、multiple file 与 encoding 行为

- `memory/` 通常已由 config import 创建；如果 `user_profile.md` 不存在，Agent 不创建它，直接使用 `暂无记录`。
- 空文件经 `.strip()` 后同样回退到 `暂无记录`。
- `memory/other.md` 等 sibling file 被忽略，不存在 multiple-file ordering 语义。
- Profile 读取使用 `errors="ignore"`：invalid UTF-8 byte 被丢弃，读取继续，不会因 decoding error 终止 Agent node。
- Store 仍使用整文件 `read()`，没有文件数或 retrieval budget；但 ContextAssembler 对注入 prompt 的 summary/profile dynamic content 使用 8000 character supplemental budget。

当前没有专用的 `read_user_profile` Tool。模型通过后续 Agent prompt 自动获得当前 effective profile，或在上下文中保留先前已知内容。

## 4. Write Path

当前唯一正式的显式 Memory write entry point 是内置 Tool：

```text
save_user_profile(new_content)
    ↓ UserProfileStore.write_profile(new_content)
    ↓ parent.mkdir(parents=True, exist_ok=True)
    ↓ 同目录 temporary file 写入、flush、关闭
    ↓ os.replace(temp, user_profile.md)
    ↓ <WORKSPACE_DIR>/memory/user_profile.md
```

`save_user_profile` 先对同一次 resolved GLOBAL/PROJECT target 构造 `MEMORY_WRITE` request。有效 profile update 使用 MEDIUM risk，默认 ASK；只有 confirmation/session grant 解析为 final ALLOW 后才会创建目录、temporary file 或执行 replace。无 handler、DENY、无效确认均不产生 persistence side effect，并通过既有 permission ToolResult 文本返回阻断。

ALLOW 后该 Tool 以完整文本覆盖目标 profile，不 append、不 merge、不维护历史版本、不自动添加 newline。单次写入在同目录 temporary file 完整准备后才通过 `os.replace()` 替换目标；失败会尽力清理 temporary file，并以稳定的 `user_profile_write_failed` 错误传播到 Tool runtime。它仍没有 lock、conflict resolution 或 history。

GLOBAL、Project A、Project B 的 write grant target 使用不同 logical `memory_id`；因此 `ALLOW_SESSION` 只能复用同一 capability、operation、tool、scope 和 profile identity，不能跨 GLOBAL/PROJECT 或跨项目授权。profile content、新 content、absolute path 与 temporary path 不进入 permission request、prompt 或 audit。

## 5. Agent Context Injection

每次 Agent node 组装模型输入时，profile 被直接插入新建的 `SystemMessage`：

```text
系统规则
    + “用户长期画像（静态偏好）” delimiter
    + user_profile.md 完整内容或“暂无记录”
    + （若存在）“近期对话上下文” summary
    ↓
SystemMessage + 保留的近期对话消息
    ↓
llm.bind_tools(...).invoke(...)
```

Profile 有明确文本 delimiter，但没有独立 provenance model、trust level、instruction/data separation 或 escaping；它以原始文本（可能因 supplemental character budget 截断）与 system-level rules 同一条 message 发送。当前 Prompt 对 profile 的描述是“静态偏好”，但没有 runtime enforcement 防止 profile 文本包含指令样内容。

`AgentState.summary` 是另一条 context injection：当 Agent code 调用 `trim_context_messages(raw_messages, trigger_turns=40, keep_turns=10)` 丢弃早期回合时，它让模型生成约 150 字的 summary，并把该 summary 更新到 graph state；下次 node 会在 `[近期对话上下文]` 段直接拼入 system prompt。150 字是 summary prompt 的要求，不是对实际 state 的硬性 runtime length check。

## 6. Run 与持久化语义

### 同一 run

`save_user_profile` 完成后，文件已经写入 filesystem。下一次 Agent node 会重新打开该文件，因此可读取新内容；当前没有 profile read cache。

### 跨 run 与 process restart

显式 profile 是普通 workspace 文件：只要后续 run 使用相同 `MICLAW_WORKSPACE`，`user_profile.md` 会被再次读取；进程重启不会删除它。这是 filesystem persistence，不是带 schema、migration、history 或 consistency guarantees 的 durable memory system。

`entry/main.py` 还会以固定 `thread_id="local_geek_master"` 打开同一 `state.sqlite3` 的 `AsyncSqliteSaver`。因此 interactive entrypoint 配置的是可跨 process 存在的 LangGraph checkpoint 路径；该路径承载 conversation messages/summary，与 profile 文件独立。本 PR 不把其内部 checkpoint schema、恢复策略或迁移行为定义为 Memory contract。

## 7. Workspace Scope

### OFFICE default

显式 profile root 属于 `WORKSPACE_DIR/memory`，不属于 `WORKSPACE_DIR/office`。Office file Tool 通过 canonical containment 拒绝 `../memory/user_profile.md`；现有 sandbox tests 已覆盖此类越界读。

### PROJECT workspace

`miclaw run --workspace <path>` 通过 ContextVar 激活已 canonicalized 的 PROJECT root，供 file/shell sandbox Tool 与 `UserProfileStore` factory 共同使用。它不改变 `config.MEMORY_DIR`，但会让 profile Store 选择其中的 PROJECT namespace：

```text
<MEMORY_DIR>/projects/<sha256(canonical-project-path)[:24]>/user_profile.md
```

PROJECT profile 存在且非空时优先读取；缺失、空或 `errors="ignore"` 后为空时只读回退 GLOBAL `<MEMORY_DIR>/user_profile.md`，不会 copy、merge 或创建 project profile。PROJECT 写入只覆盖该 PROJECT 文件，永不覆盖 GLOBAL profile；Project A/B 的 derived ID 不同，因此 namespace 隔离。

相同 canonical path 在进程重启后会得到相同 ID；项目移动到不同绝对路径则得到新 namespace。当前没有 project manifest identity、迁移或 registry。EXTERNAL/未知 workspace scope 不能静默回退 GLOBAL，而是以 `unsupported_memory_scope` fail closed。

### Shell 与 generic Tool 的关系

文件 Tool 的 relative-path containment 阻止其读取 `memory/`。Shell Tool 的 `cwd` 是 active OFFICE/PROJECT root，并另外用危险 pattern 拦截 `..`、absolute path、home path 和 Windows drive path，因此当前 shell input 不能以 `../memory/...` 方式访问 profile。

这不改变 `save_user_profile` 的性质：它通过 resolved scoped `UserProfileStore` 写入，不通过 generic file Tool 的 containment；但现在会先经过专用的 scope-aware Memory permission boundary。

## 8. Scheduler、Skill 与 Tool 关系

### Scheduler

Scheduler/heartbeat 只读取和覆写 `<WORKSPACE_DIR>/tasks.json`，到期后把任务描述包装为系统内部消息放入 `task_queue`。它不直接 import、读取或写入 `MEMORY_DIR`，task result 也不会自动写入 profile。

在 interactive `entry/main.py` 中，heartbeat 发送的消息由同一个 `agent_worker` 和同一个 Agent app 处理，所以该次 Agent node 仍会走普通 profile injection。换言之：不存在显式 Scheduler ↔ Memory API，但 scheduled task 被投递给 Agent 后会间接使用普通 Agent context。

### Skills 与 generic Tools

`LazySkillLoader` / Skill discovery 没有直接引用 `MEMORY_DIR` 或 `user_profile.md`。当前没有专用 Memory read Tool；唯一显式 Memory Tool 是 `save_user_profile`。

Generic office file/shell Tools 与 profile path 的关系如上节所述；它们不构成 Memory subsystem 的正式读写入口。`state.sqlite3` 是 graph checkpointer 文件，不是这些 Tools 的正式 target。

## 9. Observability 与安全边界

Agent 记录的 `llm_input` JSONL event 当前只包含 `message_count`，不会直接写出已拼装的 system prompt 或完整 profile。Phase 3A 还会对 `tool_call` args、`tool_result` content 和 `ai_message` content 做有界 redaction/summary，monitor、`logs --tail`、`trace` 会在显示层再次清洗。

但 profile 会影响模型生成内容。若模型把 profile 内容转写到普通、未命中 sensitive/content 规则的 Tool argument 中，当前 conservative redaction 不是完整 DLP，不能保证业务敏感信息绝不出现在 observability path。Memory access 现在有 permission decision/confirmation audit，但 profile 本身仍没有 provenance 或 prompt-injection boundary，且 audit 不记录正文。

固定配置路径与 scope-aware identity 避免了由模型提供任意 Memory path 的问题；Memory permission 不等同于 filesystem sandbox，当前 writer 仍缺少 concurrency control 和 revision history。PR 35 的 atomic replace 只保护单次替换，不是完整持久化事务。

## 10. 当前行为测试覆盖

`tests/test_memory_runtime_characterization.py` 锁定以下现状：

- `MEMORY_DIR` 与 `UserProfileStore.profile_path` 的 workspace-relative 配置关系。
- `save_user_profile` 创建 UTF-8 profile，并以同目录 temporary file + `os.replace()` complete overwrite 更新它；准备或 replace 失败时旧文件保持不变且 temporary artifact 会清理。
- 缺失 profile 回退为 `暂无记录`，其他 `memory/` sibling file 不会自动读取。
- profile 与 `AgentState.summary` 进入同一 system prompt；二者共享 8000 character supplemental budget，summary 优先于 profile。
- 同一个 Agent app 的每次 node 从 filesystem 重读 profile，文件更新可见；invalid UTF-8 byte 被忽略。
- PROJECT profile 优先于 GLOBAL fallback；PROJECT write 不会修改 GLOBAL profile。
- GLOBAL/current PROJECT read 经过 LOW-risk `MEMORY_READ`；write 经过 MEDIUM-risk `MEMORY_WRITE` ASK，只有 final ALLOW 才持久化。

既有 `tests/test_builtins.py` 覆盖 profile save 的基础成功路径；`tests/test_sandbox_tools.py` 覆盖 generic office file Tool 对 `../memory/user_profile.md` 的 containment rejection；`tests/test_agent.py` 与 context tests 覆盖 Agent graph/state 的基础行为。

`tests/test_memory_retrieval.py` 额外锁定空请求零访问、请求边界、单一 effective record、无缓存 freshness、授权读取委托、DENY 零内容读取，以及 PROJECT/GLOBAL 选择和 Agent prompt 回归。

## 11. Known Limitations

### Current behavior

- 仅有 GLOBAL profile 和一个按 canonical project path digest 路由的 PROJECT profile；没有 user/task namespace 或多 record retrieval。
- 项目移动会改变 PROJECT namespace；当前没有 manifest identity、迁移或 registry。
- 只读取一个固定文件；没有 multi-file discovery、semantic search、relevance selection 或 multi-record ranking。
- profile/summary 的注入已有 8000 character supplemental budget，但没有完整 model token-window budget、provenance 或 instruction/data boundary。
- 缺少正式 profile read Tool；当前 profile 仅通过 Agent prompt 自动注入。
- 写入使用同目录唯一 temporary file，完整写入并关闭后以 `os.replace()` 原子替换正式文件。每次单独写入是原子的；没有 lock、revision、merge 或冲突检测，多个成功 writer 仍是最后完成 replace 的内容生效。这不是数据库事务，也不承诺断电场景的完整 crash consistency。
- 仅有 scoped user-profile Memory permission 与固定 USER_PROFILE retrieval；没有 persistent grants、read Tool、semantic retrieval 或通用 Memory authorization framework。
- `errors="ignore"` 容忍损坏 UTF-8，但也会静默丢弃 byte。
- `state.sqlite3` checkpoint、conversation summary 与 profile 文件的职责边界未由独立 Memory API 表达。
- 当前 redaction 是 observability safety baseline，不是对 profile-derived information 的完整 DLP。

### Desired Phase 4 behavior（未在 PR 32 实现）

后续可以在不破坏既有 workspace 文件兼容性的前提下，明确 Memory API、scope/isolation、retrieval、budget、provenance、safe context injection 与写入一致性。本文不规定这些能力的接口或数据模型。

## 12. 后续重构约束

PR 33+ 在重构时至少需要显式处理：

1. 兼容已有 `workspace/memory/user_profile.md`，或提供可审计迁移。
2. 区分 profile、conversation checkpoint、summary 与 future task/project memory 的职责。
3. 避免把 PROJECT root authorization 当作 Memory 自动授权。
4. 将当前 supplemental character budget 扩展为可验证的 model token-window budget，并定义 provenance。
5. 为 Memory write 定义 permission、并发冲突与 history 语义；当前只保证单次原子 replace，不处理多 writer 协调。

## 13. PR 34 结构化语义（不改变持久化或路由）

`UserProfileStore.read_record()` 现在将现有非空 `user_profile.md` 映射为不可变 `MemoryRecord`：

```text
memory_id = "user-profile"
kind      = USER_PROFILE
scope     = GLOBAL
source    = USER_PROFILE_STORE
content   = 原 Markdown 全文
```

`source` 仅表示 record 来自 `UserProfileStore` 这一来源通道，不证明内容由用户本人创作。`GLOBAL` 是当前 profile 的逻辑 scope：它不表示不受限权限，也不改变 OFFICE/PROJECT filesystem authorization。模型也不会看到上述 id、scope 或 source；Agent 仅继续使用 `record.content` 组装原有 prompt。

`read_profile()` 仍保留，并委托 `read_record()` 以保持原字符串 API。PR 36 使用已授权 canonical PROJECT root 的 SHA-256 digest（前 24 个 hex 字符）作为 opaque `scope_id`，并将 PROJECT profile 放在 `<MEMORY_DIR>/projects/<scope_id>/user_profile.md`；它不把原路径放入 prompt、record、filename 或 diagnostics。

PR 32 建立了以上基线；PR 33 抽取了 `UserProfileStore` filesystem boundary；PR 34 将内部读取表示由 `str` 提升为 `MemoryRecord`；PR 35 将写入替换为 atomic replace；PR 36 有意识地改变 profile persistence routing：OFFICE 保持 GLOBAL，PROJECT 使用 scoped profile 并在缺失时回退 GLOBAL。

## 14. PR 38 检索边界（不改变 context 内容）

`MemoryRetriever` 只接受 host/runtime 构造的 `MemoryRetrievalRequest`，当前唯一支持 `USER_PROFILE`。请求要求 tuple kind 集合、无重复 kind，且 `limit` 为 1–32 的精确整数；空 kind 集合直接返回空 tuple，不触发 permission 或 filesystem access。实际 `USER_PROFILE` retrieval 始终复用 `read_authorized_user_profile()`，因此 PROJECT primary 与独立授权的 GLOBAL fallback 仍只返回一个 effective record，或在阻断时返回空 tuple。

Retriever 返回不可变 tuple，不扫描目录、不做 query、语义搜索、排序或缓存，也不格式化 prompt 或记录 profile 内容。Agent 仍只取该 record 的 `content`，沿用原有 SystemMessage delimiter、summary 顺序与 `暂无记录` fallback。

## 15. PR 39 Context Assembly（Supplemental Character Budget）

Agent 先用 `MemoryRetriever` 取得至多一个 effective `MemoryRecord`，再把 base system rules、profile record 和 `AgentState.summary` 交给纯 `ContextAssembler`，生成单一 `SystemMessage`。Assembler 不读取 filesystem、不做 permission、retrieval、写入、LLM 调用或 observability logging。

`DEFAULT_SUPPLEMENTAL_CONTEXT_CHAR_BUDGET` 为 8000，使用 Python `len()` 的 character count，不等同模型 token budget。该 hard budget 只计 summary 与 profile content；base system rules、固定 section labels/empty/omission markers，以及 retained recent messages 均不计入也不会被截断。summary 优先分配动态预算，profile 使用余量；render order 仍为 base rules、长期画像、近期对话上下文。profile 存在但无余量时显示固定“内容因上下文预算未注入”标记，与不存在 record 时的“暂无记录”区分。

PR 39 不调整 recent-message trimming、summary generation、routing、permission、retrieval、trust/provenance 或 prompt 注入语义；它只是 bounded context baseline，不是完整 tokenizer-aware model-window manager。
