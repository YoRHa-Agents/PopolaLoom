---
layout: default
title: 演示
description: PopolaLoom 产品演示、示例输出、设计思想和实现路径。
lang: zh
translation_url: /DEMO.html
---

# PopolaLoom — 产品演示

> 这不是另一个 IDE，而是本机 agent CLI 之上的 sidecar 编排层：统一派发、跨终端存活、文件化 handoff、HITL 多通道广播。

## 演示目标

一次完整演示要证明 4 件事：

1. 用户只需要一个 `popola dispatch` 入口，就能把任务派发给 Cursor / Claude / Codex / Copilot。
2. 任务状态由 `popolad` 持久化，终端关闭后仍可 `attach`。
3. 每次派发都有 `.local/.agent/handoff/<id>.md` 审计文件，可以 replay。
4. 需要人工判断时，HITL 提示会广播到 Lark / IDE / CLI / MCP / Web，谁先回答谁恢复任务。

## 5 分钟路径

```bash
pip install popolaloom
popola init
popola popolad start
popola dispatch "echo hello from popola" --cli=cursor
popola list --all
popola doctor
```

预期输出：

```text
cursor-23e74ec18917
Summary: 4/4 subsystems checked. 0 FAIL.
```

## Hands-off envelope 演示

```bash
popola dispatch "fix the NoneType bug in foo.py" --cli=cursor
popola handoff list
popola handoff show cursor-fix-the-nonetype-bug-in-foo-py-3a7f9c1d
```

envelope 是 Markdown front matter + prompt body：

```yaml
---
schema_version: '1'
handoff_id: cursor-fix-the-nonetype-bug-in-foo-py-3a7f9c1d
target_cli: cursor
adapter_extra: {}
constraints: {}
---
fix the NoneType bug in foo.py
```

这个设计把长 prompt 从 argv 里移出来，降低 shell / kernel 限制风险；同时提供可审计、可搜索、可归档、可 replay 的任务回执。

## HITL 演示

当任务遇到需要用户判断的步骤时，LangGraph 节点调用 `interrupt()`：

```text
task.elicited -> Lark card / IDE chooser / CLI pending / MCP form / Web surface
```

用户可以在任意一个通道回答。第一个回答通过 `mark_answered` 原子落盘，并触发 `state.resumed`；迟到的通道会看到已回答结果，不会重复恢复任务。

## 设计思想

PopolaLoom 的实现遵循三个原则：

- **Sidecar ownership**：daemon 管进程生命周期和状态，agent CLI 保持原生行为。
- **File-backed contracts**：复杂 payload 写成文件，而不是塞进命令行字符串。
- **Human answer as state**：HITL 回答是任务状态的一部分，而不是散落在聊天记录里的临时消息。

## 实现路径

```text
popola CLI / MCP tool
  -> UDS RPC
  -> popolad daemon
  -> HandoffEnvelope written to .local/.agent/handoff/
  -> adapter builds cursor/claude/codex argv
  -> subprocess emits stdout/stderr/state events
  -> NDJSON event log + optional Lark card
  -> attach/status/list read the same durable state
```

## 下一步

- 首次安装：[`QUICKSTART.md`](QUICKSTART.md)
- 完整命令参考：[`USER_GUIDE.md`](USER_GUIDE.md)
- 最新 release：[`RELEASE_NOTES.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/RELEASE_NOTES.md)
