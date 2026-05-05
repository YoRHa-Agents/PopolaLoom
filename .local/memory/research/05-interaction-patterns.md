# 05 · 调度 ↔ Agent ↔ Human 三方交互模式

> 研究范围: PopolaLoom 作为编织者(loom),需要同时面对 (a) 它派发的 Agent CLI 进程和 (b) 它服务的人类开发者两类对端。
> 本文聚焦 (b) 即 PopolaLoom ↔ Human 这条边,并在 Phase 1 形态选型(Skill vs Local MCP vs CLI vs Hybrid)上给出明确推荐。
> 全文综合 MCP 规范、Claude/Cursor Skills 文档、Cursor SDK、Temporal、Argo、Airflow、LangGraph、AutoGen、Prefect、LangSmith、Slack ChatOps、Linear Agents、tmux/mosh、CloudEvents/NDJSON 等共 30+ 来源,每条外部主张均带脚注链接。
> 上游同伴文档: `01-architecture-survey.md`(同期 T1 整体编排架构)、`02-task-dependencies.md`(T2 依赖图原语)、`03-cli-orchestration.md`(T3 Agent CLI 进程接口)、`04-state-persistence.md`(T4 持久化与崩溃恢复)。本文专注于交互动词与表现形态。

---

## 全景图

### 三方关系简要 ASCII

```
                       ┌──────────────────────────────┐
                       │           Human              │
                       │  (developer / operator /     │
                       │   requester / reviewer)      │
                       └──────────────┬───────────────┘
                                      │
                                      │  Phase-1 接入面 (skill / mcp / cli / web)
                                      │  动词: submit, list, status, attach,
                                      │       feedback, cancel, resume, inject
                                      ▼
                       ┌──────────────────────────────┐
                       │       PopolaLoom Loom        │
                       │  (DAG planner + dispatcher + │
                       │   state store + event bus)   │
                       └──────────────┬───────────────┘
                                      │
                       spawn(stable)  │  attach/log_tail
                       NDJSON event   │  signal/cancel
                                      ▼
       ┌──────────────────────────────────────────────────────────┐
       │   Agent CLIs:                                            │
       │   claude-code · cursor-agent · codex · kimi · copilot    │
       │   (spawned subprocess; resumable session id; PTY)        │
       └──────────────────────────────────────────────────────────┘
```

### 三类事件流

| # | 事件流 | 方向 | 触发者 | 频率 | 示例载荷 |
|---|---|---|---|---|---|
| E1 | 控制流 (control plane) | Human → Loom | 人 | 偶发 | submit_plan / cancel_task / inject_subtask / supply_feedback |
| E2 | 进度流 (progress plane) | Loom → Human | 系统 | 持续低频 + 突发 | task.started / task.heartbeat / task.tool_call / task.completed |
| E3 | 中断流 (interrupt plane) | Loom → Human → Loom | 双向同步 | 罕见但阻塞 | needs_input / approval_required / clarification_question |

> 这三层与 MCP 规范中的 client→server `tools/call`、server→client SSE 通知、以及 server→client `elicitation/create` 三类消息几乎一一对应[^mcp-elicit][^mcp-streamhttp];也与 Temporal 的 Workflow Execution、Event History、Signal/Query 三层近似[^temporal-very-long]。这一同构性是后文推荐"以 MCP 为骨架"的核心论据之一。

### 关键张力

1. **同步 vs 异步**: 人类期望"提交即转身离开",但有时 Agent 必须立即问问题。
2. **推送 vs 拉取**: IDE 内 agent 可被 LLM 自然驱动来"轮询",但纯人类用户更适合接受推送通知。
3. **进程依赖 vs 独立**: 用户关闭 IDE 后,Loom 必须能继续跑;反过来,Loom 重启后必须能复原所有"在飞"的 Agent。
4. **在地编辑 vs 远端编辑**: 用户可能在 IDE / 终端 / 手机三处来回切换;状态必须可在任何客户端 attach。

---

## 表现形态对照(给 Phase 1 选型用)

| 形态 | 启动方式 | 长进程支持 | 状态查询 | HITL 中断 | IDE 集成 | 实施成本 | 给 PopolaLoom 的契合度 |
|---|---|---|---|---|---|---|---|
| **Skill** (`SKILL.md` + scripts) | 宿主 Agent 在系统提示里看到元数据,按需"读入"指令体[^claude-skills][^cursor-skills] | ⚠ 弱: Skill 本身只是**指令包**,可调用 Bash 但不持有进程;若需后台,要在 Skill 里启动外部 daemon[^claude-skills] | 通过 Skill 内的 shell 脚本 `popola status` 间接查 | ⚠ 间接: 宿主 Agent 看到 Skill 输出后,**由它自己**问用户;Skill 不能直接弹窗 | 极佳(Cursor `.cursor/skills/` & Claude `.claude/skills/` 即装即用)[^cursor-skills][^claude-skills] | 低: 一个目录 + 一个 `SKILL.md` + 几个脚本 | **高**(开箱即用 + 跨多个 IDE Agent 复用) |
| **Local MCP Server** (stdio 或 HTTP) | 宿主 IDE/Agent 在 `mcp.json` 配置启动;通常是 `stdio` 子进程[^mcp-stdio][^mcp-streamhttp] | ✅ 强: MCP server 是独立进程,可挂 daemon、SQLite、内存队列;`Streamable HTTP` 还支持 session resumability[^mcp-streamhttp] | ✅ 一等公民: 暴露 `popola_status`、`popola_list`、`popola_logs` 等工具 | ✅ 一等公民: `elicitation/create` 直接弹用户表单 (form/url 模式)[^mcp-elicit] | 极佳(Claude/Cursor/VS Code/Cline 等几乎全栈支持 MCP) | 中: 需写 server 实现、注册工具、处理生命周期 | **极高**(交互动词全覆盖,且语义标准化) |
| **CLI** (`popola submit/list/...`) | 用户在终端直接 `popola submit plan.yaml`;脚本可调 | ✅ 强: CLI 起的进程独立于 IDE | ⚠ 拉: 用户得显式跑 `popola status`;需要轮询 | ❌ 弱: CLI 退出后无法主动通知;只能依赖 OS 通知或在 stdout 阻塞等待 | 间接(IDE Agent 可以"调用 shell"间接用,但失去结构化返回)[^codex-noninteractive] | 极低: argparse / cobra 即可 | **中**(必备的 escape hatch,但不该是 Phase 1 唯一通道) |
| **TUI / Web Dashboard** | 用户浏览器打开 `localhost:PORT`;借鉴 Temporal Web、Argo UI、Prefect 模式[^temporal-ui][^argo-ui][^prefect-interactive] | ✅ 强: 后端是独立 service | ✅ 视觉化: DAG、时间线、event history(类 Temporal Timeline)[^temporal-ui] | ⚠ 半: 用户必须主动打开页面才能看到"待审批" | 弱: IDE 内不直接可见;需切窗口 | 高: 需要写前端 + REST/WebSocket | **中**(Phase 2 推荐,Phase 1 可作为可选可视化) |
| **Chat Bot** (Slack/Lark/Discord) | 用户 `@PopolaBot submit ...`;借鉴 Slack Block Kit 审批按钮[^slack-blockkit] | ✅ 强: 后端是 service | ✅ 推送式: 状态变化主动 ping 用户 | ✅ 强: Block Kit Allow/Deny 按钮 + thread 上下文[^slack-blockkit] | 弱: 与 IDE 解耦 | 高: 需做 OAuth、webhook、bot 平台适配 | **中-低**(企业团队场景适合,个人开发不必要) |
| **Hybrid Skill + Local MCP** | Skill 元数据声明触发条件 → Skill 内一行 `popola-mcp` 启动 stdio MCP → 宿主 Agent 通过 MCP 工具实施[^claude-skills][^mcp-stdio] | ✅ 强(MCP 进程 + 可选 systemd 兜底) | ✅(MCP 工具) | ✅(MCP elicitation) | 极佳 | 中: Skill 部分零成本,MCP 部分中等 | **最高**(组合 Skill 的"发现性"与 MCP 的"能力面")—— **本文最终推荐** |
| **IDE 插件 (VS Code Ext.)** | 用户从 marketplace 装;插件用 LSP-style `WorkDoneProgress` 报进度[^vscode-toolprogress][^vscode-workdone] | ✅ 强(插件可起 long-running task) | ✅(插件 UI 面板 + 状态栏) | ✅(VS Code 原生 quickPick / inputBox) | 完美(同 IDE) | 高: 需 npm 包 + VSIX 发布 + 插件评审 | **中-低**(锁定单一 IDE,违背"多 IDE 兼容"目标) |

