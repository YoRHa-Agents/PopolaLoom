# PopolaLoom · 9-day Implementation Plan v1.0

> 状态: ✅ R4 锁定后实施排期 (2026-05-03)
> 上游: `spec.md` v1.0 + ADR-0001 + ADR-0002
> 排期总览: Day 0 准备(ADR 锁定 + 仓库脚手架) + Day 1-9 build + 第 10 天 launch buffer
> 总实施周期: 10 个工作日(~2 周日历)
> 每日预计投入: 1 个全职开发者 ≈ 6 小时编码 + 2 小时验证 + 自动跑测试
> 维护者: PopolaLoom 项目组
> 关联文件: `spec.md` §3.4.1 self-bootstrap 5 例 / §6 NFR-1..12 / §7 安全红线 5 条

---

## 排期一览 (TL;DR)

| Day | 主题 | 主交付物 | 入仓 PR(预计) | 风险 | 通过验证 |
|---|---|---|---|---|---|
| **Day 0** | 准备 + ADR 锁定 + 脚手架 | popolaloom repo 骨架 + ArkTower editable install + ADR 锁定 issue | PR #1 (skeleton) | 同 org sibling-intent 沟通节奏不可控 | `pip install -e ../reference/ArkTower && pytest -q` 全绿 |
| **Day 1** | popolad daemon 骨架 + 7 原语 stub | unix socket server + ArkTower mount + 7 verbs stub + DevolaFlow primitive 引用 | PR #2 (popolad core) | 同 org schema migration 冲突 | `popola version` + `popola probe --all` 返回 200 |
| **Day 2** | 三 CLI adapter (cursor + claude + codex) + supervisor | systemd-run + tmux 双后端 + 预生成 session ID + NDJSON 解析 | PR #3 (adapters) | CLI 版本漂移(cursor 周更) | `popola dispatch --cli=claude "echo hello"` → attach 看到 stream-json |
| **Day 3** | LangGraph subgraph 编译 + SqliteSaver + NDJSON 旁路 | dev/test/verifier subgraph 模板 + thread_id=task_id 双轨 | PR #4 (graph) | LangGraph 1.x DeltaChannel 兼容 | popolad 重启后 `probe --task` 显示完整状态 |
| **Day 4** | popolaloom-mcp + 7 dispatch verbs + ArkTower 12 tool 转发 | stdio MCP server + form-mode elicitation + tool annotation | PR #5 (mcp) | MCP server-to-client push 限制 | Cursor IDE 配 mcp.json → 看到 19 个工具 |
| **Day 5** | popolaloom-skill + popolaloom-lark + IDE notify | SKILL.md + 4 scripts + lark-cli 桥 + notify-send 兜底 + 用户旅程 1+2 | PR #6 (skill+hitl) | Lark webhook callback 校验签名 | 端到端: 提交 plan → 关 IDE → 飞书收卡 → 选项 → resume |
| **Day 6** | 自演化测试 + Gen-Verifier + DevolaFlow gate | self-update workflow 内嵌 + 5 self-bootstrap 用例 4/5 PASS | PR #7 (evo) | 自演化误判 | `pytest tests/self_bootstrap/` ≥ 80% pass |
| **Day 7** | popolaloom-tui + popolaloom-web 增量页面 | Textual TUI 3 view + NiceGUI 4 个新页面 | PR #8 (frontend) | TUI 与 daemon 通信延迟 (NFR-2 ≤ 200ms) | `popola tui` 启动 ≤ 1s 看到 DAG |
| **Day 8** | 观测 + Prometheus + OTel + 12 metric | `/metrics` + OTel trace_id + journalctl 集成 + dashboard panel | PR #9 (obs) | OTel 流量与 daemon RSS (NFR-4 ≤ 200MB) | curl :9876/metrics 看到 12 metric, RSS < 200MB |
| **Day 9** | DEMO + verify_config + README + auto-merge gate 配置 | self-bootstrap 5/5 PASS + 5 verify_config 用例 + README 5min quickstart | PR #10 (release v0.1.0) | 第三方上手成本 | 第三方用户 5 分钟内装好跑通 demo |
| **Day 10** | launch buffer | NFR 全量回归 + 隐藏文档补全 + 半自动 self-evolution 触发 | (no PR) | -- | 12 NFR 全部达 Phase 1 目标值 |

ArkTower 复用 vs 自写比例(全期估算): **复用 ArkTower 30% (核心 / 任务池 / NiceGUI 框架) + 自写 60% (popolad daemon / 3 adapter / Lark bridge / TUI / MCP / skill / graph 编译) + 复用 LangGraph + lark-cli 10% (配置层)** (出处: spec.md §3.2 末尾)。

---

## Day 0 — 准备 + ADR 锁定 + 仓库脚手架

### 目标 (1 句)
让 ArkTower editable install 成功 + 5 个 ADR 决策被用户书面确认 + popolaloom repo 骨架进入 Git。

### 子任务

1. **ArkTower 协作意向 issue (sibling-intent)**
   - **owner module**: 跨仓
   - **acceptance**: ArkTower 仓库存在 issue *"PopolaLoom (sibling project in same org) intends to depend on ArkTower as the task-pool layer"*,描述 (a) 复用清单 (b) breaking change policy 期望 (c) 上游协作权限申请;同 org 维护者已 ack 至少一次
   - **出处**: 08 §10 Q1 + ADR-0001 Status section

2. **ArkTower editable install + 跑 293 测试**
   - **owner module**: dev env
   - **acceptance**: `cd /home/agent/reference/ArkTower && pip install -e ".[dev]" && pytest -q` 显示 293 passed / coverage ≥ 71%
   - **出处**: 08 §1.2 + ADR-0001 §"Decision"

3. **PopolaLoom 仓库脚手架**
   - **owner module**: `popolaloom/` package
   - **acceptance**: `popolaloom/{__init__.py, daemon/__init__.py, mcp/__init__.py, skill/, tui/, web/, adapters/{__init__.py,cursor.py,claude.py,codex.py}, lark/__init__.py, graph/__init__.py, core/__init__.py}` + `pyproject.toml`(hatchling) + `tests/` + `Makefile` + `.github/workflows/ci.yml` 镜像 ArkTower 模板
   - **出处**: 08 §6 keyfact-15 (ArkTower CI/Pages 已搭好可对照搭)

4. **ADR 锁定**
   - **owner module**: `.local/memory/specs/popolaloom/adrs/`
   - **acceptance**: `0001-arktower-as-task-pool-dependency.md` Status = `Accepted (with conditions)` (ArkTower issue 已发);`0002-langgraph-as-graph-engine.md` Status = `Accepted` (Q5 用户答案已锁定)
   - **出处**: 06 §0.0 Q1+Q5

