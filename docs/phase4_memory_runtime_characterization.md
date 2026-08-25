# MiClaw Phase 4：Memory Runtime Characterization

本文记录 PR 32 建立、由 PR 33/34 保持的 Memory runtime 行为。PR 33 将固定 profile 的 filesystem IO 收敛到 `UserProfileStore`；PR 34 仅为读取结果补充最小的结构化语义模型，不定义新的通用 `MemoryStore`、retrieval、project isolation 或 context API。

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
UserProfileStore.profile_path = <MEMORY_DIR>/user_profile.md
```

Config import 会通过 `os.makedirs(..., exist_ok=True)` 创建 `WORKSPACE_DIR`、`memory/`、`office/`、`office/skills/` 等目录；它不会创建 `user_profile.md`。当前没有 daily memory、agent-specific memory、task memory、per-project profile 文件或 metadata index。

### 格式与范围

`user_profile.md` 是任意 UTF-8 Markdown 文本，没有 front matter、record schema、version、分段约定或文件大小限制。Agent 只读取这个固定文件；同一 `memory/` 目录中的其他 `.md` 文件不会自动进入 prompt。

正常运行中 workspace 由环境变量在 import 前决定；当前没有 runtime workspace switching API。`agent.py` 与 `builtins.py` 仍在 import 时取得 `MEMORY_DIR`，因此若进程内显式 reload `miclaw.core.config`，这些已导入模块不会自动切换到新的 Memory root。这是当前 module-level config binding 的限制；`UserProfileStore` 本身不缓存内容。

## 3. Read Path

显式 Memory 的正式 read entry point 不是一个独立 Tool/API，而是 `miclaw.core.agent.create_agent_app()` 内部的 `agent_node`，它通过 `UserProfileStore` 读取：

```text
<WORKSPACE_DIR>/memory/user_profile.md
    ↓ UserProfileStore.read_profile()
    ↓ Path.exists + read_text(encoding="utf-8", errors="ignore").strip()
    ↓ profile_content（空/缺失时为“暂无记录”）
    ↓ system prompt 的“用户长期画像（静态偏好）”段
    ↓ model invocation
```

读取发生在每次 `agent_node` 执行时，不在 app construction、CLI startup 或 Tool registration 时预加载。当前没有 profile cache、TTL、memoization、排序、目录扫描或 relevance retrieval；文件变化会在下一次 Agent node 读取时可见。

### Missing、multiple file 与 encoding 行为

- `memory/` 通常已由 config import 创建；如果 `user_profile.md` 不存在，Agent 不创建它，直接使用 `暂无记录`。
- 空文件经 `.strip()` 后同样回退到 `暂无记录`。
- `memory/other.md` 等 sibling file 被忽略，不存在 multiple-file ordering 语义。
- Profile 读取使用 `errors="ignore"`：invalid UTF-8 byte 被丢弃，读取继续，不会因 decoding error 终止 Agent node。
- 读取使用整文件 `read()`，目前没有字符、token、文件数或 retrieval budget；较大 profile 会整体拼入 prompt。

`save_user_profile` 的 docstring 提到先调用 `read_user_profile`，但当前 `BUILTIN_TOOLS` 中没有这个 Tool，代码库也没有同名正式 read API。模型实际只能通过后续 Agent prompt 自动看到 profile，或在上下文中保留先前已知内容。

## 4. Write Path

当前唯一正式的显式 Memory write entry point 是内置 Tool：

```text
save_user_profile(new_content)
    ↓ UserProfileStore.write_profile(new_content)
    ↓ parent.mkdir(parents=True, exist_ok=True)
    ↓ write_text(..., encoding="utf-8")
    ↓ <WORKSPACE_DIR>/memory/user_profile.md