> 注: "宿主 Agent" 指用户使用的 IDE 内 Coding Agent(Cursor Agent、Claude Code、Cline、Continue 等)。Skill 与 MCP 都需要"宿主"的存在;但同一个 MCP server 可以同时被多个宿主连接,Skill 文件则需在每个宿主的 skill 目录各放一份(或用符号链接)。

---

## 异步与 HITL 模式

### Pause-for-input 七种实现对比

| 框架 / 范式 | 触发方 | 阻塞模型 | 状态保留 | 跨进程恢复 |
|---|---|---|---|---|
| **MCP Elicitation** (`elicitation/create`) | Server 在 tool call 处理过程中 | Server 阻塞等待 client 响应,但**必须**关联到一个 originating client request,不允许"独立"server-initiated 通知[^mcp-elicit] | 仅在内存,直到 tool call 返回 | 不直接支持(但可由 server 自行落库) |
| **MCP Sampling** (`sampling/createMessage`) | Server 反过来要求 client 跑一次 LLM 推理[^mcp-sampling] | 同上,sync round-trip | 仅在内存 | 不直接支持 |
| **Temporal Signal** | 外部代码(任何 SDK/CLI) | Workflow `await Workflow.awaitConditional(...)`(可等数小时/数天)[^temporal-signal] | **History 持久化**,signal 是 history event | ✅ Worker 宕机重启自动 replay,signal 不丢 |
| **Temporal Query** | 外部代码 | sync, read-only | 不变更状态 | ✅ |
| **Argo Workflows Suspend** | Workflow 模板里写 `suspend: {}` | 直到 `argo resume` 或超时[^argo-suspend] | etcd CRD 持久化 | ✅(整个 Argo 控制面就是 K8s controller) |
| **Argo Intermediate Parameters** | UI 在 suspend 节点弹下拉 / 字符串输入 | 同上[^argo-intermediate] | 同上 | ✅ |
| **Airflow Sensor (poke)** | Worker 周期性轮询 | 占据 worker slot[^airflow-sensor] | DB 持久化 task instance | ✅ |
| **Airflow Sensor (reschedule)** | Worker 醒来检查后释放 slot | 不占 worker | DB 持久化 | ✅ |
| **Airflow Deferrable Operator** | Triggerer 异步事件循环 | 释放 worker,事件到达再唤醒[^airflow-deferrable] | DB | ✅(triggerer 是独立进程) |
| **LangGraph `interrupt()`** | 节点内调用 | 直到 `Command(resume=...)`[^langgraph-interrupt] | **必须**配 checkpointer(SQLite/Postgres) | ✅ |
| **AutoGen UserProxyAgent** | 团队对话推进时 | 直接 `input()` 阻塞 process[^autogen-userproxy] | 仅内存 | ❌(进程退出即丢) |
| **Prefect `pause_flow_run()`** | flow 内 | UI 表单提交才解阻[^prefect-interactive] | DB | ✅ |

**关键洞察**:

1. MCP Elicitation 看似为 PopolaLoom 的"asks"量身打造,但有一条硬限制——**server-to-client 请求必须关联到一个正在处理的 client request**[^mcp-elicit]。也就是说,Loom 不能在用户合上 IDE 三小时后,通过现有 MCP 连接"主动"弹问题;只能等用户下一次调用 `popola_status`/`popola_attach` 时,趁机会把 pending 问题塞回去。这一限制对 Phase 1 影响巨大,直接决定了"反馈通道"必须是**拉模型**而非纯推模型。
2. Temporal/LangGraph/Argo 的可恢复性都建立在**外部检查点存储**之上(History、SQLite、etcd)。PopolaLoom 必须在第一天就把"task state + pending interrupt + event log"落地到本地文件 + SQLite。
3. AutoGen 的 `human_input_mode='ALWAYS'` 在 web 场景跑不通[^autogen-userproxy],是反面教材——纯 stdin 阻塞不可移植。

### Approval Gate 模式

```
┌─────────────────────────────────────────────────────────────┐
│ 1. Loom 检测到 high-risk action (rm -rf, push --force, ...)│
│ 2. 把 task 状态置为 awaiting_approval, 入队 pending_inputs │
│ 3. 通知通道择一: ① IDE 端 Skill/MCP 拉时返回 ② Slack DM   │
│    ③ 桌面通知 ④ 邮件                                       │
│ 4. 用户 approve/deny 后,Loom 把 decision 写回 task,继续  │
└─────────────────────────────────────────────────────────────┘
```

借鉴: Argo Suspend + Intermediate Parameters[^argo-intermediate]、Slack Block Kit Allow/Deny 按钮 + `thread_ts` 保留对话线索[^slack-blockkit]、LangGraph `interrupt_before` 在工具调用前暂停[^langgraph-interrupt]。

> ⚠️ 错误模式: Argo 自己曾在 v3.x 出过 bug——suspend 弹的字符串输入 prompt 在 UI 上不显示[^argo-suspend-string-bug]。教训: 输入框 UI 比布尔/枚举难设计,Phase 1 应优先支持枚举(approve/deny/abort/edit)。

### Notification 推 vs 拉

| 范式 | 推(push) | 拉(pull) |
|---|---|---|
| 触发者 | Loom 主动发 | 客户端定时查 |
| 适合场景 | 关键中断、风险审批 | 常规状态、日志 tail |
| 技术 | Webhook、Slack message、桌面通知、SSE 流 | `popola status` CLI、MCP `tools/call` |
| 与 MCP 兼容 | ❌ 受 originating-request 限制[^mcp-elicit] | ✅ 完美贴合 |
| 与 IDE Agent 兼容 | ❌(IDE 不一定有桌面通知权限) | ✅(IDE Agent 可被自然语言驱动来"再问一下进度") |
| Linear Agent Bridge 经验 | 用 webhook + Agent Activities 推 | — [^linear-agents] |

**结论**: Phase 1 主推 + 主拉 双通道:
- **拉为主**: 通过 MCP `popola_status` 工具,IDE Agent 在被用户问起时取最新进度并把 pending interrupt 一并返回。
- **推为辅**: 操作系统级桌面通知(`notify-send` / `osascript display notification`)用于"必须立刻看见"的事件,如 approval_required。

### 长等待后恢复(Checkpoint + Replay)

参考 Temporal "very long running workflows"[^temporal-very-long] + LangGraph checkpointer[^langgraph-interrupt]:

```
event_log/  (NDJSON, append-only)
  ├── 2026-05-03T02:00:00Z  task.created   {id: T-1, plan: ...}
  ├── 2026-05-03T02:00:03Z  task.dispatched {id: T-1, agent: claude-code, pid: 12345}
  ├── 2026-05-03T02:01:05Z  task.heartbeat  {id: T-1, last_seen: ...}
  ├── 2026-05-03T02:30:00Z  task.elicited   {id: T-1, schema: {...}, message: "Which DB?"}
  ├── 2026-05-03T08:00:00Z  human.responded {id: T-1, value: "postgres"}
  └── 2026-05-03T08:00:01Z  task.resumed    {id: T-1}
checkpoints/  (snapshot every N events)
  └── T-1.checkpoint.json  {state: ..., last_event_seq: 117}
```

