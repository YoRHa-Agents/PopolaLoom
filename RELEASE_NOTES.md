> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.7.3 — popola dispatch --replay + FeedbackEnvelope (Q7) + relay bridge + docs

> Released: 2026-05-06
> Theme: closes the v0.8.0 hands-off envelope feature substrate. After this release the entire dispatch ↔ feedback round-trip is file-addressable + replayable + bridged with the legacy v0.3.0 `RelayHandoffEnvelope`. Comprehensive docs (README + USER_GUIDE + SKILL.md). v0.8.0 (next) is a documentation-only minor bump that promotes the v0.7.x foundation to a stable surface.

## Summary

PopolaLoom v0.7.3 lands the **finishing slice** of the v0.8.0 hands-off envelope feature:

1. **`popola dispatch --replay <handoff_id>`** — re-issue a previously written dispatch from disk; slug-hash addressing means same content → same id, so replay is fully deterministic.

2. **`popolaloom.handoff.FeedbackEnvelope`** — Q7=yes; HITL feedback companion to the dispatch envelope, file-addressable like its sibling. v0.7.3 ships the writer + schema; live `popola feedback ...` CLI wiring waits for v0.7.4's `--persist` flag (avoiding daemon-side coordination risk).

3. **Legacy `RelayHandoffEnvelope` bridge** (`to_handoff_envelope`) — v0.3.0 relay primitives can now produce v0.8.0 envelopes for file-based audit while the underlying `relay()` itself stays unchanged (full backward compat).

4. **Docs refresh** — README gets a new "Hands-off envelope" section; USER_GUIDE gets a comprehensive section (envelope shape, channel injection C5 双通道, programmatic API, full module surface table); SKILL.md adds 4 new commands rows.

## What's NEW · `popola dispatch --replay <handoff_id>`

```bash
# First-time dispatch writes an envelope automatically (v0.7.2)
popola dispatch "fix bug in foo.py" --cli=cursor
# → cursor-23e74ec18917
# (envelope file: .local/.agent/handoff/cursor-fix-bug-in-foo-py-3a7f9c1d.md)

# Replay the same dispatch verbatim later — no retyping prompt or flags
popola dispatch --replay cursor-fix-bug-in-foo-py-3a7f9c1d
# → cursor-1f0a2b8d4e5c (new task_id, but same dispatch payload)

# Inline overrides emit a stderr warning (No Silent Failures)
popola dispatch new-text --cli=claude --replay cursor-fix-bug-in-foo-py-3a7f9c1d
# stderr: warning: --replay overrides inline prompt='new-text', --cli='claude' with envelope values
```

`--replay` resolves the envelope from `$POPOLA_HANDOFF_DIR` (or `.local/.agent/handoff/` default); `FileNotFoundError` → exit 1 with helpful message; path-traversal in id → exit 2.

## What's NEW · `popolaloom.handoff.FeedbackEnvelope`

```python
from datetime import UTC, datetime
from popolaloom.handoff import (
    FeedbackEnvelope,
    generate_feedback_id,
    write_feedback,
)

env = FeedbackEnvelope(
    feedback_id=generate_feedback_id(
        task_id="cursor-23e74ec18917",
        hitl_id="hitl-abc-001",
        answer="approve",
        responder="alice@neolix.ai",
    ),
    created_at=datetime.now(UTC),
    task_id="cursor-23e74ec18917",
    hitl_id="hitl-abc-001",
    answer="approve",
    reason="diff looks good after the NoneType fix",
    tags=["v0.7.3", "approval"],
    responder="alice@neolix.ai",
    channel="lark",
)
path = write_feedback(env)
# → .local/.agent/handoff/cursor-23e74ec18917-fb-3a7f9c1d.md
```

The on-disk feedback file follows the same Markdown front-matter shape as the dispatch envelope:

```
---
schema_version: '1'
feedback_id: cursor-23e74ec18917-fb-3a7f9c1d
created_at: '2026-05-06T14:35:00+00:00'
task_id: cursor-23e74ec18917
hitl_id: hitl-abc-001
reason: diff looks good after the NoneType fix
tags:
- v0.7.3
- approval
responder: alice@neolix.ai
channel: lark
---
approve
```

The `-fb-` infix in the filename keeps feedback envelopes physically distinct from dispatch envelopes in the same active dir.

**Important**: v0.7.3 ships the writer + schema only — the existing `popola feedback <hitl_id> <answer>` CLI does NOT yet auto-persist. Custom scripts can call `write_feedback(env)` manually for after-the-fact audit imports. v0.7.4 will add a `--persist` flag to wire it into the live HITL flow.

## What's NEW · `to_handoff_envelope(relay_env)` bridge

