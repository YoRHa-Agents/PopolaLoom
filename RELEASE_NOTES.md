> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.7.2 — dispatch_with_envelope (E3) + adapter flag injection (C5) + handoff CLI

> Released: 2026-05-06
> Theme: wires the v0.7.1 handoff foundation into the actual dispatch path. After this release every `popola dispatch` (and every internal `dispatch_task` call from rpc.py / cli/main.py) writes a Markdown envelope file to `.local/.agent/handoff/<id>.md` and injects `POPOLA_HANDOFF_FILE` / `POPOLA_HANDOFF_ID` into the spawned sub-CLI's environment. v0.7.3 will add `popola dispatch --replay` + HITL feedback envelope + old-RelayHandoffEnvelope bridge.

## Summary

PopolaLoom v0.7.2 lands the **internal-unification** half of the v0.8.0 hands-off envelope feature:

1. **`Popolad.dispatch_with_envelope` is the canonical dispatch entry** (E3 internal unification) — `dispatch_task(cli, prompt, ...)` is now a thin wrapper that builds a `HandoffEnvelope` from kwargs and delegates. All dispatch goes through one path.

2. **C5 双通道 (env primary + flag forward-compat)** — every spawned sub-CLI now sees `POPOLA_HANDOFF_FILE=<abs path>` + `POPOLA_HANDOFF_ID=<slug-hash>` in its env. The `--popola-handoff-file <path>` argv flag is opt-in (`extra["popola_handoff_flag"]=True`) so vanilla cursor-agent / claude / codex don't break on unknown-flag errors; the flag stays as a forward-compat hook for sub-CLIs that gain native support.

3. **`popola handoff` CLI** — three new filesystem-only subcommands (`list` / `show` / `archive`) for inspecting + archiving on-disk envelopes without a running daemon.

4. **Read-side library** (`popolaloom.handoff.loader`) — `list_active_envelopes` / `load_envelope` / `resolve_envelope_path` for programmatic access to the envelope store.

The release ships **without** breaking changes: every existing `dispatch_task` caller (rpc.py / cli/main.py / 1494+ tests) keeps its signature; the only behavioural delta is that each dispatch now drops a small Markdown file in `.local/.agent/handoff/` (gitignored).

## What's NEW · `Popolad.dispatch_with_envelope`

```python
from datetime import UTC, datetime

from popolaloom.daemon import Popolad
from popolaloom.handoff import HandoffEnvelope, generate_handoff_id

popolad = Popolad(events_dir=...)  # or get the rpc-bound singleton

env = HandoffEnvelope(
    handoff_id=generate_handoff_id("cursor", "fix bug in foo.py"),
    created_at=datetime.now(UTC),
    target_cli="cursor",
    prompt="fix bug in foo.py — NoneType around line 42",
    reason="user reported during code review",          # NEW: only available via envelope path
    tags=["v0.7.x", "bug-fix"],                          # NEW
)
task_id = popolad.dispatch_with_envelope(env)
```

The kwargs surface still works (and now goes through the same internal path):

```python
task_id = popolad.dispatch_task(
    cli="cursor", prompt="fix bug", extra={"output_format": "stream-json"}
)
# Builds an envelope internally, writes it, and dispatches via dispatch_with_envelope.
```

## Sub-CLI sees the envelope via env vars

Every spawned sub-CLI (cursor-agent, claude, codex, ...) now finds:

```bash
$ env | grep POPOLA_HANDOFF
POPOLA_HANDOFF_FILE=/home/user/proj/.local/.agent/handoff/cursor-fix-bug-foo-py-3a7f9c1d.md
POPOLA_HANDOFF_ID=cursor-fix-bug-foo-py-3a7f9c1d
```

The agent (LLM running inside the sub-CLI) can `cat $POPOLA_HANDOFF_FILE` to inspect the original dispatch — including audit-only fields (`reason`, `tags`) that don't fit into a single argv prompt.

## C5 flag (opt-in forward-compat)

```bash
popola dispatch "..." --cli=cursor --cli-flag popola_handoff_flag=true
# Resulting argv now includes: ... --popola-handoff-file /path/to/<id>.md
```

This is **opt-in** because vanilla cursor-agent / claude / codex don't recognise the flag yet — auto-injection would break their argv parsing. The env channel above is always live; the flag stays as a hook for future native support.

## What's NEW · `popola handoff` CLI

```bash
# List active envelopes (newest first)
popola handoff list

# Active handoff envelopes
# ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━...
# ┃ handoff_id                                    ┃  size ┃ mtime               ┃
# ┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━...
# │ cursor-fix-bug-foo-py-3a7f9c1d                │ 412 B │ 2026-05-06 14:30:00 │
# └───────────────────────────────────────────────┴───────┴─────────────────────┴──...

# Show raw Markdown (cat-friendly, design Q1=A4)
popola handoff show cursor-fix-bug-foo-py-3a7f9c1d

# Or as JSON for piping into jq
popola handoff show cursor-fix-bug-foo-py-3a7f9c1d --json | jq .prompt

# Archive a finished task's envelope (D4 audit snapshot)
popola handoff archive cursor-fix-bug-foo-py-3a7f9c1d cursor-23e74ec18917
# → /repo/.local/.agent/archive/cursor-23e74ec18917/cursor-fix-bug-foo-py-3a7f9c1d.md
```

