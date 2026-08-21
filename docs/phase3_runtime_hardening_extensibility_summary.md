# Phase 3 — Runtime Hardening & Extensibility

## 运行时安全强化与可扩展能力建设

MiClaw Phase 3 建立在前两个阶段之上：

- Phase 1：Safe + Observable Runtime Foundation，完成基础 sandbox、permission、`ToolResult`、JSONL logging、trace skeleton 和工程化基线。
- Phase 2：Controlled Execution & Workspace Permissions，完成 `ASK` confirmation、session grant、显式 PROJECT workspace 及 scope-aware permission policy。

Phase 3 的目标不是扩大默认权限，而是在引入更多 Tool、Skill 和 MCP 外部能力时，继续维持以下边界：

- permission 默认 fail closed；
- observability 输出有界，并在存储层和展示层分别做安全处理；
- Skill 保持 metadata discovery 与 full-content loading 分离；
- 文件与 shell 操作继续受 active workspace containment 约束；
- 外部进程、外部 Tool identity 和真实执行均经过显式、可审计的边界。

本阶段分为三个部分：Phase 3A Observability Hardening、Phase 3B Skill Ecosystem Hardening，以及 Phase 3C MCP Integration。

## Phase 3A — Observability Hardening

### 完成情况

| PR | 方向 | 实际完成内容 |
| --- | --- | --- |
| PR 20 | Redaction core | 新增 `miclaw/core/redaction.py`，提供可复用的 `is_sensitive_key()`、`sanitize_value()`、`redact_mapping()`、`summarize_tool_args()` 和 `summarize_content()`。它对明确 credential key、带前缀的 credential key、Bearer credential、危险 content field、深层结构、超大 collection、超长字符串和超大整数进行保守处理，并保证输出 JSON-friendly、失败时不回退 raw value。 |
| PR 21 | Agent audit event sanitization | Agent 写入 `tool_call`、`tool_result` 和 `ai_message` JSONL event 前，分别使用共享 redaction helper 处理 arguments、result content 和 model content。原有 event 类型及 `run_id` / `step_id` correlation 保留。 |
| PR 22 | Monitor display hardening | `miclaw monitor` 不信任上游日志已经安全。Legacy `result_summary`、legacy `ai_message`、tool-call args 和 unknown event 会在展示时再次经过有界摘要；arbitrary event dict 不会被直接 dump，Rich markup 会被 escape。`miclaw logs --tail` 与 `miclaw trace <run_id>` 复用同一套 CLI-safe event formatter。 |
| PR 23 | End-to-end regression | 新增从 agent event generation、JSONL write/read，到 monitor、logs CLI、trace CLI 的集成覆盖。测试包含 Bearer credential、prefixed credential key、sanitizer key collision、legacy unsafe log、超大整数、超深 nesting、超宽 collection、malformed event 和安全 metadata 保留。 |

### Redaction core 的实际规则

当前 baseline 会识别明确的 credential 字段，例如 `password`、`passwd`、`token`、`api_key`、`authorization`、`credential`、`private_key`、`access_key`、`client_secret`，以及 `openai_api_key`、`github_token`、`database_password` 等结构化后缀形式。匹配不区分大小写，但不会因为字段名包含普通片段就把 `token_count`、`output_tokens`、`queue_length`、`response_length`、`author` 或 `auth_status` 当作 credential。

`command`、`content`、`input`、`body`、`payload`、`stdout`、`stderr`、`result`、`output` 及当前实现定义的同类 content-bearing key 不直接保留原值，而是转换为 `*_present` / `*_length` 等摘要。Sanitizer 只保护自己实际会生成的 summary key，因此恶意输入不能用 `command_present` 或 `content_length` 覆盖安全摘要，普通 `queue_length` / `response_length` 仍可保留。

默认 hard limits 包括：普通字符串最多 200 字符、每层 collection 最多 20 项、递归深度最多 4 层、超过 256 bit 的整数使用固定 placeholder。达到边界时输出稳定的 omission/depth-limit 表示，不会 stringify 完整超大值。

Bearer credential 使用保守模式识别；当前对 credential 形态的纯字母 Bearer value 从 8 个字符起进行整体 redaction。它仍是启发式规则，不应被解释为完整 credential scanner。

### 存储层与展示层的双重边界

当前链路是：

```text
agent event
    → PR20 redaction / summary
    → JSONL storage
    → monitor / logs / trace display-time sanitization
```