```python
from popolaloom.daemon.primitives import RelayHandoffEnvelope, to_handoff_envelope
from popolaloom.handoff import write_envelope

# Existing v0.3.0 relay envelope
relay_env = RelayHandoffEnvelope(
    source_cli="cursor",
    target_cli="claude",
    source_task_id="cursor-23e74ec18917",
    payload={"file": "src/foo.py", "kind": "review"},
    reason="cross-CLI code review",
    constraints={"timeout": 1800},
)

# Convert to v0.8.0 envelope schema
new_env = to_handoff_envelope(relay_env, prompt="please review src/foo.py for bugs")

# Now we can write it like any other handoff envelope
write_envelope(new_env)
# tags will include "relay-bridged" so downstream tools can filter relay-origin envelopes
```

Field mapping: `source_task_id → parent_task_id`, `payload → adapter_extra["_relay_payload"]`, `tags=["relay-bridged"]`. The `relay()` primitive itself stays unchanged (still emits the legacy schema into `extra["handoff_envelope"]`), so v0.3.0–v0.7.2 consumers keep working without modification.

## Files changed (v0.7.3)

| 改动 | 文件 |
|---|---|
| dispatch --replay | `src/popolaloom/cli/main.py` (+90 lines: `_resolve_replay`, `_ReplayPayload`, dispatch() arg refactor) |
| FeedbackEnvelope (NEW) | `src/popolaloom/handoff/feedback.py` (244 lines, FeedbackEnvelope + generate_feedback_id + feedback_path + write_feedback) |
| handoff module surface | `src/popolaloom/handoff/__init__.py` (+8 new exports) |
| Relay bridge | `src/popolaloom/daemon/primitives/relay.py` (+92 lines: `to_handoff_envelope`) |
| Primitives surface | `src/popolaloom/daemon/primitives/__init__.py` (export `to_handoff_envelope`) |
| Docs | `README.md` (new "Hands-off envelope" section), `docs/USER_GUIDE.md` (full hands-off envelope chapter), `src/popolaloom/skills/popola-loom/SKILL.md` (+4 commands rows) |
| Tests (NEW) | `tests/cli/test_dispatch_replay.py` (8 tests), `tests/handoff/test_feedback.py` (25 tests), `tests/test_relay_bridge.py` (13 tests) |
| Bump | `pyproject.toml`, `src/popolaloom/__init__.py`, SKILL.md (×2), `.popola-loom-version`, `tests/test_smoke.py`, CHANGELOG.md, RELEASE_NOTES.md |

## Status

| Capability | Status |
|---|---|
| ALL v0.7.2 capabilities | unchanged ✓ |
| `popola dispatch --replay <handoff_id>` | new ✓ |
| `popolaloom.handoff.FeedbackEnvelope` (Q7=yes foundation) | new ✓ |
| `to_handoff_envelope(relay_env)` bridge | new ✓ |
| `popola feedback ... --persist` (live HITL wiring) | deferred → v0.7.4 |
| README + USER_GUIDE + SKILL.md hands-off envelope docs | ✓ |
| 1597 default-lane tests / 94.42% coverage | ✓ |

## Self-eval (v0.7.3)

- `pytest -m "not slow and not nightly and not real_cli and not real_lark" --cov=src/popolaloom --cov-fail-under=94`：1597 passed, 18 skipped, 82 deselected, 0 failed
- `ruff check src/popolaloom tests/`：All checks passed
- `popolaloom.handoff` 模块覆盖率：100% (across all 6 source files, line + branch)
- 累计 commits 在 `fix/v0.7.1-cancel-orphan-and-rehydrate-spawn-aborted` 分支：9（5 from v0.7.1 + 2 from v0.7.2 + 1 v0.7.3 feat + 1 v0.7.3 release）

## Upgrade notes

- **No breaking changes**：所有现有 CLI 调用零感知迁移；replay 是新加 `--replay` flag，旧 `popola dispatch <prompt> --cli=...` 完全不变
- v0.7.4 / v0.8.x 会把 `popola feedback ... --persist` 接入 live HITL flow（FeedbackEnvelope 已经准备好）
- 升级仅需 `pip install -U popolaloom`

## Next steps

1. v0.8.0 final minor bump：documentation-only release（CHANGELOG/RELEASE_NOTES/README 收口、版本号 0.7.3 → 0.8.0、PR 不直 push main）
2. Beyond 0.8.0：FeedbackEnvelope live CLI wiring (`--persist`)、archive auto-trigger on terminal state、`relay()` 切到原生 v0.8.0 envelope schema（v0.9.0 时 deprecate `RelayHandoffEnvelope`）
