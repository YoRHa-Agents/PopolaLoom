# ADR-0001 · 把 ArkTower 作为 PopolaLoom 任务池层依赖

> 编号: ADR-0001
> 标题: ArkTower as Task Pool Dependency (本地 editable install + 同 reference checkout)
> 状态: **Proposed** — 等待用户对 Q-NEW-2 的最终确认 (依赖方式 A/B/C)
> 决策日期: 2026-05-03
> 作者: L3 Task Agent T3-v2 (Design 团队)
> 上游: Stage-1 调研 `08-arktower-deep-dive.md` §8.1 Verdict C + 用户答案 Q1
> 下游: spec.md §3 Architecture / §5.1 ArkTower 依赖契约;implementation-plan.md Day 0 + Day 1
> 关联 ADR: ADR-0002 (LangGraph 作为图引擎)
> 依赖文献: 08-arktower-deep-dive.md (628 行,T1-v2 编写);06-decision-and-routes.md §0.0 Q1

---

## 1. Context (背景)

### 1.1 问题陈述

PopolaLoom R4 路线 (用户答案 Q2 锁定) 的核心组件之一是一个能跨终端存活的本机 daemon (`popolad`),它需要内置 (a) 任务存储 (b) 状态机 / FSM (c) 事件总线 (d) MCP 暴露 (e) Web 仪表盘 5 个能力。如果完全自写,光 Day 1-3 就会被这 5 个底层能力消耗完 (出处: 06 §"R3 7-Day MVP" Day-1 至 Day-3,每天都在搭基础设施),挤压跨 CLI 派发器、HITL 桥、TUI 这些**真正差异化**模块的开发时间。

### 1.2 Stage-1 调研发现

T1-v2 在 `08-arktower-deep-dive.md` 中验证了以下关键事实 (引用 §1.2 + §2.2 + §6 + §7):

- **ArkTower (`https://github.com/YoRHa-Agents/ArkTower`) 是一个 Python 3.11+ / MIT / 0.1.0 / 已 293 测试 / 71% coverage / 同 org 的"被动任务池"基础设施**,自我定位为 *"foundation for agent-driven workflows — does not execute tasks itself"* (出处: README.md:21)。
- **ArkTower 已经实现了 PopolaLoom 需要的 5 项能力**: 10-state FSM (`arktower/core/state_machine.py:7-39`)、SQLite WAL + FTS5 持久化 (`arktower/store/sqlite_repository.py`)、12 MCP tools (`arktower/mcp/server.py:32-203`)、FastAPI REST + WebSocket、NiceGUI 5 页仪表盘 + 8-dim 评测体系。
- **ArkTower 与 PopolaLoom 的能力交集做集合运算后是 SUBSET 关系** (Verdict C,置信度 ≥ 90%): 10 项需求里 ArkTower 满足 4 项 + 部分满足 1 项 + 完全缺失 5 项 (跨 CLI 派发 / daemon / attach-resume / HITL / TUI)。**这 5 个缺失项正好是 PopolaLoom 的差异化空间** (出处: 08 §5 表)。
- **ArkTower 与 DevolaFlow 是 "schema-derived sibling" 模式**,同 org 但不互相 import (`migrations/004:2-3` + `.gitignore:28`),为 PopolaLoom 提供了协作模板。

### 1.3 Stage-2 用户决策 (Q1 答案)

用户在 `init_popola_loom.md:12` 明确给出 Q1 = `https://github.com/YoRHa-Agents/ArkTower`,**确认要使用真实仓库**,而非起初推测的 `Codename-11/ARC` (06-decision-and-routes.md §0.0 Q1)。这个答案直接锁定了"PopolaLoom 必须以某种形式整合 ArkTower"的决策起点,但**未明确具体依赖方式** (本地 editable / git main / vendor / fork 中的哪一种)。

### 1.4 R4 工程量缺口

如果完全自写 popolad daemon 的 5 项底层能力,根据 06 §"R3 7-Day MVP" 的估算大约要花 Day 1-3 共 3 天。如果 import ArkTower,根据 08 §8.2 的修改后 R4 plan,**Day 1 工程量缩减 70%**,节省的 2-3 天投入到 dispatcher 鲁棒性 + attach/resume 设计上。这是 ADR-0001 必须给出明确决策的工程驱动力。

---

## 2. Decision (决策)

### 2.1 核心决策