PR 21 减少新 agent JSONL 中 raw tool arguments、raw tool result content 和 raw model content 的暴露；PR 22 则针对旧日志、手工构造日志或异常 event 再做展示层防御。展示安全不依赖“上游一定来自最新 Agent”。

需要明确区分：MCP 或 local Tool 的合法 runtime result 仍可能作为有界 `ToolResult.content` 返回给 Agent；Phase 3A 保护的是 audit/observability 路径，不是要删除 Agent 完成任务所需的合法工具返回值。

### 边界

Phase 3A 是 conservative observability redaction baseline，不是完整 DLP、secret management 或内容分类产品。它不能保证识别所有未知 credential 格式、自然语言中的敏感信息或业务特定 secret。安全原则是：命中明确规则时整体省略，处理失败时使用固定 placeholder，绝不以 raw input 作为异常 fallback。

## Phase 3B — Skill Ecosystem Hardening

### 完成情况

| PR | 方向 | 实际完成内容 |
| --- | --- | --- |
| PR 24 | `miclaw skills list` | 新增只读 Skill list 命令，复用 `LazySkillLoader` 的 metadata discovery/cache。命令只读取 metadata 所需的 `SKILL.md` 前缀，不调用 full-content loader，也不填充 full Skill registry。空目录、缺失目录和 metadata 不完整均安全处理；diagnostic 不显示用户 absolute path。 |
| PR 25 | `miclaw skills lint` | 新增只读 lint 命令，对 Skill structure、metadata readability、`name` 和 `description` 做基础诊断。ERROR 导致 exit code 1，只有 WARNING 时 exit code 0。Lint parser 与 runtime parser 使用同一字段匹配语义，`name : value` 不会被 lint 误判为 runtime 可识别格式。 |
| PR 26 | Structured metadata validation | 新增 frozen dataclass：`SkillMetadata`、`SkillMetadataIssue`、`SkillMetadataValidationResult`，将 parser、validator 与 lint 的语义统一。Validation result 提供稳定 issue code、severity、field、message、metadata 和 deterministic issue ordering；lint 只负责格式化结果，不再自行重复判断规则。 |

### Metadata 与 validation 语义

当前稳定 issue code 包括：

- `missing_skill_md`
- `invalid_skill_md`
- `unreadable_metadata`
- `invalid_encoding`
- `missing_name`
- `empty_name`
- `missing_description`
- `empty_description`

结构/readability 以及缺失或空 `name` 属于 ERROR；缺失或空 `description` 属于 WARNING。Human-readable message 可以解释问题，但 machine-readable code 不包含 absolute path、workspace root、Skill body 或 raw exception repr。

Runtime 与 validator 共用 `_match_metadata_field()` 的现有语法：`name: foo`、`description: text` 可识别；冒号前带空格的 `name : foo` / `description : text` 不被识别。Validation 报告声明 metadata 是否符合 runtime parser，而不是因为 runtime 有 folder-name/default-description fallback 就把 metadata 判为正常。

### Lazy loading 与 runtime compatibility

`skills list`、`skills lint` 和 metadata validation 都是 metadata-level 操作。它们不会调用 `_load_skill_content()`、不会执行 Skill，也不会将 validator 变成 runtime gate。Runtime 既有 fallback、首次实际使用时加载完整 Skill content、metadata TTL/cache 和 force-rescan 行为保持不变。

Phase 3B 没有实现 Skill install/uninstall、enable/disable、remote registry、dependency resolution、version manifest、strict runtime validation gate 或 usage statistics。

## Phase 3C — MCP Integration

### 完成情况

