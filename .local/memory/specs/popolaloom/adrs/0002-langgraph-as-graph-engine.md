# ADR-0002 · 把 LangGraph 作为 PopolaLoom 图引擎与持久化骨干

> 编号: ADR-0002
> 标题: LangGraph as Graph Engine (StateGraph + SqliteSaver + SCC subgraph for dev↔test loop)
> 状态: **Accepted**
> 决策日期: 2026-05-03
> 作者: L3 Task Agent T3-v2 (Design 团队)
> 上游: 用户答案 Q5 = "可以使用 LangGraph" (`init_popola_loom.md:16`) + 03-dependency-methodology.md §0 + §3 + §7
> 下游: spec.md §3 Architecture / §3.5 Schemas / §5.3 LangGraph 依赖契约;implementation-plan.md Day 3 + Day 6
> 关联 ADR: ADR-0001 (ArkTower 作为任务池层依赖)
> 依赖文献: 03-dependency-methodology.md (612 行,T3 编写);05-interaction-patterns.md §"Pause-for-input 七种实现对比"

---

## 1. Context (背景)

### 1.1 问题陈述

PopolaLoom R4 路线需要一个能同时承担以下 6 件事的"图引擎":

1. **依赖图调度** — `dev → test`、`research → design → impl → test` 这样的多 task 拓扑
2. **dev↔test 反馈循环** — verifier 决定下一轮跑还是出口 (出处: 06 §1.1 用户原始诉求)
3. **跨 super-step 持久化** — daemon 重启 / 用户离开 IDE 8 小时后能完整恢复
4. **HITL interrupt 一等公民** — 派出 Lark 卡 + 等用户回答 + 续跑,不能写自定义 pause 机制
5. **subgraph 嵌套** — 把 dev↔test 循环装进单节点,外层调度器只看 DAG (SCC condensation 原则,出处: 03 §4.5)
6. **Python 原生 + 与 LangGraph 生态 (LangSmith / LangChain) 解耦能力** — Phase 2 想接观测时不锁死

### 1.2 候选范围

T3 在 03-dependency-methodology.md 系统调研了 12 个候选 (出处: 03 §2 表):

- **DAG-only 阵营** (Airflow / Dagster / Prefect / Argo / Step Functions / Flyte): 不原生支持 cycle,不适合 dev↔test 循环
- **状态机 / StateGraph 阵营** (LangGraph / Temporal / XState): 一等公民支持 cycle + checkpoint + interrupt
- **事件总线阵营** (Inngest / Trigger.dev / Conductor / OpenHands): 适合扇出但调试拓扑复杂
- **Agent 引擎特化阵营** (CrewAI / AutoGen): API 简洁但循环原语不强

T3 在 §7 给出明确推荐: **LangGraph StateGraph + SqliteSaver + subgraph 嵌套** (置信度高,理由见 §1.4)。

### 1.3 用户允许 (Q5)

用户在 `init_popola_loom.md:16` 答 "可以使用 LangGraph",**主动放行 LangGraph 作为依赖**。这与 06-decision-and-routes.md §0.0 Q5 完全锁定。**用户允许并不等同于设计层面的最优选**,因此本 ADR 仍需独立论证。

### 1.4 决策驱动力 — 为什么 LangGraph 在 12 个候选里胜出?

**逐条对照 PopolaLoom 6 个需求与 12 个候选 (出处: 03 §2 + §3 + §6)**:

| 需求 | LangGraph | Temporal | Conductor | Inngest | 自写 minimal DAG |
|---|---|---|---|---|---|
| (1) 依赖图调度 | ✅ StateGraph + conditional edges | ✅ workflow + child workflow | ✅ JSON workflow + DECISION | ✅ step + waitForEvent | ⚠️ 自己写拓扑排序 |
| (2) cycle / dev↔test loop | ✅ 一等公民 (conditional edge 可指回先行节点) | ✅ 任意 while/for | ✅ DO_WHILE task | 🟡 通过 step + event re-trigger | ❌ 必须破坏 DAG 拓扑 |
| (3) 持久化 / 跨 super-step | ✅ SqliteSaver / PostgresSaver / InMemory | ✅ event sourcing (要 cluster) | ✅ event sourcing (要 Cassandra) | ✅ step 持久化 (要 SaaS / self-host) | ❌ 自己造 |
| (4) HITL interrupt 一等公民 | ✅ `interrupt() + Command(resume=...)` | ✅ Signal | 🟡 Webhook | 🟡 waitForEvent | ❌ 自己造 |
| (5) subgraph 嵌套 / SCC | ✅ subgraph 自动 namespace | ✅ child workflow | ✅ SUB_WORKFLOW | 🟡 step.invoke | ❌ 自己造 |
| (6) Python 原生 + 解耦 | ✅ MIT / 无 SaaS / 可选 LangSmith | ⚠️ 重 (要 cluster + worker + deterministic 约束) | ⚠️ 重 (要 Cassandra + JSON DSL) | ❌ SaaS-first / 自托管选项有限 | ✅ |