5. **PR + auto-merge gate 配置**
   - **owner module**: `.github/`
   - **acceptance**: branch protection rule on `main`,要求(a) 至少 1 个 review 或 (b) auto-merge label + ArkTower 8-dim 自评 ≥ 0.85 (复用 ArkTower nines.toml 思路);Protected Branch 规则强制 (出处: spec.md §7.3)

### ArkTower 复用 vs 自写比例
- 0% 自写功能代码,**100% 准备工作**(install + sibling issue + skeleton + ADR 锁定 + branch rules)。

### 风险与 fallback
- **风险**: ArkTower 维护者沟通延迟 → ADR-0001 临时回退到 vendor checkout 方案 (出处: ADR-0001 Alternatives §4)
- **fallback**: 若 sibling-intent issue 24h 内无响应,先以本地 editable install 启动 Day 1,issue 在后台 keep-open

### 当日 verify 命令
```bash
cd /home/agent/workspace/PopolaLoom
pip install -e "../../reference/ArkTower[dev]" -e ".[dev]"
pytest /home/agent/reference/ArkTower/tests/ -q
python -c "import arktower; print(arktower.__version__)"
ls .local/memory/specs/popolaloom/adrs/
git log --oneline -5
```

---

## Day 1 — popolad daemon 骨架 + 7 原语 stub + DevolaFlow primitive 引用

### 目标 (1 句)
让 `popolad` daemon 进程可启动 + unix socket 接收 7 个原语 stub + DevolaFlow 14 primitives 的类型契约可 import。

### 子任务

1. **popolad 进程主入口 (asyncio + uvicorn)**
   - **owner module**: `popolaloom/daemon/main.py`
   - **acceptance**: `python -m popolaloom.daemon` 启动后:(a) 监听 `~/.popola/popolad.sock` (b) PID 写到 `/run/user/$UID/popola.pid` (c) 信号处理 `SIGTERM/SIGINT` 干净退出 (d) `popola version` RPC 调用返回 `{"version":"0.1.0"}` 200ms 内
   - **出处**: 06 §"R3 7-Day MVP" Day-1, spec.md §3.1 L2, NFR-1 (启动 ≤ 2s)

2. **ArkTower 服务 mount 进同一 ASGI 树**
   - **owner module**: `popolaloom/daemon/mount_arktower.py`
   - **acceptance**: popolad ASGI app `mount("/arktower", arktower.api.create_app())`;访问 `http://127.0.0.1:8765/arktower/api/tasks` 返回空 list;访问 `/api/popola/version` 返回 popolad 版本
   - **出处**: 08 §7.2 row "api" + ADR-0001 Decision

3. **7 个 Conductor 原语的 stub + Pydantic schema**
   - **owner module**: `popolaloom/daemon/primitives/{dispatch,attach,relay,supervise,federate,handoff,probe}.py`
   - **acceptance**: 每个原语:(a) 输入 / 输出 Pydantic v2 model 定义 (b) RPC handler 返回 NotImplementedError 但 schema validation 走通 (c) unit test 覆盖每个 schema 的正例 + 反例
   - **出处**: spec.md §4.2 每行 schema

4. **DevolaFlow 14 primitives 类型引用 + 复用契约**
   - **owner module**: `popolaloom/core/devola_primitives.py`
   - **acceptance**: `from popolaloom.core.devola_primitives import research, plan, implement, ...` 可正常 import (实际是从 DevolaFlow 包重导出)
   - **出处**: 06 §5.1 直接继承

5. **NDJSON event log 写入器 + CloudEvents 1.0 信封**
   - **owner module**: `popolaloom/daemon/event_log.py`
   - **acceptance**: `event_log.append({"type":"plan.created", ...})` → `~/.popola/events/<plan_id>.jsonl` 看到 CloudEvents 1.0 完整字段 (id/source/specversion/type/time/data);单条写 < 5 ms (NFR-3)
   - **出处**: spec.md §3.5.5 + 05 §"推荐的事件流格式"

### ArkTower 复用 vs 自写比例
- **复用 ArkTower**: ASGI app mount + Pydantic schema 习惯 + database connection (≈ 200 LOC 节省)
- **自写**: ~ 800 LOC (daemon main + 7 原语 stub + event log + ASGI factory)

### 风险与 fallback
- **风险**: ArkTower `005_popolaloom_extensions.sql` migration 与 ArkTower 自带 4 个 migration 冲突
- **fallback**: 暂时不挂 popolaloom-specific 表,先用 ArkTower 现有 `Task.parameters` JSON 字段塞 PopolaLoom 顶层字段(出处: spec.md §3.5.1 末尾)

### 当日 verify 命令
```bash
python -m popolaloom.daemon &  # background
sleep 2
test -S ~/.popola/popolad.sock && echo "socket OK"
test -f /run/user/$UID/popola.pid && echo "pid OK"
popola version  # 通过 unix socket 调 popolad RPC
popola probe --all  # 返回空 plan list
pytest tests/test_primitives_schema.py -v
journalctl --user -u popola.service --since "5 minutes ago" | tail
```

---

## Day 2 — Cursor + Claude + Codex adapter + 双 supervisor 后端

### 目标 (1 句)
3 个 CLI 都可以被 popolad 通过 systemd-run / tmux 派发,且预生成 session ID + NDJSON stream-json 输出全程通过 popolad 接收。

### 子任务

1. **Cursor adapter**
   - **owner module**: `popolaloom/adapters/cursor.py`
   - **acceptance**: 实现 `CursorAdapter` 类,`spawn(task)` 调用顺序: (a) `cursor-agent create-chat` → 拿 chatId (b) systemd-run --user --scope --unit=popola-<task_id> -- cursor-agent --print "..." --resume <chatId> --output-format stream-json --stream-partial-output --workspace <cwd> --approve-mcps --trust (c) 返回 native_session_id=chatId + supervisor_unit;`status()` 通过 systemctl --user status 取 unit 状态 + tail event log
   - **出处**: 02 §"Cursor Agent CLI" + 02 §附录 A 模板