| PR | 方向 | 实际完成内容 |
| --- | --- | --- |
| PR 28 | MCP Tool adapter | 新增 transport-independent `MCPToolDescriptor` 和适配函数。标准 MCP wire field `inputSchema` 可直接使用；内部兼容 alias `input_schema` 可单独使用，但两者同时出现视为 ambiguous descriptor，统一返回 `conflicting_input_schema`。Descriptor 使用 `mcp::<server_id>::<tool_name>` 作为 canonical identity，schema 会 defensive deep copy、做 bounds/JSON-safety 检查并执行最小结构校验。该层不连接 server，也不执行 Tool。 |
| PR 29 | MCP permission integration | 复用既有 `PermissionCapability.MCP_TOOL`，对 qualified identity 有效的 `invoke` 使用 `HIGH` risk 和默认 `ASK` policy，其他 MCP 请求继续 fail closed。PermissionRequest 从已验证 descriptor 构造，保留 server/tool qualified identity，但不读取 arguments、schema 或 description。现有 confirmation、session grant 与 permission audit pipeline 被复用；该 PR 的 final `ALLOW` 仍不执行 MCP Tool。 |
| PR 30 | Minimal stdio MCP client | 使用官方 MCP Python SDK v2（依赖约束 `mcp>=2,<3`）和 modern protocol mode `2026-07-28`，实现显式 host config 下的 stdio process lifecycle、`tools/list`、`tools/call` 和 `ToolResult` mapping。Tools discovery 经过 PR 28 adapter；真实 call 必须先通过 PR 29 permission。Client 提供 timeout、bounded pagination、cursor-cycle 检测、duplicate identity 拒绝、有界 arguments/result、稳定错误 taxonomy、stderr discard 和 context-managed cleanup。 |
| PR 31 | Agent-facing registration | 新增 `MCPAgentTool`、稳定 agent-facing name、local/MCP tool merge 以及 `MCPAgentToolRuntime`。Programmatic host 可以在一个 async run scope 内启动显式配置的 server、完成 discovery、把 MCP wrapper 与现有 local tools 一起传入 `create_agent_app(tools=...)`，并由真实 LangGraph `ToolNode` 调用 PR 30 client。MCP server 在 run 内复用，退出或 discovery failure 时统一清理。 |

### Descriptor 与 schema boundary

`MCPToolDescriptor` 至少保存 `server_id`、tool `name`、bounded `description` 和 `input_schema`，不保存 transport/session/process handle 或 credential。不同 server 的同名 Tool 通过 qualified identity 区分；同一 server 的重复 identity 会 fail closed，不会 silent overwrite。

Input schema 的最低 boundary checks 为：root 必须是 mapping 且 `type == "object"`；`properties` 若存在必须是 mapping；`required` 若存在必须是 string list。`$schema`、`$ref`、`$defs`、`oneOf`、`allOf`、`additionalProperties`、`enum` 等未被 adapter 人为 allowlist 阻断，但 MiClaw 也没有在这里实现完整 JSON Schema semantic validation。

### Permission boundary

MCP authorization 流程为：

```text
validated MCPToolDescriptor
    → PermissionRequest(MCP_TOOL, invoke, HIGH, qualified identity)
    → existing permission policy: ASK
    → existing session grant / confirmation resolution
    → final PermissionResult
```

看起来像 `read`、`search` 或 `list` 的 MCP Tool 不会仅凭名称或 description 自动 ALLOW。当前没有 trusted MCP server model，也不会依据可选 annotation 把默认 `ASK` 降级为 `ALLOW`。

Session grant 继续使用现有精确匹配。`server-a/search` 的 grant 不会授权 `server-b/search` 或 `server-a/delete`；MCP grant 也不能授权 unrelated local capability。当前 grant 不做 argument hashing，因此隔离单位是 capability、operation、qualified tool/target、risk 等现有 permission scope，而不是单次 arguments 内容。

### stdio client 与 execution boundary

`MCPStdioServerConfig` 由 host 显式提供 `server_id`、executable `command`、string `args`，以及可选 `env` / `cwd`。启动不使用 `shell=True`，模型也不能通过 MCP Tool arguments 改写 server command、process args、cwd、env 或 server identity。

`MCPStdioClient` 通过官方 SDK async context manager 管理 subprocess 与 protocol lifecycle。`tools/list` 使用 SDK model serialization 的 wire alias，再交给 PR 28 adapter；最多读取 100 页，并拒绝 repeated cursor 或 incomplete/partial discovery。默认 stderr 写入 discard sink，不直接转发 external server stderr；额外 env 只有 host config 明确声明的值，测试也锁定未显式允许的父进程 secret 不会被传给 server。

Tool invocation 的关键顺序是：

```text
descriptor/client server identity validation
    → MCP permission evaluation and resolution
    → final ALLOW
    → bounded/JSON-safe arguments copy
    → SDK tools/call
    → bounded ToolResult mapping
```

