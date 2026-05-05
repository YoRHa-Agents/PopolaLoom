# 04 · 行业最佳实践与一线声音 (2025–2026)

> 调研窗口:2025-06 ~ 2026-05。检索方式:WebSearch / WebFetch,聚焦官方工程博客、一线从业者长文、以及通过聚合站点回链的 Twitter/X 帖。所有引文均给出可访问 URL,凡未找到具名出处者会显式标注 "未找到具名出处,综合多源" 字样。
>
> 适用对象:`PopolaLoom` —— 一个调度多家 AI 编码 Agent CLI(Claude Code、Cursor Agent CLI、Codex CLI、Kimi CLI、Copilot CLI)的"上层编织者"。本文为选型与架构决策提供输入,不是产品发布稿。

---

## TL;DR — 给 PopolaLoom 的 8 条公理

1. **公理一:写操作必须单线程化(Single-Threaded Writes)**
   行业在 2026 已经形成强共识:多 Agent 系统目前可靠的形态是 "多个 Agent 贡献智力、但写操作只走一条线"。Cognition 4 月 2026 总结:"multi-agent systems work best today when writes stay single-threaded and the additional agents contribute intelligence rather than actions"(cognition.ai/blog/multi-agents-working)。PopolaLoom 必须把"派发任务"和"实际写文件/写代码"分开,后者天然串行,前者可以扇出。

2. **公理二:派发器永远不要自己干活(Dispatcher Must Not Execute)**
   在产线里反复被验证的"orchestrator never writes code"原则——派发器只做"分解 + 委派 + 校验 + 升级",一旦它自己开始写代码,实现细节会污染调度上下文,让它的战略推理能力快速退化(building.theatlantic.com 2026-03 "Why Your AI Orchestrator Should Never Write Code")。PopolaLoom 应当严格只承担调度/状态管理职责。

3. **公理三:Token 预算是设计变量,不是事后账单**
   Anthropic 在多 Agent 研究系统的复盘中给出"80% 的性能方差由 token 用量解释"(anthropic.com/engineering/built-multi-agent-research-system),代价是平均 ~15× 普通 chat 的 token 消耗;Claude Agent Teams 直接说明"15 个 teammate 时大约是单 session 的 7×"。PopolaLoom 必须把 token/$$ 预算作为一等公民,在每个任务、每条派发链上设置硬上限。

4. **公理四:并行不是免费的午餐,任务可分解性是前提**
   Google 在 2025 年底的研究(arxiv.org/abs/2512.08296)给出量化证据:在固定算力预算下,工具密集型任务的多 Agent 协调反而更差;基线超 ~45% 后,coordination 收益递减甚至为负;独立多 Agent 拓扑会把错误放大 17.2 倍。PopolaLoom 应当让"并行扇出"成为可选项而非默认,且必须基于任务"是否真的可独立分解"做触发判断。

5. **公理五:派发上下文不等于复制对话**
   Cognition 的核心洞见 "share full agent traces, not just individual messages" 仍然成立(cognition.ai/blog/dont-build-multi-agents)。PopolaLoom 在唤起子 Agent 时,要么传递完整 trace,要么显式声明"只读子任务";否则就是"传话游戏"的复刻。

6. **公理六:后台/长时运行 Agent 已经是基础设施,而非花活**
   2026 年所有头部 IDE/CLI 都拥抱了 background/cloud agent:Cursor Cloud Agents、Claude Code background tasks、Copilot CLI `/fleet`、VS Code 1.109 Agent Sessions view、Devin Scheduled Devins。PopolaLoom 想做"终端退出后任务仍在跑"是必修课,可以直接复用 tmux/zellij/git worktree/sandbox 这些在 2026 已成熟的子方案。

7. **公理七:Human-in-the-loop 必须按"可逆性 × 置信度"二维分级**
   2026 共识:HITL 不是"每步都问",而是"按动作严重性 × Agent 置信度做矩阵化阈值"(myengineeringpath.dev/genai-engineer/human-in-the-loop)。PopolaLoom 提供给宿主 Agent 的 Skill/MCP 接口要原生支持三种模式:human-before-action / human-on-exception / human-after-action。

8. **公理八:Agent 是有状态进程,故障必须可恢复(Resumable, not Restartable)**
   Anthropic:"When errors occur, we can't just restart from the beginning... we built systems that can resume from where the agent was when the errors occurred"(anthropic.com/engineering/built-multi-agent-research-system)。PopolaLoom 的核心调度循环必须以 checkpoint + 幂等重试为骨架,attach/detach 必须在任意时刻安全。

---

## 一、厂商官方观点

### 1.1 Anthropic

**核心立场**(2024-12 起延续):区分 "workflows"(预定义代码路径编排 LLM 与工具)与 "agents"(LLM 在循环中自主决策),反复强调"先用最简方案,只在需要时加复杂度";"In many cases, workflows are simpler, more reliable, cheaper, faster, and more performant"(anthropic.com/engineering/building-effective-agents,Erik Schluntz & Barry Zhang,2024-12)。

**五个标准 workflow pattern**(同上):
- Prompt chaining(顺序链)
- Routing(路由)
- Parallelization(并行 / 投票)
- Orchestrator-workers(中央 LLM 触发多个子调用并合成)
- Evaluator-optimizer(一个 model 在循环中检查另一个的产出)

**多 Agent 研究系统经验**(Anthropic Engineering, 2025-06-13, "How we built our multi-agent research system",Jeremy Hadfield et al.):
- 架构:`LeadResearcher`(Claude Opus 4)→ N 个 `Subagent`(Claude Sonnet 4)→ `CitationAgent`,内部 plan 持久化到 Memory 防止 200K context 截断后丢失。
- 性能:相对单 Agent Opus 4 评测提升 90.2%。
- 经济性:多 Agent 比 chat 多耗 ~15× token,因此"only economically viable for high-value tasks"。
- 失败模式:agent sprawl(简单查询起 50 个 subagent)、vague delegation、search strategy collapse(都收敛到 SEO 内容)、cascading errors → 用 checkpoint 和 token-spread 控制。
- 8 条 Prompt 经验,直接可抄到 PopolaLoom 的派发 prompt 模板:
  > 1. *Think like your agents.*  2. *Teach the orchestrator how to delegate.*(每个 subagent 必须有 objective、output format、tools/sources、boundaries)  3. *Scale effort to query complexity.*  4. *Tool design and selection are critical.*  5. *Let agents improve themselves.*  6. *Start wide, then narrow down.*  7. *Guide the thinking process.*  8. *Parallel tool calling transforms speed.*
- 工程教训:
  > "Agents are stateful and errors compound. ... Without effective mitigations, minor system failures can be catastrophic for agents. When errors occur, we can't just restart from the beginning... Instead, we built systems that can resume from where the agent was when the errors occurred."
  > "Synchronous execution creates bottlenecks. Currently, our lead agents execute subagents synchronously... Asynchronous execution would enable additional parallelism... but adds challenges in result coordination, state consistency, and error propagation."
  > "Subagent output to a filesystem to minimize the 'game of telephone.' Direct subagent outputs can bypass the main coordinator..."
- 部署:**Rainbow deployments**——不能强行升级所有正在跑的 agent,要灰度。

**Claude Agent SDK / Subagents**(code.claude.com/docs/en/agent-sdk):
- 子 Agent 是"独立进程,通过 `Task` tool 由父 Agent 派生,各自持有独立 context window"。
- 强调 *Sequential / Parallel / Hierarchical* 三种编排形态;Hierarchical 用"中层经理 Agent 持有子系统所有权,降低顶层认知负载"——这与 PopolaLoom 想做的正好一致。

