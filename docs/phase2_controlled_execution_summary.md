# MiClaw Phase 2：受控执行与工作区权限总结

`Controlled Execution & Workspace Permissions`

## 阶段概述

Phase 1 已建立 office 路径边界、permission model、ToolResult、permission audit、shell safety baseline 和基础 observability。Phase 2 在这些基础上补齐受控执行闭环，使 `ASK` decision 可以在交互式运行中被显式确认，并让用户能够在当前 run 内临时授权重复操作，以及显式选择一个真实 PROJECT directory 作为受约束的执行 root。

Phase 2 覆盖四个主要方向：

- `ASK` permission confirmation core 与 CLI 交互确认。
- 当前 session 内、不可持久化的临时 permission grant。
- OFFICE、PROJECT、EXTERNAL workspace scope 模型，以及显式 PROJECT root 激活。
- PROJECT 文件和 shell 操作的 scope-aware policy、端到端测试与 audit/trace 验证。

这些能力让 workspace boundary 与 permission boundary 可以分别表达“在哪里执行”和“允许执行什么”。它们不代表 MiClaw 已成为完整 coding agent、完整 sandbox 或 unrestricted execution runtime。

## PR 13–PR 19 完成情况

| PR | 方向 | 实际完成内容 |
| --- | --- | --- |
| PR 13 | Permission confirmation core | `PermissionConfirmationHandler` 定义了 ASK confirmation callback 协议；`resolve_permission()` 只对原始 `ASK` result 查询 session grant 或调用 handler。无 handler、handler exception、返回 `ASK`、无效值或非明确允许结果都会 fail closed。Handler 通过 `ContextVar` 注入和 reset，并新增保留 policy/final decision 的 `permission_confirmation` audit event。 |
| PR 14 | Interactive CLI permission confirmation | `entry/cli.py` 新增 CLI-specific confirmation handler，并只在 `miclaw run` 期间绑定。Prompt 展示固定安全字段，支持单次允许、当前 session 允许和拒绝；空输入、非法输入、EOF、`KeyboardInterrupt` 或 prompt exception 都返回拒绝。Run 结束或异常退出时 handler 会在 `finally` 中 reset。 |
| PR 15 | Session-scoped permission grants | 新增 `PermissionConfirmationChoice` 和 `SessionPermissionGrant`，支持 `ALLOW_ONCE`、`ALLOW_SESSION`、`DENY`。每次 interactive run 通过 `set_session_permission_grants()` 创建新的内部空 `set`，不接受 caller-owned mutable set；grant 只存在于当前 `ContextVar` lifecycle，run A 不会授权 run B。Grant matching 使用精确字段，不实现 persistent grant、wildcard 或 permission DSL。 |
| PR 16 | Workspace scope abstraction | 新增 `WorkspaceScope` 和 `WorkspaceRoot`，定义 OFFICE、PROJECT、EXTERNAL scope 与 canonical root path。Sandbox path helper 被改为 root-aware resolver，使 path validation 可以相对显式 `WorkspaceRoot` 工作；该阶段仍只有 OFFICE 作为 active root。 |
| PR 17 | Explicit authorized project roots | 新增 `miclaw run --workspace <path>`，由用户显式激活当前 run 的 PROJECT root。输入 path 会 `expanduser()`、`resolve(strict=True)`，且必须已经存在并且是 directory；不会自动创建、从 cwd 推断或进行 Git repo discovery。未提供参数时仍使用 OFFICE。Shell cwd 使用 active root，session grant 加入 `workspace_scope`，从而隔离 OFFICE 与 PROJECT。 |
| PR 18 | Project workspace permission policy | `evaluate_permission()` 开始显式读取 `workspace_scope`。PROJECT low-risk `read`/`list` 返回 `ALLOW`，PROJECT `write` 和 `shell execute` 返回 `ASK`，PROJECT unknown operation 返回 `DENY`；invalid、unknown 或 EXTERNAL scope fail closed。OFFICE 文件和 shell policy 保留 Phase 1 默认行为。 |
| PR 19 | Project workspace integration hardening | 最终没有修改 runtime，只新增 PROJECT 端到端 integration tests。测试串联 PROJECT activation、read/list、write deny/allow once/allow session、grant reuse 和跨 run 清理、safe shell cwd、critical shell hard block、path escape、CLI context cleanup，以及 permission audit 与 trace 安全字段；完整测试套件通过。 |

