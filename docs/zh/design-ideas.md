---
layout: default
title: 核心设计思想
description: PopolaLoom 的 7 个架构选择：织机隐喻、边车 daemon、文件化 handoff、五通道 HITL、vendoring、Skill 自动发现和 GA 稳定边界。
lang: zh
translation_url: /design-ideas.html
---

# 核心设计思想

<!-- updated: 2026-05-10 -->

## The Loom Metaphor (织机)

PopolaLoom 叫 loom，不是因为名字好听，而是因为它不想成为一个只会转发请求的 router。Router 只选择目的地，然后消失；织机会把多根线拉住、对齐、记录交叉关系，最后织成一张布。Cursor、Claude、Codex、Kimi、Copilot、Lark、MCP、cloud worker 和人工审批都是线；任务池、事件日志和 handoff 信封是让这些线保持张力的经线。

这个隐喻解释了两个动词。面向用户的是 `dispatch`：操作者只想把一根线拉起来，让这个 prompt 现在去这个 CLI 上跑。面向项目的是 `weave`：真正有价值的工作通常不会停在第一个子进程。一个 cloud run 可能请求审批，可能 relay 到另一个 repo，也可能留下一个信封给另一个 CLI 继续读。织机负责让这些动作可见，而不是把它们压扁成聊天记录。

它也划出边界。PopolaLoom 不替代 agent、不替代原生命令、不替代各家产品自己的能力。它提供一根共享任务总线和足够清晰的结构，让人能看懂这块布是怎么织出来的。