**PopolaLoom 通过本地 editable install 的方式将 ArkTower 作为 Python 包依赖**,直接 `import` 复用 ArkTower 9 个核心组件,**不修改 ArkTower 任何源代码**,差异通过 PopolaLoom 自身的 wrapper / extension 层提供。

具体形式:

```bash
# Day 0 启动时执行
cd /home/agent/workspace/PopolaLoom
pip install -e "../../reference/ArkTower[dev]"   # 本地 editable
pip install -e ".[dev]"                            # PopolaLoom 自身
```

`/home/agent/reference/ArkTower/` 是 Stage-1 已经 `git clone --depth 50` 落地 (08 §1.1) 的同 org sibling checkout,**作为 PopolaLoom 仓库的 git submodule 之外的独立 reference 树**,不入 PopolaLoom 的 git 仓库 (避免 vendor 模式带来的副本管理负担)。

### 2.2 import 清单 (10 个公共 API)

按照 08 §8.4 给出的列表,PopolaLoom 在 `popolaloom-core` 模块中 re-export 以下命名空间 (出处: spec.md §5.1 + §3.2 row `popolaloom-core`):

```python
# (1) 数据模型 (作为消息 schema 与状态包装)
from arktower.core.models import (
    Task, TaskCreate, TaskUpdate, TaskFilter, TaskEvent,
    TaskStatus, TaskPriority, Trigger,
    Dependency, DependencyType,
    TaskTemplate, PoolStats,
)

# (2) FSM (10 状态 / 15 触发器)
from arktower.core.state_machine import (
    StateMachine, TRANSITION_TABLE, TERMINAL_STATES,
    InvalidTransition, GateCheckError,
)

# (3) 事件总线 (PopolaLoom HITL hook 挂载点)
from arktower.core.event_bus import EventBus
from arktower.core.task_service import TASK_TRANSITION_EVENT, TaskService

# (4) 持久化 (SQLite WAL + FTS5)
from arktower.store.connection import DatabaseConnection
from arktower.store.migration import MigrationRunner
from arktower.store.sqlite_repository import SqliteTaskRepository

# (5) REST + WebSocket (popolad 内嵌 mount)
from arktower.api.rest_routes import router as arktower_router, create_app as create_arktower_app
from arktower.api.ws_manager import ConnectionManager

# (6) MCP server (12 tool 定义)
from arktower.mcp.server import create_mcp_server, TOOL_DEFINITIONS
from arktower.mcp.tools import TOOL_HANDLERS

# (7) NiceGUI 仪表盘 (作为 popolad web 入口的一部分)
from arktower.web.dashboard import setup_dashboard, get_service
from arktower.web.theme import apply_yorha_theme, get_colors
from arktower.web.i18n import t, set_lang

# (8) 评测框架 (PopolaLoom 自演化复用)
from arktower.evaluation.runner import EvalRunner
from arktower.evaluation.dimensions import EvalDimension, DimensionScore, EvalReport

# (9) 归档 (Phase 2 长 plan rotate 用)
from arktower.archive.snapshot_writer import SnapshotWriter

# (10) 配置门面
from arktower.config import Settings
```

### 2.3 不修改 ArkTower 源代码 — 通过哪些扩展点解决差异?

| 差异 | 解法 | 文件 |
|---|---|---|
| popolad 需要 daemonize / PID file / systemd unit (ArkTower 是前台进程,出处: 08 §3.6 + §6.10) | PopolaLoom 自写 `popolaloom/daemon/main.py` 包装 `arktower.api.create_app()` 为 ASGI 树根,加 `setsid` + PID 文件 | spec.md §3.2 row `popolad daemon` |
| dependency enforcement on enqueue (ArkTower 自承认 MAJOR gap, 出处: 08 §6 keyfact-5,`evaluators.py:333-338`) | 短期: PopolaLoom 在 dispatcher 层 advance_task 之前先自查 dependencies;长期: 给 ArkTower 提 PR 修这个 gap | impl-plan.md Day 1 + Day 0 PR plan |
| popolad 需要 cycle / dev↔test subgraph (ArkTower 只 DAG, 出处: 08 §7.3) | PopolaLoom 在 LangGraph subgraph 层处理 cycle (ADR-0002),ArkTower 只承担"task 状态机"职责 | spec.md §3.5 + ADR-0002 |
| TaskEvent 字段精简 (ArkTower 7 字段 vs DevolaFlow 14+ 字段, 出处: 08 §6 keyfact-14) | 短期: PopolaLoom 把扩展字段 (`progress_pct / artifacts / metrics / findings_by_severity`) 塞到 `TaskEvent.notes` JSON;长期: 给 ArkTower 提字段扩展 PR | spec.md §3.5.3 |
| popolad 自有 schema (`popola_dispatch / popola_relay / popola_handoff_signal`) | 通过 `005_popolaloom_extensions.sql` migration 加 PopolaLoom 自有表,**不 alter ArkTower 已有 4 个 migration** | impl-plan.md Day 1 row `popolad extensions migration` |
| HITL 通道 (ArkTower 完全没有 lark/notify/slack/webhook, 出处: 08 §3.6 + §6 keyfact-9) | PopolaLoom 自写 `popolaloom-lark` + `popolaloom/daemon/notify.py`,通过 `EventBus.subscribe(TASK_TRANSITION_EVENT, on_input_required)` 0 侵入挂钩 | spec.md §3.4 + impl-plan.md Day 5 |