```

该 Tool 以完整文本覆盖旧 profile，不 append、不 merge、不维护历史版本、不自动添加 newline，也没有 atomic write / lock / conflict resolution。成功返回固定中文消息；写入异常会按 Python 当前异常传播到 Tool runtime，函数本身不提供专门的 failure envelope。

`PermissionCapability.MEMORY_READ` / `MEMORY_WRITE` 已存在于 policy enum，但 `save_user_profile` 当前没有构造 `PermissionRequest`，因此不经过 Phase 2 的 confirmation/session grant/audit 链路。这是当前实现事实，不表示后续设计应继续如此。

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

Profile 有明确文本 delimiter，但没有独立 provenance model、trust level、instruction/data separation、escaping 或 length/token budget；它以原始文本与 system-level rules 同一条 message 发送。当前 Prompt 对 profile 的描述是“静态偏好”，但没有 runtime enforcement 防止 profile 文本包含指令样内容。

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

`miclaw run --workspace <path>` 只通过 ContextVar 激活 PROJECT root，供 file/shell sandbox Tool 解析 active root。它不会改变 `config.MEMORY_DIR`，也不会改变 `agent.py` / `builtins.py` 传给 `UserProfileStore` 的 `MEMORY_DIR`。

因此，Project A 与 Project B 的 Agent run 在相同 `MICLAW_WORKSPACE` 下共享同一个 `workspace/memory/user_profile.md` namespace。这是当前已知 isolation limitation；显式 PROJECT authorization 不会自动建立 project-scoped Memory。

### Shell 与 generic Tool 的关系

文件 Tool 的 relative-path containment 阻止其读取 `memory/`。Shell Tool 的 `cwd` 是 active OFFICE/PROJECT root，并另外用危险 pattern 拦截 `..`、absolute path、home path 和 Windows drive path，因此当前 shell input 不能以 `../memory/...` 方式访问 profile。

这不改变 `save_user_profile` 的性质：它通过固定配置路径构造的 `UserProfileStore` 写入，不通过 generic file Tool 的 containment，也不通过当前 permission pipeline。

## 8. Scheduler、Skill 与 Tool 关系

### Scheduler

Scheduler/heartbeat 只读取和覆写 `<WORKSPACE_DIR>/tasks.json`，到期后把任务描述包装为系统内部消息放入 `task_queue`。它不直接 import、读取或写入 `MEMORY_DIR`，task result 也不会自动写入 profile。

在 interactive `entry/main.py` 中，heartbeat 发送的消息由同一个 `agent_worker` 和同一个 Agent app 处理，所以该次 Agent node 仍会走普通 profile injection。换言之：不存在显式 Scheduler ↔ Memory API，但 scheduled task 被投递给 Agent 后会间接使用普通 Agent context。

### Skills 与 generic Tools

`LazySkillLoader` / Skill discovery 没有直接引用 `MEMORY_DIR` 或 `user_profile.md`。当前没有专用 Memory read Tool；唯一显式 Memory Tool 是 `save_user_profile`。

Generic office file/shell Tools 与 profile path 的关系如上节所述；它们不构成 Memory subsystem 的正式读写入口。`state.sqlite3` 是 graph checkpointer 文件，不是这些 Tools 的正式 target。

## 9. Observability 与安全边界

Agent 记录的 `llm_input` JSONL event 当前只包含 `message_count`，不会直接写出已拼装的 system prompt 或完整 profile。Phase 3A 还会对 `tool_call` args、`tool_result` content 和 `ai_message` content 做有界 redaction/summary，monitor、`logs --tail`、`trace` 会在显示层再次清洗。

但 profile 会影响模型生成内容。若模型把 profile 内容转写到普通、未命中 sensitive/content 规则的 Tool argument 中，当前 conservative redaction 不是完整 DLP，不能保证业务敏感信息绝不出现在 observability path。Profile 本身也没有专门的 permission、audit、provenance 或 prompt-injection boundary。

固定配置路径避免了由模型提供任意 Memory path 的问题，但当前 Memory writer 直接覆盖该路径，缺少 permission gate、atomic write、concurrency control 和 revision history。

## 10. 当前行为测试覆盖

`tests/test_memory_runtime_characterization.py` 锁定以下现状：

- `MEMORY_DIR` 与 `UserProfileStore.profile_path` 的 workspace-relative 配置关系。
- `save_user_profile` 创建 UTF-8 profile，并以 complete overwrite 更新它。
- 缺失 profile 回退为 `暂无记录`，其他 `memory/` sibling file 不会自动读取。
- profile 全量进入 system prompt，`AgentState.summary` 也进入同一 system prompt。
- 同一个 Agent app 的每次 node 从 filesystem 重读 profile，文件更新可见；invalid UTF-8 byte 被忽略。
- 激活 PROJECT root 不会切换显式 Memory root。

既有 `tests/test_builtins.py` 覆盖 profile save 的基础成功路径；`tests/test_sandbox_tools.py` 覆盖 generic office file Tool 对 `../memory/user_profile.md` 的 containment rejection；`tests/test_agent.py` 与 context tests 覆盖 Agent graph/state 的基础行为。

## 11. Known Limitations

### Current behavior

- 单一、全局的 `user_profile.md`，没有 user/project/task namespace。
- PROJECT workspace 不隔离 profile；同一 `MICLAW_WORKSPACE` 下的 Project runs 共享它。
- 只读取一个固定文件，没有 multi-file discovery、排序、retrieval 或 relevance selection。
- profile 全量 direct concatenation 进 SystemMessage，没有 hard budget、provenance 或 instruction/data boundary。
- 缺少正式 profile read Tool；现有 write Tool docstring 引用了不存在的 `read_user_profile`。
- 写入是非原子整文件覆盖，不做 permission/confirmation、locking、revision 或 merge。
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
4. 为 retrieval/context injection 定义可验证的 size/token budget 和 provenance。
5. 为 Memory write 定义 permission、failure、concurrency 与 history 语义，而不是隐式继承当前直接覆写行为。

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

模型词表也可表达带非空 opaque `scope_id` 的 `PROJECT` scope，但 PR 34 没有创建 project storage、没有从路径生成 id、也没有在 PROJECT run 中启用该 route。`read_profile()` 仍保留，并委托 `read_record()` 以保持原字符串 API。

PR 32 建立了以上基线；PR 33 抽取了 `UserProfileStore` filesystem boundary；PR 34 只将内部读取表示由 `str` 提升为 `MemoryRecord`，并保持所有 observable behavior 不变。
