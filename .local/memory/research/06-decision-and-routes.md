# PopolaLoom 路线选型判别清单 + 路线方案清单

> 编排: L0 (devola-flow research-only -> design-only)
> 综合源: research/01-05 + research/08 + DevolaFlow SKILL.md v10.1.0
> 输出日期: 2026-05-03 (R4 锁定: 2026-05-03 10:32 UTC+8)
> 状态: 已锁定 R4 + ArkTower 子模块依赖
> 备注: 本文件原 Stage-2 写入时遭遇 mojibake 损坏 (中文字符被 squashed 为 ASCII 问号), 已于 2026-05-03 11:35 由 L0 紧急通过 shell heredoc 重写; 内容相对原 884 行精简为决策记录, 实施细节迁移至 `.local/memory/specs/popolaloom/`。

---

## 0. 一页摘要 (TL;DR)

### 0.1 我们要造的是什么

**PopolaLoom 是一个本机常驻的"织机式" (loom) 元编排器**: 在用户已有的 Cursor / Claude Code / Codex 等 Agent CLI **之上** 再加一层, 负责跨 CLI 派发任务、追踪任务依赖与反馈循环、维持 agent 进程跨终端退出的存活、提供 attach/resume, 并以 Skill + Local MCP + Textual TUI + NiceGUI Web Dashboard 五形态向用户暴露自身。第一里程碑: 在 Cursor Agent 上实现 "PopolaLoom 派发任务给自己研发 + 测试 PopolaLoom" 的自闭环。

### 0.2 已锁定路线

**R4: Standalone TUI + Web Dashboard + popolad daemon** (用户回答 Q2, 2026-05-03)。

借助 ArkTower (Verdict C: 任务池子层) 的 `pip install` 复用, R4 的 popolad infrastructure 工程量缩减约 70%, 从原估 14 天降到 9 天。

### 0.3 4 句主张 (与 DevolaFlow / claude-squad / Cursor Cloud Agents 的位置)

1. **PopolaLoom 是 DevolaFlow 的"楼上一层"(L-1 Conductor)**: DevolaFlow 是单 agent 内的 4 层 Project/Stage/Wave/Task 编排; PopolaLoom 在其之上加一层 Conductor, 负责跨多个 DevolaFlow 实例 (跑在不同 CLI runtime) 的协同 (出处: 01 §3.2)。
2. **PopolaLoom = 上层编排 + ArkTower 任务池子层**: ArkTower 提供 10-state FSM、SQLite 持久化、12 MCP tools、NiceGUI 5-page dashboard、8-dim 自评测; PopolaLoom 自写 5 个差异化模块 - 跨 CLI dispatcher / popolad daemon / attach-resume / Lark+IDE HITL / Textual TUI (出处: 08 §7.2 + §8 Verdict C)。
3. **PopolaLoom 不是又一个 multi-agent 框架**: 遵循 Inngest "harness, not framework" 哲学, 只做 connect / protect / orchestrate, 不替代 Cursor/Claude/Codex 内部的 agent loop (出处: 04 §1.2)。
4. **PopolaLoom 是 Karpathy "agent command center"(2026-03-02 推文, 231 回复) 的开源具象化**, 同时填补"跨 5 厂商 CLI 统一管理面 + 长跑 attach"的现存生态空白 (出处: 04 §四)。

---

## 0.0 用户确认块 (User Confirmation Block)

> **锁定日期**: 2026-05-03
> **决策出处**: `.local/tasks/init_popola_loom.md:11-21` (用户回答原文) + `08-arktower-deep-dive.md` §8 (Verdict C)

### 9 题确认答案表

