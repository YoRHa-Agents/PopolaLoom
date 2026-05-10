---
name: popola-loom
version: 0.9.7
description: "PopolaLoom — 跨 CLI 元编排器。当用户要把任务派发给 Cursor / Claude / Codex / Kimi / Copilot 等 agent CLI 并跨终端持久化运行 (spawn → trace task_id → attach in)、查看任务状态、批量调度多 agent、需要 HITL 确认 / Lark 通知，或要查看 daemon 进程健康时使用本 Skill。提供 popola CLI (8+ root verb 含 dispatch / list / status / attach / cancel / probe / init / skill / doctor) + popolaloom-mcp stdio + Lark 双向通道。"
metadata:
  surfaces: ["cli", "ide", "mcp"]
  requires:
    bins: ["popola"]
    pythonVersion: ">=3.11"
  cliHelp: "popola --help"
tier: 1
token_estimate: 3300
last_updated: "2026-05-10"
---

<!-- updated: 2026-05-10 -->


# PopolaLoom Skill

> **v0.9.0 GA stable surface** — 自 v0.9.0 起 CLI verb / flag spelling / daemon RPC path / `--json` schema / `popolad.toml` section name 全部锁入 SemVer（详见 [`docs/API_STABILITY.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/docs/API_STABILITY.md)）。Workflow 6/7/8 涵盖的 `--cli=cursor-cloud` REST + Cloud HITL γ MCP + `popola relay` 全部 stable；Workflow 9 (`popola cloud runs`) 在 v0.9.0 仍标 **experimental**（[API_STABILITY §3.1](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/docs/API_STABILITY.md#31-popola-cloud-runs-q-c-1)）。v0.7.x → v0.9.0 升级走 [`docs/MIGRATION_v07_to_v09.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/docs/MIGRATION_v07_to_v09.md)。

## What is PopolaLoom?

PopolaLoom 是 DevolaFlow 之上的本机常驻"织机式 (loom) / 编织者 (weaver)"元编排器：把"跨 agent CLI 派发 + 持久化进程总线 + Lark + IDE 三通道 HITL"做成开发者桌面的 sidecar 服务（per `init_popola_loom.md` L1 — "纺织机 / 编织者"定位）。它通过常驻 `popolad` daemon、ArkTower 任务池（vendored）、LangGraph 子图与 9-verb MCP 桥，在 Cursor / Claude Code / Codex CLI / Kimi CLI / GitHub Copilot CLI 上空提供"派发任务 → 拿到 `task_id` → 任意终端 attach 进去看实时事件流 → 必要时 HITL 中断求人类回答 → 终态后 Lark 通知"的一等公民支持。**关键差异**：DevolaFlow 是单 agent 任务质量框架（per-task gates / convergence loops），PopolaLoom 是它在多任务 / 多 CLI 维度的横切伴侣 — 把 N 个 DevolaFlow 实例织进同一张运行图。

## When to use this Skill

调用 `popola` 系列 verb 当用户表述匹配下列意图：

- "派发 / 起 / spawn 一个任务给 Cursor / Claude / Codex / Kimi / Copilot" → `popola dispatch <prompt> --cli=<name>`
- "看一下后台跑着的所有任务 / list my running agents / 我有哪些任务在跑" → `popola list` (默认仅非终态; `--all` 含终态)
- "这个任务现在跑到哪了 / what's the status of <task_id>" → `popola status <task_id>` (含 state / pid / exit_code / latest_event_index / arktower_task_id)
- "我要 attach 进去 / 看一下实时输出 / 流一下事件 / follow this task" → `popola attach <task_id> --follow` (SSE / NDJSON 流，Ctrl-C 退 attach 但任务继续跑)
- "取消 / 停 / kill 这个任务" → `popola cancel <task_id>` (SIGTERM → 5 秒 grace → SIGKILL 升级，发 `task.canceled`)
- "确认 daemon / popolad / 后台进程是否在跑" → `popola probe` 或 `popola popolad status`
- "把 PopolaLoom 装到我的 IDE 里 / register skill / install for cursor" → `popola init [cursor|claude|codex|copilot|all]` (Stage S2/S3)
- "把 daemon 启动 / 起个后台进程" → `popola popolad start` (foreground 加 `--foreground`)
- "评估一下 PopolaLoom 自己的 self-bootstrap 健康 / 跑一下 nines" → `popola eval run --output /tmp/nines.toml`
- "我想要任务完成 / 失败 / 取消时收到 Lark 通知" → 设置 `LARK_HITL_TARGET_OPEN_ID` + `LARK_NOTIFY_ON_COMPLETED=1` (v0.4.1+)
- "需要 HITL 暂停 / 让我点确认再继续" → 任务在 LangGraph `interrupt()` 处暂停，Lark 卡片到达后用户点"通过 / 拒绝"恢复
- "诊断 PopolaLoom 整体健康 / 自检 / 一下子看完所有子系统" → `popola doctor` (v0.5.0+, Stage S4)

如果用户只是问"DevolaFlow 怎么写一个 task agent"或"如何写一个 prompt"，那是 DevolaFlow / 通用编程问题，不要走 popola — PopolaLoom 只在跨 CLI 派发 / 持久化 / 多任务调度场景介入。

## Quick reference — common commands