这正是 NDJSON event-stream 协议的天然用例[^ndjson-streaming],每行一个完整事件,支持随时间游标 replay,无需重建复杂二进制结构。事件本身可以套 CloudEvents 规范的最小信封(`id`/`source`/`specversion`/`type`)以便日后跨平台互通[^cloudevents-spec]。

---

## Attach / Detach / Re-enter 范式

### tmux/screen 模型(参考线: 35 年前就解决的问题)

**核心架构**[^tmux-architecture][^tmux-data]:
- **Client-Server**: tmux server 是常驻 daemon,clients 是短命连接。
- **Session 是状态主体**: session 持有 windows / panes / cwd / env / 引用计数。
- **多 client 可同时 attach 同一 session**: server 维护 `loop_clients` 列表,broadcast 输出。
- **detach 不杀 session**: client 退出只减少 ref count,server 继续跑。

PopolaLoom 类比:
- Loom = tmux server(常驻 daemon,管理所有 task)
- task = session(每个 task 持有 agent process、log buffer、checkpoint)
- IDE Agent / CLI / Web UI = client(可来可去)

### Claude Code 会话恢复

`claude --resume <session-id>` 从 `.claude/sessions/` 读 transcript[^claude-resume]。**局限**: session 文件绑定到 cwd,跨 client 不通(2026 年仍是 feature request[^claude-cross-client])。Phone-spawned remote session attach 也仍是 RFC[^claude-attach]。

### Cursor 三层模型

1. **CLI session** (`cursor-agent --resume [chatId]`)[^cursor-cli-resume]: 同 Claude,本地文件。
2. **Background Agent**(本地后台进程,Composer Cmd+I → Background)[^cursor-bg]: 与 IDE 解耦,IDE 关了也跑。
3. **Cloud Agent** (`bc-` 前缀 ID,`Agent.resume(...)` 跨进程拿回 handle)[^cursor-cloud][^cursor-sdk]: 真正的 server-side persistence,可在 Web/Slack/手机看。

`@cursor/sdk` 给出三种调用范式: `Agent.prompt`(一次性)、`Agent.create + send`(带 follow-up)、`Agent.resume`(跨进程)[^cursor-sdk]。SDK 文档明确警告: cloud agent ID 不是 run ID,要用 `Agent.getRun(runId, {runtime: "cloud", agentId})`[^cursor-sdk]——这是状态空间设计的反面教材,PopolaLoom 应避免在 ID schema 中混淆 task vs run。

### Codex CLI

`codex exec --session <UUID>` 或 `--resume-rollout <path>`[^codex-resume]。
**Background terminal sessions 仍是 open feature**[^codex-bg]:RFC 提议的 UX `codex run --bg --name foo` / `codex sessions list/attach/stop`,与 PopolaLoom 想要的能力几乎一致——这正是市场机会窗口的信号。

### mosh 反例

mosh 用 SSP(State Synchronization Protocol)做 IP 漫游[^mosh-design]:UDP + 心跳 + 状态同步对象。**但**它**不**支持从另一台 client 重新 attach[^mosh-reattach]——设计者明确说"想要多 client attach 请用 screen/tmux"。
**给 PopolaLoom 的教训**: 移动友好的网络层重连(IP 变化)与"多 client 共享同一 session"是**两件事**;前者是传输层的 nice-to-have,后者是会话层的 must-have。Phase 1 优先后者。

### 数据库持久化 + Replay 模型(Temporal 流派)

Temporal 的设计选择: **Event History 是真相来源**,worker 按 history 重放代码恢复状态[^temporal-very-long]。优点:
- Worker 完全无状态,挂了换一个就行。
- Signal 也是 history event,人类决策永久落地,不会因系统崩溃丢失[^temporal-hitl]。

PopolaLoom 应采用同一模式: Loom daemon 自身可崩溃可重启;真相在 `~/.popola/event_log/*.jsonl`。

### 五种 attach 模型映射

| 模型 | 代表 | PopolaLoom 适用度 | 备注 |
|---|---|---|---|
| Multiplexer (server + multi-client) | tmux | ⭐⭐⭐⭐⭐ | 推荐主架构 |
| Session ID + replay | Claude/Codex/Temporal | ⭐⭐⭐⭐⭐ | 必备,作 multiplexer 之下的真相层 |
| Stateful network reconnect | mosh | ⭐⭐ | 不是 Phase 1 重点 |
| Cloud-side persistence | Cursor Cloud Agent | ⭐⭐⭐ | Phase 2 可选(本地优先) |
| IDE plugin in-process state | VS Code 插件 | ⭐ | 容易丢 |

---

## Skill vs MCP Server 详细取舍

### 三个 Phase-1 候选

#### 候选 A: 纯 Skill (`popola-loom/SKILL.md`)

**结构**:

```
~/.claude/skills/popola-loom/
~/.cursor/skills/popola-loom/   (或 .cursor/skills/ 项目级)
└── SKILL.md
└── scripts/
    ├── submit.sh
    ├── status.sh
    ├── attach.sh
    └── feedback.sh
└── references/
    └── verbs.md
```

**触发**: 宿主 Agent 在系统提示里看到 frontmatter `description`,匹配到"orchestrate / dispatch / multi-agent / 调度"等关键词时进入 Level 2,把 SKILL.md 完整内容拉进 context[^claude-skills]。

**能力上限**:
- ✅ 用 shell 脚本可以做到任何事(包括启动 daemon)
- ✅ Cursor 的 `disable-model-invocation: true` 可强制只通过 `/popola` 触发,避免误触发[^cursor-skills]
- ❌ 没有结构化返回值——shell stdout 全靠 LLM 解析
- ❌ 没有标准的"server initiated" 通道,所有交互必须由 LLM 在每次调用时"正好问一下"
- ❌ 跨多个 IDE 安装时,每个 IDE 各管各的 skill 目录(Cursor `.cursor/skills/`, Claude `.claude/skills/`)

**成本**: 极低(~ 1 天 MVP)。

#### 候选 B: 纯 Local MCP Server (`popola-mcp`)

**结构**:

```
popola-mcp (可执行)
└── stdio 协议 (默认)  / 或 HTTP+SSE (可选, deprecated June 30 2026)
└── tools:
    ├── popola_submit_plan(plan_yaml: string)
    ├── popola_list_tasks() -> [{id, state, ...}]
    ├── popola_get_status(task_id: string)
    ├── popola_tail_log(task_id: string, since_seq: int)
    ├── popola_attach(task_id: string)  -> stream events
    ├── popola_supply_feedback(task_id, value)
    ├── popola_inject_subtask(parent_id, task_def)
    └── popola_cancel(task_id, reason)
└── elicitation/create  使用 Form mode 弹 approve/deny
```

**注册**: 用户在宿主 IDE 的 `mcp.json` 配置:

```json
{
  "mcpServers": {
    "popola": {
      "command": "popola-mcp",
      "args": ["--stdio"]
    }
  }
}
```

**能力上限**:
- ✅ 工具是一等公民,LLM 可以直接调,语义清晰
- ✅ Tool annotations(`destructiveHint`, `idempotentHint` 等)可以教 LLM 谨慎调用 cancel[^mcp-tool-annot]
- ✅ Elicitation 在 LLM 处理工具时可以同步弹问题给用户[^mcp-elicit]
- ⚠ Server-initiated 必须关联到 originating client request,不能"主动"推消息[^mcp-elicit]
- ✅ Streamable HTTP transport 支持 session resumability(若不用 stdio)[^mcp-streamhttp]
- ❌ 用户必须先在 IDE 里配置才能用,有上手摩擦
- ❌ 同一个 MCP server 进程是否被多个 IDE 共享要看实现(stdio 模式天然每 IDE 一份子进程,这恰好不利于"同一个 task 让多 IDE 看见")