LangGraph 是**唯一在 6 项全部 ✅**的 (Temporal / Conductor 也都满足但带巨大运维负担,出处: 03 §2 行 4 + 行 8 + §7.5 反模式 3)。

### 1.5 反向: 为什么不自写 minimal graph?

PopolaLoom 在 06 §0.0 Q5 备选中曾有"自写 minimal graph (Python ~ 200 行)"选项。T3 在 03 §7.5 反模式 3 与 §6 模式 B 中已经论证:

- 自写要重新发明 super-step / checkpoint / interrupt / SCC condensation 4 件事,LangGraph 已经把它们做成一等公民
- 自写带来更大的"代码 ownership 负担":每个新场景都要修改 graph runtime,LangGraph 是稳定的边界
- 测试代价: LangGraph 自身 PyPI 下载量 39M / 月 + Uber LinkedIn Klarna 生产案例,bug 已被群体测过;自写要 PopolaLoom 自己做 corner case 覆盖

**判断**: 自写仅在"避免 LangGraph 依赖"是硬约束时才考虑,Q5 已经放行,所以这条路不走。

---

## 2. Decision (决策)

### 2.1 核心决策

**PopolaLoom 用 LangGraph 1.x (`langgraph >= 0.6` 推荐 + `langgraph-checkpoint-sqlite`) 作为图引擎与持久化骨干**,具体形式:

- **主图**: 每个 plan 编译为一个 LangGraph `StateGraph`,nodes 一一对应 plan 的 task,edges 用 conditional_edges 表达跳转
- **subgraph (SCC condensation 原则,出处: 03 §4.5)**: 所有 cycle 装在 subgraph 内 — `dev↔test` 用 Gen-Verifier subgraph (出处: 03 §6 模式 B);`federate` 用 fan-out subgraph;外层 task DAG **永远无环**
- **持久化**: `SqliteSaver(sqlite3.connect("~/.popola/state.sqlite"))`,thread_id 一一对应 plan_id;每个 super-step 自动落盘 (出处: 03 §3.3)
- **HITL**: `interrupt(payload)` 暂停节点 + `Command(resume=value)` 续跑;interrupt 之前的 side effect 必须 idempotent (强制 unit test 覆盖);用户回答通过 PopolaLoom 三通道 (Lark / IDE / signal) 任一收集后调 `Command(resume=...)`
- **NDJSON 旁路**: LangGraph stream API 监听 super-step 事件,**同时**写 ArkTower TaskEvent + `~/.popola/events/<plan_id>.jsonl` (CloudEvents 1.0 信封),双轨保证 (a) 主路径 (SqliteSaver) attach/resume 用 (b) 旁路 (NDJSON) DevolaFlow 现有工具链 + grep 调试 (出处: 03 §5.5)
- **dev↔test cycle subgraph 模板** (Mode B Gen-Verifier,出处: 03 §6 模式 B):

```python
def build_gen_verifier_subgraph(spec) -> CompiledStateGraph:
    sub = StateGraph(TaskState)
    sub.add_node("dev",      dispatch_to_cli_agent("dev",      spec))
    sub.add_node("test",     dispatch_to_cli_agent("test",     spec))
    sub.add_node("verifier", score_against_acceptance_criteria(spec))
    sub.add_node("give_up",  report_failure)

    sub.add_edge(START, "dev")
    sub.add_edge("dev", "test")
    sub.add_edge("test", "verifier")
    sub.add_conditional_edges("verifier", lambda s: (
        "publish" if s["score"] >= 0.85
        else ("give_up" if s["iter"] >= 10 else "dev")
    ), {"publish": END, "give_up": "give_up", "dev": "dev"})
    sub.add_edge("give_up", END)
    return sub.compile(checkpointer=PARENT_CHECKPOINTER)  # 共享 parent checkpointer
```