| Command | Purpose | Example |
|---|---|---|
| `popola dispatch <prompt> --cli=<name>` | 派发任务到指定 agent CLI | `popola dispatch "fix the bug in foo.py" --cli=cursor` |
| `popola dispatch ... --cli=cursor-cloud` | 派发任务到 Cursor **Cloud Agents** REST（远端跑，不占本机 subprocess） | 见下文 Workflow 6 |
| `popola cloud worker {debug,start,status,handoff,dispatch}` | 启动 / 查看本机 self-hosted Cursor worker；`dispatch` 直接走 `popolad` 路由到当前 workspace worker | 见下文 Workflow 10 |
| Cloud Agent 调 `popolaloom_cloud_hitl_request` MCP 工具 | 云端任务遇高风险决策时 deferer 给真人审批走 Lark（v0.8.7+，Enterprise/γ 模式） | 见下文 Workflow 7 |
| `popola dispatch ... --wait --timeout=120` | 派发并阻塞到终态（默认 60s） | `popola dispatch "..." --cli=claude --wait` |
| `popola dispatch ... --cli-flag KEY=VAL` | 透传 adapter 专属参数（可重复；JSON 值自动解析）（v0.2.0+，详见 Workflow 4） | `popola dispatch "..." --cli=cursor --cli-flag output_format=stream-json` |
| `popola dispatch --replay <handoff_id>` | 按之前写下的 envelope 重派发（v0.7.3+） | `popola dispatch --replay cursor-fix-bug-foo-py-3a7f9c1d` |
| `popola handoff list [--json]` | 列出 active envelope（按 mtime 倒排，v0.7.2+） | `popola handoff list` |
| `popola handoff show <handoff_id> [--json]` | 打印 active envelope 内容（默认 raw Markdown） | `popola handoff show cursor-fix-bug-foo-py-3a7f9c1d` |
| `popola handoff archive <handoff_id> <task_id>` | 复制到 `<archive_root>/<task_id>/<id>.md`（D4 双层） | `popola handoff archive <handoff_id> cursor-23e74ec18917` |
| `popola list` | 列出非终态任务（v0.8.6+ 默认含 `runtime` 列：`local`/`cloud`） | `popola list` |
| `popola list --no-runtime` | v0.8.6+ 隐藏 `runtime` 列（escape hatch；`--json` 输出不受影响） | `popola list --no-runtime` |
| `popola list --all` | 含已完成 / 失败 / 取消的所有任务 | `popola list --all` |
| `popola status <task_id>` | 单任务全字段状态（JSON 加 `--json`） | `popola status cursor-23e74ec18917` |
| `popola attach <task_id> --follow` | 跟随 SSE 事件流（默认 follow=true；v0.8.6+ 云任务额外 ingest Cursor SSE） | `popola attach cursor-23e74ec18917 --follow` |
| `popola attach <id> --follow --no-stream` | v0.8.6+ 强制 legacy poll-only 路径（escape hatch） | `popola attach <id> --follow --no-stream` |
| `popola attach <task_id> --no-follow` | 一次性 dump 已有事件（不阻塞） | `popola attach <id> --no-follow` |
| `popola cancel <task_id>` | SIGTERM → 5 秒 grace → SIGKILL | `popola cancel cursor-23e74ec18917` |
| `popola probe` | 轻量 daemon 健康（pid / uptime / active） | `popola probe` |
| `popola list-cli` | 列已注册 CLI adapter + 是否在 PATH | `popola list-cli` |
| `popola popolad start` | 启动 daemon（detach 默认；`--foreground` 前台） | `popola popolad start` |
| `popola popolad status` | daemon socket / pid / probe 三检 | `popola popolad status` |
| `popola popolad stop` | SIGTERM 5s → SIGKILL，清 pid + sock | `popola popolad stop` |
| `popola eval run --output PATH` | 跑 8-dim PopolaLoom-nines self-eval | `popola eval run -o /tmp/nines.toml` |
| `popola init` | 自动检测 IDE 并装 SKILL.md | `popola init` |
| `popola init cursor --global` | 装到 `~/.cursor/skills/popola-loom/` | `popola init cursor --global` |
| `popola init local --mode=core` | 仅 scaffold `.local/` 工作区 | `popola init local --mode=core` |
| `popola init --list` | 打印检测到的 IDE + 安装路径 | `popola init --list` |
| `popola doctor` | 五项综合自检（v0.5.0+，Stage S4） | `popola doctor` |
| `popola skill upgrade --target=cursor` | 用 wheel 内最新 SKILL.md 覆盖装机版（Stage S4） | `popola skill upgrade --target=cursor` |
| `popola version` | 打印 `popolaloom <version>` | `popola version` |

## Workflows

### Workflow 1 — Dispatch + trace (single CLI)

最常见的"我有一个长任务，要丢到后台让 Cursor 跑，结果出来再回来看"流程，5 步：

1. **启动 daemon**（每台机器只启一次，`systemd-run` / 系统重启后丢了再启）：
   ```bash
   popola popolad start
   # → "popolad started, PID=12345"  socket: ~/.popola/popolad.sock
   ```
2. **派发任务**（拿到 `task_id`，立刻退 shell 也没问题）：
   ```bash
   popola dispatch "refactor module X for clarity, add tests" --cli=cursor --cwd ~/proj
   # → cursor-23e74ec18917
   ```
3. **看后台都跑着啥**：
   ```bash
   popola list
   ```
4. **回来 attach 看实时输出**（在另一台终端 / 另一个 SSH session 里也行，事件总线是持久化的）：
   ```bash
   popola attach cursor-23e74ec18917 --follow
   # → SSE 帧逐行打印：process.stdout / process.stderr / state.* / task.completed
   #   按 Ctrl-C 退出 attach（任务不受影响）
   ```
5. **任务终态后取消订阅 / 看最终事件 / 决定是否取消**：
   ```bash
   popola status cursor-23e74ec18917      # 看 exit_code
   popola cancel cursor-23e74ec18917      # 仍在跑且不想继续
   ```

### Workflow 2 — Multi-CLI handoff (relay)

跨 agent CLI 接力（设计阶段 Cursor 出方案 → 实现阶段 Claude 写代码 → 验证阶段 Codex 跑测试），3 步：

1. **首发任务到 Cursor，等其落 handoff envelope**：
   ```bash
   popola dispatch "design API for ETag cache layer; emit handoff at .local/.agent/handoff/" \
     --cli=cursor --wait
   ```
2. **从 handoff envelope 读出后继任务 prompt**（约定路径 `.local/.agent/handoff/<id>.yaml`）。
3. **接力派发到 Claude（携 envelope 摘要作为 prompt 上下文）**：
   ```bash
   popola dispatch "$(yq '.next_prompt' .local/.agent/handoff/<id>.yaml)" --cli=claude
   ```

> **Note**: v0.4.0 的 `popola_relay` MCP primitive 已就位（per `mcp/tools.py`），但 CLI 直接 verb (`popola relay`) 仍是 v0.6.0 项；当前用 shell 编排即可。

### Workflow 3 — HITL pause + Lark approval

LangGraph `interrupt()` 节点阻塞任务、Lark 卡片到人、人点确认后任务恢复，5 步：