> See: `src/popolaloom/daemon/server.py::Popolad.dispatch_task` + [`README.md#what-is-popolaloom`](../../README.md#what-is-popolaloom)

## Daemon-as-Sidecar (`popolad`)

`popolad` 是边车，因为任务生命周期不应该属于启动它的那个终端。库会跟着 import 它的进程一起消失；普通 CLI wrapper 在命令返回后就失去监督能力；纯云 SaaS 又会把本地 agent 凭据、文件系统上下文和信任边界搬出机器。`popolad` 留在本机，只绑定 `$POPOLA_HOME/popolad.sock` 这个 UDS，只负责协调层。

这个选择带来几个不变式。每个任务仍然是原生 agent CLI 的子进程，但通过 `start_new_session=True` 启动，并由 wait-thread 观察。vendored ArkTower 的 SQLite 任务池跨 daemon 重启保存状态。`$POPOLA_HOME/events/` 下的 NDJSON 事件日志让 `attach`、`status`、`list` 读同一个事实来源。终端关闭或 SSH 断开只是 attach 入口丢了，不代表任务丢了。

边车还让本地和云路径对称。本地任务从子进程收 stdout/stderr；cloud task 从 Cursor REST / SSE 收状态。两者最后都落到同一个 daemon 状态信封里。

> See: `src/popolaloom/daemon/supervisor.py` + [`docs/USER_GUIDE.md#架构深挖`](USER_GUIDE.md#架构深挖)

## File-Backed Handoff

PopolaLoom 的原则是：argv 字符串是临时的，文件才可审计。长 prompt 会遇到 shell quoting、内核 argv 限制和 adapter 解析差异；更重要的是，argv 很难在事后复查。Markdown 文件加 YAML front-matter 可以打开、搜索、归档、diff，也可以放进 PR 作为证据。

所以每次 dispatch 都会先写 handoff 信封，再构造 adapter argv。信封记录 schema version、target CLI、可选 parent task、cwd、adapter extras、constraints、reason、tags 和 prompt body。id 由内容派生：target CLI、prompt slug，再加规范化 payload 的 8 位 hash。同样内容得到同样 id，`popola dispatch --replay <handoff_id>` 因而是 replay-by-id，而不是凭记忆重打一次 prompt。

子进程会拿到 `POPOLA_HANDOFF_FILE` 和 `POPOLA_HANDOFF_ID` 环境变量。agent 可以读取原始 dispatch，而 PopolaLoom 不需要强迫 Cursor、Claude 或 Codex 接受新的 prompt 格式。织机把契约放进文件，每根线仍保持原生。

> See: `popolaloom.handoff.HandoffEnvelope` + [`docs/USER_GUIDE.md#hands-off-envelope`](USER_GUIDE.md#hands-off-envelope)

## 5-Channel HITL Fanout

HITL 的冗余是刻意设计，不是重复造轮子。人可能在 IDE 里、终端里、Lark 里、MCP 表单里，或者后续 Web 面板里。把任意一个通道当成唯一入口都会让任务变脆：agent 暂停了，人却没看到那一个入口，run 就挂住。PopolaLoom 把同一个请求同时广播到 Lark、IDE、CLI、MCP、Web，让人从注意力所在的位置回答。

冗余靠一个不变式收束：谁先回答谁赢。HITL store 保存 pending request，`mark_answered` 是跨通道原子门。一个通道提交答案后，LangGraph state writeback 发出 `state.resumed`；迟到回答只能看到 already answered 状态，不会让任务恢复两次。这让 fanout 是安全的，而不是吵闹的。

Lark 是旁路通道。它能在 IDE 忙碌或 cloud run 远端执行时把人拉回来，但它不是硬依赖。如果缺少 `lark-cli` 或没有设置 `LARK_HITL_TARGET_OPEN_ID`，PopolaLoom 会退回本地 NDJSON / CLI / MCP，并记录 skipped reason。Lark 缺席不是任务失败。

> See: `src/popolaloom/hitl/sync.py::mark_answered` + [`docs/USER_GUIDE.md#hitl-工作流`](USER_GUIDE.md#hitl-工作流)

## Vendoring Philosophy

PopolaLoom 把 ArkTower vendored 到 `popolaloom._vendored.arktower`，因为任务池是运行时基础设施，不是可有可无的集成。daemon 在可靠持久化和重启恢复之前，需要 task model、EventBus、SQLite repository、migration helper 和四个 SQL migration。采用这块表面时，ArkTower 还不是一个任何新机器都能稳定解析到的普通 package。

承诺很实际：新装 PopolaLoom 不应该要求旁边还有 sibling clone、私有 checkout 或运行时 `git clone`。只 vendor 最小 ArkTower 子集，可以让 `popola dispatch`、`popola list`、`popola doctor` 在离线、内网镜像或受限环境里仍然可用。vendored tree 带命名空间，导入路径显式，不会和用户自己的 ArkTower checkout 混在一起。

代价是 release 边界。ArkTower 一旦改到 PopolaLoom vendored 的表面，PopolaLoom 要切自己的 release，刷新副本，记录 upstream commit，并重跑 daemon persistence tests。Vendoring 不是静默漂移的借口，而是带审计记录的可移植性承诺。

> See: `src/popolaloom/_vendored/arktower/` + [`VENDORING.md`](../../VENDORING.md)

## Skill = Auto-Discovery Contract

Skill 不是更漂亮的 help text，而是 host agent 启动时发现 PopolaLoom 能力的契约。`popola init` 把 canonical `SKILL.md` 写进 Cursor、Claude、Codex、Copilot 的约定位置；这些 host 随后自动加载动词和操作规则，不需要用户每次把 CLI manual 粘进对话。

这很重要，因为 PopolaLoom 常常通过自然语言被调用。用户说“派发给 cursor 跑这个”或“attach 到我正在跑的 agents”，host 应该知道什么时候调用 CLI、什么时候追问 task id、什么时候跑 `popola doctor`。Skill 给 host 一个稳定词汇表，而不是 Typer help output 的转储。它也承载安装 / 更新生命周期：`install-popola` 负责新装，canonical `popola-loom` 负责日常编排。

版本保持 lockstep。release 任务会一起 bump package version 和 Skill front-matter；`popola doctor` 会在磁盘上的 Skill 版本和已安装包不一致时报告 drift。这样 auto-discovery 是可审计的，而不是魔法。

> See: `src/popolaloom/skills/popola-loom/SKILL.md` + [`docs/USER_GUIDE.md#ide-与-skill`](USER_GUIDE.md#ide-与-skill)

## GA Stability Boundary (v0.9.0+)

v0.9.0 划出了“可以写自动化依赖”的边界。稳定表面包括 CLI 动词名、flag 拼写、daemon RPC path、关键 `--json` schema、public Python symbols 和 `popolad.toml` section 名。下游脚本可以依赖 `popola list --json`、`popola status --json`、`POST /dispatch` 和凭据 resolver，而不用担心 patch release 改坏。

这不代表项目被冻结。可以增加字段，可以增加默认安全的新 flag，也可以增加 endpoint。实验表面会明确标注：`popola cloud runs`、部分 verbose cost 字段、SSE event sub-type 都可以在 v0.9.x 内继续演进，但需要清楚的 CHANGELOG。原则是操作者不需要猜：哪些是契约，哪些还是研究面。

这也是为什么文档把 local dispatch、Cursor Cloud REST、self-hosted worker handoff、Cloud HITL 和 relay 分开讲，而不是把它们藏在一个模糊的 cloud feature 里。稳定命名让团队把无聊部分脚本化；显式实验标签让维护者继续收紧织机。

> See: `docs/API_STABILITY.md#2-stable-surfaces-v09x-guaranteed` + [`docs/MIGRATION_v07_to_v09.md#v090--ga-deprecation-removals-pr-pending`](MIGRATION_v07_to_v09.md#v090--ga-deprecation-removals-pr-pending)