### 2.4 启动顺序 (popolad 与 ArkTower)

popolad 进程内部启动顺序 (出处: spec.md §3.1 L2 + L3):

1. 加载配置 `~/.popola/config.toml`
2. 初始化 ArkTower `MigrationRunner` (跑 ArkTower 自有 4 个 migration + PopolaLoom 自有 `005_popolaloom_extensions.sql`)
3. 实例化 `arktower.store.SqliteTaskRepository`
4. 实例化 `arktower.core.event_bus.EventBus`,在其上 subscribe popolaloom-lark / popolaloom-notify hook
5. 实例化 `arktower.core.task_service.TaskService`
6. 把 `arktower.api.create_app()` 作为子 ASGI tree 挂到 popolad 自有 ASGI app 的 `/arktower` 前缀
7. 把 `arktower.web.setup_dashboard()` 作为 NiceGUI app 挂到 `/dashboard` 前缀
8. popolad 自有的 7 个 dispatch verb 注册到顶层 ASGI tree 与 unix socket 双通道
9. popolad 启动 `popolaloom-mcp` stdio server (子进程) 暴露 7 dispatch verbs + ArkTower 12 tool 转发
10. popolad 启动 LangGraph SqliteSaver (`~/.popola/state.sqlite`) 与 NDJSON event log 双轨

### 2.5 协作策略 (sibling-intent issue)

Day 0 启动**之前**或同时,在 ArkTower 仓库上提 issue *"PopolaLoom (sibling project in same org) intends to depend on ArkTower as the task-pool layer — confirm protocol stability, breaking change policy, and welcome for upstream contributions"* (出处: 08 §10 Q1)。issue 内容包括:

- (a) 复用清单 (即 §2.2 的 10 个 import)
- (b) 期望的 breaking change policy (semver minor 不 break,major 提前 30 天通知)
- (c) PopolaLoom 计划提交的两个 PR: PR-1 修复 dependency enforcement gap,PR-2 给 TaskEvent 加 progress_pct/artifacts/metrics 字段
- (d) 上游协作权限申请 (write access to issue / PR review)

**条件**: ADR-0001 状态从 `Proposed` 推到 `Accepted (with conditions)` 需要同 org 维护者至少 1 次 ack;若 24 小时无响应,允许 Day 1 启动 (本地 editable install 不依赖上游 ack),但 PR 暂不开。

---

## 3. Consequences (后果)

### 3.1 正面 (优点)

- **解锁 70% Day 1 工程量** (出处: 08 §TL;DR + §8.2):原本"自建 popolad daemon + SQLite + state machine + MCP + Web 仪表盘"的 1-3 天工程改写为"`pip install` 复用 ArkTower 6 个组件 + 自写 daemon wrapper + dispatcher + HITL + TUI 4 个差异化模块"
- **复用 293 测试 + 71% coverage**: ArkTower 自带的高质量测试为 popolad 底层提供"工具级保险" — popolad 自己只写差异化层的测试,核心层 bug 概率降低
- **同 org 维护者协作摩擦小**: YoRHa-Agents 同 org,issue / PR 响应快,可以快速反馈上游 (§2.5 sibling-intent)
- **schema-derived from DevolaFlow 完美对齐**: ArkTower Task 模型已含 6 组 DevolaFlow-derived 增强字段 (`migrations/004:2-3`),与 PopolaLoom 期望的 dispatch payload 直接吻合,**无 schema mapping 工程**
- **NiceGUI 仪表盘 0 改动复用**: ArkTower web 已有 5 页 (pool overview / task board / task detail / analytics / dependency graph) + EN/ZH i18n + dark/light + YoRHa Tower theme,PopolaLoom 仅追加 4 个 popola-specific 页面 (出处: 08 §7.2 row `web`)
- **8-dim 自评测框架直接复用**: PopolaLoom 自演化的"自评估"目标在 §1.4 的 R4 plan 中本来要从头实现 evaluator,现在仅需替换 8 个维度定义为 PopolaLoom 特有 (`dispatch_isolation / cycle_convergence / hitl_latency / attach_correctness / cross_cli_handoff / single_threaded_writes / event_log_completeness / token_budget_compliance`,出处: spec.md §6.4 + impl-plan.md Day 6)