## Permission confirmation 架构

Phase 2 后，permission pipeline 的核心流程为：

```text
permission policy
├─ ALLOW
│  └─ 直接进入后续执行
├─ DENY
│  └─ 直接阻断，不调用 confirmation handler
└─ ASK
   ├─ 查询当前 session 的精确 matching grant
   │  └─ 命中：final ALLOW
   └─ 未命中
      ├─ 无 confirmation handler：保持阻断
      └─ 调用 confirmation handler
         ├─ 明确允许：final ALLOW
         └─ 拒绝、异常或无效结果：final DENY
```

关键约束如下：

- `DENY` 是 policy 的最终阻断结果，confirmation 和 session grant 都不能覆盖它。
- Confirmation 只解析 `ASK`，不会为原始 `ALLOW` 或 `DENY` 增加 prompt。
- 无 confirmation handler 时，`ASK` 仍是 blocked state，不会自动执行。
- Handler exception、`None`、`ASK`、字符串形式的伪造 allow 或其他无效返回值都不会 fail open。
- `resolve_permission()` 保留原始 `policy_decision=ask`，并在 final result metadata 中区分 confirmation decision、choice 和 source；不会把原始 policy event 改写成 `ALLOW`。

## CLI 用户确认机制

`miclaw run` 会为本次 interactive run 绑定 `cli_permission_confirmation_handler`。只有 permission policy 返回 `ASK` 且没有 matching session grant 时才会显示 prompt。

当前输入映射为：

| 输入 | 结果 |
| --- | --- |
| `a`、`allow`、`once`、`y`、`yes` | `ALLOW_ONCE` |
| `s`、`session` | `ALLOW_SESSION` |
| `d`、`n`、`no`、空输入或其他值 | `DENY` |
| EOF、`KeyboardInterrupt`、prompt exception | `DENY` |

Prompt 默认值是 `d`，不存在 default allow。Prompt 只展示以下安全摘要字段：

- `tool_name`
- `capability`
- `operation`
- `risk_level`
- `workspace_scope`
- 经过相对路径检查和长度限制的 safe target
- 当前状态和可选操作

Shell confirmation 不展示 raw command，只展示 workspace scope。Prompt 不展示 full metadata、raw args、file content、stdout/stderr、环境变量、API key、password、token、Authorization header 或 Bearer secret。

CLI handler、session grants 和 PROJECT root 都在 `run_agent()` 的 `finally` 中按 token/reset 模式恢复。没有显式绑定 CLI handler 的非交互式调用仍保持 ASK blocked。

## Session grant 模型

`SessionPermissionGrant` 是 frozen dataclass，实际 matching 字段包括：

- `capability`
- `operation`
- `tool_name`
- `target_scope`，来源于 `PermissionRequest.target`
- `workspace_scope`
- `risk_level`

Grant 使用 dataclass equality 做精确匹配。因此不同 capability、operation、tool、target、workspace scope 或 risk level 不会互相授权。

Session grant lifecycle 为：

```text
开始 interactive run
→ 创建新的内部空 set
→ 绑定 session-grant ContextVar
→ ALLOW_SESSION 时加入精确 grant
→ 当前 run 内可复用
→ finally reset ContextVar
→ grant 不再可见
```

最终实现不接受外部 mutable set 作为 session authorization store。每次调用 `set_session_permission_grants()` 都创建新的内部 `set()`，因此 caller 即使保留旧引用，也不能把 run A 的 stale grant 注入 run B。