| Q# | 题目 | 用户回答 | 触发的设计变更 | 出处 |
|---|---|---|---|---|
| **Q1** | ArcTower 来源澄清 | `https://github.com/YoRHa-Agents/ArkTower` (拼写为 Ar**K**Tower, 与 DevolaFlow 同 GitHub org) | Stage-1 已分析: Verdict C - ArkTower 是 PopolaLoom 任务池子层依赖; PopolaLoom `pip install -e arktower` 复用 (a) Task/TaskEvent/Dependency models、(b) 10-state FSM、(c) EventBus、(d) SQLite repo、(e) FastAPI REST+WS、(f) MCP 12 tools、(g) NiceGUI 5-page dashboard、(h) 8-dim 自评测框架。PopolaLoom 自写 5 个差异化模块: 跨 CLI dispatcher / popolad daemon / attach-resume / Lark+IDE HITL / Textual TUI | 08 §8.1 Verdict C, 08 §7.2 复用清单 |
| **Q2** | 路线选型 | **R4** (Standalone TUI + Web Dashboard + popolad daemon) | 推荐路线从原综合 R3 升级为 R4; §3 路线表中 R3 标记为 Phase 0 fallback, R4 标记为 Final Selected; 评分卡 R4 行追加 "Final Selected"; 原 7-day MVP 扩展为 9-day 实施 (因 Q1 ArkTower 复用反而比原 R3 plan 缩短了核心工程量) | tasks:13; 08 §8.2 |
| **Q3** | 实施技术栈 | **Python** (与 DevolaFlow 同源) | D11 维度锁定 Python; FastMCP / LangGraph / SqliteSaver / Pydantic v2 全部确认主栈; TypeScript / Rust / Go 选项作废 | tasks:14 |
| **Q4** | Phase 1 CLI 子集 | **Cursor + Claude + Codex** (暂缓 Kimi + Copilot) | D8 多 CLI 抽象层 Phase 1 实现 3 个 adapter (cursor / claude / codex), Kimi/Copilot 推迟到 Phase 2; Phase 1 跨 CLI handoff 测试场景从 "Cursor -> Claude" 升级为 "Cursor planner -> Claude implementer -> Codex tester" 三跳; Codex 走 app-server WS 专路径 (02 §"OpenAI Codex") | tasks:15 |
| **Q5** | 图引擎 | **允许使用 LangGraph** | D4 任务图模型锁定 "DAG + SCC subgraph (LangGraph StateGraph 风格)"; D5 持久化锁定 LangGraph SqliteSaver 主路径; dev/test/verifier subgraph 编译模板纳入 Day-3 交付物 | tasks:16; ADR-0002 |
| **Q6** | HITL 通道 | **Lark 为主 + IDE 通知** | D7 HITL 机制锁定 "Lark 为主 + IDE 桌面通知 + signal 持久化" 三通道 (替换原推荐的 "MCP elicitation + signal + OS notify"); Lark HITL bridge 复用本仓库 lark-cli skill 体系 (lark-im / lark-task / lark-doc) 而非自写 Lark SDK (待 Q-NEW-4 最终确认) | tasks:17 |
| **Q7** | popolad 启动权限 | **同时支持, 默认 `systemd-run --user --scope`** | D3 进程稳定性锁定 "默认 systemd-run --user --scope + tmux 备选" 双模式; popolad config 自动选 (Linux user -> systemd-run / 容器 macOS -> tmux); 最灵活方案, 实现成本中等 | tasks:18 |
| **Q8** | 自演化阶段允许自动 merge | **允许自动 merge** (符合 Protected Branch 规则) | D9 自我演化由 "DevolaFlow self-update + 人工 review" 升级为 "DevolaFlow self-update + 自动 merge PR"; 新增 §5 "自动 merge 边界条件": 5 条 AND 门方可 auto-merge; 违反 Protected Branch 规则的 main/master 直推被强制阻断 | tasks:19 |
| **Q9** | Cloud Agent 可选支持 | **可选支持, 默认本机** | Phase 1 默认本机 (popolad daemon); Phase 2 增量加 cloud adapter; 新生 Q-NEW-5: Phase 1 是否预留 cloud adapter 接口位 (推荐预留) | tasks:20 |

### 为什么从 R3 升级到 R4

用户在 Q2 选 R4 而非 §0 早先推荐的 R3, 核心原因有三:

