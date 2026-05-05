# PopolaLoom · 5 档测试矩阵规格 (Testing Matrix Spec)

> 状态: 🔒 v0.2.0 设计期产出, 待 v0.2.1 实施前用户书面 ack
> 综合源: roadmap §3.1 (5 档 spec) + §3.2/§3.3/§3.4 (各 patch DoD) + §11 (devola-flow 双层 gate) + §12 (HITL handle-ability) + §12.8 (Lark 双向通道) + v0.2.0-plan.md §4 (5 Stage Owned files) + spec.md §3.4.1 (S1-S5 五例) + spec.md §6 (NFR-1..12)
> 输出日期: 2026-05-04
> 作者: L3 Task Agent T-test-matrix (Design 团队), devola-flow design-only workflow
> 关联: [roadmap](/root/.cursor/plans/popolaloom_v0.2-v0.4_roadmap_e3d38a10.plan.md) · [v0.2.0-plan.md](.local/memory/specs/popolaloom/v0.2.0-plan.md) · [spec.md](.local/memory/specs/popolaloom/spec.md)
> 适用版本范围: v0.2.0 → v0.4.0 (五个 patch + 自演化 5 round + 一个 GA)
> 配套 pyproject.toml 改动: [§6](#6-coverage-gates-by-version) + [§7](#7-property-based-testing-strategy-hypothesis) + [§8](#8-snapshot-testing-strategy-syrupy)

---

## 0. TL;DR (一页摘要)

### 0.1 5 档难度命名 + 一句话定义

| Tier | 名称 | 一句话定义 | pytest mark | 默认 CI 跑? |
|------|------|------------|-------------|--------------|
| **T1** | Simple (单元级) | 单函数 / 单文件 / 无 IO / 无 subprocess; 纯逻辑分支 + 边界覆盖 | (无 mark, 默认) | ✅ 默认跑 |
| **T2** | Medium (集成级) | 2-3 模块协作 + mock subprocess + tmp_path 隔离 IO | (无 mark, 默认) | ✅ 默认跑 |
| **T3** | Hard (跨进程 / NFR 量化) | 真 daemon + UDS RPC + 真 SQLite + 跨进程 status; 含 NFR-3/5/8 量化基线 | `@pytest.mark.slow` | ❌ slow lane (周一次) |
| **T4** | Structured (DAG / langgraph 真子图) | 真 langgraph subgraph 多节点 + interrupt() + thread_id 隔离 + checkpointer round-trip | `@pytest.mark.slow @pytest.mark.real_graph` | ❌ slow lane (周一次) |
| **T5** | Project (端到端自演化) | 整闭环跑一次完整自演化轮 (mock CLI 不打真 LLM 但走真 dispatch + 真持久化 + 真 nines) | `@pytest.mark.e2e @pytest.mark.nightly` | ❌ nightly lane |

### 0.2 总用例 + 覆盖率版本目标 (来源: roadmap §3 + §6)

| 版本 | 总测试 | 行覆盖 | 新增 tier | 新增数 | 备注 |
|------|--------|--------|-----------|--------|------|
| v0.0.1 (现状) | 18 | 未报 | 仅 Simple-equivalent | — | conftest + test_smoke + test_adapters + test_daemon + test_e2e |
| **v0.2.0** | ≥ 50 | ≥ 75% | T1+T2 雏形 (旧 18 + 新 32) | +32 | Stage A-E 各自补; 加 NFR-3 + 1 个 self-bootstrap 占位 |
| **v0.2.1** | ≥ 110 | ≥ 80% | T1 (35) + T2 (25) | +60 | hypothesis property 落地 (≥ 5 property); parametrized adapter 矩阵 45 case |
| **v0.2.2** | ≥ 160 | ≥ 85% | T3 (20) + NFR (8) + chaos (12) + real_cli smoke (10) | +50 | NFR-1/3/5/8 4 条全量化达标; 12 chaos failure mode 全 emit 正确事件 |
| **v0.2.3** | ≥ 200 | ≥ 90% | T4 (18) + T5 (8) + S1-S5 mock (5) + HITL/devola-flow schema (9) | +40 | langgraph 真子图 ≥ 8; mock CLI 三件套 + devola-flow 三段输出契约 |
| **v0.3.0** | ≥ 250 | ≥ 90% (不降) | T3+T4+T5 真版填充 (HITL 全栈 26 + Lark 25 + devola-flow 双 gate 6) | +50 | F1 真 nines + F2 三原语 + F2.5 双 gate + F4 HITL 全栈 + F5 S2/S4/S5 真版 |
| **v0.3.1..v0.3.5** | ≥ 250 + 每轮 ≥ 1 (per fixed issue) | ≥ 90% (不降) | 自演化每轮 +1 testing per closed R-XXX | +5..10 (5 round) | 每个 sub-task 必带 1 红→绿 测试; mutmut Phase 2 引入 |
| **v0.4.0 (GA)** | ≥ 350 | ≥ 92% | 兜底补差; mutation testing (Phase 2 准入) | +50..100 | 8 维 nines ≥ 0.95; 5/5 真 self-bootstrap 3 连 PASS |

### 0.3 marker 一览 + 选择性运行 cheat-sheet

| Mark | 含义 | 默认 CI? | 何时跑 |
|------|------|----------|--------|
| (none) | T1 + T2 (单元级 + 集成级) | ✅ | PR 必跑, push 必跑 |
| `@pytest.mark.slow` | T3 跨进程 + NFR + chaos | ❌ (skip) | 周一 cron + 每发版前 |
| `@pytest.mark.real_graph` | T4 langgraph 真子图 (隐含 slow) | ❌ (skip) | 同上 |
| `@pytest.mark.e2e` | T5 端到端项目级 | ❌ (skip) | nightly + release-gate |
| `@pytest.mark.nightly` | nightly cron (T5 主体) | ❌ (skip) | 每晚 03:00 |
| `@pytest.mark.real_cli` | 需要真 cursor-agent / claude / codex 二进制 | ❌ (skip) | 周一 cron + 本地开发可选 |
| `@pytest.mark.real_lark` | 需要真 Lark bot 凭据 (env `LARK_BOT_APP_ID`, `LARK_BOT_APP_SECRET`) | ❌ (skip) | 周一 cron 子集 + release-gate (v0.3.0+) |

选择性运行:

```bash
pytest                                            # 默认: T1 + T2 (~50-200 case, < 1min)
pytest tests/matrix/tier1                         # 只 Simple
pytest -m "not slow and not e2e"                  # T1 + T2 (默认 = 等价)
pytest -m slow                                    # T3 + T4
pytest -m "slow and not real_cli and not real_lark"  # T3 + T4 不带真二进制
pytest -m e2e                                     # T5 端到端 (含 nightly)
pytest -m nightly                                 # nightly 子集
pytest -m real_cli                                # 真 CLI smoke (需本机装)
pytest -m real_lark                               # 真 Lark e2e (需 bot 凭据)
pytest -m "slow or e2e"                           # 周一 cron 全量
pytest --benchmark-only                           # 只跑 NFR pytest-benchmark
```

---

## 1. 5-tier 详细规格

### 1.1 Tier 1 · Simple (单元级, 默认)

#### 描述 + 范围边界

- **覆盖**: 单函数 / 单文件 / 无 IO / 无 subprocess; 覆盖纯逻辑分支 + 边界 (None / empty / large / Unicode / negative); 包含 Pydantic schema 校验 + 工厂函数 + 解析器 + 序列化器 + 路径生成器 + utility 模块
- **不覆盖**: 任何启动子进程的代码 (那是 T2+); 任何写文件 (除非用 `monkeypatch` 把文件系统 mock 掉); 任何 langgraph 实例 (T4 才用真子图; T1 仅校验 state schema 字段)
- **作用边界**: 做 "单元正确性" 与 "schema 不变量" 验证, 不验证集成行为

#### 用例数目标 (lower / upper bounds)

- **下限**: 50 case (v0.2.1 DoD 入门门槛 — Tier 1 +35 ≈ baseline 18 → 53)
- **上限**: 80 case (v0.2.3 时占总数 200 中 ~40%, 防止 Tier 1 膨胀稀释 T2-T5)
- **v0.4.0 final 占比**: 约 30-35% (≥ 350 中 ~120 case)

#### 速度上限

- **per case**: < 100 ms (95-pct, 通常 < 20ms; hypothesis property 含 100 example 默认放宽到 ≤ 200ms)
- **全套**: < 8 s (Tier 1 单独 `pytest tests/matrix/tier1` < 8s, 与 v0.0.1 全套 0.5s 同量级 + 留 16x buffer 给 hypothesis fuzzing)

#### pytest markers

- 无 mark (默认 lane); 不带 `slow` / `e2e` / `nightly` / `real_*`
- hypothesis 测试可选加 `@pytest.mark.hypothesis_max_examples_500` 自定义 mark (CI 默认 100, nightly 拉到 500)

#### 工具栈

- pytest 8.x + pytest-asyncio (asyncio_mode=auto, 即使 T1 大多 sync)
- **hypothesis ≥ 6.100** (property-based testing — TaskState FSM 状态迁移 / NDJSON envelope schema 不变量 / HITLPrompt schema 必填字段)
- **pytest-mock ≥ 3.12** (`mocker` fixture, monkeypatch 升级)
- pytest-cov (line + branch coverage)
- 不需要 responses / freezegun (那些是 T2 工具)

#### 覆盖目标 (该 Tier MUST 覆盖的代码)

- `src/popolaloom/adapters/*.py` (build_command 的所有分支 + extra 所有 key + ValueError 路径) — branch ≥ 90%
- `src/popolaloom/daemon/state.py` (TaskHandle / StateStore / TaskState 状态机) — line ≥ 95%
- `src/popolaloom/daemon/event_log.py` (CloudEvents envelope 字段生成 + tail 索引) — line ≥ 90% (实际 IO 走 T2 mock)
- `src/popolaloom/hitl/prompt.py` (HITLPrompt + HITLOption + ArtifactRef Pydantic schema, v0.2.3 占位 + v0.3.0 完整) — line ≥ 100%
- `src/popolaloom/evolution/reinforcement.py` (top-5 finding 渲染 string template) — line ≥ 95%
- `src/popolaloom/evaluation/popola_dimensions.py` (8 维 score 计算 PURE 函数) — line ≥ 90%

#### 例子 (代表性测试名)

- `tests/matrix/tier1/test_taskstate_fsm.py::test_pending_can_transition_to_running_only_via_register` — 状态机迁移合法性
- `tests/matrix/tier1/test_event_envelope_schema.py::test_envelope_specversion_always_1_0_for_any_input` — hypothesis property: 任意 (type, data) 都生成 specversion=1.0 的合法信封
- `tests/matrix/tier1/test_hitl_prompt_schema.py::test_options_must_be_at_least_two` — HITLPrompt.options ≥ 2 否则 ValidationError (per §12.2)
- `tests/matrix/tier1/test_devolaflow_context_schema.py::test_workflow_context_includes_all_required_fields` — Workflow Context prepend 必含 round_num / max_rounds / prior_nines / reinforcement_rules / gate_threshold (per §11.2)
- `tests/matrix/tier1/test_lark_card_template.py::test_card_footer_always_contains_source_attribution_marker` — Lark 卡片末尾 footer 强制含 `本消息由飞书工具 Lark-Cli 发送` (per §12.8.1 + 工作区规则)

---

### 1.2 Tier 2 · Medium (集成级, 默认)

#### 描述 + 范围边界

- **覆盖**: 2-3 模块协作; mock subprocess (用 `pytest-mock` 替 Popen, 不真 fork); tmp_path 隔离 IO; 含 HTTP mock (responses) + 时间冻结 (freezegun); CLI httpx → daemon RPC 的 mock 链路
- **不覆盖**: 真 daemon 启动 (T3); 真 langgraph subgraph 调度 (T4); 真自演化轮 (T5); 真 cursor/claude/codex 二进制 (`@pytest.mark.real_cli`)
- **作用边界**: 做 "集成路径" 与 "错误路径分支" 验证, 不做 "跨进程行为" 与 "并发 race" 验证

#### 用例数目标

- **下限**: 30 case (v0.2.1 DoD 入门门槛 — Tier 2 +25)
- **上限**: 60 case (v0.2.3 时占总数 200 中 ~30%)
- **v0.4.0 final 占比**: 约 25-30% (≥ 350 中 ~95 case)

#### 速度上限

- **per case**: 100ms - 1 s (含 mock subprocess wait、freezegun 时间步进、responses HTTP mock 序列化)
- **全套**: < 60 s (Tier 1 + Tier 2 合并 `pytest -m "not slow and not e2e"` < 75s, 容忍 GC 抖动)

#### pytest markers

- 无 mark (默认 lane)
- 个别 e.g. `@pytest.mark.timeout(2.0)` 防止单 case hang (pytest-timeout, optional)

#### 工具栈

- pytest + pytest-asyncio + pytest-mock
- **responses ≥ 0.25** (HTTP request mock — CLI httpx → daemon RPC 7 endpoint round-trip + Lark API mock 雏形)
- **freezegun ≥ 1.4** (时间冻结 — deadline 超时模拟 + S4 8h offline mock + reinforcement TTL)
- pytest-cov (line + branch)
- 不需要 hypothesis (T2 主走 parametrized 矩阵; hypothesis 是 T1 主战场)

#### 覆盖目标

- `src/popolaloom/cli/main.py` (Typer CliRunner 路径 — dispatch / status / attach / list / list-cli; --json / --wait / --timeout) — line ≥ 85%
- `src/popolaloom/daemon/server.py` (Popolad facade 主流程 + 错误路径; mock 掉 Supervisor.spawn) — line ≥ 80%
- `src/popolaloom/daemon/supervisor.py` (Supervisor.spawn / join / 错误码映射 — SIGKILL / SIGTERM / OOM 137 / cwd 不存在 / binary 不存在) — branch ≥ 85%
- `src/popolaloom/daemon/rpc.py` (FastAPI app + 7 endpoint; mock UDS transport) — line ≥ 85%
- `src/popolaloom/lark/card_templates.py` (Lark interactive card json 渲染 — header 颜色 5 trigger × button count 2/3/5 = 15 case) — line ≥ 90%
- `src/popolaloom/lark/listener.py` 路由层 (NDJSON event 路由 — button click / text command / 未匹配 / 重复 event_id 4 case; subprocess 不真启) — line ≥ 80%
- `src/popolaloom/hitl/renderers/*.py` (5 通道 renderer 的 schema → 渲染对象 PURE 函数 + parse_reply 反向; 每个 renderer ≥ 2 case) — line ≥ 85%

#### 例子

- `tests/matrix/tier2/test_dispatch_chain.py::test_dispatch_supervisor_event_log_chain_emits_in_order` — dispatch → supervisor.spawn → event_log NDJSON 事件序列断言 (mock subprocess)
- `tests/matrix/tier2/test_cli_httpx_round_trip.py::test_status_endpoint_returns_pending_after_dispatch` — CLI httpx → daemon RPC 7 endpoint mock 链路 (responses 库)
- `tests/matrix/tier2/test_lark_event_router.py::test_card_action_trigger_routed_to_hitl_answer` — mock NDJSON `card.action.trigger_v1` event → 路由到 `popolad RPC /hitl/answer` (per §12.8.2)
- `tests/matrix/tier2/test_freezegun_deadline.py::test_hitl_deadline_24h_expires_to_default_option` — freezegun 模拟时间跳到 deadline 后, HITL 自动选 default_option (per §12.4)

---

### 1.3 Tier 3 · Hard (跨进程 / NFR 量化, slow)

#### 描述 + 范围边界

- **覆盖**: 真 popolad subprocess 启动 (asyncio + uvicorn UDS 真服务) + 真 SQLite (ArkTower DB + LangGraph SqliteSaver) + 跨进程 status (终端 A dispatch + 终端 B attach 用 2 个 `subprocess.Popen` 模拟) + NFR-3/5/8 pytest-benchmark 量化基线 + chaos 12 故障注入路径
- **不覆盖**: langgraph 真子图多节点协调 (T4); mock CLI 库的 devola-flow 三段输出 (T4); 端到端自演化整轮 (T5)
- **作用边界**: 做 "跨进程行为" 与 "并发 race" 与 "性能基线" 验证; 不验证 graph DAG 收敛逻辑

#### 用例数目标

- **下限**: 15 case (v0.2.2 DoD 入门 — Tier 3 +20 = 真 daemon 20 + NFR 8 + chaos 12 + real_cli smoke 10; 这里 Tier 3 主体 ≈ 20)
- **上限**: 30 case (v0.4.0 极限, 防止 slow lane 跑超 5min)
- **v0.4.0 final 占比**: 约 15-20% (≥ 350 中 ~60 case, 含 NFR + chaos + real_cli)

#### 速度上限

- **per case**: 1 s - 10 s (含 daemon 启动 + SQLite migrate + 1-2 个 dispatch + cleanup); chaos 注入路径 ≤ 3s
- **全套**: < 5 min (T3 单独 `pytest tests/matrix/tier3 -m slow` < 5min, 含 20 daemon 启动 × 3s + NFR-3 1000-iter benchmark)

#### pytest markers

- `@pytest.mark.slow` (默认 CI 跳过)
- 量化 NFR 测试加 `@pytest.mark.benchmark` (pytest-benchmark 自动收集)
- chaos 子集加 `@pytest.mark.chaos` (可选 fine-grained)

#### 工具栈

- pytest + pytest-asyncio + pytest-mock
- **pytest-benchmark ≥ 4.0** (NFR 量化 — `benchmark(callable)` micro-benchmark + `--benchmark-min-rounds=5 --benchmark-warmup=on` 默认配置; 输出 JSON 入 `benchmarks/` 目录用于版本间对比)
- subprocess + httpx (UDS transport — `httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=...))`)
- 自定义 fixture: `real_popolad` (启 daemon + tmp_path UDS + tmp_path SQLite + 测试结束 SIGTERM 清理) — 见 [§5](#5-fixture-公共契约)

#### 覆盖目标

- `src/popolaloom/daemon/main.py` (asyncio.run + uvicorn config + signal handler SIGTERM/SIGINT + PID 文件) — line ≥ 80%
- `src/popolaloom/daemon/repository.py` (ArkTower TaskService 真注入路径; rehydrate from SQLite) — line ≥ 85%
- `src/popolaloom/daemon/event_bus.py` (订阅 TASK_TRANSITION_EVENT + 跨进程 NDJSON 写入) — line ≥ 80%
- 跨进程 attach 流式: `attach_stream/{task_id}` SSE endpoint 真启 + 客户端读 NDJSON 切片 — line ≥ 85%
- chaos 路径: 12 故障模式见 [§10](#10-chaos--fault-injection); 每条对应 `tests/matrix/chaos/test_chaos_*.py` 一个 file

#### 例子

- `tests/matrix/tier3/test_real_popolad_dispatch.py::test_dispatch_via_uds_real_daemon` — 真启 popolad + httpx UDS POST /dispatch → 真 spawn echo subprocess → 真 ArkTower SQLite insert + 真 NDJSON write
- `tests/matrix/tier3/nfr/test_nfr_3_event_log_latency.py::test_ndjson_append_avg_under_5ms_for_1000_events` — pytest-benchmark 1000 条 NDJSON append 平均 < 5ms (NFR-3)
- `tests/matrix/tier3/nfr/test_nfr_8_recovery_rate.py::test_sigkill_rehydrate_recovery_rate_at_least_95pct_over_20_runs` — 20 次 SIGKILL + restart, 验证 ArkTower SQLite rehydrate 成功率 ≥ 95% (NFR-8)
- `tests/matrix/chaos/test_chaos_arktower_create_task_raises.py::test_arktower_create_task_integrity_error_emits_dispatch_failed_no_silent_swallow` — mock TaskService.create_task raise IntegrityError, 验证 dispatch_failed 事件 emit + 不静默吞异常 (per workspace rule "No Silent Failures")

---

### 1.4 Tier 4 · Structured (DAG / langgraph 真子图, slow + real_graph)

#### 描述 + 范围边界

- **覆盖**: 真 langgraph subgraph 多节点 + interrupt() + thread_id 隔离 + checkpointer round-trip (真 SqliteSaver, 不 mock); HITL interrupt → mock human reply → resume (跨调用 thread_id 持久化); dev↔test Gen-Verifier 收敛 (max_iter=2/5); 递归 dispatch (S3) 父子链; 多 task 并发 thread_id 隔离 (5 个并发); devola-flow 双层 gate 测试 (inner PASS+outer FAIL / inner FAIL+outer PASS / 都 PASS / 都 FAIL 4 组合); HITL 跨通道同步 (Lark + CLI 同时回 1 case)
- **不覆盖**: 真 cursor/claude/codex 二进制 (用 mock CLI 库 — mock_cursor/mock_claude/mock_codex, 见 [§4](#4-mock-cli-agent-library-spec)); 端到端 cycle (T5)
- **作用边界**: 做 "DAG 收敛" 与 "interrupt-resume 持久化" 与 "thread_id 隔离" 验证; mock CLI 输出 devola-flow 三段格式来支持 inner gate 评分逻辑

#### 用例数目标

- **下限**: 10 case (v0.2.3 DoD 入门 — Tier 4 主体 +18)
- **上限**: 25 case (v0.4.0 极限)
- **v0.4.0 final 占比**: 约 8-10% (≥ 350 中 ~30 case)

#### 速度上限

- **per case**: 10 s - 60 s (含 langgraph compile + multi-node ainvoke + SqliteSaver round-trip + interrupt-resume 多步)
- **全套**: < 15 min (T4 单独 `pytest -m real_graph` < 15min)

#### pytest markers

- `@pytest.mark.slow @pytest.mark.real_graph` (双 mark; 默认 CI 跳过)

#### 工具栈

- pytest + pytest-asyncio + langgraph + langgraph-checkpoint-sqlite + 真 SqliteSaver (`SqliteSaver.from_conn_string(":memory:")` 测试默认; 跨 case 持久化测试用 tmp_path file)
- mock CLI agent fixture (`mock_cursor` / `mock_claude` / `mock_codex` — 见 §4 + §5)
- **syrupy ≥ 4.7** (snapshot — graph DAG 输出 / 节点访问序列 / final state JSON; 见 §8)

#### 覆盖目标

- `src/popolaloom/daemon/graph.py` (StateGraph 节点 dispatch / spawn / wait / emit_terminal + conditional_edges) — line ≥ 85%
- `src/popolaloom/daemon/checkpoint.py` (SqliteSaver wrapper + thread_id 隔离) — line ≥ 90%
- `src/popolaloom/daemon/subgraph_dev_test.py` (Gen-Verifier 子图 dev / test / verifier + max_iter / score gate) — line ≥ 85%
- `src/popolaloom/daemon/interrupt.py` (HITL interrupt() 节点 + Command(resume=...) 续跑) — line ≥ 90%
- `src/popolaloom/evolution/dual_gate.py` (parse L3 输出三段 → composite_score + outer/inner 决策) — line ≥ 85%
- `src/popolaloom/evolution/skill_inject.py` (Workflow Context prepend + reinforcement_rules 拼接) — line ≥ 80%

#### 例子

- `tests/matrix/tier4/test_subgraph_gen_verifier_convergence.py::test_dev_test_loop_converges_in_two_iterations_when_score_above_gate` — Gen-Verifier 子图 mock dev/test 节点序列 (score=0.7 → 0.92), max_iter=5, 验证 iter=2 时收敛
- `tests/matrix/tier4/test_interrupt_resume_thread_id_persistence.py::test_human_reply_yes_resumes_from_checkpoint` — graph interrupt() → SqliteSaver 持久化 → 新 connection mock human reply ("yes") → resume → 验证 thread_id 跨调用一致
- `tests/matrix/tier4/test_dual_gate_logic.py::test_inner_pass_outer_fail_triggers_round_rollback` — mock 全部 sub-task inner ≥ 0.85 但 outer nines delta < 0.02 → 验证 round_rollback path (per §11.2)
- `tests/matrix/tier4/test_devolaflow_skill_injection.py::test_mock_cursor_receives_workflow_context_prepend_with_round_n` — mock_cursor dispatch 时 prompt 含 `## Workflow Context (devola-flow)` + round_num + reinforcement_rules
- `tests/matrix/tier4/test_lark_full_roundtrip.py::test_graph_interrupt_lark_card_button_click_resumes_thread` — graph interrupt → Lark renderer 出站 card → mock `card.action.trigger_v1` 入站 → graph resume → 验证 thread_id checkpoint 一致 (per §12.8.5 + §12.5 Tier 4)

---

### 1.5 Tier 5 · Project (端到端自演化, e2e + nightly)

#### 描述 + 范围边界

- **覆盖**: 整闭环跑一次完整自演化轮 (mock CLI 不打真 LLM 但走真 dispatch + 真持久化 + 真 nines 8 维评分); 5 个 self-bootstrap 完整跑 (S1 + S2 + S3 + S4 + S5, mock 版); HITL 触底升级 round_floor 三选项; HITL 24h timeout 自动 default; v0.3.0+ Lark 真 e2e (`@pytest.mark.real_lark`, 用真 lark-cli + 真 bot)
- **不覆盖**: 真 LLM 调用 (mock CLI 输出预设 patch / 评分序列, 不消耗 token); 真 GitHub auto-merge (gate 测试用 dry-run mode + mock GH API)
- **作用边界**: 做 "整闭环行为正确性" 与 "端到端 nines 跃迁" 与 "S1-S5 全 PASS" 验证

#### 用例数目标

- **下限**: 5 case (v0.2.3 DoD 入门 — Tier 5 主体 +8 = 端到端 dry-run 5 + S1-S5 mock 全 5 = 实际 8 case 主, 但每条 case 串多场景)
- **上限**: 15 case (v0.4.0 极限; nightly 全套要 ≤ 30min)
- **v0.4.0 final 占比**: 约 3-5% (≥ 350 中 ~12 case, 因 case 重但单 case 价值高)

#### 速度上限

- **per case**: 30 s - 5 min (含真 popolad 启 + 真 dispatch + 真 nines 8 维计算 + 比对)
- **全套**: < 30 min (nightly cron 单 lane 跑完, 含 5 个 self-bootstrap × 3min + 自演化 dry-run 1 round × 5min + Lark real_e2e 子集 × 3min)

#### pytest markers

- `@pytest.mark.e2e @pytest.mark.nightly` (双 mark; 默认 CI 跳过, nightly 跑)
- 真 Lark 子集额外加 `@pytest.mark.real_lark` (env gating, 见 §5)
- 真 CLI 子集额外加 `@pytest.mark.real_cli` (本地开发 + 周一 cron)

#### 工具栈

- pytest + 真 popolad subprocess (复用 `real_popolad` fixture) + mock_cli_agent 库 (见 §4)
- 真 ArkTower SQLite + 真 LangGraph SqliteSaver + 真 nines runner (`popola eval run` 子命令)
- syrupy snapshot — nines.toml diff (版本间对比)
- pytest-benchmark — 闭环 round wallclock 时间趋势 (regression 检测)

#### 覆盖目标

- `src/popolaloom/evaluation/runner.py` (popola eval run 入口 + 8 维 score 聚合) — line ≥ 85%
- `src/popolaloom/evolution/{skill_inject, reinforcement, dual_gate}.py` 端到端组合调用 — integration ≥ 80%
- `src/popolaloom/gate/automerge.py` (5 条 AND 门; v0.3.0+) — line ≥ 80% via mock GH API
- `tests/self_bootstrap/test_s{1..5}_*.py` 自有逻辑 — 每文件 ≥ 1 PASS

#### 例子

- `tests/matrix/tier5/test_self_evo_round_dry_run.py::test_full_round_with_mock_cursor_emits_three_section_output_and_inner_gate_passes` — PopolaLoom 派 mock_cursor "实现 popola list --json" 微特性 (mock 输出: 加 --json flag patch + 跑测试 + 返回 success) → 验证全闭环 dispatch / attach / event log / arktower 持久化 / nines 评分 + dispatch prompt 含 §11.2 Workflow Context 段 + inner gate 解析 mock 输出三段并打分; 不真改文件 (mock 只返回 patch 字符串)
- `tests/self_bootstrap/test_s5_cross_cli_handoff.py::test_cursor_to_claude_to_codex_three_hop_handoff_via_relay_primitive` — mock_cursor → mock_claude → mock_codex 三跳, 每跳 owned_files 契约校验, 全程 trace 完整 (per spec §3.4.1 S5)
- `tests/matrix/tier5/test_hitl_round_floor_escalation.py::test_round_5_failure_escalates_via_three_channels_with_24h_default` — 模拟 round=5 触底 → §12.6 contract 三选项 → 24h 超时自动选 default → 验证后续 cycle 行为 (per §12.5 Tier 5)
- `tests/matrix/tier5/test_lark_real_e2e.py::test_real_bot_sends_card_and_listener_consumes_button_click` (`@pytest.mark.real_lark`) — 真 lark-cli + 真 bot 发卡片到测试用户 chat → fixture 注入 fake event 兜底 (manual interaction skip-ok)

---

## 2. 测试组织规则

### 2.1 目录布局

```
tests/
├── conftest.py                              # 顶层共享: popolad_factory (v0.0.1 已有, 保留)
├── test_smoke.py                            # v0.0.1 既有: __version__ 校验 (Tier 1 等价)
├── test_adapters.py                         # v0.0.1 既有 7 case (Tier 1 等价 — 暂不动, v0.2.1 可选迁入 tier1/)
├── test_daemon.py                           # v0.0.1 既有 4 case (Tier 1+2 等价)
├── test_e2e.py                              # v0.0.1 既有 3 case (Tier 2 等价 — 用 CliRunner)
├── matrix/                                  # 新增 (v0.2.0 Stage A 创建空骨架, v0.2.1 起填充)
│   ├── conftest.py                          # 跨 tier 共享 fixture (real_popolad / mock_cli_* / time_machine / mock_lark_event_stream)
│   ├── tier1/
│   │   ├── __init__.py
│   │   ├── conftest.py                      # T1 专属 (hypothesis profiles)
│   │   ├── test_taskstate_fsm.py
│   │   ├── test_event_envelope_schema.py
│   │   ├── test_hitl_prompt_schema.py        (v0.2.3 占位 + v0.3.0 完整)
│   │   ├── test_devolaflow_context_schema.py (v0.2.3 占位 + v0.3.0 完整)
│   │   ├── test_lark_card_template.py        (v0.3.0)
│   │   └── test_*.py (适配器 / utility / pure 函数)
│   ├── tier2/
│   │   ├── __init__.py
│   │   ├── conftest.py                      # T2 专属 (responses session / freezegun fixture)
│   │   ├── test_dispatch_chain.py
│   │   ├── test_cli_httpx_round_trip.py
│   │   ├── test_supervisor_error_modes.py
│   │   ├── test_lark_event_router.py         (v0.3.0)
│   │   ├── test_lark_unauthorized_responder.py (v0.3.0)
│   │   ├── test_hitl_renderers.py            (v0.3.0)
│   │   └── test_*.py
│   ├── tier3/
│   │   ├── __init__.py
│   │   ├── conftest.py                      # T3 专属 (real_popolad shared)
│   │   ├── test_real_popolad_dispatch.py
│   │   ├── test_cross_process_status.py
│   │   ├── test_attach_stream_sse.py
│   │   ├── test_lark_listener_supervision.py (v0.3.0)
│   │   ├── test_lark_send_retry.py            (v0.3.0)
│   │   ├── test_hitl_cross_channel_sync.py    (v0.3.0)
│   │   └── nfr/
│   │       ├── __init__.py
│   │       ├── test_nfr_1_startup.py
│   │       ├── test_nfr_3_event_log_latency.py
│   │       ├── test_nfr_5_cross_terminal_survival.py
│   │       └── test_nfr_8_recovery_rate.py
│   ├── tier4/
│   │   ├── __init__.py
│   │   ├── conftest.py                      # T4 专属 (langgraph SqliteSaver shared, mock_cli_agent)
│   │   ├── test_subgraph_gen_verifier_convergence.py
│   │   ├── test_interrupt_resume_thread_id_persistence.py
│   │   ├── test_recursive_dispatch_thread_isolation.py
│   │   ├── test_concurrent_thread_id_isolation.py
│   │   ├── test_dual_gate_logic.py            (v0.3.0)
│   │   ├── test_devolaflow_skill_injection.py (v0.3.0)
│   │   ├── test_hitl_interrupt_resume.py      (v0.3.0)
│   │   └── test_lark_full_roundtrip.py        (v0.3.0)
│   ├── tier5/
│   │   ├── __init__.py
│   │   ├── conftest.py                      # T5 专属 (full env, real popolad + mock CLI 三件套)
│   │   ├── test_self_evo_round_dry_run.py
│   │   ├── test_self_evo_round_with_devolaflow.py (v0.3.0)
│   │   ├── test_reinforcement_persistence.py    (v0.3.x)
│   │   ├── test_hitl_round_floor_escalation.py  (v0.3.0)
│   │   ├── test_hitl_timeout_default.py         (v0.3.0)
│   │   └── test_lark_real_e2e.py                (v0.3.0, real_lark)
│   ├── chaos/                               # v0.2.2 新增
│   │   ├── __init__.py
│   │   ├── conftest.py
│   │   └── test_chaos_*.py                  # 12 个文件, 每文件 1 故障模式 (见 §10)
│   └── real_cli/                            # v0.2.2 新增
│       ├── __init__.py
│       ├── conftest.py                      # 检测 binary 存在; pytest.skip
│       ├── test_real_cursor_smoke.py
│       ├── test_real_claude_smoke.py
│       └── test_real_codex_smoke.py
├── self_bootstrap/                          # v0.2.0 创建 (S1 + S3); v0.2.3 完整 (S1-S5 mock); v0.3.0 真版替换
│   ├── __init__.py
│   ├── test_s1_crash_recovery.py            (v0.2.0, slow)
│   ├── test_s2_reinforcement.py             (v0.2.3 mock, v0.3.0 real)
│   ├── test_s3_recursive_dispatch.py        (v0.2.0, slow)
│   ├── test_s4_offline_resume.py            (v0.2.3 mock, v0.3.0 real)
│   └── test_s5_cross_cli_handoff.py         (v0.2.3 mock, v0.3.0 real)
└── fixtures/                                # 共享 fixtures + mock 库
    ├── __init__.py
    ├── real_popolad.py                      # T3+ daemon 启动 fixture
    ├── time_machine.py                      # T4 S4 8h offline fixture
    ├── mock_lark_event_stream.py            # T2-T3 NDJSON event 流 fixture
    └── mock_cli/                            # mock CLI agent 库 (per §4)
        ├── __init__.py
        ├── README.md                        # 行为契约文档 (v0.2.3 DoD 要求)
        ├── mock_cursor.py
        ├── mock_claude.py
        ├── mock_codex.py
        ├── mock_kimi.py                     # 占位 (Phase 2)
        └── mock_copilot.py                  # 占位 (Phase 2)
```

### 2.2 共享 conftest.py 策略

- `tests/conftest.py` (root): 保留 v0.0.1 既有的 `popolad_factory` fixture (in-process Popolad), 作为 T1+T2 简单 case 用; 不被 T3+ 使用
- `tests/matrix/conftest.py`: 跨 tier 公共 fixture (`real_popolad`, `mock_cli_*`, `time_machine`, `mock_lark_event_stream`) 集中定义, 各 tier 子目录 `conftest.py` 仅放 tier 专属
- 每个 tier 子目录 `conftest.py`: 只放 tier 专属 fixture / pytest profile / mark 自动注入; 不重复定义跨 tier fixture
- pytest 自动从 `conftest.py` 链路向上发现, 无需 import; **避免循环 fixture 依赖** (e.g. `real_popolad` 不应依赖 `mock_cli_*` 反过来)

### 2.3 mock CLI fixture 组织

详见 [§4](#4-mock-cli-agent-library-spec); 简要规则:

- 单文件 = 单 mock CLI: `tests/fixtures/mock_cli/mock_cursor.py` 暴露一个 `MockCursor` 类 + 一个 pytest fixture `mock_cursor` (function-scoped, 返回新实例)
- 行为脚本 API: 测试通过 `mock.set_response(prompt_pattern, response)` 控制 mock 返回; 默认空响应 + exit_code=0
- 共享 README: `tests/fixtures/mock_cli/README.md` 列每个 mock 的 stdin/stdout/exit code 语义 + 与真 CLI 的契约对照表 (per v0.2.3 DoD 要求)
- 注册到 pytest: 通过 `tests/matrix/conftest.py` 统一 import + re-export, 各 tier `conftest.py` 不重复

### 2.4 测试命名约定

- 文件名: `test_<功能模块>_<场景>.py` (e.g. `test_lark_full_roundtrip.py`)
- 函数名: `test_<被测行为>_<前置条件>_<期望结果>` (e.g. `test_dispatch_with_unknown_cli_raises_keyerror`)
- 每个用例 docstring 必填 (描述 + 出处, e.g. `"""roadmap §3.3 chaos #5 — event_log fd close mid-write 干净降级."""`)
- snapshot 文件命名: 自动按 `tests/__snapshots__/test_<file>/test_<func>.ambr` (syrupy 默认)

---

## 3. 选择性运行 + CI 矩阵

### 3.1 pytest selector cheat-sheet (开发者本地)

```bash
# 开发循环 (~10s, 默认)
pytest

# 仅 T1 (单元级 fast feedback, ~8s)
pytest tests/matrix/tier1

# T1 + T2 (~75s, PR 必跑)
pytest -m "not slow and not e2e"

# T3 + T4 (跨进程 + DAG, ~20min, 周一 cron)
pytest -m slow

# T3 真 daemon 但跳过真 CLI (~10min, 无 binary 环境)
pytest -m "slow and not real_cli and not real_lark"

# T5 端到端 (~25min, nightly)
pytest -m e2e

# 仅 NFR benchmark (~3min, 性能回归)
pytest --benchmark-only -m slow

# 仅 chaos 12 故障模式 (~5min)
pytest tests/matrix/chaos -m slow

# 仅真 CLI smoke (本地装 binary 后)
pytest -m real_cli

# 仅真 Lark e2e (env LARK_BOT_APP_ID 已设)
pytest -m real_lark

# 全量 (release-gate, ~50min)
pytest -m "slow or e2e or real_cli or real_lark"
```

### 3.2 CI lanes (GitHub Actions)

| Lane 名 | 触发 | pytest 命令 | 时长 | 失败阻断 |
|---------|------|-------------|------|----------|
| **default** | 每 PR + push to main | `pytest -m "not slow and not e2e and not real_cli and not real_lark" --cov --cov-fail-under=<version>` | ~ 80s | ✅ 阻断 merge |
| **slow** | weekly cron (每周一 03:00 UTC) | `pytest -m "slow and not real_cli and not real_lark"` | ~ 25 min | ✅ 阻断 release |
| **nightly** | nightly cron (每天 03:00 UTC) | `pytest -m "e2e or nightly"` | ~ 30 min | ⚠️ 不阻断 PR, 仅告警 |
| **real_cli** | weekly cron + 手动触发 | `pytest -m real_cli` (CI runner 装 cursor-agent + claude + codex 二进制) | ~ 8 min | ⚠️ 仅告警, 用于检测 mock 漂移 (per R-EVO-2) |
| **real_lark** | weekly cron + release-gate (v0.3.0+) | `pytest -m real_lark` (env: `LARK_BOT_APP_ID`, `LARK_BOT_APP_SECRET`, `LARK_HITL_TARGET_OPEN_ID` from secrets) | ~ 10 min | ✅ 阻断 v0.3.0+ release |
| **release-gate** | manual (release tag 推送时) | `pytest -m "slow or e2e or real_cli or real_lark"` | ~ 50 min | ✅ 阻断 git tag |
| **mutmut** (v0.4.0 准入) | manual + monthly cron | `mutmut run --paths-to-mutate src/popolaloom/` | ~ 2 h | ⚠️ 仅生成报告 |

### 3.3 CI matrix (OS × Python)

| 维度 | 默认 lane | slow lane | nightly lane |
|------|-----------|-----------|--------------|
| **OS** | ubuntu-22.04 + ubuntu-24.04 (per v0.2.2 §3.3 DoD) | ubuntu-22.04 + ubuntu-24.04 | ubuntu-22.04 only |
| **Python** | 3.11 + 3.12 (per pyproject.toml classifiers) | 3.11 only (减少 slow lane 总时长) | 3.11 only |
| **arch** | x86_64 only (Phase 1) | x86_64 only | x86_64 only |
| **macOS / Windows** | 推迟 v0.4.0+ Phase 2 | — | — |

### 3.4 建议 cron schedule

- **每周一 03:00 UTC** (slow lane + real_cli): 给开发者周一上班时看到周末以来的 slow 回归
- **每天 03:00 UTC** (nightly lane): 给开发者每日检查端到端健康
- **每月 1 日 03:00 UTC** (mutmut, v0.4.0+): mutation testing 报告
- **release tag 推送时** (release-gate): 完整阻断, 全 lane PASS 才允许 tag

---

## 4. mock CLI agent library spec

### 4.1 设计目标

- 在 T2-T5 测试中替代真 cursor / claude / codex 二进制, 让测试可在无 binary 环境跑通 (CI 默认 lane)
- 行为契约严格匹配真 CLI 的 stdin/stdout/exit code 语义 (per v0.2.3 DoD `tests/fixtures/mock_cli/README.md` 要求)
- 支持 v0.3.0+ devola-flow 注入测试: mock 必须输出三段格式 (per roadmap §11.1 L3 output contract)

### 4.2 mock CLI 三件套 + 占位 (per roadmap §11 + spec §3.2 Phase 2)

| Mock | 文件 | 模拟对象 | 真 CLI 行为契约 (来自 spec §3.2 + Phase 1 adapters) |
|------|------|-----------|-----------------------------------------------------|
| **mock_cursor** | `tests/fixtures/mock_cli/mock_cursor.py` | `cursor-agent agent --print --output-format text` (Phase 1, v0.0.1 已实) | stdin: 空 (prompt 由 argv 传); stdout: text 或 stream-json (NDJSON); exit_code 0 / 非 0; 支持 `--session-id <id>` / `--cwd` / `-w worktree` / `--output-format text\|stream-json` |
| **mock_claude** | `tests/fixtures/mock_cli/mock_claude.py` | `claude -p <prompt> --output-format stream-json --verbose` | stdin: 可 inject follow-up; stdout: stream-json NDJSON (含 usage 字段 token count); exit_code 0 / 非 0; 支持 `--session-id <UUID>` / `--bare` / `--max-turns N` |
| **mock_codex** | `tests/fixtures/mock_cli/mock_codex.py` | `codex exec [--sandbox <mode>] <prompt>` | stdin: 空; stdout: text; exit_code 0 / 非 0; 支持 `--sandbox read-only\|workspace-write\|danger-full-access`; ValueError 路径 (非法 sandbox value) |
| **mock_kimi** | `tests/fixtures/mock_cli/mock_kimi.py` (占位) | (Phase 2 增量, 当前无规范) | 占位 stub: 仅返回固定 "kimi-mock" + exit 0; 接口预留 `set_response` / `set_exit_code` 但无 schema 检查 |
| **mock_copilot** | `tests/fixtures/mock_cli/mock_copilot.py` (占位) | (Phase 2 增量, 类似 Kimi) | 占位 stub; 同上 |

### 4.3 行为脚本 API (统一接口)

每个 mock CLI 类暴露:

```python
class MockCLIBase:
    """Base for mock_cursor / mock_claude / mock_codex behavior scripting."""

    name: str  # "cursor" / "claude" / "codex"
    binary: str  # mock binary 路径 (sys.executable + 脚本)

    def set_response(
        self,
        prompt_pattern: str | re.Pattern,  # match by substring or regex
        response: str | list[str],          # stdout lines (list = NDJSON 多行)
        exit_code: int = 0,
        delay_s: float = 0.0,                # 模拟 LLM 延迟
        emit_devolaflow_three_section: bool = False,  # v0.2.3+ 关键
    ) -> None:
        """Configure mock to respond with `response` when prompt matches."""

    def set_default_response(self, response: str, exit_code: int = 0) -> None:
        """Fallback response for unmatched prompts."""

    def get_call_log(self) -> list[CallRecord]:
        """Return list of (prompt, argv, exit_code, stdout, started_at)."""

    def reset(self) -> None:
        """Clear all responses + call log."""
```

`CallRecord` Pydantic model:

```python
class CallRecord(BaseModel):
    prompt: str
    argv: list[str]                          # 完整 argv (含 --session-id 等)
    exit_code: int
    stdout_lines: list[str]
    stderr_lines: list[str]
    started_at: datetime
    completed_at: datetime
    devolaflow_round: int | None             # 解析自 prompt "## Workflow Context" 段
```

### 4.4 v0.2.3+ devola-flow 三段输出契约 (强制)

per roadmap §11.1 L3 Output contract — 当 `emit_devolaflow_three_section=True` 时, mock CLI 必须按以下顺序输出 stdout:

1. **第一行**: `[devola-flow:round=N]` (N = 解析自 prompt 的 round_num; 若 prompt 无 Workflow Context 则用 `round=0` fallback)
2. **正文区**: 任意 mock-controlled 内容 (e.g. patch / 分析 / fake LLM 输出)
3. **末尾三段** (固定顺序, 必须出现):
   - `## Acceptance Verification` (列每个 AC 满足证据, ≥ 1 行)
   - `## Gate Score Components` (test_quality / code_review / architecture / benchmark 各得分 + composite, ≥ 4 行 + composite_score 行)
   - `## Findings` (severity-classified: blocker / critical / major / minor / info, 至少 1 个 severity 段)

示例输出 (mock_cursor, round=3):

```
[devola-flow:round=3]

(任意 mock-controlled 正文 — e.g. "已实现 popola list --json flag, patch 见下:")

## Acceptance Verification
- AC-1: popola list --json 输出合法 JSON — pytest tests/matrix/tier2/test_cli_list_json.py PASS
- AC-2: 不破坏既有 popola list 默认 Rich 输出 — pytest test_e2e PASS

## Gate Score Components
- test_quality: 0.92 (新增 5 case, 全 PASS)
- code_review: 0.88 (复用既有 _format_status_table)
- architecture: 0.85 (无新依赖)
- benchmark: 0.90 (--json 路径 < 50ms)
- composite: 0.886

## Findings
- info: 输出格式遵循 ndjson convention (每行一个 task)
- minor: 错误退出码尚未走 --json (推迟 v0.3.0)
```

PopolaLoom inner gate verifier (`src/popolaloom/evolution/dual_gate.py`) 解析此三段:

- 第一行 missing `[devola-flow:round=N]` → inner gate FAIL + sub-task retry (max 2x); 失败则 `findings` reinforcement 标注 "L3 unable to follow devola-flow contract" (per R-DEVOLAFLOW-1)
- 三段任一缺失 → 同上 retry path
- 三段格式 OK 但 `composite_score < 0.85` → inner gate FAIL (但不 retry — 本身就是 score 不够, retry 不能改 score)

### 4.5 测试用法示例 (在 T4 / T5 case 中)

```python
def test_mock_cursor_outputs_devolaflow_three_section(mock_cursor):
    """Tier 4 case: mock_cursor 收到 dispatch 输出三段格式."""
    mock_cursor.set_response(
        prompt_pattern="实现 R-001",
        response="(任意正文)",
        emit_devolaflow_three_section=True,
    )
    # ... dispatch + 等待完成 ...
    record = mock_cursor.get_call_log()[-1]
    assert record.stdout_lines[0].startswith("[devola-flow:round=")
    assert any("## Acceptance Verification" in line for line in record.stdout_lines)
    assert any("## Gate Score Components" in line for line in record.stdout_lines)
    assert any("## Findings" in line for line in record.stdout_lines)
```

### 4.6 与真 CLI 的漂移检测 (per R-EVO-2)

- v0.3.0+ 起每周一次 nightly real_cli smoke (`tests/matrix/real_cli/`) 跑最小 echo prompt → 对比 mock 输出
- 漂移 > 5% 触发 issue + mock 重写 task
- 5% 度量: 对比 (a) exit_code; (b) stdout 行数; (c) 关键字段 (e.g. claude `usage.tokens`); 任一不一致计 1; 三个维度 / 3 = 漂移率

---

## 5. fixture 公共契约

### 5.1 `real_popolad` (T3+; `tests/fixtures/real_popolad.py`)

**用途**: 启动真 popolad daemon 进程, 用 tmp_path UDS + tmp_path SQLite, 测试结束 SIGTERM 清理

**Scope**: `module` (per file, 避免反复启 daemon)

**契约**:

```python
@pytest.fixture(scope="module")
def real_popolad(tmp_path_factory) -> Iterator[RealPopoladHandle]:
    """启 popolad subprocess + 等 ready (UDS 监听 < 5s) + yield handle.

    Returns:
        RealPopoladHandle:
            socket_path: Path                    # ~/.popola/<tmp>/popolad.sock
            arktower_db: Path                    # ~/.popola/<tmp>/arktower.db
            sqlite_saver_db: Path                # ~/.popola/<tmp>/state.sqlite
            events_dir: Path
            pid: int
            client: httpx.AsyncClient            # UDS transport pre-configured
            wait_terminal(task_id, timeout): TaskState  # poll helper
    """
```

**清理**: 测试结束 fixture 自动 SIGTERM (5s 优雅) → SIGKILL (兜底) + 清理 tmp_path

**v0.2.0 vs v0.2.2 vs v0.3.0 演进**:

- v0.2.0: 仅 in-process popolad (Stage A 还未做 daemon 进程化); fixture 暂用 `popolad_factory` 等价
- v0.2.2 (Tier 3 引入): 真 daemon 进程化已完成, 此 fixture 首次落地
- v0.3.0+: 加 `with_lark_listener: bool` 参数 (默认 False) — True 时同时启 mock lark-cli listener 子进程

### 5.2 `mock_cli_*` (T2+; `tests/fixtures/mock_cli/`)

**用途**: 替代真 cursor / claude / codex 二进制, 控制 mock 行为脚本

**Scope**: `function` (每 case 独立, reset)

**契约**: 见 [§4.3](#43-行为脚本-api-统一接口)

```python
@pytest.fixture
def mock_cursor() -> Iterator[MockCursor]:
    m = MockCursor()
    yield m
    m.reset()

@pytest.fixture
def mock_claude() -> Iterator[MockClaude]: ...
@pytest.fixture
def mock_codex() -> Iterator[MockCodex]: ...
```

**集成方式**: mock 实例通过 `register_adapter(mock.as_adapter())` 注入 adapter registry, dispatch 时被走 (与真 cursor adapter 分流)

### 5.3 `time_machine` (T4 S4 场景; `tests/fixtures/time_machine.py`)

**用途**: 模拟 8h 跨度 (S4 离线场景) + HITL deadline 24h 倒计时

**实现**: 基于 `freezegun` 包装

**Scope**: `function`

**契约**:

```python
@pytest.fixture
def time_machine() -> Iterator[TimeMachine]:
    """tm.travel(timedelta(hours=8)) — 跳到 8h 后, freeze; tm.tick(seconds=1) — 走 1s; tm.now() — 当前 frozen 时间."""
    with freeze_time(datetime.now(timezone.utc)) as frozen:
        yield TimeMachine(frozen)
```

**T4 S4 用法**:

```python
def test_s4_8h_offline_resume(real_popolad, time_machine, mock_cursor):
    # ... dispatch + 触发 interrupt() ...
    time_machine.travel(timedelta(hours=8))  # 模拟 IDE 关闭 8h
    # ... 重开 IDE 拉 pending interrupt → supply_feedback ...
    assert task.state == TaskState.COMPLETED
```

### 5.4 `mock_lark_event_stream` (T2-T3 Lark; `tests/fixtures/mock_lark_event_stream.py`)

**用途**: 替代真 `lark-cli event consume` 子进程, 注入 NDJSON event 流到 listener

**Scope**: `function`

**契约**:

```python
@pytest.fixture
def mock_lark_event_stream() -> Iterator[MockLarkEventStream]:
    """mock subprocess.Popen for lark-cli event consume.

    s.emit_button_click(hitl_id, option_id)  # 注入 card.action.trigger_v1 event
    s.emit_text_message(text, sender_open_id)  # 注入 im.message.receive_v1 event
    s.emit_heartbeat()  # 注入 event.heartbeat (60s 心跳)
    s.die(exit_code=1)  # 模拟 listener crash → supervisor restart 触发
    """
```

**T2 用法**: 路由测试 (event → RPC 调用 → 验证 RPC payload)

**T3 用法**: 真 popolad + listener supervision 测试 (subprocess 重启 ≤ 3 次)

### 5.5 `real_lark` (T5 Lark e2e; gated by env)

**用途**: 真 lark-cli + 真 bot, 实际发卡 / 收 event

**Scope**: `session` (整 test session 共享, 避免反复 auth)

**契约**:

```python
@pytest.fixture(scope="session")
def real_lark() -> RealLarkClient:
    """要求 env: LARK_BOT_APP_ID, LARK_BOT_APP_SECRET, LARK_HITL_TARGET_OPEN_ID.

    缺任一则 pytest.skip("real_lark fixture requires bot credentials").
    """
    for var in ("LARK_BOT_APP_ID", "LARK_BOT_APP_SECRET", "LARK_HITL_TARGET_OPEN_ID"):
        if not os.environ.get(var):
            pytest.skip(f"real_lark requires env {var}")
    return RealLarkClient(...)
```

**仅在 `@pytest.mark.real_lark` case 使用** — 默认 CI 跳过, weekly cron + release-gate 跑

---

## 6. coverage gates by version

### 6.1 `[tool.coverage.report] fail_under` 版本递进

per roadmap §3 + §6 顶层 DoD 矩阵, `pyproject.toml [tool.coverage.report]` 的 `fail_under` 字段每版本递增:

| 版本 | `fail_under` | 备注 | pyproject.toml 改动时机 |
|------|--------------|------|--------------------------|
| v0.0.1 | (无) | 现状未配置 | — |
| **v0.2.0** | **75** | Stage E 完成时设入 (per v0.2.0-plan.md §5 DoD §3) | v0.2.0 Stage E |
| **v0.2.1** | **80** | Tier 1+2 完成时升 +5 | v0.2.1 patch 头一次 commit |
| **v0.2.2** | **85** | Tier 3 + NFR 完成时升 +5 | v0.2.2 patch |
| **v0.2.3** | **90** | Tier 4+5 完成时升 +5 | v0.2.3 patch |
| **v0.3.0** | **90** (不降) | 自演化基础设施期, 守住 90% | v0.3.0 |
| **v0.3.1..v0.3.5** | **90** (不降) | 每轮自演化新增代码必带测试, 守 90% | 各 round |
| **v0.4.0 GA** | **92** | 玻璃天花板; 余 8% 用 `# pragma: no cover` 标无法测边界 (per R-COV-1) | v0.4.0 release-gate |

### 6.2 配置原则 (per [tool.coverage.run] + [tool.coverage.report])

```toml
[tool.coverage.run]
source = ["src/popolaloom"]
branch = true                 # 必须开 — Tier 1 主战场是 branch coverage
parallel = true               # 多 lane CI 合并 .coverage 文件

[tool.coverage.report]
fail_under = 75               # 各版本递增
show_missing = true           # 输出每个 missing line, 帮 Tier 1 补 case
skip_covered = false          # 不跳已覆盖文件 (避免漏看哪些已 100% 哪些 0%)
exclude_lines = [
    "pragma: no cover",
    "raise NotImplementedError",
    "if TYPE_CHECKING:",
    "if __name__ == .__main__.:",
]
```

### 6.3 覆盖率分布目标 (per roadmap §3.1 末尾)

各 tier 对全局 line coverage 的贡献目标 (cumulative):

- Tier 1 + 2: ≥ 60% 全局
- Tier 3: +15% (达 75%)
- Tier 4: +10% (达 85%)
- Tier 5: +5% (达 90%)

v0.4.0 92% 通过 mutation testing (mutmut) 调优 + 边界 `# pragma: no cover` 标记达标。

---

## 7. property-based testing strategy (hypothesis)

### 7.1 候选实体 + 不变量

| 实体 | 不变量 | hypothesis strategy | tier |
|------|--------|---------------------|------|
| **TaskState FSM** | 任意合法状态序列 → state machine 不会进入未定义状态; 任意非法迁移 raise ValueError | `hypothesis.stateful.RuleBasedStateMachine` + `@rule(state=...)` 8 个状态 × M 输入 | T1 |
| **NDJSON CloudEvents 1.0 envelope** | 任意 (type: str, data: dict) → envelope.specversion == "1.0", envelope.id startswith "evt-", envelope.time endswith "Z", envelope.source matches pattern `popola/<id>` | `st.text() + st.dictionaries()` for inputs | T1 |
| **HITLPrompt schema** | 任意合法 5 字段 → Pydantic 不 raise; 任意缺字段 / option < 2 / channel < 2 → ValidationError | `st.builds(HITLPrompt, ...)` + composite strategy | T1 |
| **ConductorDispatch parser** | 任意合法 yaml dict → 解析为 ConductorDispatch 不 raise; 任意 cycle (a→b→a) → raise CycleError | `st.fixed_dictionaries({"plan_dag": st.lists(...)})` | T1 |
| **NDJSON event_log envelope id 唯一性** | append N 次 → 所有 envelope.id distinct (uuid4 collision probability ~ 0) | `st.integers(min_value=1, max_value=10_000)` for N | T1 |
| **adapter build_command extra-key matrix** | 3 adapter × 任意 extra subset → cmd 是 list[str] + cmd[0] == binary + 必含 prompt | `st.sampled_from(["cursor", "claude", "codex"])` + `st.dictionaries()` | T1 |

### 7.2 默认 examples 数 + deadline override

```python
# tests/matrix/tier1/conftest.py
from hypothesis import settings, HealthCheck

settings.register_profile(
    "default",
    max_examples=100,
    deadline=300,  # 300ms per example, 给 Pydantic 校验留余地
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "ci_nightly",
    max_examples=500,  # nightly 拉 5x examples
    deadline=1000,
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))
```

CI 默认 lane 用 `default` profile (~100 examples / property × 5 property ≈ 500 total examples ≈ 5s 总耗时, 容忍范围内); nightly 拉到 500 examples (~25s, 仍 < 30s 全套 T1 上限).

### 7.3 v0.2.1 落地清单

- ≥ 5 property test (per v0.2.1 DoD)
- 至少 1 个 `RuleBasedStateMachine` (TaskState FSM)
- 全部 hypothesis test 必须显式 `@settings(deadline=...)` 标注 (避免 default 200ms 失败)
- `tests/matrix/tier1/test_taskstate_fsm.py::TestTaskStateMachine` 用 `@invariant()` 断言: state ∈ defined enum + completed_at 在 terminal 后必 not-None

---

## 8. snapshot testing strategy (syrupy)

### 8.1 适用场景

| 场景 | 文件 | tier | 何时更新 |
|------|------|------|----------|
| **langgraph DAG 节点访问序列** | `tests/matrix/tier4/test_subgraph_*.py` | T4 | DAG 拓扑变更时 (`pytest --snapshot-update`) |
| **Lark interactive card JSON** | `tests/matrix/tier1/test_lark_card_template.py` | T1 | card 模板字段变更时 |
| **HITLPrompt rendered output 5 通道** | `tests/matrix/tier2/test_hitl_renderers.py` | T2 | renderer 改 schema → 渲染对象映射时 |
| **nines.toml diff (版本间对比)** | `tests/matrix/tier5/test_nines_progression.py` | T5 | 每 patch / round 后 review diff |
| **TaskState transition trace JSON** | `tests/matrix/tier3/test_real_popolad_*.py` | T3 | dispatch 路径变更时 |
| **mock CLI devola-flow 三段输出格式** | `tests/matrix/tier4/test_devolaflow_skill_injection.py` | T4 | mock CLI 行为契约变更时 |

### 8.2 配置

```toml
# pyproject.toml (无需额外 syrupy section, 用默认即可)
# snapshot 文件落到: tests/__snapshots__/ (syrupy 默认)
```

### 8.3 update workflow

- **新建 snapshot**: `pytest tests/matrix/tier1/test_lark_card_template.py --snapshot-update` (生成 `__snapshots__/test_lark_card_template/test_*.ambr`)
- **review snapshot**: code review 时 PR diff 必含 snapshot 文件 (避免无审查 update)
- **CI 校验**: `pytest --snapshot-warn-unused` (检测 stale snapshot, 仅 warn 不 fail; nightly 升级 `--snapshot-strict` 强制 fail)
- **冲突解决**: snapshot 文件视为 generated artifact, 冲突时优先 re-generate (`--snapshot-update`) 而非手动 merge

### 8.4 反模式 (禁用 snapshot 的场景)

- ❌ NDJSON 时间戳字段 (会因 `time.time()` 漂移; 用 `freezegun` 冻结时间后再 snapshot)
- ❌ envelope.id (uuid4 随机; snapshot 序列化时用 `serializer=lambda x: re.sub(r'evt-[a-f0-9]+', 'evt-<UUID>', x)`)
- ❌ subprocess pid (随机; 同上 mask)
- ❌ langgraph thread_id (uuid4; 同上 mask)

---

## 9. NFR 量化绑定

### 9.1 NFR ↔ Tier 3 测试 + pytest-benchmark assertion 表

| NFR | 指标 | 目标值 | Tier 3 文件 | pytest-benchmark assertion | 实现 owner |
|-----|------|--------|-------------|----------------------------|------------|
| **NFR-1** | 启动 daemon 时间 | ≤ 2 s | `tests/matrix/tier3/nfr/test_nfr_1_startup.py` | `benchmark.pedantic(start_popolad, rounds=5, iterations=1)` + `assert benchmark.stats["mean"] < 2.0` | v0.2.2 Stage 3 |
| **NFR-3** | 单 task event log 写入 | < 5 ms (NDJSON append) | `tests/matrix/tier3/nfr/test_nfr_3_event_log_latency.py` | `benchmark(event_log.append, "task.test", {...})` 1000 iterations + `assert benchmark.stats["mean"] < 0.005` | v0.2.0 Stage C (mvp) → v0.2.2 (强化) |
| **NFR-5** | 跨终端退出存活成功率 | ≥ 99% | `tests/matrix/tier3/nfr/test_nfr_5_cross_terminal_survival.py` | 100 次循环: 终端 A `popola dispatch` → 终端 A 关闭 → 终端 B `popola attach` 取 final event; success / 100 ≥ 0.99 | v0.2.2 Stage 3 (per spec NFR-5) |
| **NFR-8** | 失败回滚成功率 | ≥ 95% | `tests/matrix/tier3/nfr/test_nfr_8_recovery_rate.py` | 20 次循环: dispatch → SIGKILL popolad → restart → rehydrate; success / 20 ≥ 0.95 | v0.2.2 Stage 3 |

### 9.2 NFR-3 详细规格 (示例 — 其他 NFR 模板相同)

```python
# tests/matrix/tier3/nfr/test_nfr_3_event_log_latency.py
"""NFR-3: NDJSON event log append 单条延迟 < 5ms (mean over 1000 iter).

出处: spec.md §6 NFR-3 + roadmap §3.3 v0.2.2 NFR 量化基线.
"""

import pytest
from popolaloom.daemon.event_log import EventLog


@pytest.mark.slow
@pytest.mark.benchmark(group="nfr-3")
def test_ndjson_append_avg_under_5ms_for_1000_events(benchmark, tmp_path):
    log = EventLog(tmp_path / "events" / "T-bench.jsonl")

    def append_one():
        log.append("task.heartbeat", {"task_id": "T-bench", "ts": "2026-05-04T00:00:00Z"})

    benchmark.pedantic(
        append_one,
        rounds=10,
        iterations=100,  # 10 × 100 = 1000 total
        warmup_rounds=2,
    )
    assert benchmark.stats["mean"] < 0.005, (
        f"NFR-3 violation: mean = {benchmark.stats['mean']*1000:.2f}ms, target < 5ms"
    )
```

### 9.3 NFR 量化结果入库

- 每个 lane 跑完后, pytest-benchmark JSON 输出存到 `benchmarks/<commit-sha>/<lane>.json`
- v0.4.0 release 时 commit `benchmarks/baseline.json` 作为基线; 后续 PR 用 `--benchmark-compare=baseline.json --benchmark-compare-fail=mean:5%` 检测回归 (mean > 5% 涨即 fail)

### 9.4 其他 NFR 在何处覆盖 (非 Tier 3 主测)

- **NFR-2** (attach 延迟 ≤ 200ms): T2 `tests/matrix/tier2/test_attach_latency.py` (mock subprocess 即可); T3 nfr/ 加一条 real benchmark
- **NFR-4** (popolad 内存 ≤ 200 MB 空载 / ≤ 1 GB 10 并发): T3 `tests/matrix/tier3/nfr/test_nfr_4_memory_baseline.py` (用 `psutil.Process(pid).memory_info().rss`)
- **NFR-6** (HITL 通知 ≤ 5s Lark / ≤ 1s IDE): T3 `tests/matrix/tier3/test_lark_send_retry.py` benchmark sub-test
- **NFR-7** (auto-merge 误判率 ≤ 5%): T5 `tests/matrix/tier5/test_automerge_gate_decision.py` 跑 100 次 PR mock 决策, fail rate ≤ 5%
- **NFR-9** (token 成本 < 5× chat baseline): mock 不真消耗 token, 仅在真 e2e nightly + manual review 中度量
- **NFR-10** (收敛轮数 ≤ 3 平均): T4 / T5 self-bootstrap S2 metric + nightly aggregation
- **NFR-11** (max 10 并发): T3 `test_concurrent_dispatch_caps_at_10.py` stress
- **NFR-12** (单 plan event log ≤ 50 MB): T3 `test_event_log_rotation_at_50mb.py`

---

## 10. chaos / fault injection (12 失败模式)

per roadmap §3.3 v0.2.2 — Chaos 12 failure modes, 每条 emit 正确事件 (No Silent Failures), 0 silent crash. 全部归 Tier 3 (`tests/matrix/chaos/`), 共享 `@pytest.mark.slow @pytest.mark.chaos` 双 mark.

| # | 失败模式 | 一句话描述 | Tier 3 文件 | 期望 emit |
|---|----------|------------|-------------|-----------|
| **C1** | ArkTower TaskService.create_task IntegrityError | 重复 task_id 插入触发 SQLite UNIQUE constraint | `test_chaos_arktower_create_task_integrity.py` | `dispatch.failed` event + 不创建 NDJSON 文件 + StateStore 不 register |
| **C2** | ArkTower TaskService.create_task DatabaseLocked | SQLite WAL busy 锁触发 (mock `sqlite3.OperationalError("database is locked")`) | `test_chaos_arktower_db_locked.py` | `dispatch.retry_exhausted` event + 重试 3 次后放弃 (per supervisor retry policy) |
| **C3** | SqliteSaver checkpoint write 磁盘满 | mock `open()` raise `OSError(ENOSPC)` | `test_chaos_sqlite_saver_disk_full.py` | `checkpoint.failed` event + graph node 中止 + 不污染 in-memory state |
| **C4** | SqliteSaver corrupt checkpoint schema mismatch | mock SqliteSaver.from_conn_string 加载到 v0.1 schema 但程序是 v0.2 | `test_chaos_sqlite_saver_schema_mismatch.py` | `checkpoint.schema_mismatch` event + popolad 启动 abort + 提示 migration |
| **C5** | event_log fd close mid-write | mock `os.close(fd)` 在 append 中途 (用 monkeypatch + threading) | `test_chaos_event_log_fd_close_mid_write.py` | `event_log.write_failed` event + 自动重新 open fd + 重试 1 次 |
| **C6** | event_log file system permission denied | tmp_path 后 `chmod 000` 该目录, 模拟 mid-run 权限被夺 | `test_chaos_event_log_permission_denied.py` | `event_log.permission_denied` event + popolad 优雅停止 + 提示用户 |
| **C7** | Supervisor.spawn binary not found | mock 注入 cli="ghost" 没有 adapter; 或 cursor-agent 二进制 unlink 后 spawn | `test_chaos_supervisor_binary_missing.py` | `dispatch.binary_not_found` event + KeyError raise + StateStore 不 register |
| **C8** | Supervisor subprocess SIGKILL (OOM 模拟) | spawn 后立即 `os.kill(pid, signal.SIGKILL)` | `test_chaos_supervisor_subprocess_oom.py` | `task.killed_by_signal` event (signal=9, exit=137) + state=FAILED + 不 hang join |
| **C9** | Supervisor subprocess SIGTERM (用户 kill -15) | spawn 后用户主动 `popola cancel <task_id>` 触发 SIGTERM | `test_chaos_supervisor_subprocess_user_cancel.py` | `task.canceled` event + state=CANCELED + cleanup 完整 |
| **C10** | asyncio loop stalled (HITL hang > timeout) | mock interrupt() 后 supply_feedback 不来; deadline 到 | `test_chaos_asyncio_hitl_hang_timeout.py` | `hitl.timeout_default` event + 走 default_option_id + cancel 其他通道 |
| **C11** | UDS socket disconnect mid-RPC | 启 daemon → CLI 发 `/dispatch` 中途 daemon 重启 (mock `httpx.RemoteProtocolError`) | `test_chaos_uds_disconnect_mid_rpc.py` | CLI emit clear error "popolad disconnected, retrying..." + 自动重试 1 次 (idempotent endpoint 才重试) |
| **C12** | ArkTower migration partial fail (005 mid-apply rollback) | mock 005 migration 跑到一半 raise → ArkTower MigrationRunner rollback | `test_chaos_arktower_migration_partial_fail.py` | `migration.rollback` event + popolad 启动 abort + 提示用户手动修复 |

### 10.1 共通断言模板

每个 chaos test 必须断言:

```python
def test_chaos_X(real_popolad, mock_failure):
    # 1. 触发 dispatch 触发故障
    # 2. 等 ≤ 3s 看 NDJSON 末尾事件
    events = read_events(...)
    failure_events = [e for e in events if e["type"].endswith(".failed") or "error" in e["type"]]
    assert len(failure_events) >= 1, "No Silent Failures violation: 必须 emit failure event"

    # 3. 断言失败事件含 error 详情 (per workspace rule: 不 silent swallow)
    assert "error" in failure_events[-1]["data"], "failure event 必须含 error 字段"

    # 4. popolad 进程没死 (除非该故障设计就是 abort, 例如 C4 C12)
    if not failure_mode_aborts_daemon:
        assert real_popolad.is_alive()

    # 5. StateStore 状态一致 (无 ghost task)
    listed = real_popolad.client.get("/list").json()
    # ...
```

---

## 11. test count growth ledger

### 11.1 各版本测试新增 + 累计 + 覆盖率 + tier + fixtures 表

| 版本 | 新增测试 | 累计总数 | 覆盖率目标 | 引入 tier / 新组件 | 新增 fixtures |
|------|----------|----------|------------|---------------------|---------------|
| **v0.0.1** (现状) | — | 18 | 未报 | (Tier 1 等价) | `popolad_factory` |
| **v0.2.0** | +32 | 50 | ≥ 75% | Stage A: T2 cli httpx + daemon RPC; Stage B: T1 graph schema; Stage C: T3 NFR-3 mvp; Stage D: T2 mcp tools; Stage E: 1 self-bootstrap S1 | (无新; 复用 popolad_factory) |
| **v0.2.1** | +60 | 110 | ≥ 80% | **Tier 1** (35) + **Tier 2** (25): hypothesis property (≥ 5 property), parametrized adapter 矩阵 (45 case), supervisor 错误路径 5 模式 × 3 adapter, CLI httpx → daemon RPC mock 7 endpoint, Pydantic v2 state schema validation | hypothesis profile (`tier1/conftest.py`); responses session (`tier2/conftest.py`); freezegun helper |
| **v0.2.2** | +50 | 160 | ≥ 85% | **Tier 3** (20) + NFR-1/3/5/8 (8) + chaos (12) + real_cli smoke (10): 真 popolad subprocess fixture, S1 真版 (跨进程 SIGKILL/restart), 12 chaos failure mode, NFR pytest-benchmark | `real_popolad`; `mock_lark_event_stream` (mvp); CI matrix (ubuntu 22+24, slow lane) |
| **v0.2.3** | +40 | 200 | ≥ 90% | **Tier 4** (18) + **Tier 5** (8) + S1-S5 mock 全 (5) + HITL/devola-flow schema 单测 (9): 真 langgraph 子图 ≥ 8 case, mock CLI 三件套, devola-flow 三段输出 mock 契约, HITLPrompt + Workflow Context Pydantic schema 落地 | `mock_cursor` / `mock_claude` / `mock_codex`; `time_machine`; mock CLI README |
| **v0.3.0** | +50 | 250 | ≥ 90% (不降) | T3+T4+T5 真版填充: F1 真 nines (8 维真测量), F2 三原语 (relay/supervise/federate, +12 case), F2.5 双 gate (6), F4 HITL 全栈 (Tier 1 schema 10 + Tier 2 renderer 10 + Tier 3 sync 4 + Tier 4 interrupt-resume 6 + Tier 5 floor-escalation 3 = 33; -10 重叠 v0.2.3), Lark 专项 (Tier 1 卡片 15 + Tier 2 router 4 + Tier 3 supervisor 2 + Tier 4 roundtrip 4 = 25), F5 S2/S4/S5 真版 | `real_lark` (env-gated); Lark listener supervision fixture; nines.toml syrupy snapshot |
| **v0.3.1..v0.3.5** | +5..10 / round | 250+ → ~270 | ≥ 90% (不降) | 自演化每轮: per fixed R-XXX 必带 1 红→绿 测试 (避免 R-EVO-3 silent regression) | (无新; mutmut Phase 2 准备) |
| **v0.4.0 GA** | +60..80 | ≥ 350 | ≥ 92% | 兜底补差: 8 维 ≥ 0.85 各自补维度专项 case; mutmut 报告引入 (mutation testing); 玻璃天花板 `# pragma: no cover` 标边界 | mutmut runner (manual + cron) |

### 11.2 累计 fixture 库

到 v0.4.0 时, `tests/fixtures/` 应含:

- `real_popolad.py` — daemon 启动 (T3+)
- `mock_cli/{mock_cursor,mock_claude,mock_codex}.py` — 三件套 (T2+)
- `mock_cli/{mock_kimi,mock_copilot}.py` — Phase 2 占位
- `mock_cli/README.md` — 行为契约文档
- `time_machine.py` — 时间冻结 (T4 S4)
- `mock_lark_event_stream.py` — Lark NDJSON event 注入 (T2-T3)
- `real_lark.py` — 真 Lark bot (T5 e2e)
- `nines_baseline.toml` — 各 patch 版本 nines snapshot baseline (T5 progression)
- `mock_arktower_db.py` (optional v0.3.0) — 离线 ArkTower SQLite snapshot 加载

### 11.3 测试 LOC 预算 (推断, 用于 v0.4.0 LOC 体检)

- v0.0.1: 788 行 (test) + 0 fixtures = 788
- v0.2.0: + 700 行 (~ 32 case × 22 行/case) = 1488
- v0.2.1: + 1300 行 (~ 60 case × 22 行) = 2788
- v0.2.2: + 1500 行 (chaos 12 + NFR 4 + T3 20 + real_cli 10, 各 ~ 30 行) = 4288
- v0.2.3: + 1400 行 (T4/T5 case 重) = 5688
- v0.3.0: + 1800 行 (Lark 专项 25 case 重 + HITL 全栈) = 7488
- v0.4.0: + 2000 行 (mutmut 修补 + 兜底 60-80 case) = ~ 9500 行 test code

src code 预估: v0.4.0 ≈ 8000-10000 行 src; **测试 / src 比 ≈ 1.0-1.2** (业界 0.5-1.5 中位; PopolaLoom 偏高反映 self-bootstrap + 自演化对覆盖率高要求)

---

## 12. 时间戳

> 文档时间戳: 2026-05-04
> 作者: L3 Task Agent T-test-matrix (Design 团队), devola-flow design-only workflow
> 上游引用: roadmap §3.1 (5 档 spec) + §3.2/§3.3/§3.4 (各 patch DoD) + §11 (devola-flow 双层 gate) + §12 (HITL handle-ability) + §12.8 (Lark 双向通道) + v0.2.0-plan.md §4 (5 Stage Owned files) + spec.md §3.4.1 (S1-S5 五例) + spec.md §6 (NFR-1..12) + 工作区规则 ("No Silent Failures" + "Mandatory Verification" + "Protected Branch Workflow" + "lark-cli 写入操作须追加来源标注")
> 下一步: 等待用户对 §0.2 版本测试目标 + §1 5 档 tier 范围边界 + §4 mock CLI 三段输出契约 + §10 chaos 12 模式 + §6 fail_under 递进 5 项书面 ack → 锁定 → v0.2.1 patch 启动后按本 spec 落 Tier 1+2 测试矩阵