外层 task DAG 把这个 `dev_test_subgraph` 当成单节点接入,dispatcher 永远只看到 DAG。

### 2.2 与 ArkTower FSM 的关系 (出处: spec.md §3.5 + §5.1)

LangGraph 与 ArkTower 各管不同层:

- **ArkTower FSM 管 task 状态生命周期**: `submitted / queued / in_progress / input_required / blocked / review / completed / failed / canceled / timed_out` 10 个状态 (出处: 08 §3.3)
- **LangGraph 管 plan 调度 + cycle 控制**: dev/test/verifier 节点之间的转换、dev↔test 循环 retry、Gen-Verifier 评分门
- **桥接**: LangGraph 节点函数内部调用 `arktower.core.task_service.TaskService.advance_task(task_id, trigger)` 翻 ArkTower FSM 状态;ArkTower EventBus 监听 `INPUT_REQUIRED` 事件触发 PopolaLoom 三通道 HITL,然后由 LangGraph `Command(resume=...)` 续跑

这种"LangGraph (plan 级 control flow) + ArkTower (task 级 lifecycle)"分层,是双方各自最强项的组合,**不重叠也不冲突**。

### 2.3 持久化双轨 (出处: 03 §5.5)

```
                         ┌───────────────────────────┐
                         │   LangGraph SqliteSaver    │  ← 主路径
                         │   ~/.popola/state.sqlite   │     attach/resume 用
                         │   thread_id = plan_id      │     super-step 边界自动落盘
                         └────────────┬───────────────┘
                                       │
       LangGraph stream() ─────────────┤
                                       │
                         ┌────────────┴───────────────┐
                         │  ~/.popola/events/<plan_id>.jsonl  │  ← 旁路
                         │  CloudEvents 1.0 信封              │     给人看 + grep + replay
                         │  append-only, rotate 50 MB         │     与 ArkTower TaskEvent 双轨
                         └──────────────────────────────────┘
```

主路径 SqliteSaver 由 LangGraph 自动管,popolad 仅传 thread_id;旁路 NDJSON 由 popolad 在 LangGraph stream API 钩子里 append。两轨**永远不互相依赖**,任一损坏不影响另一轨提供恢复能力。

### 2.4 反模式红线 (从 03 §7.5 引入,作为 LangGraph 使用必须遵守的 contract)

| 反模式 | 后果 | 防御 |
|---|---|---|
| **AP-1: 把 dev↔test 循环建模成跨节点的"自反向边"** (破坏 DAG 拓扑) | 调度器 / 可视化 / SCC 检测全部要重写 | popolaloom-graph 的 builder 函数仅暴露 subgraph 工厂,**不允许直接 add_edge 跨子图** |
| **AP-2: 在 `interrupt()` 之前做不可逆 side effect** | 重跑导致 audit log 重复 / API 重复调用 | unit test 强制每个 interrupt 节点都有 "is_idempotent_before_interrupt" 测试断言 |
| **AP-3: 用 Temporal 级 event sourcing 跑桌面工具** | deterministic 约束 + cluster 运维负担 | Phase 1 锁 SqliteSaver,**禁止**引入 Temporal / Conductor |
| **AP-4: 在 `interrupt()` 内传不可序列化对象** | 序列化失败 / replay 跳分支 | linter 检查 interrupt payload 必须 JSON-serializable |
| **AP-5: 让条件函数 (conditional edges) 非 deterministic** | replay 走不同分支 | gate 函数仅依赖 state,严禁读 `time.time()` / `random.random()` 之类 |

反模式 AP-1, AP-2, AP-3 已经在 spec.md §7.4 锁为 **hard NO**。

### 2.5 版本契约

```toml
# popolaloom/pyproject.toml
[project.dependencies]
langgraph = ">=0.6,<2.0"                    # API 稳定窗
langgraph-checkpoint-sqlite = ">=0.1,<2.0"  # SqliteSaver
langchain-core = ">=0.4"                    # 间接依赖,锁为 0.4+
```

**禁止**升到 langgraph 2.x 直到 PopolaLoom 跑全量 self-bootstrap 回归;每周一对照 PyPI latest 跑兼容测试。