(1) **R4 控制面上限更高**。R4 在 R3 (Hybrid Skill+MCP+popolad) 基础上叠加独立运行的 Textual TUI + NiceGUI Web Dashboard 两个监控前台, 使 PopolaLoom 一上来就具备完整的"agent command center"形态 (Karpathy 2026-03-02 推文意象), 而 R3 只能依赖 IDE-side 的 Skill 入口, 用户合上 IDE 即失去观测面。

(2) **ArkTower (Q1) 把 R4 的高投入主体折掉了 ~70%**。Stage-1 ArkTower 深度分析 (08 §8.2) 确认其已实现"任务池 + 10-state FSM + SQLite 持久化 + MCP server (12 tools) + NiceGUI 5-page dashboard + 8-dim 自评测", 这正是 R4 评分卡里"Web Dashboard + popolad infra"差异化得分的主体。直接 `pip install -e arktower` 后, R4 的 Web Dashboard 几乎免费 (复用 ArkTower NiceGUI 5 页), Textual TUI 只需对接相同 ArkTower API 即可。

(3) **R4 vs R3 时间差从 +2-3 天压到 +1 天**。综合 ArkTower 复用, R4 9-day plan 比 R3 7-day plan 仅多 2 天 (主要在 Day 6 Textual TUI 自写), 换来一个 Phase 1 即可独立运行 (不依赖 IDE 在线) 的控制平面, 以及 Phase 2 直接对接云 agent 的预留位 - 性价比显著。

---

## 1. 项目定义与边界

### 1.1 是 / 不是

| 是 | 不是 |
|---|---|
| 跨 3 个本机 Agent CLI 的元编排器 (Phase 1: Cursor/Claude/Codex) | 单 agent 内部的 prompt engineering / tool calling 框架 (DevolaFlow 范畴) |
| 本机常驻 daemon (popolad) + Skill + MCP + Textual TUI + NiceGUI Web 五形态 | 云端 SaaS / 自托管多机集群 (Phase 3 才考虑) |
| 派发器 / 状态总线 / 反馈通道 / 持久化层 / 控制平面 | 实际写代码的执行体 (写代码的是被派发的 CLI) |
| 任务依赖图 (DAG + dev↔test cycle 的 SCC subgraph) | 通用 workflow engine (Airflow/Temporal 那种规模) |
| 自我编排能力 (self-bootstrap on Cursor Agent) + 自动 merge PR | 自训练 / 自调参 LLM (那是模型层的事) |
| Survives-terminal-exit + attach/resume | mosh 风格的网络层会话漂移 |

### 1.2 与同类工具的位置

```
+------------------------------------------------------------+
| Human Developer                                            |
| +-------------+ +-------------+ +------------------+       |
| | IDE Agent   | | Terminal    | | Lark / Browser   |       |
| | (Cursor /   | | popola CLI  | | Web Dashboard    |       |
| |  Claude)    | | (TUI)       | | (NiceGUI 5-page) |       |
| +------+------+ +------+------+ +--------+---------+       |
+--------|---------------|------------------|----------------+
         | MCP / Skill   | unix socket      | HTTP / WS
+--------|---------------|------------------|----------------+
| PopolaLoom (本仓库)                                        |
|  +-----+-------+ +------+----------+ +-----+--------+      |
|  |popolaloom-  | | popolaloom-mcp  | |popolaloom-   |      |
|  |  skill      | | (7 verbs)       | |  tui / web   |      |
|  +-----+-------+ +-----+-----------+ +-----+--------+      |
|        +------+--------+--------------------+              |
|               v                                            |
|  +------------------------------------------------------+  |
|  | popolad daemon (LangGraph StateGraph + SqliteSaver   |  |
|  |   + NDJSON CloudEvents + 3-CLI dispatchers)          |  |
|  +------+-----------------------------------------+-----+  |
+---------|-----------------------------------------|--------+
          | Python API                              | subprocess
+---------v-------------+              +------------v------------+
| ArkTower (依赖)       |              | Agent CLIs (本机)       |
| - Task/Event models   |              | - cursor-agent          |
| - 10-state FSM        |              | - claude (Claude Code)  |
| - SQLite repo         |              | - codex (app-server WS) |
| - 12 MCP tools        |              | (Phase 1 = 3 CLI)       |
| - NiceGUI dashboard   |              |                         |
| - 8-dim self-eval     |              |                         |
+-----------------------+              +-------------------------+
```

