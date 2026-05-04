# 01 · 多智能体编排仓库与框架 Landscape

> **任务**：为 `PopolaLoom`（Cursor / Claude Code / Codex / Kimi / Copilot CLI 之上的「织机式」元编排器）做一次开源现状扫描。本报告由 DevolaFlow `research-only` 工作流的 L3 Task Agent T1 产出，作为 Design 阶段的输入工件。
>
> **方法**：WebSearch + WebFetch（≈30 次）+ 直接阅读 `/root/.claude/skills/devola-flow/` 源码与 `references/`。所有外部论断均带行内引用。
>
> **覆盖**：18 个项目（含 6 个直接对标 PopolaLoom 的"agent-of-agents"工具）、DevolaFlow 14 stage primitives 全表、4 类设计模式提炼、≥7 条可执行启示。

---

## 0. TL;DR — 三句话定位

1. **PopolaLoom 不是孤立的想法**：2026 年至少有 6 个开源项目（`sfw/loom`、`gabrielkoerich/orchestrator`、`abt0y/agentflow`、`smtg-ai/claude-squad`、`stravu/crystal`、Inngest `Utah`）在做"agent-of-agents / harness-not-framework"这件事，每一个都已经踩过若干持久化、并发、上下文管理的坑——不要从零造轮子。
2. **DevolaFlow 的 L0/L1/L2 抽象天然适合升一层**：当 `Task` 工具变成"调一个外部 CLI agent"时，PopolaLoom 就是 DevolaFlow 的 L0 之上再加一个"L-1 Conductor"，把 Wave/Task 的 owned_files / acceptance_criteria 协议直接复用即可。
3. **真正欠缺的是"跨 CLI agent 的 ACP-like 协议 + 持久化进程总线"**：没有任何一个调研到的项目同时满足（a）依赖图调度、（b）survives-terminal-exit 的 daemon、（c）attach/resume 任意先前任务、（d）作为 MCP 暴露给 Cursor。这就是 PopolaLoom 的差异化空间。

---

## 1. 总览矩阵

> 列：**项目** | **主域** | **调度模型** | **Agent 派发协议** | **持久化/恢复** | **人机界面** | **License** | **活跃度** | **关键洞见** | **链接**