**成本**: 中(~ 1 周 MVP,含 schema 设计 + 工具实现 + 持久化对接)。

#### 候选 C: Hybrid Skill + Local MCP(**最终推荐**)

**思路**:
- Skill 是**入口和发现层**: 用户/IDE Agent 看到 SKILL.md 就知道"这里有个 PopolaLoom 可以用"。
- MCP 是**能力层**: SKILL.md 里只放 ~30 行说明文 + 一句"工具列表见 MCP server `popola`",触发时 SKILL 帮宿主 Agent 调好 MCP。
- 同时,**底层 daemon 进程**(`popolad`)独立运行,用户可以在终端 `popola submit ...` 直连 daemon,绕开宿主 Agent。

```
┌──────────────────────┐    ┌───────────────────────┐
│  Cursor / Claude     │    │   Terminal user       │
│  IDE Agent           │    │   (CLI: popola ...)   │
└──────────┬───────────┘    └────────────┬──────────┘
           │ /popola or auto              │
           ▼                              │
   ┌───────────────────┐                  │
   │ Skill (SKILL.md)  │                  │
   │  发现 + 触发      │                  │
   └────────┬──────────┘                  │
            │ exec popola-mcp --stdio     │
            ▼                              │
   ┌───────────────────┐                  │
   │ popola-mcp (子进程)│                 │
   │  MCP tools/elicit │                  │
   └────────┬──────────┘                  │
            │ unix socket / gRPC          │
            ▼                              ▼
   ┌──────────────────────────────────────────┐
   │       popolad (独立 daemon)              │
   │  DAG planner + state store + event bus   │
   │  spawn agent CLI subprocesses            │
   │  ~/.popola/event_log/*.jsonl             │
   └──────────────────────────────────────────┘
```

**为何最优**:
1. **零认知负担发现**: 用户在 Cursor 里说"帮我跑一下这个多任务计划",IDE Agent 自动看到 Skill 元数据,自动调 MCP。
2. **能力与发现解耦**: 同一个 `popolad` daemon 服务多个 IDE,且 CLI 也直连,不会因为 IDE 关了就失联。
3. **跨 client 一致**: 所有 client(IDE Skill、CLI、未来的 Web UI)都打到同一个 daemon,同一个 event log,同一份真相。
4. **失败模式可控**: 即使 MCP 实现有 bug,CLI 仍然可用作 escape hatch。

**Phase 2 演进路径**:
- 把 `popolad` 变成可选远程,保留 `popola-mcp` 作为前端代理(类比 Cursor Cloud Agents 通过 SDK 远程访问的模式[^cursor-sdk])。
- 加一个轻量 Web UI (vite + DAG.js),嵌入 IDE 内 webview。
- 加 Slack/Lark Bot 适配器订阅 daemon 的 NDJSON event stream,做企业团队场景。

### 决策矩阵(逐项打分,1=差 5=好)

| 维度 | A: Skill | B: MCP | C: Hybrid |
|---|---|---|---|
| 上手成本 | 5 | 3 | 4 |
| 长进程支持 | 2 | 4 | 5 |
| 结构化交互动词 | 2 | 5 | 5 |
| HITL 中断 | 2 | 4 | 4 |
| 跨 IDE 复用 | 4 | 5 | 5 |
| Daemon 解耦 | 1 | 3 | 5 |
| Phase 2 演进路径 | 2 | 4 | 5 |
| 综合 | 18/35 | 28/35 | **33/35** |

---

## 端到端用户旅程模拟

### 场景 1: 在 IDE 提交多任务并稍后回来查看

**前提**: 用户在 Cursor IDE 中起草了一个 4 阶段计划(survey → design → impl → test),把它写成 `plan.yaml`。

**步骤**:

1. 用户在 Cursor Composer: "帮我把 plan.yaml 提交给 PopolaLoom 跑起来"。
2. Cursor Agent 检测到 SKILL.md 元数据(描述里有 "orchestrate / dispatch / multi-task")自动加载[^claude-skills][^cursor-skills],进入 Level 2 看到工具列表。
3. Cursor Agent 调用 MCP `popola_submit_plan({plan_yaml: <file content>})`,返回 `{plan_id: "P-2026-05-03-01", task_ids: ["T-1", "T-2", "T-3", "T-4"]}`。
4. `popola-mcp` 把 submit 命令转给 `popolad` daemon。daemon 在 `~/.popola/plans/P-2026-05-03-01/` 落盘,启动 `T-1` (survey) 子进程(`claude-code` CLI 子进程,设 `setsid` 脱离终端 process group,符合 long-running orchestrator 模式[^temporal-very-long])。
5. Cursor Agent 把 `plan_id` 报告给用户:"已提交,你可以关 IDE 了"。
6. **3 小时后**用户重开 Cursor。在 Composer: "PopolaLoom 那个计划跑得怎么样了"。
7. Cursor Agent 看到上下文中没有 plan_id,主动调 `popola_list_tasks()` 看到 `[{P-2026-05-03-01, state: in_progress, ...}]`,继续 `popola_get_status("P-2026-05-03-01")`。
8. daemon 从 SQLite + event_log 中拉出最新进度(类似 Temporal 的 history replay[^temporal-very-long]),返回 `{T-1: completed, T-2: in_progress (45%), T-3: pending, T-4: pending}`。
9. Cursor Agent 用自然语言汇报。

**关键设计点**:
- IDE 关闭过程中 daemon 必须独立存活——通过 `setsid` + 写 PID 文件 + 可选 systemd-user unit。
- plan_id 不依赖 IDE session,可在任意 client 查到。
- 第 7 步是"拉模型"的标准用法,完美利用 LLM 的工具调用能力。

### 场景 2: Agent 中途需要人类输入

**前提**: 场景 1 的 `T-3` (impl) 跑到一半,代码生成 Agent 不确定该用 PostgreSQL 还是 SQLite。

**步骤**:

1. `T-3` 子进程的 NDJSON 输出流里出现 `{type: "needs_input", schema: {oneOf: ["postgres", "sqlite"]}, message: "Which DB?"}`(借鉴 LangGraph interrupt[^langgraph-interrupt] 与 NDJSON discriminated-union[^ndjson-streaming])。
2. daemon 解析这一行,把 `T-3.state = awaiting_input`,把 schema 入队 `pending_inputs[T-3]`,写一条 `task.elicited` 事件到 event_log。
3. **同时**触发桌面通知: `notify-send "PopolaLoom T-3 needs input"`。
4. **用户场景 A: 在 IDE**: 用户 Composer: "T-3 怎么停了?"。Cursor Agent 调 `popola_get_status("T-3")` 看到 `awaiting_input` + schema,通过 MCP `elicitation/create` (form mode + enum schema[^mcp-elicit]) 弹给用户 UI。用户选 `postgres`,Cursor Agent 把答案传 `popola_supply_feedback("T-3", "postgres")`。
5. **用户场景 B: 在终端**: 用户跑 `popola attach T-3` 看到 prompt:`Which DB? [postgres/sqlite]`,直接键入 `postgres`,CLI 把答案打到同一个 daemon。
6. daemon 写 `human.responded` 事件,把 `T-3.state` 置回 `in_progress`,把答案通过 stdin / unix-socket / signal-file 传给子进程。
7. T-3 子进程读取后继续。

**为何这样设计**:
- MCP elicitation 严格要求"server-initiated 必须 in-flight client request 关联"[^mcp-elicit]——所以 daemon **不能**主动 push 给 IDE,必须等 IDE Agent 调 status 时把 pending input 一并返回,这是个完美的 fit。
- 场景 B 的 CLI 路径保证即使 IDE 不可用,用户仍能反馈。
- Form mode 用 enum 而非自由文本,绕开 Argo 的字符串输入 UI bug[^argo-suspend-string-bug]。

