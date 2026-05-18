---
layout: default
title: 用户指南
description: popola CLI、MCP、HITL、Lark 和配置项的中文参考。
lang: zh
translation_url: /USER_GUIDE.html
---

# PopolaLoom — 用户指南 (v1.0.0-pre.1)

<!-- updated: 2026-05-11 -->

> PopolaLoom 自 v0.9.0 起进入 GA 稳定边界；当前公开文档面向 v1.0.0-pre.1。首次使用请先看 [`QUICKSTART.md`](QUICKSTART.md)，需要演示路径和示例输出请看 [`DEMO.md`](DEMO.md)。需要纯 Cloud Agent 启动时，先配置 Cursor API key，再运行仓库根目录的 `cloud-quickstart.sh`。

<details class="toc" open>
<summary>目录</summary>

- [心智模型](#心智模型)
- [CLI 速查](#cli-速查)
- [IDE 与 Skill](#ide-与-skill)
- [MCP 集成](#mcp-集成)
- [HITL 工作流](#hitl-工作流)
- [Credentials 与安全存储（v0.9.2+）](#credentials-与安全存储v092)
- [`popola init` 交互式 intake（v0.9.5+）](#popola-init-交互式-intakev095)
- [Self-hosted worker handoff（v0.9.1+）](#self-hosted-worker-handoffv091)
- [云端派发（v1.0.0-pre.1）](#云端派发v100-pre1)
- [Hands-off envelope](#hands-off-envelope)
- [用户偏好（v0.9.10+）](#用户偏好v0910)
- [配置](#配置)
- [故障排查](#故障排查)
- [架构深挖](#架构深挖)

</details>

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

## Self-hosted worker handoff（v0.9.1+；**v1.6.0 单路径**）

<!-- updated: 2026-05-18 -->

`popola cloud worker` 是 Cursor `agent worker` CLI 的薄包装。`debug` 做预检，`start` 启动或复用当前 workspace 的 worker（v1.6.0 起 **仅 My Machines 模式**；`--pool` / `--pool-name` 标志已移除），`status` 读取本机 management server，`handoff` 输出可复制到 Cursor Cloud Agents UI 的 prompt + URL，`dispatch` 则通过 `popolad` 创建一个 popola 可追踪的 Path-B JWT 直连任务。

> **v1.6.0 单路径 self-hosted dispatch**（关闭 [`feedback_for_v1.5.2.md`](../../.local/feedbacks/feedback_for_v1.5.2.md) 的 6 项强约束）：`popola dispatch ... --cloud-target=self-hosted` 走且**只走** Path-B JWT 直连。变更摘要：
>
> | v1.5.x | v1.6.0 |
> |---|---|
> | `popola cloud worker start --pool` | Click `UsageError`（退 2）—— 改用 `agent worker start --pool` 直接走上游 CLI |
> | `popola dispatch --cloud-target=self-hosted --auth-mode=rest` | 退 2,提示改用 `--auth-mode=session-jwt`（隐式默认） |
> | `popola dispatch --cloud-target=self-hosted --allow-fallback` | no-op + 中英双语 WARN（绝不回退到本地 CLI） |
> | （派发后不打印 URL） | stdout 多打印一行 `view: https://cursor.com/agents/<bcId>` |
>
> Managed cloud（`--cloud-target=cursor-managed`）和本地 CLI 派发不变。self-hosted 新形态需要一次性 `cursor login`（生成 `~/.config/cursor/auth.json`）—— 不再需要 `CURSOR_API_KEY`。

```bash
cursor login                                                # 一次性 JWT bootstrap
popola cloud worker debug --worker-dir "$(pwd)"             # 预检
popola cloud worker start --worker-dir "$(pwd)"             # My Machines 模式
popola cloud worker status --management-addr 127.0.0.1:39231 --json
popola cloud worker handoff --worker-dir "$(pwd)" --prompt "Run the migration smoke"

# v1.6.0 单路径 self-hosted 派发:
popola dispatch "ship the release notes" \
  --cloud-target=self-hosted --worker-name=popolaloom-myrepo-deadbeef \
  --cli-flag repo_url=https://github.com/acme/myrepo
# → self-hosted-feedf00d
# → view: https://cursor.com/agents/bc-...
```

默认未传 `--name` 时，PopolaLoom 会按 workspace 生成稳定 worker 名，并复用已经存在的同目录 worker；只有明确传 `--allow-duplicate` 才会启动第二个。需要 Self-Hosted Pool worker 的运维人员请直接调用 `agent worker start --pool --pool-name <X>`（需 service-account `CURSOR_API_KEY`，详见 [Cursor service accounts 文档](https://cursor.com/docs/account/enterprise/service-accounts)）—— popola v1.6.0 不再包装该路径。

## 云端派发（v1.0.0-pre.1）

<!-- updated: 2026-05-11 -->

> **v1.0.0-pre.1 的核心变化。** 本节把 v0.9.x 的 [Cloud Agent dispatch](#cloud-agent-dispatchv085) 和 [Self-hosted worker handoff](#self-hosted-worker-handoffv091) 两个流合并成同一套心智模型：**两条云端路径，一个 CLI 入口**。[Cursor Cloud Agents Dashboard](https://cursor.com/agents) 是判定"云端派发"的唯一事实来源 —— 如果一次派发的运行没有出现在那里，那它**不算**云端派发（用户原话见 [`feedback_for_v0.10.0.md`](../../.local/feedbacks/feedback_for_v0.10.0.md) L5："在 Cursor 的语境下，是需要将任务在云端的 Cursor Agent 网页界面能够看到这个任务，才叫做云端派发"）。完整设计依据见 [`DECISIONS.md` Q-1..Q-12](../../.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md)。

### 两条云端路径

PopolaLoom v1.0.0-pre.1 只承认两种云端派发形态。两者的派发结果都会出现在 [`cursor.com/agents`](https://cursor.com/agents)，区别在于**实际执行任务的环境在哪里**。

| 路径 | 任务在哪里执行 | 鉴权要求 | 前置条件 | 适用场景 |
|---|---|---|---|---|
| `cursor-managed` | Cursor 托管的云端 VM（执行环境不归你管） | Cursor API key（环境变量或 keyring 任一） | **Cursor GitHub App** 已安装到 `github.com/<owner>/<repo>`（即 `repos[].url` 的 host） | 纯 REST 流；不需要本地 worker；适合任意已授权的 GitHub 仓库 |
| `self-hosted` | 你自己用 `popola cloud worker start --name X` 启动的 worker（在 [`cursor.com/agents`](https://cursor.com/agents) 的 `workerId` 维度可见，符合 Stage-1 调研结果） | Cursor API key（personal 或 service-account 任一） | **已注册的同名 worker**（通过 `GET /v0/private-workers` 校验） | 需要完全控制执行环境（私网、机内依赖、自定义工具链）时使用 |

两条路径共享同一个 CLI 动词（`popola dispatch`）和同一套 daemon 链路；路由决策由新增的 `--cloud-target` flag 编码。

### Init 阶段：一次性记录默认目标

当 `default_runtime` 是 `cloud` 或 `ask-each-time` 时，`popola init --interactive` 会在询问完 `default_runtime` 之后立刻追问 `default_cloud_target`：

```bash
popola init --interactive
# ...
# Default runtime? [local / cloud / ask-each-time]: cloud
# Default cloud target? [self-hosted / cursor-managed / ask-each-time]: self-hosted
# （当 default_runtime=local 时此提示自动跳过）
```

选定的值写入 `popolad.toml` 的 `[user_preferences].default_cloud_target`。这是后续每次 `popola dispatch` 的**默认值**；按任务粒度的 override（见下一节）优先级更高。非交互场景可以用 `popola init --set default_cloud_target=self-hosted`（也支持其它任意 `--set` 写法）。

> 历史字段 `cloud_target_priority`（list 形态）保留一个 release 周期，读取时输出 deprecation `WARN`；解析器不再消费它。请把 `popolad.toml` 里的旧字段迁移到 `default_cloud_target`（单值）。

### 按任务粒度 override

```bash
# self-hosted：派发到指定 worker；--worker-name 必填。
popola dispatch --cloud-target=self-hosted --worker-name=my-team-worker \
  "Refactor the caching layer and add unit tests"

# cursor-managed：托管 VM；--worker-name 必须为空。
popola dispatch --cloud-target=cursor-managed \
  "Plan the database migration scaffolding"
```

当 `--cloud-target` 给定且 `--cli` 没指定时，`cli="cursor-cloud"` 会被自动设置（这样不必每次都同时传 `--cli=cursor-cloud --cloud-target=...`）。v0.9.x 的旧写法 `popola dispatch --cli=cursor-cloud --cli-flag worker_name=W` 仍然向后兼容 —— 该 flag 的值会流入同一份 extras dict，内部会被翻译成新的 `env: {type:"machine", name:"W"}` 请求体形态，并发出一次 `DeprecationWarning`。CLI 解析层强校验互斥关系：

| `--cloud-target` | `--worker-name` | 行为 |
|---|---|---|
| `self-hosted` | 非空 | 进入派发 |
| `self-hosted` | 空 | exit 2（参数校验错误："--worker-name required when --cloud-target=self-hosted"） |
| `cursor-managed` | 空 | 进入派发 |
| `cursor-managed` | 非空 | exit 2（参数校验错误："--worker-name not allowed when --cloud-target=cursor-managed"） |
| `ask-each-time` | 任意 | exit 2（仅可作为 `default_cloud_target` 默认值，不可作为单次任务参数） |

> 解析优先级是 **任务级 `--cloud-target` flag > `[user_preferences].default_cloud_target` > `"ask-each-time"`**（详见 [`DECISIONS.md` Q-6](../../.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md)）。CLI 进程在派发出栈之前会把上述链路收敛成一对最终 `(target, worker_name)`。

### no-fallback 契约 —— worker 不存在时的行为

当 `--cloud-target=self-hosted` **且** 指定的 worker **未在 Cursor 注册**（即 `GET /v0/private-workers` 返回的列表里没有 `name == --worker-name` 的行）时，`popola dispatch` 会以 **78** 退出，并打印一条双语 hint 指向真正的修复路径：

```text
error: self-hosted worker 'my-team-worker' is not registered with Cursor.

Reason: popola cloud worker dispatch with --cloud-target=self-hosted requires
a registered self-hosted worker (verified via GET /v0/private-workers per
DECISIONS Q-3). The named worker 'my-team-worker' was NOT found in the
inventory returned by Cursor.

Fix — start a worker for this workspace, then retry:
  popola cloud worker start --name my-team-worker --worker-dir <repo-root>
  # ...wait for the worker to register, then re-run:
  popola cloud worker dispatch "<prompt>" --worker-dir <repo-root> --repo-url <repo-url>

Per the v0.10.0 no-fallback contract (DECISIONS Q-7), popola will NOT silently
re-route this dispatch to a local cursor-agent subprocess — cloud dispatch and
local execution are semantically distinct.

错误：Worker 'my-team-worker' 不存在 — 该 self-hosted worker 未在 Cursor 注册。
原因：popola cloud worker dispatch 在 --cloud-target=self-hosted 模式下需要已注册的
self-hosted worker（通过 GET /v0/private-workers 校验）。

解决方案：先在仓库根目录启动同名 worker，再重试派发：
  popola cloud worker start --name my-team-worker --worker-dir <repo-root>
  # 等 worker 注册成功后：
  popola cloud worker dispatch "<prompt>" --worker-dir <repo-root> --repo-url <repo-url>

根据 v0.10.0 no-fallback 契约（DECISIONS Q-7），popola 不会静默回退到本地
cursor-agent 子进程 —— 云端派发与本地执行是语义不同的两件事。
```

契约本身（详见 [`DECISIONS.md` Q-7](../../.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md)，引述 [`feedback_for_v0.10.0.md`](../../.local/feedbacks/feedback_for_v0.10.0.md) L5+L11 的用户原话）：popola **绝不**把一次失败的云端派发静默改路由到本地 `cursor-agent` 子进程。云端派发和本地执行是语义不同的两件事 —— 本地子进程不会出现在 [`cursor.com/agents`](https://cursor.com/agents)，静默回退会破坏用户对"云端派发"的定义。Worker 不存在时唯一正解就是先把它启动起来；上面那段 hint 已经把命令给到具体形态。`[user_preferences].fallback_chain` 仍然只对 `default_runtime=local` 流生效；当解析后的云端目标是 `self-hosted` 时，它不会被消费。

如果同名 worker **已注册**但当前正忙（`isInUse=true`），pre-flight 只发一次软 `WARN`（"the run will queue until the worker is free"）然后放行 —— Cursor 网关会接受 POST 并把该 run 入队，所以这种情况下派发本身是允许的。

### `cursor-managed` + `github.com` 仓库的 GitHub-App 前置条件

当 `repos[0].url` 的 host 是 `github.com`，**且** 你选了 `--cloud-target=cursor-managed`（或者 self-hosted 但仓库 URL 是 github.com 域名）时，如果 Cursor GitHub App 没装到对应的仓库上，Cursor REST 网关会返回 `400 validation_error: "Failed to verify existence of branch '<X>' in repository <owner>/<name>"`（或者第二种文案变体 `Failed to determine repository default branch`），**无论那个分支真的存在与否**。v1.0.0-pre.1 把这件事提前到了真正发请求之前：

- **预检拒绝**（新增路径）：`cursor_cloud.create_agent` 在发 POST 前会先调一次 `GET /v1/repositories`。若返回 `{"items":[]}`（即 App 在你的所有仓库上都未授权），派发立刻抛 `GithubAppMissingError`，hint 指向 [`https://cursor.com/integrations/github`](https://cursor.com/integrations/github)，省下那次注定失败的 POST。
- **后置兜底**（v0.9.x 路径，仍保留作为安全网）：如果你显式跳过预检（`extras["skip_github_app_preflight"] = True`），或者 App 已装但具体仓库未在 allowlist 内，网关那边的 400 仍然会被 `_ERROR_CATALOG` 的 `integration_github_app_branch_not_found` 规则路由到同一个 `GithubAppMissingError`，输出同样的 hint。

授权 App 的入口在 [`https://cursor.com/integrations/github`](https://cursor.com/integrations/github)，按页面里的 org/repo allowlist 步骤勾选目标仓库即可。PopolaLoom 不会代你装这个 App（这是一次组织/仓库级权限授予，超出本工具范围）—— 详见 [`DECISIONS.md` Q-9](../../.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md)。预检只对 `github.com` host 生效；GitLab / Gitea / 私网 git provider 会跳过它（已记入 `BL-v1.0.0-pre.2-non-github-host-preflight`）。

### 端到端 smoke

```bash
# Tier-4 release-gate live smoke（需要 CURSOR_API_KEY）。
pytest tests/cloud/test_real_cursor_cloud_env_shape_v0_10_0.py -m real_cursor_cloud

# 真实派发到 self-hosted worker。
popola dispatch --cloud-target=self-hosted --worker-name=$WORKER --repo-url=$REPO --cli=cursor-cloud "<prompt>"

# no-fallback 契约抽检 —— 必须 exit 78。
popola dispatch --cloud-target=self-hosted --worker-name=ghost-worker "test prompt"
echo "exit_code=$?"
```

> 参见：`src/popolaloom/adapters/cursor_cloud.py`（env-shape 请求体构建）、`src/popolaloom/cli/cloud_worker_cmd.py::_enforce_self_hosted_worker_exists`（worker 存在性 pre-flight gate）、`src/popolaloom/cloud/preflight.py`（纯函数 helper）、`src/popolaloom/cli/main.py`（`--cloud-target` / `--worker-name` flag）、[`DECISIONS.md` Q-1..Q-12](../../.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md)。

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

## 用户偏好（v0.9.10+）

`[user_preferences]` 是实验性的操作者偏好 schema，用来把常用 dispatch 选择显式写下来，而不是藏在 shell alias 里。v1.0.0-pre.1 文档和 Skill workflow 会引用它；稳定边界仍标为 experimental，直到 v1.0.0 stable 再决定是否锁定。

```toml
[user_preferences]
default_cli = "cursor"
default_cwd = "~/src/current-project"
confirm_before_cloud = true
prefer_streaming = true
handoff_tags = ["daily-driver", "reviewable"]

[user_preferences.dispatch]
default_wait = false
timeout_seconds = 120
extra_cli_flags = { output_format = "stream-json" }
```

四个典型命令：

```bash
popola init --interactive
popola dispatch "summarize the repository state" --use-preferences
popola dispatch "review the migration plan" --profile daily-driver --json
popola doctor --json | jq '.user_preferences'
```

偏好文件不能放 secret。Cursor API key 仍然放 OS keyring、`CURSOR_API_KEY`，或 v0.9.9+ 的 0o600 fallback 文件；偏好只用于路由、交互默认值和可重复的 dispatch 旋钮。

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