`ASK` 未确认、confirmation DENY/exception/invalid result、policy DENY、descriptor/client server mismatch 都不会触发 `tools/call`。Text result 有长度上限；image、audio、resource 等非文本 block 使用稳定 placeholder，不把 binary/base64 直接塞入 model context。`is_error=True` 映射为失败 `ToolResult`，spawn/connection/protocol/timeout 等异常收敛为稳定 error type，不向用户回显 raw command、env、server stderr 或 protocol payload。

### Agent-facing registration

MCP wrapper 不直接访问 SDK session，只调用绑定的 `MCPStdioClient.call_tool()`，因此不能绕过 PR 29 permission。Agent-facing name 使用：

```text
mcp__<bounded-server-part>__<bounded-tool-part>__<qualified-identity-digest>
```

该名称 deterministic、符合常见 model Tool name 字符限制，并通过 digest 降低 normalized-name collision；canonical `mcp::<server_id>::<tool_name>` identity 仍保存在 descriptor/metadata mapping 中。Local tool、server A/tool 与 server B/tool 可以共存，任何最终 agent-facing name collision 都会 fail closed。

`MCPAgentToolRuntime` 的 lifecycle 由 programmatic host/run scope 持有：每个配置在 run 中创建一个 client，discovery 全部成功后才发布完整 tool set，同一 server 的多次 Tool call 复用该 client，run 退出或异常时统一 close。当前 MCP wrapper 只支持 async agent execution；同步 graph path 返回稳定的 `mcp_async_required` 错误，不调用 MCP server。

PR 31 的测试通过真实 test MCP stdio server 和最小 LangGraph agent path 验证：Agent 能看到 local + MCP tools，能调用 echo，permission 未最终 ALLOW 时 side effect 不发生，`ALLOW_ONCE` / `ALLOW_SESSION` 遵循既有语义，不同 server/tool grant 隔离，Tool error 以 ToolMessage 返回，client 在 run 后清理，agent audit/monitor/logs/trace 不显示注入的 argument/result secret。

当前没有面向用户的 MCP server 配置 CLI。Agent-facing MCP integration 需要 host programmatically 提供 `MCPStdioServerConfig`，并使用 async graph invocation；不会从 cwd、系统配置或模型输出自动发现、安装或启动 server。

## Phase 3 关键安全不变量

### Permission

1. Policy `DENY` 不能被 confirmation 或 session grant 覆盖。
2. 只有 policy `ASK` 会进入 grant lookup 或 confirmation。
3. `ASK` 无 matching grant、无 handler 时保持 blocked。
4. Handler 返回无效值、`ASK`、`None` 或抛出异常时 fail closed。
5. 文件、shell 或 MCP side effect 只允许在 final decision 为 `ALLOW` 后发生。

### Workspace

1. 未显式指定 PROJECT 时，active root 仍为 OFFICE。
2. PROJECT root 必须由调用方显式提供、canonicalize，并且路径必须存在且为 directory。
3. File target 与 shell cwd 必须保持在 active authorized root 内；absolute input、Windows drive path、解析后越出 root 的 traversal/prefix/sibling escape 和 symlink escape 继续被拒绝。
4. Workspace authorization 只决定“在哪里执行”，不等于 blanket operation permission。

### Observability

1. Raw tool args、tool result、model content 和 legacy event payload 均视为不可信。
2. 新 agent event 在写 JSONL 前做 storage-time sanitization；monitor/logs/trace 对读取内容再次做 display-time sanitization。
3. Sanitization failure 使用固定安全 placeholder，不回退 raw input。
4. 输出受字符串、collection、depth 和 integer bounds 约束。
5. 这些规则是 conservative baseline，不构成完整 DLP 保证。

### Session grants

1. Grant 只存在于当前 run 的内存 ContextVar 中，每个 run 创建 fresh internal set。
2. Run 结束后 reset，不写入 config、JSON、SQLite、workspace 或其他持久化存储。
3. OFFICE/PROJECT 通过 workspace scope 隔离；MCP 通过 capability、operation、server/tool qualified identity、target 和 risk 等 scope 隔离。
4. Session grant 不能覆盖 hard-blocked shell command 或 policy DENY。

### MCP

1. Tool discovery 不等于 execution permission。
2. Tool registration 不等于 execution permission。
3. Model 选择 MCP Tool 不等于 execution permission。
4. 只有 PR 29/30 产生的 final `ALLOW` 才能进入 SDK `tools/call`。
5. Server config 是 host-controlled boundary；模型不能控制 executable、process args、cwd、env 或替换 server identity。
6. Wrapper、descriptor 与 client 的 server identity 必须一致，不能跨 server 调用。
7. Sync Agent MCP path fail closed；当前真实 MCP execution 仅走 async wrapper。

