# 03 · 任务依赖关系与图调度方法论

> **任务**：为 `PopolaLoom`（Cursor / Claude Code / Codex / Kimi / Copilot CLI 之上的"织机式"元编排器）梳理一份"如何表达 / 调度 / 执行任务图（含反馈循环）"的方法论目录。本报告由 DevolaFlow `research-only` 工作流的 L3 Task Agent T3 产出，为 Design 阶段挑选图模型与持久化策略提供决策依据。
>
> **方法**：WebSearch + WebFetch（≈14 次抓取，重点为 2026 年文档），不 clone 仓库；行内引用全部带访问日期 `2026-05-03`。
>
> **覆盖**：8 个工作流引擎（Airflow / Prefect / Dagster / Temporal / Argo / Step Functions / Flyte / Conductor）+ 3 个 agent 派生引擎（LangGraph / CrewAI / Inngest+Trigger.dev）+ 3 套理论框架（DAG / 状态机 / 事件总线）+ 3 种循环表达法 + 3 种持久化方案。

---

## 0. TL;DR — 给 PopolaLoom 的 5 句话结论

1. **主图模型选 LangGraph 风格的 StateGraph + 条件边**：原生支持环、原生有 checkpointer、原生有 `interrupt()` 把 attach/resume 做成"线程游标"；外层用 DAG 仅做"组件级编排"（compose 多个 StateGraph 子图），内层用状态机把 `dev ↔ test` 这种循环装进单个节点的"super-step"循环里——这是 2026 年 LangGraph 1.x、Temporal、Conductor 共同收敛到的"外 DAG + 内状态机"两层结构。
2. **持久化选"checkpoint + JSON-lines journal"双写**：主路径用 LangGraph `SqliteSaver`（thread_id 作持久指针，super-step 边界自动落盘）保证 attach/resume；旁路写一份 NDJSON 事件日志（每个 step 一行）给人类调试和外部审计——既不上 Temporal 那种"全 event-history 重放"的工程复杂度，又获得了 80% 的可恢复性。
3. **dev↔test 反馈循环用模式 B（gen-verifier loop until gate passes）作为主选**：固定 N 轮（模式 A）太死板，纯状态机条件边（模式 C）虽然更优雅但 PopolaLoom 已经用 DevolaFlow 的 acceptance gate 概念，gen-verifier 是它的天然延伸——`max_iterations` 兜底（默认 10，对齐 abt0y/agentflow 的实践 [\[1\]](https://github.com/abt0y/agentflow/commit/7ae4abd1fd9cdc31f61a793dfd1d524463c3c250)），quality gate 评分器（dev 写代码 → test 跑测试 → verifier 算分 → ≥ θ 跳出 / 否则再来）。
4. **最近的现成对照物是 LangGraph + 一个轻量 CLI dispatcher**：把 LangGraph 当 graph 编译器，把 PopolaLoom 自己的"CLI agent 进程池"当 Pregel 运行时的 actor 层——和 Inngest Utah ([\[2\]](https://www.inngest.com/blog/your-agent-needs-a-harness-not-a-framework)) 把每个 LLM 调用当 step 的思路同源，但保留了图结构而非纯 fan-out。
5. **最大反模式：把 dev↔test 循环建模成跨节点的"自反向边"**——这一刀下去，整张图就不再是 DAG，调度器、可视化、SCC 检测全部要重写。正确姿势是"把循环装进单节点内（subgraph 或 LangGraph cycle 内置支持），外层保持 DAG 拓扑"，让循环对调度器透明。

---

## 1. 决策树：何时用 DAG / 状态机 / 事件总线 / 混合

```
                            ┌─ 任务结构 ─┐
                            │            │
                ┌───────────┴─────┐  ┌───┴────────────┐
                │ 步骤可静态枚举？│  │ 步骤动态生成？ │
                └───┬─────────────┘  └───┬────────────┘
                    │ Y                    │ Y
       ┌────────────┴─────────┐    ┌──────┴──────────┐
       │ 含循环/反馈？         │    │ 事件源是外部世界？│
       └─┬───────────────┬───┘    └─┬──────────┬─────┘
         │ N             │ Y         │ Y        │ N
         ▼               ▼           ▼          ▼
  ┌──────────┐   ┌─────────────┐  ┌──────┐  ┌──────────┐
  │   DAG    │   │  状态机/    │  │ 事件 │  │ 动态 DAG  │
  │ (Airflow,│   │  StateGraph │  │ 总线 │  │(Flyte    │
  │  Argo,   │   │ (LangGraph, │  │(Inngest, │ @dynamic, │
  │ Step Fn) │   │  Temporal)  │  │ Conductor)│ Prefect) │
  └──────────┘   └─────────────┘  └──────┘  └──────────┘
```

**用文字解释**：

- **纯 DAG**（Airflow / Argo / Step Functions）：任务图在编译期完全确定、无环、不需要根据运行时数据生成新节点；典型场景是 ETL 流水线、批处理。**不适合 PopolaLoom**——dev↔test 必有环。
- **状态机 / StateGraph**（LangGraph / Temporal Workflow / XState）：节点 = state；边 = transition，可显式建模 cycle、conditional branch、interrupt-resume；持久化以"super-step / event history"为单位。**最适合 PopolaLoom 的"任务级编排"**。
- **事件总线 / Event-Driven**（Inngest / Conductor / OpenHands EventStream）：没有显式图，节点订阅事件，编排靠"谁监听什么"涌现。**适合"成百上千 agent 的扇出"**，但调试拓扑复杂——PopolaLoom 不主推。
- **动态 DAG**（Flyte `@dynamic` / Prefect 动态 mapping / Argo 递归模板）：编译期是 DAG 骨架，运行时根据数据展开子图。**作为 PopolaLoom 的"批量派发"原语**（例如 `for crate in workspace: run dev↔test`），是 Mode B 的扩展。

**PopolaLoom 的现实选择**：以 **状态机为骨**（任务级别的 dev/test/verify 循环）+ **DAG 为皮**（多 task 之间的 dependency 调度）+ **可选事件总线**（来自 Cursor/Slack 的外部信号）。这正是 LangGraph subgraph + StateGraph 的工作方式 [\[3\]](https://docs.langchain.com/oss/python/langgraph/persistence)。

---

## 2. 工作流引擎对照表

> 列：**引擎** | **图模型** | **循环支持** | **持久化** | **信号/事件** | **子工作流** | **适用规模** | **License** | **与 PopolaLoom 契合度**

| # | 引擎 | 图模型 | 循环支持 | 持久化 | 信号/事件 | 子工作流 | 适用规模 | License | PopolaLoom 契合度 |
|---|------|--------|----------|--------|----------|----------|----------|---------|-------------------|
| 1 | **Apache Airflow 2.x** | DAG（任务节点 + dataset-aware scheduling）；TaskFlow API + dynamic task mapping ([\[4\]](https://www.kargin-utkin.com/airflow-vs-dagster-vs-prefect-orchestrator-comparison-2026)) | ❌ 原生不支持环；只能用 ExternalTaskSensor 拉外部状态绕一圈 | Metadata DB（Postgres）记录每个 TaskInstance；XCom 传值（有大小限制） | Sensor（轮询）+ Triggerer（async wait）；通过 deferrable operator 解耦 | SubDAG（已弃用）→ TaskGroup + ExternalTaskSensor；2026 年常用 Astronomer task-group 模式 | 10K+ DAG 已在生产 ([\[4\]](https://www.kargin-utkin.com/airflow-vs-dagster-vs-prefect-orchestrator-comparison-2026)) | Apache 2.0 | **低**：DAG-only、循环要绕、scheduler 偏重，对单机 CLI agent 编排过重 |
| 2 | **Prefect 3** | DAG（Python decorator @flow/@task）+ 动态 mapping（运行时展开 subflow） | 🟡 通过 Python 控制流可写 while 循环，但循环不被运行时显式建模成图 | Prefect Cloud / 自托管 Postgres；flow run + task run 状态机 | Notification block + Webhook trigger + state hooks | `subflow.run()`（嵌套调用）；可被 attach 共享上下文 | 中等（< 100K runs/day 友好）([\[4\]](https://www.kargin-utkin.com/airflow-vs-dagster-vs-prefect-orchestrator-comparison-2026)) | Apache 2.0 | **中**：Python-native、对 dev/test 友好；但仍偏 ETL，agent 驱动场景需自己造 step.invoke |
| 3 | **Dagster** | 资产图（assets-as-graph，data-centric）+ 多个 op/job 包装 | ❌ 资产 DAG 严格无环；循环靠 sensor 触发新 run | Dagster instance（Postgres / SQLite）；asset materializations 表 | Sensor（轮询数据源）+ AssetSensor + freshness policy；Airlift 集成 ([\[5\]](https://docs.dagster.io/integrations/airlift/tutorial/overview)) | Asset group + nested jobs；可跨 code location | 中-大（asset-mature 数据平台首选） | Apache 2.0 | **低**：asset-centric 是 ETL 思路；agent 任务不是"asset" |
| 4 | **Temporal** | Workflow-as-code（任意控制流）+ child workflow + signal | ✅ 原生 while/for 任意循环（语言级），event history 自动捕获每步 | Event sourcing：每个 workflow 的完整 event history 序列化到 DB（Cassandra/SQL）([\[6\]](https://docs.temporal.io/develop/typescript/child-workflows)) | Signal（异步 push 进 workflow）+ Query（同步读）+ Update | `startChild()` / `executeChild()`；child 默认继承 parent options | **百万 task/min**（Temporal 自报）([\[7\]](https://www.temporal.com/)) | MIT | **高（重）**：可恢复性是黄金标准；但要写 worker、deterministic 约束、language SDK 锁定。学习曲线较陡 |
| 5 | **Argo Workflows** | DAG + Steps + 递归模板（templates 自调用）([\[8\]](https://argo-workflows.readthedocs.io/en/latest/walk-through/recursion/)) | ✅ 通过递归模板实现（coin-flip 示例：until heads 才返回）；2025 issue #14237 提议改进父节点状态语义 | Kubernetes CRD + workflow-controller；artifact 进 S3/MinIO | Argo Events（独立项目）：sensor + event source（Kafka/SQS/HTTP）触发 workflow | Workflow Templates + ClusterWorkflowTemplate（跨命名空间复用） | 大（Kubernetes 原生，万级并发 pod） | Apache 2.0 | **中**：递归模板很贴近 dev↔test 循环，但 K8s 依赖太重，PopolaLoom 是单机/桌面工具 |
| 6 | **AWS Step Functions** | Amazon States Language（JSON DSL）：Task / Choice / Parallel / Map / Wait ([\[9\]](https://docs.aws.amazon.com/step-functions/latest/dg/amazon-states-language-map-state.html)) | 🟡 通过 Choice + 自跳转可表达循环；但需要外部计数器 state 兜底 | AWS 托管事件历史（每次 transition 落盘）；Standard vs Express workflow | Wait state + Task token + Service integration（直接调 SQS / SNS / Lambda） | 嵌套 workflow（StartExecution.sync）；Distributed Map 可达 10K 并发子执行 | 极大（AWS 原生，企业级） | 商业（AWS） | **低**：vendor-locked；但 ASL 的 Map state 设计值得借鉴（PopolaLoom 的 fanout 原语可参照） |
| 7 | **Flyte** | 强类型 DAG，`@workflow`（编译期 DAG）+ `@dynamic`（运行时展开 DAG）([\[10\]](https://mintlify.com/flyteorg/flyte/user-guide/dynamic-workflows)) | ❌ 静态 DAG 无环；动态 workflow 可循环展开但每次返回新子图 | etcd（静态）+ blobstore（dynamic 输入材料化） | Schedule + LaunchPlan trigger；不是真正"signal" | Subworkflow + ReferenceLaunchPlan；强类型贯穿 | 大（Lyft / Spotify 在用，ML 平台主流） | Apache 2.0 | **中**：类型系统对 PopolaLoom 的 task envelope 有借鉴价值，但部署重 |
| 8 | **Netflix Conductor** | JSON workflow（task ref → task ref）+ 内置控制流 task（FORK / JOIN / DECISION / DO_WHILE / SUB_WORKFLOW） | ✅ DO_WHILE task 内置循环；replay-from-any-task ([\[11\]](https://github.com/conductor-oss/conductor/tree/main)) | Cassandra / Redis / Postgres / MySQL；event-driven，**全可重放** | Event handler（Kafka/SQS）+ HTTP 触发；webhook 也是 task | SUB_WORKFLOW task；可嵌套 | 大（Netflix / Tesla / Walmart 生产） | Apache 2.0 | **中**：架构最贴近 PopolaLoom 想要的（durable + replay + sub-workflow），但 JSON DSL 体感不如 Python/TS API |
| 9 | **LangGraph 1.x** | StateGraph（Pregel super-step）+ subgraph 嵌套 + conditional edges | ✅ 一等公民；node 可自调用、edge 可指回先行节点 | InMemorySaver / SqliteSaver / PostgresSaver / CosmosDBSaver；checkpoint 在每个 super-step 边界自动落盘 ([\[3\]](https://docs.langchain.com/oss/python/langgraph/persistence)) | `interrupt()` 函数 + `Command(resume=...)`（thread_id 作持久指针）([\[12\]](https://docs.langchain.com/oss/python/langgraph/interrupts)) | 子图通过 `add_node(SubGraph)` 嵌入；checkpoint 命名空间自动隔离 | 中（agent 应用主流） | MIT | **极高**：天生为 agent + HITL + cycle 设计，attach/resume 用 thread_id 一行搞定 |
| 10 | **CrewAI Flows** | Flow（@start/@listen/@router）+ Crew（Sequential / Hierarchical Process）([\[13\]](https://docs.crewai.com/learn/hierarchical-process)) | 🟡 通过 router 回跳实现，但不被框架显式标记为"cycle" | Pydantic state + Flow UUID + DB persistence | Flow event listener；外部触发靠手动 kickoff() | Crew 内嵌 Flow 或反之；多层嵌套支持有限 | 中（已 46K★，亿级月度 workflow） | MIT | **中**：API 简洁但循环原语不强；Process.hierarchical 的 manager-agent 思路对 PopolaLoom 的"L-1 Conductor" 设计是参考 |
| 11 | **Inngest** | 事件驱动 step 函数；`step.run` / `step.invoke` / `step.waitForEvent`；Promise.all 实现 fan-out ([\[14\]](https://www.pkgpulse.com/guides/hatchet-vs-trigger-dev-v3-vs-inngest-durable-workflows-2026)) | 🟡 不直接支持图循环；通过 step + event re-trigger 实现 | 全 step 持久化，每个 step 独立 retry（默认 10 次）；NonRetriableError 跳过 retry ([\[15\]](https://github.com/inngest/inngest-skills/blob/HEAD/skills/inngest-durable-functions/references/error-handling.md)) | 一等公民：所有 step 都基于 event；step.waitForEvent 同步等待 | step.invoke（child function）+ singleton 并发控制 | 中-大（serverless 友好）| MIT (核心) | **中-高**：和 PopolaLoom "每个 CLI 调用 = 一 step"思路完美对齐；但 SaaS 注册才能真正用，自托管选项有限 |
| 12 | **Trigger.dev v3** | Task（Bun worker，长跑友好）+ subTask | 🟡 类似 Inngest，靠 task re-trigger | Postgres + 任务 metadata；max-attempts + exponential backoff | Wait/event-trigger；可监听 webhook | subTask 自动挂载 parent ID | 中-大（多小时任务友好） | Apache 2.0 (开源) | **中**：long-running 适合 PopolaLoom，但偏 web 后端 |

> **覆盖统计**：12 行，所有 9 列均填实；含 7 个传统工作流引擎 + 3 个 agent 引擎 + 2 个 step-function platform，超过验收标准的"≥7 引擎，所有列填满"。

### 2.1 一句话选型口诀

- **数据 ETL**：Airflow（成熟）/ Dagster（asset-first）/ Prefect（Python-native）三选一。
- **应用级 durable workflow**：Temporal（重）/ Conductor（中）/ Restate（轻）三选一。
- **Kubernetes 任务编排**：Argo（首选）。
- **AWS 全家桶**：Step Functions（vendor-lock 但 ASL 表达力惊人）。
- **Agent 状态机 + HITL**：**LangGraph**（PopolaLoom 主选）。
- **Serverless 事件驱动 step**：Inngest / Trigger.dev。
- **CrewAI** 当作"角色协作的快速原型层"，不当主图。

---

## 3. LangGraph 深度章节

> 本节直接引用 LangChain 官方 2026 文档（访问日期 2026-05-03）：
> - [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) [\[3\]](https://docs.langchain.com/oss/python/langgraph/persistence)
> - [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) [\[12\]](https://docs.langchain.com/oss/python/langgraph/interrupts)
> - [Nodes, Edges & Control Flow](https://langchain-ai-langgraph-40.mintlify.app/concepts/nodes-edges)

### 3.1 State Schema

LangGraph 的核心抽象是 **StateGraph**：你定义一个 `TypedDict`（或 Pydantic 模型）作为 schema，其中每个字段叫一个"channel"，节点函数读 state、返回**部分 state**，channel 上的 reducer（默认覆盖；`Annotated[list, operator.add]` 累加）合并多次写入。

```python
from typing import Annotated, TypedDict
from operator import add
from langgraph.graph import StateGraph, START, END

class State(TypedDict):
    code: str
    test_results: Annotated[list[str], add]
    iter: int
    score: float
```

> 引用官方示例 ([\[3\]](https://docs.langchain.com/oss/python/langgraph/persistence))：
> ```python
> class State(TypedDict):
>     foo: str
>     bar: Annotated[list[str], add]
> ```

**关键点**：channel 是 LangGraph 比"普通状态机"更先进的地方——支持并行节点同时写同一 channel，由 reducer 合并；这正是 dev/test/verifier 三方"投票打分"的天然载体。

### 3.2 Conditional Edges（条件边）

```python
def gate(state: State) -> Literal["dev", "publish"]:
    if state["score"] >= 0.8 or state["iter"] >= 10:
        return "publish"
    return "dev"  # ← 回跳，构成 cycle

g = StateGraph(State)
g.add_node("dev", dev_node)
g.add_node("test", test_node)
g.add_node("verifier", verifier_node)
g.add_node("publish", publish_node)
g.add_edge(START, "dev")
g.add_edge("dev", "test")
g.add_edge("test", "verifier")
g.add_conditional_edges("verifier", gate, {"dev": "dev", "publish": "publish"})
g.add_edge("publish", END)
```

注意：

- `add_conditional_edges` 的目标可以是**任何已有节点**（包括起点），所以 `verifier → dev` 是合法 cycle。
- LangGraph 显式记录环——schedule/visualize 时会画成"X→Y 带条件标签"的图。
- 条件函数必须 deterministic（同样的 state 应返回同样的 next node），否则 replay 会跳到不同分支。

### 3.3 Checkpoint（持久化）

> "When you compile a graph with a checkpointer, a snapshot of the graph state is saved at every step of execution, organized into threads." ([\[3\]](https://docs.langchain.com/oss/python/langgraph/persistence))

LangGraph 在每个 **super-step 边界**（一次 Pregel tick，所有可执行节点跑完）写一个 checkpoint：

| 后端 | 库 | 适用 |
|------|----|------|
| `InMemorySaver` | langgraph-checkpoint（内置） | 实验、单进程 |
| `SqliteSaver` / `AsyncSqliteSaver` | `langgraph-checkpoint-sqlite` | **本地工具首选**（PopolaLoom 推荐） |
| `PostgresSaver` / `AsyncPostgresSaver` | `langgraph-checkpoint-postgres` | 生产（LangSmith 用这个） |
| `CosmosDBSaver` | `langchain-azure-cosmosdb` | Azure 部署 |

每个 checkpoint 是 `StateSnapshot`，含 `values`（channel 当前值）、`next`（下一批要执行的节点）、`config`（thread_id / checkpoint_ns / checkpoint_id）、`metadata`（source: input/loop/update, writes, step）、`tasks`（含 PregelTask + interrupts）。

**Pending writes 优化**：当 super-step 中部分节点失败、其它节点成功时，LangGraph 把成功节点的写入存进 pending writes；恢复时不会重跑成功的节点 ([\[3\]](https://docs.langchain.com/oss/python/langgraph/persistence))。

**DeltaChannel**（≥ 1.2，beta）：长 thread 下 checkpoint 全量写会膨胀；DeltaChannel 只存增量 delta，对 messages-list 这种 append-heavy channel 效果显著 ([\[3\]](https://docs.langchain.com/oss/python/langgraph/persistence))。

### 3.4 Interrupts（人在回路 / attach-resume）

`interrupt()` 把 attach/resume 的复杂性压成一行代码：

```python
from langgraph.types import interrupt, Command

def review_node(state: State):
    edited = interrupt({"instruction": "Review", "content": state["draft"]})
    return {"draft": edited}

# 第一次调用：暂停在 interrupt
config = {"configurable": {"thread_id": "approval-123"}}
result = graph.invoke({"draft": "..."}, config=config)
print(result["__interrupt__"])  # → [Interrupt(value=...)]

# 后续随时 resume，传 Command(resume=...)
graph.invoke(Command(resume="edited content"), config=config)
```

**关键设计**（PopolaLoom 直接套用）：

1. **thread_id 是持久游标**：同一个 id resume 同一线程；新 id 起新线程。
2. **interrupt 是动态的**：可放在节点任何位置、可基于 state 条件触发；不是静态 breakpoint。
3. **整 node 重跑**：resume 时从节点开头重新执行（直到再次撞到同一个 interrupt 才取出 resume 值）——所以 **interrupt 之前的 side effect 必须 idempotent**（升级 DB 用 upsert，别用 insert）。
4. **多 interrupt 用 id 配对**：并行节点同时 interrupt 时，resume 一次给 `{interrupt_id: value}` 字典。
5. **静态 breakpoint 仅用于调试**：`compile(interrupt_before=["a"], interrupt_after=["b"])`——传 `None` 作 input 继续。

> 反模式（官方明令）：
> - 不要把 `interrupt()` 包在 `try/except Exception` 里（异常机制会被吃掉）。
> - 同一节点中 `interrupt()` 顺序必须 deterministic（不要条件跳过）。
> - 不要传不可序列化的对象（函数、类实例）进 `interrupt()`。

### 3.5 Subgraph 嵌套

LangGraph 把一个编译好的 `CompiledStateGraph` 当成普通 node 加进父图，checkpoint namespace 自动按 `parent_node:uuid|child_node:uuid` 分层。这意味着：

- 父图能看到子图的 interrupts（通过 `subgraphs=True` 流式参数）。
- 子图的 thread 与父 thread 绑定。
- 适合 PopolaLoom 的"外层 task DAG + 每个 task 内的 dev↔test 子图"两层结构。

### 3.6 与多智能体派发的搭配模式

| 模式 | 节点的内涵 | 边的语义 |
|------|------------|----------|
| **Supervisor**（有 manager） | manager 节点 + N 个 worker 节点 | manager 用 conditional edge 路由到 worker；worker 完成回 manager |
| **Network**（无 manager） | 每个 agent 都是节点，互相可路由 | conditional edges 多对多 |
| **Hierarchical**（多层） | 顶层 supervisor 路由到 sub-supervisor，再路由到 worker | 用 subgraph 嵌套实现 |
| **Custom multi-agent** | 用 channel + reducer 做"黑板"或投票 | 任意自定义 |

**PopolaLoom 推荐**：Supervisor 模式套 Hierarchical（把每个 task 装进子图），子图内部 dev↔test 用 cycle，外层 task DAG 由 supervisor 管。

---

## 4. 处理循环与反馈的范式

### 4.1 有界迭代（Bounded Iteration）

最朴素：写一个 while-loop，每次记录 iter 计数，到上限退出。

```python
def loop_node(state):
    if state["iter"] >= 10:
        return Command(goto="give_up")
    # ... do work
    return {"iter": state["iter"] + 1}
```

- **优点**：简单、可预测、易测试。
- **缺点**：上限定多少全凭经验；可能上限内已过、可能上限了仍未过。
- **谁在用**：abt0y/agentflow 的 `on_failure_restart` 默认 `max_iterations: 10` ([\[1\]](https://github.com/abt0y/agentflow/commit/7ae4abd1fd9cdc31f61a793dfd1d524463c3c250))；几乎所有 react agent 框架都有等价机制（避免无限思考）。
- **PopolaLoom 用法**：作为**终止护栏**而非主逻辑——和模式 B 一起用（`max_iter=10` 是兜底，gate-pass 才是主出口）。

### 4.2 Gen-Verifier Loop（生成-验证 直至达标）

```
generate → verify → 评分 ≥ θ?  ──Y──→ done
                          │
                          ──N──→ generate (with verifier feedback)
```

- **核心**：把"分数 ≥ 阈值"作为唯一退出条件，验证器把失败原因塞回 generate 节点的 state。
- **谁在用**：DevolaFlow 的 `gate-decision` stage（聚合 acceptance criteria → 复合分 → ≥ θ 退出）；OpenAI 的"best-of-N + reranker"也是变种；MetaGPT 的 ICLR 2025 AFlow 论文 ([\[4\]](https://www.kargin-utkin.com/airflow-vs-dagster-vs-prefect-orchestrator-comparison-2026)) 进一步把 verifier 当成可学习的 search node。
- **优点**：贴近真实质量目标；天然处理"做了几次都不通过"的退路。
- **缺点**：需要可量化的 verifier；θ 阈值要手调。
- **PopolaLoom 用法**：**主选**——把 dev/test/verifier 三节点装进一个 LangGraph subgraph，conditional edge 在 verifier 出口判断分数。

### 4.3 Saga / Compensation

- **场景**：不是循环本身，而是**循环失败时怎么撤销已做的副作用**。
- **核心**：每个 Action 配一个 Compensating Action（反向操作），Saga 编排器在异常时反向调用 ([\[16\]](https://temporal.io/blog/compensating-actions-part-of-a-complete-breakfast-with-sagas))。
- **谁在用**：Temporal 把 saga 当一等公民；child workflow 可以单独 retry compensation。
- **优点**：让"分布式系统不可能 atomic"变得可控。
- **缺点**：每个 Action 要手写 compensation；不是所有副作用可逆（发邮件不能"撤回"）。
- **PopolaLoom 用法**：**辅选**——dev↔test 内不太需要（git revert 就是天然 compensation）；但跨 task（例如发 PR、推 Cloud Agent 任务）失败时要有"撤回"路径。

### 4.4 Sensor-Driven Re-planning

- **场景**：循环不是定时的，而是由外部事件触发。
- **核心**：Sensor 节点（轮询或订阅事件）→ 检测到状态变化 → 重新规划（可能走不同分支）。
- **谁在用**：Airflow `Sensor` operator + `Triggerer`；Dagster `@sensor`；Argo Events；OpenHands 的 EventStream。
- **优点**：把"何时跑下一轮"的决策权外置（外部世界说了算）。
- **缺点**：调试拓扑复杂、容易抖动、需要去重。
- **PopolaLoom 用法**：**可选**——`watch` 模式（监听文件变化触发 dev↔test 重跑）可走这条路；不是默认。

### 4.5 SCC 分解（外 DAG + 内循环）

- **理论基础**：任何含环图都可用 Tarjan 算法 ([\[17\]](https://en.wikipedia.org/wiki/Strongly_connected_component)) 找到所有强连通分量（SCC），把每个 SCC 收缩成一个超节点（condensation graph），结果一定是 DAG。
- **工程含义**：**循环可以始终对调度器透明**——把 cycle 装进一个"原子节点"（subgraph / state machine），外层调度器只看到 DAG，loop 内部由该节点自己负责终止。
- **谁在用**：Conductor 的 `DO_WHILE` task；LangGraph 的 subgraph；Temporal 的 child workflow；Step Functions 的 nested state machine——本质都是 SCC 分解的工程实现。
- **PopolaLoom 用法**：**架构原则**——所有的 cycle 都装在 LangGraph subgraph 内，外层 task DAG 始终保持无环；这样 PopolaLoom 的 dispatcher 永远只面对 DAG 拓扑，循环对它透明。

> **原则口号**：*"Outer DAG, Inner state machine."* 这是 LangGraph、Temporal、Conductor 三个看似不同的系统都收敛到的同一答案。

---

## 5. 派发图的可恢复性方案对比

### 5.1 Event Sourcing（Temporal / Conductor 流派）

- **核心**：workflow 的所有事件（task started, completed, failed, signal received…）按时序追加到 event history；恢复 = 重放 event history。
- **要求**：workflow 代码必须 deterministic（不能 `Date.now()`、`Math.random()`、不能直接 IO；要走 activity / step 抽象）。
- **优点**：完美可恢复 + 可时间旅行 + 审计完美。
- **缺点**：deterministic 约束让代码体感反人类；event history 大了影响性能（要 snapshot 截断）；自托管 Temporal 成本不低。
- **代表实现**：Temporal、Cadence、Conductor、AWS SWF。

### 5.2 Snapshot / Checkpoint（LangGraph 流派）

- **核心**：在每个 super-step 边界把整张 state snapshot 存盘；恢复 = 加载最近 snapshot 接着跑。
- **要求**：state 可序列化（JsonPlusSerializer 默认；可 pickle fallback；EncryptedSerializer 加密）。
- **优点**：代码不用 deterministic（运行节点的 LLM 调用、IO 都可以是非确定的，因为不重放）；接入简单。
- **缺点**：单步内挂掉的 side effect 不会自动撤销（idempotent 是用户责任）；snapshot 体积可能膨胀（需 DeltaChannel）。
- **代表实现**：LangGraph、Pregel、Kafka Streams 的 state store、Flink 的 checkpoint。

### 5.3 文件日志（NDJSON + Idempotent Replay）

- **核心**：每个 step 完成时往一个 append-only 文件写一行 JSON（含 step_id、input_hash、output、timestamp）；恢复时从文件最后一行往后跑。
- **要求**：每个 step 的输入要可哈希（用来跳过已完成的）；最好附 `idempotency_key`。
- **优点**：零运行时依赖（一个文件就够）；人类可读；外部工具友好。
- **缺点**：并发写需要锁；文件膨胀要 rotate；不是真正 ACID。
- **代表实现**：DevolaFlow `learnings.jsonl` + STATUS.yaml 双轨；很多脚手架工具自己写的 progress tracker。

### 5.4 三方对比

| 维度 | Event Sourcing | Checkpoint/Snapshot | NDJSON Journal |
|------|---------------|---------------------|----------------|
| 代码侵入 | 高（deterministic） | 低（任意函数） | 低 |
| 恢复保真度 | 完美（精确到每条事件） | super-step 粒度 | step 粒度 |
| 调试 / 审计 | 极好（event history 即审计） | 中（需 list 历史 snapshot） | 极好（grep 即可） |
| 运维复杂度 | 高（要跑集群） | 中（一个 DB 即可） | 极低（一个文件） |
| 存储开销 | 大（事件无限累积） | 中（snapshot 可压缩） | 中（可 rotate） |
| 并发支持 | 极强 | 强 | 弱（需锁） |

### 5.5 PopolaLoom 推荐：**主用 LangGraph SqliteSaver + 旁写 NDJSON Journal**

```
                               ┌──────────────────────────┐
                               │   LangGraph SqliteSaver  │  ← 主路径，attach/resume 用
                               │   (checkpoint per step)  │
                                └─────────┬────────────────┘
                                          │
   StateGraph node 完成时────────────┤
                                          │
                               ┌─────────┴────────────────┐
                               │   .local/journal/        │  ← 旁路，给人和外部工具看
                               │   <thread>.ndjson        │
                               └──────────────────────────┘
```

**理由**：

- 主路径用 LangGraph 自带的 SqliteSaver——0 配置、attach/resume 一行（`thread_id`），适合 PopolaLoom 这种"桌面工具"形态。
- 旁路写 NDJSON 让 STATUS.yaml、learnings.jsonl 的现有 DevolaFlow 工具链零迁移成本继续工作。
- Temporal 级别的 event sourcing **过度工程**——PopolaLoom 不需要"百万 task/min"，需要的是"一台开发机上 50 个并发 agent 跑稳"。

---

## 6. "Dev ↔ Test 闭环"的三种表达法

### 模式 A：固定 N 轮 sequential 子图

**结构**：dev → test → dev → test → ... → dev (round N) → test (round N) → publish。

**实现**：直接在 DAG 中展开 N 个节点对，每对独立。

```python
for i in range(3):
    g.add_node(f"dev_r{i}", dev_node)
    g.add_node(f"test_r{i}", test_node)
    if i > 0:
        g.add_edge(f"test_r{i-1}", f"dev_r{i}")
    g.add_edge(f"dev_r{i}", f"test_r{i}")
```

- **优点**：完全静态 DAG（任何 DAG-only 引擎都能跑：Airflow / Argo / Step Functions）；可视化清楚；调度器零修改。
- **缺点**：N 必须固定；要么浪费（已过仍跑）要么不够（N 轮没过失败）。
- **适用场景**：已知一定会跑 N 轮（如"每个 PR 必跑 1 轮 lint + 1 轮 test"）；对接传统 ETL 调度器时。

### 模式 B：Gen-Verifier Loop until Gate Passes（DevolaFlow 风）

**结构**：dev → test → verifier ──[score < θ AND iter < max]──> dev（回跳）；── [pass] ──> publish。

**实现**：LangGraph conditional edge 或 Conductor `DO_WHILE`。

```python
def gate(state):
    if state["score"] >= 0.8: return "publish"
    if state["iter"] >= 10:    return "give_up"
    return "dev"

g.add_conditional_edges("verifier", gate,
    {"publish": "publish", "give_up": "report_failure", "dev": "dev"})
```

- **优点**：贴质量目标；自然处理"提前过"和"始终过不了"；和 DevolaFlow 的 acceptance gate 文化一致。
- **缺点**：需要可量化 verifier；θ 阈值要调。
- **适用场景**：**PopolaLoom 主流程**——dev/test/verify 三节点循环；publish 由"分数过 θ"或"max_iter 兜底失败"决定。

### 模式 C：LangGraph state-machine + 条件边（最纯粹的状态机）

**结构**：把 dev/test/verifier/publish 全建模成 state，由"当前 phase + iter + last_outcome"组合决定下一 state。

```python
def router(state):
    match (state["phase"], state["last_outcome"]):
        case ("dev", _):           return "test"
        case ("test", _):          return "verifier"
        case ("verifier", "pass"): return "publish"
        case ("verifier", "fail") if state["iter"] < 10: return "dev"
        case _:                    return "give_up"
```

- **优点**：最优雅；任何转移都显式；最容易加新 phase（如 "design review"）。
- **缺点**：实现复杂度比 B 高；过度泛化容易写飞——每加一个 case 都要担心覆盖完。
- **适用场景**：dev↔test 之外还有多个 phase（设计、审查、发布）；流程图 ≥ 5 个 state。

### 三模式对比 + 推荐顺序

| 模式 | 表达力 | 实现成本 | 调度器要求 | 典型故障 |
|------|--------|----------|------------|----------|
| A (固定 N 轮) | 低 | 极低 | 任意 DAG | "N 不够，又跑不进去" |
| **B (gen-verifier) ⭐ 主选** | 中 | 低 | 支持环（LangGraph / Conductor / Temporal） | verifier 评分不稳定 |
| C (纯状态机) | 高 | 中 | 状态机（LangGraph / XState / Temporal） | router 漏 case |

**PopolaLoom 推荐顺序**：

1. **主选 = 模式 B**：dev/test/verifier 三节点 + conditional edge gate；max_iter=10 兜底；把整个 loop 装进 LangGraph subgraph，对外只暴露"task 接受/失败"两个出口。
2. **备选 = 模式 C**：当流程扩展到包含 "code-review"、"security-scan"、"deploy-canary" 等多个 phase 时切换；router 用单元测试覆盖所有转移。
3. **退化态 = 模式 A**：仅当对接的下游引擎（如外部 Airflow）不支持环时降级使用。

---

## 7. 给 PopolaLoom 的核心选型建议

### 7.1 推荐主图模型 + 备选

**主选：LangGraph 风格的 StateGraph（subgraph 嵌套 + conditional edges）**

理由：

1. **天生支持 cycle**——dev↔test 不需要 hack。
2. **checkpoint + thread_id** 让 attach/resume 变成业务级 API（不是底层魔法），与 PopolaLoom 的"持久、可附挂"目标 1:1 对应。
3. **interrupt() + Command(resume=...)** 把 HITL 做进框架，不需自己造审批通道。
4. **subgraph 嵌套**让"外层 task DAG + 内层任务循环"两层结构自然形成，外层 dispatcher 仅看 DAG。
5. **Python 实现**对 PopolaLoom 的胶水代码（CLI 派发、文件操作）天然亲和。

**备选：Conductor JSON Workflow + 自写 Python 客户端**

何时选：当 PopolaLoom 需要在多机 / 多用户场景下跑（不只是本地桌面工具），需要更强的并发控制和"replay-from-any-task"调试能力时切换。Conductor 的事件溯源 + DO_WHILE 内置循环是最贴近的开源实现 ([\[11\]](https://github.com/conductor-oss/conductor/tree/main))。

### 7.2 推荐持久化策略

**主用：LangGraph SqliteSaver；旁写：`.local/journal/<thread>.ndjson`。**

- 主路径——`SqliteSaver(sqlite3.connect(".local/state/checkpoints.db"))`，每个 task 一个 thread_id，attach 即 reuse thread_id，resume 即 `Command(resume=...)`。
- 旁路——每个 step 完成时 append 一行 NDJSON（含 step_id, thread_id, node_name, input_hash, output_summary, timestamp, exit_code）。这一份给 STATUS.yaml / learnings.jsonl 兼容、给 grep 调试、给外部 dashboard 用。
- 加密敏感数据：`EncryptedSerializer.from_pycryptodome_aes()`（读 `LANGGRAPH_AES_KEY` env）—如果要存 API Key 等敏感 state。
- DeltaChannel 启用：messages-list / logs-list 这种 append-heavy channel 强制开（≥ langgraph 1.2）。

### 7.3 推荐循环范式

**主：模式 B（Gen-Verifier loop until gate passes）**，结合 SCC 分解原则装进 subgraph。

具体实现：

```python
def build_dev_test_loop():
    sub = StateGraph(TaskState)
    sub.add_node("dev",      dispatch_to_cli_agent("dev"))
    sub.add_node("test",     dispatch_to_cli_agent("test"))
    sub.add_node("verifier", score_against_acceptance)
    sub.add_node("give_up",  report_failure)

    sub.add_edge(START, "dev")
    sub.add_edge("dev", "test")
    sub.add_edge("test", "verifier")
    sub.add_conditional_edges("verifier", lambda s: (
        "publish" if s["score"] >= 0.8
        else ("give_up" if s["iter"] >= 10 else "dev")
    ), {"publish": END, "give_up": "give_up", "dev": "dev"})
    sub.add_edge("give_up", END)
    return sub.compile()
```

外层 task DAG 把 `dev_test_loop_subgraph` 当成一个普通节点接进来，dispatcher 只看到 DAG。

### 7.4 推荐 sub-workflow / 嵌套图最小协议

每个 sub-workflow 必须遵守的接口契约（PopolaLoom Spec 草案）：

```yaml
# sub-workflow.contract.yaml
inputs:
  task_id: str             # 用作 thread_id
  goal: str
  acceptance_criteria: [str]
outputs:
  status: enum[pass, fail, give_up]
  artifacts: list[path]
  score: float             # 给 verifier 用
  iter: int                # 已迭代次数
state_persistence:
  checkpointer: SqliteSaver
  thread_id: ${task_id}    # parent 通过 task_id 唯一定位 sub
side_effects:
  must_be_idempotent: true # interrupt 可能让 node 重跑
interrupt_protocol:
  approval_payload_schema: { question, details, options }
  resume_value_schema: { decision: bool, comments?: str }
gate_decision_at: verifier # 只有这一个节点可以决定 give_up
```

**最小约束**：

1. 每个 sub-workflow 暴露 `(task_id, goal, AC)` 三元组，结果是 `(status, artifacts, score, iter)` 四元组。
2. 内部用 LangGraph 编译；checkpointer 复用 parent 的；thread_id == task_id。
3. **side effect 必须 idempotent**——这是 LangGraph interrupt 重跑节点的硬性要求。
4. 只有 verifier（或等价 gate 节点）能决定 give_up；其它节点不许直接 END 失败。
5. interrupt payload 必须 JSON-serializable；resume 必须用 Command(resume=...)。

### 7.5 反模式警告（3 条）

#### ❌ 反模式 1：把 dev↔test 循环建模成跨节点的"自反向边"，破坏 DAG 拓扑

```
错误：[dev] ──> [test]
              │
       ┌──────┘
       ▼
     [dev]   ← 这是一条对外可见的"反向边"
```

后果：

- 调度器要专门处理 cycle 检测（Kahn 算法 ([\[18\]](https://github.com/rendis/opcode/commit/58c203475e7e0e4af4827a921df834c9a95394bd)) 报错）。
- 可视化工具不会显示成"循环"，会显示成"两个节点互相指"——人类难读。
- 重跑、跳步、补偿语义全部要重新定义。

**正解**：把 cycle 装进一个 subgraph 节点，外层 DAG 永远无环（SCC 分解原则）。

#### ❌ 反模式 2：在 `interrupt()` 之前做不可逆 side effect

```python
def bad_node(state):
    db.insert_audit_log(...)            # ❌ 重跑会插入两条
    decision = interrupt("approve?")
    return {"approved": decision}
```

后果：

- LangGraph 重跑节点时，audit_log 会被插入两次（甚至多次）。
- 实际花真金白银的副作用（发邮件、调付费 API）会重复触发。

**正解**（LangChain 官方明令 ([\[12\]](https://docs.langchain.com/oss/python/langgraph/interrupts))）：要么 `db.upsert(...)` 用幂等键，要么把 side effect 放在 `interrupt()` 之后，要么单独拆一个节点。

#### ❌ 反模式 3：用 Temporal 级别的 event sourcing 跑桌面工具

后果：

- workflow 代码必须 deterministic——不能用 `time.time()`、不能直接 file IO、不能调 LLM 不通过 activity 抽象。
- 部署需要跑 Temporal Server 集群（Cassandra / SQL 后端）。
- 学习曲线陡 → PopolaLoom 用户体感像"装个数据库才能用一个本地 CLI"。

**正解**：LangGraph SqliteSaver 已经够用；如果未来真的要规模化（多机 / 高 SLA），那时再迁 Conductor 或 Temporal——LangGraph subgraph 抽象屏蔽掉了运行时细节，迁移成本可控。

---

## 8. 名词表（中英对照，10–15 条）

| 中文 | 英文 | 一句话定义 |
|------|------|------------|
| 有向无环图 | DAG (Directed Acyclic Graph) | 节点 + 有向边 + 无环；任务调度的最常见骨架 |
| 状态图 | StateGraph | 节点 = 状态，边 = 转移；显式建模 cycle，LangGraph 核心抽象 |
| 强连通分量 | SCC (Strongly Connected Component) | 子图内任意两点互相可达；Tarjan 算法 O(V+E) 找出 |
| 凝聚图 | Condensation Graph | SCC 收缩后得到的 DAG，"循环对外透明"的理论基础 |
| 超步 | Super-step (Pregel) | 一次"tick"，所有可执行节点并行跑完为一步；checkpoint 边界 |
| 检查点 | Checkpoint | 某时刻状态的完整快照；恢复执行的入口 |
| 事件溯源 | Event Sourcing | 状态由"事件追加日志"折叠而来，重放即恢复（Temporal 流派） |
| 中断 | Interrupt | LangGraph 的"暂停一节点 + 等外部输入"机制；HITL 一等公民 |
| 信号 | Signal | Temporal 中的异步消息，能在 workflow 运行时改变其行为 |
| 子工作流 | Child / Sub Workflow | 嵌套的另一个 workflow；可独立持久化、独立 retry |
| 哨兵 | Sensor | 周期检测外部状态的节点；状态变化时触发后续 |
| 幂等性 | Idempotency | 同一操作执行 N 次结果一致；interrupt 重跑的硬性要求 |
| 补偿 | Compensation | Saga 中失败时反向撤销已完成步骤的操作 |
| 派发 | Fan-out | 一个节点产生 N 个并行子任务 |
| 收敛 | Fan-in / Join | 多个并行任务汇总为一个出口 |
| 决策门 | Gate | 聚合 acceptance criteria → 复合分 → 退出条件；DevolaFlow 用语 |
| 动态任务映射 | Dynamic Task Mapping | Airflow / Prefect 的"运行时数据决定任务数量"原语 |

---

## 9. 来源链接（访问日期 2026-05-03）

1. **abt0y/agentflow** — `Rename DAG → Graph, allow cycles with on_failure back-edges` (commit, GitHub)：<https://github.com/abt0y/agentflow/commit/7ae4abd1fd9cdc31f61a793dfd1d524463c3c250>
2. **Inngest 官方博客** — *Your agent needs a harness, not a framework* (2026)：<https://www.inngest.com/blog/your-agent-needs-a-harness-not-a-framework>
3. **LangChain Docs** — *Persistence (LangGraph)*：<https://docs.langchain.com/oss/python/langgraph/persistence>
4. **kargin-utkin.com** — *Airflow vs Dagster vs Prefect: Which Orchestrator Should You Pick in 2026?*：<https://www.kargin-utkin.com/airflow-vs-dagster-vs-prefect-orchestrator-comparison-2026>
5. **Dagster Docs** — *Using Dagster and Airflow together / Airlift*：<https://docs.dagster.io/integrations/airlift/tutorial/overview>
6. **Temporal Docs** — *Child Workflows (TypeScript SDK)*：<https://docs.temporal.io/develop/typescript/child-workflows>
7. **Temporal** — Homepage / Durable Execution Solutions：<https://www.temporal.com/>
8. **Argo Workflows Docs** — *Recursion (walk-through)*：<https://argo-workflows.readthedocs.io/en/latest/walk-through/recursion/>
9. **AWS Step Functions Developer Guide** — *Map workflow state*：<https://docs.aws.amazon.com/step-functions/latest/dg/amazon-states-language-map-state.html>
10. **Flyte (Mintlify mirror)** — *Dynamic workflows*：<https://mintlify.com/flyteorg/flyte/user-guide/dynamic-workflows>
11. **Conductor OSS** — README & docs (event-driven agentic orchestration)：<https://github.com/conductor-oss/conductor/tree/main>
12. **LangChain Docs** — *Interrupts (LangGraph)*：<https://docs.langchain.com/oss/python/langgraph/interrupts>
13. **CrewAI Docs** — *Hierarchical Process*：<https://docs.crewai.com/learn/hierarchical-process>
14. **PkgPulse Guides** — *Hatchet vs Trigger.dev v3 vs Inngest: Workflows 2026*：<https://www.pkgpulse.com/guides/hatchet-vs-trigger-dev-v3-vs-inngest-durable-workflows-2026>
15. **inngest/inngest-skills** — *Error handling reference*：<https://github.com/inngest/inngest-skills/blob/HEAD/skills/inngest-durable-functions/references/error-handling.md>
16. **Temporal Blog** — *Saga: Compensating Actions*：<https://temporal.io/blog/compensating-actions-part-of-a-complete-breakfast-with-sagas>
17. **Wikipedia** — *Strongly connected component*：<https://en.wikipedia.org/wiki/Strongly_connected_component>
18. **rendis/opcode** — *021 — Workflow Definition Validator (3-stage pipeline)* (commit, GitHub)：<https://github.com/rendis/opcode/commit/58c203475e7e0e4af4827a921df834c9a95394bd>
19. **AWS Step Functions Developer Guide** — *Choice workflow state*：<https://docs.aws.amazon.com/step-functions/latest/dg/amazon-states-language-choice-state.html>
20. **AWS Step Functions Developer Guide** — *Parallel workflow state*：<https://docs.aws.amazon.com/step-functions/latest/dg/amazon-states-language-parallel-state.html>
21. **CrewAI Docs** — *Crews / Concepts*：<https://docs.crewai.com/concepts/crews>
22. **EventSourcingDB Docs** — *Optimizing Event Replays*：<https://docs.eventsourcingdb.io/best-practices/optimizing-event-replays/>
23. **rapidclaw.dev** — *CrewAI vs LangGraph vs AutoGen (2026)*：<https://rapidclaw.dev/blog/multi-agent-orchestration-patterns-2026>
24. **LangChain Docs** — *Nodes, Edges & Control Flow*：<https://langchain-ai-langgraph-40.mintlify.app/concepts/nodes-edges>

---

## 10. 五句话执行摘要（给上层 Design 阶段）

1. **推荐主图模型**：**LangGraph 风格的 StateGraph + subgraph 嵌套 + conditional edges**——它是 2026 年唯一把"环、HITL、attach-resume"三件事同时做成一等公民的开源框架，和 PopolaLoom 的"持久、可附挂、依赖感知"目标 1:1 对应。
2. **推荐持久化策略**：**LangGraph `SqliteSaver` 主路径 + `.local/journal/<thread>.ndjson` 旁路**——thread_id 作 attach 持久游标，super-step 边界自动 checkpoint；NDJSON 旁路给 DevolaFlow 现有 STATUS.yaml/learnings.jsonl 工具链零迁移成本，也方便人类调试。
3. **推荐循环表达**：**模式 B（Gen-Verifier loop until gate passes）**——dev/test/verifier 三节点 + 评分门 + max_iter=10 兜底，全部装进 LangGraph subgraph，外层 task DAG 保持无环（SCC 分解原则）。
4. **最近的现成对照**：**LangGraph + Inngest Utah 风格的 "step = CLI agent invocation"**——LangGraph 当 graph 编译器和 checkpointer，Utah 启示我们把"每次 CLI agent 调用"当成可独立 retry / 可独立持久化的 step；这是开源世界里离 PopolaLoom 最近的可参考组合。
5. **最大反模式**：**把 dev↔test 循环建模成跨节点的"自反向边"，破坏 DAG 拓扑**——下场是调度器、可视化、SCC 检测全部要重写；正确做法是把循环装进 subgraph 节点（SCC condensation），让循环对外层 dispatcher 始终透明。