All three commands are **filesystem-only** — no daemon required, safe to run during incidents when popolad might be down.

## Read-side library

```python
from popolaloom.handoff import (
    list_active_envelopes,
    load_envelope,
    resolve_envelope_path,
)

# Enumerate
for s in list_active_envelopes():
    print(s.handoff_id, s.path, s.size_bytes, s.mtime)

# Load + parse a specific envelope
env = load_envelope("cursor-fix-bug-foo-py-3a7f9c1d")
print(env.prompt, env.reason, env.tags)

# Just the path (no I/O)
p = resolve_envelope_path("cursor-fix-bug-foo-py-3a7f9c1d")
```

`$POPOLA_HANDOFF_DIR` env is honoured by all three (matches writer / archive).

## Files changed (v0.7.2)

| 改动 | 文件 |
|---|---|
| Server E3 + C5 | `src/popolaloom/daemon/server.py` (+178 lines: dispatch_with_envelope + _resolve_handoff_path + _call_adapter post-processing) |
| Loader (NEW) | `src/popolaloom/handoff/loader.py` (HandoffSummary + list_active_envelopes + resolve_envelope_path + load_envelope) |
| Module surface | `src/popolaloom/handoff/__init__.py` (+5 new exports) |
| CLI (NEW) | `src/popolaloom/cli/handoff_cmd.py` (popola handoff list / show / archive) |
| CLI registration | `src/popolaloom/cli/main.py` (add_typer for handoff_app) |
| Test isolation | `tests/conftest.py` (session-scoped autouse $POPOLA_HANDOFF_DIR fixture) |
| Tests (NEW) | `tests/daemon/test_dispatch_with_envelope.py` (16 tests), `tests/handoff/test_loader.py` (16 tests), `tests/cli/test_handoff_cmd.py` (11 tests) |
| Smoke test | `tests/test_smoke.py` (version assertion 0.7.0 → 0.7.2) |
| Bump | `pyproject.toml`, `src/popolaloom/__init__.py`, `src/popolaloom/skills/popola-loom/SKILL.md`, `src/popolaloom/skills/popola-loom/.popola-loom-version`, `src/popolaloom/skills/install-popola/SKILL.md`, `CHANGELOG.md`, `RELEASE_NOTES.md` |

## Status

| Capability | Status |
|---|---|
| ALL v0.7.1 capabilities | unchanged ✓ |
| `Popolad.dispatch_with_envelope` (E3) | new ✓ |
| `dispatch_task` → `dispatch_with_envelope` thin wrapper | new ✓ (signature preserved) |
| C5 env channel (POPOLA_HANDOFF_FILE / POPOLA_HANDOFF_ID) | new ✓ |
| C5 flag channel (`--popola-handoff-file`) | new ✓ (opt-in via `popola_handoff_flag`) |
| `popola handoff list / show / archive` CLI | new ✓ |
| `popolaloom.handoff.loader` module | new ✓ |
| `popola dispatch --replay` | deferred → v0.7.3 |
| HITL feedback envelope | deferred → v0.7.3 |
| Old `RelayHandoffEnvelope` bridge to new `HandoffEnvelope` | deferred → v0.7.3 |
| Final 0.8.0 minor bump | deferred → v0.8.0 |
| 1551 default-lane tests / 94.42% coverage | ✓ |

## Self-eval (v0.7.2)

- `pytest -m "not slow and not nightly and not real_cli and not real_lark" --cov=src/popolaloom --cov-fail-under=94`：1551 passed, 18 skipped, 82 deselected, 0 failed
- `ruff check src/popolaloom tests/`：All checks passed
- `popolaloom.handoff` 模块覆盖率：100% (line + branch)
- `Popolad.dispatch_with_envelope` 覆盖：100%
- 累计 commits 在 `fix/v0.7.1-cancel-orphan-and-rehydrate-spawn-aborted` 分支：7（5 from v0.7.1 + 1 v0.7.2 feat + 1 v0.7.2 release）

## Upgrade notes

- **No breaking changes**：所有 `dispatch_task` caller 零感知迁移
- 升级仅需 `pip install -U popolaloom`（local clone：`pip install -e ".[dev]"`）
- 每次 dispatch 现在会向 `.local/.agent/handoff/` 写一个小的 Markdown 文件（已 gitignored）。如需自定义位置，设 `POPOLA_HANDOFF_DIR` 环境变量
- v0.7.3 将提供 `popola dispatch --replay <handoff_id>` 实现"重放上次派发"，以及 HITL feedback 走同一 envelope 体系

## Next steps

1. v0.7.3 (next release): `popola dispatch --replay` + HITL feedback envelope (Q7=yes) + 老 `RelayHandoffEnvelope` 桥接到新 `HandoffEnvelope` + 文档 (README / QUICKSTART / USER_GUIDE / DEMO 全面刷新)
2. v0.8.0 (final minor): 整体回归 + PR 提交（不直 push main，按 Protected Branch Workflow）