### 2.6 LangGraph 不做的事 (与 ArkTower / DevolaFlow 边界)

- ❌ LangGraph 不存任务元数据 (Task / Dependency / TaskEvent) — 那是 ArkTower 职责
- ❌ LangGraph 不做 task 状态机 (10 状态 / 15 触发器 FSM) — 那是 ArkTower 职责
- ❌ LangGraph 不做派发到子 CLI subprocess (systemd-run / tmux) — 那是 popolaloom-adapter 职责
- ❌ LangGraph 不做 Lark / OS notify 推送 — 那是 popolaloom-lark / popolaloom/daemon/notify 职责
- ❌ LangGraph 不做 stage primitive 类型契约 — 那是 DevolaFlow 14 primitives 职责
- ❌ LangGraph 不做 8-dim 自评测 — 那是 ArkTower evaluation 框架职责

LangGraph 仅做"**在节点之间组织 control flow**"和"**在 super-step 边界自动 checkpoint**"两件事,边界清晰。

---

## 3. Consequences (后果)

### 3.1 正面 (优点)

- **HITL 一等公民**: `interrupt() + Command(resume=...)` 让 spec.md §3.4 的三通道 HITL (Lark / IDE / signal) 在恢复路径上**统一收口** — 任何通道收到用户答案后都调同一个 `graph.invoke(Command(resume=...), config={"thread_id":plan_id})`,无需自定义 signal API (出处: 03 §3.4)
- **dev↔test cycle 优雅装包**: SCC condensation 原则下,Gen-Verifier subgraph 是单个 LangGraph 实例,外层 task DAG 永远无环,popolad dispatcher 永远只面对 DAG 拓扑 (出处: 03 §0 TL;DR-5)
- **跨 super-step 持久化 0 配置**: SqliteSaver 自动在 super-step 边界落盘,popolad 重启 / 用户离开 8 小时都能 resume,**不需要自己设计 checkpoint 协议** (出处: 03 §3.3)
- **subgraph 嵌套 namespace 自动管**: 父图把子图当 node 加进去,checkpoint namespace 按 `parent_node:uuid|child_node:uuid` 自动分层 (出处: 03 §3.5)
- **Pending writes 优化**: super-step 中部分节点失败时,成功节点的写入存进 pending writes,恢复时不重跑成功节点 (出处: 03 §3.3) — 对 PopolaLoom 多 CLI 并行场景关键
- **DeltaChannel (≥ 1.2)**: messages-list / event-stream 这种 append-heavy channel 用增量存储,长 thread 不膨胀 (出处: 03 §3.3)
- **Python 原生 + 与 LangChain 解耦**: LangGraph 是独立 PyPI 包,**不强制**带 LangChain (popolaloom 不需要 LangChain LLM provider 抽象,因为 PopolaLoom 是 CLI dispatcher 而不是 LLM SDK 用户)
- **生态成熟**: LangGraph 月下载 39M / Uber + LinkedIn + Klarna 生产案例,corner case 已被市场测过;PopolaLoom 不需自己负担 graph runtime bug (出处: 03 §2 行 9 + §7.1)

### 3.2 负面 / 待管理风险

- **学习曲线**: 团队需要熟悉 StateGraph / Pregel super-step / DeltaChannel / pending writes / interrupt protocol 等概念。**缓解**: Day 3 实现 + Day 7 上手时,以 03-dependency-methodology.md §3 (LangGraph 深度章节) 作为 onboarding 文档
- **依赖体积**: `langgraph + langchain-core + langgraph-checkpoint-sqlite` 共约 ~ 30 MB Python 包。**缓解**: PopolaLoom 不引入 LangChain LLM 子包 (对 popolad 不需要),只装 graph runtime + checkpointer
- **SqliteSaver 性能上限**: 高并发下 sqlite3 single-writer 是瓶颈;PopolaLoom NFR-11 设 10 并发上限暂不会碰到,但**长期 (Phase 2 跨多机)** 要切 PostgresSaver。**缓解**: Phase 1 锁 SqliteSaver;Phase 2 切 Postgres 仅需改一行 `from langgraph.checkpoint.postgres import PostgresSaver` (LangGraph 抽象屏蔽了运行时细节,出处: 03 §3.3)
- **deterministic 约束 (节点重跑场景)**: LangGraph interrupt 重跑 node 时,该 node 内 side effect 要 idempotent,这一点比 Inngest "step.run 自动幂等" 更易出错。**缓解**: spec.md §7.4 反模式 AP-3 强制 unit test;impl-plan.md Day 3 + Day 5 把 idempotency 测试列入 acceptance
- **DeltaChannel 兼容**: langgraph 1.x 的 DeltaChannel 是 beta,与 SqliteSaver 序列化可能有边界 case (出处: 03 §3.3)。**缓解**: impl-plan.md Day 3 风险与 fallback 已登记 — 不行就退到 0.4.x checkpoint API
- **interrupt 内传不可序列化**: 函数 / 类实例 / 大 binary 不能直接进 `interrupt()` 载荷。**缓解**: PopolaLoom interrupt 仅传 LarkInterrupt schema (4 字段最小,出处: spec.md §3.5.4) 与 IDE elicitation form-mode 的 enum schema,**禁止**传非 JSON 对象;linter 检查
- **生态变化**: LangGraph 版本节奏 ~ 1 个 minor / 月,可能引入 break;**缓解**: §2.5 锁版本范围 + 每周一兼容测试