详细模块清单见 `specs/popolaloom/spec.md` §3.2; 数据流见 §3.3 / §3.4。

---

## 2. 维度选择简表 (12 维)

每行的"出处"指向用户答案或上游 dossier 的具体节; 详细推理在 `spec.md` 与 ADRs。

| # | 维度 | 候选 | 用户最终选择 | 出处 |
|---|---|---|---|---|
| **D1** | 表现形态 (给人) | Skill / MCP / Hybrid / TUI / Web / IDE 插件 | **Hybrid 五形态: Skill + MCP + popolad + Textual TUI + NiceGUI Web** | tasks:13 (R4) + spec §3.2 |
| **D2** | 派发协议 (给 Agent) | 子进程派生 / MCP / RPC / 文件队列 / WebSocket | **子进程派生 + ArkTower MCP 12 tools 复用 + Codex app-server WS 专路径** | tasks:15 + 08 §3.4 + 02 §"OpenAI Codex" |
| **D3** | 进程稳定性 | foreground / nohup / tmux / systemd-run / supervisor | **systemd-run --user --scope 默认 + tmux 备选 (双模式自动选)** | tasks:18 |
| **D4** | 任务图模型 | DAG-only / FSM / DAG+SCC subgraph / 事件总线 / Petri net | **DAG + SCC subgraph (LangGraph StateGraph 风格)** | tasks:16 + ADR-0002 |
| **D5** | 持久化 | event-sourcing / SqliteSaver / NDJSON / DB 双写 | **LangGraph SqliteSaver 主路径 + NDJSON CloudEvents 1.0 旁路 (双轨)** | ADR-0002 §2.3 + spec §3.5 |
| **D6** | 循环表达 | 固定 N 轮 / Gen-Verifier / 条件边 / 自反向边 | **Gen-Verifier (装在 LangGraph subgraph 内, SCC 内禁外漏)** | 03 §"Dev↔Test 闭环" + spec §3.4 |
| **D7** | HITL 机制 | 同步问 / 异步暂停 / signal 持久化 / 通知拉 | **Lark 为主 + IDE 桌面通知 + signal 持久化 (三通道)** | tasks:17 + 05 §"HITL 推荐" |
| **D8** | 多 CLI 抽象 | 自写 adapter / ACP / Codex app-server / 仅子进程 | **自写 adapter 三个 (Cursor/Claude/Codex) + ArkTower MCP 任务池 API** | tasks:15 + 02 §"派发抽象建议" |
| **D9** | 自演化 | 不演化 / 单元测试 / DevolaFlow self-update / 自评 | **DevolaFlow self-update + 自动 merge PR (5 条 AND 门见 §5)** | tasks:19 + spec §7 |
| **D10** | 任务原语 | 自创 / DevolaFlow 14 / DevolaFlow + Conductor 顶层 | **DevolaFlow 14 全部继承 + 7 个新 Conductor 原语 (dispatch/attach/relay/supervise/federate/handoff/probe)** | spec §4 |
| **D11** | 实施栈 | Python / TypeScript / Rust / Go | **Python** (与 DevolaFlow 同源) | tasks:14 |
| **D12** | 鉴权与凭据 | 共享 env / 子 CLI 自管 / 自有 vault / 不管 | **共享宿主 env + 子 CLI 自管 + popolad 不存凭据 + ArkTower schema 不含凭据字段** | spec §7 |

---

## 3. 路线选择记录

### 3.1 路线选择表