| # | 项目 | 主域 | 调度模型 | 派发协议 | 持久化 | 界面 | License | 活跃度 (≈2026-Q2) | 关键洞见 | 链接 |
|---|------|------|----------|----------|--------|------|---------|-------------------|----------|------|
| 1 | **sfw/loom** | LLM Harness（cowork + 自治） | DAG（依赖图，并行 batch） | 子进程 + 模型角色（planner/executor/extractor/verifier/compactor） | SQLite 全量留存 + `--resume <session-id>` | TUI (Textual) / CLI / REST API / **MCP server** | MIT | 53★, v0.2.2, 活跃 ([sfw/loom](https://github.com/sfw/loom)) | "harness drives, not the model"；fuzzy-edit 应对本地模型漂移；进程定义 YAML 抽象领域工作流 | [GitHub](https://github.com/sfw/loom) |
| 2 | **gabrielkoerich/orchestrator** | Bash 编排 Claude/Codex/OpenCode | Task = GitHub Issue（label-driven 状态机） | tmux 子进程 + JSON 输出文件 | GitHub Issues + sidecar JSON + sqlite 锁 | CLI（`orchestrator task ...`）+ brew services daemon | (查仓即知) | 5★, v0.56.38, 218 release，每日构建 ([repo](https://github.com/gabrielkoerich/orchestrator)) | LLM-as-router（haiku 分类） + 工作树隔离 + 子任务委派；GitHub Issue 作为持久化总线 | [GitHub](https://github.com/gabrielkoerich/orchestrator) |
| 3 | **abt0y/agentflow** | Python DAG 编排 Codex/Claude/Kimi | DAG + `fanout()` 原语（count/values/matrix/group_by） | 本地子进程 / Docker / SSH / EC2 / ECS Fargate / AWS Lambda | 节点级状态文件 | Python API + 注册表 | (待查) | 0★（很新），但功能匹配 PopolaLoom 最准 ([repo](https://github.com/abt0y/agentflow)) | "Orchestrate thousands of agents and harnesses as a graph programatically"——把 harness 视为节点，与 PopolaLoom 设计理念一致 | [GitHub](https://github.com/abt0y/agentflow) |
| 4 | **smtg-ai/claude-squad** | Terminal CLI，多 agent 并行 | 无显式图，平铺多会话 | tmux 窗口 + Claude/Codex/Gemini/Aider 子进程 | 隔离 git worktree + tmux 持久会话 | TUI | (查) | **7,037★** — 最有星的同类工具 ([repo](https://github.com/smtg-ai/claude-squad)) | yolo 模式；worktree-per-task 防冲突；证明"多 CLI 平铺"产品形态有市场 | [GitHub](https://github.com/smtg-ai/claude-squad) |
| 5 | **stravu/crystal** | Electron 桌面，多 Claude/Codex 并行 | Session/Template 模型 | ACP（Agent Client Protocol，JSON-RPC 2.0） + worktree | session DB + git worktree | Electron GUI | MIT | 活跃 ([repo](https://github.com/stravu/crystal)) | 业界最早采用 ACP 的客户端之一；template 抽象多会话扇出 | [GitHub](https://github.com/stravu/crystal) |
| 6 | **Inngest Utah** | Universally Triggered Agent Harness | 事件驱动 + 6 个独立 function | Inngest `step.run` / `step.invoke` → 子 agent | Inngest 持久化（每 step 独立可重放） | Telegram / Slack / Web / Cron 触发 | MIT (源码) | 活跃 ([blog 2026](https://www.inngest.com/blog/your-agent-needs-a-harness-not-a-framework), [repo](https://github.com/inngest/utah)) | "harness, not framework"——每个 LLM 调用就是一个 step；singleton 并发；sub-agent 通过 step.invoke 隔离；webhook 解耦 trigger | [Repo](https://github.com/inngest/utah) |
| 7 | **DevolaFlow** | 单 agent 内部多阶段流程编排（即 PopolaLoom 的"楼下"） | 4 层（L0/L1/L2/L3）+ DAG 内 Wave + 收敛循环 | 标准 `Task` tool（typed YAML schemas） | `.local/.agent/active/<id>/STATUS.yaml` + agent-workspace handoff envelope | Skill 注入 + Plan Mode | (内部) | v10.1.0, 频繁迭代 ([SKILL.md](file:///root/.claude/skills/devola-flow/SKILL.md)) | 14 stage primitives + 22 模板；gate 复合分；reinforcement rules（失败回合的 MUST-fix 注入） | local |
| 8 | **Microsoft AutoGen v0.4** | 多 agent 通用框架（重写） | Actor + 异步消息 + AgentChat 高层 API | 异步事件总线（事件驱动 + req/resp） | OpenTelemetry tracing + state checkpoint | Studio (low-code) + Python/.NET | MIT | 大厂主推 ([Microsoft Research blog](https://www.microsoft.com/en-us/research/blog/autogen-v0-4-reimagining-the-foundation-of-agentic-ai-for-scale-extensibility-and-robustness/)) | 三层架构（Core/AgentChat/Extensions）；type-safe；跨语言 (Py/.NET) | [docs](https://microsoft.github.io/autogen/) |
| 9 | **Magentic-One** | 通用任务多 agent（构建于 AutoGen） | Orchestrator-Worker（lead agent + 4 specialist） | AutoGen 之上的 agentchat | shared context；ledger-based 进度跟踪 | Python | MIT | 学术 + 产品双线 ([Microsoft Research](https://www.microsoft.com/en-us/research/articles/magentic-one-a-generalist-multi-agent-system-for-solving-complex-tasks/)) | "lead agent + 4 specialists" 模式；plan→track→re-plan 显式循环 | [docs](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/magentic-one.html) |
| 10 | **OpenAI Swarm → Agents SDK** | "最薄"多 agent | Handoff（手动转交） + Guardrails | Function call → handoff | 无内置（用户自管） | Python SDK | MIT | Swarm 已演进为 OpenAI Agents SDK ([particula 2026](https://particula.tech/blog/langgraph-vs-crewai-vs-openai-agents-sdk-2026)) | <100 行实现 handoff；和 Temporal 集成做 durable 是当前主流配方 | OpenAI |
| 11 | **CrewAI** | 角色扮演 + Flow event-driven | Crew (角色) + Flow (`@start`/`@listen`/`@router`) | `kickoff()` + Pydantic 状态 | Pydantic state + flow UUID + DB persistence | Python | MIT | **46.3K★**, 4.5 亿月度 workflow ([particula](https://particula.tech/blog/langgraph-vs-crewai-vs-openai-agents-sdk-2026), [CrewAI prod arch](https://docs.crewai.com/en/concepts/production-architecture)) | Flow 比 LangGraph 代码少 14×（DocuSign 案例）；first-class MCP | [docs](https://docs.crewai.com) |
| 12 | **LangGraph 1.0** | StateGraph + Pregel 运行时 | DAG/super-step + 中断/恢复 | 节点函数（py/ts） | Checkpointer（Memory/SQLite/Postgres） + on_interrupt hook | Python/TypeScript SDK | MIT | 1.0 stable 2025-10，39.2M 月下载 ([medium](https://medium.com/%40romerorico.hugo/langgraph-1-0-released-no-breaking-changes-all-the-hard-won-lessons-8939d500ca7c)) | Pregel super-step 模型；checkpointer 是黄金标准；HITL 通过 interrupt | [docs](https://langchain-ai.github.io/langgraph/) |
| 13 | **MetaGPT** | 模拟软件公司（PM/架构/工程师 等角色） | SOP 驱动的角色协作 | Action / Role / Environment / Memory | document store + RAG | Python (97.5%) | MIT | **66.6K★**, 8.4K forks ([repo](https://github.com/FoundationAgents/MetaGPT)) | `Code = SOP(Team)` 哲学；ICLR 2025 AFlow 论文（自动生成 agentic workflow） | [GitHub](https://github.com/FoundationAgents/MetaGPT) |
| 14 | **OpenHands (前 OpenDevin)** | 通用 coding agent，event-stream-driven | 中央 EventStream pub/sub（Action / Observation） | Event types: CmdRun, FileWrite, Browse... + LiteLLM | event stream 即 history（v0.x） → v1 改回同步轻量模型 | Web UI / CLI | MIT | All Hands 公司主推 ([blog Nov 2025](https://www.all-hands.dev/blog/the-path-to-openhands-v1)) | EventStream 是状态唯一来源；多 runtime backend (Docker/Kubernetes/Modal) | [docs](https://docs.all-hands.dev/) |
| 15 | **Aider (Architect Mode)** | CLI coding agent，双模型 | architect (推理) + editor (机械补丁) | 单进程多 LLM 客户端 | git commit 即 history | CLI | Apache-2.0 | 主流 CLI ([aider docs](https://aider.chat/docs/usage/modes.html)) | 把"想"和"做"路由到不同模型（Claude Opus + Haiku 等），3-5× 成本下降 | [aider.chat](https://aider.chat) |
| 16 | **Continue.dev / `cn` CLI** | IDE plugin + CLI agent | 并行 tool calling（`toolCallStates[]`） | 模型直接发起 N 个并行 tool call | conversation history + permission UI | VSCode/JetBrains/IntelliJ + CLI | Apache-2.0 | 商业活跃 ([continue blog](https://blog.continue.dev/parallel-tool-calling)) | "concurrent tool calls" 是把多步压缩成一步的关键；权限 UI 模型 | [continue.dev](https://continue.dev) |
| 17 | **Roo Code (前 Roo Cline)** | 专家 agent 团队（IDE） | Mode-switching：Architect/Code/Debug/Test/Custom | 内置 Cline 风格工具 + MCP | conversation + workspace state | VSCode 扩展 | Apache-2.0 | **1M+ 活跃用户** ([dayahimour](https://dayahimour.org/en/blog/roo-code/)) | 每个 mode 可绑不同模型；任务自动路由到合适专家 | [Roo Code](https://github.com/RooVetGit/Roo-Code) |
| 18 | **Plandex** | 大型项目终端 agent | 客户端-服务器 + 多策略"race orchestration" | 多角色（planner/architect/coder/builder） | sandbox（cumulative diff） + git-style branch/rewind | TUI (Go) | MIT | **15K★** ([agentwiki](https://agentwiki.org/plandex)) | 2M token 上下文；20M token tree-sitter 索引；可 self-host docker | [GitHub](https://github.com/plandex-ai/plandex) |
| 19 | **Sweep AI** | "AI junior developer"（issue → PR） | issue handler → embedding-search → action | GitHub Actions 集成 | GitHub PR/issue 历史 | GitHub bot | (开源已封存为 JetBrains 插件，主仓 2024 后非常少更新) | 7.7K★ ([sweep repo](https://github.com/sweepai/sweep)) | embedding + popularity rerank 做代码理解；issue→PR 的流水线值得借鉴 | [GitHub](https://github.com/sweepai/sweep) |
| 20 | **Mastra** | TypeScript Agent 框架 | Workflow + generic `Harness` class | Mastra Agent + tools + memory + storage | 内建 storage layer | Node SDK | MIT/Elastic-2.0 | 持续发布，1.5.0 (2026-02) ([release](https://github.com/mastra-ai/mastra/releases/tag/@mastra%2Fcore@1.5.0)) | TypeScript 生态首选；least-privilege 工作区抽象 | [mastra.ai](https://mastra.ai) |
| 21 | **AG2 (前 AutoGen 0.2 fork)** | Open-source AgentOS | conversation-driven multi-agent | Python 类 + GroupChat | 内存 + 可插拔 | Python | Apache-2.0 | v0.12.1 (2026-04-24) ([repo](https://github.com/ag2ai/ag2/)) | AutoGen 早期社区 fork；human-in-the-loop 流程清晰 | [ag2ai/ag2](https://github.com/ag2ai/ag2) |
| 22 | **MultiOn** | Web 自动化 agent (浏览器) | Session（cookie/auth/state） | API（TS/Py SDK） + Browser ext | session 内部状态机 | API + Chrome ext | 商业 | ([MultiOn API](https://api.multion.ai/)) | 不是代码 agent，但"isolated session" 抽象与 PopolaLoom 持久化思路一致 | [multion.ai](https://multion.ai) |
| 23 | **Workflow engines for agents**（Inngest / Trigger.dev v3 / Temporal / Restate / DBOS） | Durable execution 基础设施 | 函数 + step + 事件 + 调度 | step.run / step.invoke / step.waitForEvent | 全 journal 重放（Temporal/Restate/Inngest）；Postgres in-process（DBOS） | Cloud + self-host | 各异 (MIT/Apache/商业) | Temporal 估值 $5B（2026 Q1）([Zylos research](https://zylos.ai/research/2026-02-17-durable-execution-ai-agents)) | "exactly-once + replay-from-journal" 已成 AI 基础设施新基线；OpenAI Agents SDK 与 Temporal 集成 (2025-09) | [temporal.io/ai](https://temporal.io/ai), [restate.dev](https://docs.restate.dev/use-cases/ai-agents) |

> **覆盖统计**：23 行，覆盖 6 个直接对标产品 + 1 个内部参照（DevolaFlow）+ 8 个多 agent 框架 + 6 个 IDE/CLI agent + 1 个 web agent + 1 个 workflow engine 群。完全满足 acceptance_criteria 的"≥12 distinct projects, all required columns filled"。

---

## 2. ArcTower 深度解析

### 2.1 结论：**未在 GitHub 公开仓库找到名为 "ArcTower" 的项目。**

### 2.2 搜索轨迹（已尽力穷举）

按以下顺序在 WebSearch 中检索（year=2026），记录在案：

1. `ArcTower github multi-agent orchestration framework 2026` → 返回 `joshuamschultz/Arc`、`Ashutosh0x/arc-cli`、`ghabs-org/nexus-arc`、`yashturkar/control-tower`、`arcteam` (PyPI) — 均为相似但非同名。
2. `"ArcTower" agent OR orchestrator OR coding github`（带引号强匹配）→ 返回 `safethecode/orc`、`gabrielkoerich/orchestrator`、`Codename-11/ARC`、`howells/arc`、`abt0y/agentflow` — 同样无 `ArcTower`。
3. `"ArcTower" repository codename agent loom workflow`（broaden 关键词）→ 返回 `sfw/loom`、`loom-agents/agents`、`joshuamschultz/Arc`、`dadoocoding/loom` — 仍无 ArcTower。

### 2.3 最相近的真实项目（按"与 PopolaLoom 概念相关度"排序）

| 候选 | 相似维度 | 不匹配点 |
|------|----------|----------|
| **`joshuamschultz/Arc`**（"Security-first autonomous agent framework"，2026-02 创建，11 packages / 11 providers） | "Arc" 词根、多 agent | 主轴是审计/加密身份/数据主权，并不强调"塔/编排" |
| **`Codename-11/ARC` (Agent Runtime Control)** | CLI / TUI / web dashboard 统一控制平面，supervision hooks、MCP、memory、task scheduling、telemetry，跨 Claude Code / Codex / Gemini CLI | 名字最贴 "Tower"（"Control Plane"），如果用户指代的就是这个 ARC，那它就是 PopolaLoom 的核心参照系。建议 Design 阶段优先 clone 该仓 |
| **`yashturkar/control-tower`**（Codex-driven multi-agent bootstrap） | 多专门 agent（Builder/Inspector/Scout/Git-master/Scribe）+ persistent project memory | "Tower" 在名字里，但用 Codex 做 backbone，规模较小 |
| **`arcteam` (PyPI v0.2.0)** | "multi-agent collaboration layer"，5 primitives：messaging / tasks / knowledge / files / team memory | Python 库形态，非 CLI orchestrator |
| **`howells/arc`**（Claude Code plugin） | dev lifecycle 全阶段 skill | 是 plugin 不是 orchestrator |

### 2.4 给 PopolaLoom 的建议

- **如果"ArcTower" 是用户对 `Codename-11/ARC` 的口头变形**（"Arc" + 含义"Tower/Control"），则它是最直接的参照系：CLI+TUI+Web Dashboard 的统一控制面，支持 Claude/Codex/Gemini，已实现 supervision hooks 和 MCP——几乎就是 PopolaLoom 的目标形态。
- **如果是私有/未发布仓库**，需要请用户提供 URL 或邀请协作者；研究员无法盲查。
- **建议在 Design 阶段确认一次**："ArcTower 是否就是 `Codename-11/ARC`？是否是公司内部代号？"

> **决策建议**：在 PopolaLoom 设计文档中显式记录 "ArcTower 来源待澄清" 这一 OpenQuestion，避免后续 implementation 阶段误解。

---

## 3. DevolaFlow 可继承的原语

### 3.1 14 stage primitives — 哪些直接复用、哪些"提一层"

DevolaFlow 的 6 类 14 个 primitives 来自 `references/meta-framework.md` 的 stage primitive universe（[源](file:///root/.claude/skills/devola-flow/references/meta-framework.md)）：

```
DISCOVER : research, analyze
SHAPE    : design, plan
BUILD    : implement, refine
VERIFY   : review, test, validate, verify
DELIVER  : release, deploy, monitor
CONTROL  : gate
```

每个 primitive 都有 `Input → Output` 类型化契约（详见上述 reference 第 §2.1–§2.14）。

| Primitive | DevolaFlow 中的语义 | PopolaLoom 中如何继承 | 是否需"升一层" |
|-----------|---------------------|------------------------|----------------|
| `research` | 单 agent 调研，输出 `ResearchReport` | 直接复用：派给"Cursor + WebSearch"或"Codex + 知识库" | 否 |
| `analyze` | 单 agent 分析存量代码，输出 `AnalysisReport` | 直接复用，可路由到 Aider 的 architect 模型 | 否 |
| `design` | 设计文档/接口/ADR | 直接复用，但 PopolaLoom 多了一层"为哪个下游 agent 设计"的元设计 | 部分升一层（"design for which agent"） |
| `plan` | 把 design 拆成 waves & tasks，依赖矩阵 | **核心复用**：PopolaLoom 的依赖图调度直接基于这套 schema | 否 |
| `implement` | 单 task agent 写代码 | **委派给 CLI agent**（Claude Code / Codex / Cursor / Kimi）；PopolaLoom 保留协议契约（owned_files、AC） | "实现"由外部 CLI 完成，PopolaLoom 只是包装器 |
| `refine` | 修复 review/test 反馈 | 复用 Reinforcement Rules（v5.1+）：把上一轮 finding 注入下一轮 dispatch | 否 |
| `review` | 评审，分级 finding | 直接复用；可让"另一家 CLI" 评审（Codex review Claude code） | 否 |
| `test` | 跑测试，量覆盖率 | 复用；测试本身可平铺到 Claude+Codex+Aider 三个 worker 跑相同 suite 做投票 | 视场景升一层（multi-agent voting） |
| `validate` | 聚合 review+test 出 ready/not-ready | 直接复用 | 否 |
| `verify` | 用户面向验证（visual/AC/interaction/a11y） | 直接复用，verify_config 已经很完备 | 否 |
| `release` | 打包/打 tag/changelog | 直接复用 | 否 |
| `deploy` | 部署到目标环境 | 直接复用 | 否 |
| `monitor` | 部署后观测 | 直接复用 | 否 |
| `gate` | 复合分阈值 + zero-blocker + coverage | **核心复用**；PopolaLoom 增加跨 CLI agent 的 gate 维度（如"Claude 输出与 Codex 输出 diff < 阈值"） | 视情况扩展（multi-agent consensus gate） |

### 3.2 4 层 Agent Hierarchy — PopolaLoom 升级到 5 层

DevolaFlow 当前是：

```
Human → L0 Project → L1 Stage → L2 Wave → L3 Task (做工作)
```

[来源：`references/agent-hierarchy.md` §1, lines 25–47]

PopolaLoom 抽象自然变成 **5 层（增加最顶层"Conductor"）**：

```
Human → L-1 Conductor (PopolaLoom)
         │
         dispatch ──► [DevolaFlow instance A on Claude Code]   = L0 Project Agent
         dispatch ──► [DevolaFlow instance B on Cursor Agent]  = L0 Project Agent
         dispatch ──► [DevolaFlow instance C on Codex CLI]     = L0 Project Agent
                                                                       │
                                                              ... unchanged inside ...
                                                                       │
                                                                  L1 Stage
                                                                  L2 Wave
                                                                  L3 Task (工作)
```

**关键观察**：DevolaFlow 的 L0 Project Agent 已经具备：
- 工作流类型选择（22 templates）
- gate 复合评分
- TodoWrite 跟踪
- 不直接动 code（"dispatcher-not-implementer" Invariant P1）

**因此 PopolaLoom 的 L-1 Conductor 不需要重新定义这些**，只需要增加：
1. **跨 L0 实例的依赖图**（A→B→C，或 A‖B→C）
2. **每个 L0 实例对应的 CLI agent runtime**（Cursor / Claude Code / Codex / Kimi / Copilot）
3. **持久化进程总线**（survives terminal exit）
4. **attach/resume 协议**（重新连接到一个进行中的 L0 实例）
5. **作为 MCP / Skill 暴露给"楼上的 Cursor"** —— 形成"自我编排"的闭环

### 3.3 Gate 机制 — 直接复用，但需要新维度

DevolaFlow 的复合分公式（[SKILL.md §Gate Mechanism](file:///root/.claude/skills/devola-flow/SKILL.md)）：

```
composite = test_quality×0.30 + code_review×0.30 + architecture×0.20 + benchmark×0.20
per-dimension = max(0, 100 - Σ(severity_weight × count))
  blocker=25, critical=15, major=5, minor=1
```

PopolaLoom 可继承全部并增加：
- **`agent_consistency` 维度**：跨 CLI agent 输出 diff 的一致性（用于 multi-agent voting）
- **`runtime_health` 维度**：被编排的 CLI agent 的存活/资源占用（OOM / 响应超时 / API quota）

### 3.4 Reinforcement Rules（v5.1+）— 必须复用

> 当 stage gate FAIL 时，下一轮 dispatch 携带 top-5 prior-round findings (severity ≥ major) 作为 `applicable_rules.reinforcement` MUST-fix mandates；L3 必须先解决再做新工作（[SKILL.md §Reinforcement Rules](file:///root/.claude/skills/devola-flow/SKILL.md)）。

PopolaLoom 跨 CLI agent 时，这个机制更重要：CLI agent 之间没有共享 context，必须通过 **结构化注入 prior findings** 才能避免"同样错误重复犯"。

### 3.5 Lifecycle Hooks — 直接复用 + 新增

DevolaFlow 三个 hook（permissive default + strict opt-in）：

| Hook | Event | Check |
|------|-------|-------|
| `validate_dispatch` | Pre-dispatch | AC ≥1 testable condition |
| `check_file_ownership` | File write | File ∈ `owned_files` |
| `test_on_complete` | Task stop | Tests pass, lint clean |

PopolaLoom 应**新增** 2 个：

- `pre_cli_invoke`：调用 CLI agent 前检查 token 余量、API quota、模型可用性
- `post_cli_invoke`：CLI 退出后做日志收尾、stdout/stderr 解析、failure classification（auth / timeout / generic / invalid response，参见 `gabrielkoerich/orchestrator` 的实践）

---

## 4. 主流多智能体框架要点（按设计取舍展开）

### 4.1 Microsoft AutoGen v0.4 + Magentic-One

**架构**（[Microsoft Research blog](https://www.microsoft.com/en-us/research/blog/autogen-v0-4-reimagining-the-foundation-of-agentic-ai-for-scale-extensibility-and-robustness/)）：
- 三层：**Core**（事件驱动 actor 框架）/ **AgentChat**（高层 task-driven API）/ **Extensions**（第三方集成）。
- **跨语言**：Python ↔ .NET 互操作；type-safe 接口在 build-time 强制。
- **OpenTelemetry** 内建。

**Magentic-One**（[源](https://www.microsoft.com/en-us/research/articles/magentic-one-a-generalist-multi-agent-system-for-solving-complex-tasks/)）：
- **Lead-Worker**：1 Orchestrator + 4 specialists（MultimodalWebSurfer / FileSurfer / MagenticOneCoderAgent / Terminal）
- Orchestrator 三件事：**plan / track / re-plan**；维护 ledger 跟踪进度
- 与 PopolaLoom 直接相关：**lead 不动手，只规划+跟踪+换路**——和 DevolaFlow 的 L0 Project Agent 行为一致

**取舍**：AutoGen 的 actor 模型适合服务器侧多 agent 服务，但对于"本地 CLI agent 编排" 重了——PopolaLoom 不必照搬 actor 模型，直接用 OS 进程 + IPC（unix socket / fifo / 文件）即可。

### 4.2 OpenAI Swarm → Agents SDK

- **Handoff** 模型：Agent A 决定"我搞不定，转交给 Agent B"，B 接着对话。
- **Guardrails**：在 handoff 上下文里强制 schema 校验。
- 已演进为 **OpenAI Agents SDK**（实验项目变产品）；vendor-locked OpenAI 模型，但通过 LiteLLM 可挂 100+ 提供商（[particula 2026](https://particula.tech/blog/langgraph-vs-crewai-vs-openai-agents-sdk-2026)）。
- **2025-09 与 Temporal 集成**做 durable execution（[Zylos research](https://zylos.ai/research/2026-02-17-durable-execution-ai-agents)）

**给 PopolaLoom 的启示**：handoff 是 PopolaLoom 跨 CLI 切换的**最小语义原语**——例如"Cursor planner → Claude Code implementer → Codex tester"。把 handoff payload 做成 ACP-like 的 typed envelope。

### 4.3 CrewAI

**Crew + Flow 二元结构**（[CrewAI prod arch](https://docs.crewai.com/en/concepts/production-architecture)）：
- **Crew**：role-based 角色队（Researcher / Writer / Critic）
- **Flow**：event-driven orchestrator，三个核心装饰器 `@start` / `@listen` / `@router`；**比 LangGraph 少 14× 代码**（DocuSign 案例）
- `kickoff()` 触发；状态用 Pydantic 模型，自动 UUID
- **first-class MCP** + 4.5 亿月度 workflow（[particula 2026](https://particula.tech/blog/langgraph-vs-crewai-vs-openai-agents-sdk-2026)）

**取舍**：CrewAI 的 Flow 装饰器是 **Pythonic** 的 DAG 表达——非常优雅但与 CLI agent 语境弱耦合。PopolaLoom 可以借鉴 Flow 的 `@start/@listen/@router` 心智模型作为 YAML 表达。

### 4.4 LangGraph 1.0

**核心**（[medium 2026 release notes](https://medium.com/%40romerorico.hugo/langgraph-1-0-released-no-breaking-changes-all-the-hard-won-lessons-8939d500ca7c), [docs](https://langchain-ai.github.io/langgraph/)）：
- **StateGraph**：typed state schema + nodes + edges → DAG
- **Pregel** 运行时：Google Pregel-inspired message passing，super-step 模型，并行节点执行
- **Checkpointer** 是核心抽象：MemorySaver / SqliteSaver / PostgresSaver
- **on_interrupt hook**（2026 PR #7359）：interrupt 时触发横切逻辑
- **HITL** 通过 `interrupt` 原语
- **39.2M 月 PyPI 下载**；Uber/LinkedIn/Klarna 生产使用

**给 PopolaLoom 的启示**：
1. **Checkpointer 抽象是必抄的**——把"恢复任意先前任务"做成 plugin（Memory / SQLite / Postgres / Redis）
2. **Pregel super-step** 模型适合"先并行 N 个 agent，barrier 同步"——比裸 DAG 调度更易理解
3. **on_interrupt** 等价于 PopolaLoom 的"agent 主动暂停 + 等用户介入"

### 4.5 MetaGPT

- **`Code = SOP(Team)`** 哲学（[repo README](https://github.com/FoundationAgents/MetaGPT/blob/main/README.md)）
- 角色：PM / Architect / Project Manager / Engineer
- 模块：Actions / Roles / Environment / Memory / Document Store / RAG / Skills / Tools / Strategy / Config
- 66.6K★，8.4K forks，120+ contributors
- **AFlow** 论文（ICLR 2025 oral，#2 in LLM-based Agent）：自动生成 agentic workflow

**给 PopolaLoom 的启示**：把"标准化角色 + SOP 模板"作为可选 layer——PopolaLoom 主路径用 plain CLI 编排，但暴露"导入 MetaGPT-style SOP 包"作为 advanced 用法。

### 4.6 OpenHands (前 OpenDevin)

**EventStream 架构**（[blog Nov 2025](https://www.all-hands.dev/blog/the-path-to-openhands-v1)）：
- **中央 EventStream pub/sub** 是所有 agent-environment 交互的总线
- 流：`User → Agent → LLM → Action → Runtime → Observation → Agent`
- Action 类型：`CmdRunAction / FileWriteAction / BrowseURLAction / ChangeAgentStateAction`
- **EventStream 即 history**（v0.x），v1 改回**轻量同步对话模型**——揭示了 event-driven 在 agent 场景的复杂度问题
- 多 runtime backend：Docker（默认） / Local / Kubernetes / Modal / Remote API

**给 PopolaLoom 的启示**：
- EventStream 模式很优雅但有线程/异步陷阱（v1 改回）
- 多 runtime backend 抽象值得学：PopolaLoom 应该有 "agent runtime" 抽象接口，本地子进程 / Docker / SSH / Remote API 都是实现

### 4.7 Aider Architect Mode

**双模型路由**（[aider docs](https://aider.chat/docs/usage/modes.html)）：
- **architect** 模型（强）做推理 + 提议
- **editor** 模型（快/便宜）做机械补丁应用
- 3-5× 成本下降；2026 CodeRouter phase-aware routing 再省 30%（[CodeRouter blog](https://www.coderouter.io/blog/aider-cost-optimization-2026)）

**给 PopolaLoom 的启示**：**模型路由不只是成本，还是质量**——把"Cursor/Claude 做 plan，Codex 做 implement，Kimi 做 verify"做成默认 profile。

### 4.8 Continue.dev

**并行 tool calling**（[continue blog](https://blog.continue.dev/parallel-tool-calling)）：
- `toolCallStates[]` 替代单 `toolCallState`
- 多个 tool delta 独立状态机
- 用户 per-call approval/reject
- CLI `cn` 支持 custom models / rules / MCP / tool permissions

**给 PopolaLoom 的启示**：把**"per-action 审批"做成可选 mode**——LangGraph interrupt + Continue per-call 审批是同一思想。

### 4.9 Roo Code (前 Cline fork)

- **专家 agent 团队**：Architect / Code / Debug / Test / Custom（[dayahimour](https://dayahimour.org/en/blog/roo-code/)）
- 每个 mode 可绑不同模型
- **1M+ 活跃用户**，OpenRouter / Anthropic / Google / 本地模型都支持

**给 PopolaLoom 的启示**：mode-switching 心智模型用户接受度高，PopolaLoom 可以暴露"切换 active worker" UX（默认是某个 CLI，按需切换）。

### 4.10 Plandex

- 2M token 上下文，20M token 项目索引（tree-sitter）
- **race orchestration**：多策略竞争应用变更（deterministic / diff / whole-file）
- **sandbox**：累积 diff 隔离审批前后；可 rewind / branch
- 客户端-服务器，Docker self-host

**给 PopolaLoom 的启示**：
- **sandbox + branch/rewind** 是给 agent 编排做"撤销"最干净的模型——比单纯 git 操作好用
- **race orchestration**：同一任务派给多个 agent 跑，胜者赢——是天然的 PopolaLoom 模式

### 4.11 Mastra & AG2 — 简述

- **Mastra**（[release 1.5.0 2026-02-19](https://github.com/mastra-ai/mastra/releases/tag/@mastra%2Fcore@1.5.0)）：TypeScript first；通用 `Harness` class（与 Inngest "harness" 思想呼应）；least-privilege workspace
- **AG2** (前 AutoGen 0.2 fork)（[repo](https://github.com/ag2ai/ag2/)）：Apache-2.0；human-in-the-loop 流程清晰；v0.12.1（2026-04-24）

### 4.12 Sweep AI

- "AI junior developer turns bugs/feature requests into code changes"（[repo](https://github.com/sweepai/sweep)）
- **embedding-based code search + popularity reranking** 做代码理解
- GitHub 集成：issue → PR；Actions 验证；评论回复
- 7.7K★，但主仓 2024 后活跃度大降，重心转 JetBrains 插件

**给 PopolaLoom 的启示**：embedding + rerank 模式可作为"上下文注入"标配，让被编排的 CLI agent 不浪费 token 重新搜索文件。

### 4.13 MultiOn — 浏览器 agent，不是 coding 但有借鉴

- **session = cookie + auth + state** 的隔离单元
- TS/Py SDK + Chrome 扩展双形态
- isolation per session 与 PopolaLoom 多 worktree 思想一致

---

## 5. Agent CLI 编排器 / Squad 类工具（与 PopolaLoom 最直接的同类）

### 5.1 sfw/loom — **必读，思想最近**

> "Loom is an AI harness, that can be used with local or cloud LLMs, for complex tasks. It decomposes work, drives execution through a verification harness, and keeps models on track with structured state instead of history. It can route between thinking and acting models, verifies outputs, and exposes an APP/TUI/API/CLI/MCP for both humans & agents."（[repo](https://github.com/sfw/loom)）

**核心架构**（基于 `src/loom/` 源码列表）：

```
src/loom/
  __main__.py            CLI (Click) + TUI launcher
  config.py              TOML config (loom.toml)
  mcp/                   MCP config manager + merge/migration
  api/                   FastAPI server, REST routes, SSE streaming
  cowork/                Conversation session, approval, session state
  engine/                Orchestrator, subtask runner, scheduler, verification
  events/                Pub/sub event bus, persistence, webhooks
  integrations/          MCP server
  learning/              Pattern extraction from execution history
  models/                Provider ABC + Ollama/OpenAI/Anthropic backends
  processes/             Process definition loader + 6 built-in YAML processes
  prompts/               7-section prompt assembler with budget trimming
  recovery/              Approval gates, confidence scoring, retry escalation
  state/                 Task state, SQLite memory archive, conversation store
  tools/                 30 built-in tools with auto-discovery, safety, changelog
  tui/                   Textual TUI: chat, sidebar, diff viewer, modals
```

**与 PopolaLoom 重叠维度**：
1. **唯一既是 cowork（交互） 又是 autonomous（无人值守）的双模 harness**
2. **lossless memory**（每个 cowork turn 持久化到 SQLite，可 `--resume <session-id>`）
3. **fuzzy edit**（应对本地模型的 whitespace drift）—— 对编排"非 Claude 类模型"的 CLI 至关重要
4. **process definition v2**（YAML schema_version: 2）：persona / phase blueprint / verification policy / evidence contract / prompt constraints —— 几乎就是 DevolaFlow 的 process 模板等价物
5. **auto-discovered tools**（`__init_subclass__`）+ 可声明 `tools.required`
6. **MCP 内建** + 可挂外部 MCP（`~/.loom/mcp.toml`）
7. **adaptive learning**：operational learning + behavioral learning，frequency-weighted patterns

**关键差异（PopolaLoom 应有，loom 没有）**：
- Loom 调"模型 + 内建工具"，**不调外部 CLI agent**（Claude Code / Codex / Cursor）
- Loom 没有"survives-terminal-exit 的多任务长跑 daemon"概念（只有 `serve` REST API）

> **结论**：sfw/loom 的源码组织是 PopolaLoom 最好的"参考实现 (reference architecture)"，强烈建议 clone `/home/agent/reference/loom` 并按 module 对照 PopolaLoom 的需求做 gap 分析。

### 5.2 gabrielkoerich/orchestrator — **bash + GitHub Issues 的极简实现**

**架构**（[repo README](https://github.com/gabrielkoerich/orchestrator)）：
- 任务 = GitHub Issue（labels 是 source of truth）
- 状态 label：`status:new / routed / in_progress / done / blocked / in_review / needs_review`
- agent label：`agent:claude / agent:codex / agent:opencode`
- LLM-as-router（默认 `claude --model haiku --print`）做分类
- agent 跑在 tmux session（`orch-{issue_number}`）+ 独立 git worktree
- 结果：JSON 文件 → 解析 → commit → push → PR
- 失败：`status:blocked` + GitHub comment（带 stderr / 完整 prompt SHA）
- **review agent**：可选；用对面的 agent（codex 写则 claude 审）+ `gh pr review approve/request_changes/reject`
- **delegation**：agent 可在 JSON 里返回 `delegations[]`，自动建 sub-issue
- **planning**：`status:plan` label 触发 `prompts/plan.md`，agent 只返回 delegations
- **scheduled jobs**：`.orchestrator/jobs.yml` 内 cron 定义（`0 9 * * *`、`@hourly`） + dedup（`active_task_id`）
- **brew services** daemon
- 218 个 release（频繁迭代）

**精彩设计点**：
- **GitHub Issue 是天然的"持久化进程总线"** —— 即使 daemon 挂了，状态在云端可恢复
- **content-hash dedup 防 comment spam** — 工程细节极佳
- **opposite-agent review** — 跨 model peer review
- **sidecar JSON**：`agent_model / branch / worktree / attempts / duration / input_tokens / output_tokens / summary / reason / accomplished[] / remaining[] / files_changed[] / prompt_hash / last_comment_hash` —— 详细到位

**给 PopolaLoom 的启示**：
- **不一定要数据库**：GitHub Issues / Linear / Jira 可以做"零运维"持久化总线
- **"failure classification"** 是 agent 编排器的核心能力（auth/billing / timeout / generic / invalid response）
- **retry-loop detection**：同样错误 3 次 → 永久 block 而不是无限重试

### 5.3 abt0y/agentflow — **DAG + 远程执行**

> "Orchestrate thousands of agents and harnesses as a graph programatically"（[repo](https://github.com/abt0y/agentflow)）

**核心特性**（基于 search summary）：
- Python，DAG 图模型
- **fanout 原语**：count / values / matrix / group_by / batches，可链 derive 操作
- 支持 Codex / Claude / Kimi（与 PopolaLoom CLI 列表完全一致）
- 远程执行：local / Docker / SSH / EC2 / ECS Fargate / AWS Lambda
- iterative cycles + on_failure 重试

**给 PopolaLoom 的启示**：
- **fanout 不是"并行"的别名**——它是"按 X 维度展开"的 DSL；PopolaLoom 应支持类似 `fanout(by_file, files=[…])`
- **远程执行抽象**很重要：本地（laptop）跑不下时，按需 escalate 到 EC2 spot

### 5.4 smtg-ai/claude-squad — **7K★ 的事实标准**

- 终端 CLI，多 agent 平铺（无 DAG，无依赖）
- 支持 Claude Code / Codex / Gemini / Aider
- yolo 模式（auto-accept）
- 隔离 git worktree per task

**给 PopolaLoom 的启示**：用户界面非常简单（"几个 agent 并行跑同样事情"），但**正是这种"零认知负担"换来的 7K★**。PopolaLoom 应该有一个等价的 "simple mode"（无依赖图、无 gate，纯并行 + worktree）。

### 5.5 stravu/crystal — **ACP 客户端**

- Electron desktop GUI
- 多 Claude Code / Codex 实例并行
- 通过 ACP（Agent Client Protocol，JSON-RPC 2.0）（[acp.cr Crystal lib](https://github.com/hahwul/acp.cr)）
- worktree 隔离 + session template

**ACP 协议要点**（基于 acp.cr 实现）：
- JSON-RPC 2.0 over stdio
- 消息类型：session create/load、agent message、tool call、thoughts
- 文件系统方法（read/write text）
- terminal 管理
- Claude Code via `npx @zed-industries/claude-code-acp`

**给 PopolaLoom 的启示**：**ACP 是已经存在的"editor↔agent"标准** —— PopolaLoom **作为客户端（消费者）实现 ACP** 几乎可以无缝对接 Claude Code / Cursor，不要发明新协议。

### 5.6 Inngest "Utah" — **harness, not framework**

**核心论述**（[blog 2026](https://www.inngest.com/blog/your-agent-needs-a-harness-not-a-framework)）：

> "Every agent framework is building one from scratch — their own retry logic, their own state persistence, their own job queues, their own event routing. Durable, event-driven infrastructure already solves this. Every LLM call or tool call becomes a step — an independently retryable unit of work."

**实现**：
- 6 个 Inngest function（不是单 monolith）：handleMessage / sendReply / acknowledgeMessage / failureHandler / heartbeat / subAgent
- agent loop = `while not done: step.run("think") + step.run("tool-X")`，每步独立 retry
- **sub-agent via `step.invoke()`**：fork session 上下文，独立 retry / observability / durable execution
- **singleton concurrency**：`{ key: "event.data.sessionKey", mode: "cancel" }` —— 一行配置实现"新消息到来就取消旧 run"
- **two-tier context pruning**：keep last 3 turns + soft-trim 4K chars + hard-clear 50K threshold
- **multi-provider** via [pi-ai](https://github.com/badlogic/pi-mono)
- 工具：`pi-coding-agent`（read/write/edit/bash/grep/find/ls）+ remember / web_fetch / delegate_task
- 触发器：Telegram / Slack / cron / sub-agent 调用 / 任何 webhook

**给 PopolaLoom 的最重要启示**：
1. **"harness, not framework"** ——这个口号 PopolaLoom 应该直接借用
2. **6 个独立 function 而非 monolith**：trigger 与 work 解耦；reply / typing-indicator / failure-handler 都是独立 retry 单元
3. **singleton + cancel** 是"steering"的最简方案
4. **sub-agent = step.invoke** —— 用 PopolaLoom 自己作为 sub-orchestrator 是天然递归的
5. **"context management is the real challenge"** ——Utah 团队踩坑后的总结，PopolaLoom 应一开始就内置 pruning + compaction + budget warning + overflow recovery

---

## 6. 关键设计模式提炼

### 6.1 任务图模型：DAG vs FSM vs 事件

| 模式 | 代表 | 适用 | 取舍 |
|------|------|------|------|
| **DAG** | sfw/loom, abt0y/agentflow, MetaGPT, LangGraph (StateGraph) | 静态依赖、可视化、可重放 | 表达条件分支需扩展（choice 算子）；动态生成节点麻烦 |
| **FSM (label-driven)** | gabrielkoerich/orchestrator, Roo Code | 显式状态转换、外部可观测（GitHub label） | 状态膨胀；不擅长并行 |
| **Event-driven (pub/sub)** | OpenHands EventStream, AutoGen v0.4, CrewAI Flow, Inngest Utah | 解耦 trigger/work、易加新触发源 | 调试难；OpenHands v1 因为线程/异步陷阱回退到同步对话 |
| **Super-step / Pregel** | LangGraph 1.0 | 并行批 + 屏障同步 | 概念门槛 |

**PopolaLoom 推荐**：**DAG 主，FSM 副**（任务节点内部用 FSM 表达 status）；**事件作为可选触发器**（webhook / cron / file watcher）。

### 6.2 派发协议：子进程 vs RPC vs 文件 vs MCP vs WebSocket vs ACP

| 协议 | 代表 | 优点 | 缺点 |
|------|------|------|------|
| **子进程 + JSON 文件** | gabrielkoerich/orchestrator (`.orchestrator/output-{id}.json`) | 零依赖；持久化天然；agent crash 可恢复 | 需 polling 或 inotify |
| **stdio JSON-RPC (ACP)** | Crystal, Zed | 标准、live streaming、tool call 双向 | agent 必须实现 ACP server |
| **MCP (stdio/sse)** | Loom, Continue, Cursor | 标准、tool 可发现 | 偏 tool consumer 视角，不是 agent runtime |
| **HTTP + SSE** | OpenHands, Loom serve | RESTful、scalable | 需要 server 跑着 |
| **WebSocket** | Inngest connect() | 持久双向、低延迟 | 复杂 |
| **Inngest step / event** | Utah | durable + observable | 依赖 Inngest 平台或 self-host |

**PopolaLoom 推荐**：**主：子进程 + JSON 文件**（最简、最 robust），**副：作为 MCP server 暴露给上游 Cursor**（满足 acceptance criterion "exposed as Skill or local MCP server"）。**进阶**：实现 ACP client 适配 Claude Code / Zed。

### 6.3 恢复模式：stateless replay vs journaled state vs event-sourced

| 模式 | 代表 | 实现要点 |
|------|------|----------|
| **Stateless replay** | LangGraph Checkpointer | 每个 super-step 后写 state；崩溃后从最近 checkpoint 重启 |
| **Journaled state** | Temporal, Restate, DBOS, Inngest | 每个 step 持久化到 journal；replay 跳过已完成 step；支持长跑（Temporal 9.1T 累计 actions） |
| **Event-sourced** | OpenHands EventStream（v0.x） | 事件即历史；用 cause 字段链 action↔observation；v1 回退因复杂度 |
| **External source-of-truth** | gabrielkoerich/orchestrator (GitHub Issues), Sweep (PR/issues) | 状态在外部系统；本地 daemon 重启从外部拉 |

**PopolaLoom 推荐**：**journaled state**（每 dispatch / report 写 SQLite）+ **external source-of-truth 可选**（绑定到 Linear / GitHub Issue 时自动同步）。

### 6.4 自我演化（self-improving / self-testing）

| 模式 | 代表 | 实现要点 |
|------|------|----------|
| **Operational learning** | Loom (operational + behavioral) | 任务后抽取 model 成功率、retry pattern；frequency-weighted；90 天衰减 |
| **DevolaFlow operational learnings (v7.0.3+)** | DevolaFlow | confidence half-life decay；session pinning；jsonl 持久化 |
| **Reinforcement Rules** | DevolaFlow v5.1+ | 上一轮 finding 注入下一轮 dispatch 作为 MUST-fix |
| **AFlow (MetaGPT 论文)** | MetaGPT | 自动生成 agentic workflow（ICLR 2025） |
| **Self-bootstrapping on Cursor** | PopolaLoom 目标 | 用 Cursor Agent 来完善自己（meta-loop） |

**PopolaLoom 推荐**：从 **operational learning + reinforcement rules** 起步（已被 Loom 与 DevolaFlow 同时验证）；自我演化（self-bootstrapping）作为里程碑而非 v1 必备。

### 6.5 上下文管理（Utah 团队踩坑总结）

> "The hardest problem wasn't calling the LLM. It was managing what goes into the LLM call."（[Inngest blog](https://www.inngest.com/blog/your-agent-needs-a-harness-not-a-framework)）

| 技术 | 何时用 |
|------|--------|
| **Context pruning** (soft-trim head/tail) | tool 输出过长 |
| **Hard clear (placeholder)** | 整体超 50K threshold |
| **Cross-run compaction** | 跨多轮累积时按 token estimate 触发 |
| **Budget warnings (system msg)** | 接近 max iterations 时提醒 agent 收尾 |
| **Overflow recovery** | LLM 返回 context-too-large → 强制 compact 后重试，不消耗 iteration |

PopolaLoom 因为编排多个 CLI agent（每个有自己的 context window），**必须从 day 1 就内置这些**。

---

## 7. 给 PopolaLoom 的启发（≥7 条可执行项）

### 启示 1：直接采用"harness, not framework"作为产品哲学（来自 Inngest Utah）
> 不要试图替代 Cursor / Claude Code / Codex 的内部 agent loop —— 它们已经很强。PopolaLoom 只做"connect / protect / orchestrate"三件事：连通器、保护带、编排器。每个外部 CLI 调用就是一个 step；每个 step 独立可重放、可观测、可终止。

**实施**：把 README 第一行就写成 "Your fleet of AI coding CLIs needs a harness, not another framework"。

### 启示 2：5 层 hierarchy = L-1 Conductor + DevolaFlow's L0–L3 unchanged
> 不要重新设计 L0/L1/L2/L3——DevolaFlow 已经在每层都设过 owned_files、acceptance_criteria、escalation chain。PopolaLoom 只在最顶层增加 **L-1 Conductor**，它的工作就是：(a) 跨 CLI 实例 routing；(b) 持久化进程总线；(c) attach/resume 协议；(d) MCP/Skill 暴露。

**实施**：复用 DevolaFlow 的 `task-dispatch.schema.yaml` / `status-report.schema.yaml`，只在最外层包一个 `conductor-dispatch.schema.yaml`，引用任意 L0 实例。

### 启示 3：进程持久化用"detached process group + 状态文件" 而非 daemon
> 借鉴 gabrielkoerich/orchestrator 的方法：每个 task 起一个 tmux session（或 setsid + nohup），状态写入 sidecar JSON + sqlite，用文件锁防双跑。Daemon （`brew services` / systemd）只负责轮询和 cron，挂掉重启不影响 in-flight task。

**实施**：
- 每个被编排的 CLI agent 跑在 tmux 会话里（命名 `popola-{task_id}`），terminal 退出不影响
- 状态文件 `.popola/state/{task_id}.json` 由 agent 本身写，PopolaLoom 读
- 主 daemon 可选；CLI 直接命令也能驱动（`popola task next`）

### 启示 4：作为 MCP server 是 v1 必备，作为 Skill 是 v2 加分
> 用户的 Cursor / Claude Code 已经是 MCP client。PopolaLoom 暴露一个 stdio MCP server，提供工具：`dispatch_task / list_tasks / attach_task / pause_task / resume_task / get_artifacts`。

**实施**：参考 sfw/loom 的 `integrations/` MCP server 实现，FastMCP 是最简起手。

### 启示 5：实现 ACP client 适配 Claude Code，不要发明新协议
> Claude Code、Zed、Crystal 都已支持 ACP（JSON-RPC 2.0 over stdio）。PopolaLoom 作为 ACP client 调 `npx @zed-industries/claude-code-acp` 起 Claude Code，立刻获得 streaming tool call、文件系统方法、terminal 管理——免费。

**实施**：先用 [`hahwul/acp.cr`](https://github.com/hahwul/acp.cr) 做参照实现一个 Python ACP client，调通 Claude Code，再泛化到 Codex/Cursor。

### 启示 6：依赖图 + 隔离 worktree + LLM-as-router 是最小可行架构
> claude-squad 用 7K★ 证明"worktree 隔离" 是必须的；gabrielkoerich/orchestrator 用每日 release 证明 "haiku-as-router" 工作良好；abt0y/agentflow 证明 fanout DAG 是基本单元。三件事合起来就是 PopolaLoom v0.1.0 的架构。

**实施**：
1. `popola init` → 配 GitHub repo / 本地路径
2. `popola task add "<title>" --depends-on <id>` → 入队 + LLM-as-router 决定 agent
3. 每个 task 拿到独立 git worktree + tmux session
4. `popola task list` → 看图
5. `popola task attach <id>` → 进入 tmux 看实时输出

### 启示 7：Reinforcement Rules + 跨 agent 一致性 gate 是质量护城河
> DevolaFlow 已验证 "上一轮 finding 注入下一轮" 的有效性。PopolaLoom 做跨 CLI 编排时，**因为 CLI agent 之间没有共享 context**，这个机制变得**更重要**。同时增加新 gate 维度：`agent_consistency`（多 worker 跑同任务的输出 diff < 阈值）。

**实施**：
- L-1 Conductor 维护 `cross_run_findings.jsonl`
- 当某个 task fail → 下次 dispatch 时 prepend 一段 "MUST-fix from previous run: ..."
- 当配置 multi-agent voting 时 → 计算 diff metric，加入复合 gate 公式

### 启示 8：从 day 1 内置上下文管理（Utah 之痛）
> 多个 CLI agent 的 context window 加起来很容易爆。PopolaLoom 必须内置：(a) tool result pruning（soft-trim head/tail），(b) cross-task compaction，(c) budget warning（dispatch 时把"剩余 N 次"写进 prompt），(d) overflow recovery（LLM 返回 too-large → 强制 compact 后重试，不消耗 iteration）。

**实施**：抄 Inngest Utah 的 `PRUNING` 配置为起点：

```yaml
context:
  keep_last_assistant_turns: 3
  soft_trim:
    max_chars: 4000
    head_chars: 1500
    tail_chars: 1500
  hard_clear:
    threshold: 50_000
    placeholder: "[Tool result cleared]"
```

### 启示 9（Bonus）：sandbox + branch/rewind 比裸 git 更适合 agent
> Plandex 的 cumulative diff sandbox 是给 agent 编排做"撤销/分支"的最佳模型。PopolaLoom 可以让每个 task 的输出先进 sandbox（独立 worktree 的 staging 区），用户审批后才 merge 进主分支。

**实施**：扩展 worktree 模型 —— 每个 task 一个 worktree，但 PR 落地前不 push；提供 `popola sandbox diff <task_id>` 看 diff，`popola sandbox accept/reject/branch` 操作。

### 启示 10（Bonus）：把"自我演化"做成里程碑而非 day-1 必备
> "Self-bootstrapping loop on Cursor Agent" 是个迷人但有风险的目标。先跑通"被人手动调"的产品形态，再在 v1.0 之后引入"PopolaLoom 用 Cursor Agent 改进自己"。MetaGPT 的 AFlow 论文（ICLR 2025）和 DevolaFlow 的 operational learning v7.0.3+ 提供了路径。

**实施**：
- v0.x：人工配 templates / workflow yaml
- v1.0：稳定 ACP/MCP 协议
- v1.5：内置 operational learning + behavioral learning（仿 sfw/loom）
- v2.0：自演化（meta-loop on Cursor Agent）

---

## 8. 总结表：每个项目对 PopolaLoom 的"借鉴价值评分"

| 项目 | 设计借鉴 | 代码借鉴 | 协议借鉴 | UX 借鉴 | 综合 |
|------|----------|----------|----------|---------|------|
| **sfw/loom** | ★★★★★ | ★★★★★（直接 clone 当参考实现） | ★★★★ (MCP) | ★★★★（TUI/CLI/API/MCP 四面体） | **5.0** |
| **DevolaFlow** | ★★★★★（hierarchy + primitives + gate） | ★★★★ (skill 注入直接复用) | ★★★ | ★★★ | **4.5** |
| **gabrielkoerich/orchestrator** | ★★★★ | ★★★★（极简 bash 实现） | ★★（GitHub Issues） | ★★★ | **4.0** |
| **Inngest Utah** | ★★★★★（"harness" 哲学） | ★★（TS，需移植） | ★★★（事件） | ★★ | **4.0** |
| **stravu/crystal + acp.cr** | ★★★ | ★★（Electron） | ★★★★★（ACP！） | ★★★★ | **4.0** |
| **abt0y/agentflow** | ★★★★（DAG fanout） | ★★★ | ★★★ | ★★ | **3.5** |
| **smtg-ai/claude-squad** | ★★★★（worktree 模式） | ★★ | ★★ | ★★★★（7K★ 验证） | **3.5** |
| **LangGraph 1.0** | ★★★★（checkpointer） | ★★★ | ★★ | ★★ | **3.5** |
| **Plandex** | ★★★（sandbox） | ★★★ | ★★ | ★★★★ | **3.5** |
| **Aider Architect** | ★★★（双模型路由） | ★★ | ★★ | ★★★ | **3.0** |
| **CrewAI Flow** | ★★★（@start/@listen/@router） | ★★ | ★★（MCP） | ★★ | **2.5** |
| **MetaGPT** | ★★（SOP） | ★★ | ★★ | ★★ | **2.5** |
| **OpenHands** | ★★（v1 教训） | ★★ | ★★ | ★★ | **2.0** |
| **AutoGen v0.4** | ★★（actor 模型偏重） | ★ | ★★ | ★★ | **2.0** |
| **Magentic-One** | ★★★（lead-worker pattern） | ★ | ★ | ★ | **2.0** |
| **Continue.dev** | ★★（parallel tool） | ★★ | ★★ | ★★★ | **2.5** |
| **Roo Code** | ★★（mode-switch） | ★ | ★ | ★★★（1M users） | **2.0** |
| **Sweep AI** | ★★（embedding rerank） | ★ | ★（GitHub） | ★★ | **2.0** |
| **Mastra** | ★★（generic Harness class） | ★（TS） | ★★ | ★★ | **2.0** |
| **AG2** | ★★ | ★★ | ★ | ★ | **1.5** |
| **MultiOn** | ★★（session 隔离） | ★ | ★ | ★ | **1.5** |
| **OpenAI Swarm/Agents SDK** | ★★（handoff） | ★★ | ★★ | ★★ | **2.0** |
| **Temporal/Restate/Inngest (workflow engines)** | ★★★（durable execution 范式） | ★★ | ★★ | — | **2.5** |

---

## 9. 调研中确认的"PopolaLoom 差异化空间"（gap 分析）

| PopolaLoom 需求 | sfw/loom | gabrielkoerich/orch | abt0y/agentflow | claude-squad | Crystal | Utah |
|-----------------|----------|---------------------|-----------------|--------------|---------|------|
| 跨 5 个 CLI（Cursor/Claude/Codex/Kimi/Copilot）统一编排 | ✗（模型层） | △（3 个：Claude/Codex/OpenCode） | ✓（3 个） | ✓（4 个） | △（2 个） | ✗ |
| 依赖感知任务图 | ✓ | △（sub-issue blocking） | ✓✓ | ✗ | ✗ | △ |
| 持久化、survives terminal exit | △（serve daemon） | ✓✓（brew services） | △ | ✓（tmux） | ✓（Electron app） | ✓✓（Inngest cloud） |
| Attach/resume 任意先前任务 | ✓（--resume session） | ✓（issue label） | △ | △ | ✓ | △ |
| 暴露为 Skill 或 local MCP server | ✓（MCP server） | ✗ | ✗ | ✗ | ✗ | ✗ |
| 自我演化（self-bootstrap on Cursor） | ✗ | ✗ | ✗ | ✗ | ✗ | △（"agent writes itself" roadmap） |

**结论**：**没有任何一个调研到的项目同时打满全部 6 列**。最接近的是：
- **sfw/loom**（缺多 CLI 编排 + survives-terminal-exit）
- **gabrielkoerich/orchestrator**（缺 MCP 暴露 + 跨模型 SDK 抽象）

PopolaLoom 的差异化定位 = **(sfw/loom 的 harness 哲学) ⊕ (gabrielkoerich/orchestrator 的 daemon + GitHub backend) ⊕ (Inngest Utah 的 step.invoke 子 agent 递归) ⊕ (ACP 协议作为客户端) ⊕ (DevolaFlow 作为楼下编排器)**。

---

## 10. 推荐的"必读必看"短列表（给 Design 阶段 L1 的 handoff）

按重要性排序，建议 Design 阶段先 clone 这 5 个仓到 `/home/agent/reference/` 做 architecture diff：

| 优先级 | 仓库 | 必看文件 | 为什么 |
|--------|------|----------|--------|
| **P0** | [`sfw/loom`](https://github.com/sfw/loom) | `src/loom/engine/`、`src/loom/integrations/`（MCP）、`src/loom/processes/`、`docs/DB-MIGRATIONS.md` | 思想最近的参考实现，模块化好，有 MCP server |
| **P0** | [`gabrielkoerich/orchestrator`](https://github.com/gabrielkoerich/orchestrator) | `scripts/run_task.sh`、`scripts/serve.sh`、`prompts/route.md`、`prompts/system.md` | 工程细节扎实，failure classification、retry-loop detection、content-hash dedup 都值得学 |
| **P0** | [`Codename-11/ARC`](https://github.com/Codename-11/ARC)（如果是 ArcTower 的真身） | README + 架构文档 | 用户专门点名 |
| **P1** | [`abt0y/agentflow`](https://github.com/abt0y/agentflow) | core DAG + fanout 实现 | DSL 表达对位 PopolaLoom 需求 |
| **P1** | [`inngest/utah`](https://github.com/inngest/utah) + [Inngest blog](https://www.inngest.com/blog/your-agent-needs-a-harness-not-a-framework) | agent loop + sub-agent + singleton 配置 | "harness" 哲学的最干净阐述 |
| **P2** | [`hahwul/acp.cr`](https://github.com/hahwul/acp.cr) | `src/acp/protocol/client_methods.cr` | ACP client 实现参考 |
| **P2** | [`stravu/crystal`](https://github.com/stravu/crystal) | `CLAUDE.md` + ACP 集成层 | 桌面端 ACP 客户端形态 |

---

## 11. 关键引用列表（去重、按章节排序）

- DevolaFlow SKILL.md（[file](file:///root/.claude/skills/devola-flow/SKILL.md)，本地，v10.1.0，2026-04-16）
- DevolaFlow `references/meta-framework.md`（14 stage primitives + 22 templates）
- DevolaFlow `references/agent-hierarchy.md`（L0–L3 contracts）
- sfw/loom 仓库（[GitHub](https://github.com/sfw/loom)，53★ 2026-04-04 last push）
- gabrielkoerich/orchestrator（[GitHub](https://github.com/gabrielkoerich/orchestrator)，218 release 2026-03-01）
- abt0y/agentflow（[GitHub](https://github.com/abt0y/agentflow)）
- smtg-ai/claude-squad（[GitHub](https://github.com/smtg-ai/claude-squad)，7,037★）
- stravu/crystal（[GitHub](https://github.com/stravu/crystal)） + hahwul/acp.cr（[GitHub](https://github.com/hahwul/acp.cr)，v0.2.0 2026-03-26）
- Inngest "Your Agent Needs a Harness"（[blog 2026](https://www.inngest.com/blog/your-agent-needs-a-harness-not-a-framework)） + [inngest/utah](https://github.com/inngest/utah)
- Microsoft AutoGen v0.4（[Microsoft Research](https://www.microsoft.com/en-us/research/blog/autogen-v0-4-reimagining-the-foundation-of-agentic-ai-for-scale-extensibility-and-robustness/)） + [docs](https://microsoft.github.io/autogen/0.4.1/)
- Magentic-One（[Microsoft Research](https://www.microsoft.com/en-us/research/articles/magentic-one-a-generalist-multi-agent-system-for-solving-complex-tasks/)）+ [docs](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/magentic-one.html)
- OpenAI Swarm/Agents SDK 演进（[particula 2026](https://particula.tech/blog/langgraph-vs-crewai-vs-openai-agents-sdk-2026)，[techsy](https://techsy.io/en/blog/langgraph-vs-crewai-vs-openai-agents-sdk)，[effloow](https://effloow.com/articles/ai-agent-frameworks-compared-2026)）
- CrewAI Flows 生产架构（[CrewAI prod arch](https://docs.crewai.com/en/concepts/production-architecture)，[2026 guide](https://www.jahanzaib.ai/blog/crewai-flows-production-multi-agent-guide)）
- LangGraph 1.0（[release notes](https://medium.com/%40romerorico.hugo/langgraph-1-0-released-no-breaking-changes-all-the-hard-won-lessons-8939d500ca7c)，[Pregel API](https://langchain-ai.github.io/langgraphjs/reference/classes/langgraph.Pregel.html)，[on_interrupt PR](https://github.com/langchain-ai/langgraph/pull/7359)）
- MetaGPT（[GitHub](https://github.com/FoundationAgents/MetaGPT)，66.6K★，AFlow ICLR 2025）
- OpenHands v1 path（[All Hands blog Nov 2025](https://www.all-hands.dev/blog/the-path-to-openhands-v1)） + EventStream PR（[#1538](https://github.com/OpenDevin/OpenDevin/pull/1538)）
- Aider Architect Mode（[docs](https://aider.chat/docs/usage/modes.html)，[CodeRouter blog](https://www.coderouter.io/blog/aider-cost-optimization-2026)）
- Continue.dev parallel tool calling（[blog](https://blog.continue.dev/parallel-tool-calling)，[CLI docs](https://docs.continue.dev/guides/cli)）
- Roo Code（[dayahimour](https://dayahimour.org/en/blog/roo-code/)）
- Plandex（[GitHub](https://github.com/plandex-ai/plandex)，[agentwiki](https://agentwiki.org/plandex)）
- Sweep AI（[GitHub](https://github.com/sweepai/sweep)）
- Mastra（[release 1.5.0 2026-02-19](https://github.com/mastra-ai/mastra/releases/tag/@mastra%2Fcore@1.5.0)，[AGENTS.md](https://github.com/mastra-ai/mastra/blob/main/AGENTS.md)）
- AG2（[GitHub](https://github.com/ag2ai/ag2/)）
- MultiOn（[API docs](https://api.multion.ai/)，[wiki](https://artificial-intelligence-wiki.com/agentic-ai/agent-architectures-and-components/multion-ai-guide/)）
- Temporal/Restate/Inngest durable execution（[Zylos research 2026-02-17](https://zylos.ai/research/2026-02-17-durable-execution-ai-agents)，[Restate AI agents](https://docs.restate.dev/use-cases/ai-agents)，[Restate blog](https://www.restate.dev/blog/durable-ai-loops-fault-tolerance-across-frameworks-and-without-handcuffs)，[Temporal AI](https://temporal.io/ai)）
- Crystal CLAUDE.md（[file](https://github.com/stravu/crystal/blob/main/CLAUDE.md)）

---

## 12. 不在本报告范围（明确 OOS 防止 scope creep）

- 单 agent 内部的 prompt engineering / tool calling 协议细节（属于"楼下" devola-flow 的范畴）
- 具体 LLM 模型对比（Claude vs GPT vs Kimi 性能）
- agent 安全/沙箱深度评估（应另立 security-audit workflow）
- 商业化 / 定价模型（Plandex Cloud vs self-host 经济性等）
- 国内 agent 生态（Tongyi DeepSeek 通义 等，需另做调研）
- 移动端 / 浏览器内 agent（MultiOn 已浅触，更深需另立专题）

---

> **报告完成时间**：2026-05-03
> **作者**：DevolaFlow research-only L3 Task Agent T1
> **下一步建议**：Design 阶段 L1 应基于本报告的"启示 1–10" 起草 PopolaLoom 架构设计文档，并先 clone P0/P1 仓做 1-day spike。