1. **派发一个含 HITL 节点的任务**（adapter 内部检测到危险操作时调 `interrupt(prompt="ok to delete prod table?")`）：
   ```bash
   export LARK_HITL_TARGET_OPEN_ID=ou_xxx          # 你的飞书 open_id
   popola dispatch "drop the staging.users table" --cli=cursor
   ```
2. **任务卡在 `interrupt()`，daemon 经 lark-cli 推送一张交互卡到 `LARK_HITL_TARGET_OPEN_ID`**（per `lark/notifier.py` + `lark/card_templates.py`）。
3. **用户在飞书 App 里点"通过"或"拒绝"** —`lark-cli event consume` listener (per `lark/listener.py`) 捕获 button event。
4. **`LarkSupervisor` 把 reply 写回 LangGraph state，任务恢复执行**（state 切回 `running`，事件总线发 `state.resumed`）。
5. **用 `popola attach <task_id> --follow` 在终端同步看到 resume 后的输出**，或等 `LARK_NOTIFY_ON_COMPLETED=1` 后 Lark 卡通知终态。

### Workflow 4 — Adapter-specific arg passthrough (`--cli-flag`)

每个 agent CLI 有自己的可选参数（cursor 的 `--output-format` / `--session-id`、claude 的 `--max-turns` / `--session-id`、codex 的 `--sandbox` 三档）；PopolaLoom 用统一的 `--cli-flag KEY=VAL` 选项透传，daemon 把 `KEY=VAL` 收进 `extra` dict 后由各 adapter 的 `build_command` 拼成最终 argv。Value 优先按 JSON 解析（`true` / `123` / `"foo"`），解析失败 fall back 到字符串（`output_format=text` 等同 `output_format="text"`），出处 `cli/main.py:_parse_cli_flags`（R-012 落地）。

**支持的 KEY**（按 adapter 列；多余 / 未识别的 KEY 会被 adapter 静默忽略）：

| Adapter | KEY | 类型 | 含义 / 落点 argv |
|---|---|---|---|
| `cursor` | `output_format` | str | `text`（默认）/ `stream-json`，落到 `--output-format <val>`（白名单校验，违规直接 ValueError，No Silent Failures） |
| `cursor` | `cwd_flag` | bool | `true` 时把 `--cwd <cwd>` 注入 argv；默认 `false`（让 supervisor 通过 `Popen(cwd=...)` 控制） |
| `cursor` | `session_id` | str | 追加 `--session-id <chatId>`，与 `cursor-agent create-chat` 预生成的 chat 复用 |
| `claude` | `session_id` | str (UUID) | 追加 `--session-id <UUID>`，"先分配 ID 再 spawn" 形态 |
| `claude` | `max_turns` | int | 追加 `--max-turns <n>`，限对话轮数（防长任务失控） |
| `codex` | `sandbox` | str | 三档之一: `read-only` / `workspace-write` / `danger-full-access`，落到 `--sandbox <val>`（白名单校验） |

**3 个常见用法**：

1. **Cursor `stream-json` 输出**（让 supervisor 端 NDJSON 解析器逐行消费 cursor 的工具调用事件）：
   ```bash
   popola dispatch "design caching layer" --cli=cursor \
     --cli-flag output_format=stream-json
   ```
2. **Claude 先生成会话 UUID 再 spawn**（PopolaLoom 派发器的标准做法 — 让 `task_id` 与 `session_id` 一一对应，便于 attach / handoff 复用）：
   ```bash
   SESSION=$(python -c "import uuid;print(uuid.uuid4())")
   popola dispatch "refactor module X" --cli=claude \
     --cli-flag session_id="$SESSION" \
     --cli-flag max_turns=10
   ```
3. **Codex 限制 sandbox 到只读模式**（适合 design-only / review-only 派发，防 codex 不小心写文件）：
   ```bash
   popola dispatch "review src/foo.py for bugs" --cli=codex \
     --cli-flag sandbox=read-only
   ```

> **Tip**：`--cli-flag` 可重复多次（典型用法是 cursor 的 `output_format=stream-json` + `session_id=<chatId>` 同时给）。Value 含空格 / 等号时用 shell 引号 + JSON 字符串：`--cli-flag 'cmd_args="--foo --bar"'`（注意 popolaloom 的 cursor adapter 当前**不**透传任意 cmd_args；需要 cursor-agent 自定义 flag 时走 `popolaloom._vendored` 二开或等 v0.6+ 的 `--passthrough` 项）。

### Workflow 5 — Self-eval (PopolaLoom-nines)

跑 8 维度自评、写 TOML 报告，2 步：

1. **跑评估（不需要 daemon，从 `~/.popola/events/` 走 NDJSON 收据）**：
   ```bash
   popola eval run --output /tmp/nines.toml
   # → composite=0.91 → /tmp/nines.toml
   ```
2. **看维度权重 / debug 单维度**：
   ```bash
   popola eval show --json
   ```

### Workflow 6 — Cloud Agent dispatch (`--cli=cursor-cloud`, v0.8.5+ / SSE v0.8.6+; **stable since v0.9.0**)

<!-- updated: 2026-05-08 -->