2. **Claude adapter**
   - **owner module**: `popolaloom/adapters/claude.py`
   - **acceptance**: 实现 `ClaudeAdapter` 类,spawn 流程:(a) `uuidgen` 预生成 UUID (b) systemd-run --user --scope --unit=popola-<task_id> -- claude --bare --session-id <UUID> --output-format stream-json --include-partial-messages --include-hook-events --strict-mcp-config --mcp-config /tmp/popola-<UUID>.json --max-budget-usd 5 -p "..." (c) 返回 native_session_id=UUID;hook 注入 PreToolUse hook 走 popolad webhook
   - **出处**: 02 §"Claude Code"

3. **Codex adapter (走 app-server WS 专路径)**
   - **owner module**: `popolaloom/adapters/codex.py`
   - **acceptance**: 实现 `CodexAdapter`:(a) 启动一个共享的 `codex app-server --listen ws://127.0.0.1:7300 --ws-auth capability-token --ws-token-file /run/popola/token` (b) 派发任务通过 WS,`-c session.id="$UUID"` 预生成 ID (c) `--sandbox workspace-write --ask-for-approval never` 默认值 (d) ⚠ 加 600s timeout 兜底 (issue #14470)
   - **出处**: 02 §"OpenAI Codex"

4. **双 supervisor 后端抽象**
   - **owner module**: `popolaloom/daemon/supervisor.py`
   - **acceptance**: `Supervisor` 接口定义 `start(cmd, env, cwd) -> unit` / `status(unit) -> Status` / `tail(unit, since) -> EventStream` / `stop(unit, signal)`;两个实现 `SystemdRunSupervisor` 和 `TmuxSupervisor`;运行时根据 `which systemd-run` 自动选择,fallback 到 tmux,再 fallback 到 nohup
   - **出处**: 02 §"PopolaLoom 派发抽象建议-3", 06 D3

5. **dispatch 原语接通**
   - **owner module**: `popolaloom/daemon/primitives/dispatch.py`
   - **acceptance**: `popola dispatch --cli=claude --prompt="echo hello"` → 返回 task_id;`popola attach <id>` → tail 到 stream-json 输出末尾包含 "echo hello" 的 final message
   - **出处**: spec.md §4.2 dispatch 原语 + spec.md §3.3 happy-path 序列图 T+10–T+14

### ArkTower 复用 vs 自写比例
- **复用 ArkTower**: ArkTower TaskService.create_task → 生成 task_id (≈ 50 LOC 节省)
- **自写**: ~ 1200 LOC (3 adapter × ~ 300 LOC + supervisor 抽象 + dispatch 接通)

### 风险与 fallback
- **风险**: cursor-agent 周更导致 stream-json 字段变化 → adapter 解析挂
- **fallback**: 实现 `cursor_agent_version_lock` 检查,启动时调 `cursor-agent --version`,若与已测试清单不符 → 警告 + 进 degraded 模式(只跑 final-message-only,不解析 stream)

### 当日 verify 命令
```bash
popola dispatch --cli=claude --prompt="say hello in JSON"
TASK_ID=$(popola list --filter=running --json | jq -r '.[0].id')
popola attach $TASK_ID --follow &
sleep 30  # 等任务完成
systemctl --user status popola-$TASK_ID
ls ~/.popola/events/
test -f ~/.popola/events/*.jsonl && wc -l ~/.popola/events/*.jsonl
# 重复 cursor / codex
popola dispatch --cli=cursor --prompt="..." && sleep 30
popola dispatch --cli=codex  --prompt="..." && sleep 30
pytest tests/test_adapters.py -v
```

---

## Day 3 — LangGraph subgraph 编译 + SqliteSaver + NDJSON 旁路

### 目标 (1 句)
将 plan 编译为 LangGraph StateGraph,内层装 dev/test/verifier 子图,SqliteSaver 与 NDJSON 同时写,popolad 重启后通过 thread_id 完整恢复。

### 子任务

1. **State schema + 主图骨架**
   - **owner module**: `popolaloom/graph/main_graph.py`
   - **acceptance**: 定义 `PlanState` TypedDict (status / dispatch_results / iter / score / handoff_envelopes / dag_snapshot);`build_main_graph(plan: ConductorDispatch) -> CompiledStateGraph` 返回编译好的 graph;graph 的 nodes 一一对应 plan_dag.nodes
   - **出处**: 03 §3.1 + 03 §3.5

2. **dev↔test SCC subgraph 模板 (Gen-Verifier)**
   - **owner module**: `popolaloom/graph/subgraphs/gen_verifier.py`
   - **acceptance**: 实现 `build_gen_verifier_subgraph(spec) -> CompiledStateGraph`:(a) nodes = `dev` `test` `verifier` `give_up` (b) edges START→dev→test→verifier (c) verifier conditional_edges: score≥0.85 → END / iter≥10 → give_up → END / else → dev (cycle) (d) max_iter=10 兜底
   - **出处**: 03 §6 模式 B + 03 §7.3 实现示例 + ADR-0002 Decision

3. **SqliteSaver 集成**
   - **owner module**: `popolaloom/graph/persistence.py`
   - **acceptance**: `from langgraph.checkpoint.sqlite import SqliteSaver`;创建 `~/.popola/state.sqlite`;每个 plan thread_id = plan_id;运行时 popolad 用 `graph.invoke(initial_state, config={"configurable":{"thread_id":plan_id}})` 跑;每个 super-step 自动落盘
   - **出处**: 03 §3.3 + 03 §5.5

4. **NDJSON 旁路写**
   - **owner module**: `popolaloom/graph/event_listener.py`
   - **acceptance**: 通过 LangGraph stream API 监听每个 super-step 完成事件,同时 append 到 `~/.popola/events/<plan_id>.jsonl` (CloudEvents 1.0 信封) 与 ArkTower TaskEvent (双轨)
   - **出处**: 03 §5.5 双轨持久化

5. **冷启动恢复**
   - **owner module**: `popolaloom/daemon/recovery.py`
   - **acceptance**: popolad 启动时:(a) 扫描 `~/.popola/state.sqlite` 找所有非 terminal 状态的 thread (b) 对每个 thread 调用 `graph.invoke(None, config=...)` 接续运行 (c) 通过 NDJSON event log 自动重建 attach session;模拟 SIGKILL → systemd 重启 → in-flight task 自动恢复
   - **出处**: spec.md §3.4 末尾"冷启动恢复"序列, 03 §3.3 pending writes

### ArkTower 复用 vs 自写比例
- **复用 LangGraph**: 100% 图引擎(~ 1000 LOC 等价节省)
- **自写**: ~ 600 LOC (graph 编译适配层 + persistence + event listener + recovery)

### 风险与 fallback
- **风险**: LangGraph 1.x DeltaChannel 与 SqliteSaver 序列化兼容
- **fallback**: 退回 langgraph 0.4.x checkpoint API,Phase 2 再升级

### 当日 verify 命令
```bash
popola dispatch_plan_yaml plan.yaml  # 提交一个含 dev↔test cycle 的 plan
TASK_ID=...
sleep 60
sqlite3 ~/.popola/state.sqlite "SELECT thread_id, checkpoint_id FROM checkpoints WHERE thread_id='$TASK_ID' LIMIT 5;"
# 模拟崩溃
pkill -9 popolad
sleep 1
systemctl --user start popola.service
sleep 2
popola probe --task $TASK_ID  # 应该看到状态完整重建
pytest tests/test_graph_recovery.py -v
```

---

## Day 4 — popolaloom-mcp + 7 dispatch verbs + ArkTower 12 tool 转发

### 目标 (1 句)
popolaloom-mcp stdio server 暴露 7 个 dispatch verbs + 转发 ArkTower 12 tool,使 Cursor/Claude IDE 通过 mcp.json 配置后直接看到 19 个工具。

### 子任务

1. **popolaloom-mcp stdio server 骨架**
   - **owner module**: `popolaloom/mcp/server.py`
   - **acceptance**: `python -m popolaloom.mcp.server --stdio` 启动一个 MCP stdio server (使用 `mcp` SDK Python);通过 STDIN/STDOUT 接收 JSON-RPC initialize 请求,返回 capabilities;tools/list 返回 19 个工具签名
   - **出处**: 02 §"MCP 作为派发协议"

2. **7 dispatch verbs 实现**
   - **owner module**: `popolaloom/mcp/tools/popola_*.py`
   - **acceptance**: 实现 `popola_submit_plan / popola_list_tasks / popola_get_status / popola_attach / popola_supply_feedback / popola_inject_subtask / popola_cancel`;每个工具 (a) 调用 popolad RPC (unix socket) (b) tool annotation 正确 (`destructiveHint=true` 给 cancel,`idempotentHint=true` 给 list/get/probe) (c) 输入输出 schema 用 Pydantic
   - **出处**: 05 §"必须实现的 7 个核心交互动词" + 02 §MCP tool annotation

3. **ArkTower 12 tool 转发**
   - **owner module**: `popolaloom/mcp/arktower_relay.py`
   - **acceptance**: `from arktower.mcp.server import TOOL_DEFINITIONS, TOOL_HANDLERS`;在 popolaloom-mcp 启动时把 ArkTower 12 个 tool 注册到同一 MCP server;tools/list 看到 19 = 7 + 12 个工具;调用 ArkTower tool 时直接 forward 到 ArkTower handler
   - **出处**: 08 §6 keyfact-6 + 08 §8.4 import 列表

4. **MCP form-mode elicitation (handoff)**
   - **owner module**: `popolaloom/mcp/elicitation.py`
   - **acceptance**: 当 `popola_get_status` 返回的 status 含 `pending_interrupts` 时,自动调 MCP `elicitation/create` (form mode + enum schema) 把 pending interrupts 一并弹给宿主 IDE;用户回答后调 `popola_supply_feedback`
   - **出处**: 05 §"必须避免的 5 个失败模式"-1 + lark/ide 双通道

5. **mcp.json 模板 + 一键安装脚本**
   - **owner module**: `popolaloom/mcp/install.sh`
   - **acceptance**: `popola install-mcp [--ide=cursor|claude]` 自动写 `~/.cursor/mcp.json` 或 `~/.claude/settings.json` 项目级配置;Cursor / Claude 重启后能看到 popolaloom-mcp 19 个工具
   - **出处**: 05 §"候选 C: Hybrid"

### ArkTower 复用 vs 自写比例
- **复用 ArkTower MCP 12 tool**: ~ 800 LOC 节省 (12 × ~70 LOC)
- **自写**: ~ 800 LOC (7 dispatch verbs + relay layer + elicitation + install script)

### 风险与 fallback
- **风险**: ArkTower MCP server 与 PopolaLoom MCP server 都用 mcp SDK,可能 port / handler conflict
- **fallback**: Phase 1 让两个 server 独立 stdio,popolaloom-mcp 通过 socket forward 到 ArkTower MCP(出处: ADR-0001 Status section)

### 当日 verify 命令
```bash
python -m popolaloom.mcp.server --stdio < /dev/null > out.json &
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python -m popolaloom.mcp.server --stdio
# 安装到 Cursor
popola install-mcp --ide=cursor
# (重启 Cursor IDE 后) 在 Composer 输入: "调 popola_list_tasks"
# 验证 IDE Agent 能看到工具并调用
pytest tests/test_mcp_tools.py -v
```

---

## Day 5 — popolaloom-skill + popolaloom-lark + IDE notify + 用户旅程 1+2

### 目标 (1 句)
完成 Skill 入口 + Lark HITL 桥 + IDE 桌面通知,用户旅程 1 (提交 plan + 关 IDE + 重开查看) 与场景 2 (handoff 反馈) 端到端跑通。

### 子任务

1. **SKILL.md + 4 scripts**
   - **owner module**: `popolaloom/skill/popola-loom/{SKILL.md,scripts/{submit,status,attach,feedback}.sh}`
   - **acceptance**: SKILL.md frontmatter `name: popola-loom`、`triggers: ["dispatch","orchestrate","multi-task","派发","调度"]`、`description: <30 行说明文>`;4 个 shell script wraps `popola` CLI;同一份 skill 用符号链接装到 `~/.cursor/skills/popola-loom/` 和 `~/.claude/skills/popola-loom/`
   - **出处**: 05 §"候选 C: Hybrid" + Anthropic Agent Skills overview

2. **popolaloom-lark 桥 (订阅 + 推送)**
   - **owner module**: `popolaloom/lark/bridge.py`
   - **acceptance**: 实现 `LarkBridge` 类:(a) 启动时通过 `arktower.core.event_bus.EventBus.subscribe(TASK_TRANSITION_EVENT, on_input_required)` 订阅 (b) 当 `to_status == INPUT_REQUIRED` 时,构造互动卡 (Block Kit 风) → `subprocess.run(["lark-cli", "im", "+send", "--as", "bot", "--chat-id", chat_id, "--card", json.dumps(card)])` (c) 同时调 `lark-cli task +create` 兜底任务收件箱 (d) 卡片附末尾"---\n本消息由飞书工具 Lark-Cli 发送"
   - **出处**: spec.md §3.4 + 工作区规则 "lark-cli 写入操作须追加来源标注" + 08 §6 keyfact-4 EventBus hook

3. **Lark webhook callback 接收 (event consume)**
   - **owner module**: `popolaloom/lark/event_consumer.py`
   - **acceptance**: popolad 启动时 spawn 一个后台子进程 `lark-cli event consume <event-key> --max-events 999999` 输出 NDJSON 到 popolad event_consumer 队列;popolad 解析 button_value + 校验 signed action_id (HMAC: secret + plan_id + ts + nonce) → 调用 `Command(resume=button_value)` 喂回 LangGraph
   - **出处**: lark-event SKILL.md + spec.md §3.5.4 LarkInterrupt schema

4. **IDE 桌面通知 (notify-send / osascript)**
   - **owner module**: `popolaloom/daemon/notify.py`
   - **acceptance**: 跨平台兼容: Linux 用 `notify-send`,macOS 用 `osascript -e 'display notification ...'`,Windows 用 PowerShell `BurntToast` (Phase 2 再做);触发条件 = `to_status == INPUT_REQUIRED` 同时(NFR-6 ≤ 1s 投递)
   - **出处**: 05 §"Notification 推 vs 拉"

5. **端到端场景 1 + 2**
   - **owner module**: `tests/self_bootstrap/test_scenario_1.py / test_scenario_4.py`
   - **acceptance**: scenario_1 = "提交 4-task plan + 关 IDE + 重开 Cursor + 通过 popola_get_status 重建上下文 + 看到完整状态" PASS;scenario_4 = "T-X 进 INPUT_REQUIRED + 飞书收卡 + button click + Command(resume) + T-X 完成" PASS
   - **出处**: spec.md §3.4.1 S1 + S4

### ArkTower 复用 vs 自写比例
- **复用 ArkTower**: EventBus hook 完美吻合(0 改动 ArkTower)
- **复用 lark-cli**: 100% Lark 操作走 lark-cli skill,**0 行 SDK 代码**
- **自写**: ~ 600 LOC (Lark bridge + event consumer + notify + skill scripts)

### 风险与 fallback
- **风险**: Lark 互动卡 button click 回调签名校验复杂,误拒可能丢用户决策
- **fallback**: Phase 1 用最简版"5 分钟有效期 + 单调时间戳",生产再加 nonce 防重放

### 当日 verify 命令
```bash
# 安装 skill
popola install-skill --ide=cursor --ide=claude
# 触发场景 1
popola dispatch_plan_yaml tests/fixtures/plan-4-tasks.yaml
PLAN_ID=...
# 模拟关 IDE: pkill cursor-agent / claude (popolad 仍跑)
sleep 30
# 重开 IDE,在 Composer: "查看 PopolaLoom 的最新 plan"
# (人工或 SDK 模拟): 验证返回 4 个 task 状态 + plan_id 一致
# 触发场景 4
popola dispatch --cli=claude --prompt="ask user which DB"
sleep 5
# 检查飞书消息
lark-cli im +chat-messages-list --as user --chat-id $POPOLA_LARK_CHAT_ID --limit 3
# 模拟点击按钮 (用 lark-cli 发送伪造 event,或真人点)
sleep 5
popola probe --task <task_id>  # 应看到状态变 IN_PROGRESS 后 completed
pytest tests/self_bootstrap/test_scenario_1.py tests/self_bootstrap/test_scenario_4.py -v
```

---

## Day 6 — 自演化测试 + Gen-Verifier + DevolaFlow gate 复合分

### 目标 (1 句)
复用 DevolaFlow `self-update` workflow + Gen-Verifier subgraph + 8-dim 自评测,跑通 5 个 self-bootstrap 用例至少 4/5 PASS。

### 子任务

1. **DevolaFlow `self-update` workflow 内嵌**
   - **owner module**: `popolaloom/daemon/self_update.py`
   - **acceptance**: 调用 `devolaflow self-update --target-repo .` 触发自演化;触发条件 = `git status` clean + 当前在 feature branch;workflow 复用 DevolaFlow 现有 4 stage (research → design → implement → review) 注入 PopolaLoom 自身 plan
   - **出处**: 06 §"R3 7-Day MVP" Day-6 + DevolaFlow SKILL.md `self-update` workflow

2. **Gen-Verifier subgraph 接入 DevolaFlow gate composite**
   - **owner module**: `popolaloom/graph/subgraphs/gate_decision.py`
   - **acceptance**: verifier 节点输出 `{score: composite_score, profile: standard, blockers: [], findings_by_severity: {...}}`;composite_score 公式 = 0.30 × test_quality + 0.30 × code_review + 0.20 × architecture + 0.20 × benchmark (出处: spec.md §7.3 ev2);≥ 0.85 + 0 blocker → PASS,否则进 dev 重写
   - **出处**: 06 §6.1 + DevolaFlow SKILL.md `references/decomposition-gate.md`

3. **8-dim 自评测复用 ArkTower**
   - **owner module**: `popolaloom/evaluation/popola_dimensions.py`
   - **acceptance**: 复用 `arktower.evaluation.runner.EvalRunner` 框架,但替换维度为 PopolaLoom 特有的 8 维(`dispatch_isolation / cycle_convergence / hitl_latency / attach_correctness / cross_cli_handoff / single_threaded_writes / event_log_completeness / token_budget_compliance`);跑 self-eval 输出 `nines.toml` 兼容报告
   - **出处**: 08 §6 keyfact-11 + 06 §"R3 7-Day MVP" Day-6

4. **5 个 self-bootstrap 测试用例完整跑**
   - **owner module**: `tests/self_bootstrap/test_scenario_{1..5}.py`
   - **acceptance**: 5 个用例至少 4/5 PASS (Day 6 目标),Day 9 必须 5/5 PASS
   - **出处**: spec.md §3.4.1 表

5. **PR auto-merge gate 配置 (Protected Branch 安全)**
   - **owner module**: `.github/workflows/auto_merge.yml`
   - **acceptance**: 工作流检测 PR (a) 仅触及 `popolaloom/*` (b) 8-dim ≥ 0.85 (c) 0 blocker (d) multi-CLI peer review 双 PASS (e) coverage 不下降;5 条 AND 条件全部满足 + auto-merge label → 自动 squash merge
   - **出处**: spec.md §7.3 + 工作区规则 "Protected Branch Workflow"

### ArkTower 复用 vs 自写比例
- **复用 ArkTower 评测框架**: ~ 500 LOC 节省
- **复用 DevolaFlow self-update**: ~ 300 LOC 节省
- **自写**: ~ 600 LOC (gate_decision adapter + 8 PopolaLoom-specific 维度 + 5 test scenario + auto-merge yaml)

### 风险与 fallback
- **风险**: 自演化 PR 误将 cycle 引入 main → 自动 merge 后 user 实际 push 失败
- **fallback**: 自演化 PR 必须经过 5 条 AND 条件 + Protected Branch 规则;**任何 Blocker/Critical finding 一律阻断**;且 auto-merge 仅作用于 `popolaloom/*` 路径(即不修改 `arktower/`、`devolaflow/`、CI 配置)

### 当日 verify 命令
```bash
# 触发自演化
git checkout -b feature/self-evolve-test
devolaflow self-update --target-repo . --plan "add 1 small primitive"
# 等到 PR 自动开 + auto-merge gate check
gh pr list --label auto-merge
gh pr checks <PR-NUMBER>
# 跑 self-bootstrap suite
pytest tests/self_bootstrap/ -v --cov=popolaloom
# 跑 8-dim 自评
popola eval run --output ./nines-report.toml
cat nines-report.toml | grep overall
# 必须 ≥ 0.85
```

---

## Day 7 — popolaloom-tui + popolaloom-web 增量 4 页

### 目标 (1 句)
Textual TUI 提供 DAG 视图 / log tail / interrupt 列表三个面板,NiceGUI dashboard 在 ArkTower 5 页之外挂载 popola 自有 4 页。

### 子任务

1. **Textual TUI 主程序**
   - **owner module**: `popolaloom/tui/app.py`
   - **acceptance**: `popola tui` 启动后:(a) 左面板 = plan list (Tree)(b) 上中面板 = DAG 节点状态色彩可视化 (c) 下中面板 = log tail (textual Log widget)(d) 右面板 = pending interrupt 列表 + supply feedback action;k/j 上下、Enter 进入详情、ESC 返回、F1 帮助
   - **出处**: 06 D1 R4 增项 + 01 §5.1 sfw/loom 借鉴

2. **WebSocket 订阅事件流到 TUI**
   - **owner module**: `popolaloom/tui/ws_client.py`
   - **acceptance**: TUI 启动时连接 popolad WebSocket(`ws://127.0.0.1:8765/ws/events`),订阅当前 selected plan 的 NDJSON event 流;新事件 < 1s 内出现在 TUI;NFR-2 attach 延迟 ≤ 200 ms 在此通道也满足
   - **出处**: NFR-2 + 03 §5.5

3. **NiceGUI 4 个增量页面**
   - **owner module**: `popolaloom/web/pages/{runtime_supervisor,attach_console,hitl_inbox,federate_consensus}.py`
   - **acceptance**:
     - `runtime_supervisor.py` = popolad daemon 健康看板 (RSS / 子进程 unit 列表 / restart count)
     - `attach_console.py` = 每个 task 一行 + 点击进入 attach console (NDJSON live tail in browser)
     - `hitl_inbox.py` = 全部 INPUT_REQUIRED 状态 task + 直接在浏览器选枚举 supply_feedback
     - `federate_consensus.py` = federate 原语的 voting 结果可视化 (柱状图)
   - **出处**: 08 §7.2 row "web" + spec.md §3.2 row popolaloom-web

4. **i18n 双语 (EN/ZH)**
   - **owner module**: `popolaloom/web/i18n_extra.py`
   - **acceptance**: 复用 `arktower.web.i18n.t / set_lang`,在其上注册 popola 特有的 ~ 50 个翻译条目 (`popola.runtime_supervisor / popola.attach_console / ...`);用户切换 lang 自动应用
   - **出处**: 08 §7.2 row "web" 末尾

5. **TUI ↔ Web 用户体验对齐**
   - **owner module**: `popolaloom/tui/web_bridge.py`
   - **acceptance**: TUI 与 Web 看到的 plan / task 状态实时一致(同源 WebSocket);TUI 内 hotkey `w` 在浏览器自动打开同一 plan 的 web 页面 URL
   - **出处**: 推断 + spec.md §3.2

### ArkTower 复用 vs 自写比例
- **复用 ArkTower NiceGUI 框架**: ~ 1500 LOC 节省 (theme / dashboard / 5 个现有 page)
- **自写**: ~ 1200 LOC (Textual TUI 700 + web 4 页 500)

### 风险与 fallback
- **风险**: Textual TUI 与 daemon 通信延迟可能超 200 ms (NFR-2)
- **fallback**: 改用直接 unix socket SUB/PUB 而非 WebSocket;Phase 2 再升 ZeroMQ

### 当日 verify 命令
```bash
popola tui &  # 后台启动 TUI
# 在 TUI 内浏览
popola dispatch --cli=claude --prompt="say hello" &
# 应该实时看到任务出现在 TUI
# 浏览器
xdg-open http://127.0.0.1:8765/popola/runtime_supervisor
xdg-open http://127.0.0.1:8765/popola/attach_console
xdg-open http://127.0.0.1:8765/popola/hitl_inbox
# 从 TUI 按 'w' 自动跳到当前 plan 的 web 页
```

---

## Day 8 — 观测层: Prometheus + OTel + 12 metric

### 目标 (1 句)
popolad 暴露 `/metrics` (12 metric) + OpenTelemetry trace_id 贯穿到子 CLI + journalctl --user 集成。

### 子任务

1. **`/metrics` 端点**
   - **owner module**: `popolaloom/daemon/metrics.py`
   - **acceptance**: HTTP `127.0.0.1:9876/metrics` 返回 Prometheus 文本格式;12 个 metric 全部上线 (出处: spec.md §8.2)
   - **出处**: spec.md §8.2

2. **OpenTelemetry trace_id 贯穿**
   - **owner module**: `popolaloom/daemon/otel.py`
   - **acceptance**: 每个 plan 起一个 root span,trace_id 注入到子 CLI 的 env (`OTEL_TRACEPARENT`) 或命令行;子 CLI 自身的 span 用 `traceparent` 关联;Copilot 的 OTel 流(`COPILOT_OTEL_FILE_EXPORTER_PATH`)被 popolad 自动消费
   - **出处**: spec.md §8.2 + 02 §"Copilot 内置 OTel"

3. **journalctl --user 集成**
   - **owner module**: `popolaloom/daemon/journal.py`
   - **acceptance**: popolad 自身 unit `popola.service`,日志通过 `journal-send` 发到 user journal;每个 task unit `popola-<task_id>.service` (来自 systemd-run --unit) 自动入 journal;`journalctl --user -u popola-<task_id>` 可以看到完整 stdout/stderr
   - **出处**: 02 §"哪些 CLI 天然支持 daemon"-systemd-run

4. **Web dashboard panel**
   - **owner module**: `popolaloom/web/pages/observability.py`
   - **acceptance**: NiceGUI 增加第 5 个 popola-specific 页 `observability`,展示当前 metric 值 + 历史 trace 列表 + journalctl tail
   - **出处**: 08 §7.2 row "web"

5. **OTel collector 配置 (Phase 1 仅 file exporter,Phase 2 接 otlp)**
   - **owner module**: `popolaloom/daemon/otel_config.py`
   - **acceptance**: 默认 file exporter 写到 `~/.popola/otel/spans.jsonl` + `metrics.jsonl`,Phase 2 通过 `~/.popola/config.toml` 改 endpoint
   - **出处**: 02 §"PopolaLoom 接入要点-1"

### ArkTower 复用 vs 自写比例
- **复用 OpenTelemetry SDK**: ~ 800 LOC 节省
- **自写**: ~ 500 LOC (12 metric 实现 + trace 注入逻辑 + journal-send wrapper + observability 页面)

### 风险与 fallback
- **风险**: 观测层流量 + popolad RSS 突破 NFR-4 (≤ 200 MB 空载)
- **fallback**: file exporter 限速 + 历史 metric 保留 1 小时 (Phase 2 接外部 collector 后再放开)

### 当日 verify 命令
```bash
curl http://127.0.0.1:9876/metrics | grep popola_
# 应看到 12 个 metric
# 触发任务并 trace
popola dispatch --cli=claude --prompt="hello" &
sleep 30
ls ~/.popola/otel/
cat ~/.popola/otel/spans.jsonl | jq '.trace_id' | sort -u | wc -l  # 应 ≥ 1
# RSS 检查
ps -o rss= -p $(pgrep popolad) | awk '{print $1/1024 " MB"}'
# 应 < 200
```

---

## Day 9 — README + DEMO + verify_config 5 例 + release v0.1.0

### 目标 (1 句)
第三方用户能在 5 分钟内装好 PopolaLoom 并跑 demo,5 个 self-bootstrap 全 PASS,5 个 verify_config 全 PASS,release v0.1.0 tag。

### 子任务

1. **README.md 5 分钟 quickstart**
   - **owner module**: 仓库根 `README.md`
   - **acceptance**: README 包含 (a) 一句话定义 (b) 5 步安装(`pip install popolaloom + pip install arktower + popola init + popola install-skill + popola tui`) (c) 一个 demo (PopolaLoom 自己派 cursor 改自己一个小 feature) (d) 5 分钟 走通 (e) 链接到 spec.md / ADR / contributing
   - **出处**: 06 §"R3 7-Day MVP" Day-7

2. **DEMO 任务: 自演化 PR 端到端**
   - **owner module**: `examples/demo_self_evolve.py`
   - **acceptance**: 一个 Python 脚本,执行后 PopolaLoom 派发 cursor 给自己加一个小 feature(如 "在 popola probe 输出添加 emoji 状态指示"),全过程跑通 + 输出 PR URL + auto-merge gate 通过
   - **出处**: 06 §6.1 + spec.md §3.4.1 S5 跨 CLI 变种

3. **5 个 verify_config 用例**
   - **owner module**: `tests/verify/test_verify_{visual,ac,interaction,a11y,token_cost}.py`
   - **acceptance**: 5 个 verify_config 用例覆盖 5 个 verify 维度 (visual / AC / interaction / a11y / token-cost),全部 PASS;复用 DevolaFlow `verify` primitive 的 verify_config 模板
   - **出处**: 06 §"R3 7-Day MVP" Day-7 + DevolaFlow `references/meta-framework.md` §2.10 verify

4. **5 个 self-bootstrap 用例 5/5 PASS**
   - **owner module**: `tests/self_bootstrap/`
   - **acceptance**: Day 6 余下的 1 个修完,5/5 PASS;NFR-5 ≥ 99% 跨终端存活实测达标
   - **出处**: spec.md §3.4.1

5. **release v0.1.0 + GitHub release notes**
   - **owner module**: 仓库根
   - **acceptance**: `git tag v0.1.0` + GitHub release notes (CHANGELOG.md + 主要变更 + ArkTower / DevolaFlow / LangGraph 依赖版本表) + Pages 站点上线(借鉴 ArkTower docs/)
   - **出处**: 08 §6 keyfact-15 ArkTower CI/Pages 模板

### ArkTower 复用 vs 自写比例
- **复用 ArkTower release 模板**: ~ 200 LOC 节省 (CI/release.yml + Pages workflow 几乎照搬)
- **自写**: ~ 400 LOC (README + 5 verify test + demo script + 1 修 self-bootstrap)

### 风险与 fallback
- **风险**: 第三方用户上手时遇到 ArkTower editable install 路径问题
- **fallback**: README 中明确写 *"建议先 git clone https://github.com/YoRHa-Agents/ArkTower 到 ../reference/ArkTower 然后 pip install"*;Phase 2 推 PyPI 发包

### 当日 verify 命令
```bash
# 模拟第三方用户 (用全新 docker container 或全新 venv)
python -m venv /tmp/popola_test && source /tmp/popola_test/bin/activate
git clone https://github.com/YoRHa-Agents/ArkTower /tmp/ArkTower
pip install -e /tmp/ArkTower
git clone https://github.com/YoRHa-Agents/PopolaLoom /tmp/PopolaLoom
pip install -e /tmp/PopolaLoom
popola init
popola install-skill --ide=cursor
popola install-mcp --ide=cursor
# 应在 5 分钟内
# 跑 demo
python /tmp/PopolaLoom/examples/demo_self_evolve.py
# 5 self-bootstrap
pytest /tmp/PopolaLoom/tests/self_bootstrap/ -v
# 5 verify
pytest /tmp/PopolaLoom/tests/verify/ -v
# 全 PASS
git -C /tmp/PopolaLoom tag v0.1.0
```

---

## Day 10 — Launch buffer (NFR 全量回归 + 自演化半自动触发)

### 目标 (1 句)
12 个 NFR 全部跑实测达 Phase 1 目标值 + 自演化 workflow 半自动触发跑一次完整 self-bootstrap PR + 隐藏文档(Troubleshooting / FAQ / 设计哲学)补全。

### 子任务

1. **12 NFR 实测**
   - **owner module**: `tests/nfr/test_nfr_{1..12}.py`
   - **acceptance**: 12 个 NFR 各对应一个测试,全部 PASS;有任何不达标 → block release v0.1.1
   - **出处**: spec.md §6 NFR 全表

2. **自演化半自动触发**
   - **owner module**: `popolaloom/daemon/self_update_trigger.py`
   - **acceptance**: 设置定时任务 (cron 或 systemd-timer) 每周日 02:00 触发 `devolaflow self-update`;PR 自动开 + auto-merge gate 跑;首次 dry-run 仅产生 PR 不自动 merge,人工 review 后再启用 auto-merge
   - **出处**: 06 §0.0 Q8 + spec.md §7.3

3. **Troubleshooting / FAQ / 设计哲学**
   - **owner module**: `docs/{troubleshooting,faq,design_philosophy}.md`
   - **acceptance**: Troubleshooting = 30 个常见错误 + 解法;FAQ = 20 问;设计哲学 = 5 公理引述 (来自 04 §A1-A10) + 反模式红线 5 条 (来自 spec.md §7.4) + 与 DevolaFlow / ArkTower / claude-squad / Cursor Cloud Agent 的关系
   - **出处**: spec.md §7.4 + 06 §1.1 + 06 §"5 句话主张"

4. **lark-cli 来源标注审计**
   - **owner module**: `popolaloom/lark/bridge.py + tests/test_lark_attribution.py`
   - **acceptance**: 所有 lark-cli `+send / +create` 调用末尾必带 `---\n本[消息/任务]由飞书工具 Lark-Cli [发送/创建]`(Phase 1 场景: send / create);测试覆盖
   - **出处**: 工作区规则 "lark-cli 写入操作须追加来源标注"

5. **隐藏文档生成器(doc_auto)**
   - **owner module**: `doc_auto/{architecture.md,evaluation.md}` 仿 ArkTower
   - **acceptance**: 镜像 ArkTower `doc_auto/` 体系,自动生成 architecture / evaluation 文档,带时间戳 (出处: 工作区规则 "Documentation Protocol")
   - **出处**: 工作区规则 "Documentation Protocol" + 08 §1.3 ArkTower 项目布局 row `doc_auto/`

### ArkTower 复用 vs 自写比例
- **复用 ArkTower doc_auto 体系**: ~ 200 LOC 节省
- **自写**: ~ 400 LOC (12 NFR test + 3 docs + cron timer + lark attribution test)

### 风险与 fallback
- **风险**: 隐藏文档与 spec.md 内容漂移
- **fallback**: doc_auto 自动从 spec.md 提取关键章节 + 加时间戳;CI 检测 spec.md 变更必须同步更新 doc_auto

### 当日 verify 命令
```bash
# NFR 全量
pytest tests/nfr/ -v --tb=short --benchmark
# 自演化触发 (dry-run)
DEVOLAFLOW_DRY_RUN=1 popola self-update --branch=feature/self-update-week-1
# 文档审计
test -f docs/troubleshooting.md && wc -l docs/troubleshooting.md
test -f docs/faq.md
test -f docs/design_philosophy.md
test -f doc_auto/architecture.md
# Lark 来源标注
pytest tests/test_lark_attribution.py -v
# 最终 release
git tag v0.1.1
```

---

## 风险滚动表 (跨日)

| 风险 | 出现日 | 缓解负责日 | 备注 |
|---|---|---|---|
| ArkTower 不接受 sibling-intent | Day 0 | Day 0–1 | 退路: vendor checkout (ADR-0001 §4 alt 4) |
| ArkTower upstream breaking change | 任意 | 持续 (CI 周对比) | 锁 commit `467a087` 直到 Phase 2 |
| Cursor / Claude / Codex CLI 周更不兼容 | 任意 | Day 2 + 持续 | `popola check-cli-versions` Day-2 上线 |
| MCP server-to-client push 限制 | Day 4 | Day 5 | Lark 主推 + signal 持久化补足 |
| LangGraph 1.x DeltaChannel 与 SqliteSaver 兼容 | Day 3 | Day 3 fallback | 退到 langgraph 0.4.x |
| Self-evolution PR 自动 merge 误判 | Day 6 | Day 6 + Day 10 | 5 条 AND 条件 + Protected Branch |
| 隐私: popolad 误存 token | 任意 | Day 1 + Day 8 | popolad 工具白名单 + redact env |
| 多 CLI 资源争用 (RAM / API quota) | Day 7+ | Day 8 metric + NFR-11 | 10 并发上限 + 单 task budget |

---

## 横切关注点 (跨日恒定)

- **每日 verify 命令必须 5 分钟内完成**(Day 0 / 1 / 2 已设计如此)
- **每日 PR 入仓前必跑 `pytest tests/ -q --cov=popolaloom`**
- **每日 commit message 必带 Day-N 标签 + S-8 invariant 检查**(L3 task agent inside `.local/.agent/active/<change-id>/` 不能写出 owned_files 之外)
- **lark-cli 写入操作必须末尾追加来源标注**(工作区规则强制)
- **所有 if/else 分支必须用花括号**(工作区规则 "Always use braces for if",Python N/A 但 shell 需要)
- **No silent failures**: 异常必 log + re-raise / 显式错误状态(工作区规则 "No Silent Failures")
- **spec.md 变更必须在 `.local/.agent/active/<change-id>/spec.md` 中表达 delta**(工作区规则 "Documentation Protocol")
- **session 上传**: 每 5 分钟保存 prompt 历史到 `~/.cursor/prompt_hist`(工作区规则 "记录你的聊天记录和prompt")

---

## 进入 Phase 2 的 gate 条件 (Phase 1 出口)

1. spec.md §3.4.1 五个 self-bootstrap 用例 5/5 PASS (Day 9 / Day 10 双重验证)
2. spec.md §6 12 个 NFR 全达 Phase 1 目标值 (Day 10 实测)
3. spec.md §7.3 五条 auto-merge AND 条件可工作 (Day 6 + Day 10 dry-run)
4. README + Quickstart 经第三方用户验证 5 分钟内可上手 (Day 9)
5. ArkTower sibling-intent issue 同 org 维护者 ack (Day 0 启动 + Day 9 复核)
6. ArkTower 8-dim 自评 ≥ 0.85 (复用 nines.toml 框架,Day 6 + Day 9 双轨)
7. PopolaLoom 自身 8 dim 自评 ≥ 0.85 (Day 6 + Day 9)

---

> **Plan 完成时间**: 2026-05-03
> **作者**: L3 Task Agent T3-v2 (Design 团队), devola-flow design-only workflow
> **下一步**: 等待用户 Day 0 启动确认 (ADR-0001 协作模式) → 进入 Day 1 实施。