### 3.2 负面 / 待管理风险

- **ArkTower upstream breaking change 风险**: 当前 ArkTower 在每天高频 commit (08 §1.2: latest commit 2026-05-03 当天有 4 个 PR 合入),如果上游引入 breaking change 而 PopolaLoom 在追 main,会破坏 popolad 启动。**缓解**: spec.md §9 R-5 已登记;Phase 1 锁定到 commit `467a087`,每周对照 main 跑一次回归测试;ADR-0001 §3.5 明确 sibling-intent issue 中要求 breaking change policy。
- **sibling-intent issue 必要性**: 跳过 issue 直接 import 是技术上可行的 (MIT 协议允许),但是协作信号差;**缓解**: §2.5 已经明确 ADR-0001 状态条件性 (`Proposed → Accepted (with conditions)` 取决于上游 ack)。
- **同进程 import 增加 popolad 故障半径**: ArkTower 内部 bug 会直接导致 popolad 崩溃,而 sidecar / IPC 模式下两边解耦。**缓解**: ArkTower 已 293 测试 + 71% coverage,质量已过关 (出处: 08 §1.2);LangGraph SqliteSaver + NDJSON 双轨持久化保证崩溃恢复 (出处: spec.md §3.4 末尾"冷启动恢复")。
- **PopolaLoom 自身 release 带 ArkTower 依赖**: 用户 `pip install popolaloom` 时也得装 ArkTower,出现版本兼容矩阵需求。**缓解**: pyproject.toml 用 `arktower>=0.1,<0.2` 锁定 minor 版本范围;Phase 2 推 ArkTower 上 PyPI 后用户体验改善。
- **不修改 ArkTower 源 → 长尾扩展受限**: ArkTower 的 `INPUT_REQUIRED` FSM、EventBus、Task model 已经够用,但若未来 PopolaLoom 需要某个非 hook-able 的修改 (例如改 `atomic_claim` SQL),只能提 PR 给上游;若上游不接受则 PopolaLoom 卡死。**缓解**: 本地 editable install 给了一个"紧急 fork 然后 import 的逃生通道",ADR 第 §4 alternatives 中保留 fork 选项的可能性 (但不推荐)。
- **测试隔离**: PopolaLoom CI 跑 pytest 时需要先装 ArkTower (editable),CI 配置变得依赖外部 checkout 路径。**缓解**: CI 用 `git submodule` 或 `gh repo clone` 自动获取 ArkTower 后再装。

### 3.3 不可逆性评估

| 维度 | 不可逆度 | 备注 |
|---|---|---|
| import 接口 | 中 | 一旦写满 100+ `from arktower.* import ...`,要切换到其他后端 (例如自写 task pool) 需要全仓 search/replace |
| schema 形状 | 低 | ArkTower Task model 字段是 superset,即使后续切换底座也可以用同一份字段 |
| daemon 进程模型 | 中 | popolad 现在 mount ArkTower ASGI app 进自身,如果切换 → popolad 主入口需要重写 |
| 评测框架 | 低 | EvalRunner 是独立组件,可以替换为别的 (LangSmith / 自写) |
| 仪表盘 | 中 | NiceGUI 框架 + ArkTower theme 的 lookup,切换需要 UI 重做 |

**总评**: 中等不可逆 — 切换成本相当于 1-2 周工程,不致命。

---

## 4. Alternatives Considered (其他方案)

> 4 个备选方案,按"上游耦合度从低到高"排序。

### 4.1 备选 (A): PyPI 版本绑定 — `pip install arktower==0.1.0`

**做法**: 给 PopolaLoom `pyproject.toml` 加 `arktower==0.1.0` 依赖,从 PyPI 拉。

**优点**:
- 最干净的 release 体验 (用户不用装 ArkTower 源码)
- 版本号语义明确,不受 main 分支波动