### 3.3 不可逆性评估

| 维度 | 不可逆度 | 备注 |
|---|---|---|
| StateGraph API 写法 | 中 | popolaloom-graph 函数会写 30+ 个 `g.add_conditional_edges(...)`,切换到其他引擎要全文修改 |
| SqliteSaver schema | 低 | LangGraph SqliteSaver 内部 schema 是黑盒,但 PopolaLoom 只用 thread_id 索引,切换 PostgresSaver 0 改动 |
| `interrupt() + Command(resume)` 协议 | 中 | popolaloom-lark / popolaloom-mcp 都在调这个 API,切换需要重写 HITL 接续逻辑 |
| subgraph 嵌套语义 | 低 | 概念性 (SCC condensation),与 LangGraph 实现无强绑定,切到 Conductor SUB_WORKFLOW 仅 10% 工程量 |
| NDJSON 旁路 | 0 | 完全独立于 LangGraph,与 03 §5.5 双轨设计保证 |

**总评**: 中等不可逆 — 切换成本约 1 周工程,可控。

---

## 4. Alternatives Considered (其他方案)

> 4 个备选,按"成熟度从高到低 + 工程负担从低到高"排序。

### 4.1 备选 (A): 自写 minimal DAG / 状态机 (Python ~ 200 行)

**做法**: 不引入任何外部图引擎,自己用 Python 协程 + asyncio.Queue + sqlite3 写一个最简的 task DAG runner;dev↔test cycle 用 Python while 循环硬编码。

**优点**:
- 0 外部依赖
- 完全控制运行时
- 学习成本 0

**缺点 (致命)**:
- super-step / pending writes / interrupt-resume / SCC condensation 全部自写,~ 800 LOC + 长尾测试
- 每个新场景 (subgraph / federate / handoff) 都要修 runtime
- 测试代价: 没有群体测试基础,每个 corner case 自己负担
- 失去与 LangSmith 等观测工具集成的可能 (Phase 2 想接时再迁移代价更大)

**判断**: ❌ 不推荐 — 03 §7.5 反模式 3 已经详细论证 "Don't reinvent Pregel"。

### 4.2 备选 (B): Temporal — workflow-as-code + event sourcing

**做法**: PopolaLoom 把每个 plan 封装为一个 Temporal Workflow,每个 dispatch 是 Activity;child workflow 装 dev↔test cycle。

**优点**:
- 完美可恢复 (event history 重放)
- 信号 (Signal) 是异步消息,持久化,跨重启不丢 (出处: 03 §2 行 4 + 05 §"Pause-for-input"-Temporal Signal)
- 百万 task/min 横向扩展能力 (PopolaLoom 用不上)

**缺点**:
- **deterministic 约束反人类**: workflow 代码不能 `time.time()` / `Math.random()` / 直接 IO,必须走 activity 抽象 (出处: 03 §5.1)
- **集群运维负担**: 自托管要装 Temporal Server + Cassandra/SQL,体感像"装个数据库才能用一个本地 CLI" (出处: 03 §7.5 反模式 3)
- **学习曲线陡**: Temporal 概念体系比 LangGraph 复杂得多
- **过度工程**: PopolaLoom 是单机桌面工具,不需要"百万 task/min"

