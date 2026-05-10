---
layout: default
title: 已知限制
description: PopolaLoom 已记录限制、绕行方式和后续跟踪入口。
lang: zh
translation_url: /known-issues.html
---

# 已知限制

<!-- updated: 2026-05-10 -->

本页是英文 [`known-issues.md`](../known-issues.html) 的中文导航版，记录当前 v0.9.x 文档中最常见、且已有绕行方式的限制。完整细节以英文页为准。

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
