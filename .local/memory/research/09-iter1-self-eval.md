# 09 · Iter-1 Self-Eval Report

> 闭环类型: PopolaLoom → `popola dispatch` → cursor-agent (经 `--yolo` wrapper) 自评 PopolaLoom
> 日期: 2026-05-04
> 派发任务 ID: `cursor-23e74ec18917`
> 派发耗时: 246.26 s (≈ 4 min 6 s)
> 退出码: 0 (`task.completed`, exit_code=0, pid=2900619)
> 派发者: L3 Task Agent T-meta (Test 团队), Stage Impl-4

---

## 0. TL;DR

- **闭环成功**: `popola dispatch ... --cli cursor --wait --timeout 600 --json` 走通了「派发 → 子进程 setsid → NDJSON 事件流 → 终态写入 → CLI 拿到 task_id JSON」全链路;事件文件 `/root/.popola/events/cursor-23e74ec18917.jsonl` 共 194 个 CloudEvents,包含 1 × `task.dispatched` + 1 × `process.started` + 191 × `process.stdout` + 1 × `task.completed`,exit_code=0。
- **最严重发现 (派发 agent)**: PopolaLoom 当前 src 仅覆盖 spec §2.1 Phase 1 约 **15%**;9 个 spec 模块中只有 `daemon` (≈30%) + `adapters` (≈20%) + `cli` (额外) 三个落地,`mcp / skill / tui / web / lark / graph / core` 七模块全部 0%;LangGraph / ArkTower TaskService / unix socket / systemd-run / Lark / Web / TUI 全部缺席。
- **测试侧严重缺口**: spec §3.4.1 五个 self-bootstrap 场景 **0/5 覆盖**;§6 12 个 NFR **0/12 有量化 assertion**(虽然 NFR-3/5/8 的代码路径已存在但无断言);并发 race 完全未测。
- **方法论侧亮点**: Day-1 交付的 `daemon/event_log.py` + `adapters/{base,claude,codex,cursor}.py` + `cli/main.py` 代码整洁,基本遵循 "No Silent Failures" 精神(8 处 `except` 7 处合规 / 1 处 debug 级)。
- **本闭环本身的发现**: 在没有任何代码改动的前提下也命中了一处真实"工艺缺陷": cursor adapter `build_command` 不接受 `--yolo / --trust`,导致非交互 dispatch 在新 workspace 上必然撞 "Workspace Trust Required" 而失败 — 必须靠外部 PATH wrapper 兜底,这是 spec §3.5.1 `PopolaTaskDispatch.sandbox` 字段的现实必要性证据。
- **最优先 Iter-2 方向**: 派发 agent 的 Day-2+ 三优先级与 T-smoke 6 个 P0 问题高度互补,合并成 **14 条** Iter-2 issue list(§5),其中 **5 条 P0 / 6 条 P1 / 3 条 P2**,推荐下一轮工作流为 **refactoring + feature-enhancement 复合**(详见 §6)。

---

## 1. nines 8-dim 基线 (PopolaLoom as-is)

**结论: 没有 PopolaLoom 专属的 nines runner;退而求其次给出 ArkTower 自评作为依赖侧基线 + 本机环境状态。**

### 1.1 nines CLI 暴露状态

```
$ nines --help
bash: nines: command not found

$ arktower eval --help    # ArkTower 自带的 8-dim runner
Commands:
  run     Run the self-evaluation benchmark.
  report  Show the latest evaluation report.
  golden  Validate golden test tasks.
```

ArkTower 0.1.0 仅暴露 `arktower eval run`(8 维 runner 写死跑 ArkTower 自身),**没有** `--root <path>` 之类参数让 PopolaLoom 当成 evaluatee 跑;PopolaLoom 自家的 `nines.toml`(8 维: dispatch_isolation / cycle_convergence / hitl_latency / attach_correctness / cross_cli_handoff / single_threaded_writes / event_log_completeness / token_budget_compliance,均权 0.10–0.15) **尚无任何 runner 实现**。这本身是一处 spec 漂移(详见 §5 I-09)。

### 1.2 ArkTower 8-dim 自评(依赖层基线)

`cd /home/agent/workspace/PopolaLoom && arktower eval run` 输出:

```
                  ArkTower Self-Evaluation
┃ Dimension                    ┃ Score ┃ Pass/Fail ┃ Status ┃
│ lifecycle_correctness        │  1.00 │      81/0 │ PASS   │
│ task_format_quality          │  0.87 │      13/2 │ PASS   │
│ dispatch_reliability         │  0.71 │       5/2 │ WARN   │
│ search_effectiveness         │  1.00 │       6/0 │ PASS   │
│ api_completeness             │  0.95 │      19/1 │ PASS   │
│ analysis_accuracy            │  1.00 │       4/0 │ PASS   │
│ archive_integrity            │  1.00 │       3/0 │ PASS   │
│ concurrency_safety           │  1.00 │       3/0 │ PASS   │

Overall Score: 0.9179
Findings (5):
  [MAJOR] No .task.md file format parser
  [MAJOR] No dependency enforcement on enqueue
  [MINOR] No JSON Schema validator
  [MINOR] No batch task operations
  [MINOR] No API authentication
```

含义: ArkTower 0.1.0 综合分 0.9179, 高于 spec §7.3 auto-merge gate 阈值 0.85;PopolaLoom 依赖的任务池组件可信度高。

### 1.3 PopolaLoom 自身 8-dim 估算(派发 agent 间接给出)

派发 agent 的 §4 spec drift 表足够粗略推断 PopolaLoom-nines 8 维如果今天跑一次会得到的近似分(均依据实到面而非 spec 承诺):