**Claude Agent Teams**(docs.anthropic.com/en/docs/claude-code/agent-teams,2026-02-05):
- 实验性 feature,需 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` + Opus 4.6+。
- 一个 team lead session + 最多 15 个 teammate,每个 teammate 自带 1M context、可通过 mailbox 直接 P2P 通信、共享 task list、git-based locking 与 continuous merge。
- 官方警示:15 teammate 时 token 大约是单 session 的 7×,"justifiable only for genuinely parallelizable work"。
- 适用场景:research/review、清晰可分的新模块、并行 debug 假设、跨层(前/后/测/库)协调。

**Claude "Advisor Strategy"**(claude.com/blog/the-advisor-strategy,2026 早春):
- 让较小模型在卡壳时调用较大模型——这是"smart friend"模式被 Anthropic 自己也认可的信号。

---

### 1.2 OpenAI

**Codex CLI Subagents**(developers.openai.com/codex/multi-agent/):
- 内置三种 agent 类型:`default`(通用)、`worker`(执行)、`explorer`(只读探索)。
- 自定义 Agent:`~/.codex/agents/*.toml`(personal)或 `.codex/agents/*.toml`(项目级)。每个文件包含 `name`、`description`、`developer_instructions`,可选 `model`、`model_reasoning_effort`、`sandbox_mode`、`mcp_servers`。
- 派发由 Codex 自动管理:spawn / route follow-up / wait / close;"Codex only spawns subagents when explicitly requested"——明显比 Claude Code 更保守。
- 子 Agent 继承父 session 的 sandbox policy,审批可在 *inactive* agent thread 上浮现;父级 runtime override 在 spawn child 时被重新应用。
- 通过 `/agent` slash 命令切换、检查、停止子 Agent。

**OpenAI Agents SDK Handoffs**(openai.github.io/openai-agents-js/guides/multi-agent):
- 两种主模式:
  > **Handoffs**: triage agent 把对话路由到 specialist,specialist 接管"that turn 的余下部分"——适合"路由本身就是流程的一部分,specialist 该自己跟用户说话"。
  > **Agents as Tools**: manager agent 始终 own 最终回答,通过 `agent.asTool()` 调用 specialist。
- Handoff input 数据结构:`input_history` / `pre_handoff_items` / `new_items` / `input_items`(可过滤),且支持 `inputFilter` 函数定制——给 PopolaLoom 提供了"上下文裁剪可编程"的实现范式参考。

**OpenAI Agents SDK Human-in-the-loop**(openai.github.io/openai-agents-js/guides/human-in-the-loop):
- 工具通过 `needsApproval`(boolean 或 async function)声明审批要求;触发时 SDK 暂停 run,在 `interruptions` 数组中返回 pending approval,人类调用 `result.state.approve()` / `result.state.reject()` 后从保存状态恢复。

**AGENTS.md 标准**(developers.openai.com/codex/guides/agents-md/):
- OpenAI 发起(2025-08),已被 Cursor、Copilot、Windsurf、Devin、Sourcegraph Amp 等接纳;2025-12 进入 Linux Foundation Agentic AI Foundation。
- Princeton 124 个真实 PR 研究:AGENTS.md 让运行时间下降 28.6%、token 用量下降 16.6%(vibecoding.app/blog/agents-md-guide)。
- PopolaLoom 应当在派发任务时把 AGENTS.md 视为"项目级 contract",不要重复传递。

---

### 1.3 Microsoft / GitHub

**Microsoft Agent Framework 1.0 GA**(devblogs.microsoft.com/agent-framework/microsoft-agent-framework-version-1-0,2026-04-03):
- 把 Semantic Kernel 的企业基座 + AutoGen 的多 Agent 编排能力合并;.NET / Python 双语言。
- Workflow 引擎:Sequential / GroupChat / **Magentic-One** 三种 multi-agent 拓扑。
- Magentic-One Manager 用 *ledger-based coordination*,跟踪 task facts / plan / progress,**动态选择**下一步该哪个 agent 干。
- 原生支持 MCP、A2A、OpenAPI——从 Microsoft 角度承认了"跨厂商互通协议层" 的重要性。

**GitHub Copilot Coding Agent**(GA 2026-03;github.blog/2026/02/whats-new-with-github-copilot-coding-agent):
- 后台 4 阶段流程:assignment → planning → autonomous execution → human review。
- 升级:可选 model(快/强)、自检(在开 PR 前先跑 Copilot code review)、安全扫描(code scanning / secret scanning / dep vuln 自动跑)。
- *最佳任务范围*:bug fix、test 覆盖、文档、tech debt、可访问性。**避免**:复杂重构、生产关键 issue、安全敏感、模糊问题、需要深度领域知识——明确给出"什么不该交给后台 Agent"。

**GitHub Copilot CLI `/fleet`**(github.blog/ai-and-ml/github-copilot/run-multiple-agents-at-once-with-fleet-in-copilot-cli):
- orchestrator 把任务分解成离散 work item、识别可并行部分、分发到独立 agent 在不同文件/区域并行执行——模式与 Cursor 2.0 / Claude Agent Teams 同构。

**VS Code 1.109 Agent Sessions View**(code.visualstudio.com/updates/v1_109,2026-01):
- 单一 view 同时管理 local / background / cloud 三类 agent,可在 Claude / Codex / Copilot 之间切换;"all under the same GitHub Copilot subscription"——这是行业向 Karpathy "command center" 喊话的回应。

**"Multi-agent workflows often fail"**(github.blog/ai-and-ml/generative-ai/multi-agent-workflows-often-fail-heres-how-to-engineer-ones-that-dont/):
- 方法论:typed schemas + action schemas + 严格 interface,防止 agent 互相矛盾或冲突。GitHub 把"结构化数据契约"列为多 Agent 可靠性的 first-class 设计要求。

---

### 1.4 Google

**Jules SDK**(github.com/google-labs-code/jules-sdk):
- TypeScript,面向"长跑、上云"的代码 Agent 编排。
- ephemeral 云 sandbox(Node.js / Python / Rust / Bun / 等预装),与 GitHub repo 集成,自动开 PR;支持 streaming activity update。
- 默认尊重 `AGENTS.md` 来理解 codebase。

**Gemini Enterprise Agent Designer**(cloud.google.com/gemini/enterprise/docs/agent-designer):
- 低代码可视化编辑,支持单步 / 多步 agent 编排,subagent 树形组合。

**Agent Registry + ADK**(docs.cloud.google.com/agent-registry/resolve-endpoints-and-build-orchestrators):
- 程序化发现并组合多个专门 Agent 成层级,orchestrator agent 把远端 agent 当作 sub-agent 调用——这是"协议化跨厂商调度"的官方落地。

**Agent2Agent (A2A) Protocol**(developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability;a2aproject.org;1.0 于 2026-03 发布,Linux Foundation 托管):
- 开源标准,JSON-RPC 2.0 over HTTP(S) + SSE + push,支持 sync request/response、streaming、async push。
- 让不同厂商/框架的 Agent 能 *相互发现 capability、协商交互模态、在不暴露内部 state 的前提下安全协作*。
- 官方语境里 A2A(agent-to-agent)与 MCP(agent-to-tool)互补。
- 支持 Python / JS / Java / Go / Rust / C# 官方 SDK。
- **PopolaLoom 含义**:如果想未来不绑死任何一家 CLI,A2A 是值得跟踪的协议层。

**Google Research "Towards a Science of Scaling Agent Systems"**(arxiv.org/abs/2512.08296):
- 4 benchmark × 5 架构(Single-Agent + Independent / Centralized / Decentralized / Hybrid Multi-Agent)× 3 模型族 = 180 配置。
- 三个发现:
  > (1) tool-coordination trade-off:固定算力预算下,tool-heavy 任务在多 Agent 下吃亏更多。
  > (2) capability saturation:单 Agent baseline 超过 ~45% 后,coordination 边际收益递减甚至变负。
  > (3) topology-dependent error amplification:Independent 拓扑放大错误 17.2×,centralized 控制在 4.4×。
- 顺序推理任务,**所有**多 Agent 变体都比单 Agent 退化 39–70%。

---

### 1.5 Cursor

**Cursor 2.0 + Composer**(www.cursor.com/blog/2-0,2025-10-29):
- Composer 是 Cursor 自家 coding model,4× 同等水平模型速度,大多数 turn 30 秒内完成。
- 单条 prompt **最多 8 个 agent 并行**,通过 git worktrees 或远程机器隔离;agent-centric UI。
- Best practice 显式建议"have multiple models attempt the same problem and pick the best result"——即 **N 个 best-of-N + 人类挑选**,而非协作。

**Cloud Agents**(www.cursor.com/blog/cloud-agents):
- 三类用法:fixing bugs(并行多模型尝试)、quick todos(早间预派发)、complex features(本地讨论 plan,plan mode 直接送云端实现)。
- "We have also revamped the GPT-5 Codex agent harness to work better for long time horizons in the cloud" —— Cursor 给不同模型分别 tune harness。

**Cloud Agents API v1**(cursor.com/docs/background-agent/api/overview):
- public beta,支持 programmatic launch & manage,custom prompt / model / repo config / env var,Basic Auth。

**Long-running Agents Research Preview**(cursor.com/blog/long-running-agents,2026-02-12):
- 特征:**plan first, wait for approval, then execute**;**multiple agents check each other's work** 维持连贯性;能持续运行数小时到数十小时。
- 案例:有用户压缩"季度级"项目到 ~两天,52 小时任务产出 151k LOC,minimal babysitting。

**Best practices for coding with agents**(www.cursor.com/blog/agent-best-practices):
- "Agent harness" 三要素:Instructions / Tools / Model(Cursor 给每个 frontier model tune 不同的 harness)。
- 强调"先 plan、再 code":Plan Mode(Shift+Tab),plan 存到 `.cursor/plans/`,失败先回到 plan 而不是反复改 prompt。
- 鼓励 *agent 自己找上下文*(语义搜索 / grep)而不是手动 @ 标全文件,避免上下文污染。
- 明确建议"start a new conversation when... the agent seems confused or keeps making the same mistakes"——上下文衰退是真实信号。
- Cursor Skills + Hooks:可以写一个 stop hook 让 agent 反复迭代(`.cursor/hooks.json` + `bun run grind.ts`),"useful for: running until tests pass, iterating on UI, any goal-oriented task where success is verifiable"。

**Worktrees**(cursor.com/docs/configuration/worktrees):
- 自动管理 git worktree 让多个 agent 在同一 repo 隔离修改——值得 PopolaLoom 直接照抄成 default 隔离策略。

---

### 1.6 Cline / Roo Code

**Cline Plan & Act 双模**(docs.cline.bot/features/plan-and-act):
- **Plan Mode**:不能修改文件、不能执行命令,只做探索、讨论、生成方案。
- **Act Mode**:在 Plan 上下文基础上执行。
- "Different models can be configured for each mode"——这是 PopolaLoom 可以学的 *model-per-stage* 模式。

**Roo Code Orchestrator (Boomerang)**(docs.roocode.com/features/boomerang-tasks):
- "🪃 Orchestrator Mode" 把任务分解成子任务,每个子任务在不同 specialized mode 中 *独立 context window* 跑;父任务暂停,子任务完成后只把 *summary* 回传给父任务。
- 关键设计:**子任务彻底隔离 + 只回 summary**,本质上把上下文压缩做成调度原语。
- 3.17 (2025-05) 引入 "Smarter Boomerang" + 模式定义中的 `When to Use` 字段帮助 Orchestrator 路由。
- `cline-agent-orchestrator`(github.com/harryosmar/cline-agent-orchestrator,2026-03)进一步提供:planner / engineer / QA / PR reviewer 多角色、`.clinerules/workflows/`、MCP servers、lifecycle hooks、handoff/state orchestration——是 PopolaLoom 这种"上层编织者"最直接的开源对照物之一。

---

### 1.7 LangChain / LangGraph

**Harrison Chase: "How to think about agent frameworks"**(blog.langchain.com/how-to-think-about-agent-frameworks):
- 引用 Anthropic 的 workflow vs agent 二分,并赞同"agentic systems 在产线上几乎都是 workflow + agent 的组合"。
- *核心论断*:
  > "The hard part of building reliable agentic systems is making sure the LLM has the appropriate context at each step. This includes both controlling the exact content that goes into the LLM, as well as running the appropriate steps to generate relevant content."
- 把"控制 context"作为框架价值的判定标准,直接批 OpenAI Agents SDK 等"只是抽象,不是 orchestration framework"。
- Floor / Ceiling 框架:工作流框架 high floor / high ceiling;agent 框架 low floor / low ceiling;LangGraph 自我定位 low floor + high ceiling。

**LangGraph Supervisor / Swarm**(myengineeringpath.dev/genai-engineer/langgraph-multi-agent/,2026):
- `Command(goto="next_node")` 替代条件 edge,让 agent 自己决定路由;`@task` / `@entrypoint` 函数式 API。
- `langgraph-supervisor`(中央 supervisor 路由)与 `langgraph-swarm`(去中心,P2P handoff)两套 prebuilt 实现。
- Persistence 层默认提供 *interrupt / approve / resume / time travel*——human-in / human-on the loop 是协议级 first-class。

**Lance Martin (LangChain) 反思**(rlancemartin.github.io/2025/07/30/bitter_lesson/):
- 结论:工程上预设的 *workflow 结构*会随着模型变强成为天花板。他自己的 open-deep-research 用 hand-built workflow,反而错过了 tool-calling + MCP 起飞的红利。
- "incremental structure based on current constraints, then remove it as capabilities improve" —— PopolaLoom 不要在第一天就过度结构化,要给未来"模型变聪明后简化掉自家中介层"留路。

---

### 1.8 LlamaIndex

**Multi-agent patterns in LlamaIndex**(docs.llamaindex.ai/en/stable/understanding/agent/multi_agent/):
- 三种模式:`AgentWorkflow` swarm 内建 handoff / Orchestrator-as-tool / Custom planner。
- `MultiAgentWorkflow` PR(2025-01-17 合并)支持 ctx state injection、`ctx.wait_for_event()` 实现 HITL、ReAct & function-calling 双模型形态、跨 run 维持 context、controllable handoff——很完整的"事件驱动多 Agent 编排"参考。

---

### 1.9 Cognition / Devin

**"Don't Build Multi-Agents"**(cognition.ai/blog/dont-build-multi-agents,Walden Yan,2025-06-12):
- 两条铁律:
  > **Principle 1**: Share context, and share full agent traces, not just individual messages.
  > **Principle 2**: Actions carry implicit decisions, and conflicting decisions carry bad results.
- 经典反例:Flappy Bird → subagent 1 错做成 Mario 背景,subagent 2 做出风格不一致的鸟,合成 agent 无法收拾。
- 推荐的 fallback 是单线程线性 Agent,溢出问题用一个**专门训练的 context compressor 模型**解决(Cognition 自己 fine-tune 过)。
- 2025 年的明确判断:"running multiple agents in collaboration only results in fragile systems"。

**"Multi-Agents: What's Actually Working"**(cognition.ai/blog/multi-agents-working,Walden Yan,2026-04-22):
- 10 个月后的更新:已经在产线跑通的 multi-agent 模式有三类。
- ① **Code-Review-Loop**(Devin Review):
  > "Devin Review catches an average of 2 bugs per PR, of which roughly 58% are severe"
  > 反直觉:**reviewer 与 coder 完全不共享 prior context** 反而更好——干净 context 让 reviewer 走 attention math 的红利,绕过 context rot,逼它从代码 reverse-engineer spec。
- ② **Smart Friend**(Windsurf 试验):
  > 把"更聪明/更贵的模型"作为 *tool* 暴露给 primary 小模型,需要时主动 call out。
  > 关键困难:dumber model 往往不知道自己卡了;**实务 80/20 是 fork 一份 primary 完整 context 给 smart friend**;让 primary 问广(`what should I do?`),smart friend 决定有趣点。
  > Cross-frontier(Claude × GPT)版本工作良好,本质是 *capability router 而非 difficulty escalator*。
- ③ **Higher-Level Delegation**(Devin manager → child Devin via 内部 MCP):
  > 经验教训:小作用域训练出的 manager 默认 over-prescriptive,缺乏 codebase context 时翻车;agent 默认假设跟 child 共享 state(实则不共享);cross-agent communication(child 写消息回 manager,经 manager 转发给 sibling)需要训练。
- 总结:
  > "multi-agent systems work best today when writes stay single-threaded and the additional agents contribute intelligence rather than actions."
  > "The practical shape is map-reduce-and-manage: a manager splits work, children execute, the manager synthesizes and reports back."
- 给 PopolaLoom 的直接启示:把"读"和"写"在调度原语上明确区分,parallel readonly 是安全区,parallel write 是雷区。

**Scheduled Devins**(cognition.ai/blog/devin-can-now-schedule-devins):
- Devin 自己可以 schedule 周期性 Devin 任务,state 跨 run 持续。"组合 managed Devins 实现自动周度 QA"——后台调度 + 计划任务在产线已经是常态,不再是噱头。

**Jason Liu 与 Walden Yan 对谈摘录**(jxnl.co/writing/2025/09/11/why-cognition-does-not-use-multi-agent-systems):
- "Even read-only sub-agents can create problems when they return conflicting information, leaving the main agent to resolve contradictions without full context."
- 关于 Edit-Apply 模型:Cursor / Windsurf 用的"smart 写指令 + 小模型应用"模式 fragile,instructions 稍模糊就翻车。
- "If you're abiding by principles of good context engineering, your system as a whole, even if it has subparts and tasks, should feel to the user like a single agent."
- 关于 Agent 自我能力评估:
  > "In Devin... when you collaborate with Devin, Devin will give you how confident it is in its plan. And that way you, as a human, can say, like, okay, Devin is like 99% confident that this is going to work, you can roughly be hands off."
- **Universal tools > 专门 integration**:
  > "the shell command, like the bash tool, for example, is so fucking powerful... literally anything you can do on a computer like your agent can do now."
- Caching-aware framework + module-level 评估埋点是 Cognition 的内部工程支柱。

---

## 二、一线从业者观点

### 2.1 关于"单 Agent vs Orchestrator of Agents"

- **theahura(12 Grams of Carbon, 2026-02-19, "Agent orchestrators are bad")** —— 当下最具代表性的"反 orchestrator"长文(完整阅读自 simonwillison.net 的 1 月 archive 链接):
  > "If someone came to me and said 'theahura, I have a great new product, it costs 100x more than what you currently do and produces worse outputs, do you want to buy it?' I would say something like 'no.'"
  > 形式化:`D` = 长 context 的衰减,`L` = agent 间信息丢失;若 `L > D`,subagent 不划算。
  > "for most tasks, L > D, and subagents do not make sense."
  > "Building software mostly does not fall into this category. ... For software engineering, L >>> D."
  > 精彩比喻:Agent orchestrator 是"tool-shaped object"—— 它 *看起来像工具*,但你拿着它 *感觉在做工作*,实际上输出更差成本更高,只是给人在前进的错觉。
  > 仍然认可的子 Agent 用法:research(fan-out and discard)、debugging(枪试假设)、file rename(机械独立)、review(需要 fresh eyes)。
  > 对未来的看法:"agent swarms will eventually be useful... in that hypothetical future, the agent swarms will be interfacing with the user as if they were one coherent agent. It's a band-aid for not having larger models."

- **Walden Yan (Cognition) 同向**:正面承认 Anthropic 多 Agent research system 与自家"Don't Build"的结论 *不矛盾*——它们都强调 read-heavy 是第一可行区域(脚注[1] in cognition.ai/blog/multi-agents-working)。

- **Anthropic 内部对比**(同上 2025-06 文):
  > "most coding tasks involve fewer truly parallelizable tasks than research, and LLM agents are not yet great at coordinating and delegating to other agents in real time."

- **共识小结**:对编码 ➜ "默认单 Agent + 选择性 readonly subagent + 偶尔 best-of-N 投票"。对 research / breadth-first 信息采集 ➜ "orchestrator-worker 多 Agent 显著有效"。

### 2.2 关于并行 Agent 的边际收益

- **Anthropic 的 quantitative line**(2025-06):"three factors explained 95% of the performance variance in BrowseComp... token usage by itself explains 80% of the variance"——并行的最大价值是"花更多 token"。
- **Google Research 2025-12**(arxiv.org/abs/2512.08296):
  > "(2) capability saturation: coordination yields diminishing or negative returns once single-agent baselines exceed ~45%."
  > "(3) topology-dependent error amplification: independent agents amplify errors 17.2x, while centralized coordination contains this to 4.4x."
- **Cursor 2.0 (2025-10)**:推荐"多模型同 prompt 跑、人工挑结果",这种**ensemble-then-select** 的并行 *不是协作*,本质是把模型方差当成多样性资源。
- **iterathon.tech 2026-02 案例**:某客服系统 multi-agent $47k/月,单 GPT-5.2 $22.7k/月,准确率差 2.1pp(94.3% vs 92.2%)——多 Agent 的额外开销往往覆盖不了边际收益。
- **Deloitte 数据**(同上):多 Agent 编排在约 30% 用例上能提供 15–30% 价值;**对其余 70% 用例,well-prompted 单 Agent 的 1/3 成本就能提供等价结果**。
- **Walden Yan (2026-04)**:"clean context leads to a notable improvement in capabilities when using a generator-verifier loop. But clear communication and synthesis with the overall context is important for a cohesive experience."

### 2.3 关于长时运行 / 后台 Agent

- **Cursor Long-Running Agents**(2026-02-12):"plan first → wait for approval → multiple agents check each other → maintain focus across hours or days"。
- **Cognition Scheduled Devins**:周期 + state 跨 run 持续是产线已落地的形态。
- **Devin 多 Agent 架构**(leadai.dev / cognition.ai):每个 child Devin 独立 VM 隔离,父 Devin 只负责 distribution + 合成。
- **GitHub Copilot Coding Agent**:跑在 ephemeral GitHub Actions 环境中,完成后请求 review,人类 mention `@copilot` 再循环。
- **VS Code 1.109 Agent Sessions view**:把 local / background / cloud session 统一管理。
- **`agentd` daemon**(github.com/robmorgan/agentd, 2026):专门做"durable tasks owning their own git worktrees & PTYs",`agent attach` 复用——和 PopolaLoom 想要的"重新启动后能 attach 到之前的任务"几乎完全同形。
- **Emdash / tmux-assistant-resurrect**(github.com/timvw/tmux-assistant-resurrect):用 tmux/zellij 把 Claude Code、OpenCode、Codex CLI 的 session 持久化、跨重启复活——业界把"在 tmux/zellij 里跑 agent"当成 *已成熟* 的 backend。
- **Anthropic 工程教训**:"use rainbow deployments to avoid disrupting running agents"——长时 agent 必须考虑灰度发布,否则升级直接掀桌。

### 2.4 关于 Human-in-the-loop 的合适粒度

- **myengineeringpath.dev (2026)** —— "Calibrated autonomy" 主张:高置信、可逆、低风险动作给完全自治;不确定/不可逆/高风险要走人工层。"It's easier to remove unnecessary gates than add them after incidents." → 上线偏严,逐步放开。
- **Medium @arvisionlab (Anna Jey, 2026-04)** —— 三种模式都要支持:
  > 1. *human-before-action*  2. *human-on-exception*  3. *human-after-action*
- **TuringPulse 二轴决策框架**(turingpulse.ai/blog/human-in-the-loop-design):**严重性 × 置信度** 矩阵决定 sync approve / async notify / auto-execute。
- **OpenAI Agents SDK** 与 **LangGraph persistence**:已经原生提供 `interruptions` / `interrupt` / `resume` / `time travel` 这套原语,PopolaLoom 不该重复发明,而要直接选型。
- **Walden Yan 对人机协作的核心观察**(jxnl.co):
  > "When can a system recognize when it reaches its own limits of the decisions it is allowed to make... How do you know, like a you should? Either way, you should notify the human..."
  > "people hate how LLMs will just send you a wall of text. ... a smart engineer will manage to hone in on the key problems with minimal words."
- **Karpathy 2026-03-02 Tweet**(回链聚合自 walseth.ai/blog/karpathy-command-center, agent-wars.com):
  > "tmux grids are awesome, but i feel a need to have a proper 'agent command center' IDE for teams of them, which I could maximize per monitor. E.g. I want to see/hide toggle them, see if any are idle, pop open related tools (e.g. terminal), stats (usage), etc."
  > 231 条回复来自正在做多 Agent 系统的开发者——这是"agent 管理面板"成为公共痛点的最强信号。

### 2.5 关于失败处理 / 重试 / 回滚

- **Anthropic 工程教训(2025-06)**:
  > "Agents are stateful and errors compound."
  > "Without effective mitigations, minor system failures can be catastrophic for agents."
  > "We combine the adaptability of AI agents built on Claude with deterministic safeguards like retry logic and regular checkpoints."
  > "Adding full production tracing let us diagnose why agents failed and fix issues systematically."
- **GitHub blog "Multi-agent workflows often fail"**:typed schemas + action schemas + 严格 interface 是结构化护栏。
- **Squad 项目**(github.blog/ai-and-ml/github-copilot/how-squad-runs-coordinated-ai-agents-inside-your-repository):
  > "Squad enforces genuine peer review by preventing the original agent from revising rejected work; a different agent must fix it instead."
  > 用"换人重写"作为反 echo chamber 的强约束。
- **Hamel Husain (hamel.dev/blog/posts/evals/)** —— 失败处理的根因不是 agent loop,而是 *eval gap*。三层评估(unit test → model+human eval → A/B),"most teams skip Level 2 entirely, which prevents systematic improvement"。失败能被治理,前提是先能被观测和分类。
- **Eugene Yan (eugeneyan.com/writing/eval-process/)**:"An LLM-as-Judge Won't Save the Product—Fixing Your Process Will."——Agent 失败不是加 evaluator agent 就能解决,关键是 process。

### 2.6 关于成本和延迟权衡

- **Anthropic**:多 Agent 系统 token 用量 ~15× chat,**只对高价值任务经济上合理**。
- **Claude Agent Teams**:15 teammate ≈ 7× single session token。
- **Cursor 2.0**:8 agent 并行约 25–35% token overhead(相对单 Agent;数字更小因为是 ensemble 而非分工协作)。
- **Iterathon 案例**:多 Agent 客服系统 $47k vs 单 Agent $22.7k,2.1pp 准确率差。
- **Cognition Smart Friend**:让小快模型按需调用大慢模型,目标是"以更低代价摸到 frontier 能力"——但前提是 primary 足够强(SWE-1.5 不够,SWE-1.6 才够)。
- **swarm signal "When Single Agents Beat Swarms"** 量化:某些独立多 Agent 配置错误放大 17.2×;Stanford 数据显示 LLM 团队比 expert single agent 落后多达 37.6%(因为模型平均化了 expert 与 novice 视角)。
- **PopolaLoom 含义**:对于大多数 IDE 类编码任务,**Single Agent 是经济默认**;PopolaLoom 的存在价值要么是 (a) 跨任务编排的认知复杂度由它承担、解放人,(b) 多任务并行带来真实墙钟时间收益,(c) 提供事后可审计的可观测性。如果三个都不成立,就不要再加一层。

### 2.7 反模式与风险

- **Agent sprawl**(Anthropic):简单查询起 50 个 subagent。
- **Vague delegation**(Anthropic / Cognition):没有 objective / output format / boundaries 的派发,导致 subagent 重复劳动。
- **Search strategy collapse**(Anthropic):所有 agent 都收敛到 SEO 优化的内容农场。
- **Telephone game**(Cognition):full agent trace 没传过去,只传 message → 上下文丢失。
- **Conflicting implicit decisions**(Cognition):并行写产生风格/接口/边界冲突。
- **Tool-shaped object**(theahura,引 Minutes Substack):配置 agent harness *感觉像在干活*,实则 token 多花结果更差。
- **Orchestrator 自己干活**(building.theatlantic.com 2026-03):一旦 orchestrator 写代码,实现细节污染战略 context。
- **Multi-agent prompt injection cascade**(covertswarm.com / arxiv 2503.12188):**82% of 17 LLMs executed malicious commands when requested by peer agents**, even when refusing identical prompts from users。Agent 之间的"信任"是多 Agent 系统最危险的攻击面。Riley Goodside 2022 起就在强调 prompt injection 是未解的根本性问题(self.md/people/riley-goodside-prompting/)。
- **Auto-generated AGENTS.md**(morphllm.com/agents-md-guide):*降低* 任务成功率、抬高成本——human-written 才管用。

---

## 三、关键概念的 2025–2026 共识矩阵

| 维度 | 2025 主流姿态 | 2026 演进 / 修正 |
| --- | --- | --- |
| 单 Agent vs 多 Agent | "Don't build multi-agents"(Cognition 2025-06) | 收敛到"writes single-threaded, intelligence multi-agent"(Cognition 2026-04) |
| 并行 Agent 数量 | Cursor 2.0 主推 8 个并行 ensemble(2025-10) | Claude Agent Teams 15 teammate(2026-02);GitHub `/fleet` 任意扩展。**注意 token 7× 起跳** |
| Reviewer 与 Coder 是否共享 context | 直觉:共享更好 | 实证:**完全不共享** prior context 让 reviewer 表现更好(Devin Review,Cognition 2026-04) |
| Background Agent 持久化机制 | 自建 PTY,脆弱 | tmux/zellij 作为 first-class backend;agentd / Emdash / tmux-assistant-resurrect 成熟 |
| Cross-vendor Agent 通信 | 各家自定义 | A2A 1.0 (2026-03) + MCP 互补 |
| HITL 接口 | ad-hoc | OpenAI SDK `needsApproval` / LangGraph `interrupt+resume` 协议化 |
| 项目级 contract | 各家不同(CLAUDE.md / .cursorrules / etc.) | AGENTS.md 跨厂商标准,Linux Foundation 托管(2025-12) |
| 评估方法论 | "上线了再补 eval" | Eval-driven development (Hamel Husain / Eugene Yan):start with 20 cases, level 2 evals are the gap |
| Manager Agent 的能力边界 | "decompose + execute" | "decompose + delegate + verify + escalate, **never execute itself**" |

---

## 四、2025–2026 关键发布(时间线)

| 日期 | 厂商 | 发布 | 影响 |
| --- | --- | --- | --- |
| 2024-12 | Anthropic | Building Effective Agents(Schluntz & Zhang) | 确立"workflow vs agent"二分,五种 workflow pattern 成为行业词汇 |
| 2025-04 | Google | A2A Protocol 公布 | 跨厂商 Agent 互通协议有了 reference |
| 2025-06-12 | Cognition | "Don't Build Multi-Agents"(Walden Yan) | "Single agent + context engineering"成为反 swarm 的旗帜 |
| 2025-06-13 | Anthropic | "How we built our multi-agent research system" | Orchestrator-worker 在 research 领域被实证化(+90.2%) |
| 2025-08 | OpenAI | AGENTS.md 倡议 | 跨工具项目级 contract 出现 |
| 2025-10-29 | Cursor | Cursor 2.0 + Composer + 8 并行 agent | Best-of-N ensemble 成为主流并行范式 |
| 2025-12 | Linux Foundation | AGENTS.md 进入 Agentic AI Foundation | 公共协议化 |
| 2025-12 | Google Research | "Towards a Science of Scaling Agent Systems"(arxiv 2512.08296) | 多 Agent 边际效用、错误放大、capability saturation 有量化证据 |
| 2026-01 | VS Code 1.109 | Agent Sessions view | 跨厂商 Agent 在编辑器内统一管理 |
| 2026-02-05 | Anthropic | Claude Agent Teams research preview(Opus 4.6+) | P2P teammate / mailbox / 共享 task list / git locking |
| 2026-02-12 | Cursor | Long-Running Agents research preview | Plan-approve-execute,52 小时单任务跑通 151k LOC 案例 |
| 2026-02-26 | building.theatlantic.com | "Why Your AI Orchestrator Should Never Write Code" | 行业总结"orchestrator must not execute" |
| 2026-03 | GitHub | Copilot Coding Agent GA + `/fleet` | 后台 agent 进入企业默认配置 |
| 2026-03-02 | Karpathy | "agent command center" tweet | 公开承认管理面成痛点;231 回复 |
| 2026-03 | A2A | v1.0 release,Linux Foundation 托管 | 跨 SDK(Python/JS/Java/Go/Rust/C#)就位 |
| 2026-04-03 | Microsoft | Agent Framework 1.0 GA(.NET + Python) | Magentic-One ledger orchestrator 进入 enterprise |
| 2026-04-22 | Cognition | "Multi-Agents: What's Actually Working"(Walden Yan) | 给出 *map-reduce-and-manage* 这个公认范式名,reviewer/smart-friend/manager 三类落地形态被官方背书 |
| 2026-04-?? | Cognition | SWE-1.6(close to Opus-4.5 SWE-bench) | "Smart Friend" 模式解锁前提的 primary capability 到位 |

---

## 五、经验沉淀:对 PopolaLoom 的启示(15 条可执行)

> 每条都给出"出处"。把这些当成项目的"建国章程",在每次架构争论时回到这里。

1. **PopolaLoom 自己永远不写业务代码,只做调度/状态/可观测**。出处:building.theatlantic.com 2026-03("Why Your AI Orchestrator Should Never Write Code");Anthropic engineering 2025-06。
   → 实施:PopolaLoom 进程的工具白名单严格不含 `Write` / `Edit` / 任何长 shell 命令的执行权,只能 spawn / monitor / message / checkpoint / approve。

2. **派发原语区分 read 与 write**:`spawn_readonly_agent` 可以并行扇出多个,`spawn_writer_agent` 强制全局只能有一个 active。出处:cognition.ai/blog/multi-agents-working("writes stay single-threaded")。
   → PopolaLoom 的状态机里把"哪个子 Agent 持有写令牌"做成显式锁,类似 Claude Agent Teams 的 git-based locking。

3. **以 git worktree 作为默认隔离介质**。出处:Cursor worktrees doc;agentd;Cursor 2.0 release notes。
   → 每个被派发任务自动开一个 worktree,完成后 PR 化合并;天然避免冲突,符合 single-threaded write 公理。

4. **每个派发必须包含 5 件套**:objective / output format / 允许的 tool 列表 / sources / boundaries。出处:Anthropic 2025-06 第 2 条。
   → PopolaLoom 的派发 schema 把这 5 个字段做成必填校验。

5. **任务大小先用 *启发式 scaling rule* 限流**:简单查询 1 agent / 3-10 tool call;直接对比 2-4 agent / 10-15 calls 各;复杂 research >10 agent。出处:Anthropic 2025-06 第 3 条。
   → PopolaLoom 在分析任务依赖图时用类似阶梯做"该不该并行扇出"的初判。

6. **绝不 restart,只 resume**:每次 spawn / 每个状态变更落 checkpoint,可从任意 checkpoint 续跑。出处:Anthropic engineering 2025-06("we built systems that can resume from where the agent was when the errors occurred")。
   → PopolaLoom 的核心存储是事件流 + 快照,attach 操作就是 reload 最近 checkpoint + tail event stream。

7. **Background backend 直接用 tmux / zellij,不要自建 PTY**。出处:emdash issue #1571;tmux-assistant-resurrect;agentd;1devtool.com 2026 guide。
   → MVP 阶段:把每个被唤起的 CLI 都包在 tmux session 里,PopolaLoom 只持有 session id,attach 复用现成 tooling。

8. **Plan-first 接口**:与宿主 Agent 对话的 Skill / MCP 工具至少提供 `plan` / `dispatch` / `status` / `attach` / `approve`/`reject` 五个动作,默认 dispatch 之前必须先 plan 并由人确认。出处:cursor.com/blog/agent-best-practices(Plan Mode);cline.bot Plan & Act;Cursor Long-Running Agents 2026-02。

9. **HITL 三模式必须原生支持**:human-before-action(default for write)/ human-on-exception(异常或低置信度自动升级)/ human-after-action(完成即通知,可在窗口期内 reject)。出处:medium @arvisionlab 2026-04;myengineeringpath.dev 2026;OpenAI Agents SDK `needsApproval`。
   → PopolaLoom Skill 里每个 dispatch 自带 `approval_policy` 字段,enum 三选一。

10. **Reviewer Agent 与 Coder Agent 默认 *不共享* 上下文**,这是 2026 反直觉但被实证的硬经验。出处:cognition.ai/blog/multi-agents-working(Devin Review experiment);Squad("a different agent must fix it")。
    → PopolaLoom 的"自我研发-自测试"自闭环里,review 子 Agent 启动时只给 diff,不给 coder 完整 trace。

11. **Token 与时间双预算硬上限**。出处:Anthropic 2025-06(15× chat 成本);Iterathon 2026-02 案例;Claude Agent Teams 7× warning。
    → 每次 dispatch 必须填 `max_tokens` / `max_wallclock_minutes`,超出自动 pause + 通知人类。

12. **派发 prompt 让 sub-agent 优先用 *外部存储* 输出大产物,只回 reference**。出处:Anthropic 2025-06 Appendix("Subagent output to a filesystem to minimize the 'game of telephone'")。
    → 子 Agent 完成后写文件 + 返回 path/diff,而不是把全文塞回 Lead Agent context。

13. **跨厂商互通走 A2A + MCP**,自定义协议是技术债。出处:a2aproject.org;developers.googleblog.com 2025-04。
    → MVP 可以先各家 wrapper,但 PopolaLoom 内部状态/事件模型从一开始就向 A2A 概念(Capability / Task / Artifact / Push notification)对齐,后续无痛接入。

14. **可观测性高于一切**:全 trace、agent 决策模式、交互结构监控,不依赖看消息内容。出处:Anthropic 2025-06("We monitor agent decision patterns and interaction structures—all without monitoring the contents of individual conversations")。
    → PopolaLoom 必须自带 dashboard:每条派发链的 token / time / status / approval / errors,可重放。

15. **评估先行,从 20 个 case 起步**。出处:Anthropic 2025-06("We started with a set of about 20 queries");Hamel Husain ("evals are practices applying the scientific method")。
    → 在写第二行调度逻辑之前,先把 20 个真实派发场景变成 PopolaLoom 自身的 eval set:对每个场景断言"是否触发了正确的并行/串行决策",这是项目的"自我裁判"。

---

## 六、PopolaLoom 选型判别 / 应当回避的 5 个反模式

1. **写一个全自动多 Agent swarm,允许子 Agent 互相喊话改代码**。理由:Google Research 2025-12 显示 independent 拓扑放大错误 17.2×;prompt injection 在 peer-to-peer 情境下成功率 82%。**不要做**。
2. **让 PopolaLoom 自己写代码"以省 1 跳"**。理由:orchestrator 一旦执行,context 退化;实际只省了一次 spawn 的开销,失去了所有可审计/可重启的好处。**不要做**。
3. **把 background agent PTY 自己造一遍**。理由:tmux/zellij 已经是 2026 的 *de facto* 后端;造轮子会把工程时间烧在原本不需要的地方。**不要做**。
4. **默认让所有任务 fan-out 到多个 model**。理由:Cursor 2.0 推 8 并行 ensemble 但只是 *选最佳*,不是 swarm;Cognition 明确说大部分任务多 Agent 反而更差。**只在用户/任务声明可并行时才扇出**。
5. **生成 AGENTS.md 给宿主项目**。理由:morphllm.com 2026 数据显示 auto-generated AGENTS.md *降低* 任务成功率;它应当由人类拥有。PopolaLoom 可以 *读* / *提示用户更新*,但不要 *写*。

---

## 七、与 PopolaLoom 项目本身相关的进一步建议(基于本次调研)

- **MVP 第一阶段:仿 cline-agent-orchestrator + Roo Code Boomerang**(github.com/harryosmar/cline-agent-orchestrator;docs.roocode.com/features/boomerang-tasks)。两者直接是 PopolaLoom 的概念前辈,先把这种"父任务暂停 → 子任务隔离 context → 只回 summary" 的原语跑通。
- **MCP Server 形态优先于 Skill**:Skill 适合在 Cursor 等单 Agent 上 *触发* PopolaLoom,但 MCP Server 更天然支持多家 Agent CLI(Claude Code、Codex、Cursor、Gemini CLI、Copilot CLI 都已或正在支持 MCP)接入。Cognition 的 manager Devin 就是用 *内部 MCP* 协调 child Devin。
- **Self-validation loop:"派发自己改进自己"**。可以从一个最小目标做起:让 PopolaLoom 的 Eval-Set(20 个场景)由它自己派发任务给 Claude Code / Codex CLI 来跑,自动收集 token / time / approval 数据,生成 weekly report——这就是项目的"自我能力评估"机制(参考 Karpathy AutoResearch 模式:agent 自己做 700 个实验找出 20 个改进)。
- **跨 CLI 兼容层建议**:每家 CLI 的"派发"语义不同(Claude Code 通过 `Task` tool;Codex CLI 通过 `/agent` + TOML;Cursor 通过 worktree + cloud agent API;Copilot CLI 通过 `/fleet`)。建议 PopolaLoom 内部维护一个 *AgentAdapter* 抽象,把 spawn / send / status / attach / kill / cost-meter 这 6 个动作做成接口,每家 CLI 一个 implementation,后续接入新 CLI 就只是写一个 adapter。
- **HITL 通道设计参照 LangGraph + OpenAI SDK**:`interrupt`(暂停带 context 快照)、`resume(decision)`(恢复并把人类决策注入 state)、`time_travel(checkpoint_id)`(回到任意 checkpoint 重放)是协议级三件套——这套语义已经在两大 SDK 中被验证。

---

## 八、来源清单(全部带可访问链接,访问日期 2026-05-03)

### A. 厂商官方文档 / 工程博客

1. Anthropic, "Building Effective AI Agents"(Schluntz & Zhang, 2024-12-20)— https://www.anthropic.com/engineering/building-effective-agents
2. Anthropic, "How we built our multi-agent research system"(Hadfield et al., 2025-06-13)— https://www.anthropic.com/engineering/built-multi-agent-research-system
3. Anthropic Claude Code Docs, "Orchestrate teams of Claude Code sessions"— https://docs.anthropic.com/en/docs/claude-code/agent-teams
4. Anthropic Claude Code Docs, "How the agent loop works"— https://code.claude.com/docs/en/agent-sdk/agent-loop
5. Anthropic Claude SDK Docs, "Subagents in the SDK"— https://platform.claude.com/docs/en/agent-sdk/subagents
6. Anthropic Managed Agents Docs, "Multiagent sessions"— https://platform.claude.com/docs/en/managed-agents/multi-agent
7. Anthropic, "The Advisor Strategy"— https://claude.com/blog/the-advisor-strategy
8. OpenAI Codex Docs, "Subagents"— https://developers.openai.com/codex/multi-agent/
9. OpenAI Codex Docs, "Slash commands"— https://developers.openai.com/codex/cli/slash-commands
10. OpenAI Codex Docs, "Advanced Configuration"— https://developers.openai.com/codex/config-advanced
11. OpenAI Codex Docs, "AGENTS.md custom instructions"— https://developers.openai.com/codex/guides/agents-md/
12. OpenAI Agents SDK, "Agent Orchestration"— https://openai.github.io/openai-agents-js/guides/multi-agent
13. OpenAI Agents SDK, "Handoff"— https://openai.github.io/openai-agents-js/openai/agents/classes/handoff/
14. OpenAI Agents SDK, "Human-in-the-loop"— https://openai.github.io/openai-agents-js/guides/human-in-the-loop
15. Microsoft Learn, "Magentic Workflow Orchestration"— https://learn.microsoft.com/en-us/agent-framework/user-guide/workflows/orchestrations/magentic
16. Microsoft Research, "Magentic-One: A Generalist Multi-Agent System"— https://www.microsoft.com/en-us/research/articles/magentic-one-a-generalist-multi-agent-system-for-solving-complex-tasks/
17. Microsoft DevBlogs, "Microsoft Agent Framework Version 1.0"— https://devblogs.microsoft.com/agent-framework/microsoft-agent-framework-version-1-0
18. Microsoft Tech Community, "The Future of Agentic AI: Inside Microsoft Agent Framework 1.0"— https://techcommunity.microsoft.com/blog/azuredevcommunityblog/the-future-of-agentic-ai-inside-microsoft-agent-framework-1-0/4510698
19. GitHub Blog, "Run multiple agents at once with /fleet in Copilot CLI"— https://github.blog/ai-and-ml/github-copilot/run-multiple-agents-at-once-with-fleet-in-copilot-cli
20. GitHub Blog, "Multi-agent workflows often fail. Here's how to engineer ones that don't."— https://github.blog/ai-and-ml/generative-ai/multi-agent-workflows-often-fail-heres-how-to-engineer-ones-that-dont/
21. GitHub Blog, "How Squad runs coordinated AI agents inside your repository"— https://github.blog/ai-and-ml/github-copilot/how-squad-runs-coordinated-ai-agents-inside-your-repository
22. GitHub Blog, "What's new with GitHub Copilot coding agent"(2026-02)— https://github.blog/2026/02/whats-new-with-github-copilot-coding-agent/
23. GitHub Docs, "Best practices for using GitHub Copilot to work on tasks"— https://docs.github.com/en/copilot/tutorials/coding-agent/get-the-best-results
24. VS Code Blog, "Your Home for Multi-Agent Development"— https://code.visualstudio.com/blogs/2026/02/05/multi-agent-development
25. VS Code Updates, "January 2026 (version 1.109)"— https://code.visualstudio.com/updates/v1_109
26. Google Developers Blog, "Announcing the Agent2Agent Protocol (A2A)"— https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability
27. A2A Project (Linux Foundation)— https://a2aproject.org/
28. A2A Specification (GitHub)— https://github.com/google/A2A/blob/7b900e77/docs/specification.md
29. Google Cloud Docs, "Resolve endpoints and build orchestrators"— https://docs.cloud.google.com/agent-registry/resolve-endpoints-and-build-orchestrators
30. Google Cloud Docs, "Agent Designer overview"— https://cloud.google.com/gemini/enterprise/docs/agent-designer
31. Jules SDK (GitHub)— https://github.com/google-labs-code/jules-sdk
32. Jules Docs, "Getting started"— https://jules.google/docs/
33. Cursor Blog, "Best practices for coding with agents"— https://www.cursor.com/blog/agent-best-practices
34. Cursor Blog, "Cloud Agents"— https://www.cursor.com/blog/cloud-agents
35. Cursor Blog, "Introducing Cursor 2.0 and Composer"— https://www.cursor.com/blog/2-0
36. Cursor Blog, "Expanding our long-running agents research preview"(2026-02-12)— http://www.cursor.com/blog/long-running-agents
37. Cursor Changelog, "Long-running Agents in Research Preview"(2026-02-12)— https://www.cursor.com/changelog/02-12-26
38. Cursor Docs, "Cloud Agents API overview"— https://cursor.com/docs/background-agent/api/overview
39. Cursor Docs, "Subagents"— https://cursor.com/docs/agent/subagents
40. Cursor Docs, "Worktrees"— https://cursor.com/docs/configuration/worktrees
41. Cursor Docs, "Customizing Agents"— https://cursor.com/learn/customizing-agents
42. Cline Docs, "Plan & Act Mode"— https://docs.cline.bot/features/plan-and-act
43. Roo Code Docs, "Boomerang Tasks"— https://docs.roocode.com/features/boomerang-tasks
44. Roo Code Docs, "Using Modes"— https://docs.roocode.com/basic-usage/using-modes/
45. Roo Code 3.17 Release Notes(2025-05-14)— https://docs.roocode.com/update-notes/v3.17
46. cline-agent-orchestrator(2026-03)— https://github.com/harryosmar/cline-agent-orchestrator
47. LangChain Blog, "How to think about agent frameworks"(Harrison Chase)— https://blog.langchain.com/how-to-think-about-agent-frameworks
48. LlamaIndex Docs, "Multi-agent patterns in LlamaIndex"— https://docs.llamaindex.ai/en/stable/understanding/agent/multi_agent/
49. LlamaIndex `MultiAgentWorkflow` PR #17237— https://github.com/run-llama/llama_index/pull/17237
50. multi-agent-concierge demo— https://github.com/run-llama/multi-agent-concierge/

### B. 一线从业者博客 / 长文

51. Cognition, "Don't Build Multi-Agents"(Walden Yan, 2025-06-12)— https://cognition.ai/blog/dont-build-multi-agents
52. Cognition, "Multi-Agents: What's Actually Working"(Walden Yan, 2026-04-22)— https://cognition.ai/blog/multi-agents-working
53. Cognition, "Devin can now Schedule Devins"— https://cognition.ai/blog/devin-can-now-schedule-devins
54. Cognition, "SWE-1.5"— https://cognition.ai/blog/swe-1-5
55. Cognition, "SWE-1.6"— https://cognition.ai/blog/swe-1-6
56. Devin Docs, "2026 Release Notes"— https://docs.devin.ai/release-notes/2026
57. Jason Liu (jxnl), "Why Cognition does not use multi-agent systems"(2025-09-11)— https://jxnl.co/writing/2025/09/11/why-cognition-does-not-use-multi-agent-systems/
58. theahura, "Agent orchestrators are bad"(2026-02-19)— https://12gramsofcarbon.com/p/agent-orchestrators-are-bad
59. Mikhail Rogov, "Why Your AI Orchestrator Should Never Write Code"(2026-03)— https://building.theatlantic.com/why-your-ai-orchestrator-should-never-write-code-a1b5d1a2807d
60. Lance Martin (LangChain), "Learning the Bitter Lesson"(2025-07-30)— https://rlancemartin.github.io/2025/07/30/bitter_lesson/
61. Lance Martin, "Agent design patterns"(2026-01-09)— https://rlancemartin.github.io/2026/01/09/agent_design
62. Hamel Husain, "Your AI Product Needs Evals"— https://hamel.dev/blog/posts/evals/index.html
63. Hamel Husain, "Using LLM-as-a-Judge"— https://hamel.dev/blog/posts/llm-judge/index.html
64. Hamel Husain, "LLM Evals FAQ"— https://hamel.dev/blog/posts/evals-faq/index.html
65. Hamel Husain, "Evals Skills for Coding Agents"— https://hamelhusain.substack.com/p/evals-skills-for-coding-agents
66. Eugene Yan, "An LLM-as-Judge Won't Save The Product—Fixing Your Process Will"— https://eugeneyan.com/writing/eval-process/
67. Eugene Yan, "Evaluating LLM-Evaluators (LLM-as-Judge)"— https://eugeneyan.com/writing/llm-evaluators/
68. Simon Willison Archive(2026-01)— https://simonwillison.net/2026/Jan/19/scaling-long-running-autonomous-coding
69. Simon Willison, "Anthropic: How we built our multi-agent research system" 评论(2025-06-14)— https://simonwillison.net/2025/Jun/14/multi-agent-research-system/
70. swyx, "Cognition: The Devin is in the Details"— https://www.swyx.io/writing/cognition
71. swyx Latent Space, "Agent Labs: Welcome to GPT Wrapper Summer"— https://www.latent.space/p/agent-labs
72. NextBigFuture, "Andrej Karpathy on Code Agents, AutoResearch and the Loopy Era"(2026-03)— https://www.nextbigfuture.com/2026/03/andrej-karpathy-on-code-agents-autoresearch-and-the-self-improvement-loopy-era-of-ai.html

### C. Twitter/X 二手聚合(原推因平台限制无法直链,均通过聚合站点回链)

73. Karpathy 2026-03-02 "agent command center" tweet— 聚合自 https://walseth.ai/blog/karpathy-command-center 与 https://agent-wars.com/news/2026-03-15-andrej-karpathy-agentic-ide
74. Karpathy 2026 No Priors 访谈摘录— https://pjfp.com/andrej-karpathy-on-autoresearch-ai-agents-and-why-he-stopped-writing-code-full-breakdown-of-his-2026-no-priors-interview/
75. Cursor mntruell tweet "agents built a web browser"— 引用自 theahura 文(原推:x.com/mntruell/status/2011562190286045552)
76. Riley Goodside 风格 / 提示注入主题概览— https://self.md/people/riley-goodside-prompting/

### D. 实证研究 / 行业基准数据

77. Google Research, "Towards a Science of Scaling Agent Systems"(arXiv 2512.08296, 2025-12)— https://arxiv.org/abs/2512.08296
78. arXiv 2503.12188, "Multi-Agent Systems Execute Arbitrary Malicious Code"— https://arxiv.org/html/2503.12188
79. Iterathon.tech, "Multi-Agent Orchestration Economics" (2026)— https://iterathon.tech/blog/multi-agent-orchestration-economics-single-vs-multi-2026
80. Coverge.ai, "Multi-agent orchestration: patterns, pitfalls, production reality"— https://coverge.ai/blog/multi-agent-orchestration
81. SwarmSignal, "When Single Agents Beat Swarms"— https://swarmsignal.net/when-single-agents-beat-swarms/

### E. 概念专题 / 模式总结

82. AnhTu.dev, "AI Agent Orchestration — 6 Patterns for Production 2026"— https://anhtu.dev/ai-agent-orchestration-6-patterns-for-production-2026-1121
83. heyuan110.com, "Multi-Agent Orchestration: 4 Patterns That Actually Work"(2026-02)— https://www.heyuan110.com/posts/ai/2026-02-26-multi-agent-orchestration/
84. MyEngineeringPath, "LangGraph Multi-Agent — Supervisor Pattern Guide (2026)"— https://myengineeringpath.dev/genai-engineer/langgraph-multi-agent/
85. MyEngineeringPath, "Human-in-the-Loop Patterns for AI Agents (2026)"— https://myengineeringpath.dev/genai-engineer/human-in-the-loop/
86. Anna Jey (Medium), "Human-in-the-Loop AI Agents: How to Add Approvals, Escalation, and Safe Autonomy in Production"(2026-04)— https://medium.com/@arvisionlab/human-in-the-loop-ai-agents-how-to-add-approvals-escalation-and-safe-autonomy-in-production-0a21e359781c
87. TuringPulse, "Human-in-the-Loop Done Right: Designing Review Gates That Scale"— https://turingpulse.ai/blog/human-in-the-loop-design
88. AgentPatterns, "LLM Map-Reduce Pattern"— https://agentpatterns.ai/multi-agent/llm-map-reduce/
89. Stochastic Sandbox, "AI Agent Orchestration Patterns"(2026-04)— https://stochasticsandbox.com/posts/ai-agent-orchestration-patterns-2026-04-21/
90. Tianpan Cao, "Effective Context Engineering for AI Agents"(2026-02)— https://tianpan.co/blog/2026-02-23-effective-context-engineering-for-ai-agents
91. ToolHalla, "Context Engineering for AI Agents (2026)"— https://toolhalla.ai/blog/context-engineering-ai-agents-2026

### F. Background / Persistent Agent 工具

92. agentd(durable agent daemon)— https://github.com/robmorgan/agentd
93. Emdash issue #1571 "Persistent session backends"— https://github.com/generalaction/emdash/issues/1571
94. Emdash issue #1215 "Optionally back PTY sessions with tmux"— https://github.com/generalaction/emdash/issues/1215
95. tmux-assistant-resurrect— https://github.com/timvw/tmux-assistant-resurrect
96. 1devtool.com, "Persistent Terminal Sessions for Coding: 2026 Guide"— https://1devtool.com/blog/persistent-terminal-sessions-for-coding-guide-2026

### G. AGENTS.md 标准生态

97. vibecoding.app, "AGENTS.md Guide (2026): Copilot, Cursor & More"— https://vibecoding.app/blog/agents-md-guide
98. morphllm.com, "AGENTS.md & SKILL.md: Complete Guide (2026)"— https://www.morphllm.com/agents-md-guide
99. blakecrosley.com, "AGENTS.md Patterns: What Actually Changes Agent Behavior"— https://blakecrosley.com/blog/agents-md-patterns

### H. 安全 / 风险

100. CovertSwarm, "Inject one agent, own them all: cascading risk of multi-agent AI"— https://www.covertswarm.com/post/multi-agent-ai-security-risks
101. COMPEL Framework, "Goal Hijacking, Excessive Agency, and Prompt-Injection Cascades"— https://www.compelframework.org/articles/goal-hijacking-excessive-agency-and-prompt-injection-cascades
102. MrDuc (Medium), "The Lethal Trifecta: How Indirect Prompt Injection Is Breaking Agentic AI"(2026-03)— https://medium.com/@itpro677/the-lethal-trifecta-how-indirect-prompt-injection-is-breaking-agentic-ai-and-what-security-teams-c2ecba874ed1

---

## 附录 A · 行业上"Agent of Agents" 命名速查

| 概念 | 谁创造 / 强化 | 可读出处 |
| --- | --- | --- |
| Orchestrator-worker | Anthropic 2024-12 | building-effective-agents |
| Agent-of-agents (negative) | theahura 2026-02 | 12gramsofcarbon.com 该文 |
| Agent Teams (peer-to-peer) | Anthropic 2026-02 | claude code agent-teams docs |
| Boomerang task | Roo Code | docs.roocode.com/features/boomerang-tasks |
| Smart Friend | Cognition Windsurf 2026 | cognition.ai/blog/multi-agents-working |
| Map-reduce-and-manage | Cognition 2026-04 | 同上 |
| Manager Devin / Child Devin | Cognition 2026-04 | 同上 |
| Squad / Coordinator + Specialists | GitHub/bradygaster | github.blog Squad post |
| `/fleet` Copilot CLI | GitHub | 同 GitHub blog |
| Magentic / Magentic-One | Microsoft | learn.microsoft.com agent-framework magentic |
| Supervisor / Swarm | LangGraph | langgraph-supervisor / langgraph-swarm |
| AgentWorkflow / Concierge | LlamaIndex | docs.llamaindex.ai multi_agent |
| A2A / MCP | Google + Anthropic | a2aproject.org / modelcontextprotocol.io |
| Long-running agent | Cursor 2026-02 | cursor.com/blog/long-running-agents |
| Scheduled Devin | Cognition | cognition.ai/blog/devin-can-now-schedule-devins |
| Agent command center (term) | Karpathy 2026-03-02 | walseth.ai/blog/karpathy-command-center |

## 附录 B · 关于本文有意识省略 / 弱处理的内容

- **AutoGen 0.x 框架细节**:已被 Microsoft Agent Framework 1.0 整合;读者直接看 1.0 即可。
- **CrewAI 详细 API**:多次被 Cognition 与 theahura 列入"过度抽象"清单,作为反例提到即可。
- **Devin 特定企业指标**:cognition 自家披露的 ~8× enterprise 增长仅作 *动机* 引用,不作为 PopolaLoom 的设计依据。
- **AGI / 长期路线图争论**:与 PopolaLoom 当前阶段无关,仅在 Karpathy / swyx 段落中点到为止。
- **Riley Goodside 个人 Twitter 原推**:无法直链,仅以 self.md 聚合页和 covertswarm 综述代替,凡涉及的"82% LLM 在 peer agent 命令下执行恶意代码"数字均给 arXiv 直链。

---

*文档版本 v1.0 / 2026-05-03 撰写 / 调研者:L3 Task Agent T4 (Research) / 后续维护建议:每月扫一次 Cognition、Anthropic engineering、Cursor blog、Cline/Roo Code release notes、A2A 规范变更。*