Grant 只保存在进程内存和当前 execution context 中，不写入 JSON、SQLite、config、environment、memory Markdown 或 `tasks.json`。`ALLOW_ONCE` 和 `DENY` 都不会创建 grant。

`OFFICE grant != PROJECT grant`。此外，PROJECT shell grant 不会授权 PROJECT file capability。Shell grant 不保存 raw command；在同一 workspace scope 内，它可以复用到后续匹配的 safe `shell_exec/execute` request，但每条 command 仍先经过 shell hard safety。

## Workspace 模型

### `WorkspaceScope`

当前 enum 包含：

- `OFFICE = "office"`
- `PROJECT = "project"`
- `EXTERNAL = "external"`

### `WorkspaceRoot`

`WorkspaceRoot` 保存 canonical `Path` 和 `WorkspaceScope`，并可通过 `to_dict()` 输出 JSON-friendly 描述。它只描述 root，不提供 persistent registry、multiple-root manager 或 trust database。

### 当前实际支持

**OFFICE** 是默认 active root。未显式设置 PROJECT 时，sandbox tools 使用配置中的 `OFFICE_DIR`。

**PROJECT** 必须由当前 run 的调用方通过显式 path 激活。当前只允许一个 `ContextVar` 绑定的 PROJECT root；它优先于 OFFICE，并在 run 结束后 reset。

**EXTERNAL** 目前只作为 enum/policy 概念存在。没有 public activation path，permission policy 对显式 EXTERNAL scope fail closed。

## PROJECT root 激活与边界

用户通过以下命令显式选择 PROJECT root：

```bash
miclaw run --workspace /path/to/existing/project
```

激活流程为：

```text
显式 path
→ Path(...).expanduser()
→ resolve(strict=True)
→ 验证 path 已存在
→ 验证 path 是 directory
→ 构造 WorkspaceRoot(scope=PROJECT)
→ 绑定当前 run 的 ContextVar
```

显式参数中的 path 如果无效、不存在或指向普通文件，会产生明确错误并终止启动，不会 fallback 到更宽的 filesystem access。省略 `--workspace` 时按设计继续使用 OFFICE。PROJECT root 不会自动创建，也不会从 current working directory、Git root、recent workspace 或配置 registry 推断。

PROJECT 文件 target 仍必须是 root-relative path：

```text
project-relative input
→ 相对 canonical PROJECT root 解析
→ resolved containment check
→ PermissionRequest
→ permission policy
→ session grant / confirmation
→ final ALLOW
→ side effect
```

Path resolver 继续拒绝 absolute path、Windows drive path、prefix/sibling escape，以及平台支持时指向 root 外部的 symlink。对于包含 `..` 的 relative path，判断依据是 canonical resolution 后的 containment：解析结果越出 active authorized root 时拒绝；例如 `a/../b` 在最终结果仍位于 root 内时可以通过。

## Workspace-aware permission matrix

当前 `evaluate_permission()` 对 workspace file/shell capability 的行为如下：

| Scope | Operation | 当前 policy |
| --- | --- | --- |
| OFFICE | `FILE_READ`，low risk | `ALLOW` |
| OFFICE | `FILE_READ`，高于 low risk | `ASK` |
| OFFICE | `FILE_WRITE`，low risk | `ALLOW` |
| OFFICE | `FILE_WRITE`，高于 low risk | `ASK` |
| OFFICE | `SHELL_EXEC` | `ASK` |
| PROJECT | `FILE_READ/read`，low risk | `ALLOW` |
| PROJECT | `FILE_READ/list`，low risk | `ALLOW` |
| PROJECT | `FILE_READ/read|list`，高于 low risk | `ASK` |
| PROJECT | `FILE_WRITE/write` | `ASK` |
| PROJECT | `SHELL_EXEC/execute` | `ASK` |
| PROJECT | unknown file/shell operation | `DENY` |
| EXTERNAL | 带显式 scope 的 capability | `DENY` |
| invalid/unknown scope | 带显式 scope 的 capability | `DENY` |
| missing scope | workspace-sensitive file/shell capability | `DENY` |