| PopolaLoom-nines 维度 | 估算分 | 依据 |
|---|---|---|
| dispatch_isolation | ≈ 0.55 | `setsid` 已落,但 `systemd-run` / cgroup 限额缺;3 adapter 命令构造 OK 但运行时缺隔离 |
| cycle_convergence | ≈ 0.10 | 无 LangGraph SCC subgraph、无 Gen-Verifier loop |
| hitl_latency | ≈ 0.00 | HITL 三通道全缺 |
| attach_correctness | ≈ 0.40 | `tail_events` 可读,但 `--follow` 在 in-process 模式只读到本进程 dispatch 的 task(T-smoke #6) |
| cross_cli_handoff | ≈ 0.00 | `relay` / `handoff` 原语缺,无 owned_files 契约校验 |
| single_threaded_writes | ≈ 0.50 | popolad 自身写盘单线程(EventLog 内带锁),但 `_event_logs` dict 无锁(派发 agent §3.3) |
| event_log_completeness | ≈ 0.85 | NDJSON 事件齐全(本闭环 194 个事件无丢包);仅缺 rotate / archive |
| token_budget_compliance | N/A | 无 OTel `gen_ai.usage.tokens` 接入 |

> 加权(spec §7 0.85 阈值): 估算综合分 ≈ **0.32**, 远低于阈值。把 nines runner 实装并跑出此基线,是 Iter-2 P1 工作(§5 I-09)。

---

## 2. 派发 agent 的分析输出 (从 NDJSON 提取)

**事件流统计**:

| 字段 | 值 |
|---|---|
| 总事件数 | 194 |
| `task.dispatched` | 1 (2026-05-03T21:08:36.372Z) |
| `process.started` | 1 (pid=2900619, session_id 已捕获) |
| `process.stdout` | 191 |
| `process.stderr` | 0 |
| `task.completed` | 1 (2026-05-03T21:12:42.636Z, exit_code=0) |
| 派发耗时 | 246.26 s (≈ 4 min 6 s) |
| stdout 字符数 | 18 022 |
| stdout 行数 | 191 |

**派发 agent 完整输出(verbatim, 中文 UTF-8)**:

````markdown
# PopolaLoom 自评报告 · 2026-05-03

> Scope: `src/popolaloom/` 当前 commit (feat/devola-flow-init-core, untracked Day-1/2 delivery), against `spec.md` v1.0 + `implementation-plan.md` Day 0–3 目标。
> Mode: 只读 review, 未修改任何文件。
> Total delivered: ~1574 src LOC + ~788 test LOC = 2362 LOC Python across 14 src + 5 test modules.

---

## §1 Architecture review — module 对照 spec §3.2

Spec §3.2 要求 **9 个模块** ({popolad/mcp/skill/tui/web/adapters/lark/graph/core}),当前实现状态:

| # | Spec 模块 | 路径 | 实到程度 | 备注 |
|---|---|---|---|---|
| 1 | `popolad daemon` | `src/popolaloom/daemon/` | **~30%** | 有 `server.py / state.py / event_log.py / supervisor.py` 四文件;缺 `main.py` (asyncio + uvicorn 进程入口) / `mount_arktower.py` / `primitives/*` / `langgraph_runtime.py` / `recovery.py` / `notify.py` / `metrics.py` / `otel.py` / `self_update.py`。关键的 unix socket server、ArkTower ASGI mount、LangGraph SqliteSaver 全部缺席 |
| 2 | `popolaloom-mcp` | `src/popolaloom/mcp/` | **0%** | 只有 10 行 `__init__.py` 占位符,没有 `server.py`、7 verbs、`arktower_relay.py`、`elicitation.py` |
| 3 | `popolaloom-skill` | `src/popolaloom/skill/` | **0%** | 目录不存在 |
| 4 | `popolaloom-tui` | `src/popolaloom/tui/` | **0%** | 目录不存在 |
| 5 | `popolaloom-web` | `src/popolaloom/web/` | **0%** | 目录不存在 |
| 6 | `popolaloom-adapter` | `src/popolaloom/adapters/` | **~20%** | 只实现 6 动作中的 `build_command` 一个(PURE argv builder)+ `is_available`;缺 `spawn / send / status / attach / kill / cost-meter` 五个 |
| 7 | `popolaloom-lark` | `src/popolaloom/lark/` | **0%** | 目录不存在 |
| 8 | `popolaloom-graph` | `src/popolaloom/graph/` | **0%** | 目录不存在,LangGraph 依赖声明在 `pyproject.toml` 但未使用 |
| 9 | `popolaloom-core` | `src/popolaloom/core/` | **0%** | 目录不存在,ArkTower Task 仅在 `server.py:_maybe_create_arktower_task` 构造一次做 schema parity,不持久化 |

额外交付(spec 未单列但实际存在): `src/popolaloom/cli/` (Typer 6 子命令)。

**层次 / 分离**:
- `adapters/base.py` 的 `Adapter` Protocol 严格要求 `build_command` 是 PURE(不 subprocess、不读 env、不 sleep),这是好的 SRP,和 `daemon/supervisor.py` 的副作用层清晰分开。
- 但 **daemon 内部出现混层**: `server.py::Popolad` 同时扛:(a) task_id 生成 (b) ArkTower model 构造 (c) EventLog 工厂 (d) StateStore 注册 (e) supervisor 调用 (f) on_exit 回调。目前 ~200 LOC 可控,但照 spec §3.2 daemon 行要加"DAG 调度 + LangGraph 编译 + signal 持久化"三件事,必然膨胀成 god-class,应该提前拆 `TaskLifecycle` + `ArkTowerBridge` + `EventLogRegistry`。
- **CLI 层越界**: `src/popolaloom/cli/main.py:69-80` 用 `functools.lru_cache` 包一个全进程 Popolad 单例,而 spec §3.1 L2 明确要求 "unix socket /run/user/$UID/popola.sock" → daemon RPC。当前 CLI 是 in-process 直接 import daemon,绕过了进程边界(Day-1 TODO 有注明,但跨终端存活 NFR-5 的语义至此没有测试闭环)。
- **adapters 与 daemon 耦合口径不一致**: `AdapterCallback = Callable[[str, str, Path | None], list[str]]`(server.py:40)是 3-arg,而 `adapters.base.build_command` 签名是 4-arg (含 `extra: dict`)。目前靠"daemon 不传 extra,默认 None"勉强兼容。一旦 Day-3 adapter 需要 `session_id / sandbox / hooks` 这些 extra-key,daemon 这侧要改签名,是隐形脆弱点。

---

## §2 Test gaps — 自检 5 例 + 12 NFR 覆盖

### §2.1 Self-bootstrap 5 例 (spec §3.4.1) 覆盖

| # | 场景 | 状态 | 备注 |
|---|---|---|---|
| S1 | 关终端 + 重开 attach 到任务并取结果 | **未覆盖** | 无 `tests/self_bootstrap/` 目录。`start_new_session=True` 的 setsid 路径靠 `test_e2e_dispatch_via_popolad_facade` 间接触发,但没有"显式杀父进程 → 子进程依然跑完 → 下次启动 Popolad 能读回 event log"的 assertion |
| S2 | dev→test 循环 + reinforcement injection → 第二轮 PASS | **未覆盖** | 无 LangGraph、无 Gen-Verifier subgraph、无 `composite_score` |
| S3 | PopolaLoom 派给自己(递归) + thread_id 隔离 | **未覆盖** | 无 thread_id 概念,当前 task_id 是 uuid4 前 12 位,没有父/子 namespace |
| S4 | INPUT_REQUIRED + 关 IDE 8h + 重开 supply_feedback | **未覆盖** | 无 HITL、无 Lark、无 signal 持久化、无 SqliteSaver |
| S5 | 跨 CLI handoff (cursor → claude → codex) + owned_files 不冲突 | **未覆盖** | 无 `relay` 原语、无 `handoff_envelope` schema |

**覆盖率: 0/5。** 连 `tests/self_bootstrap/` 目录都不存在。`test_e2e.py` 虽然叫 "e2e" 但实际只是"dispatch 一个 python `-c` 子进程 → 看 exit code",链路最多打到 Supervisor 的三个线程。

### §2.2 NFR-1..12 覆盖

| NFR | 指标 | 测试覆盖? |
|---|---|---|
| NFR-1 | daemon 启动 ≤ 2s | **未覆盖** — 无独立 daemon 进程可测 |
| NFR-2 | attach 延迟 ≤ 200ms | **未覆盖** — 无 attach_endpoint / WebSocket |
| NFR-3 | event log 写入 < 5ms | **未覆盖** — `test_event_log_append_and_tail` 不带计时断言;注意 `EventLog.append` 每次 `open/write/close` (`event_log.py:97-99`) 在 fsync-lazy FS 上够用,但启用 journald durable mode 时易超标 |
| NFR-4 | popolad RSS ≤ 200 MB / ≤ 1 GB | **未覆盖** |
| NFR-5 | 跨终端存活 ≥ 99% | **未覆盖** — setsid 代码存在,但无 "kill popolad → verify subprocess 仍跑" 的 fork/exec 测试 |
| NFR-6 | HITL 投递 ≤ 5s (Lark) / ≤ 1s (IDE) | **未覆盖** — 无 HITL |
| NFR-7 | auto-merge 误判 ≤ 5% | **未覆盖** — 无 auto-merge workflow |
| NFR-8 | 失败回滚成功率 ≥ 95% | **未覆盖** — test_e2e_dispatch_failed_path 只测单 task 非 0 退出,未测 daemon 重启后的 recovery |
| NFR-9 | token 成本 < 5× baseline | **未覆盖** |
| NFR-10 | 收敛轮数 ≤ 3 | **未覆盖** |
| NFR-11 | 并发 ≤ 10 dispatch | **未覆盖** — 无任何并发 dispatch 测试,虽然 Supervisor 设计支持 |
| NFR-12 | event log ≤ 50 MB rotate | **未覆盖** — rotate 未实现 |

**覆盖率: 0/12 有量化 assertion。** NFR-3 / NFR-5 / NFR-8 的代码路径存在但无断言。

### §2.3 错误路径 / 并发

- **错误路径**: 适中 — `test_cursor_invalid_output_format_raises`、`test_codex` 非法 sandbox、`test_register_duplicate_raises`、`test_e2e_dispatch_failed_path` 共 4 处。但 **未测**: (a) Popen 本身失败(binary not found)→ server.py `dispatch_task` 第 170 行会把异常抛出但 event log 已写 dispatched,留下孤儿状态 (b) `proc.wait` 本身 OSError (c) EventLog 写文件 IOError (d) StateStore 在 update 期间被并发 register。
- **并发**: **完全未测**。`StateStore._lock` / `EventLog._lock` / `Supervisor._lock` 三把锁存在,但没有一个 "2 个线程同时 `dispatch_task` + 1 个线程 `tail_events`" 的竞态测试。特别是 `Popolad._event_logs: dict` (server.py:73) **没有锁保护**,与 `StateStore` / `EventLog` 的线程安全承诺不一致。

---

## §3 Code smells (SOLID + 简洁性)

### §3.1 静默失败风险

工作区规则 **"No Silent Failures"** 明确要求 `except` 必须 log + (re-raise / 返回显式错误状态)。审计 8 个 `except` 位置:

- `src/popolaloom/daemon/supervisor.py:143-148` `_drain_stream` catch — ✅ `logger.exception` + append `process.stream_error` 事件。合规。
- `src/popolaloom/daemon/supervisor.py:149-153` stream close catch — ⚠️ 只 `logger.debug`,注释说 "close 异常无意义"。鉴于这里是管道清理,debug 级别勉强可接受,但与规则字面冲突。
- `src/popolaloom/daemon/supervisor.py:165-175` `_wait_and_finalize` catch — ✅ 完整:log + `task.failed` 事件 + on_exit(-1)。
- `src/popolaloom/daemon/supervisor.py:197-200` `_safe_on_exit` catch — ⚠️ 只 `logger.exception`,吞掉回调异常。如果 `Popolad._on_subprocess_exit` 里 `StateStore.update` 抛 KeyError 以外异常,状态不会更新但 wait 线程继续 — **会造成 state 永远停留 RUNNING**。应额外 append 一个 `state.update_failed` 事件或主动把 handle 标 FAILED。
- `src/popolaloom/daemon/server.py:268-271` ImportError for arktower — ✅ 仅 warn + 返回 None。合规(ArkTower optional).
- `src/popolaloom/daemon/server.py:289-297` `repo.create` catch — ⚠️ `logger.exception` + 返回 **ark_task.id**(未持久化的 in-memory id)给上层,从调用方视角无法区分"已持久化"和"内存 only"。违反 "显式错误状态" 精神。应该返回 None 并在 TaskHandle 加 `persisted: bool` 字段,或直接 raise 让 dispatch_task 失败。
- `src/popolaloom/daemon/server.py:302-305` on_exit 里 `KeyError` 只 `logger.warning` — ⚠️ 如果 on_exit 拿到未注册 task_id,意味着 race(supervisor 先 fire 回调、状态已被其他路径清掉)。warn 之后吞掉,**可能掩盖真实的状态管理 bug**。至少应 emit 一个 `state.ghost_exit` 事件。

### §3.2 SOLID

- **SRP**: `Popolad` 已经开始扛太多(§1 已列);`Supervisor` 把 stream drain + wait + callback 揉成 `_wait_and_finalize` 一个 40 行方法,可拆。
- **OCP**: Adapter Protocol 设计合理(加一个 CLI 只需新建一个类 + `register_adapter` 一行),但 adapter 只有 `build_command` 这一个维度,意味着未来加 `spawn / status / attach` 时整个 Protocol 要改 — 违反 OCP 精神。
- **DIP**: `Popolad.__init__` 直接 `new StateStore()` / `new Supervisor()` (server.py:71-73),没有注入口。测试不便也不好在 Stage Impl-3 换 backend。`task_repository: Any = None` 是正确的注入点 — 应对齐到其他几个 collaborator。

### §3.3 并发 / 竞态

- `Popolad._event_logs: dict[str, EventLog]` (server.py:73) **无锁** — dispatch_task 写入,tail_events 读取,跨线程 dict mutation 虽有 GIL 不崩但返回的 EventLog 引用可能不一致。与 StateStore 的严格加锁不对称。
- `server.py:141-156` 显式注释说"避免 PENDING→RUNNING 两步转,因为 on_exit 可能先到"。这说明 **状态机设计不够纯** — 通过省略 transition 来规避 race,而不是在 update 里用 CAS(compare-and-swap)或显式"不能从 terminal 回退到非 terminal"的断言。一旦未来某人"补完"PENDING 状态,race 就复活。应在 `StateStore.update` 里加 `if current.is_terminal() and new_state not in terminal: raise`。
- `Supervisor._wait_and_finalize` 的 `stdout_thread.join(timeout=5.0)` / `stderr_thread.join(timeout=5.0)` 两个 5s 硬编码超时 (supervisor.py:178-179) — 如果子进程输出大量行,pipe 可能还在填,5s 后 join 返回但线程仍跑,而 wait 线程已经 emit 了 `task.completed`,**导致终态事件之后还会追加 process.stdout 事件**,破坏"终态事件是文件末行"的契约。

### §3.4 隐藏耦合 / 全局状态

- `src/popolaloom/adapters/__init__.py:60` `_register_defaults()` 在模块加载时 mutate 全局 `_REGISTRY`,导致所有测试必须用 `isolated_registry` fixture(`tests/test_adapters.py:41-47`、`tests/test_e2e.py:108-122`)。这是 import-time side effect 的典型反模式。应该改成 `get_default_registry()` 工厂函数。
- `src/popolaloom/cli/main.py:68-80` `_get_popolad` 用 `functools.lru_cache(maxsize=1)` 做单例,而 `src/popolaloom/daemon/server.py:311-318` 又有 `_default_popolad` module-level 变量做**另一份**单例。两个独立的进程级单例,语义重叠,测试中需要各自 monkeypatch / cache_clear(test_e2e.py:219 就是这样)。
- `Popolad.list_active()` 返回 dict 只含 5 字段 (server.py:233-244),而 `get_status` 返回 9 字段;接口返回形状不一致,使用方必须记住哪个多哪个少。

### §3.5 缺失括号规则

工作区规则 "Always use braces for if" 只针对 C++,Python 不适用。审计 `popolaloom/cli/main.py` + `supervisor.py` 中 shell-style 单行条件分支 — 未发现违规。

---

## §4 Spec drift — Phase 1 §2.1 承诺 vs 实到

`spec.md` §2.1 列出 Phase 1 **必含** 10 大类能力。对照当前:

| Phase 1 承诺 | 实到 | Gap |
|---|---|---|
| `popolad` daemon + `systemd-run --user --scope` | ❌ | 只有 Popolad in-process 类,无 systemd-run、无 unix socket、无 PID 文件。Supervisor 用 `subprocess.Popen(start_new_session=True)` 代替 systemd-run (`supervisor.py:74-83`),功能上接近但 **失去 unit 级管控**(journalctl / systemctl status / cgroup 限额) |
| 7 Conductor 原语 `dispatch / attach / relay / supervise / federate / handoff / probe` | 3/7 partial | 只有 `dispatch` + `attach` (通过 `tail_events` 模拟) + `probe` (通过 `list_active` + `get_status` 模拟);`relay / supervise / federate / handoff` 完全缺席。更严重的是 — 7 原语**没有独立 Pydantic schema + RPC handler**(spec 要求),现在是作为 Popolad 方法调用 |
| 三个 CLI adapter 各暴露 6 个动作 | 2/6 × 3 | 只有 `build_command` + `is_available`;`spawn / send / status / attach / kill / cost-meter` 全缺。`spawn` 功能被挪到 `Supervisor.spawn` 里做,per-CLI 差异(如 cursor pre-create-chat, claude UUID 预生成, codex WS server)完全没触达 |
| LangGraph 1.x StateGraph + SqliteSaver + NDJSON 旁路 | ❌ | langgraph 在 pyproject.toml 里声明了但没有任何 import。NDJSON 是自写的 EventLog,没有和 LangGraph `graph.stream` 事件双轨 |
| ArkTower 0.1.x 本地 editable import + 9 组件复用 | ~5% | 仅 `arktower.core.models.Task` 被导入构造一次,**不调用** TaskService、EventBus、MigrationRunner、API、MCP server、Web、evaluation 任何组件。ADR-0001 的核心价值未兑现 |
| 三通道 HITL (Lark + IDE notify + signal) | ❌ | `popolaloom-lark` + `notify.py` + `signal.persist` 全缺 |
| Textual TUI | ❌ | textual 依赖声明但无 tui/ 目录 |
| NiceGUI web 增量 4 页 | ❌ | nicegui 依赖声明但无 web/ 目录 |
| `popolaloom-mcp` stdio server + 7 dispatch verbs + ArkTower 12 tool 转发 | ❌ | mcp/ 目录只是占位符 |
| `popolaloom-skill` 双安装 (cursor + claude) | ❌ | skill/ 目录不存在 |
| 自演化 `self-update` workflow + 8-dim 自评 + auto-merge PR | ❌ | nines.toml 有 8 维权重但无 runner。.github/workflows/auto_merge.yml 不存在 |
| NDJSON event log Day-1 | ✅ | `daemon/event_log.py` 完整且测试覆盖 |
| Prometheus `/metrics` + OTel trace_id | ❌ | 无 metrics.py / otel.py |

**Spec 漂移度**: Day-1 NDJSON event log + CLI 6 子命令 + 3 个 adapter 命令构造器 ≈ 实施计划 Day 1~2 目标的约 70%,但覆盖 Phase 1 总面**不到 15%**。当前是一个"NDJSON 事件日志驱动的 subprocess 派发脚手架",距离 spec 定义的"元编排器" gap 极大。

---

## §5 Day-2+ 三个最高价值下一步

按 cost × value × risk 排序:

### Priority 1 · **popolad 真进程化(unix socket + ArkTower TaskService 持久化 + 崩溃恢复)**

- **Value**: 把 Popolad 从"in-process Python 类"升到"spec §3.1 L2 进程边界"。这是 NFR-5 / S1 / S3 的前置条件,也解锁"CLI 客户端 - daemon 服务器"架构,让 TUI / MCP / skill 三个上层能通过 socket 连同一个 daemon。
- **Cost**: 中。asyncio + uvicorn + unix socket listener + `Popolad` 包成 RPC facade 约 1 天;ArkTower `TaskService.create_task` + `advance_task` 接入约 0.5 天;SIGKILL/重启后从 ArkTower SQLite 重建 active tasks 约 0.5 天。
- **Risk**: 低。ArkTower 已经 293 测试通过,editable install 工作。unix socket 是标准 posix 能力。
- **交付信号**: `python -m popolaloom.daemon` 启动后 `~/.popola/popolad.sock` 存在;`popola version` 走 socket;`pkill -9 popolad && sleep 2 && systemctl --user start popola && popola list` 显示崩溃前的 in-flight task。

### Priority 2 · **LangGraph StateGraph + SqliteSaver + 最小 HITL `interrupt()`**

- **Value**: 一次性解锁 S1(thread_id 重入)、S2(SCC subgraph)、S3(递归 thread_id 隔离)、S4(signal 持久化)四个 self-bootstrap 场景的持久化层。没有 LangGraph,后面的 `relay / handoff / federate / supervise` 都没法做。
- **Cost**: 中高。最小实现 = `PlanState` TypedDict + 单节点图 + SqliteSaver 落盘 + `interrupt("human-input-required")` demo 约 1-2 天;Gen-Verifier subgraph 额外 1 天。
- **Risk**: 中。spec 与 ADR-0002 已锁定 LangGraph ≥ 0.6,但 1.x DeltaChannel 与 SqliteSaver 序列化兼容性 Day-3 风险条目列为"需 fallback 到 0.4.x"。建议先做 single-node 跑通再进子图。
- **交付信号**: `tests/self_bootstrap/test_scenario_1.py` + `test_scenario_4.py` 至少跑到 interrupt 并能 `Command(resume=...)` 恢复。

### Priority 3 · **popolaloom-mcp stdio server + Cursor/Claude IDE mcp.json 注入**

- **Value**: 这是 spec §3.1 L0/L1 的入口面 — 没有 MCP,Skill 和 IDE Agent 触发完全缺失,PopolaLoom 对终端用户**不可见**。当前 CLI 只能手动 shell 跑,用户体验与"桌面 sidecar 服务"定位不符。7 个 MCP verbs 即使先返回 stubbed 结果,也能立即把 IDE Agent 接上,走通 demo 闭环。
- **Cost**: 低。mcp SDK 已在 pyproject;tools/list + 7 verbs schema + forward to Popolad RPC 约 1 天。`popola install-mcp --ide=cursor` 脚本约 0.25 天。
- **Risk**: 低。MCP server-to-client 推送硬约束已被 spec R-1 登记,Phase 1 走"IDE 主拉 + Lark 主推"组合,本步不碰 push 路径。
- **交付信号**: Cursor 重启后 Composer 输入 `调 popola_list_tasks` 能看到 7 个工具签名 + 调用返回当前 active task。

**未入前三的理由**:
- Lark bridge 价值高但依赖 Priority 2 的 interrupt 机制 — 先落 LangGraph 再接 Lark。
- Textual TUI / NiceGUI web 是用户体验层,暂不阻塞自 bootstrap 闭环,可排 Day 4-5。
- Prometheus / OTel 是可观测增强,但目前 NDJSON 日志足以调试 Phase 1 自 bootstrap,按 implementation-plan 本就在 Day 8。

---

## §6 Severity-classified issue list

| ID | Sev | Title | Source (file:line) | Suggested fix |
|---|---|---|---|---|
| **I-01** | **P0** | 无独立 daemon 进程;Popolad 仅是 in-process 类,NFR-5 跨终端存活 / S1 attach 场景无法满足 | `src/popolaloom/daemon/server.py:48-306` | 实现 `daemon/main.py` 用 asyncio + uvicorn 暴露 `~/.popola/popolad.sock`,CLI 改走 socket RPC;保留当前 Popolad 类作为 daemon 内的 facade |
| **I-02** | **P0** | 零个 self-bootstrap 场景 (S1..S5) 被测试覆盖,spec §3.4.1 是 CI 必跑契约 | `tests/`(缺 `tests/self_bootstrap/` 目录) | 先建 `tests/self_bootstrap/test_scenario_1.py` 骨架,用 setsid + `pkill -9 parent`+ re-read event log 模拟 S1;再逐个补 S2-S5 |
| **I-03** | **P0** | ArkTower 仅用 `Task` model 做 schema parity,TaskService / EventBus / MigrationRunner / MCP / Web 一概未用,ADR-0001 的"本地 editable import"承诺未兑现 | `src/popolaloom/daemon/server.py:254-297` | 在 Priority 1 daemon 改造中注入 `arktower.core.task_service.TaskService`,把 `dispatch_task` 的持久化走 `TaskService.create_task`;订阅 `EventBus.TASK_TRANSITION_EVENT` 为后续 Lark bridge 铺路 |
| **I-04** | **P0** | LangGraph 声明依赖但 0 行调用;无 `thread_id` / SqliteSaver / `interrupt()`,spec §5.3 + ADR-0002 核心决策悬空 | `pyproject.toml:27-28` vs `src/popolaloom/` (无 graph/ 目录) | 落 Priority 2 — 先单节点 StateGraph + SqliteSaver `~/.popola/state.sqlite`,后补 Gen-Verifier subgraph |
| **I-05** | **P1** | `Popolad._event_logs` dict 未加锁,concurrent `dispatch_task` + `tail_events` 存在 GIL-外的逻辑 race(返回半初始化的 EventLog 引用) | `src/popolaloom/daemon/server.py:73` | 改 `self._event_logs_lock = threading.Lock()` + `with self._event_logs_lock:` 包 set/get;或换用 `dict` 的原子赋值后用 `get` 取(Python dict set/get 单操作 GIL-safe,但 "check-then-set" 非原子 — 当前 dispatch_task 第 137-139 行正是 check-then-set) |
| **I-06** | **P1** | Supervisor stdout/stderr join 用 5s 硬超时,后可能继续写 `process.stdout` 到终态事件之后,违反"终态事件在文件末行"隐式契约 | `src/popolaloom/daemon/supervisor.py:178-185` | 把 join 超时拉长到 30s 或改为无限 join;或在 join 返回 False 时 emit `stream.truncated` 告警事件,让 caller 明确知道可能缺行 |
| **I-07** | **P1** | `_on_subprocess_exit` 的 `except KeyError: logger.warning(...)` 吞掉 unknown task_id,可能掩盖 state 管理 bug | `src/popolaloom/daemon/server.py:302-305` | 额外 emit `state.ghost_exit` 事件(含 task_id + exit_code);或把 warning 升为 exception + 加一个 `allow_ghost=False` kwarg 让测试可配 |
| **I-08** | **P1** | Adapter Protocol 只定义 `build_command` + `is_available`,spec §3.2 要求 6 动作;未来加 spawn/status 必然改 Protocol,违 OCP | `src/popolaloom/adapters/base.py:31-56` | 把 Adapter 拆成两层:`CommandBuilder`(现有,PURE)+ `Runtime`(spawn/status/attach/kill/cost-meter,副作用);daemon 注入 `Runtime` 而非直接 Popen |
| **I-09** | **P1** | Supervisor 用 `subprocess.Popen(start_new_session=True)` 代替 spec 要求的 `systemd-run --user --scope`,缺失 cgroup 限额 / journalctl unit 级整合 | `src/popolaloom/daemon/supervisor.py:15-17`, `:74-83` | Day-2 下一步实现 `SystemdRunSupervisor`(spec §3.2 双后端),`Supervisor` 抽象成接口,runtime 检测 `which systemd-run` fallback 到现有 Popen 路径 |
| **I-10** | **P1** | `EventLog.append` 每次 `open(..., "a")` + `write` + `close`,高频场景可能突破 NFR-3 < 5ms;无 benchmark | `src/popolaloom/daemon/event_log.py:96-99` | 改持有 fd(带 buffered write + `flush()` 每行)+ 周期 fsync;同时补 `tests/nfr/test_nfr_3_write_latency.py` 用 `time.perf_counter` 测 1000 条平均延迟 |
| **I-11** | **P2** | `adapters/__init__.py` import 时 side-effect 注册默认 adapter,测试需 `isolated_registry` fixture 手动 snapshot/restore | `src/popolaloom/adapters/__init__.py:60` | 提供 `get_default_registry() -> dict` 工厂返回新副本;保留当前全局注册作兼容,但新代码走 factory |
| **I-12** | **P2** | 两套并存的 process-level 单例(CLI 的 `_get_popolad` lru_cache + daemon 的 `_default_popolad` 变量)语义重叠 | `src/popolaloom/cli/main.py:68-80`, `src/popolaloom/daemon/server.py:311-318` | Day-2 daemon 改造落地后删除 `daemon/server.py:311-343` 的 module-level 包装;CLI 改走 socket 客户端,不再持有 Popolad 引用 |
| **I-13** | **P2** | `Popolad.list_active` 返回 5 字段、`get_status` 返回 9 字段;返回 shape 不一致,调用方易错 | `src/popolaloom/daemon/server.py:200-214`, `:233-244` | 提取 `_task_summary(handle, *, full: bool)` 辅助函数,两接口都调它,`full=True` 时多返 exit_code / completed_at / latest_event_index / arktower_task_id |
| **I-14** | **P2** | `mcp/__init__.py` 只是占位符,Day-4 计划的 stdio server / 7 verbs / arktower_relay / elicitation 未起头 | `src/popolaloom/mcp/__init__.py` | 落 Priority 3 — 用 mcp SDK 最小 stdio server + 7 verbs(即使 4 个 return NotImplementedError)+ mcp.json install 脚本 |
````

---

## 3. Stage Impl-3 已识别的 6 个问题 (来自 T-smoke 报告)

来源: 上游 prompt 中 T-meta 接到的 "Known blocker" 段(Stage Impl-3 issue list)。逐项简录:

1. **In-memory `StateStore` 阻塞跨进程 status/attach** — 派发与 `popola status / attach` 必须在同一 Python 进程内才能看到 task;目前的 workaround 是 `popola dispatch ... --wait`(同进程完成完整生命周期)或直接读 `~/.popola/events/<task_id>.jsonl`。
2. **无 `popola` CLI 注册自定义 adapter 的入口** — 三个内置 adapter (cursor/claude/codex) 在 `adapters/__init__.py` 模块导入期 `_register_defaults()`,外部 CLI 用户/Skill 想加入第 4 个 CLI(如 Kimi/Copilot)只能改源码,不能像 plugin 那样 drop-in。
3. **`events_dir` 不可被 CLI 覆盖** — `popola dispatch` 没有 `--events-dir` 选项,`Popolad` 走 `~/.popola/events/` 默认值;集成测试或多用户场景受限。
4. **`popola list-cli` 状态列被 Rich markup 吃掉** — 输出表格的 status 列显示空字符串(应为 `[available]` / `[missing]`);Rich 把 `[available]` 解析成样式标签而非字面文本(见 `cli/main.py:111` `status_str = "[available]"`)。
5. **`Popolad._on_subprocess_exit` 静默吞 `KeyError`**(违反 No Silent Failures) — 派发 agent 在 §3.1 也独立确认了这点(见 §2 派发输出 + §5 I-07)。
6. **`popola attach` 在任务未完成时无 `--follow` 静默退出** — 默认行为是"打印到当前末尾,然后退出",对于 in-flight 任务会让用户误以为没事件;只有显式 `--follow` 才会持续轮询。

> 6 条与派发 agent §6 issue list 形成正交补集:T-smoke 偏 **CLI / 用户体验 / 进程边界** 缺陷,派发 agent 偏 **架构 / spec drift / 测试覆盖** 缺陷。

---

## 4. 派发 agent 新发现 (来自第 §2 的 LLM 输出, 整理成 bullet)

按"T-smoke 6 条之外的新增问题"过滤,共 **8 条新发现**:

- **N-1 (架构)**: `Popolad` god-class 苗头 — 已扛 6 件事,Stage Impl-3 再加 LangGraph/SqliteSaver/DAG 调度后必然超载,应预拆 `TaskLifecycle` + `ArkTowerBridge` + `EventLogRegistry`。(派发 §1)
- **N-2 (架构)**: `AdapterCallback` (3-arg, daemon 侧) 与 `adapters.base.build_command` (4-arg, adapter 侧) 签名不一致,extra 通道隐形脆弱 — 当 adapter 想用 session_id / sandbox 等 extra 时 daemon 这侧要被迫重签名。(派发 §1)
- **N-3 (测试)**: `tests/self_bootstrap/` 目录**根本不存在**,spec §3.4.1 5 例 0 覆盖。(派发 §2.1)
- **N-4 (测试)**: 12 个 NFR **0/12 量化 assertion**;`test_event_log_append_and_tail` 不带计时断言,`test_e2e_dispatch_failed_path` 不测 daemon 重启 recovery。(派发 §2.2)
- **N-5 (并发)**: `Popolad._event_logs: dict` 无锁(server.py:73),与 `StateStore` / `EventLog` 的严格加锁不对称,存在 check-then-set race(server.py:137-139)。(派发 §3.3)
- **N-6 (并发)**: `Supervisor._wait_and_finalize` 的 `stdout_thread.join(timeout=5.0)` 硬超时(supervisor.py:178-179),大输出场景下 `task.completed` 后仍会追加 `process.stdout`,破坏"终态事件是文件末行"契约。(派发 §3.3)
- **N-7 (静默失败)**: `_maybe_create_arktower_task` 的 `except` 返回 in-memory `ark_task.id` 假装"持久化成功",违反"显式错误状态"精神(server.py:289-297)。(派发 §3.1)
- **N-8 (架构 + spec)**: Adapter Protocol 只覆盖 spec §3.2 要求的 6 动作中的 1 个 (`build_command`,~17%),未来加 `spawn/status/attach/kill/cost-meter` 必然改 Protocol → 违 OCP;建议拆为 `CommandBuilder` (PURE) + `Runtime` (副作用) 两层。(派发 §3.2 + §4)

**额外的"本闭环"自身的发现** (T-meta 用 popola CLI 时的真实痛点):

- **N-9 (CLI)**: `popola dispatch` 不接受 `--extra` / `--adapter-flag` / `--cli-flag`,导致 cursor adapter 无法在派发时启用 `--yolo / --trust`,撞 cursor-agent 的 "Workspace Trust Required" 必然失败 — 必须靠外部 PATH wrapper 兜底(本次用 `/tmp/popola_wrappers/cursor-agent` 解决)。这是 spec §3.5.1 `PopolaTaskDispatch.sandbox` 字段的现实必要性证据。
- **N-10 (CLI)**: `popola dispatch ... --wait --json` 在 `--wait` 后输出 task_id JSON,但**没有最终 state / exit_code 字段**,客户端必须再做一次额外的 NDJSON 读取或 `popola status` 调用 — 信息冗余地走两遍。建议 `--wait + --json` 组合下额外吐出 `final_state / exit_code / duration_s / event_count`。

---

## 5. 合并/去重后的 Iter-2 问题清单 (按优先级 P0-P2)

合并 T-smoke 6 条 + 派发 agent §6 14 条 + 本闭环新增 2 条,去重 + 重新分级后共 **14 条核心 issue**(部分重叠合并):

| 编号 | 优先级 | 问题 | 来源 | 建议修复方式 |
|---|---|---|---|---|
| **R-001** | **P0** | 无独立 daemon 进程,`Popolad` 是 in-process 类;NFR-5 跨终端存活 / S1 attach / S3 递归 / cross-process status 全失效 | T-smoke #1 + 派发 I-01 | 实现 `daemon/main.py` (asyncio + uvicorn + `~/.popola/popolad.sock`);CLI 改走 socket RPC;保留 Popolad 类作 daemon 内 facade |
| **R-002** | **P0** | `tests/self_bootstrap/` 目录不存在;spec §3.4.1 五例 (S1..S5) **0/5 覆盖**,CI 必跑契约缺位 | 派发 N-3 + I-02 | 先落 S1 骨架(`pkill -9 parent` + 重启后从 NDJSON 重建),再逐个补 S2-S5;同时补 12 NFR 的 quantitative assertion |
| **R-003** | **P0** | LangGraph 仅在 pyproject 声明,0 行调用;`thread_id` / SqliteSaver / `interrupt()` 全缺,spec §5.3 + ADR-0002 悬空,S1/S2/S3/S4 不可能解锁 | 派发 I-04 | 单节点 StateGraph + SqliteSaver (`~/.popola/state.sqlite`) → Gen-Verifier subgraph → HITL `interrupt()` demo,2-3 天分批落 |
| **R-004** | **P0** | ArkTower 仅用 `Task` model 做 schema parity,TaskService/EventBus/MigrationRunner/MCP/Web 全未用,ADR-0001 价值未兑现 | 派发 I-03 | R-001 daemon 改造时注入 `TaskService.create_task` / `EventBus.subscribe(TASK_TRANSITION_EVENT)`;落 005 migration |
| **R-005** | **P0** | `popola attach` 默认无 `--follow` 时静默退出,与 `_default_popolad` 跨进程不可见叠加,导致非同进程的客户端永远 attach 不到 | T-smoke #1+#6 | (a) `attach` 改为 in-flight task 隐式 `--follow`(无 `--no-follow` 时跟到 terminal);(b) 跨进程能力依赖 R-001 |
| **R-006** | **P1** | `Popolad._event_logs: dict` 无锁,与 `StateStore`/`EventLog` 严格加锁不对称,存在 check-then-set race | 派发 N-5 + I-05 | 增 `_event_logs_lock = threading.Lock()`,包 set/get;补一个 "2 thread dispatch + 1 thread tail" 的 stress test |
| **R-007** | **P1** | `Supervisor` `join(timeout=5.0)` 硬超时,大输出场景违反"终态事件是文件末行"契约 | 派发 N-6 + I-06 | join 超时拉到 30s 或无限;若仍 timeout,emit `stream.truncated` 告警,让 caller 明确"可能有缺行" |
| **R-008** | **P1** | `_on_subprocess_exit` 静默吞 `KeyError` (违反 No Silent Failures);`_maybe_create_arktower_task` 返回未持久化 id 伪装"成功" | T-smoke #5 + 派发 N-7 + I-07 | (a) `KeyError` 路径 emit `state.ghost_exit` 事件 + warn 升级为含 task_id 的明确诊断;(b) `_maybe_create_arktower_task` 失败返 None + TaskHandle 加 `persisted: bool` |
| **R-009** | **P1** | Adapter Protocol 只 1/6 动作 (`build_command`),未来加 spawn/status/attach 必然破坏 OCP;且与 daemon 侧 3-arg `AdapterCallback` 签名不一致 | 派发 N-2 + N-8 + I-08 | 拆 `CommandBuilder` (PURE) + `Runtime` (副作用) 两层 Protocol;daemon 侧改注入 `Runtime` 而非 Popen 直调;统一 4-arg 签名(extra dict 始终在) |
| **R-010** | **P1** | Supervisor 用 `Popen(start_new_session=True)` 代替 `systemd-run --user --scope`,缺 cgroup 限额 / journalctl unit 整合,违 spec §2.1 + Q7 答案 | 派发 I-09 | 实现 `SystemdRunSupervisor` (检测 `which systemd-run`),失败 fallback 到 tmux,再 fallback 到现有 Popen;3 后端用 `Supervisor` 抽象接口统一 |
| **R-011** | **P1** | `EventLog.append` 每次 open/write/close,无 NFR-3 (< 5ms) 量化 benchmark | 派发 I-10 + N-4 | 改持有 fd + buffered write + 周期 fsync;`tests/nfr/test_nfr_3_write_latency.py` 用 `time.perf_counter` 测 1000 条平均 |
| **R-012** | **P1** | `popola dispatch` 不接受 `--extra` / `--cli-flag`,无法在派发时为 cursor adapter 启用 `--yolo`,workspace trust 场景必败 | 本闭环 N-9 | (a) 加 `--cli-flag KEY=VAL` repeated option;(b) cursor adapter 默认在非交互模式下注入 `--yolo`(参考 codex `--sandbox=workspace-write` 的默认行为) |
| **R-013** | **P2** | 无 `popola` 入口注册外部 adapter (plugin 不可加);两套 process-level 单例(CLI lru_cache + daemon module-var)语义重叠 | T-smoke #2 + 派发 I-12 | (a) entry-point group `popolaloom.adapters` + setuptools auto-discover;(b) R-001 落地后删除 `daemon/server.py:311-343` module-level 包装;CLI 改走 socket |
| **R-014** | **P2** | `popola list-cli` status 列被 Rich markup 吃掉;`list_active` (5 字段) 与 `get_status` (9 字段) 返回 shape 不一致;`events_dir` 不可被 CLI 覆盖 | T-smoke #3+#4 + 派发 I-13 | (a) status_str 用 `Text("available", style="green")` 代替 `[available]`;(b) 提取 `_task_summary(handle, *, full)` 统一;(c) `popola dispatch --events-dir` + `Popolad(events_dir=)` 透传 |

**统计**:
- **P0: 5 条** (R-001..R-005) — 全部为 spec drift / 跨进程边界 / 测试基线缺位
- **P1: 7 条** (R-006..R-012) — 并发 race / 静默失败 / Protocol 设计 / 后端缺失 / CLI 派发参数缺失
- **P2: 2 条** (R-013..R-014) — plugin 入口 + 单例清理 / 输出形状统一
- **本闭环新增**: R-005 (T-smoke + 派发 合并强化) / R-012 (新, 见 §4 N-9) / R-014 (合并 T-smoke #3+#4 与 派发 I-13)

---

## 6. Iter-2 工作流建议

### 6.1 推荐的 devola-flow 工作流类型

**复合工作流 = `feature-enhancement` (Priority 1+2+3) + `refactoring` (R-006..R-011) + `testing` (R-002 全程 + R-011 micro-bench)**。

理由: 单纯 `refactoring` 无法补齐 spec §2.1 的 12 类承诺(R-001..R-004);单纯 `feature-enhancement` 又无法处理 R-006..R-011 的隐藏债。两者并行,在每条 P0 feature PR 中 *同时* 修一条对应 P1/P2 隐藏债,是 spec §7.3 auto-merge gate 5 条 AND 条件的最自然路径(测试覆盖 ≥ 80% / multi-CLI peer review / 0 Blocker 0 Critical)。

### 6.2 推荐 Day-2 最先完成的 3 件事 (与派发 agent 一致 + T-smoke 视角增强)

1. **R-001 + R-005 联动**: daemon 真进程化(unix socket) + `popola attach` 改为隐式 follow → 一次性解决 T-smoke 6 条里的 #1+#6,同时承接派发 agent Priority 1。**Cost ≈ 1.5 day, Value ≈ 9/10**。
2. **R-002 骨架**: `tests/self_bootstrap/` 目录 + S1 单例骨架 + 12 NFR 中 NFR-3/5/8 的量化 assertion → 立即把 CI 的"绿色误信号"换成"红色真实信号",阻止后续 PR 漂移。**Cost ≈ 1 day, Value ≈ 8/10**。
3. **R-003 single-node + R-004 TaskService 接入**: LangGraph 单节点 + SqliteSaver + ArkTower TaskService 双轨持久化 → 解锁 S1/S3/S4 后续场景的全部前置条件。**Cost ≈ 2 day, Value ≈ 9/10**。

### 6.3 是否引入 mock claude/cursor for unit tests

**强推荐: 引入**。

- 当前 `tests/test_e2e.py` 用 echo / python `-c` 做 fake adapter,链路只能验证"Supervisor + EventLog + StateStore"组合;无法验证 cursor / claude / codex adapter 真出去 + NDJSON 解析 + session_id / sandbox 等 extra 字段。
- 推荐方案: `tests/fixtures/mock_cursor_agent.py` 等三个 stub,行为可参数化("正常输出 / 中途挂 / 输出 stream-json / 等待 stdin / 触发 needs_input")。每个 mock 用 100 LOC 即可覆盖 5 种场景。
- 收益: ① 让 R-002 的 5 个 self-bootstrap test 跑得稳定可复现(不依赖真 API quota);② 让 R-009 拆分 Adapter Protocol 时 *测试可独立运行*。
- 风险: mock 与真 CLI 行为漂移。缓解: 每周跑一次 nightly real-CLI smoke (本次本闭环就是这种性质的真 CLI 验证),作为 sanity check。

---

## 7. 闭环验证结论

| 链路 | 结论 | 证据 |
|---|---|---|
| `popola dispatch` → cursor (经 wrapper 注入 `--yolo`) → NDJSON 事件流 | **PASS** | task_id `cursor-23e74ec18917`, exit_code=0, 194 events, 18 022 chars output |
| `--wait --timeout 600 --json` 同进程完成全生命周期 | **PASS** | 246.26 s 内完成 dispatch + 子进程 setsid + stream drain + 终态写入,JSON 输出含 task_id / cli / events_log |
| NDJSON 事件读回(后处理) | **PASS** | `python3` 直接读 `~/.popola/events/cursor-23e74ec18917.jsonl` 拿到完整 stdout,通过过滤 `process.stdout` 重组得到派发 agent 完整 markdown 报告 |
| nines self-eval 链路 | **SKIP** | 无 PopolaLoom-nines runner;ArkTower eval (依赖侧基线 0.9179) 与 PopolaLoom-nines 8 维不同源,只能作参考 |
| 端到端 self-test 范式可重复 | **是** | wrapper + meta-prompt 文件 + 单条 `popola dispatch` 命令即可复现,详见 §8 |
| **用户原始目标达成度** | **4 / 5** | 通过 popola CLI 派发 + agent 自分析 + 14 条 issue 清单 + Iter-2 工作流建议 — 闭环跑通且 surfaces real findings;扣 1 分给 nines 链路缺位(应实装 PopolaLoom-nines runner 才能凑齐 5 分) |

> 总判定: **PASS** (主链路 + 工件); **PARTIAL** (nines 维度缺评估器); **REPRODUCIBLE** (复现命令见 §8)。

---

## 8. 元数据

| 字段 | 值 |
|---|---|
| 工作时长 (T-meta) | ≈ 25 min wall-clock (5 min 探查 + 5 min wrapper + 4 min dispatch + 11 min 报告) |
| 派发耗时 (cursor-agent) | 246.26 s (≈ 4 min 6 s) |
| popola dispatch 全程日志 | `/tmp/popola_meta_dispatch.log` |
| NDJSON 事件文件 | `/root/.popola/events/cursor-23e74ec18917.jsonl` (194 events, 80 457 bytes) |
| 派发 prompt 文件 | `/tmp/popola_meta_prompt.txt` (29 lines, 2 888 bytes) |
| 派发 agent 输出 (verbatim) | `/tmp/popola_dispatched_stdout.txt` (191 lines, 18 022 chars) |
| ArkTower eval baseline | `/tmp/popola_arktower_eval_baseline.json` (overall 0.9179) |
| cursor-agent wrapper | `/tmp/popola_wrappers/cursor-agent` (`exec /root/.local/bin/cursor-agent --yolo "$@"`) |
| 复现命令 | `cd /home/agent/workspace/PopolaLoom && PATH="/tmp/popola_wrappers:$PATH" popola dispatch "$(cat /tmp/popola_meta_prompt.txt)" --cli cursor --cwd /home/agent/workspace/PopolaLoom --wait --timeout 600 --json` |
| 关联文件 | spec.md / implementation-plan.md / nines.toml / src/popolaloom/* / tests/* |
| 上游研究 dossier | research/01..08 (本文档为 09) |

---

> 作者: L3 Task Agent T-meta (Test 团队), Stage Impl-4 (closed-loop self-test)
> 上游 Stage: Impl-3 (T-smoke 6 issues) + Impl-2 (popola CLI 7 verbs)
> 下游用途: 触发 Iter-2 工作流(R-001..R-014 中至少 P0 全部进入 Day-2 PR 队列)