> **v0.9.0 GA**：本 Workflow 涉及的 CLI verb (`popola dispatch --cli=cursor-cloud`) + `--cli-flag` keys + `popola list` `runtime` 列 + `popola attach --no-stream` flag 全部进 v0.9.x stable surface。仅 `cloud.sse.*` 子事件类型 payload shape 仍 experimental（[API_STABILITY §3.4](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/docs/API_STABILITY.md#34-sse-event-sub-types-cloudsse)）。需要纯云端项目脚手架走 `popola init --target=cloud-only`（v0.9.0+，Q-D-4 偏离默认）；要 copy-paste-ready 上手脚本走 [`cloud-quickstart.sh`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/cloud-quickstart.sh)。

云端 Background Agent：**不走本机 subprocess**，而是用 httpx 调 Cursor Cloud Agents REST（`CloudCursorClient`），任务出现在浏览器里的 Cloud Agents UI（仪表盘入口例如 `https://cursor.com/dashboard/cloud-agents`，任务列表亦可从 `https://cursor.com/agents` 跳转）。daemon 侧的 `Supervisor` 检测到 `CLOUD_BUILD_COMMAND_MARKER` sentinel 就走 `_spawn_cloud()` + **cloud poller** 线程对齐状态事件。

先决：**非空 `CURSOR_API_KEY` 环境变量** — HTTP Basic：`username=api_key`、`password=` 空串（适配器读环境变量，`CloudCursorAdapter.is_available()` 亦以此为准）。

派发命令形态（与工作区 Decision matrix Q6 对齐：默认 **`autoCreatePR=false`**，需要的话用 flag 打开）：

```bash
export CURSOR_API_KEY="cr_..."
popola dispatch "implement smoke-test stub in README" \
  --cli=cursor-cloud \
  --cwd ~/src/myrepo \
  --cli-flag repo_url=https://github.com/acme/monorepo \
  --cli-flag starting_ref=main \
  --cli-flag model=composer-2 \
  --cli-flag auto_create_pr=false
```

支持的 `--cli-flag extra` keys（传给 `cursor_cloud.CursorCloudAdapter` / REST）：

| Key | 说明 |
|---|---|
| `repo_url` | Git HTTPS 克隆地址（或与 `pr_url` 二选一） |
| `pr_url` | 直接基于已有 PR URL 派发（与 `repo_url` 二选一） |
| `starting_ref` | branch / tag，默认 `"main"` |
| `model` | 云端模型 id，默认 `"composer-2"` |
| `auto_create_pr` | bool，默认 `false` |
| `work_on_current_branch` | bool，默认 `false` |
| `skip_reviewer_request` | bool，`auto_create_pr=true` 时可选 |
| `env_vars` | `dict[str,str]` JSON blob（透传到 payload `envVars`） |
| `use_private_worker` | bool，显式请求 Cursor REST `usePrivateWorker=true` |
| `labels` | `dict[str,str]` JSON blob，自托管 / 本地 worker 路由 labels |
| `worker_name` / `machine_name` / `pool_name` | 非空字符串 convenience keys；分别合并为 `labels.worker` / `labels.machine` / `labels.pool`，并自动启用 `use_private_worker` |
| `timeout_s` | HTTP 单次请求超时 float |
| `api_key` | 覆盖环境变量里的 key（一般由测试/DI 用；生产不推荐写进 envelope） |

若设置了 `labels` 或任一 convenience key，`use_private_worker` 会自动变为 `true`；显式传 `use_private_worker=false` 同时又设置路由 label 会被拒绝，避免误以为已请求自托管 worker 路由。

**与本地 `cursor` subprocess 的差异（一眼）**：`popola list` / `popola status` 会带出 `runtime="cloud"`、`cursor_agent_id`（通常 `bc-*`）、`cursor_run_id`、`cloud_phase`。v0.8.6+ 起 `popola list` 默认渲染 **`runtime` 列**（在 `task_id` 与 `cli` 之间，`local`/`cloud`），加 `--no-runtime` 可隐藏。取消：`popola cancel` 在云路径走 **`cancel_run` REST**，不是 `SIGTERM`。HITL：若云端任务需要通过 Popola daemon 请人拍板走 Lark，可走 **`cloud`** 通道：`POST /hitl/cloud/request` → block `GET /hitl/cloud/wait/{hitl_id}` → Lark 侧照旧 first-responder wins → **`POST /hitl/cloud/answer/{hitl_id}`**。

**SSE 实时拉取（v0.8.6+）**：`popola attach <task_id> --follow` 在 `runtime=cloud` 任务上**默认启用 SSE**：在 daemon `/attach_stream` 之外另起一个后台线程 pump Cursor 的 `GET /v1/agents/{id}/runs/{run_id}/stream`，把 `cloud.sse.{assistant,tool_call,result,status,parse_error,stream_expired,dedup_drop}` 事件流入同一渲染器。每条 envelope 携带 `(task_id, run_id, stream_session_id, sse_id, seq)` 五元组用于幂等去重。**自动 fallback 到 poll**：遇到 `CursorCloudStreamExpiredError`（HTTP `410 stream_expired`）/ `httpx.ReadError` / `httpx.ConnectError` / `httpx.TimeoutException` / 缺 `CURSOR_API_KEY` / 主线程 Ctrl-C 时，SSE 线程优雅退出并 append 一条 `cloud.sse.fallback_to_poll` 边界事件 + stderr 一行 `[cloud sse] ...` 提示（No-Silent-Failures），既有 poll-driven 视图继续工作。**强制 legacy 路径**：`popola attach <id> --follow --no-stream`。**容忍漂移**：`cloud_poller` 仍是 `cloud_phase` 的唯一写入者（state SoT 锁），SSE 是 append-only 旁路；SSE-side `stream:running` 与 poller-side `cloud_phase=CREATING` 之间最长容忍 ≤3 s 不一致（`interval_s + 1s`，默认 `interval_s=2s`）。

**云端错误提示（v0.8.6+）**：v0.8.6 在 `cursor_cloud.py` 内嵌 16 条 `_ERROR_CATALOG` 条目，按 `(error.code → error.message regex → HTTP status)` 优先级派发到 10 个新 `CursorCloud*Error` 子类，每个都带双语 `.hint_en` / `.hint_zh`（每条 ≤2 句、含 ≥1 个 dashboard URL）+ 稳定 `.cli_exit` 退出码。覆盖：`401 unauthorized` / `api_key_not_found`、`403 plan_required` / `role_forbidden` / `feature_unavailable`、`404 agent_not_found` / `run_not_found`、`409 agent_busy` / `agent_archived` / `run_not_cancellable`、`410 stream_expired`、`422` 三类 GitHub-App 集成错（`RepoAllowlistError` / `GithubAppMissingError` / `GithubAppPermissionError`）、`400/422 validation_error`、`429 rate_limit_exceeded`（v0.8.8 完整重试）、`5xx internal_error`/`upstream_error`。完整提示文本与重试矩阵见研究备忘 `.local/research/v0.8.6_sse/422-error-catalog.md` §3（`.local/` 仅本地存在，已 gitignore）。

**Opt-in quota smoke**：导出 `CURSOR_API_KEY` 后跑 `pytest tests/real_cursor_cloud/ -m real_cursor_cloud`，否则该目录四项 case 仅 **skipped**（见 `pytest` marker `real_cursor_cloud`）。

出处：`.local/research/v0.8.5_cloud_agent/research.md`（Option α）+ `00-decision-matrix-zh.md` §7；v0.8.6 SSE / 422 / state SoT 设计：`.local/research/v0.8.6_sse/{sse-event-schema,state-source-of-truth,422-error-catalog}.md`（local-only research notes）。

### Workflow 7 — Cloud HITL approval via MCP tool (γ mode, v0.8.7+)

<!-- updated: 2026-05-08 -->

> **Tier**：Enterprise / Self-Hosted。本 workflow 让 **Cursor Cloud Agent** 在云端跑任务时，遇到高风险决策（删数据、上线变更、生产部署等）能 **deferer 给真人** — 经 Lark 卡片走人审批，结果回传给 Cloud Agent 继续执行。先决条件、详细架构与安装步骤详见 [`docs/USER_GUIDE.md#cloud-hitl-enterprise--self-hosted`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/docs/USER_GUIDE.md#cloud-hitl-enterprise--self-hosted)。

**架构**（γ — Worker stdio MCP 一等公民模式）：

```text
Cursor Cloud Agent (云端) ──tool_call──▶ Self-Hosted Worker
                                              │
                                              │ spawns / pipes stdio
                                              ▼
                                        popolaloom-mcp
                                              │
                                              │ HTTP RPC (loopback / VPC)
                                              ▼
                                          popolad ──▶ popola_hitl (SQLite)
                                              │
                                              │ outbound HTTPS
                                              ▼
                                      open.larksuite.com (Lark 卡片)
                                              │
                                              ▼
                                          人审批 → 回写 → Cloud Agent 继续
```

派发 + 审批一条龙，6 步：

1. **先做 Enterprise 准备**（一次性，per worker host）：
   - 在 Cursor Self-Hosted Pool 装 worker（`curl https://cursor.com/install -fsS | bash` 然后 `agent worker start --pool`）。详见 USER_GUIDE Cloud HITL §"Install steps (γ)"。
   - 在同 host 装 `popolad` + `popolaloom-mcp`（`pipx install popolaloom`），并 `popolad up` 让 RPC 绑到 `127.0.0.1:<popolad_port>`（**绝不**绑公网接口）。
   - 在 [Cloud Agents dashboard](https://cursor.com/agents) 注册自定义 MCP server（transport：**Command (stdio)**），命令 `popolaloom-mcp`，args `[]`（v0.8.7 入口默认就是 cloud HITL 桥），env 至少含 `POPOLAD_BASE_URL` + `POPOLAD_API_KEY`（per-tenant scoped）。env 白名单（per SECURITY L2）由 systemd / launchd unit 配 `Environment=` + `EnvironmentFile=` 完成（运维负责），`popolaloom-mcp` 进程本身不再持有 shell / git / 云 creds。
2. **配 `popolad.toml [hitl.cloud]`**（一次性，可选 — 默认值已经 ok）：
   ```toml
   [hitl.cloud]
   timeout_seconds      = 1800   # 默认 30 min；clamp 到 [60, 86400]
   idempotency_window_s = 3600   # 1 h 内重复请求短路返回原 hitl_id
   max_concurrent_per_run = 1
   ```
3. **派发一个会用到 HITL 的云端任务**（broad-audience cloud dispatch + 让 cloud agent 知道有 HITL 工具可用）：
   ```bash
   export CURSOR_API_KEY="cr_..."
   export LARK_HITL_TARGET_OPEN_ID="ou_xxx"   # 审批人 / 群 open_id
   popola dispatch "迁移生产数据库 schema，遇到 destructive 操作请通过 popolaloom_cloud_hitl_request 求人审批" \
     --cli=cursor-cloud \
     --cli-flag repo_url=https://github.com/acme/monorepo \
     --cli-flag starting_ref=main
   # → cursor-cloud-deadbeef（任务在 Cursor Cloud 上跑）
   ```
4. **Cloud Agent 在云端 runtime 调 MCP 工具**（这一步由 Cloud Agent 自己根据 prompt 与场景判断；典型 tool_call shape 如下，agent 的 LLM 自动构造）：
   ```jsonc
   {
     "tool_name": "popolaloom_cloud_hitl_request",
     "input": {
       "task_id": "cursor-cloud-deadbeef",
       "cursor_agent_id": "bc-ad-...",
       "cursor_run_id": "run-...",
       "question_text": "Drop staging.users (10M rows)? This is irreversible.",
       "prompt_body": "About to execute: DROP TABLE staging.users; ... full context ...",
       "options": ["approve", "reject"],
       "responder_policy": "single",
       "timeout_s": 1800
     }
   }
   ```
   工具内部：（a）走 worker-side `popolaloom-mcp` → `POST /hitl/cloud/request` 到 `popolad`；（b）`popolad` 写 `popola_hitl` 行 + 触发 Lark 卡片 `cloud_hitl_request_card_v1` 推送到 `LARK_HITL_TARGET_OPEN_ID`；（c）`popolaloom-mcp` 内部 long-poll `GET /hitl/cloud/wait/{hitl_id}?timeout_s=55` 一直拉到 30 min 超时为止。
5. **真人在 Lark 点 Approve / Reject / Custom**（卡片含 verbatim 问题 + 200-字截断 context + Expand 链接 + 三按钮）：
   - 点 Approve / Reject → `lark/listener.py` 接 button event（γ 模式经 `lark-cli event consume` 长连接，认证由 lark-cli 持有的 bot websocket session 完成 — 无 HMAC 校验在 listener 边界；β 模式才在公网 HTTPS gateway 做 HMAC 校验）→ `POST /hitl/cloud/answer/{hitl_id}` → `bridge.submit_answer`（含 `expected_cursor_*` mis-route 防御）→ `mark_answered`。
   - 点 Custom → 弹 `open_input` 文本框 → 自由文本回答。
   - 30 min 内没人响应 → 工具返回 `error.code: "timeout"`（**显式失败**，非默默通过 — per Q-B-3 锁定）。
6. **Cloud Agent 收到结果继续执行**，或在 timeout 时按自己的策略 retry / fail-loud。在本机用 `popola attach cursor-cloud-deadbeef --follow` 同步看到全过程；用 `~/.popola/events/cursor-cloud-deadbeef.jsonl` 审计 `cloud_hitl.{requested,answered,failed,transition}` 4 类 NDJSON 事件。

**关键安全约束**（不可省）：

- **L3 季度轮换 Lark webhook secret**（Q1 1/15、Q2 4/15、Q3 7/15、Q4 10/15）；γ 与 β 共用同一 secret + 轮换流程。
- **L6 Team follow-ups**：若组织里多人共用一个 Cloud Agent，**禁用 Team follow-ups** 或仅让服务账号能跟进 — 否则 teammate B 可能 driver agent 用 user A 的 secret 调 HITL 工具。
- **L8 MCP 配置块视为机密**：dashboard 里 env / headers 加密+读时屏蔽，但**绝不**把 JSON blob commit 进 git 或粘贴到聊天。
- **L10 Cursor Cloud network access policy**：处理 HITL 的 agent 设为 **"Allowlist only"**，仅放行 [Egress allowlist](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/docs/USER_GUIDE.md#egress-allowlist) 的 host。
- **绝不** 用 public IP / port-forward / residential NAT / 入站端口 / VPN 隧道把 `popolad` 暴露给 Cursor Cloud — 仅 γ 的 outbound HTTPS（worker 主动出站）或 β 的 backend-proxied HTTPS（Cursor backend 代理）是合法路径。详见 [`docs/known-issues.md` §"v0.8.7 — Cloud HITL transport (anti-patterns)"](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/docs/known-issues.md#v087--cloud-hitl-transport-anti-patterns)。

**β（HTTP MCP backend-proxied 模式）的差异**：把第 1 步换成"在 VPC 里搭一个公网 HTTPS gateway，跑 HMAC 校验 + 转发给 popolad"，并在 dashboard 里改注册 transport=**HTTP** 的 MCP server；其余步骤一致。Β 不需要 Cursor Enterprise 套餐，但需要团队自有 SRE 维护 gateway。完整对照见 USER_GUIDE Cloud HITL §"Decision matrix — γ vs β vs neither"。

**幂等 + 容灾**：同 `(task_id, cursor_agent_id, cursor_run_id, question_text)` 在 1 h 内重复调，后到的请求短路返回原 `hitl_id` + `deduped: true`（不重发卡片，不写新 `popola_hitl` 行）。`popolad` 重启不影响 dedup，因为 SQLite 是唯一真相源（无内存 cache）。

出处：v0.8.7 设计与契约见 `.local/research/v0.8.7_hitl/{deployment-modes,mcp-tool-contract,lark-card-spec,long-tool-call-probe}.md`；安全 gate 见 `.local/.agent/active/v0.8.7-cloud-hitl-prod/SECURITY_CHECKLIST.md`（10 项 lateral-movement + 4 项 secret hygiene + 4 项 idempotency + 4 项 audit + 3 项 approval policy）。

### Workflow 8 — Cross-PR relay (`popola relay`, v0.8.8+)

<!-- updated: 2026-05-08 -->

> **⚠️ Q-C-4 deviation**：v0.8.8 把 `popola relay <task_a>` 默认改成 **auto-dispatch**（偏离默认；详见 [`RELEASE_NOTES.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/RELEASE_NOTES.md) 顶部 callout 与 [`docs/USER_GUIDE.md#cross-pr-relay--popola-relay-v088`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/docs/USER_GUIDE.md#cross-pr-relay--popola-relay-v088)）。配套 5 项强制缓解：repo allowlist（默认 `[]` 阻断一切）+ `0o600` audit log + detect-secrets 预扫（6 种 token shape）+ RELEASE_NOTES callout + CI 隔离测试。

把已完成的 cloud `task_a` 的 PR / branch / summary 接力成新的 cloud `task_b`，3 步：

1. **预演（强烈推荐先 `--dry-run`）**：跑一遍**完整** policy gate（allowlist + secret scan + size cap），写一行 `mode="dry-run"` audit row，**不发任何 cloud API 请求**：
   ```bash
   popola relay v088-task-abc --dry-run --json | jq
   # → {"mode": "dry-run", "outcome": "would_dispatch",
   #     "source_task": "v088-task-abc", "target_repo": "neolix-ai/popola-loom",
   #     "model": "composer-2", "prompt_sha256": "9c1f...",
   #     "audit_path": ".local/.agent/archive/relay/v088-task-abc.jsonl",
   #     "dispatched_at": null}
   ```
2. **确认 audit 路径 + sha256 后真发**（默认就是 auto；同 prompt 同 target 同 idempotency_key 在 1 h 内重复发会短路）：
   ```bash
   popola relay v088-task-abc
   # → DISPATCHED v088-task-def → https://github.com/neolix-ai/popola-loom
   #   model=composer-2  prUrl=https://github.com/neolix-ai/popola-loom/pull/42
   #   audit=.local/.agent/archive/relay/v088-task-abc.jsonl
   ```
3. **跨 org 接力（罕见、需要显式 override）**：默认 `[cloud.relay] repo_allowlist = []` 会**阻断**任何 target；要想跨 allowlist 必须 `--confirm-allowlist`，且会在 stderr 打 WARN + 在 audit 行记 `gate_decision="override_confirm_allowlist"`：
   ```bash
   popola relay v088-task-abc \
     --target-repo https://github.com/external/fork \
     --confirm-allowlist
   # WARNING: dispatching relay outside repo_allowlist via --confirm-allowlist
   #          (target=external/fork); audit row recorded at <path>
   # DISPATCHED v088-task-ghi → https://github.com/external/fork  ...
   ```

**5 项缓解（M1..M5）的快速记忆**（详见 [USER_GUIDE Cross-PR relay](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/docs/USER_GUIDE.md#cross-pr-relay--popola-relay-v088)）：(1) **repo_allowlist 默认 `[]` 阻断一切**；(2) `0o600` append-only audit log，crash 也留 `dispatch_inflight` 行；(3) detect-secrets 预扫 6 种 shape（AWS/GitHub PAT/Stripe/JWT/Slack/high-entropy），命中即 exit 1 + `…<last4>` 脱敏；(4) RELEASE_NOTES 顶部 callout（M4 lint 强制）；(5) `tests/cli/test_relay_safety.py` 在默认 CI 走道里跑。要恢复 v0.8.7 「人工确认」默认行为：在 `popolad.toml` 设 `[cloud.relay] mode = "confirm"`。

### Workflow 9 — `popola cloud runs` 列出云端 run 历史（v0.8.8+；**experimental in v0.9.0**）

<!-- updated: 2026-05-08 -->

> **v0.9.0 GA**：`popola cloud` 子 app 本身 stable，`runs` verb 在 v0.9.0 仍标 **experimental**：6 列默认表格布局、`--include-events` slow-path JSON shape、跨 verb 404→exit `4`（vs `popola dispatch --cli=cursor-cloud` 的 100）可能在 v0.9.x minor 调整（[API_STABILITY §3.1](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/docs/API_STABILITY.md#31-popola-cloud-runs-q-c-1)；CHANGELOG 会显式记 column / shape 变更）。

`popola cloud runs <task_id>` 包装 Cursor `GET /v1/agents/{id}/runs`，按 newest-first 列出该 cloud agent 的全部 run（含手工通过 `https://cursor.com/agents` 浏览器追加的）；`popola list` 仍保持 single-row-per-task。完整 dispatch → wait → cloud runs 一条龙：

1. **派发一个 cloud 任务**：
   ```bash
   export CURSOR_API_KEY="cr_..."
   popola dispatch "重构 caching 模块并补齐 unit test" \
     --cli=cursor-cloud \
     --cli-flag repo_url=https://github.com/neolix-ai/popola-loom \
     --cli-flag starting_ref=main \
     --cli-flag model=composer-2
   # → cursor-cloud-deadbeef
   ```
2. **等任务跑（attach 跟随；多 run 时自动加 `[run-N]` 前缀 + 分隔行）**：
   ```bash
   popola attach cursor-cloud-deadbeef --follow
   # [run-0] STARTING ───► RUNNING
   # [run-0] tool_call: read_file(path="src/cache.py")
   # [run-0] FINISHED (exit 0)
   # ─── follow-up: run-1 (parent=run-0) ───
   # [run-1] CREATING ───► RUNNING ...
   ```
3. **列出全部 run（默认 6 列表格）**：
   ```bash
   popola cloud runs cursor-cloud-deadbeef
   # ┃ run_id              ┃ run_index ┃ state    ┃ created_at                  ┃ wall_clock ┃ model      ┃
   # │ run-yyyyyyyy-00…    │ 1         │ finished │ 2026-05-08T18:30:00.000Z    │ 00:32:00   │ composer-2 │
   # │ run-xxxxxxxx-00…    │ 0         │ finished │ 2026-05-08T17:00:00.000Z    │ 00:15:00   │ composer-2 │
   ```
4. **要 JSON 写脚本（`--json`，full `run_id` 不截断；分页用 `--cursor`）**：
   ```bash
   popola cloud runs cursor-cloud-deadbeef --json --limit 100 | jq '.runs[] | .run_id'
   ```
5. **要 events_summary（`--include-events` 慢路径，1 row 多 1 个 `GET /runs/{run_id}` round-trip）**：
   ```bash
   popola cloud runs cursor-cloud-deadbeef --include-events --json \
     | jq '.runs[] | {run_index, state, events_summary}'
   ```

**和 `popola status --verbose` 的对比**：`status --verbose` 显示**单条最新 run**的 5 字段 cost block（`cost: n/a` + `model` + `wall` + `link` 等；honest disclosure，不编 cost 数字）；`popola cloud runs` 显示**全部 run 的可分页历史**。错误退出码：`task_id` 本地未知 → exit `4`（per Q-C-1 OQ-1，**与 `popola dispatch` 的 100 退出码不同**，CHANGELOG §Changed 显式记录此偏离）；401/403 → exit `77`（per Q-C-1 OQ-2，对齐 `_ERROR_CATALOG`）；429 / 5xx → exit `75`；403 plan_required → exit `78`。

出处：wire 级细节见 `.local/research/v0.8.8_multi_run/runs-subcommand-spec.md`（`.local/` 仅本地存在，已 gitignore）。

### Workflow 10 — Self-hosted worker handoff (`popola cloud worker`, v0.9.1+)

<!-- updated: 2026-05-10 -->

触发：`agent worker` / "self-hosted worker" / "My Machines" / "Self-Hosted Pool" / "把本机注册到 Cursor 云端"。`start` / `handoff` **不**创建 popola task id；要 popola-tracked task 可用 Workflow 6 (`--cli=cursor-cloud` REST) 或本 workflow 的 `dispatch` 便捷命令。

5 verb：`debug` 跑 `agent worker debug` 预检；`start` 默认 My Machines（`agent login` 即可），自动生成 `popolaloom-<repo>-<hash>` 名称并按 `--worker-dir` 复用已有进程，`--allow-duplicate` 才强制开第二份；`status` 读 `/healthz` + `/readyz` + `/metrics`（loopback only，无需 API key）；`handoff` 输出 `prompt + URL` 信封，`popola_task_id: null`；`dispatch` 默认直接 POST 到 `popolad`，携带 `cli=cursor-cloud`、`worker_name`、repo/PR、`starting_ref`、`model` extras，把任务路由到当前 workspace worker；`--print-only` / `--dry-run` 只输出等价命令。

```bash
popola cloud worker start --worker-dir "$(pwd)" \
    --management-addr 127.0.0.1:39231
# → "Run agents: https://cursor.com/agents#workerId=<uuid>"
popola cloud worker status --management-addr 127.0.0.1:39231 --json | jq
popola cloud worker handoff --worker-id <uuid> --prompt "..."
popola cloud worker dispatch "..." --worker-dir "$(pwd)" \
    --repo-url https://github.com/acme/repo
```

完整文档：[USER_GUIDE §Self-hosted worker handoff](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/docs/USER_GUIDE.md#self-hosted-worker-handoff-popola-cloud-worker-v091)。

## Configuration

PopolaLoom 用环境变量做配置（per ADR — 显式优于隐式）；下表是常用项：

| Env var | Purpose | Default |
|---|---|---|
| `POPOLA_HOME` | daemon socket / events / sqlite / pid 根目录 | `~/.popola/` |
| `LARK_HITL_TARGET_OPEN_ID` | Lark HITL 卡片 / 终态通知收件人 open_id | (unset → Lark 静默) |
| `LARK_NOTIFY_TARGET_OPEN_ID` | 终态通知收件人（独立于 HITL） | fallback to `LARK_HITL_TARGET_OPEN_ID` |
| `LARK_NOTIFY_ON_COMPLETED` | task.completed 时发 Lark 卡（v0.4.1+） | `1` (ON) |
| `LARK_NOTIFY_ON_FAILED` | task.failed 时发 Lark 卡（v0.4.1+） | `1` (ON) |
| `LARK_NOTIFY_ON_CANCELED` | task.canceled 时发 Lark 卡（v0.4.1+） | `1` (ON) |
| `LARK_NOTIFY_ON_CANCEL_ESCALATED` | SIGKILL 升级时单独发卡（v0.4.1+） | `0` (OFF) |
| `LARK_NOTIFY_PROMPT_TRUNCATE` | 卡片 body 内 prompt 字符上限 (50–2000) | `200` |
| `CODEX_HOME` | Codex skill / config 目录 | `~/.codex/` |
| `POPOLA_USE_GRAPH` | 是否启用 LangGraph 子图（v0.3.0+） | `1` |
| `LARK_PRIORITY_BOT_ID` | 出口卡用哪个 bot 发（multi-bot setup） | (unset → 默认 bot) |
| `CURSOR_API_KEY` | Cursor Cloud Agents REST Basic 用户名 (= API key)，密码空 | (unset → `--cli=cursor-cloud` 不可用 / `CursorCloudAdapter.is_available()==False`) |

> **Lark gating**：当 `lark-cli` 不在 PATH **或** `LARK_HITL_TARGET_OPEN_ID` unset 时，daemon 静默退化为只发本地事件 + 写本机 NDJSON（不抛异常，per the No Silent Failures + degrade-gracefully 双约束）。

## Reference

- **Repo / README**：[github.com/YoRHa-Agents/PopolaLoom](https://github.com/YoRHa-Agents/PopolaLoom) — 5 分钟 quickstart、架构 ASCII 图、5/5 self-bootstrap scenario 矩阵。
- **Architecture diagram**：repo 内 `docs/DEMO.md`（截图 + 完整会话 walkthrough）。
- **Spec + ADRs**：repo 内 `.local/memory/specs/popolaloom/`（spec.md / implementation-plan.md / v0.5.0-plan.md / adrs/）。
- **MCP verbs（IDE Agent integration）**：9-verb stdio 桥（`popola_submit` / `popola_list` / `popola_status` / `popola_attach_stream` / `popola_supply_feedback` / `popola_cancel` / `popola_inject_subtask` / `popola_relay` / `popola_supervise`）— 见 `src/popolaloom/mcp/tools.py`。
- **Sibling project**：[ArkTower](https://github.com/YoRHa-Agents/ArkTower) — 任务池 / FSM / EventBus / SQL migrations 提供方（v0.5.0 起 vendored 进 `popolaloom._vendored.arktower`，`pip install popolaloom` 不再需要 ArkTower 单独 clone）。
- **Related Skill**：[devola-flow](https://github.com/YoRHa-Agents/DevolaFlow) — 单 agent 任务质量 / 4-layer hierarchy / convergence-loop framework；PopolaLoom 是它的多任务 / 多 CLI 编排互补层（你可以同时装两个 Skill）。
- **Repo 内 examples**：`examples/quickstart.sh`（5 步自动 smoke）、`examples/quickstart-skill.md`（v0.5.0 Stage S5 起的长版 SKILL example）。

## Version + upgrade

- **Current**: v0.9.6 patch（2026-05-10，**stable since v0.9.0**）— Skill 前缀 (`name`/`version`/`description`) 进 v0.9.x SemVer-stable 锁；body 内容（含本 Workflow 编号）显式标 out-of-scope（[API_STABILITY §7](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/docs/API_STABILITY.md#7-out-of-scope)）。`popola init` (Stage S2/S3 起) 自动写本 SKILL.md 到 `~/.cursor/skills/popola-loom/SKILL.md`、`~/.claude/skills/popola-loom/SKILL.md`、`$CODEX_HOME/skills/popola-loom/SKILL.md`、`<cwd>/.github/copilot-instructions.md`（Copilot 单文件 flatten）。Stage 5 release task 在每次 minor 把 `__version__` 与本 frontmatter 同步 bump（`tests/cli/test_skill_md_canonical.py::test_skill_md_version_matches_package` 卡死 lockstep）。
- **Install / Upgrade**:
  ```bash
  ./install.sh install                # v0.9.6 起 default --from=git（canonical, tracks main）
  ./install.sh install --ref=v0.9.6   # tag-pinned 等价于 git+...@v0.9.6
  # 手动 fallback（PyPI 未发，Q-D-5 偏离默认 / BL-v0.9.x-PyPI）：
  pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.6
  popola skill upgrade --target=cursor   # Stage S4 比对 SHA256 + 备份
  popola init                            # 兜底：idempotent re-run
  ```
  > v0.9.6 GitHub Release-only。`./install.sh install` 默认改走 `--from=git`，不再因 PyPI 镜像 404 而失败（`.local/feedbacks/feedback_for_v0.9.4.md` 第 2-5 行）。当 v0.9.x promote 到 PyPI（`BL-v0.9.x-PyPI`）后，`--from=pypi --version=0.9.x` 是 opt-in。
- **Check**: `popola version` 打印当前 wheel 版本；`cat ~/.cursor/skills/popola-loom/.popola-loom-version` 看安装版（Stage S4 `popola doctor` 检测两者 drift）。
- **Drift detection (v0.5.0+ Stage S4)**: `popola doctor` 走 5 项审计（Skill / Daemon / Lark-cli / ArkTower / IDE config），任一 ✗ 退 1，全 ✓ 退 0；脚本可信赖此 exit code。
- **Idempotency**: 所有 `popola init <verb>` 二次执行打印 `SKIP <path> (already installed)` 不覆盖；要强制刷新走 `popola skill upgrade`。
- **v0.7.x → v0.9.0 升级**：详见 [`docs/MIGRATION_v07_to_v09.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/docs/MIGRATION_v07_to_v09.md)（4 条 spec-locked recipes：A audit `TaskState` predicates；B fix `popola list` shell parsers；C port `POST /hitl/cloud/request` callers；D preserve v0.8.7 `popola relay` 默认行为通过 `[cloud.relay] mode = "confirm"`）。