### 场景 3: 用户关机过夜,Agent 继续

**前提**: 用户晚上 23:00 提交了一个 8 任务的 full-pipeline,关笔记本去睡觉。

**步骤**:

1. 用户 `popola submit overnight-plan.yaml`(直接走 CLI,绕开 IDE)。
2. CLI 进程把命令通过 unix socket `/tmp/popolad.sock` 发给 daemon。
3. daemon 派发任务到 8 个子 Agent CLI 进程。**所有进程必须**:
   - 用 `setsid` / `nohup` 脱离用户终端
   - 把 stdout/stderr 重定向到 `~/.popola/logs/T-N.jsonl`
   - 进程被 `popolad` 通过 process group 管理,daemon 自身用 systemd-user unit 或 launchd plist 保活
4. 用户合上 MacBook(macOS 不会主动 kill 后台 Python 进程,只是 sleep CPU)。
5. **若有任何任务进入 `awaiting_input`**: daemon 把它标记为 paused,不阻塞 DAG 中其他可独立推进的分支(参考 Argo DAG 的 suspend 节点不影响 sibling[^argo-suspend])。
6. **若有任务出错**: daemon 按 retry policy 重试 N 次,最终失败则置为 `failed`,但**不**杀整个 plan(类比 Airflow 的 `trigger_rule="all_done"`)。
7. **早上 8:00** 用户开机,Cursor Agent 自动看到 daemon 的 plan_id(从 ~/.popola/recent.json 读),汇报:`8 个任务: 6 完成, 1 failed, 1 awaiting_input`。
8. 用户处理 awaiting_input 那一个,failed 的让 Loom 重派或自己修。

**关键设计点**:
- daemon 必须能被 OS 重启服务管理(systemd-user / launchd)——崩溃即重启,event_log 还原状态。
- 桌面通知不能依赖 IDE 在线;改用 OS 原生通知 + 早上 IDE 重开后的"补一次"机制。
- 事件日志要带时间戳,方便用户睡醒一眼看到"昨晚 03:42 卡在 T-5"。

### 场景 4: 实时附着某个 Agent 的日志

**前提**: 场景 3 的 T-7 卡了很久,用户想看实时日志找原因。

**步骤**:

1. **CLI 路径**: `popola attach T-7 --follow`。CLI 通过 unix socket 订阅 daemon 的 NDJSON event stream(类似 `tmux attach` 加入既有 session[^tmux-architecture])。daemon 推送从该 task 的 `event_log` 末尾起的所有事件,加 `tail -f` 增量。
2. **IDE 路径**: 用户 Composer: "把 T-7 的最近 50 行日志贴出来"。Cursor Agent 调 `popola_tail_log("T-7", since_seq=null, lines=50)`,返回结构化数组。如需流式,Cursor Agent 可循环调,或者(高级)走 MCP Streamable HTTP 的 SSE 通道[^mcp-streamhttp]。
3. **多 client 同时 attach**: daemon 像 tmux server 一样,把同一份 event stream broadcast 给所有 attach 的 client[^tmux-architecture]。引用计数,任何 client detach 不影响别人。
4. 用户在 CLI 看到 T-7 卡在 `git push`,推断是 ssh-agent 没解锁,手动 `ssh-add ~/.ssh/id_ed25519`,T-7 自动恢复。

**关键设计点**:
- attach 不能改变 task 行为(只读),除非走 `popola_supply_feedback`。
- detach 不能让 task 关掉(参考 mosh 反例:mosh 不支持多 client attach[^mosh-reattach])。
- Stream 用 NDJSON(每行一个事件)便于 client 增量解析,比 JSON-array 强得多[^ndjson-streaming]。

### 场景 5: 在运行中的图里注入新子任务

**前提**: 场景 1 进行到 `T-2` (design),用户突然意识到忘了让设计阶段也产出 ADR(Architecture Decision Record)。

**步骤**:

1. 用户 Composer: "在 T-2 设计完成之后再加一个 T-2.1: 写 ADR"。
2. Cursor Agent 调 `popola_inject_subtask({parent: "T-2", position: "after", task_def: {...}})`。
3. daemon 验证:
   - parent 存在且 state ∈ {in_progress, completed, pending}
   - DAG 加边后**仍然无环**(用 Tarjan / Kahn 判)
   - 新任务的 owned_files 与 parallel siblings 无冲突
4. 通过则把新 task 入库,event_log 写 `task.injected`,同时根据当前 T-2 状态:
   - 若 T-2 已完成: 立即调度 T-2.1
   - 若 T-2 未完成: 入 wait queue,等 T-2 done event
5. 同步给所有 attach client: `dag.updated` 事件。
6. **若新任务会破坏 DAG 形状**: 拒绝,返回 `{error: "would create cycle: T-2.1 → T-3 → T-2.1"}`。

**关键设计点**:
- DAG 修改必须事务化,两个并发 inject 不能同时通过(参考 Temporal signal handler 的串行化语义[^temporal-signal])。
- 注入新任务比"修改运行中任务的指令"安全得多——前者只是加节点,后者破坏可重放性。
- LangGraph 的 state-editing pattern[^langgraph-interrupt] 给了思路:让人类暂停 → 编辑 state → resume,这与"注入子任务"在数据模型层是同一件事。
- 类似 Argo Intermediate Parameters[^argo-intermediate]——动态加 parameters 给后续节点,我们这里是动态加节点本身。

---

## PopolaLoom 推荐交互骨架

### Phase 1 形态: **Hybrid (Skill 入口 + Local MCP server + 独立 popolad daemon)**

**理由(三句话总结)**:
1. **Skill 提供发现性**: 用户/IDE Agent 第一次看到 SKILL.md 就知道这里有 PopolaLoom 可用,免去 README 的折磨[^claude-skills][^cursor-skills]。
2. **MCP 提供结构化能力**: 工具签名清晰,elicitation 标准化 HITL,能借力所有 MCP 兼容 IDE(Cursor、Claude Code、Cline、Continue、VS Code Insiders 等)[^mcp-elicit][^mcp-tool-annot]。
3. **独立 daemon 提供持久化**: 不被 IDE 生命周期绑架,可崩溃重启,真相在 event_log,同 tmux 与 Temporal 的成熟模式[^tmux-architecture][^temporal-very-long]。

### Phase 2 演进路径

| 阶段 | 添加 | 不变 |
|---|---|---|
| 1.0 | Skill + stdio MCP + popolad daemon + CLI | event_log NDJSON, SQLite checkpoint |
| 1.5 | 桌面通知集成 (notify-send / osascript) | 同上 |
| 2.0 | 嵌入式 Web UI (DAG 可视化, 借鉴 Argo[^argo-ui] / Temporal[^temporal-ui]) | popolad 后端不动 |
| 2.5 | Streamable HTTP MCP transport[^mcp-streamhttp], 远程 popolad 可选 | 协议契约不变 |
| 3.0 | Slack/Lark Bot 适配器, 订阅 event_log[^slack-blockkit] | event_log NDJSON 格式不变 |
| 3.5 | Cursor Cloud Agent 集成: 把 cloud agent 也作为 PopolaLoom 可派发的 worker[^cursor-sdk][^cursor-cloud] | DAG 原语不变 |

### 必须实现的 7 个核心交互动词

