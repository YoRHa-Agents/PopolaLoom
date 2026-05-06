---
layout: default
title: PopolaLoom
description: Meta-orchestrator over local agent CLIs (Cursor / Claude / Codex / Copilot)
---

# PopolaLoom

> Meta-orchestrator over local agent CLIs. Per-task isolation, persistent process bus, HITL via 5 channels — all on a single `popolad` UDS daemon.

[GitHub](https://github.com/YoRHa-Agents/PopolaLoom) · [Quickstart](QUICKSTART.html) · [User Guide](USER_GUIDE.html) · [Demo](DEMO.html) · [Release Notes](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/RELEASE_NOTES.md) · [Changelog](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/CHANGELOG.md)

## What is PopolaLoom?

PopolaLoom is a local-first **meta-orchestrator** that sits on top of every agent CLI on your machine — Cursor, Claude Code, Codex, Kimi, GitHub Copilot — and gives them a single dispatch surface, a persistent task bus, and a unified HITL channel. Think of it as the "loom" (织机) that weaves N agent CLIs into one coherent run-graph: each per-task strand is its own subprocess managed by the `popolad` UDS daemon.

It is the multi-task / multi-CLI sibling of [DevolaFlow](https://github.com/YoRHa-Agents/DevolaFlow) (the per-task quality framework) and ships **vendored** with the relevant subset of [ArkTower](https://github.com/YoRHa-Agents/ArkTower) (the task pool / SQLite persistence / EventBus). On a fresh machine, `pip install popolaloom` gives you `popola dispatch` + `popola attach` + `popola doctor` with zero sibling clones.

## 5-minute install

```bash
pip install popolaloom
popola init                          # auto-detect Cursor / Claude / Codex / Copilot
popola popolad start                 # boot the UDS daemon
popola dispatch "echo hello popola" --cli=cursor
popola doctor                        # 4-subsystem health check
```

→ Full guide: [Quickstart](QUICKSTART.html)

## Key features

- **Single dispatch surface** — `popola dispatch "..." --cli=cursor|claude|codex|kimi|copilot` instead of N command shapes.
- **Cross-terminal task survival** — `popolad` UDS daemon with persistent SQLite task pool; tasks outlive shell close, SSH disconnect, even reboots.
- **Vendored ArkTower** — task pool / EventBus / 4 SQL migrations bundled under `popolaloom._vendored.arktower`; no sibling repo required.
- **HITL across 5 channels** — Lark / IDE / CLI / MCP / Web; first responder wins via atomic cross-channel sync.
- **Skill-based auto-discovery** — `popola init` writes `SKILL.md` into Cursor / Claude / Codex / Copilot; host agents auto-load it.
- **MCP-native** — 9 dispatch / inspect / HITL verbs over stdio for any MCP-aware IDE.
- **8-dim self-eval baseline** — `popola eval run` produces a PopolaLoom-nines composite score with per-dimension evidence pipelines.
- **Idempotent installer** — re-running `popola init` prints `SKIP <path> (already installed)` instead of clobbering operator edits.

## Install via your AI agent

Just say `install popola` to any host agent (Cursor, Claude Code, Codex, GitHub Copilot) — the `install-popola` Skill (v0.7.0+) handles the install for you. The Skill walks the agent through `pip install popolaloom` → `popola init <ide> --global` → `popola popolad start` → `popola doctor`, mirroring the conventional `/install-devola-flow` slash-command shape.

## Getting started

| Doc | What it covers |
|---|---|
| [Quickstart](QUICKSTART.html) | 5-minute onboarding (install → first task) |
| [User Guide](USER_GUIDE.html) | Full CLI + MCP + HITL reference |
| [Demo](DEMO.html) | Walkthroughs + example outputs + self-evolution journey |

## Latest release: v0.7.0

PopolaLoom v0.7.0 closes the four user-feedback items from v0.6.1: (1) `.local/` is now gitignored as a strictly local-only working surface (on-disk files preserved); (2) ten per-version `release-notes-v*.md` files consolidate into a single floating `RELEASE_NOTES.md` plus the `CHANGELOG.md` historical archive; (3) comprehensive docs refresh (`README.md` + `docs/QUICKSTART.md` + `docs/USER_GUIDE.md` + this Jekyll site + `docs/DEMO.md` v0.7.0 era refresh); (4) NEW `install-popola` Skill at `src/popolaloom/skills/install-popola/SKILL.md` mirroring the `/install-devola-flow` workflow.

→ Full notes: [RELEASE_NOTES.md](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/RELEASE_NOTES.md) · Historical archive: [CHANGELOG.md](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/CHANGELOG.md)

## License

MIT — see [LICENSE](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/LICENSE) (project repo).
