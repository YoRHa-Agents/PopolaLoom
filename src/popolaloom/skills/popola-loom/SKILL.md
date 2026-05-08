---
name: popola-loom
version: 0.8.6
description: "PopolaLoom — 跨 CLI 元编排器。当用户要把任务派发给 Cursor / Claude / Codex / Kimi / Copilot 等 agent CLI 并跨终端持久化运行 (spawn → trace task_id → attach in)、查看任务状态、批量调度多 agent、需要 HITL 确认 / Lark 通知，或要查看 daemon 进程健康时使用本 Skill。提供 popola CLI (8+ root verb 含 dispatch / list / status / attach / cancel / probe / init / skill / doctor) + popolaloom-mcp stdio + Lark 双向通道。"
metadata:
  surfaces: ["cli", "ide", "mcp"]
  requires:
    bins: ["popola"]
    pythonVersion: ">=3.11"
  cliHelp: "popola --help"
tier: 1
token_estimate: 3200
last_updated: "2026-05-08"
---

# PopolaLoom Skill

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

### Workflow 6 — Cloud Agent dispatch (`--cli=cursor-cloud`, v0.8.5+ / SSE v0.8.6+)

<!-- updated: 2026-05-08 -->

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
| `timeout_s` | HTTP 单次请求超时 float |
| `api_key` | 覆盖环境变量里的 key（一般由测试/DI 用；生产不推荐写进 envelope） |

**与本地 `cursor` subprocess 的差异（一眼）**：`popola list` / `popola status` 会带出 `runtime="cloud"`、`cursor_agent_id`（通常 `bc-*`）、`cursor_run_id`、`cloud_phase`。v0.8.6+ 起 `popola list` 默认渲染 **`runtime` 列**（在 `task_id` 与 `cli` 之间，`local`/`cloud`），加 `--no-runtime` 可隐藏。取消：`popola cancel` 在云路径走 **`cancel_run` REST**，不是 `SIGTERM`。HITL：若云端任务需要通过 Popola daemon 请人拍板走 Lark，可走 **`cloud`** 通道：`POST /hitl/cloud/request` → block `GET /hitl/cloud/wait/{hitl_id}` → Lark 侧照旧 first-responder wins → **`POST /hitl/cloud/answer/{hitl_id}`**。

**SSE 实时拉取（v0.8.6+）**：`popola attach <task_id> --follow` 在 `runtime=cloud` 任务上**默认启用 SSE**：在 daemon `/attach_stream` 之外另起一个后台线程 pump Cursor 的 `GET /v1/agents/{id}/runs/{run_id}/stream`，把 `cloud.sse.{assistant,tool_call,result,status,parse_error,stream_expired,dedup_drop}` 事件流入同一渲染器。每条 envelope 携带 `(task_id, run_id, stream_session_id, sse_id, seq)` 五元组用于幂等去重。**自动 fallback 到 poll**：遇到 `CursorCloudStreamExpiredError`（HTTP `410 stream_expired`）/ `httpx.ReadError` / `httpx.ConnectError` / `httpx.TimeoutException` / 缺 `CURSOR_API_KEY` / 主线程 Ctrl-C 时，SSE 线程优雅退出并 append 一条 `cloud.sse.fallback_to_poll` 边界事件 + stderr 一行 `[cloud sse] ...` 提示（No-Silent-Failures），既有 poll-driven 视图继续工作。**强制 legacy 路径**：`popola attach <id> --follow --no-stream`。**容忍漂移**：`cloud_poller` 仍是 `cloud_phase` 的唯一写入者（state SoT 锁），SSE 是 append-only 旁路；SSE-side `stream:running` 与 poller-side `cloud_phase=CREATING` 之间最长容忍 ≤3 s 不一致（`interval_s + 1s`，默认 `interval_s=2s`）。

**云端错误提示（v0.8.6+）**：v0.8.6 在 `cursor_cloud.py` 内嵌 16 条 `_ERROR_CATALOG` 条目，按 `(error.code → error.message regex → HTTP status)` 优先级派发到 10 个新 `CursorCloud*Error` 子类，每个都带双语 `.hint_en` / `.hint_zh`（每条 ≤2 句、含 ≥1 个 dashboard URL）+ 稳定 `.cli_exit` 退出码。覆盖：`401 unauthorized` / `api_key_not_found`、`403 plan_required` / `role_forbidden` / `feature_unavailable`、`404 agent_not_found` / `run_not_found`、`409 agent_busy` / `agent_archived` / `run_not_cancellable`、`410 stream_expired`、`422` 三类 GitHub-App 集成错（`RepoAllowlistError` / `GithubAppMissingError` / `GithubAppPermissionError`）、`400/422 validation_error`、`429 rate_limit_exceeded`（v0.8.8 完整重试）、`5xx internal_error`/`upstream_error`。完整提示文本与重试矩阵见研究备忘 `.local/research/v0.8.6_sse/422-error-catalog.md` §3（`.local/` 仅本地存在，已 gitignore）。

**Opt-in quota smoke**：导出 `CURSOR_API_KEY` 后跑 `pytest tests/real_cursor_cloud/ -m real_cursor_cloud`，否则该目录四项 case 仅 **skipped**（见 `pytest` marker `real_cursor_cloud`）。

出处：`.local/research/v0.8.5_cloud_agent/research.md`（Option α）+ `00-decision-matrix-zh.md` §7；v0.8.6 SSE / 422 / state SoT 设计：`.local/research/v0.8.6_sse/{sse-event-schema,state-source-of-truth,422-error-catalog}.md`（local-only research notes）。

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

- **Current**: 0.4.1 — `popola init` (Stage S2/S3 of the v0.5.0 milestone, available on `feature/v0.5.0-skill-install`) 自动安装本 SKILL.md 到 `<scope>/.cursor/skills/popola-loom/SKILL.md`、`<scope>/.claude/skills/popola-loom/SKILL.md`、`$CODEX_HOME/skills/popola-loom/SKILL.md`、`<cwd>/.github/copilot-instructions.md`（Copilot 单文件 flatten）。Stage S5 of v0.5.0 bumps `__version__` (and this frontmatter) to 0.5.0 in lockstep.
- **Check**: `popola version` 打印当前 wheel 版本；`cat ~/.cursor/skills/popola-loom/.popola-loom-version` 看安装版（Stage S4 `popola doctor` 检测两者 drift）。
- **Upgrade**:
  ```bash
  pip install --upgrade popolaloom
  popola skill upgrade --target=cursor   # v0.5.0+ Stage S4，比对 SHA256 + backup .popola-loom-bak.<ts>
  popola init                             # 兜底：手动 re-run 触发 idempotent install
  ```
- **Drift detection (v0.5.0+ Stage S4)**: `popola doctor` 走 5 项审计（Skill / Daemon / Lark-cli / ArkTower / IDE config），任一 ✗ 退 1，全 ✓ 退 0；脚本可信赖此 exit code。
- **Idempotency**: 所有 `popola init <verb>` 二次执行打印 `SKIP <path> (already installed)` 不覆盖；要强制刷新走 `popola skill upgrade`。
