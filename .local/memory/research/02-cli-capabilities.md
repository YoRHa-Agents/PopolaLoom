# 02 · Agent CLI 编排能力矩阵

> **生成时间**: 2026-05-03 02:30 CST  
> **作者**: L3 Task Agent T2 (Research team) ·  devola-flow research-only workflow  
> **范围**: PopolaLoom "weaver" 编排器选型所需的本地 Agent CLI 能力调研  
> **方法**: 本机 `--help` 输出 + 2026 年官方文档 WebSearch/WebFetch  
> **本机捕获时间**: 全部 CLI 的 `--help` 在 2026-05-03 02:27–02:29 CST 捕获于 `/home/agent/workspace/PopolaLoom`

本文盘点 6 个一线 Agent CLI（Claude Code / Cursor Agent / OpenAI Codex / Kimi / GitHub Copilot / 加上 5 个 Bonus），围绕 PopolaLoom 需要的"派发 → 后台运行 → 状态查询 → Resume → 注入 MCP" 五项能力展开。**所有事实链都附带证据**：本机 `--help` 行号或官方 URL。

---

## 概览对比表

| CLI | 一次性调用 | 流式 NDJSON 输出 | 后台/守护原生支持 | 状态/会话查询 | Resume / Attach | MCP 注入 | Hook / 拦截 | SDK | Auth 模型 | 适用场景 |
|---|---|---|---|---|---|---|---|---|---|---|
| **Claude Code** (`claude` 2.1.126) | `claude -p "..."` | `--output-format stream-json` (含 `--include-partial-messages`、`--include-hook-events`) | ❌ 需外包装 (`nohup`/`tmux`/systemd) | 会话日志在 `~/.claude/projects/<dir>/<uuid>.jsonl`；无内置 `ps` 命令 | `--resume <UUID>` / `--continue` / `--session-id <UUID>` / `--fork-session` | `--mcp-config <file>` + `claude mcp add/get/list` + `claude mcp serve`(自身作为 MCP) | 配置式 8 类 hook (`PreToolUse/PostToolUse/SessionStart/...`) | TS `@anthropic-ai/claude-agent-sdk` & Python `claude-agent-sdk` | `ANTHROPIC_API_KEY` env / OAuth(`claude auth`) / `apiKeyHelper` | 长任务、严格 hook 拦截、想接 Anthropic Sonnet/Opus |
| **Cursor Agent** (`cursor-agent` 2026.05.01) | `cursor-agent -p "..."` | `--output-format stream-json` + `--stream-partial-output` | ❌ 同上 | `cursor-agent ls`(交互 picker)；本地会话在 `~/.cursor/chats/<hash>/` | `--resume [chatId]` / `--continue` / `cursor-agent resume`；`create-chat` 预创建 ID | `cursor-agent mcp add/list/list-tools/enable/disable/login` | ❌（无 hook 体系） | TS `@cursor/sdk` (`Agent.create/prompt/resume/list/get/getRun`) + Cloud REST `/v1/agents` | `CURSOR_API_KEY` env / `--api-key` / `cursor-agent login` | 已有 Cursor 订阅、需要 Cloud Background Agents、跨进程恢复 |
| **OpenAI Codex** (`codex` 0.128.0) | `codex exec "..."` | `codex exec --json` (=`--experimental-json` JSONL) | ❌ 但有 `codex app-server` (websocket) 可常驻 | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`；可 `codex resume --all` 列举 | `codex resume <UUID>` / `codex exec resume <UUID>` / `--last` / `codex fork` / `--remote ws://...` | `codex mcp add/list/...` + `codex mcp-server` (自身作为 MCP stdio) | `--ask-for-approval` 三档；execpolicy `.rules` 文件 | 无独立 SDK；通过 `mcp-server` 子命令将 Codex 作为 MCP 客户端集成 | `codex login` (ChatGPT OAuth/device/API key)；`OPENAI_API_KEY` | 严苛 sandbox（read-only / workspace-write / danger-full）、长任务、Codex Cloud |
| **Kimi CLI** (`kimi` 1.41.0) | `kimi --print -p "..."` | `--output-format stream-json` | ❌ | `kimi export <session_id>` 导出会话 ZIP；`~/.kimi/` | `-S/--session [ID]` 或 `-r/--resume [ID]` / `-C/--continue` | `--mcp-config-file/--mcp-config` 多次；`kimi mcp add/list/auth/test` | ❌（无 hook，但有 `--afk` 自动模式） | ❌ 无 SDK，但内置 ACP 服务器：`kimi acp` 暴露 Agent Client Protocol | `kimi login` / `MOONSHOT_API_KEY` (账号 OAuth) | 国内可用、Moonshot 模型、ACP 标准化集成 |
| **GitHub Copilot** (`copilot` 1.0.39) | `copilot -p "..." --allow-all-tools` | `--output-format json` (JSONL，每行一个对象) + `--stream=on` | 半官方：`/keep-alive` 实验性、subagent + `--connect` 远程接入 | `~/.copilot/logs/`；`/sessions` slash + 远程 GitHub web/mobile | `--resume[=ID/前缀/名字]` / `--continue` / `--connect[=ID]` (远程) / `-n/--name` | `--additional-mcp-config @json` + `~/.copilot/mcp-config.json` + `copilot mcp add/list` | ACP server 模式 (`--acp`)；OpenTelemetry 全套 trace/metrics/events | ❌ 无独立 SDK；REST + ACP | OAuth device flow / `COPILOT_GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_TOKEN` | 已有 GH 订阅、CI/CD、需要 GitHub MCP 内置 |
| **Aider** | `aider --message "..."` | ❌ 行式纯文本 | ❌ | git commit 即"会话产物" | 无显式 session ID | 仅本地工具 | ❌ | Python `Coder` 类 | 各 LLM provider key | 轻量、git 友好的本地 patch 流 |
| **Cline (Roo Code) CLI 2.0** | `cline -p "..." -y` | `--json` JSONL | ❌ | TBD（官方文档简略） | 隐式：基于 git 状态 | 通过 IDE 设置 | ❌ | VS Code 扩展为主 | provider key | 已用 Cline 的团队 |
| **Plandex** | `plandex tell "..."` | ❌ | ✅ **原生 `--bg` + `plandex ps/connect/stop`** | `plandex ps` 列举 | `plandex new` plan id；任务 ID | 自定义 model packs | autonomy levels | 服务端可独立部署 | provider key | 长复杂改造，多 plan 管理 |
| **AmpCode** (`amp`) | `amp -x "..."` | `--stream-json` + `--stream-json-input` | ❌ | `amp threads continue` | thread ID 连续对话 | inline + 外部 MCP | ❌ | TS `@sourcegraph/amp-sdk` | `AMP_API_KEY` | Sourcegraph 生态、强 thread 模型 |
| **Continue CLI** | 配置驱动 | TBD | ❌ | TBD | TBD | YAML 配置 | ❌ | TS/Python 扩展 SDK | provider key | 基于配置文件的多 provider |

> **图例**：✅ = 原生支持；❌ = 不支持（需 PopolaLoom 自己包一层）；TBD = 当前公开文档未说明，需后续 spike。

---

## Claude Code (`claude`)

### 调用形态

**版本**: `2.1.126 (Claude Code)`，本机路径 `/root/.npm-global/bin/claude`（基于 `--version` 输出 02:27:42 捕获）。