| 路线 | 描述 | 评分 | 状态 | 理由 |
|---|---|---|---|---|
| R1 | 纯 Skill MVP (最低成本) | 2.4/5 | 未选 | 无后台进程 / 无依赖图, 7 天能交付但上限太低 |
| R2 | Skill + 单 CLI MCP (Cursor 自闭环优先) | 2.6/5 | 未选 | 延后扩 CLI, 只覆盖单一通路 |
| R3 | Hybrid Skill + MCP + popolad daemon | 4.6/5 | Phase 0 fallback | 上一轮综合推荐, 用户升级为 R4 后保留作为参照 |
| **R4** | **Standalone TUI + Web Dashboard + popolad** | **4.4/5** | **Final Selected** | **用户 Q2 选; ArkTower 复用使其成本从 14 天压到 9 天, 上限最高** |
| R5 | 嵌入 DevolaFlow plugin | 2.5/5 | 未选 | 与 PopolaLoom 项目独立性目标冲突 |

### 3.2 R4 与 R3 评分对比表

| 维度 | R3 评分 | R4 评分 | 说明 |
|---|---|---|---|
| 形态 (覆盖度) | 3.5 | 5.0 | R4 五形态完整, 不依赖 IDE 在线 |
| 进程稳定性 | 5.0 | 5.0 | 同 (双模式 systemd + tmux) |
| 多 CLI 编排 | 5.0 | 5.0 | 同 (Phase 1 三 CLI) |
| 依赖图 (DAG) | 5.0 | 5.0 | 同 (LangGraph StateGraph) |
| HITL 完整度 | 4.5 | 5.0 | R4 多了 Web Dashboard 上的人工干预面 |
| 自演化能力 | 4.5 | 4.5 | 同 |
| 7-Day MVP 可达性 | 5.0 | 3.0 | R4 实际 9 天 (但因 ArkTower 复用, 比原估 14 天压缩) |
| 6-Month 上限 | 4.5 | 5.0 | R4 上限更高 (独立控制平面) |
| **加权综合** | **4.6** | **4.4** | R4 在 MVP 时长上劣 0.2, 但上限和形态完整度优 |

详细评分卡逻辑见 `specs/popolaloom/spec.md` §3 + `implementation-plan.md` Day 0-9 的工程量分配。

---

## 4. 实施细节定位指南

decision-doc 不重复实现细节, 以下交叉引用直达 source-of-truth。

| 关注点 | 详见文件 | 节 |
|---|---|---|
| 模块清单 (9 模块, 自写 vs 复用 ArkTower 比例) | `specs/popolaloom/spec.md` | §3.2 |
| 数据流 (调度 happy path 序列图) | `specs/popolaloom/spec.md` | §3.3 |
| HITL 数据流 (Lark + IDE + signal 三通道) | `specs/popolaloom/spec.md` | §3.4 |
| 关键 Schemas (ConductorDispatch / NDJSON CloudEvents) | `specs/popolaloom/spec.md` | §3.5 |
| 任务原语 (14 DevolaFlow 继承 + 7 新 Conductor) | `specs/popolaloom/spec.md` | §4 |
| 依赖契约 (ArkTower / DevolaFlow / LangGraph / lark-cli) | `specs/popolaloom/spec.md` | §5 |
| 非功能需求 (12 NFR 量化指标) | `specs/popolaloom/spec.md` | §6 |
| 安全与边界 (凭据 / Protected Branch) | `specs/popolaloom/spec.md` | §7 |
| 可观测性 (NDJSON / Prometheus / OpenTelemetry) | `specs/popolaloom/spec.md` | §8 |
| 路径与命名约定 (14 canonical paths) | `specs/popolaloom/spec.md` | §10 |
| Day-by-Day 实施 (Day 0-9) | `specs/popolaloom/implementation-plan.md` | Day 0-9 |
| ArkTower 依赖方式 (本地 editable 推荐) | `specs/popolaloom/adrs/0001-arktower-as-task-pool-dependency.md` | (Status: Proposed) |
| LangGraph 选型 | `specs/popolaloom/adrs/0002-langgraph-as-graph-engine.md` | (Status: Accepted) |

---

## 5. 自动 merge 边界条件 (Q8)