非 workspace capability 的既有默认策略仍为：low-risk `MEMORY_READ` 允许，更高风险 memory read 返回 `ASK`，`MEMORY_WRITE` 返回 `ASK`，`NETWORK_ACCESS`、`MCP_TOOL` 和 unknown capability 返回 `DENY`。这些 enum 和 policy 分支不代表 network 或 MCP tool 已经实现。

## 文件执行安全顺序

List/read/write 工具的共同链路是：

```text
不可信 relative input
→ 获取 active WorkspaceRoot
→ 拒绝 absolute / Windows drive input
→ resolve candidate
→ relative_to(active root) containment check
→ existing target 或 write parent/target validation
→ 生成 workspace-relative target
→ 构造 PermissionRequest
→ evaluate_permission()
→ resolve_permission()：session grant / confirmation
→ final ALLOW
→ 文件操作
```

对 write 工具，parent directory 创建、文件创建、overwrite 和 append 都在 final `ALLOW` 之后。被 path validation、`DENY`、未确认 `ASK` 或 confirmation denial 阻断时，不会调用 `mkdir()` 或打开目标文件。

当前 write 支持 `w` 和 `a` 两种 mode。Mode validation 位于 permission resolution 之后、filesystem side effect 之前；非法 mode 返回 `invalid_mode`，不会写文件。

## Shell 执行安全顺序

Shell tool 的实际链路是：

```text
command request
→ 获取 active WorkspaceRoot
→ shell command risk classification
→ critical/high hard block
→ dangerous directory/absolute path pattern block
→ active root symlink escape scan
→ 构造 SHELL_EXEC PermissionRequest
→ permission policy
→ matching session grant lookup
→ confirmation（仍未 final ALLOW 时）
→ final ALLOW
→ subprocess.run(cwd=canonical authorized root)
```

Shell hard safety 位于 permission confirmation 和 session grant 之前。即使 confirmation handler 返回 `ALLOW`，或者存在 matching shell grant，被 classifier 标记为 blocked 的 command 仍不会进入 permission evaluation 或 `subprocess.run`。

当前 shell baseline 还包括：

- `subprocess.run(..., shell=True)` 的 cwd 固定为 canonical active root。
- Timeout 为 `SHELL_TIMEOUT_SECONDS = 10` 秒。
- stdout 和 stderr 分别限制为 `SHELL_OUTPUT_LIMIT = 4000` characters，截断时增加 marker，并只把长度和 truncation 状态放入 ToolResult data。
- Permission audit 仅记录 `shell_command_present`、`command_length`、`shell_risk_level`、`blocked_by_shell_safety` 和 safe cwd scope，不记录 raw command 或 stdout/stderr。

该实现仍只是 conservative shell safety baseline。Regex classifier、cwd containment 和 permission gating 不能替代 container、OS-level jail、seccomp 或独立低权限执行用户，也不能覆盖所有 shell 语法和间接副作用。

## OFFICE 与 PROJECT 的隔离

Phase 2 明确区分两个边界：

- **Workspace boundary**：决定文件和 shell 操作在哪个 canonical root 内执行。
- **Permission boundary**：决定某个 capability、operation、target 和 risk 是否可以执行。

显式授权 PROJECT root 只建立 filesystem containment，不等于 blanket authorization。PROJECT low-risk read/list 可以按 policy 直接执行，但 file write 和 shell execute 仍返回 `ASK`，必须由 confirmation 或 matching session grant 解析为 final `ALLOW`。

Grant key 包含 `workspace_scope`，因此：

- OFFICE grant 不能授权 PROJECT request。
- PROJECT grant 不能授权 OFFICE request。
- PROJECT shell grant 不能授权 PROJECT file request。
- 不同 target、operation、tool 或 risk level 的 grant 不匹配。

## Audit 与 Observability

Phase 2 复用 Phase 1 的 JSONL logging 和 trace context，并对 permission event、CLI confirmation prompt 及部分 CLI formatter 实施有明确范围的安全摘要。当前 observability pipeline 并不是全链路 secret-safe 或统一 redaction layer。

