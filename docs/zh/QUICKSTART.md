---
layout: default
title: 快速开始
description: PopolaLoom 5 分钟上手：安装、注册 Skill、派发第一个任务、健康检查。
lang: zh
translation_url: /QUICKSTART.html
---

# PopolaLoom — 5 分钟快速开始

<!-- updated: 2026-05-09 -->

> 从安装到看到第一个 `popola list` 任务，只需要一条本地 daemon 线和一个 agent CLI。完整参考见 [`USER_GUIDE.md`](USER_GUIDE.md)。

## 前置条件

- Python 3.11 或 3.12
- `pip`
- 可选：Cursor、Claude Code、Codex CLI 或 GitHub Copilot CLI。没有 IDE 时，PopolaLoom 也可以只作为 headless daemon 使用。
- 可选：`lark-cli`，用于 Lark HITL 和任务通知。

## Step 1 — 安装

```bash
# 当前 v0.9.1 release。PyPI promotion 仍在后续 v0.9.x patch 中，
# 需要 v0.9.x surface 时请使用 GitHub tag。
pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.1
# 或在仓库根目录使用统一安装脚本：
./install.sh install --from=git

# 或从源码安装开发版
git clone https://github.com/YoRHa-Agents/PopolaLoom.git
cd PopolaLoom
pip install -e ".[dev]"

# 验证
python -c "import popolaloom; print(popolaloom.__version__)"   # -> 0.9.1
which popola
popola version                                                 # -> "popolaloom 0.9.1"
```

如果你明确只需要 PyPI 上最新的已发布稳定线，`pip install popolaloom` 仍可使用；但在 `BL-v0.9.x-PyPI` promotion patch 落地前，它当前解析到上一条 v0.8.x 线。

如果安装后提示 `popola: command not found`，通常是 shell 没有包含 `~/.local/bin`：

```bash
export PATH="$HOME/.local/bin:$PATH"
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

## Step 2 — 注册 Skill

```bash
popola init

# 也可以显式指定 IDE
popola init cursor --global
popola init claude --global
popola init codex
popola init copilot
popola init local

# 查看和预演
popola init --list
popola init --interactive
popola init cursor --project --dry-run
```

`popola init` 是幂等的：已经安装过的目标会显示 `SKIP`，不会覆盖用户修改。升级 wheel 后需要刷新 Skill 时，用 `popola skill upgrade --target=<ide>`。

## Step 3 — 启动 daemon

```bash
popola popolad start
# popolad started, PID=12345
# socket: ~/.popola/popolad.sock
# log:    ~/.popola/log/popolad.log

popola probe
popola popolad status
```

daemon 使用 `start_new_session=True` 管理子进程，所以关闭终端或 SSH 断线不会让正在跑的 agent task 消失。需要停止时运行 `popola popolad stop`。

## Step 4 — 派发第一个任务

```bash
popola dispatch "echo hello from popola" --cli=cursor
# -> cursor-23e74ec18917

popola list
popola list --all
popola status cursor-23e74ec18917
```

`task_id` 会同步返回；真正的 agent 子进程由 daemon 在后台托管。

## Step 5 — 订阅输出

```bash
popola attach cursor-23e74ec18917 --follow
# process.stdout / process.stderr / state.* / task.completed
```

`Ctrl-C` 只会退出 attach，不会杀掉任务。一次性读取已有事件时用 `popola attach <id> --no-follow`。

## Step 6 — 健康检查

```bash
popola doctor
popola doctor --strict
popola doctor --json
```

`popola doctor` 检查 4 个子系统：Skill、daemon、Lark 和 vendored ArkTower。默认 WARN / MISS 不会让命令失败；`--strict` 用于 CI。

## 自动 smoke

```bash
bash examples/quickstart.sh
# [quickstart] all 6 steps PASS — popolaloom v0.9.1 ready
```

脚本默认使用临时 `$POPOLA_HOME`，不会污染真实的 `~/.popola`。

## 下一步

- 完整 CLI 与 MCP 参考：[`USER_GUIDE.md`](USER_GUIDE.md)
- 产品演示与设计说明：[`DEMO.md`](DEMO.md)
- 最新发布说明：[`RELEASE_NOTES.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/RELEASE_NOTES.md)
- 历史变更：[`CHANGELOG.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/CHANGELOG.md)
- 纯 Cloud Agent 启动：配置 Cursor API key（env var 或 keyring，见下条）后运行 `./cloud-quickstart.sh`。
- 安全凭据存储（v0.9.2+）：`pip install 'popolaloom[credentials]'` 后用 `popola auth cursor set` 把 API key 存到操作系统 keyring，从此不需要每次 shell 都 `export`。`popola auth cursor status` 在不泄露原值的情况下查看解析状态。详见 [`USER_GUIDE.md#credentials--secure-storage-v092`](../USER_GUIDE.md#credentials--secure-storage-v092)。
- self-hosted worker handoff：当你要把本机注册到 Cursor Cloud Agents UI 时，参考 [`USER_GUIDE.md#self-hosted-worker-handoff-popola-cloud-worker-v091`](../USER_GUIDE.md#self-hosted-worker-handoff-popola-cloud-worker-v091)；如果需要 popola 追踪的 task id，请继续使用 `--cli=cursor-cloud`。