**一次性 / 非交互**：
```bash
claude -p "Find and fix the bug in src/auth.py" --allowedTools "Read,Edit,Bash"
```
`-p`/`--print` 直接打印响应并退出，且会自动跳过 workspace trust 对话框（参见本机 `--help`：*"The workspace trust dialog is skipped when Claude is run in non-interactive mode (via -p, or when stdout is not a TTY)"*）。来源：[code.claude.com/docs/en/headless](https://docs.claude.com/en/docs/claude-code/headless) + 本机 `claude --help` 02:27:42。

**流式 NDJSON 输出**：
```bash
claude -p "..." --output-format stream-json --include-partial-messages --include-hook-events --verbose
```
- `--output-format` 取值：`text` (默认) / `json` (单结果对象) / `stream-json` (NDJSON 实时) — 仅 `--print` 模式可用。
- `--include-partial-messages` 输出 token 级 delta（要求 `stream-json`）。
- `--include-hook-events` 把 8 类 hook 生命周期事件并入流。
- 来源：本机 `claude --help` + [backgroundclaude.com/blog/stream-json](https://backgroundclaude.com/blog/stream-json)。

**结构化输出 / Schema**：`--json-schema '{"type":"object",...}'` — 让最终回答匹配 JSON Schema，方便派发器解析。

**预算闸门**：`--max-budget-usd <amount>` — 仅 `--print`，超额自动停止，PopolaLoom 必备。

**裸/隔离模式**：`--bare` — 跳过 hooks/LSP/plugin sync/CLAUDE.md 自动发现/keychain 读取，强制 `ANTHROPIC_API_KEY` 或 `apiKeyHelper`，**最适合 PopolaLoom 这种"宿主已经决定一切"的派发器**（来源：本机 `claude --help`）。

### 后台与会话

**后台进程可行性**：原生 *没有* daemon/PID/`--nohup-friendly` 标志。但配合：
- `--no-session-persistence`（仅 `--print`）— 不写本地状态。
- 重定向：`nohup claude -p "..." --output-format stream-json > run.ndjson 2>&1 &`，脱离终端可存活；`pid=$!` 即派发器要保存的句柄。
- 推荐外包装：`tmux new-session -d -s <name> "claude -p ..."` 或 `systemd --user run --unit=claude-<id>`。

**会话存储**：`~/.claude/projects/<cwd-mangled>/<UUID>.jsonl`（本机 02:30 实测：`/root/.claude/projects/-home-agent/736044e9-aa1e-411b-97b9-1df30b2cf72e.jsonl`）。每行一个 JSON 事件，PopolaLoom 可直接 tail。

**会话 ID 控制**：
- `--session-id <UUID>` — **派发器可以预生成 UUID 并强制写入**，无需事后猜会话 ID；这是 PopolaLoom 的关键能力，因为它实现"先分配 ID 再启动子进程"的统一接口。
- `-r/--resume [UUID]` — 恢复指定 / 交互 picker。
- `-c/--continue` — 恢复 cwd 最近一次。
- `--fork-session` — 在 resume 时分叉新 session ID（不污染原会话）。
- `--from-pr <number>` — 通过 PR 号绑定会话。

来源：本机 `claude --help` + [claudecodeguides.com/claude-code-resume-flag-how-to-use-it](https://claudecodeguides.com/claude-code-resume-flag-how-to-use-it/)。

### MCP / Hook 集成点

**MCP 注入**（4 种途径）：
1. `--mcp-config <file...>` — 加载 JSON 文件中定义的 MCP servers（PopolaLoom 派发时最干净的方式）。
2. `--strict-mcp-config` — 配合 1 使用，**只**用 PopolaLoom 注入的，忽略全局 / project `.mcp.json`，保证派发隔离。
3. 持久化：`claude mcp add <name> <commandOrUrl> [args...]` / `claude mcp add-json <name> <json>` — 写到 `~/.claude/`。
4. **作为 MCP 服务端**：`claude mcp serve` — Claude Code 自身可以成为 MCP server 暴露给其他 agent（互操作的关键）。
来源：本机 `claude mcp --help` 02:28:01。

**Hook 体系**（PopolaLoom 拦截/审计的核心）：
- 8 类生命周期：`PreToolUse` / `PostToolUse` / `SessionStart` / `SessionEnd` / `Stop` / `SubagentStop` / `Notification` / `PreCompact` / `PermissionRequest` 等。
- 配置位置：`~/.claude/settings.json`（user）/ `.claude/settings.json`（project）/ `.claude/settings.local.json`（local，不入库）。
- 类型：`"command"`（bash 脚本，可阻塞工具调用）和 `"prompt"`（用 LLM 评估）。
- 与 PopolaLoom 配合：派发器写 `--settings <inline-json>` 把 hook 注入临时配置，例如把 `PreToolUse` hook 指向 PopolaLoom 的本地 webhook，从而拦截每一次工具调用做审计/限流/熔断。
来源：[claude.com/blog/how-to-configure-hooks](https://claude.com/blog/how-to-configure-hooks) + [code.claude.com/docs/en/hooks](http://code.claude.com/docs/en/hooks) + 本机 `claude --help`。

### Resume / Status 查询机制

| 查询/操作 | 命令 | 说明 |
|---|---|---|
| 列举所有会话 | `claude agents` (limited) / 直接读 `~/.claude/projects/` | 无内置 `ps`，PopolaLoom 自己扫文件 |
| 查指定会话 | 读 `~/.claude/projects/<dir>/<UUID>.jsonl` | NDJSON，最新事件在末尾 |
| 派生新会话 | `claude --resume <UUID> --fork-session -p "..."` | 不修改原会话 |
| 持续追加 | `claude --resume <UUID> -p "follow-up"` | 自动将历史包入上下文 |

### Auth 模型

- `ANTHROPIC_API_KEY` env（优先）— PopolaLoom 子进程可直接继承。
- `claude auth` 子命令 → OAuth → 写到 macOS Keychain / Linux libsecret / Windows credman。
- `--bare` 模式强制不读 keychain（适合容器/CI）。
- 第三方 provider：Bedrock / Vertex / Foundry 走自有凭证；通过 `-c provider=...` (本机 `--help` 提示)。

### PopolaLoom 接入要点

1. **预分配 UUID**：派发时 `uuidgen`，传 `--session-id` 让 PopolaLoom 在子进程启动前就拿到关联键。
2. **统一日志通道**：`claude -p ... --output-format stream-json --include-hook-events --include-partial-messages` → tail → 解析 → 推到 PopolaLoom 内部 event bus。
3. **MCP 注入策略**：组合 `--strict-mcp-config --mcp-config /tmp/popola-<id>.json` 写入临时 MCP 清单，**保证容器隔离**。
4. **后台必须自包装**：建议 `systemd-run --user --scope --unit=popola-claude-<id>` 或 `tmux new -d -s popola-<id>`，PID 由 PopolaLoom 维护。
5. **预算 + Permission**：`--max-budget-usd 5 --permission-mode auto --allowedTools "Read,Edit,Bash(git *)"` — 三道闸门。

---

## Cursor Agent CLI / Cursor SDK (`cursor-agent` + `@cursor/sdk`)

### 调用形态

**版本**: `2026.05.01-eea359f`，路径 `/root/.local/bin/cursor-agent`（实测 `cursor-agent --version` 02:27:42）。

**一次性 / 非交互**：
```bash
cursor-agent -p "Refactor src/utils.ts" --output-format stream-json --workspace /repo
```
- `-p/--print` 头部说明：*"Has access to all tools, including write and shell"*（本机 `--help`）。
- `--trust` 仅在 `--print/headless` 模式下生效，跳过 workspace trust 对话。
- `-f/--force`（别名 `--yolo`）— 跳过命令二次确认。

**流式输出**：
```bash
cursor-agent -p "..." --output-format stream-json --stream-partial-output
```
- 三档：`text` / `json` / `stream-json` (NDJSON)。
- `--stream-partial-output`：仅与 `stream-json` 配合，每个 token 一个事件；**注意官方提醒**："只有 `timestamp_ms` 存在且 `model_call_id` 缺失的事件才包含新文本，其他事件须跳过避免重复"（[cursor.com/docs/cli/reference/output-format](https://cursor.com/docs/cli/reference/output-format)）。

**模式切换**：`--mode plan` / `--mode ask` / `--plan`（plan 简写）— PopolaLoom 派发"先 review 后 act" 工作流的关键。

**Worktree 隔离**：`-w/--worktree [name]` 自动在 `~/.cursor/worktrees/<repo>/<name>` 创建独立工作树；`--worktree-base <branch>` 指定基线。**这是 cursor-agent 独有的隔离能力**，PopolaLoom 不需要自己 `git clone`。

来源：本机 `cursor-agent --help` 02:27:43 + [cursor.com/docs/cli/reference/parameters](https://cursor.com/docs/cli/reference/parameters)。

### 后台与会话

**后台**：CLI 本身没有 `--bg`/daemon。需要：
```bash
nohup cursor-agent -p "..." --output-format stream-json > run.ndjson 2>&1 &
```
配合 `tmux`/`systemd-run`/PopolaLoom supervisor。

**会话生命周期**：
- `cursor-agent create-chat` — **预创建空 chat 并返回 ID**（极重要：PopolaLoom 可以在派发前先拿到 chat ID 注册到内部状态机，再用 `--resume <chatId>` 启动）。
- `cursor-agent ls` — 交互式 picker 列举会话。
- `cursor-agent resume` — 直接 resume 最近一次。
- `cursor-agent --resume <chatId>` — 显式 resume by ID。
- `--continue` — 等同 `--resume=-1`。
- 会话存储：本机 02:30 实测 `~/.cursor/chats/<workspace-hash>/` + `~/.cursor/projects/<dir-name>/`；具体 chat 体在 `chats/<hash>/`，含 `last_message.json`、`messages.jsonl` 等。

**Cloud Agents（独立路径）**：
- ID 前缀 `bc-` 自动路由到 Cursor Cloud；REST API 见 [cursor.com/docs/background-agent/api/overview](https://cursor.com/docs/background-agent/api/overview)。
- 通过 SDK：`Agent.create({ cloud: { repos: [{url}], autoCreatePR: true, skipReviewerRequest: true } })` — PopolaLoom 可一键派发"代克隆 repo + 跑任务 + 开 PR"。
- 来源：`/root/.cursor/skills-cursor/cursor-sdk/SKILL.md`。

### MCP / Hook 集成点

**MCP**（命令池见本机 `cursor-agent mcp --help` 02:28:01）：
- `cursor-agent mcp login <id>` — 走 MCP server 的 OAuth 流。
- `cursor-agent mcp list` / `mcp list-tools <id>` — 状态查询。
- `cursor-agent mcp enable/disable <id>` — 开关。
- 配置位置：`.cursor/mcp.json`（项目）+ `~/.cursor/mcp.json`（用户）。
- `--approve-mcps` — 派发时自动批准所有 MCP（headless 必需）。

**SDK 注入 MCP**（重要）：
```ts
const agent = Agent.create({
  apiKey: process.env.CURSOR_API_KEY!,
  model: { id: "composer-2" },
  local: { cwd: "/repo" },
  mcpServers: { popola: { transport: "http", url: "https://localhost:9999/mcp" } },
});
```
**Cloud agent 内 stdio MCP 不可用**（VM 内没有本地进程），必须 HTTP；resume 时 `mcpServers` **不持久化**，必须再传一次。来源：cursor-sdk SKILL.md。

**Hook**：cursor-agent CLI 本身没有 hook 体系；需要的话用 `.cursor/rules/`（静态指令）或外部 git hooks。

### Resume / Status 查询机制

| 操作 | CLI | SDK |
|---|---|---|
| 创建会话 | `create-chat` 返回 ID | `Agent.create()` |
| 列举会话 | `cursor-agent ls`（交互） | `Agent.list({ runtime: "local", cwd })` |
| 查指定 | 读 `~/.cursor/chats/<hash>/<chatId>/` | `Agent.get(agentId, { apiKey })` |
| Resume | `--resume <chatId>` | `Agent.resume(agentId, ...)` |
| 观察某 run | tail messages.jsonl | `Agent.getRun(runId, { runtime, agentId, apiKey })` |

### Auth 模型

- `CURSOR_API_KEY` env（user key 或 service-account key 都可）。
- `--api-key <key>` CLI 入参。
- `cursor-agent login` 浏览器 OAuth；`NO_OPEN_BROWSER=1 cursor-agent login` 走 device-code（headless 友好）。
- `cursor-agent status` / `whoami` — 查当前账号。
- AWS Bedrock 走 `cursor-agent bedrock` 子命令独立配置。

### PopolaLoom 接入要点

1. **派发抽象的"标杆"**：cursor-agent 同时支持 *本地*（`-p` + `--workspace`）和 *cloud*（SDK + `bc-` ID）；PopolaLoom 应抽象 `runtime: "local" | "cursor-cloud"` 字段并直接映射。
2. **Worktree 是免费午餐**：派发并行任务时用 `-w` 让每个 task 跑在独立 worktree，PopolaLoom 不必维护 git clone 池。
3. **预创建 chat ID**：先 `cursor-agent create-chat` 拿 ID 写入 PopolaLoom 状态机，再 `--resume <id>` 启动 — 与 Claude `--session-id` 思路一致。
4. **流式约束**：必须丢弃 `model_call_id` 存在 + 无 `timestamp_ms` 的重复事件；PopolaLoom 流处理器要区分。
5. **Cloud 派发**：用 SDK 而非 CLI（CLI 现版本只对接本地），调用 `Agent.create({ cloud: { repos, autoCreatePR } })` + `skipReviewerRequest: true`，PopolaLoom 取回 `agent.agentId`/`run.id` 写入跟踪表。

---

## OpenAI Codex CLI (`codex`)

### 调用形态

**版本**: `codex-cli 0.128.0`，路径 `/root/.npm-global/bin/codex`（实测 02:27:42）。

**一次性 / 非交互**：
```bash
codex exec "Refactor src/utils.ts" --sandbox workspace-write --ask-for-approval never
```
- 子命令 `codex exec`（别名 `codex e`）专为脚本用。
- `--ask-for-approval never` 是非交互必备（`on-failure` 已弃用）。
- `--sandbox` 三档：`read-only` / `workspace-write` / `danger-full-access`，**Codex 是这 5 个 CLI 中沙箱模型最严谨的**。

**流式 JSON**：
```bash
codex exec --json "..." --output-last-message /tmp/final.txt
```
- `--json`（亦写作 `--experimental-json`）输出 NDJSON 事件流：`thread.started` / `turn.started` / `turn.completed` / `item.*` / `error`。
- `--output-last-message <path>`：把最终 assistant message 单独写文件 — PopolaLoom 简单情况下直接 cat 这个文件即可。
- `--output-schema <FILE>` JSON Schema 校验最终回答。

**安全旁路**：`--dangerously-bypass-approvals-and-sandbox`（别名 `--yolo`）— 仅在已沙箱化的容器里使用。

来源：本机 `codex --help` / `codex exec --help` 02:28:00 + [developers.openai.com/codex/cli/reference](https://developers.openai.com/codex/cli/reference/)。

### 后台与会话

**`codex app-server`** 子命令：
- 把 Codex 拉成 stdio / WebSocket 长驻服务（`--listen ws://IP:PORT`）。
- 支持 `--ws-auth capability-token` / `signed-bearer-token` 鉴权。
- **这是 5 个 CLI 中唯一 *官方* 的 long-running daemon 形态**，PopolaLoom 可直接把它当成"Codex 工作池"。

**会话存储**：本机 02:30 实测 `~/.codex/sessions/2026/05/02/rollout-2026-05-02T02-11-33-019de4bd-2095-7320-a6da-c5c2d41fd46c.jsonl` —— 文件名即包含 `<UUID>`（v7 UUID，含时间戳）；首行 `session_meta` 含 `cwd`、`originator`、`cli_version`、`model_provider`、`base_instructions`。

**Resume**：
- `codex resume <UUID>` — 交互模式恢复。
- `codex exec resume <UUID> "follow-up"` — 非交互续聊。
- `--last` — 最近一次。
- `--all` — 跨 cwd 列举。
- `codex fork [--last|<UUID>]` — 分叉新线程，原会话不变。
- `--remote ws://host:port` — 接到远程 app-server。
- 已知坑：[github.com/openai/codex/issues/14470](https://github.com/openai/codex/issues/14470) — `codex exec --json resume` 在 macOS + MCP 启动后可能挂死；PopolaLoom 派发时建议加超时。

**Ephemeral**：`--ephemeral` — 不写 rollout 文件，PopolaLoom 跑短任务时用。

### MCP / Hook 集成点

**MCP 双向**：
- 客户端：`codex mcp add/list/get/remove/login/logout`，配置 `~/.codex/config.toml` 中 `[mcp_servers.<name>]` 段。
- 服务端：`codex mcp-server` — **Codex 自身作为 MCP server 暴露 stdio**，**PopolaLoom 可以将 Codex 注册为 MCP，再让 Claude/Cursor 通过 MCP 调用 Codex**（跨 CLI 互操作的金钥）。
- `mcp_servers.<name>.required = true` — `codex exec` 失败 fast。
- 已知缺陷：[github.com/openai/codex/issues/17501](https://github.com/openai/codex/issues/17501) — MCP server 启动通知不在 `--json` 流中体现。

**Hook 类**：通过 execpolicy `.rules` 文件（用户级 `~/.codex/policies` 或项目级），声明哪些命令 `allow / prompt / deny`；可被 `--ignore-rules` 旁路。

### Resume / Status 查询机制

```bash
ls ~/.codex/sessions/2026/05/02/  # 直接看 UUID + 时间戳
codex resume --all                 # 内置 picker (TUI)
codex exec resume --last "..."     # 续最近一次
```

PopolaLoom 推荐：派发前 `uuid_session=$(date +%s%N | sha256sum | head -c 32)`（或 UUID v7）写入 PopolaLoom 状态；启动时 `-c session.id="$uuid"`（v0.128 支持 `-c` 覆盖任意 toml 键），再用此 ID 在 `~/.codex/sessions/` 找回 jsonl。

### Auth 模型

- `codex login`：3 选 1
  1. ChatGPT OAuth（推荐，最稳）
  2. device-code
  3. API key 通过 stdin 管道
- 凭证文件：`~/.codex/auth.json`（600 权限）；`OPENAI_API_KEY` env 也认。
- `codex logout` 清除。
- 子进程继承 env 变量即可获 auth；OAuth 模式由共享 `auth.json` 完成（PopolaLoom 派发时不需要重新登录）。

### PopolaLoom 接入要点

1. **唯一原生 daemon**：用 `codex app-server --listen ws://127.0.0.1:7300 --ws-auth capability-token --ws-token-file /run/popola/token`，PopolaLoom 单点连接，长任务投递；多任务用 thread/fork。
2. **MCP 桥接**：`codex mcp-server` 让 Codex 成为其他 CLI 的工具集，**这是 PopolaLoom 实现"Claude 调用 Codex"互操作的低成本路径**。
3. **沙箱标杆**：默认 `--sandbox workspace-write` 比 Claude 的 `--permission-mode` 更明确分级，PopolaLoom 派发协议应直接复用 `read-only/workspace-write/danger-full-access` 三值。
4. **超时保险**：因 issue #14470，`codex exec --json` 必须配套 `timeout 600s` 兜底。
5. **Schema 输出**：派发时附 `--output-schema /tmp/popola-task.json`，让 Codex 强制结构化结果，方便回填。

---

## Kimi CLI / Moonshot 终端体 (`kimi`)

### 调用形态

**版本**: `kimi, version 1.41.0`（agent spec v1, wire protocol 1.9, Python 3.13.13），路径 `/root/.local/bin/kimi -> /root/.local/share/uv/tools/kimi-cli/bin/kimi`（实测 `kimi info` 02:28:01）。

**一次性 / 非交互**：
```bash
kimi --print -p "Refactor app.py" --yolo --output-format stream-json
```
- `--print`：non-interactive，自动消化 `AskUserQuestion`，自动批准工具。
- `-p/--prompt/--command/-c`：用户提示词（注意 `-c` 不是 codex 那种 config 覆盖，是 prompt 别名）。
- `--quiet` ≡ `--print --output-format text --final-message-only`。

**流式 NDJSON**：
```bash
kimi --print -p "..." --output-format stream-json --input-format stream-json < pipe.jsonl
```
- `--output-format stream-json`：每行 `{"role":"assistant","content":"..."}` 或 `{"role":"tool","content":...,"tool_call_id":"..."}`。
- `--input-format stream-json`（仅 `--print`）：让 PopolaLoom 用 stdin 持续投递新轮次 — **类似 Claude 的双向 stream-json**。

**AFK 模式**：`--afk` —— "no user is present, AskUserQuestion is auto-dismissed, tool calls are auto-approved"，`--yolo` 进阶版（也省 plan 提示）。

**特色 Ralph 模式**：`--max-ralph-iterations <N>`（−1 = 无限）— Kimi 独有的"在第一轮之后自动追加迭代轮次"模式，PopolaLoom 可借此跑长链推理任务。

来源：本机 `kimi --help` 02:28:01 + [moonshotai.github.io/kimi-cli/en/customization/print-mode.html](https://moonshotai.github.io/kimi-cli/en/customization/print-mode.html)。

### 后台与会话

**后台**：无原生 daemon。但 `kimi acp`（Agent Client Protocol server）和 `kimi web`（内置 web UI）可作长驻服务接入。

**Session ID**：
- `-S/--session [ID]`（也 `-r/--resume [ID]`）— resume 指定 session；不带 ID 进 picker。
- `-C/--continue`：续 cwd 最近一次。
- 导出：`kimi export <session_id>` → `session-<id>.zip` —— **这是 5 个 CLI 中唯一原生支持把会话打包为 ZIP 归档的**，便于 PopolaLoom 做长期审计/回放。
- 存储：`~/.kimi/`（实测 02:28:01 含 `config.toml`），具体 session 路径在 `--config-file` 默认下。

**退出码**（极有用）：
- `0` 成功
- `1` 永久性失败
- `75` 可重试错误（来源：[moonshotai.github.io/kimi-cli/en/customization/print-mode.html](https://moonshotai.github.io/kimi-cli/en/customization/print-mode.html)）
PopolaLoom 重试策略可直接读 exit code。

### MCP / Hook 集成点

**MCP**：
- `--mcp-config-file <FILE>`（可重复）/ `--mcp-config <JSON>`（可重复） — 多个配置可叠加。
- `kimi mcp add/remove/list/auth/reset-auth/test` — `auth` 走 OAuth-enabled MCP；`test` 直接连测列工具。
- 来源：本机 `kimi mcp --help` 02:28:39。

**Hook**：无显式 hook 体系；但 ACP（Agent Client Protocol）模式 `kimi acp` 让 Kimi 成为标准 ACP server，**外部可以通过 ACP 介入每一轮工具调用**——这是 Kimi 在多 CLI 互操作上的差异化优势。

**Skills / Agents**：
- `--agent default|okabe`（内置预设）/ `--agent-file <FILE>`（自定义）。
- `--skills-dir <DIR>`（可重复） — 自定义 skill 加载点（与 Claude/Cursor skills 体系类似）。

### Resume / Status 查询机制

```bash
kimi --print -p "..." --output-format stream-json   # 启动新会话；首事件含 session_id
# 后续：
kimi -S <session_id> --print -p "follow-up" --output-format stream-json
# 归档：
kimi export <session_id> -o /var/popola/archive/<session_id>.zip
```

### Auth 模型

- `kimi login` / `kimi logout`（账号 OAuth）。
- 配置 toml 中存凭证；env 变量优先级未在 `--help` 中明示。
- 中国大陆可访问，是 Anthropic/OpenAI 之外的关键备选。

### PopolaLoom 接入要点

1. **国内可用**：与 Claude/Cursor 形成互补，PopolaLoom 在 region-aware 派发时优先走 Kimi。
2. **ACP 桥接**：`kimi acp` 让 Kimi 提供 ACP 服务，PopolaLoom 可作为 ACP 客户端统一调度多个 ACP 引擎（GitHub Copilot 也支持 `--acp`）。
3. **会话归档**：`kimi export` 是唯一原生 ZIP 归档 — PopolaLoom 任务完成后自动归档，便于事后审计。
4. **Ralph 长链**：派发深度复杂任务时用 `--max-ralph-iterations 50` 让 Kimi 自我推进；监控 token / 时间。
5. **退出码语义清晰**：`exit 75` → 可重试，`exit 1` → 永久失败 — PopolaLoom 重试机直接读。

---

## GitHub Copilot CLI (`copilot`)

### 调用形态

**版本**: `GitHub Copilot CLI 1.0.39`，路径 `/root/.npm-global/bin/copilot`（实测 02:27:43）。本机 `gh copilot` 是 wrapper，会下载 `copilot` 到 `~/.local/share/gh/copilot`。

**一次性 / 非交互**：
```bash
copilot -p "Fix bug in main.js" --allow-all-tools --output-format json
```
- **`--allow-all-tools` 是非交互模式必需**（也可设 `COPILOT_ALLOW_ALL=1` env，本机 `--help` 明确标注）。
- `-p/--prompt`：执行后退出。
- `-i/--interactive`：起交互但预填提示。
- `-s/--silent`：仅打印 agent response，无 stats（管道友好）。
- `--mode interactive|plan|autopilot` / `--plan` / `--autopilot`。

**JSON 输出**：
```bash
copilot -p "..." --allow-all-tools --output-format json --stream=on
```
- `--output-format text|json`（json 即 JSONL，每行一对象）。
- `--stream=on/off` 控流式 — 与 `--output-format json` 配合即得 NDJSON。
- 来源：[docs.github.com/.../cli-command-reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)，本机 `copilot --help` 02:27:43。

**Autopilot 多轮自治**：
```bash
copilot -p "Migrate legacy API" --autopilot --max-autopilot-continues 50 --no-ask-user --allow-all-tools
```
`--no-ask-user` 关闭 `ask_user` 工具，`--max-autopilot-continues` 截断保险。

### 后台与会话

**远程会话（独有能力）**：
- `--remote` / `--no-remote` — 启/停远程接入；GitHub web 和 mobile app 可远程"steer"会话。
- `--connect[=SESSION-ID]` — **直接连入一个已存在的远程会话**，与 `--resume`/`--continue` 互斥。
- **这是唯一允许"另一台机器从外部接管运行中的 CLI"的产品**——PopolaLoom 跨机器协同最直接的钩子。

**会话管理**：
- `--name/-n NAME` 给会话起名。
- `--continue` — 恢复 cwd 最近一次（fallback 全局最近）。
- `--resume[=VALUE]` — 支持 ID / 7+字符 ID 前缀 / session name / UUID。
- `/sessions` slash 子命令（`info|checkpoints|files|plan|rename|cleanup|prune|delete|delete-all`）— 在交互模式可用，但派发器一般不用。

**Subagent / Fleet**：
- `task` 工具 — 触发 subagent。
- `/fleet [PROMPT]` — 并行跑多 subagent。
- `COPILOT_SUBAGENT_MAX_CONCURRENT`（默认 32，可调到 256）控总并发。
- **Copilot 内置了"派发器"语义**——PopolaLoom 可将其作为参考。

**Keep-alive**：实验性 `/keep-alive [on|busy|<N>m|<N>h]` —— **唯一原生防系统休眠的 CLI**（macOS caffeinate 等价物）。

**日志**：默认 `~/.copilot/logs/`；`--log-dir` 自定义；`--log-level none|error|warning|info|debug|all`。

**OpenTelemetry**：内置全套 OTel trace + metrics + events（`gen_ai.*` namespace + `github.copilot.*` 自定义指标，含 `time_to_first_chunk` 等）；`COPILOT_OTEL_FILE_EXPORTER_PATH` env 自动启用文件导出。**这是 5 个 CLI 中观测能力最全的**——PopolaLoom 直接接 OTel collector。

来源：本机 `copilot --help` + [docs.github.com/copilot/reference/copilot-cli-reference/cli-command-reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)。

### MCP / Hook 集成点

**MCP**：
- `--additional-mcp-config @file.json` 或 JSON 字符串（可重复）— 仅本次 session。
- 持久化：`~/.copilot/mcp-config.json` 或项目级 `.mcp.json`、仓库级 `.github/mcp.json`。
- `copilot mcp add/list/get/...`（CLI）和 `/mcp` slash（交互）双管。
- 内置 MCP：`github-mcp-server` 默认开（可 `--disable-builtin-mcps` 关）；`--add-github-mcp-toolset all` / `--enable-all-github-mcp-tools` 一键开全。
- **唯一对 GitHub OIDC 一等支持**：MCP 配置中 `"oidc": true` 自动注入 `GITHUB_COPILOT_OIDC_MCP_TOKEN` / Bearer 头。

**Hook 替代品**：
- `--acp` 模式：让 Copilot 自身成为 ACP server，外部 ACP 客户端可以介入对话流。
- 没有 Claude 风格的命名 hook。
- 但有 `--secret-env-vars VAR ...` 自动从 shell/MCP 环境中 redact 任意环境变量；`GITHUB_TOKEN` / `COPILOT_GITHUB_TOKEN` 默认 redact。

### Resume / Status 查询机制

| 操作 | 命令 |
|---|---|
| 列举 | `/sessions` slash（交互） / 通过 `--resume` 不带值进 picker |
| 续最近 | `--continue` |
| 续指定 | `--resume=<id>` 或 `--resume="my feature"`（按名字） |
| 远程接入 | `--connect=<id>` |
| 删除 | `/session delete <id>`(交互) |

### Auth 模型

- `copilot login` 默认浏览器 OAuth；token 存系统 credential store（无则 `~/.copilot/`）。
- 环境变量优先级：`COPILOT_GITHUB_TOKEN` > `GH_TOKEN` > `GITHUB_TOKEN`。
- 支持 v2 fine-grained PAT（需 "Copilot Requests" 权限）、Copilot CLI app OAuth、`gh` CLI OAuth；**经典 PAT (`ghp_`) 不支持**。
- 来源：[docs.github.com/en/copilot/reference/.../cli-command-reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference) §`copilot login`。

### PopolaLoom 接入要点

1. **观测最强**：直接打开 OTel 导出（`COPILOT_OTEL_FILE_EXPORTER_PATH=/var/log/popola/copilot.otel.jsonl`），PopolaLoom 不用自己加 instrumentation 也能拿到 token usage / TTFT / 错误堆栈。
2. **远程接入**：`--remote` + `--connect` 是跨机协同的唯一原生原语，PopolaLoom 可以让 worker 机起会话、master 机或运维机用 `--connect=<id>` 接管。
3. **subagent fleet**：派发深度 ≥ 2 的任务时直接用 `/fleet`，PopolaLoom 不必自己 fork 进程。
4. **GH 生态**：`/delegate` 可一键把任务交给 GitHub 的 Cloud Coding Agent（独立后台 agent）；PopolaLoom 在用户已有 GH 订阅时把"长任务"路由这里。
5. **`--share` 副作用**：`--resume + --share` 当前会覆盖原 session.md（[github/copilot-cli#1650](https://github.com/github/copilot-cli/issues/1650)），PopolaLoom 派发时给 share path 加 `<session>-<turn>.md` 后缀避免。

---

## 其它 (Aider / Continue / Cline / Plandex / Amp)

### Aider
- 调用：`aider --message "fix bug in foo.py" --yes-always --no-auto-commits foo.py`。`--message` 一次性，`--yes-always` 跳过确认，`--no-auto-commits` 由 PopolaLoom 自己 commit。
- Python API：`from aider.coders import Coder; coder.run("...")` — 进程内可控。
- `--auto-test --test-cmd 'pytest'` 跑修-测-修循环（headless 文档不全：[#4923](https://github.com/Aider-AI/aider/issues/4923)）。
- 无 session ID / 无 stream-json；会话即 git history，PopolaLoom 用分支隔离。
- 来源：[aider.chat/docs/scripting.html](https://aider.chat/docs/scripting.html)。

### Continue CLI
- 主要是 IDE 扩展（VS Code/JetBrains），CLI 较薄；通过 `~/.continue/config.yaml` 声明 provider。
- 无类似 Claude/Cursor 的 stream-json 一等公民支持。PopolaLoom 把它当 OK-but-niche 备选。

### Cline (Roo Code) CLI 2.0 — 已发布 headless mode
- `cline -p "..." -y --json` —— `-y/--yolo` 全自动批准，`--json` JSONL，且 stdin/stdout pipe 自动进 headless（来源：[docs.cline.bot/cline-cli/three-core-flows](https://docs.cline.bot/cline-cli/three-core-flows)）。
- `-p` plan 模式 / `-a` act 模式。
- PopolaLoom 集成成本中等，主要看 Cline 用户基数。

### Plandex — **唯一原生 daemon 派发**
- `plandex tell --bg "task"` + `plandex ps/connect/stop` —— **PopolaLoom 抽象的对照原型**。
- `plandex apply --full` 自动批准 + auto-exec。
- `plandex set-config auto-exec true` 全局开关。
- v2 默认禁用 background tasks（autonomy 兼容性），需手动调级。
- 来源：[docs.plandex.ai/core-concepts/background-tasks](https://docs.plandex.ai/core-concepts/background-tasks)。

### AmpCode (`amp`) — **非交互 / SDK 双线**
- CLI: `amp -x "..." --stream-json --stream-json-input` —— stdin 持续投递、stdout NDJSON 与 Claude/Kimi 一致。
- `amp threads continue` 跨 invocation 续 thread。
- TS SDK：`@sourcegraph/amp-sdk` 暴露 `execute()` 与 type-safe API。
- 来源：[ampcode.com/news/streaming-json](https://ampcode.com/news/streaming-json) + [registry.npmjs.org/@sourcegraph/amp-sdk](https://registry.npmjs.org/@sourcegraph/amp-sdk)。

---

## PopolaLoom 派发抽象建议

下面给出"周末就能落地"的派发协议草案 + 实施路径。

### 1. 统一 dispatch 接口（最低公共字段）

```ts
interface PopolaTaskDispatch {
  // === 核心标识 ===
  taskId: string;              // PopolaLoom 自己生成的 UUID（v7 推荐，含时间戳）
  cli: "claude" | "cursor-agent" | "codex" | "kimi" | "copilot" | "amp" | string;
  cliVersion?: string;          // 锁定版本，避免行为漂移

  // === 输入 ===
  prompt: string;               // 主 prompt
  promptFromStdin?: boolean;    // 大 prompt 改走 stdin
  inputFormat?: "text" | "stream-json";
  attachments?: { type: "file"|"image"; path: string }[];

  // === 工作区 ===
  workspace: string;            // 绝对路径
  worktree?: string;            // cursor-agent 友好；其他 CLI 由 PopolaLoom 实现 git worktree
  addDirs?: string[];           // 额外可读目录

  // === 沙箱 + 权限（统一 3 档，向 codex 模型对齐） ===
  sandbox: "read-only" | "workspace-write" | "danger-full-access";
  allowedTools?: string[];      // 白名单
  disallowedTools?: string[];   // 黑名单
  autoApprove?: boolean;        // 触发各 CLI 的 yolo/--allow-all/--afk

  // === 模型 ===
  model?: string;               // claude/cursor/codex/kimi/copilot 各自模型 ID
  fallbackModel?: string;       // 降级
  maxBudgetUsd?: number;
  maxTurns?: number;            // 限制轮数

  // === 流式 / 输出 ===
  outputFormat: "text" | "json" | "stream-json";
  outputSchema?: object;        // JSON Schema 约束最终回答
  outputSink: { type: "file"|"socket"|"http"; target: string };

  // === 会话 ===
  sessionId?: string;           // 若 CLI 支持预生成（claude/cursor/codex），传入；否则 CLI 启动后回填
  resumeFrom?: string;          // 续指定会话
  forkFrom?: string;            // codex/claude --fork-session
  ephemeral?: boolean;          // 不写盘
  
  // === MCP 注入 ===
  mcpServers?: Record<string, McpServerSpec>;
  strictMcp?: boolean;          // 仅用注入的，忽略全局
  
  // === Hook（仅 claude 一等支持，其他降级到 ACP/wrapper） ===
  hooks?: Record<HookType, HookSpec>;
  
  // === 后台 / 守护 ===
  detach: "none" | "tmux" | "systemd-run" | "screen" | "nohup";
  detachUnit?: string;          // popola-<taskId>
  keepAlive?: boolean;          // copilot /keep-alive 或 PopolaLoom 心跳
  timeoutMs?: number;
  
  // === 远程 / Cloud ===
  runtime: "local" | "cursor-cloud" | "codex-cloud" | "remote-attach";
  remote?: { url: string; authTokenEnv: string };  // codex --remote 或 copilot --connect
  
  // === Auth ===
  authMode: "env" | "oauth-shared" | "key-file";
  authEnvVars?: string[];       // 子进程必须继承的 env

  // === 审计 ===
  redactEnvVars?: string[];     // copilot --secret-env-vars
  recordTo?: string;            // 强制 PopolaLoom 录制
}
```

### 2. 各 CLI 的"最低公共能力"

| 能力 | claude | cursor | codex | kimi | copilot |
|---|---|---|---|---|---|
| `-p/--print/--prompt` 一次性 | ✅ | ✅ | ✅(`exec`) | ✅ | ✅ |
| stream-json 输出 | ✅ | ✅ | ✅(`--json`) | ✅ | ✅(`--output-format json`) |
| stream-json 输入 | ✅ | ❌ | ❌ | ✅ | ❌ |
| 可恢复会话（带 ID） | ✅ | ✅ | ✅ | ✅ | ✅ |
| **预生成 session ID** | ✅(`--session-id`) | ✅(`create-chat`) | ⚠️(`-c session.id=`) | ❌ | ❌(`-n NAME` 间接) |
| MCP 注入（cli flag） | ✅ | ⚠️(子命令) | ⚠️(toml) | ✅ | ✅ |
| sandbox 三档 | ⚠️(`--permission-mode`) | ⚠️(`--sandbox enabled/disabled`) | ✅ | ⚠️(`--yolo/--afk`) | ⚠️(`--allow-all`) |
| schema 输出 | ✅(`--json-schema`) | ❌ | ✅(`--output-schema`) | ❌ | ❌ |
| 预算闸门 | ✅(`--max-budget-usd`) | ❌ | ❌ | ❌ | ❌ |
| 远程接入 | ❌ | ⚠️(SDK) | ✅(`--remote`) | ⚠️(ACP) | ✅(`--connect`) |
| 原生 daemon | ❌ | ❌ | ✅(`app-server`) | ❌ | ⚠️(remote 非 daemon) |

**最低公共能力（PopolaLoom MUST 支持）**：
1. `prompt` + `--print` 一次性。
2. NDJSON 输出（兜底解析）。
3. 会话 ID（即使是 CLI 启动后回填）。
4. `--resume <id>` 续聊。
5. 工作目录 + 额外可写目录。
6. 工具白/黑名单。
7. MCP 配置注入（最差也通过 toml/json 文件）。
8. Auth 通过 env 继承。

### 3. 哪些 CLI 天然支持 daemon，哪些需要 supervisor

| CLI | 原生 daemon | PopolaLoom 推荐 supervisor |
|---|---|---|
| **Codex** | ✅ `codex app-server --listen ws://...` | 直接连，再用 PopolaLoom 工作池路由 |
| **Plandex** (bonus) | ✅ `--bg` + `plandex ps/connect/stop` | 直接用 |
| **Copilot** | ⚠️ `--remote` + `--connect` (跨机接管，不算 daemon) | systemd-run + `--remote` 跑工作机 |
| Claude / Cursor / Kimi / Aider / Cline / Amp | ❌ | **systemd-run --user**（生产）/ **tmux**（开发）/ **screen**（兜底）/ **PM2** （Node 用户）/ **nohup**（最小） |

**推荐方案**（按环境优先级）：
1. **生产 Linux 主机**：`systemd-run --user --scope --unit=popola-<cli>-<taskId> -- <cli> --print ...`
   - 每任务一个 ephemeral unit，自动清理；崩溃可 `journalctl --user -u popola-...` 取日志。
2. **共享 SSH 多人开发机**：`tmux new-session -d -s popola-<taskId> -x 200 -y 50 "<cli> --print ... 2>&1 | tee /var/popola/<taskId>.ndjson"`
   - `tmux ls | grep popola-` 即 PopolaLoom 的 `ps`。
3. **容器内**（无 systemd / tmux）：`nohup <cli> --print ... > /tmp/<taskId>.ndjson 2>&1 & echo $! > /tmp/<taskId>.pid`
   - `kill -0 $(cat <pid>)` 探活。
4. **跨平台用户进程**：PM2（`pm2 start --name popola-<taskId> ...`）— Node 生态最熟。

PopolaLoom 自己应实现 `Supervisor` interface 抽象：`start(cmd, env, cwd) -> processId` / `status(processId) -> Status` / `tail(processId, since) -> EventStream` / `stop(processId, signal)`。

### 4. Resume 的最小协议（session_id 可移植性）

**事实**：5 个主流 CLI 的 session ID 互不通用（claude UUID / cursor 自定义 hash / codex UUID v7 / kimi 内部 / copilot 名字+ID）。

**PopolaLoom 的最小协议**：在 PopolaLoom 内维护 `taskId → (cli, native_session_id, cwd, started_at, last_event_at)` 映射表（SQLite 即可）。Resume 时：
```python
def resume(taskId: str, prompt: str):
    row = db.get(taskId)
    cmd = build_cmd(row.cli, "--resume", row.native_session_id, "-p", prompt, ...)
    run_in_supervisor(cmd, ...)
```

**最小 Resume 接口（统一）**：
```yaml
PopolaResume:
  taskId: required          # PopolaLoom 内部 ID
  followUpPrompt: required
  outputSink: required
  fork: bool = false        # 不污染原会话（claude --fork-session / codex fork）
  ephemeral: bool = false   # 不写盘
```

**关键技巧**：派发新任务时，**优先选支持"预生成 session ID"的 CLI**（claude --session-id, cursor create-chat, codex -c session.id=），让 PopolaLoom 在 spawn 之前就完成绑定，省去"竞态地等 ID 回写"。

### 5. MCP 作为派发协议的可行性

**结论**：✅ 可行，且是 PopolaLoom 实现"跨 CLI 互操作"最便宜的路径。

- 6 个主流 CLI 中 **5 个原生支持 MCP 客户端**（aider 不支持），其中 **3 个还能作为 MCP server**：
  - `claude mcp serve` — Claude 自己暴露为 MCP server。
  - `codex mcp-server` — Codex 自己暴露为 MCP stdio server。
  - `copilot --acp` / `kimi acp` — Agent Client Protocol（与 MCP 同源），可双向。
- **PopolaLoom 自己实现一个 PopolaMcpServer**（HTTP transport，因为 cloud agents 不能 stdio），把"调用其他 CLI 跑子任务"封成 MCP tools，例如：
  - tool `dispatch_to_codex({prompt, sandbox})` → PopolaLoom 真正派发 Codex 子进程并返 final message。
  - tool `dispatch_to_kimi(...)`、`dispatch_to_amp(...)` 等。
- 这样 Claude Code 派发任务给 Codex 不需要 PopolaLoom CLI 介入：Claude 直接调 MCP tool，PopolaLoom 监听 MCP 调用并转发。

**两层 MCP 架构**（推荐）：
```
                          ┌──────────────────┐
       Claude Code ──┐    │  PopolaLoom      │    ┌── Codex (--mcp-server)
                     │    │  ─ MCP server ───┼─── │
   Cursor Agent  ────┼──> │  ─ Supervisor ───┼─── │
                     │    │  ─ State store   │    └── Kimi (acp)
       Copilot   ────┘    └──────────────────┘
       (mcp client)                                Copilot (--acp)
```

**注意事项**：
1. Cloud agent 不支持 stdio MCP，PopolaLoom MCP server 必须用 HTTP/streamable-HTTP transport。
2. Resume 时各 CLI 不持久化 inline `mcpServers`（cursor SDK 已确认，其他类似），PopolaLoom 派发包装层每次 resume 都要重新注入。
3. MCP authentication：HTTP 方案用 capability token（参考 codex `--ws-token-file`）；OIDC 给 GH 生态（copilot 默认）。

### 6. 实施优先级建议

1. **Day 1 MVP**：Supervisor 抽象（systemd-run + tmux + nohup 三后端）+ Codex `app-server` 原生路径 + Claude `--session-id` + `stream-json` ndjson 解析器。
2. **Day 2-3**：Cursor SDK 集成（cloud + local）+ Kimi ACP 桥接。
3. **Week 2**：自家 PopolaMcpServer（HTTP）暴露 `dispatch_to_<cli>` tools；统一 OTel 接入（直接消费 Copilot 的 OTel 流）。
4. **Week 3**：Copilot `--remote` + `--connect` 跨机协同；GH 生态深耦合。
5. **Bonus**：Aider Python API 直接 in-process 调用 + Plandex 作"长任务" 引擎模式参考。

---

## 附录 A · 各 CLI 一行命令速查（PopolaLoom 派发模板）

```bash
# Claude Code
claude -p "$PROMPT" --session-id "$UUID" --output-format stream-json \
  --include-partial-messages --include-hook-events \
  --strict-mcp-config --mcp-config /tmp/popola-$UUID.json \
  --permission-mode auto --max-budget-usd 5 --bare 2>&1 | tee /var/popola/$UUID.ndjson

# Cursor Agent
cursor-agent --print "$PROMPT" --output-format stream-json --stream-partial-output \
  --resume "$(cursor-agent create-chat)" --sandbox enabled --workspace "$CWD" \
  --approve-mcps --trust 2>&1 | tee /var/popola/$UUID.ndjson

# OpenAI Codex
codex exec "$PROMPT" --json --output-last-message /tmp/$UUID.last \
  --sandbox workspace-write --ask-for-approval never \
  --output-schema /etc/popola/schemas/task-result.json --cd "$CWD" \
  -c "session.id=\"$UUID\"" 2>&1 | tee /var/popola/$UUID.ndjson

# Kimi
kimi --print -p "$PROMPT" --output-format stream-json --yolo --afk \
  --work-dir "$CWD" --mcp-config-file /tmp/popola-$UUID.json \
  --max-ralph-iterations 20 2>&1 | tee /var/popola/$UUID.ndjson

# GitHub Copilot
COPILOT_ALLOW_ALL=1 COPILOT_OTEL_FILE_EXPORTER_PATH=/var/popola/$UUID.otel.jsonl \
copilot -p "$PROMPT" --output-format json --autopilot --max-autopilot-continues 30 \
  --no-ask-user --add-dir "$CWD" --name "popola-$UUID" \
  --additional-mcp-config @/tmp/popola-$UUID.json --log-dir /var/popola/copilot-logs/ \
  2>&1 | tee /var/popola/$UUID.ndjson

# Bonus: AmpCode
AMP_API_KEY=$AMP_KEY amp -x "$PROMPT" --stream-json --stream-json-input < /dev/null

# Bonus: Plandex (原生 background)
plandex tell --bg "$PROMPT"
plandex ps   # 列举 background tasks
plandex connect <task-id>
```

---

## 附录 B · 本机捕获到的关键存储路径（供 PopolaLoom 直接 tail/扫描）

| CLI | 路径 | 内容 |
|---|---|---|
| Claude Code | `~/.claude/projects/<dir-mangled>/<UUID>.jsonl` | 每行 1 个事件，UUID = sessionId |
| Claude Code | `~/.claude/settings.json` | hooks / mcpServers |
| Claude Code | `~/.claude/sessions/` | 会话状态（700 权限） |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<UUID>.jsonl` | rollout 文件，首行 session_meta |
| Codex | `~/.codex/auth.json` (600) | OAuth/API key |
| Codex | `~/.codex/config.toml` | MCP servers + profiles |
| Codex | `~/.codex/logs_2.sqlite` | SQLite，含 telemetry (PopolaLoom 可 SELECT) |
| Cursor | `~/.cursor/chats/<workspace-hash>/<chat-id>/` | messages.jsonl + last_message.json |
| Cursor | `~/.cursor/projects/<dir-name>/` | 项目级状态 |
| Cursor | `~/.cursor/cli-config.json` | CLI 配置 |
| Cursor | `~/.cursor/agent-cli-state.json` | CLI 全局状态 |
| Kimi | `~/.kimi/config.toml` | 配置（sessions 可通过 `kimi export <id>` 取） |
| Copilot | `~/.copilot/logs/` | 默认日志 |
| Copilot | `~/.copilot/mcp-config.json` | MCP servers |
| Copilot | `~/.copilot/settings.json` | 用户配置 |

---

## 附录 C · 互操作 / Risks 速记

1. **Claude `--bare` vs MCP** — `--bare` 跳过 MCP 自动发现，**必须配合 `--mcp-config` 显式注入**，否则 PopolaLoom 派发无 tool。
2. **Cursor stream-partial-output 去重** — 见上文，按 `timestamp_ms`+`model_call_id` 判重。
3. **Codex `exec --json resume` macOS 挂死**（[issue #14470](https://github.com/openai/codex/issues/14470)）— Linux 暂未复现，但派发器一定加超时。
4. **Codex MCP startup 不在 stream**（[issue #17501](https://github.com/openai/codex/issues/17501)）— PopolaLoom 派发 MCP 启动失败 only 通过进程 stderr 感知。
5. **Copilot `--share + --resume` 覆盖**（[issue #1650](https://github.com/github/copilot-cli/issues/1650)）— 不同 turn 用不同 share path。
6. **Copilot 1.0.6 schema 兼容**（[issue #2089](https://github.com/github/copilot-cli/issues/2089)）— 跨版本 resume 需小心；PopolaLoom 应锁版本。
7. **Cloud agent + stdio MCP**：所有 cloud runtimes（cursor cloud / codex cloud / GitHub coding agent）**禁止 stdio MCP**，仅 HTTP/streamable-HTTP；PopolaLoom MCP server 一律 HTTP。
8. **Resume 时 mcpServers 不持久化**（cursor SDK 已确认，估计其他亦然）— 包装层每次 resume 都重传。
9. **Auth 共享**：所有 CLI 都接受子进程继承 env（`ANTHROPIC_API_KEY`/`CURSOR_API_KEY`/`OPENAI_API_KEY`/`COPILOT_GITHUB_TOKEN` 等），PopolaLoom 派发器构造子进程 env 即可；OAuth 共享需先 `*-cli login` 一次写盘。
10. **退出码语义**：Kimi 明示 `0/1/75` 三态；Copilot/Claude/Codex/Cursor 多为 `0=成功 / non-zero=失败`，PopolaLoom 重试机统一以 Kimi 三态为协议，将其他 CLI 的非零码默认归为"永久失败"，再针对个别 stderr 模式打补丁。

---

## 附录 D · 证据来源索引

### 本机 `--help` 捕获时间表（2026-05-03 CST）
- 02:27:42 — `claude --version` / `claude --help`
- 02:27:42 — `codex --version` / `codex --help` / `codex exec --help`
- 02:27:43 — `cursor-agent --version` / `cursor-agent --help`
- 02:27:43 — `copilot --version` / `copilot --help`
- 02:28:01 — `kimi --version` / `kimi --help` / `kimi info`
- 02:28:01 — `claude mcp --help` / `claude agents --help`
- 02:28:01 — `cursor-agent ls/resume/mcp/agent/create-chat --help`
- 02:28:39 — `codex mcp/resume/mcp-server --help`、`kimi acp/mcp/export --help`

### 官方文档 URL（2026-05 验证）
- Claude Code headless: https://docs.claude.com/en/docs/claude-code/headless
- Claude Code hooks: https://code.claude.com/docs/en/hooks · https://claude.com/blog/how-to-configure-hooks
- Claude Agent SDK: https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk · https://www.npmjs.com/package/@anthropic-ai/claude-agent-sdk · https://github.com/anthropics/claude-agent-sdk-python
- Cursor CLI 头道入口: https://cursor.com/docs/cli/using · https://cursor.com/docs/cli/headless · https://cursor.com/docs/cli/reference/parameters · https://cursor.com/docs/cli/reference/output-format
- Cursor SDK: https://cursor.com/docs/api/sdk/typescript（也见 `/root/.cursor/skills-cursor/cursor-sdk/SKILL.md`）
- Cursor Cloud Agents API: https://cursor.com/docs/background-agent/api/overview
- Codex CLI 参考: https://developers.openai.com/codex/cli/reference/ · https://developers.openai.com/codex/noninteractive/
- Codex 已知 issues: https://github.com/openai/codex/issues/14470 · https://github.com/openai/codex/issues/17501
- Kimi CLI: https://moonshotai.github.io/kimi-cli/ · https://moonshotai.github.io/kimi-cli/llms.txt · https://moonshotai.github.io/kimi-cli/en/customization/print-mode.html
- Kimi changelog: https://github.com/MoonshotAI/kimi-cli/blob/main/CHANGELOG.md
- GitHub Copilot CLI: https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference · https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference · https://docs.github.com/en/copilot/concepts/agents/copilot-cli/autopilot
- Copilot CLI 已知 issues: https://github.com/github/copilot-cli/issues/1650 · https://github.com/github/copilot-cli/issues/2089 · https://github.com/github/copilot-cli/issues/52
- Aider scripting: https://aider.chat/docs/scripting.html · https://github.com/Aider-AI/aider/issues/4923
- Cline headless: https://docs.cline.bot/cline-cli/three-core-flows
- Plandex: https://docs.plandex.ai/cli-reference/ · https://docs.plandex.ai/core-concepts/background-tasks
- AmpCode: https://ampcode.com/manual · https://ampcode.com/news/streaming-json · https://www.npmjs.com/package/@sourcegraph/amp-sdk

---

> **维护提示**：CLI 演进很快（kimi/copilot 周更，codex 已是 0.128，cursor-agent 每周 nightly）。建议 PopolaLoom 实现一个 `popola check-cli-versions` 周期任务，把当前安装的版本号与"已测试通过"清单做 diff，遇到不兼容版本拉警报。