每次进入 permission evaluation 的 sandbox request 会记录一个 `permission_decision` event，其中包含：

- `tool_name`
- `capability`
- `operation`
- workspace-relative `target`
- `decision`
- `risk_level`
- `requires_confirmation`
- permission-related `error_type`
- 经过 allowlist 过滤的 metadata，包括 `workspace_scope`

当 policy 为 `ASK` 且确实调用 confirmation handler，或命中 session grant 时，还会记录 `permission_confirmation` event。该 event 保留：

- `policy_decision`
- `confirmation_decision`
- `confirmation_choice`
- `source`，当前可能是 `interactive`、`session_grant` 或兼容 handler source
- `final_decision`
- safe request fields 和 metadata

Hard-blocked shell command 在 permission evaluation 之前停止，因此不会伪造一条 permission decision event。

当 `TraceContext` active 时，central logger 为同一 run 的 event 增加 `run_id` 和递增的 event-level `step_id`。`entry.main.main()` 为每次 interactive run 创建新的 `run_id`，并在退出时 reset trace context。

现有观测入口包括：

- `miclaw monitor`
- `miclaw logs --tail`
- `miclaw trace <run_id>`

`permission_decision` 已有专门的安全摘要渲染。`permission_confirmation` 会写入 JSONL；当前 monitor/log CLI 对未专门格式化的 event 使用 generic fallback，不展示 full metadata。`miclaw logs --tail` 和 `miclaw trace <run_id>` 复用 `format_log_event_for_cli()`：其中 tool-call 只显示参数 key、presence 和 length，tool-result 只显示 tool name，不输出底层 result content。`miclaw trace <run_id>` 只筛选和排序 JSONL event，不读取 checkpoint、不 replay，也不 re-execute tool。

上述限制不等于底层 JSONL 已完成统一脱敏。`miclaw/core/agent.py` 当前仍会：

- 在 agent `tool_call` event 中写入 raw `tool_call.args`。
- 在 agent `tool_result` event 中写入 `msg.content[:200]` 作为 `result_summary`。
- 在 `ai_message` event 中写入 response content。

`miclaw monitor` 对 tool-call 使用 safe summary，但其 tool-result panel 会读取并显示 `result_summary`；因此该摘要如果本身含有 secret 或 file content，仍可能出现在 monitor 中。Permission audit builder、permission confirmation event、CLI confirmation prompt 以及 logs/trace CLI formatter 已做的安全限制，不能被扩大解释为整个 JSONL/monitor pipeline 都已脱敏。

另外，shell tool 的 model-facing success string 仍包含本次 command 与截断后的 stdout/stderr。Phase 2 的安全摘要控制不适用于所有模型工具返回值。

## Phase 2 集成验证

PR 19 新增 `tests/test_project_workspace_integration.py`，并复用既有 focused regression tests 验证完整受控执行链。覆盖内容包括：

- 显式 PROJECT activation 与 canonical root。
- 未指定 PROJECT 时回到 OFFICE。
- PROJECT low-risk list/read 不触发 confirmation。
- PROJECT write denial 无 side effect。
- `ALLOW_ONCE` 只允许当前 mutation，不创建 reusable grant。
- `ALLOW_SESSION` 创建 grant，matching write 可在同一 run 内复用。
- Safe PROJECT shell 通过 ASK/confirmation 执行，cwd 等于 canonical PROJECT root。
- PROJECT shell grant 在同一 run 内复用。
- Critical shell command 在 confirmation/grant 之前 hard block。
- Run A grant 不进入 run B，fresh grant container identity 不同。
- OFFICE/PROJECT、capability、operation、target 和 risk matching isolation。
- Traversal、prefix/sibling、absolute path 和 symlink escape 阻断。
- Permission failure 不创建或修改文件。
- CLI valid/invalid PROJECT path、正常退出和 agent exception 后的 ContextVar cleanup。
- `permission_decision`、interactive/session-grant `permission_confirmation`、`run_id`、递增 `step_id` 和 safe audit fields。