PopolaLoom 自演化阶段允许 **PR 自动 merge**, 但必须同时满足以下 5 条 AND 门 (任一不满足即阻断, 转人工 review + Lark 通知项目维护者):

1. **Gen-Verifier subgraph PASS**: dev/test/verifier 三节点 LangGraph subgraph 收敛通过 (出处: 03 §"Dev↔Test 闭环" Mode B)
2. **ArkTower 8-dim 自评测 ≥ 阈值** (默认 0.85, 可在 `nines.toml` 调整) (出处: 08 §3.4 + ArkTower README 8-dim 框架)
3. **0 Blocker / 0 Critical findings** (DevolaFlow gate 标准) (出处: DevolaFlow SKILL.md §Gate Mechanism)
4. **PR 仅触及 `popolaloom/*` 路径** (不允许跨项目变更, 静态 path-glob 检查) (出处: spec §7)
5. **目标分支非 main / master / yc_dev / production** (符合本仓库 Protected Branch Workflow 规则)

详细实现见 `specs/popolaloom/spec.md` §7 安全与边界 + `implementation-plan.md` Day 7。

---

## 6. 风险登记 (Top 5)

| # | 风险 | 严重度 | 出处 | 缓解措施 |
|---|---|---|---|---|
| **R-1** | ArkTower upstream breaking change | Major | ADR-0001 §6 | 锁 commit hash + 周对照 main 跑回归; sibling-intent issue 建立沟通 (Q-NEW-1) |
| **R-2** | Lark webhook 签名校验复杂 (HMAC + plan_id+ts+nonce) | Major | impl-plan Day 5 | 走本仓库 lark-cli skill 体系 (lark-im / lark-task) 不自写 SDK (Q-NEW-4) |
| **R-3** | SCC 决策不当导致循环不可见, popolad 调度死锁 | Critical | ADR-0002 §2.4 AP-1 | dev↔test 必须装入 LangGraph subgraph; 外层 task DAG 强制无环 (CI 静态检查) |
| **R-4** | popolad daemon 跨终端存活失败 | Major | tasks:18 + spec §3.2 | 默认 systemd-run + tmux 兜底 + nohup 容器降级; 启动时探测可用 supervisor |
| **R-5** | 自动 merge 引入回归 (误触 §5 边界) | Major | tasks:19 + §5 | 5 条 AND 门 + popolaloom/* 路径白名单 + Gen-Verifier 严格 + Lark 通知人工兜底 |

---

## 7. 新生 OpenQuestions (来自 Stage-1/2 分析)

| Q# | 问题 | 推荐 | 出处 |
|---|---|---|---|
| **Q-NEW-1** | 是否给 ArkTower 维护者 (同 org) 发 sibling-intent issue? | **推荐发**, Day 0 即起草, 使用 ADR-0001 §2.5 内容做 body | 08 §10 |
| **Q-NEW-2** | ArkTower 依赖方式: 本地 editable / git main / PyPI? | **推荐本地 editable** (`pip install -e ../reference/ArkTower[dev]`) + 周对照 main; 备选 git main + commit hash 锁定 | ADR-0001 §3 |
| **Q-NEW-3** | ArkTower NiceGUI 仪表盘是否直接当 PopolaLoom Web 仪表盘初版? | **推荐复用 + 增量** (spec §3.2 标记"复用 ArkTower"), 不 fork | 08 §7.2 |
| **Q-NEW-4** | Lark HITL 走本仓库 lark-cli skill 体系还是自写 SDK? | **推荐 lark-cli skill 体系** (lark-im / lark-task / lark-doc, 已在 workspace skills 列表中) | tasks:17 + spec §5 |
| **Q-NEW-5** | Cloud Agent 接口位是否 Phase 1 预留? | **推荐 Phase 1 预留** (Q9 用户选"可选支持", Phase 2 加 cloud adapter 时无须重构) | tasks:20 |

> 这 5 道是 Day-0 启动前可以非阻塞地一并确认的问题; 如用户在 Day-0 启动后再回答, 也仅影响 ADR-0001 状态从 Proposed -> Accepted (with conditions) 的推进时机。

---

## 8. Day 0 启动指引

完整 9-day 实施路径在 `specs/popolaloom/implementation-plan.md`。Day 0 canonical 启动命令 (一旦用户对 Q-NEW-2 回答"本地 editable"):

```bash
cd /home/agent/workspace/PopolaLoom && \
  pip install -e "../../reference/ArkTower[dev]" && \
  pytest /home/agent/reference/ArkTower/tests/ -q && \
  gh issue create --repo YoRHa-Agents/ArkTower \
    --title "PopolaLoom (sibling project in same org) intends to depend on ArkTower as task-pool layer" \
    --body-file .local/memory/specs/popolaloom/adrs/0001-arktower-as-task-pool-dependency.md
```

执行后请逐项核对:

- [ ] ArkTower 293 测试在本机通过 (任何 Failure 阻断 Day 1)
- [ ] sibling-intent issue 已在 `YoRHa-Agents/ArkTower` 创建, 引用 ADR-0001
- [ ] `.local/memory/specs/popolaloom/adrs/` 4 个 ADR 文件齐备 (0001/0002 已有, 0003/0004 在 Day 1-2 增量)
- [ ] 当前 git branch 为 feature 分支 (非 main/master), 符合 Protected Branch 规则
- [ ] ADR-0001 状态可推到 `Accepted (with conditions)`, conditions = 维护者无异议 + 本机 editable install 通过

---

## 9. 来源索引

| 文件 / URL | 角色 |
|---|---|
| `.local/tasks/init_popola_loom.md` | 用户原始任务 + 9 题确认答案 (lines 11-21) |
| `research/01-repo-landscape.md` (719 行) | 23 个项目对比矩阵 + ArcTower 候选搜索 |
| `research/02-cli-capabilities.md` (811 行) | Claude/Cursor/Codex/Kimi/Copilot 5 CLI 编排能力对照 |
| `research/03-dependency-methodology.md` (611 行) | 12 个 workflow engine + LangGraph 深度 + 3 模式循环表达 |
| `research/04-industry-best-practices.md` (637 行) | 9 厂商 + 一线观点 + 8 公理 + 反模式 |
| `research/05-interaction-patterns.md` (617 行) | 7 形态对照 + 5 用户旅程 + Skill vs MCP 评分 |
| `research/06-decision-and-routes.md` | (本文件) 用户面决策记录 + 12 维 + 5 路线 |
| `research/07-review-report.md` (223 行) | Stage-3 评审门 (99.1/100 PASS) |
| `research/08-arktower-deep-dive.md` (628 行) | ArkTower 仓库深度分析 (Verdict C: SUBSET) |
| `specs/popolaloom/spec.md` (606 行) | 项目设计规格 v1.0 (source of truth) |
| `specs/popolaloom/implementation-plan.md` (708 行) | 9-day Day-by-Day 实施计划 |
| `specs/popolaloom/adrs/0001-arktower-as-task-pool-dependency.md` (343 行) | ArkTower 依赖方式 (Status: Proposed) |
| `specs/popolaloom/adrs/0002-langgraph-as-graph-engine.md` (374 行) | LangGraph 图引擎 (Status: Accepted) |
| `https://github.com/YoRHa-Agents/ArkTower` | 上游兄弟项目 (任务池子层依赖) |
| `https://github.com/YoRHa-Agents/DevolaFlow` | 上游兄弟项目 (单 agent 内部编排) |
| `https://github.com/YoRHa-Agents/PopolaLoom` | 本仓库 (跨 agent 元编排) |
| DevolaFlow SKILL.md v10.1.0 | 14 stage primitives + 4 层 hierarchy + gate 机制 |

---

> 文档维护协议: 后续每次决策变更追加到 `.local/.agent/active/<change-id>/spec.md` 表达增量; 本文件仅记录已锁定的最终决策。
> 上次更新: 2026-05-03 11:35 UTC+8 (R4 锁定 + ArkTower 依赖确认 + mojibake 紧急恢复重写)