**判断**: ❌ 不推荐 — 留作 Phase 3+ 跨机扩展时的考虑选项。

### 4.3 备选 (C): Inngest Utah ("harness, not framework") 移植

**做法**: 借鉴 Inngest 的 step-as-fn 模型,在 PopolaLoom 内部实现一个 "step.run / step.invoke / step.waitForEvent" 抽象,每个 CLI 调用是一个 step。

**优点**:
- 思路与 PopolaLoom "派发 = step" 完美对齐 (出处: 03 §0 TL;DR-4)
- 每个 step 独立 retry / 持久化
- 无显式图,扇出 / 顺序自然组合

**缺点**:
- Inngest 自身是 SaaS 优先 (出处: 03 §2 行 11),自托管选项有限
- 移植到 PopolaLoom 内部 = 自写 Inngest runtime,~ 600 LOC + 长尾测试 (回到 §4.1 自写陷阱)
- 没有 LangGraph 那种 SCC subgraph 概念,需要自己设计循环嵌套

**判断**: ⚠️ 思路可借鉴 (PopolaLoom 在 LangGraph 节点内调用 popolaloom-adapter 时确实是 step-style),但**不作为底层引擎**。

### 4.4 备选 (D): Netflix Conductor

**做法**: PopolaLoom 把 plan 写成 Conductor JSON DSL,DO_WHILE task 装 dev↔test cycle,SUB_WORKFLOW 装 Federate fan-out。

**优点**:
- DO_WHILE 内置循环 + replay-from-any-task (出处: 03 §2 行 8)
- event-driven + Cassandra/Redis/Postgres 全可重放
- JSON DSL 与 PopolaLoom 的 `ConductorDispatch` schema (spec.md §3.5.2) 名字巧合

**缺点**:
- **JSON DSL 体感不如 Python API** (出处: 03 §2 行 8 末尾)
- 自托管要 Cassandra/Redis (生态学习负担)
- 与 PopolaLoom 的"本地桌面工具"形态不匹配

**判断**: ❌ 不推荐 (Phase 1) — Phase 3+ 多机部署时考虑。

### 4.5 备选 (E): 不要引擎,直接用 Python 生成器 / asyncio

**做法**: PopolaLoom 不引入任何"图"概念,把 plan 翻译成一个 Python `async def plan_runner()` 函数,每个 task 是一个 await。

**优点**:
- 极简
- Python 习惯

**缺点 (灾难)**:
- 无持久化 (daemon 重启就丢)
- 无 cycle 抽象
- 无 HITL (interrupt 必须自写)
- 失去所有 §1.1 的 6 项需求

**判断**: ❌❌ 强烈不推荐。

### 4.6 综合: 推荐顺序

1. ✅ **(LangGraph)** — 本 ADR 推荐 (Phase 1 + 2)
2. ⚠️ **(Inngest 思路借鉴)** — 仅借概念,不作为底层
3. ⏳ **(Temporal)** — Phase 3+ 跨机部署时考虑
4. ❌ **(自写 minimal)** — 不推荐
5. ❌❌ **(Conductor / 直接 asyncio)** — 不推荐

---

## 5. Status (状态与未决事项)

### 5.1 当前状态

**Accepted** (2026-05-03 T3-v2 锁定)

理由: 用户答案 Q5 (`init_popola_loom.md:16`) 已经书面允许 LangGraph;03-dependency-methodology.md §0 + §3 + §7 给出系统性论证;ADR-0002 §1.4 已经在 12 个候选中明确选出 LangGraph 是唯一同时满足 6 项需求的低运维负担方案。**无需用户额外确认**。

### 5.2 推到 Deprecated 的条件

ADR-0002 转 `Deprecated` 仅在以下任一发生:

- LangGraph 自身被 archive / 不再维护 (当前生态预测 ≤ 1% 概率,3 年内 LangChain 停服)
- LangGraph 引入持续 6 周以上的 breaking change 而 PopolaLoom 无法接受 (例如 SqliteSaver 移除 / interrupt 协议改名)
- PopolaLoom 工程发现 SCC subgraph 在 LangGraph 1.x 实现下有不可解决的 corner case

若发生,切到 §4.2 Temporal 或 §4.4 Conductor + 提一个 ADR-0002-rev-2。

### 5.3 后续 ADR 中需要回应的问题