| 动词 | 调用者 | 接口 | 语义 | 类比 |
|---|---|---|---|---|
| **submit** | Human/IDE | `popola_submit_plan(plan)` | 提交一个 DAG plan,返回 plan_id | Temporal `start_workflow`[^temporal-very-long] |
| **list** | Human/IDE | `popola_list_tasks(filter?)` | 列出所有/过滤后的 plan 与 task | Argo `list` |
| **status** | Human/IDE | `popola_get_status(id)` | 取单个 plan/task 的当前状态(含 pending interrupt) | Temporal `query` |
| **attach** | Human/IDE | `popola_attach(id)` → stream | 订阅 event stream,实时跟随日志 | tmux `attach`[^tmux-architecture] |
| **feedback** | Human/IDE | `popola_supply_feedback(task_id, value)` | 把人类决策回写给在 await 的 task | Temporal `signal`[^temporal-signal] / LangGraph `Command(resume=...)`[^langgraph-interrupt] |
| **cancel** | Human/IDE | `popola_cancel(id, reason)` | 取消一个 task 或整个 plan | 通用 |
| **inject** | Human/IDE | `popola_inject_subtask(parent, def)` | 在运行中 DAG 注入子任务 | LangGraph state edit[^langgraph-interrupt] |

> 可选 v1.1+: `pause`(临时暂停某个分支)、`resume`(从 pause 状态恢复)、`fork`(从某个 task 节点克隆出新 plan)。

### 推荐的事件流格式

**采用 NDJSON,信封套用 CloudEvents 最小子集**[^ndjson-streaming][^cloudevents-spec]:

```jsonl
{"specversion":"1.0","id":"evt-01HJ...","source":"popola/T-1","type":"task.created","time":"2026-05-03T02:00:00Z","datacontenttype":"application/json","data":{"task_id":"T-1","agent":"claude-code","prompt_preview":"Survey..."}}
{"specversion":"1.0","id":"evt-02HJ...","source":"popola/T-1","type":"task.dispatched","time":"2026-05-03T02:00:03Z","data":{"task_id":"T-1","pid":12345,"cwd":"/home/.../task_T1"}}
{"specversion":"1.0","id":"evt-03HJ...","source":"popola/T-1","type":"task.tool_call","time":"2026-05-03T02:00:42Z","data":{"task_id":"T-1","tool":"WebSearch","args":{"q":"..."}}}
{"specversion":"1.0","id":"evt-04HJ...","source":"popola/T-1","type":"task.elicited","time":"2026-05-03T02:30:00Z","data":{"task_id":"T-1","schema":{...},"message":"Which DB?"}}
{"specversion":"1.0","id":"evt-05HJ...","source":"popola/T-1","type":"human.responded","time":"2026-05-03T08:00:00Z","data":{"task_id":"T-1","value":"postgres","by":"user@host"}}
{"specversion":"1.0","id":"evt-06HJ...","source":"popola/T-1","type":"task.completed","time":"2026-05-03T08:15:00Z","data":{"task_id":"T-1","exit_code":0,"artifacts":["..."]}}
```

**为什么是这个组合**:
- NDJSON: 每行独立可解析、append-only、tail-able、易 grep[^ndjson-streaming]
- CloudEvents 信封: 跨平台互通 + 标准元数据(`id` 全局唯一支持去重重放;`type` 区分事件类型;`source` 可追溯[^cloudevents-spec])
- 事件类型空间: `task.{created,dispatched,heartbeat,tool_call,output,elicited,completed,failed,canceled,injected}` + `human.{responded,canceled,injected}` + `plan.{created,completed,paused}` + `dag.{updated}`
- 借鉴现成的 ADR[^ndjson-adr],事件类型采用 discriminated union,client 用 `type` 字段做 switch

### 推荐的人类反馈通道(优先级降序)

1. **MCP elicitation form mode (enum/boolean 优先)**[^mcp-elicit]——IDE 内最自然
2. **CLI prompt** (`popola attach <id>` 进入交互式)——终端用户
3. **OS desktop notification + click-through**(`notify-send` Linux / `osascript display notification` macOS)——离开 IDE 时
4. **(Phase 2) Web UI form**——长任务复杂表单
5. **(Phase 3) Slack/Lark Block Kit interactive**[^slack-blockkit]——团队场景

### 必须避免的 5 个失败模式

| 失败模式 | 真实案例 | PopolaLoom 防御 |
|---|---|---|
| **Server-initiated push 跨进程不可靠** | MCP 强制要求 server-to-client 请求关联到 in-flight client request[^mcp-elicit] | 主拉模型: pending interrupts 在 `popola_get_status` 时一起返回,不依赖 push |
| **CLI session 绑死 cwd** | Codex `--resume` 不还原原 `--cd`[^codex-cd-bug] | plan_id 与 cwd 解耦,daemon 记录每个 task 的 launch dir |
| **多 client attach 不支持** | mosh 反例[^mosh-reattach] | tmux 风格 server-client 架构,引用计数 |
| **Suspend 后 string 输入 UI 不显示** | Argo Workflows v3.x bug[^argo-suspend-string-bug] | Phase 1 优先 enum,字符串输入用 form schema 预定义 minLength/pattern |
| **进程被 IDE 关闭杀掉** | 任何在 IDE 内"shell"启动的 daemon 都会被杀 | 用 `setsid`/`nohup` 脱离 IDE process group;daemon 主体走 systemd-user 或 launchd |

### 可选: 借鉴 DevolaFlow 现有模式

DevolaFlow 已经在 SKILL.md 里定义了 4 层升级链(Task → Wave → Stage → Project → Human)与 4 档 escalation severity (`AUTO_RECOVER` / `PAUSE` / `HUMAN_INTERVENE` / `FULL_ROLLBACK`)[^devola-skill]。PopolaLoom 应直接复用这套语义:`HUMAN_INTERVENE` ↔ `task.elicited`,`PAUSE` ↔ `task.paused`,`FULL_ROLLBACK` ↔ `plan.aborted`。这样 PopolaLoom 与 DevolaFlow 是兼容的(DevolaFlow 跑单 agent 的 4 层,PopolaLoom 跑跨 agent 的多 plan)。

---

## 名词表 / 缩略语

| 缩写/名词 | 含义 |
|---|---|
| MCP | Model Context Protocol—— Anthropic 主导,IDE/Agent 与 tools/resources 的通用协议[^mcp-streamhttp] |
| stdio transport | MCP 的本地子进程传输,JSON-RPC over stdin/stdout[^mcp-stdio] |
| Streamable HTTP | MCP 的现代 HTTP 传输,POST + SSE,支持 session resumability[^mcp-streamhttp] |
| Elicitation | MCP server 在 tool call 处理过程中向 client 请求 user input(form/url 模式)[^mcp-elicit] |
| Sampling | MCP server 反向请求 client 跑一次 LLM completion[^mcp-sampling] |
| Tool Annotation | MCP 工具上的 hint 字段(readOnlyHint/destructiveHint/idempotentHint/openWorldHint)[^mcp-tool-annot] |
| Skill | Claude Code 与 Cursor 共享的指令包格式,YAML frontmatter + markdown[^claude-skills][^cursor-skills] |
| Progressive Disclosure | Skill 的三层加载策略: metadata 常驻 / instructions 触发 / files 按需[^claude-skills] |
| Cursor Background Agent | Cursor 本地后台进程,与 IDE 解耦但仍在用户机[^cursor-bg] |
| Cursor Cloud Agent | Cursor 云端运行的 agent,`bc-` 前缀,可在 Web/Slack 看[^cursor-cloud][^cursor-sdk] |
| Temporal Signal/Query | Temporal 的工作流交互原语;signal 异步可改状态,query 同步只读[^temporal-signal][^temporal-very-long] |
| LangGraph interrupt() | 在节点中暂停图执行,等 `Command(resume=...)`[^langgraph-interrupt] |
| Checkpointer | LangGraph/Temporal 的状态持久化层 |
| Argo Suspend | Argo Workflows 的暂停模板,支持 Intermediate Parameters[^argo-suspend][^argo-intermediate] |
| Sensor | Airflow 用于等外部条件的 task 类型(poke/reschedule/deferrable)[^airflow-sensor][^airflow-deferrable] |
| Deferrable Operator | Airflow 异步事件驱动 operator,释放 worker[^airflow-deferrable] |
| UserProxyAgent | AutoGen 中代表用户的 agent,有 NEVER/TERMINATE/ALWAYS 三档输入模式[^autogen-userproxy] |
| pause_flow_run | Prefect 的暂停函数,UI 自动生成 Pydantic 表单[^prefect-interactive] |
| Block Kit | Slack 的交互组件,支持按钮/选择/对话框[^slack-blockkit] |
| Agent Activities | Linear 的 frozen-in-time 评论机制,优于普通 comment 用于 agent 对话[^linear-agents] |
| NDJSON | Newline-Delimited JSON,每行一个 JSON 对象的事件流格式[^ndjson-streaming] |
| CloudEvents | CNCF 的事件信封规范(id/source/specversion/type)[^cloudevents-spec] |
| WorkDoneProgress | LSP/VS Code 的进度报告协议[^vscode-workdone][^vscode-toolprogress] |
| HITL | Human-In-The-Loop |
| DAG | Directed Acyclic Graph,无环有向图;PopolaLoom 的 plan 即一个 DAG |
| popolad | 本文给 PopolaLoom 后台 daemon 起的名字(loom + d) |
| popola-mcp | 本文给 MCP server 前端起的名字 |
| popola | 本文给 CLI 起的名字 |

