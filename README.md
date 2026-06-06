# MiClaw

MiClaw 是一个面向本地使用的透明 AI 智能体终端。它把对话、工具调用、文件沙盒、长期记忆、定时任务和运行监控放在同一个命令行体验里，适合用来构建可观察、可约束、可扩展的个人 AI 工作台。

## 亮点

- **终端即工作台**：通过 `miclaw run` 进入交互式对话，直接调度模型、工具和后台任务。
- **行为可观察**：运行日志以 JSONL 写入，并可通过 `miclaw monitor` 实时查看智能体正在做什么。
- **文件操作有边界**：读写文件和 Shell 执行默认限制在 office 工作区，降低误操作风险。
- **记忆可维护**：使用 Markdown 保存用户画像和长期偏好，方便审阅、迁移和手动调整。
- **技能可扩展**：支持从 `SKILL.md` 加载外部技能，并通过懒加载减少启动成本。
- **多模型接入**：支持 OpenAI、Anthropic、Ollama 以及 OpenAI-compatible 服务。

## 安装

建议使用 Python 3.10+，并在独立虚拟环境中安装：

```bash
cd /home/huanbai/project/MiClaw
python -m pip install -e .
```

安装完成后会注册 `miclaw` 命令。

## 配置

运行配置向导：

```bash
miclaw config
```

向导会写入项目根目录下的 `.env` 文件。常用配置项包括：

```env
DEFAULT_PROVIDER=openai
DEFAULT_MODEL=gpt-4o-mini
OPENAI_API_KEY=your_api_key
```

如需改变运行工作区，可设置：

```bash
export MICLAW_WORKSPACE=/path/to/workspace
```

未设置时，MiClaw 会使用项目根目录下的 `workspace/`。

工作区内会按用途自动创建目录：

- `memory/`：长期记忆和用户画像。
- `office/`：模型可访问的沙盒工位。
- `office/skills/`：外部技能目录。
- `tasks.json`：定时任务列表。
- `state.sqlite3`：对话状态和检查点数据。

## 运行

启动交互式智能体：

```bash
miclaw run
```

查看运行日志监控面板：

```bash
miclaw monitor
```

查看命令帮助：

```bash
miclaw --help
miclaw config --help
miclaw monitor --help
```

## 核心能力

- 模型提供商配置：通过配置向导保存默认 Provider、模型名、API Key 和兼容 Base URL。
- 对话智能体：基于 LangGraph 组织 Agent 循环、工具选择和上下文状态。
- 沙盒工具：提供 office 目录内的文件列表、读取、写入和 Shell 执行能力。
- 长期记忆：通过 Markdown 档案维护用户画像，支持在对话中主动更新。
- 定时任务：后台心跳循环检查 `tasks.json`，到点后把任务投递给智能体处理。
- 技能加载：从 `SKILL.md` 动态加载工具说明，支持懒加载和缓存刷新。
- 监控面板：读取 JSONL 事件日志并实时渲染模型输入、工具调用和输出状态。

## 项目结构

```text
MiClaw/
├── miclaw/              # 核心 Python 包
│   └── core/            # Agent、配置、上下文、工具、日志和任务循环
├── entry/               # CLI、交互入口和监控入口
├── examples/            # 使用示例与懒加载基准脚本
├── tests/               # pytest 测试
├── docs/                # 懒加载相关 Markdown 文档
├── requirements.txt
└── setup.py
```

## 测试

```bash
python -m pytest
```

可在安装后额外验证入口命令：

```bash
miclaw --help
miclaw config --help
miclaw monitor --help
```

## 使用建议

- 第一次运行前先执行 `miclaw config`，确认模型连通后再启动对话。
- 把需要智能体处理的文件放进 `workspace/office/`，避免让模型接触无关目录。
- 新增技能时，把技能说明写成 `SKILL.md` 并放入 `workspace/office/skills/<skill-name>/`。
- 如果运行状态异常，优先打开 `miclaw monitor` 查看最近的模型输入、工具调用和错误信息。