- ADR-0005 (Phase 2): "PopolaLoom 是否升级到 PostgresSaver 跨多机部署"
- ADR-0006 (Phase 2): "DeltaChannel 是否在 popolaloom event log 大 plan 场景启用"
- ADR-0007 (Phase 2 观测): "是否引入 LangSmith 作为 trace UI" (LangGraph 一等公民,但带 SaaS 依赖)

### 5.4 直接受影响的下游 artifact

- spec.md §3 Architecture (5 层组件图 / 数据流 happy path / 数据流 HITL interrupt)
- spec.md §3.5.1 PopolaTaskDispatch schema (`thread_id` 字段语义)
- spec.md §4.2 Conductor 原语 (handoff / supervise 都依赖 `interrupt() + Command(resume)`)
- spec.md §5.3 LangGraph 依赖契约
- spec.md §6 NFR-2 (attach 延迟 ≤ 200ms,LangGraph stream API 性能边界)
- spec.md §6 NFR-3 (event log 写入 < 5ms,旁路 NDJSON)
- spec.md §6 NFR-8 (失败回滚成功率 ≥ 95%,SqliteSaver pending writes 直接贡献)
- spec.md §7.4 反模式红线 AP-3, AP-4 (interrupt 之前 idempotent + DAG 严格无环)
- impl-plan.md Day 3 (LangGraph subgraph 编译 + SqliteSaver + NDJSON 旁路)
- impl-plan.md Day 5 (Lark 通道与 `Command(resume)` 接续)
- impl-plan.md Day 6 (Gen-Verifier subgraph 接入 DevolaFlow gate composite)

---

## 6. References

- 03-dependency-methodology.md (612 行,T3 编写,2026-05-03)
  - §0 TL;DR (5 句话主张:LangGraph + SCC subgraph + dev↔test loop B 模式)
  - §1 决策树 (DAG / 状态机 / 事件总线 / 混合)
  - §2 工作流引擎对照表 (12 引擎 × 9 列,LangGraph 是 PopolaLoom 契合度极高)
  - §3 LangGraph 深度章节 (StateGraph schema / 条件边 / Checkpoint / Interrupts / Subgraph / 多智能体派发模式)
  - §4 处理循环与反馈范式 (有界迭代 / Gen-Verifier loop / Saga / Sensor / SCC 分解)
  - §5 持久化方案对比 (Event Sourcing vs Checkpoint vs NDJSON Journal,LangGraph + NDJSON 双轨推荐)
  - §6 dev↔test 闭环三种表达法 (模式 A 固定 N / 模式 B Gen-Verifier / 模式 C 纯状态机)
  - §7 PopolaLoom 选型建议 (主图 / 持久化 / 循环 / sub-workflow 协议 / 反模式)
  - §7.5 反模式警告 (3 条 hard NO)
- 05-interaction-patterns.md
  - §"Pause-for-input 七种实现对比" (LangGraph interrupt 与 Temporal Signal / MCP elicitation 对比)
  - §"Approval Gate 模式" (interrupt_before / interrupt_after)
  - §"长等待后恢复 (Checkpoint + Replay)" (LangGraph + NDJSON 双轨)
- 04-industry-best-practices.md
  - §A8 公理八 (Resumable, not Restartable) — LangGraph SqliteSaver 直接落地
  - §1.7 LangChain (LangGraph + interrupt + resume 三件套)
- 06-decision-and-routes.md §0.0 Q5 (用户答案: 可以使用 LangGraph)
- spec.md §3 Architecture / §5.3 LangGraph 依赖契约 / §7.4 反模式红线
- LangGraph 官方文档:
  - Persistence — https://docs.langchain.com/oss/python/langgraph/persistence
  - Interrupts — https://docs.langchain.com/oss/python/langgraph/interrupts
  - Nodes, Edges & Control Flow — https://langchain-ai-langgraph-40.mintlify.app/concepts/nodes-edges

---

> ADR-0002 完成时间: 2026-05-03
> 维护者: PopolaLoom 项目组 / L3 Task Agent T3-v2
> 锁定下一步: impl-plan.md Day 3 落地 LangGraph 主图 + SqliteSaver + NDJSON 旁路;Day 5 接 Lark `Command(resume)` 续跑;Day 6 接 Gen-Verifier subgraph + DevolaFlow gate composite_score