---

## 五句话执行摘要

1. **Phase 1 推荐形态: Hybrid Skill + Local MCP Server + 独立 `popolad` daemon**——Skill 提供"宿主 IDE Agent 一眼看到"的发现性,MCP 提供结构化的 7 个核心交互动词与标准化 elicitation,独立 daemon 解耦 IDE 生命周期(综合得分 33/35,优于纯 Skill 的 18/35 与纯 MCP 的 28/35)。
2. **HITL 推荐主原语: MCP Form-mode Elicitation(enum 优先)+ Temporal-style 持久化 signal**——前者解决"IDE 内同步问"的 90% 场景,后者解决"用户合上电脑过夜"场景;两者互补,前者 fail-fast,后者 fail-safe。
3. **Attach/Resume 推荐主原语: tmux 风格 server-client 架构 + Event-History-as-Truth**——`popolad` 像 tmux server 常驻,任意 client(IDE Skill / CLI / Web UI)随时 attach 到 `~/.popola/event_log/<plan>.jsonl` 的尾部;reference count 管理 detach,真相永远在 event log,daemon 自身可崩溃重启。
4. **事件流推荐格式: NDJSON 每行套 CloudEvents 1.0 最小信封 (id/source/specversion/type/time/data)**——append-only,易 tail/grep/replay,跨平台互通,事件类型用 discriminated union (`task.*` / `human.*` / `plan.*` / `dag.*`)。
5. **最大单点交互风险: MCP server-to-client 请求必须关联到 in-flight client request 这一硬约束**——意味着 PopolaLoom **不能**像 Slack bot 那样在用户合上 IDE 三小时后通过现有 MCP 连接主动弹问题;必须设计成"主拉 + OS 桌面通知兜底"的双通道,任何依赖纯 push 的设计都会在生产里悄悄死掉,需要在架构 day-1 就避免。

---

## 引用脚注

