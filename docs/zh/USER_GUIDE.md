---
layout: default
title: 用户指南
description: popola CLI、MCP、HITL、Lark 和配置项的中文参考。
lang: zh
translation_url: /USER_GUIDE.html
---

# PopolaLoom — 用户指南 (v0.9.7)

<!-- updated: 2026-05-10 -->

> PopolaLoom 自 v0.9.0 起进入 GA 稳定边界；当前公开文档面向 v0.9.7。首次使用请先看 [`QUICKSTART.md`](QUICKSTART.md)，需要演示路径和示例输出请看 [`DEMO.md`](DEMO.md)。需要纯 Cloud Agent 启动时，先配置 Cursor API key，再运行仓库根目录的 `cloud-quickstart.sh`。

## 目录

- [心智模型](#心智模型)
- [CLI 速查](#cli-速查)
- [IDE 与 Skill](#ide-与-skill)
- [MCP 集成](#mcp-集成)
- [HITL 工作流](#hitl-工作流)
- [Credentials 与安全存储（v0.9.2+）](#credentials-与安全存储v092)
- [`popola init` 交互式 intake（v0.9.5+）](#popola-init-交互式-intakev095)
- [Self-hosted worker handoff（v0.9.1+）](#self-hosted-worker-handoffv091)
- [Hands-off envelope](#hands-off-envelope)
- [配置](#配置)
- [故障排查](#故障排查)
- [架构深挖](#架构深挖)

## 心智模型

PopolaLoom 由三部分组成：

- `popolad` 边车 daemon：绑定 `$POPOLA_HOME/popolad.sock`，负责派发、查询、取消、探活和任务状态。
- `popola` CLI：所有用户命令都通过 UDS RPC 调 daemon。
- `popolaloom-mcp` stdio server：把同一组能力暴露给 Cursor / Claude 等支持 MCP 的 IDE。

每个任务都会启动一个独立的 agent CLI 子进程，例如 `cursor-agent`、`claude` 或 `codex`。daemon 负责捕获 stdout/stderr、写 NDJSON 事件日志、更新任务状态，并在终态发送可选的 Lark 通知。你可以从任意终端运行 `popola dispatch "..." --cli=cursor`，关掉终端后再从另一个 shell 里 `popola attach <id> --follow`，任务仍然可见、可追踪、可取消。

## CLI 速查

| 命令 | 作用 | 示例 |
|---|---|---|
| `popola dispatch <prompt> --cli=<name>` | 在指定 agent CLI 上启动任务 | `popola dispatch "fix foo.py" --cli=cursor` |
| `popola list [--all]` | 查看运行中任务，或包含终态任务 | `popola list --all` |
| `popola status <task_id>` | 查看单个任务状态 | `popola status cursor-23e74ec18917` |
| `popola attach <task_id> --follow` | 订阅任务事件流 | `popola attach <id> --follow` |
| `popola cancel <task_id>` | SIGTERM、宽限、必要时 SIGKILL | `popola cancel <id>` |
| `popola probe` | daemon 探活 | `popola probe` |
| `popola popolad start/stop/status` | daemon 生命周期 | `popola popolad start` |
| `popola init` | 注册 Skill 到本机 IDE，也可运行交互式 intake | `popola init --interactive` |
| `popola auth cursor set --validate` | 把 Cursor API key 写入 OS keyring 并校验 | `popola auth cursor set --validate` |
| `popola cloud worker start` | 启动或复用 Cursor self-hosted worker | `popola cloud worker start --worker-dir "$(pwd)"` |
| `popola doctor` | Skill / daemon / Lark / ArkTower 健康检查 | `popola doctor --strict` |

任务生命周期是 `dispatched -> running -> interrupted -> completed / failed / canceled`。当 LangGraph 节点调用 `interrupt()` 时，任务进入 `interrupted`，HITL 提示会广播到多个通道。

## IDE 与 Skill

`popola init` 会自动探测并安装 canonical Skill：

| IDE | 安装位置 |
|---|---|
| Cursor global | `~/.cursor/skills/popola-loom/SKILL.md` |
| Cursor project | `<repo>/.cursor/skills/popola-loom/SKILL.md` |
| Claude Code global | `~/.claude/skills/popola-loom/SKILL.md` |
| Claude Code project | `<repo>/.claude/skills/popola-loom/SKILL.md` |
| Codex | `$CODEX_HOME/skills/popola-loom/SKILL.md` |
| Copilot | `<repo>/.github/copilot-instructions.md` |

`init` 是首次安装入口且幂等；`popola skill upgrade --target=all` 用于升级后覆盖刷新 Skill，覆盖前会写备份。

## MCP 集成

把 MCP server 加到 IDE 配置后，host agent 可以直接调用同一组工具：

```jsonc
{
  "mcpServers": {
    "popolaloom": {
      "command": "python",
      "args": ["-m", "popolaloom.mcp.server"]
    }
  }
}
```

主要工具包括 `popola_submit`、`popola_list`、`popola_status`、`popola_attach_stream`、`popola_cancel`、`popola_relay`、`popola_supervise`、`popola_supply_feedback` 和 `popola_inject_subtask`。

## HITL 工作流

任务内部调用 `interrupt()` 后，daemon 会把提示广播到 5 个通道：

| 通道 | 用户如何回答 |
|---|---|
| Lark | 点击交互卡片上的通过 / 拒绝 |
| IDE | 在 host IDE 的 chooser UI 中选择 |
| CLI | `popola pending` 后运行 `popola feedback <hitl_id> <answer>` |
| MCP | 调用 `popola_supply_feedback` |
| Web | Web 入口与 MCP / CLI 状态共享同一个 HITL store |

第一个回答通过 `hitl/sync.py:mark_answered` 原子落盘并恢复任务；迟到回答会看到已回答状态并退出。

## Credentials 与安全存储（v0.9.2+）

Cursor Cloud dispatch、`popola cloud runs`、跨 PR relay、cloud attach/cancel，以及 Enterprise self-hosted worker pool 都需要 Cursor Cloud Agents API key。解析顺序固定为：显式 override（测试 / 库调用）> `CURSOR_API_KEY` 环境变量 > OS keyring > missing。

推荐路径：

```bash
./install.sh install --with-credentials      # 新机器，一次性带上 keyring extra
./install.sh update --with-credentials       # 已安装环境，补上 keyring extra
popola auth cursor set --validate            # 隐藏输入，写入 OS keyring，并验证 REST
popola auth cursor status --json             # 只显示 source/backend/fingerprint，不显示原值
```

在没有 SecretService / D-Bus 的 Linux 容器里，`--with-credentials` 只能安装 Python keyring 包，不能凭空提供 OS 后端。这时请显式使用 `CURSOR_API_KEY` 或权限为 `0o600` 的 `.env` 文件。

## `popola init` 交互式 intake（v0.9.5+）

v0.9.5 把 `popola init` 从单纯 Skill 安装器扩展成首次运行 intake：如果操作者在初始化时已经拿到 Cursor API key，可以在同一次命令里交给 PopolaLoom，写入与 `popola auth cursor` 相同的凭据解析器，后续 cloud dispatch 不需要再次询问。

交互式路径仍先处理 IDE 计划：探测 Cursor、Claude、Codex、Copilot 和本地 `.local/` workspace，询问安装哪些目标、每个目标用 global 还是 project scope，打印计划，确认后再写文件。凭据步骤由 `--configure-cursor-auth` 控制，放在最后执行；所以 keyring 不可用只会让凭据存储降级，不会回滚已经成功的 Skill / scaffold 写入。

```bash
popola init --interactive --configure-cursor-auth
# PopolaLoom interactive setup wizard
# Auto-detected: cursor, claude
# Install for Cursor? [Y/n]: y
# Secure Cursor API key storage (v0.9.2+):
#   Store a Cursor API key in the OS keyring now? [y/N]:
```

非交互 bootstrap 可以使用两种 flag：`--cursor-api-key` 直接接收值，`--cursor-api-key-file` 读取文件中第一条非空 UTF-8 行。两者互斥，空值会直接 `BadParameter`，并且二者都会隐式启用 `--configure-cursor-auth`，适用于 auto-detect、单 IDE verb、`--target=cloud-only` 和 `--interactive`。

```bash
popola init --cursor-api-key "cr_..."
popola init --cursor-api-key-file ./secrets/cursor.key
popola init cursor --cursor-api-key "cr_..."
popola init --target=cloud-only --cursor-api-key-file ./secrets/cursor.key
```

实际写入发生在 `popolaloom.credentials.store_cursor_api_key`，目标是 OS keyring 的 `popolaloom.cursor/default` 槽位。输出只显示 backend 名称和 SHA-256 前 12 位 fingerprint；原始 key 不会出现在 stdout、stderr、日志、handoff envelope、`$POPOLA_HOME/credentials.toml` 或任何 `--json` 状态面里。

`--dry-run` 会显式跳过凭据持久化：

```bash
popola init --dry-run --cursor-api-key "cr_..."
# credential setup skipped during dry-run preview (--dry-run is set; secret persistence requires a real install)
```

`--no-cursor-key` 是给本地-only、CI 注入密钥、或团队政策禁止写本机 keyring 的环境使用的退出口。之后只要环境里有 `CURSOR_API_KEY`，或者手动运行 `popola auth cursor set --validate`，cloud surfaces 仍然可用。

> See: `src/popolaloom/cli/init_cmd.py::_resolve_cursor_api_key_input` + `src/popolaloom/credentials.py::CredentialResolver` + [`Credentials 与安全存储`](#credentials-与安全存储v092)

## Self-hosted worker handoff（v0.9.1+）

`popola cloud worker` 是 Cursor `agent worker` CLI 的薄包装。`debug` 做预检，`start` 启动或复用当前 workspace 的 worker，`status` 读取本机 management server，`handoff` 输出可复制到 Cursor Cloud Agents UI 的 prompt + URL，`dispatch` 则通过 `popolad` 创建一个 popola 可追踪的 `cursor-cloud` 任务。

```bash
popola cloud worker debug --worker-dir "$(pwd)"
popola cloud worker start --worker-dir "$(pwd)"
popola cloud worker status --management-addr 127.0.0.1:39231 --json
popola cloud worker handoff --worker-dir "$(pwd)" --prompt "Run the migration smoke"
```

默认未传 `--name` 时，PopolaLoom 会按 workspace 生成稳定 worker 名，并复用已经存在的同目录 worker；只有明确传 `--allow-duplicate` 才会启动第二个。

## Hands-off envelope

每次 dispatch 都会把 payload 写成 `.local/.agent/handoff/<handoff_id>.md`：

```yaml
---
schema_version: '1'
handoff_id: cursor-fix-bug-in-foo-py-3a7f9c1d
target_cli: cursor
---
fix the bug in foo.py
```

这个信封是 dispatch payload 的单一事实来源：长 prompt 不再受 argv 限制影响；每次派发都有可审计的 Markdown 回执；`popola dispatch --replay <handoff_id>` 可以确定性重放；`POPOLA_HANDOFF_FILE` 和 `POPOLA_HANDOFF_ID` 会注入 agent 子进程环境。

## 配置

| 环境变量 | 作用 | 默认值 |
|---|---|---|
| `POPOLA_HOME` | socket、event log、sqlite、pid 根目录 | `~/.popola/` |
| `POPOLA_USE_GRAPH` | 启用 LangGraph 子图和 HITL | `1` |
| `CODEX_HOME` | Codex Skill 目录 | `~/.codex/` |
| `CURSOR_API_KEY` | Cursor Cloud Agents API key env fallback | 未设置 |
| `LARK_HITL_TARGET_OPEN_ID` | HITL 与任务卡片接收人 | 未设置则 Lark 静默 |
| `LARK_NOTIFY_ON_COMPLETED` | 完成任务通知 | `1` |
| `LARK_NOTIFY_ON_FAILED` | 失败任务通知 | `1` |
| `LARK_NOTIFY_ON_CANCELED` | 取消任务通知 | `1` |

`popola init` 不会修改你的 shell rc；环境变量由操作者显式管理。

## 故障排查

| 现象 | 处理 |
|---|---|
| `popola: command not found` | `export PATH="$HOME/.local/bin:$PATH"` |
| socket 绑定失败 | 先 `popola popolad stop`，必要时清理陈旧 socket |
| Skill 显示 `DRIFT` | `popola skill upgrade --target=all` |
| IDE 没加载 Skill | 重启 IDE 或新开会话 |
| `popola list` 为空 | 用 `popola list --all` 和 `popola status <id>` 查看失败原因 |
| keyring 不可用 | 新安装用 `./install.sh install --with-credentials`；容器里改用 `CURSOR_API_KEY` |
| Lark 为 WARN / OFF | 安装 `lark-cli` 或设置 `LARK_HITL_TARGET_OPEN_ID`；不使用 Lark 时可忽略 |

## 架构深挖

PopolaLoom 的四个内部子系统是：

1. RPC server：FastAPI over UDS，承接 CLI / MCP 请求。
2. Task pool：vendored ArkTower SQLite repository，保存跨重启状态。
3. Subprocess supervisor：启动 agent CLI 子进程，捕获输出并写事件。
4. HITL bridge：Lark / IDE / CLI / MCP / Web 渲染器接收 `task.elicited` 并同步回答。

更多示例输出和演示路径见 [`DEMO.md`](DEMO.md)。完整设计哲学见 [`design-ideas.md`](design-ideas.md)。
