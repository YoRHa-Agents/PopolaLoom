---
layout: default
title: 已知限制
description: PopolaLoom 已记录限制、绕行方式和后续跟踪入口。
lang: zh
translation_url: /known-issues.html
---

# 已知限制

<!-- updated: 2026-05-19 -->

本页是英文 [`known-issues.md`](../known-issues.html) 的中文导航版，记录当前 v0.9.x 文档中最常见、且已有绕行方式的限制。完整细节以英文页为准。

## v1.6.1 — `agent worker` 退出时删除 `~/.config/cursor/auth.json`（上游行为）

<!-- updated: 2026-05-19 -->

**限制。** 当上游 Cursor CLI 的 `agent worker start` 子进程退出（SIGTERM、SIGKILL 或正常退出）时，会作为清理流程的一部分删除操作员的会话 JWT 文件 `~/.config/cursor/auth.json`。v1.6.0 Stage T live-probe 实测确认：`popola cloud worker stop`（或任何针对 worker 的进程 kill）之后，下一次派发会失败并报 `Authentication required for worker mode. Please run 'agent login', or provide an API key with --api-key or CURSOR_API_KEY.` 恢复方式只能重新跑 `agent login` 把 JWT 写回去。完整经验性 trace（命令、时间戳、暴露上游提示的 worker 日志片段）见 [`feedback_for_v1.6.0.md` L62-L80](../../.local/feedbacks/feedback_for_v1.6.0.md)。

**影响。** PopolaLoom **无法**阻止这个行为 —— `auth.json` 的生命周期归上游 Cursor CLI 所有。v1.6.1 在 `popola cloud worker start` 增加防御性预检（auth.json 缺失时退 1 并打印 `agent login` 提示），让操作员在 popola 边界就看到失败，而不是埋在 worker 子进程的 "Authentication required" 日志里。

**绕行方式。** 在 worker 重启之间重新跑一次 `agent login`。长期挂着的工作区推荐流程：`popola cloud worker stop` → `agent login` → `popola cloud worker start` —— 新预检会在第一时间挡下失败，避免一连串失败派发。`--allow-missing-auth` 是给 CI smoke 用的转义出口（CI 故意跳过 JWT 步骤时使用）。

**跟踪。** `CHANGELOG.md §Unreleased` 中的 `BL-v1.6.x-worker-shutdown-auth-deletion`。延后到上游 Cursor —— popola 在客户端层面无法修复。

## v1.6.0 — Cursor 服务端 `env=machine→pool` 静默降级（上游回归）

<!-- updated: 2026-05-18 -->

| 限制 | 绕行方式 |
|---|---|
| **Cursor 的 Connect-RPC `StartBackgroundComposerFromSnapshot` 把请求 body 里的 `env={"type":"machine","name":X}` 静默降级到 `env={"type":"pool"}`** —— 请求 200 + `bc_id` 都正常,但 `GET /v1/agents/<bcId>` 返回 `env={"type":"pool"}`、`name` 字段被删 | popola **无法**在客户端修复服务端路由。v1.6.0 在 popola 层已经满足 constraint #1（worker 进程只走 My Machines 模式,popola daemon 拒绝 self-hosted 派发携带 `extra.env.type='pool'`），但若你的账号挂多个 worker,Cursor 服务端的 pool fallback 可能让另一个 worker 抢到任务。**绕行方式**：每个仓库只挂 1 个 My-Machines worker —— 单个 worker 的情况下 pool fallback 没得选,自然落到目标 worker 上 |

跟踪：`BL-v1.6.x-cursor-env-machine-to-pool`，详见英文 [`known-issues.md` §v1.6.0 — Cursor server downgrades `env=machine→pool`](../known-issues.html#v160--cursor-server-downgrades-envmachinepool-upstream-regression) 和 [`CHANGELOG.md` §Unreleased](../../CHANGELOG.md)。

## v0.9.x 常见限制

| 限制 | 绕行方式 |
|---|---|
| `popola init --target=cloud-only` 只写 cloud scaffold，不安装 IDE Skill | 需要 IDE Skill 时显式运行 `popola init cursor`、`popola init all` 或 `popola skill install --target=<ide>` |
| Headless Linux 容器没有可用 keyring backend | 使用 `CURSOR_API_KEY` 环境变量，或 v0.9.9+ 的 `~/.popola/cursor_api_key.env` 0o600 fallback |
| Personal API key + self-hosted worker 仍不能稳定创建 Dashboard-visible popola-tracked task | 使用 `popola cloud worker handoff`，或改用 service-account / Self-Hosted Pool 路径 |
| Cloud SSE 可能过期 | `popola attach` 会显式 fallback 到 polling；脚本可保留 `--no-stream` 作为 legacy escape hatch |

## v1.0.0-pre.1 已记录限制

<!-- updated: 2026-05-11 -->

| 限制 | 绕行方式 |
|---|---|
| **Cursor 云端 auto-create-PR 偶发失败** — 即便 `autoCreatePR=true` 且 agent 已成功 `git push`，run 仍可能返回 `"No branch name available for PR creation"`，commit 不丢但 PR 没开 | 改用 `gh pr create --base main --head <branch>` 手动开 PR；commit / branch / remote 这三件事都已经正确，只有 Cursor 侧的 PR step 没成 |
| **Self-hosted worker 直接把 commit push 到 dispatch 时所在的分支** — agent 用的是 worker 绑定 checkout 的当前分支，会**覆盖**你正在累积的整合分支 | 派发前先 `git checkout -b <agent-task-branch>` 把 worker 隔离到全新分支；或直接用 `--cloud-target=cursor-managed`（Cursor cloud VM 会自动建 `cursor/<slug>-<id>` 分支）|

详见英文 [`known-issues.md` §v1.0.0-pre.1 — Cursor cloud auto-create-PR is occasionally flaky](../known-issues.html#v100-pre1--cursor-cloud-auto-create-pr-is-occasionally-flaky)
和 [§v1.0.0-pre.1 — Self-hosted worker pushes to the dispatch-time branch](../known-issues.html#v100-pre1--self-hosted-worker-pushes-to-the-dispatch-time-branch)；
源反馈：[`feedback_for_v1.0.0-pre.1.md` §2](../../.local/feedbacks/feedback_for_v1.0.0-pre.1.md)。

## 相关文档

- [用户指南](USER_GUIDE.html)
- [API 稳定边界](../API_STABILITY.html)
- [演示页](demo-page.html)