[^mcp-elicit]: Model Context Protocol —— Elicitation 规范(form/url 模式;server-to-client 请求必须关联到 originating client request)。<https://modelcontextprotocol.io/docs/concepts/elicitation> 与 <https://mcp.mintlify.app/specification/draft/client/elicitation>
[^mcp-streamhttp]: MCP TypeScript SDK —— Server Guide,Streamable HTTP transport(session resumability、SSE、双模式);Migrating from stdio to Streamable HTTP。<https://ts.sdk.modelcontextprotocol.io/documents/server.html> 与 <https://chatforest.com/guides/mcp-server-migration-stdio-to-http/>
[^mcp-stdio]: MCP-Go —— STDIO Transport(本地子进程,JSON-RPC over stdin/stdout)。<https://mcp-go.dev/transports/stdio/>
[^mcp-tool-annot]: Model Context Protocol Blog —— Tool Annotations as Risk Vocabulary (2026-03-16);MCPBlog.dev —— MCP Tool Annotations Explained (2026-03-13);PR #489 (closed Sep 2025) 提议 streamingHint/statelessHint/asyncHint 但未合并。<https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/> 与 <https://mcpblog.dev/blog/2026-03-13-mcp-tool-annotations> 与 <https://github.com/modelcontextprotocol/modelcontextprotocol/pull/489>
[^mcp-sampling]: MCPBlog.dev —— MCP Sampling: server-initiated LLM calls(2026-04-07,含 Unit42 安全分析);MCP-Go Sampling 文档。<https://mcpblog.dev/blog/2026-04-07-mcp-sampling-attack-vector> 与 <https://mcp-go.dev/servers/advanced-sampling>
[^claude-skills]: Anthropic Engineering —— Equipping agents for the real world with Agent Skills(三层 progressive disclosure);Claude API Docs —— Agent Skills Overview;Ry Walker 评论(2026-04)。<https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills> 与 <https://docs.anthropic.com/en/docs/agents-and-tools/agent-skills/overview> 与 <http://rywalker.com/research/anthropic-skills>
[^cursor-skills]: Cursor Docs —— Agent Skills(SKILL.md 格式,触发方式: 自动/`/skill-name`/`@skill-name`,`disable-model-invocation` 选项)。<https://cursor.sh/docs/skills> 与 <https://cursor.com/help/customization/skills>
[^cursor-cli-resume]: Cursor Docs —— Using Agent in CLI(`--resume [chatId]`、`--continue`、`agent ls`、`agent resume`)。<https://cursor.com/docs/cli/using> 与 <https://cursor.com/docs/cli/reference/parameters>
[^cursor-bg]: LLMversus —— Cursor Background Agents 2026 解析(Composer Cmd+I,toggle Background;move-to-background)。<https://llmversus.com/coding-tools/cursor/background-agents>
[^cursor-cloud]: Cursor Docs —— Cloud Agents(隔离 VM、Web/Slack/Linear/API 多通道访问、自动调度)。<https://cursor.com/docs/background-agent> 与 <https://cursor.com/help/ai-features/cloud-agents>
[^cursor-sdk]: Cursor SDK Skill —— `Agent.create/prompt/resume`、cloud `bc-` 前缀 ID 与 run ID 区分、MCP server 配置不跨 resume 持久化。`/root/.cursor/skills-cursor/cursor-sdk/SKILL.md`
[^claude-resume]: Claude Code Docs —— CLI reference(`--resume <session-id>`);Claude Code Guides 教程(sessions 存于 `.claude/sessions/`)。<https://code.claude.com/docs/en/cli-reference> 与 <https://claudecodeguides.com/claude-code-resume-flag-how-to-use-it/>
[^claude-cross-client]: Anthropic Claude Code Issue #44063 —— Resume Any Claude Session in the CLI(2026-04 提议跨 client server-side session,尚未实现)。<https://github.com/anthropics/claude-code/issues/44063>
[^claude-attach]: Anthropic Claude Code Issue #40310 —— Allow terminal attachment to phone-spawned Remote Control sessions(open RFC)。<https://github.com/anthropics/claude-code/issues/40310>
[^codex-noninteractive]: OpenAI Codex Docs —— Non-interactive mode(`codex exec`,stdout-only 终态、stderr 进度)。<https://developers.openai.com/codex/noninteractive/>
[^codex-resume]: OpenAI Codex PR #4374 —— Let `codex exec` resume runs by session UUID(`--session <UUID>` / `--resume-rollout <path>`)。<https://github.com/openai/codex/pull/4374>
[^codex-bg]: OpenAI Codex Issue #3968 —— Background Terminal Sessions(open feature, 2026-03 仍未实现)。<https://github.com/openai/codex/issues/3968>
[^codex-cd-bug]: OpenAI Codex Issue #4703 —— resume 不还原 `--cd`。<https://github.com/openai/codex/issues/4703>
[^temporal-very-long]: Temporal Blog —— Managing very long-running Workflows with Temporal(history-as-truth、worker 无状态);Temporal Web UI 文档。<https://temporal.io/blog/very-long-running-workflows> 与 <https://docs.temporal.io/web-ui>
[^temporal-signal]: James Carr —— Temporal Patterns: Process Manager with Signals(Signal 异步 + history,Query 同步只读)。<https://james-carr.org/posts/2026-02-03-temporal-process-manager/>
[^temporal-hitl]: Learn Temporal —— Building Long-Running MCP Tools with Human-in-the-Loop;Adding Durable HITL to Research Application。<https://learn.temporal.io/tutorials/ai/building-mcp-tools-with-temporal/adding-hitl-to-mcp-tools/> 与 <http://learn.temporal.io/tutorials/ai/building-durable-ai-applications/human-in-the-loop/>
[^temporal-ui]: Temporal UI PR #1658(Timeline 改进)、#3160(event history legend)、#3077(event type colors)、#2589(full-height timeline)。<https://github.com/temporalio/ui/pull/1658> 等
[^argo-suspend]: Argo Workflows Docs —— Suspending(suspend 模板、`argo resume`、超时自动恢复)。<https://argo-workflows.readthedocs.io/en/stable/walk-through/suspending/>
[^argo-intermediate]: Argo Workflows Docs —— Intermediate Parameters(v3.4+, suspend 节点 UI 接受 enum/dropdown 输入)。<https://argo-workflows.readthedocs.io/en/stable/intermediate-inputs/>
[^argo-suspend-string-bug]: Argo Workflows Issue #13256 —— UI: prompt for string input during suspend(open bug, 字符串输入 UI 未实现)。<https://github.com/argoproj/argo-workflows/issues/13256>
[^argo-ui]: Argo Workflows Docs —— New Features(DAG 视图、live logs、retry single node、debug pause v3.3+)。<https://argo-workflows.readthedocs.io/en/stable/new-features/> 与 <https://argo-workflows.readthedocs.io/en/latest/debug-pause/>
[^airflow-sensor]: Airflow Docs —— `airflow.sensors.base`(poke vs reschedule mode)。<https://airflow.apache.org/docs/apache-airflow/2.8.1/_api/airflow/sensors/base/index.html>
[^airflow-deferrable]: Airflow Docs —— Deferrable Operators & Triggers(triggerer 异步 polling,释放 worker slot)。<https://airflow.apache.org/docs/apache-airflow/2.8.1/authoring-and-scheduling/deferring.html>
[^langgraph-interrupt]: LangChain Blog —— Making it easier to build human-in-the-loop agents with interrupt;BSWEN 教程(2026-04-16)。<https://blog.langchain.dev/making-it-easier-to-build-human-in-the-loop-agents-with-interrupt> 与 <https://docs.bswen.com/blog/2026-04-16-langgraph-human-in-the-loop>
[^autogen-userproxy]: Microsoft AutoGen 0.2 文档 —— Allowing Human Feedback in Agents(NEVER/TERMINATE/ALWAYS 三档);0.4.1 HITL 教程(team 内 UserProxyAgent 阻塞团队执行)。<https://microsoft.github.io/autogen/0.2/docs/tutorial/human-in-the-loop> 与 <https://microsoft.github.io/autogen/0.4.1/user-guide/agentchat-user-guide/tutorial/human-in-the-loop.html>
[^prefect-interactive]: Prefect Docs —— How to write interactive workflows(`pause_flow_run`、Pydantic `RunInput`、UI 自动生成表单);AI Database Cleanup with Approval。<https://docs.prefect.io/latest/guides/creating-interactive-workflows/> 与 <https://docs-3.prefect.io/v3/examples/ai-database-cleanup-with-approval>
[^slack-blockkit]: openclaw PR #48567 —— `feat(slack): add exec approval buttons via Block Kit`(Allow Once / Allow Always / Deny);Hermes-agent PR #5890(`thread_ts` 上下文保留)。<https://github.com/openclaw/openclaw/pull/48567> 与 <https://github.com/NousResearch/hermes-agent/pull/5890>
[^linear-agents]: Linear Developers —— Getting Started Agents(Agent Session events webhooks);Interaction Best Practices(Agent Activities 优于 mutable comments);linear-agent-bridge 实现示例。<https://linear.app/developers/agents.md> 与 <https://linear.app/developers/agent-best-practices> 与 <https://github.com/tokezooo/linear-agent-bridge>
[^tmux-architecture]: tmux DeepWiki —— Client Lifecycle and Attachment;tmux 源码 `tmux.h` 与 `session.c`(client-server 架构、reference counting、多 client 同时 attach)。<https://deepwiki.com/tmux/tmux/6.5-client-lifecycle-and-attachment> 与 <https://github.com/tmux/tmux/blob/master/session.c>
[^tmux-data]: tmux DeepWiki —— Core Data Structures;Session, Window, and Pane Hierarchy。<https://deepwiki.com/tmux/tmux/2.2-core-data-structures> 与 <https://deepwiki.com/tmux/tmux/2.3-session-window-and-pane-hierarchy>
[^mosh-design]: LWN —— Entering the mosh pit(SSP 状态同步协议、UDP heartbeat、IP 漫游);Wikipedia: Mosh (software);Stack Exchange 问答(为何能跨网络变化保持登录)。<https://lwn.net/Articles/722923/> 与 <https://en.wikipedia.org/wiki/Mosh_(software)>
[^mosh-reattach]: Stack Overflow —— How do I reattach to a detached mosh session?(回答: 不能;mosh 不替代 screen/tmux)。<https://stackoverflow.com/questions/17857733/how-do-i-reattach-to-a-detached-mosh-session>
[^vscode-workdone]: vscode-gcode-extension PR #150 —— `feat(lsp): apply WorkDoneProgress to long-running operations`(orchestrator + producer 两角色模型,100ms 节流)。<https://github.com/QuickBoyz/vscode-gcode-extension/pull/150>
[^vscode-toolprogress]: VS Code PR #246768 —— `chat: allow tools to report progress`(toolProgress proposed API,for MCP servers)。<https://github.com/microsoft/vscode/pull/246768>
[^ndjson-streaming]: NDJSON.com —— JSONL for Data Streaming & Pipelines;JSONL for Logs - Structured Logging。<https://ndjson.com/use-cases/data-streaming/> 与 <https://ndjson.com/use-cases/log-processing/>
[^ndjson-adr]: JoelClaw ADR-0058 —— Streamed NDJSON Protocol for Agent-First CLIs(discriminated-union event types: session_start / text_delta / message / tool_start/end / step / progress / log / result/error)。<https://joelclaw.com/adrs/adr-0058>
[^cloudevents-spec]: CNCF CloudEvents v1.0 spec —— required attributes(id/source/specversion)、optional(subject/datacontenttype/dataschema)、HTTP binding(binary/structured/batched)。<https://github.com/cloudevents/spec/blob/v1.0/spec.md> 与 <https://github.com/cloudevents/spec/blob/v1.0/http-protocol-binding.md>
[^devola-skill]: DevolaFlow `SKILL.md` v10.1.0 —— 4-layer hierarchy (L0-L3)、4 escalation severities (AUTO_RECOVER/PAUSE/HUMAN_INTERVENE/FULL_ROLLBACK)、context isolation。`/root/.claude/skills/devola-flow/SKILL.md`