**缺点 (致命)**:
- ArkTower **目前未发到 PyPI** (出处: 08 §1.2 "version 0.1.0" 仅 git tag,无 wheel)
- 走这条路要先推动上游发包,堵在依赖路径上
- 开发期改 ArkTower 时无法快速验证 (要 release-or-bust)

**判断**: ❌ 不推荐 (Phase 1) — 留作 Phase 2 选项 (即 PopolaLoom v0.2.0 时切到 PyPI install,前提是 ArkTower 0.2.0 已发包)。

### 4.2 备选 (B): git main 分支 install — `pip install git+https://github.com/YoRHa-Agents/ArkTower.git@main`

**做法**: 给 `pyproject.toml` 加 git URL 依赖。

**优点**:
- 自动跟上 ArkTower 上游更新,无需本地 checkout
- CI 友好 (pip 自动 clone + install)
- release 时绑定 commit hash 比绑分支更稳

**缺点**:
- 上游 breaking change 直接传染 (没有"先在本地试一试"的缓冲)
- 开发期要给 ArkTower 改一行,必须先 push 到 ArkTower 远端再让 PopolaLoom 重装,**双仓 PR 节奏被 IO 拖慢**
- 离线环境完全跑不动 (没有缓存)

**判断**: ⚠️ 第二选 — 在 §2.1 的本地 editable 不可行时退回到这条路 (例如生产部署的 docker image 内装时,绑 commit hash)。

### 4.3 备选 (C): 本地 editable install + reference checkout — **本 ADR 推荐 (§2.1)**

**做法**: 见 §2.1。

**优点**: 同 §3.1 全部。

**缺点**: 同 §3.2 全部。

**判断**: ✅ **推荐 (Phase 1)**。

### 4.4 备选 (D): vendor 一份 ArkTower 源到 PopolaLoom 仓库

**做法**: 把 `arktower/` 目录直接拷贝到 `popolaloom/vendor/arktower/`,作为 PopolaLoom 仓库的一部分。

**优点**:
- 完全自包含 (clone 即用,无外部依赖)
- 离线 / air-gapped 环境友好
- 长尾修改 (越过 ArkTower 不接受 PR 的情况) 可以直接改 vendored 副本

**缺点**:
- 副本管理负担 (每次 ArkTower 升级要手动 sync)
- 同 org 项目 vendor 是反协作信号 (出处: 08 §7.5 "不推荐 fork" 同理)
- PopolaLoom 仓库膨胀 (ArkTower 仓库已 ~30 MB)
- 失去上游 bugfix 推送

**判断**: ❌ 不推荐 (Phase 1) — 仅在 sibling-intent issue 被拒绝 + 上游不肯接 breaking change PR 的双重 worst case 下使用。

### 4.5 备选 (E): fork ArkTower

**做法**: 在 PopolaLoom 仓库里维护一个永久 fork (`YoRHa-Agents/ArkTower-Popola`),按 PopolaLoom 节奏改。

**优点**: 完全控制权。

**缺点 (灾难)**:
- 同 org 项目 fork 是高度反协作信号
- 失去所有上游 bugfix / 新特性
- ArkTower 当前每天高频 commit,fork 后每周要 rebase
- 与 ADR-0001 §1.2 的 Verdict C 直接冲突 — 反而把"复用 sibling 节省工程量"变成"独立维护两个项目工程量翻倍"

**判断**: ❌❌ 强烈不推荐。

### 4.6 综合: 推荐顺序

1. ✅ **(C) 本地 editable install + reference checkout** — Phase 1 主路径
2. ⚠️ **(B) git main install + commit hash 锁定** — 当 (C) 不可行时的退路 (例如 docker production)
3. ⏳ **(A) PyPI 版本绑定** — Phase 2 (前提: ArkTower 推 PyPI)
4. ❌ **(D) vendor**: 仅 disaster recovery
5. ❌❌ **(E) fork**: 永不

---

## 5. Status (状态与未决事项)

### 5.1 当前状态

**Proposed** (2026-05-03 T3-v2 起草)

### 5.2 推到 Accepted 的条件

ADR-0001 转 `Accepted (with conditions)` 需要满足 **AND**:

