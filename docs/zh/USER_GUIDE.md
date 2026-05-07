---
layout: default
title: 用户指南
description: popola CLI、MCP、HITL、Lark 和配置项的中文参考。
lang: zh
translation_url: /USER_GUIDE.html
---

# PopolaLoom — 用户指南 (v0.8.4)

> 这份指南解释 PopolaLoom 的工作模型、常用命令和关键设计。首次使用请先看 [`QUICKSTART.md`](QUICKSTART.md)，需要演示路径和示例输出请看 [`DEMO.md`](DEMO.md)。

## 心智模型

PopolaLoom 由三部分组成：

- `popolad` sidecar daemon：绑定 `$POPOLA_HOME/popolad.sock`，负责派发、查询、取消、探活和任务状态。
- `popola` CLI：所有用户命令都通过 UDS RPC 调 daemon。
- `popolaloom-mcp` stdio server：把同一组能力暴露给 Cursor / Claude 等支持 MCP 的 IDE。

每个任务都会启动一个独立的 agent CLI 子进程，例如 `cursor-agent`、`claude` 或 `codex`。daemon 负责捕获 stdout/stderr、写 NDJSON 事件日志、更新任务状态，并在终态发送可选的 Lark 通知。

核心结果是：你可以从任意终端运行 `popola dispatch "..." --cli=cursor`，关掉终端后再从另一个 shell 里 `popola attach <id> --follow`，任务仍然可见、可追踪、可取消。

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
| `popola init` | 注册 Skill 到本机 IDE | `popola init cursor --global` |
| `popola doctor` | Skill / daemon / Lark / ArkTower 健康检查 | `popola doctor --strict` |
| `popola eval run` | 8 维 PopolaLoom-nines 自评 | `popola eval run --output /tmp/nines.toml` |

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

`init` 是首次安装入口且幂等；`popola skill upgrade --target=all` 用于升级 wheel 后覆盖刷新 Skill，覆盖前会写备份。

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
| Web | 当前以静态 docs 入口展示，浏览器 dashboard 是后续 roadmap |

第一个回答通过 `hitl/sync.py:mark_answered` 原子落盘并恢复任务；迟到回答会看到已回答状态并退出。

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

这个文件是 dispatch payload 的单一事实来源：

- 长 prompt 不再受 argv 限制影响。
- 每次派发都有可审计的 Markdown 回执。
- `popola dispatch --replay <handoff_id>` 可以确定性重放。
- `POPOLA_HANDOFF_FILE` 和 `POPOLA_HANDOFF_ID` 会注入 agent 子进程环境。

## 配置

| 环境变量 | 作用 | 默认值 |
|---|---|---|
| `POPOLA_HOME` | socket、event log、sqlite、pid 根目录 | `~/.popola/` |
| `POPOLA_USE_GRAPH` | 启用 LangGraph 子图和 HITL | `1` |
| `CODEX_HOME` | Codex Skill 目录 | `~/.codex/` |
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
| Lark 为 WARN / OFF | 安装 `lark-cli` 或设置 `LARK_HITL_TARGET_OPEN_ID`；不使用 Lark 时可忽略 |

## 架构深挖

PopolaLoom 的四个内部子系统是：

1. RPC server：FastAPI over UDS，承接 CLI / MCP 请求。
2. Task pool：vendored ArkTower SQLite repository，保存跨重启状态。
3. Subprocess supervisor：启动 agent CLI 子进程，捕获输出并写事件。
4. HITL bridge：Lark / IDE / CLI / MCP / Web 渲染器接收 `task.elicited` 并同步回答。

更多示例输出和演示路径见 [`DEMO.md`](DEMO.md)。