最终完整 pytest suite、compileall、ruff 和 diff check 均通过。文档不固定写入 test count，以避免后续新增测试后数字失真。

## Phase 2 最终安全不变量

1. PROJECT root 必须由调用方通过显式 path 授权。
2. 未显式授权 PROJECT 时，默认仍为 OFFICE。
3. Workspace authorization 只确定执行 root，不等于 operation permission。
4. Policy `DENY` 不能被 confirmation 或 session grant 覆盖。
5. `ASK` 在无 handler、无 grant、handler exception 或无效返回时 fail closed。
6. 每个 interactive run 创建新的内部 session grant set，grant 不跨 run。
7. OFFICE 与 PROJECT grants 通过 `workspace_scope` 隔离。
8. 文件 side effect 只发生在 path validation 和 final `ALLOW` 之后。
9. Critical/high blocked shell safety 不能被 confirmation 或 grant 覆盖。
10. 文件 resolved path 和 shell cwd 必须保持在 active authorized root 内。
11. Permission decision/confirmation event、CLI confirmation prompt 以及 logs/trace CLI safe formatter 不应输出 raw shell command、raw tool args、file content、secret 或不必要的 absolute local path；该不变量不覆盖尚未统一脱敏的 agent-level JSONL event 和 monitor tool-result panel。

## 当前尚未实现

以下能力不属于 Phase 2 已实现范围：

- 没有 permanent permission grants 或“always allow”。
- 没有跨 session trusted workspace。
- 没有 persistent workspace registry、recent workspace list 或 trust database。
- 没有多个同时 active 的 PROJECT roots。
- 没有 EXTERNAL root activation 或任意 external filesystem access。
- 没有 cwd/Git repo auto-discovery。
- 没有 Git-aware permission policy。
- 没有 patch-specific tool 或 patch-specific permission。
- 没有完整 Docker/container sandbox。
- 没有 OS-level shell isolation、seccomp、jail 或独立低权限执行 backend。
- 没有 MCP integration、MCP client 或 MCP tool adapter。
- 没有 network tool system；现有 `NETWORK_ACCESS` 只是默认 DENY 的 capability enum。
- 没有完整 permission DSL、wildcard grant 或 per-file glob policy。
- 没有 full trace replay/debugger、checkpoint inspection 或 tool re-execution。
- 没有通用 trace search/filter/export system。
- 没有全链路 audit/log redaction；部分 agent `tool_call`/`tool_result` event 仍可能包含 raw args 或结果摘要，monitor 也可能显示该 `result_summary`。
- 没有 `miclaw skills list`、`miclaw skills lint` 或正式 SKILL.md metadata validation command。
- 没有完整 memory/context redesign。
- 没有 scheduler redesign；仍保留现有 heartbeat/`tasks.json` 方式。

## Phase 3 候选方向

以下仅是 future work 候选，不是已完成能力或固定承诺：

### Skill ecosystem hardening

- `miclaw skills list`
- `miclaw skills lint`
- SKILL.md metadata validation

### MCP preparation

- MCP tool abstraction
- MCP permission integration
- Minimal MCP client

### 后续安全增强

- 更清晰的 sandbox backend abstraction
- 可选 container runtime
- 更丰富但仍可审计的 permission scope
- Trace summary 或按需 filtering/export

## 阶段结论

Phase 2 完成的是“受控执行与工作区权限基础”。MiClaw 现在可以在用户显式授权的 PROJECT workspace 中执行受 workspace containment、permission policy、interactive confirmation、session grant 和 shell hard safety 共同约束的文件及 shell 操作。

该阶段仍不意味着 MiClaw 已成为完整 sandbox、production-grade autonomous coding runtime 或 unrestricted coding agent。当前实现的价值在于形成了可测试、fail-closed、scope-aware 的执行链，并明确保留了未来扩展所需的安全边界。