1. 用户对 06-decision-and-routes.md §8 Q-NEW-2 "依赖方式" 给出书面答案 (推荐选 (A) 本地 editable install + reference checkout,即本 ADR §2.1)
2. ArkTower 仓库已存在 sibling-intent issue (§2.5),issue body 含本 ADR §2.2 + §3.5 + §3.5 中提及的协作要点
3. impl-plan.md Day 0 子任务 1+2 跑通 (`pytest /home/agent/reference/ArkTower/tests/ -q` 显示 293 passed)

### 5.3 推到 Accepted (无条件) 的条件

ADR-0001 转 `Accepted` (移除 with conditions) 需要满足 §5.2 全部 **AND**:

4. ArkTower 同 org 维护者对 sibling-intent issue 至少 1 次 ack (任何形式: 评论 / 加 label / 说 "yes")
5. PR-1 (修 dependency enforcement gap) 已发到 ArkTower (即使未合入也算,只要发出)

### 5.4 推到 Deprecated 的条件

ADR-0001 转 `Deprecated` 仅在以下任一发生:

- ArkTower 仓库被 archive / 删除 / 转移所有权
- 上游引入持续 6 周以上的 breaking change 而不接受 PopolaLoom 反向 PR
- PopolaLoom 工程量从 ArkTower 复用获得的红利低于 §3.1 估计的 50%

若发生,则切到 §4.4 vendor 模式 + 提一个 ADR-0001-rev-2 替换。

### 5.5 后续 ADR 中需要回应的问题

- ADR-0003 (待写,Phase 2 启动): "如何向 ArkTower 上游提交 PopolaLoom 自评测框架的扩展 PR"
- ADR-0004 (待写,Phase 2 启动): "PopolaLoom NiceGUI 自有 4 页与 ArkTower 5 页是否合并到一个 dashboard 单一入口"

### 5.6 直接受影响的下游 artifact

- spec.md §3.2 (模块清单 row `popolaloom-core`)
- spec.md §5.1 (ArkTower 依赖契约)
- impl-plan.md Day 0 (sibling-intent issue + editable install)
- impl-plan.md Day 1 (popolad mount ArkTower ASGI app)
- impl-plan.md Day 4 (popolaloom-mcp 转发 ArkTower 12 tool)
- impl-plan.md Day 6 (8-dim 自评测复用 ArkTower EvalRunner)
- impl-plan.md Day 7 (NiceGUI 仪表盘复用 + 增量 4 页)

---

## 6. References

- 08-arktower-deep-dive.md (628 行,T1-v2 编写,2026-05-03)
  - §1.1 克隆方式 + §1.2 仓库元数据 (验证 ArkTower 真实仓库 + 质量基线)
  - §3 架构概览 (FSM / 数据模型 / 调度流程 / 持久化 / HITL hook)
  - §4 与 DevolaFlow 关系 (schema-derived sibling 模式)
  - §5 PopolaLoom 需求映射表 (10 项需求 → 4 满足 / 1 部分 / 5 缺失)
  - §6 关键发现 15 条 bullet (含 file:line 证据)
  - §7 与 R4 路线兼容性 (复用清单 + 冲突点 + sidecar/import/fork 三选一)
  - §8 Verdict C (置信度 90%+) + R4 plan 重写
  - §10 OpenQuestions Q1-Q4 (sibling-intent / 依赖方式 / PR-1 / TaskEvent 字段)
- 06-decision-and-routes.md §0.0 Q1 (ArcTower 来源 — 用户答案锁定 ArkTower)
- 06-decision-and-routes.md §0.0 Q2 (路线 R4)
- spec.md §3.2 模块清单 + §5.1 ArkTower 依赖契约
- ArkTower repo:`/home/agent/reference/ArkTower/`
  - `README.md:21` — *"It does not execute tasks itself"*
  - `arktower/core/state_machine.py:7-39` — 10 states / 15 triggers
  - `arktower/core/models.py:56-110` — Task 42 字段
  - `arktower/mcp/server.py:32-203` — 12 tool definitions
  - `arktower/.workflow/config.yaml` — DevolaFlow gate 风格的 self_update + quality_gates
  - `migrations/004_add_enriched_fields.sql:2-3` — DevolaFlow-derived 字段声明
  - `arktower/evaluation/evaluators.py:333-338` — dependency enforcement on enqueue MAJOR gap (PopolaLoom PR-1 修复点)

---

> ADR-0001 完成时间: 2026-05-03
> 维护者: PopolaLoom 项目组 / L3 Task Agent T3-v2
> 锁定下一步: 等待用户对 Q-NEW-2 的依赖方式确认 + Day 0 启动 sibling-intent issue 起草