### Skill

1. `skills list`、`skills lint` 和 validation 都是 metadata-level 操作。
2. Metadata inspection 不触发 full Skill content load 或 Skill execution。
3. Structured validation 是 diagnostic contract，不是 runtime strict gate。
4. Runtime fallback 和 lazy-loading cache 语义保持不变。

## 当前架构边界

### Tool 与 MCP 路径

```text
Agent / LangGraph
    |
    +-- Local Tools
    |
    +-- Lazy-loaded Skills
    |
    +-- MCP Agent Wrappers
            |
            v
      MCPStdioClient.call_tool()
            |
            +-- MCP Permission
            |      |
            |      v
            |   final ALLOW
            |
            v
      official SDK tools/call
            |
            v
      explicit MCP stdio server
```

Descriptor adaptation、permission、transport/execution 和 agent wrapper 分别位于 `mcp_adapter.py`、`mcp_permissions.py`、`mcp_client.py` 和 `mcp_tools.py`。这四层共享 identity 与调用顺序，但没有合并成第二套 Agent runtime。

### Observability 路径

```text
Agent events
    → redaction / bounded summary
    → JSONL
    → monitor / logs --tail / trace <run_id>
    → display-time sanitization and Rich escaping
```

Permission decision/confirmation 继续进入原有 audit pipeline；MCP 没有新增 raw request/response protocol logger。

## 明确未实现的能力与当前限制

Phase 3 没有实现以下能力：

- Docker/container sandbox 或完整 OS-level isolation。
- seccomp、jail、独立低权限用户等 shell execution backend。
- Persistent trusted workspace registry、跨 session workspace trust 或多个同时 active PROJECT roots。
- 任意 EXTERNAL root access。
- 完整 DLP、通用 secret scanner 或所有业务敏感信息的自动识别。
- MCP Streamable HTTP、SSE 或其他 HTTP transport。
- MCP OAuth、authentication flow 或 credential manager。
- MCP resources、prompts、sampling、elicitation、MRTR、tasks、subscriptions 或 dynamic tool-list notification。
- MCP server registry、persistent config、配置 CLI、auto-discovery、auto-install 或 model-controlled server launch。
- 多 server orchestration framework、health checking、retry/restart manager 或 hot reload。
- MCP sync Agent execution；当前 MCP wrapper 需要 async graph path。
- Skill install/uninstall、enable/disable、remote registry、dependency resolution、version manifest 或 usage statistics。
- Strict runtime Skill manifest validation gate。
- 完整 memory API/boundary 重构、retrieval policy、context budgeting 或 context selection redesign。
- Scheduler persistence/retry/history redesign。
- Full trace replay/debugger、checkpoint replay 或 tool re-execution。

当前 shell safety 仍是 conservative classifier、timeout、output bound 与 permission 的组合，不是真正的 process sandbox。显式 PROJECT workspace 也只建立 filesystem boundary，不代表任意 operation 自动获准。

## Phase 4 Handoff — Memory & Context Reliability

后续阶段可以围绕以下方向继续，但这些内容不属于 Phase 3 已实现能力：

- 明确 memory API、数据边界与写入责任。
- 定义 memory retrieval 的选择、时效和隔离语义。
- 建立 context selection 与 token/context budget 约束。
- 增加 memory injection、retrieval decision 和 context composition 的 observability。
- 验证不同 user、project、run 之间的 memory correctness 与 isolation。
- 在 memory/context 边界稳定后，再评估与 scheduler 的交互。

本总结不预设 Phase 4 的具体存储格式、数据库或 retrieval framework。

## 阶段结论

Phase 3 完成的是“运行时安全强化与可扩展能力建设”：MiClaw 现在拥有共享 observability redaction core、存储与展示双层安全边界、只读且保持 lazy loading 的 Skill listing/lint/validation，以及 programmatic、async、permission-gated 的 MCP stdio Tool discovery 与 Agent invocation 路径。

这并不意味着 MiClaw 已具备完整 sandbox、完整 DLP、成熟 Skill registry、通用 MCP platform 或 production-grade autonomous coding runtime。当前能力仍依赖显式 workspace、host-controlled MCP config、fail-closed permission、run-scoped grants、有界输出与测试覆盖共同维持安全边界。
