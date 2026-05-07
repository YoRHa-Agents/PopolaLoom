> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.8.0 — Hands-off envelope (stable)

> Released: 2026-05-06
> Theme: documentation-only minor bump that promotes the v0.7.1 → v0.7.3 hands-off envelope feature to a stable v0.8.x surface. **No new code, no breaking changes** — every Python API and CLI verb shipped in v0.7.3 is preserved verbatim.

## Why a v0.8.0 minor (and not a v0.7.4 patch)?

The v0.7.x patch series (v0.7.1 → v0.7.2 → v0.7.3) landed the entire hands-off envelope feature in three iterative slices, each with its own `pytest --cov-fail-under=94` + `ruff` gate, per the user-requested "几轮自验证和自己迭代，每一轮 patch 一个版本" workflow. v0.8.0 is the **stability anchor** that:

- Promotes `popolaloom.handoff.HandoffEnvelope` `schema_version="1"` to a non-experimental contract — future schema evolutions will go through the `schema_version` field, not silent edits.
- Stabilises the `popola dispatch` / `popola handoff` / `--replay` / `dispatch_with_envelope` surface — these are now first-class building blocks, not v0.7.x experiments.
- Marks the C5 双通道 (env primary, flag forward-compat) injection contract as load-bearing.

All v0.7.x patches are summarised below into a single coherent feature description so users can understand the v0.8.0 surface in one read.

## What's new in v0.8.0 (vs v0.7.0)

### 1. `popolaloom.handoff` module — file-backed dispatch payload

Every `popola dispatch` writes a Markdown front-matter envelope to `.local/.agent/handoff/<handoff_id>.md` (gitignored as of v0.7.0):

```
---
schema_version: '1'
handoff_id: cursor-fix-bug-in-foo-py-3a7f9c1d
created_at: '2026-05-06T14:30:00+00:00'
source_cli: null
target_cli: cursor
parent_task_id: null
cwd: null
adapter_extra: {}
constraints: {}
reason: null
tags: []
---
fix the bug in foo.py — there's a NoneType error around line 42
```

- **Schema** (Pydantic v2, `extra="forbid"`, `schema_version="1"`): 13 fields including `handoff_id`, `created_at`, `source_cli` / `target_cli`, `parent_task_id`, `prompt`, `cwd`, `adapter_extra`, `constraints`, `reason`, `tags`.
- **Slug-hash addressing**: id format is `<target_cli>-<slug-from-prompt>-<8hex content hash>` over `(target_cli, prompt, parent_task_id, adapter_extra, constraints)` — content-derived, so the same inputs always map to the same id.
- **Atomic writer**: `write_envelope` uses `os.replace` over a same-dir `.tmp` to dodge cross-device rename failures (POSIX) + the Windows replace-if-exists contract.
- **Active/archive 双层**: `archive_envelope(handoff_path, task_id)` copies via `shutil.copy2` to `.local/.agent/archive/<task_id>/<id>.md` (mtime preserved, source not deleted — audit snapshot semantics).
- **Loader**: `list_active_envelopes` / `load_envelope` / `resolve_envelope_path` read-side helpers.

### 2. `Popolad.dispatch_with_envelope` (E3 internal unification)

The canonical dispatch entry. Writes the envelope, injects `POPOLA_HANDOFF_FILE` + `POPOLA_HANDOFF_ID` into the spawn env, delegates to graph/legacy with the merged env. **`Popolad.dispatch_task(cli, prompt, ...)` is now a thin wrapper** that builds an envelope from kwargs and delegates — every dispatch in the codebase goes through one path internally. Public signatures unchanged so rpc.py / cli/main.py / 1494+ existing tests keep working.

```python
from datetime import UTC, datetime
from popolaloom.daemon import Popolad
from popolaloom.handoff import HandoffEnvelope, generate_handoff_id

popolad = Popolad(events_dir=...)

env = HandoffEnvelope(
    handoff_id=generate_handoff_id("cursor", "fix bug"),
    created_at=datetime.now(UTC),
    target_cli="cursor",
    prompt="fix bug in foo.py",
    reason="user reported during code review",   # audit-only, not in argv
    tags=["v0.8.0", "bug-fix"],                   # audit-only, not in argv
)
task_id = popolad.dispatch_with_envelope(env)
```

### 3. C5 双通道 (env primary + flag opt-in forward-compat)

Every spawned sub-CLI now sees:

```bash
POPOLA_HANDOFF_FILE=/abs/path/to/.local/.agent/handoff/<id>.md
POPOLA_HANDOFF_ID=<id>
```

The agent inside the sub-CLI can `cat $POPOLA_HANDOFF_FILE` to inspect the original dispatch — including audit-only fields (`reason`, `tags`) that don't fit into a single argv prompt. Overlay always wins over caller-provided base_env keys with the same name (anti-impersonation invariant).

The `--popola-handoff-file <path>` argv flag is **opt-in** (`extra["popola_handoff_flag"]=True` / `--cli-flag popola_handoff_flag=true`) — vanilla cursor-agent / claude / codex don't recognise it yet, so auto-injection would break their argv parsing. The flag stays as a forward-compat hook for sub-CLIs that gain native support.

### 4. `popola handoff` CLI — filesystem-only inspection / archive

| Verb | Purpose |
|---|---|
| `popola handoff list [--json] [--handoff-dir DIR]` | List active envelopes, sorted by mtime descending |
| `popola handoff show <id> [--json] [--handoff-dir DIR]` | Print Markdown envelope (or JSON) |
| `popola handoff archive <id> <task_id> [--archive-root DIR]` | Snapshot to `<archive_root>/<task_id>/<id>.md` |

All three are filesystem-only — no daemon required, safe to run during incidents.

### 5. `popola dispatch --replay <handoff_id>` — deterministic replay

Read a previously written envelope and re-issue the exact dispatch:

```bash
popola dispatch --replay cursor-fix-bug-in-foo-py-3a7f9c1d
```

Inline overrides (`prompt` / `--cli` / `--cwd` / `--cli-flag`) emit a stderr warning; missing id → exit 1; path-traversal in id → exit 2.

### 6. `popolaloom.handoff.FeedbackEnvelope` — HITL feedback (Q7=yes foundation)

Companion to `HandoffEnvelope` for the user's typed reply to a `LangGraph.interrupt()` prompt:

```
---
schema_version: '1'
feedback_id: cursor-23e74ec18917-fb-3a7f9c1d
created_at: '2026-05-06T14:35:00+00:00'
task_id: cursor-23e74ec18917
hitl_id: hitl-abc-001
reason: diff looks good after the NoneType fix
tags: [v0.8.0, approval]
responder: alice@neolix.ai
channel: lark
---
approve
```

Filename: `<task_id>-fb-<8hex>.md`. The `-fb-` infix keeps feedback envelopes physically distinct from dispatch envelopes in the same active dir.

**Note**: v0.8.0 ships the writer + schema; live `popola feedback ... --persist` wiring is in the v0.8.x patch backlog (intentionally deferred to avoid daemon-side coordination risk).

### 7. Legacy `RelayHandoffEnvelope` bridge

`popolaloom.daemon.primitives.to_handoff_envelope(relay_env, prompt=..., cwd=...)` converts the v0.3.0 schema to the v0.8.0 schema — `source_task_id → parent_task_id`, `payload → adapter_extra["_relay_payload"]`, `tags=["relay-bridged"]`. The `relay()` primitive itself stays unchanged (still emits the legacy schema into `extra["handoff_envelope"]`), so v0.3.0–v0.7.2 consumers keep working without modification.

### 8. v0.7.0 BUG fixes (consolidated in v0.7.1)

- **BUG-A**: `popola cancel <task_id>` 现在能清 daemon-restart 后留下的 `pid=null` 孤儿（`_soft_cancel_orphan` 路径，不发 SIGTERM 直接写 `task.canceled`）。
- **BUG-B**: `rehydrate_from_persistence()` 不再复活从未真正 spawn 的 SUBMITTED 任务（缺 `popola_dispatch` row 直接标 `failed` + emit `popolad.spawn_aborted`）。
- **BUG-C**: `popola attach <task_id> --no-follow` 在大事件流时不再误报 `httpx.ReadTimeout`（hybrid 修复：终止事件即 break + 已观测终止后 ReadTimeout 视作正常 EOF）。

## Final gate (v0.8.0)

- `pytest -m "not slow and not nightly and not real_cli and not real_lark" --cov=src/popolaloom --cov-fail-under=94`：1597 passed, 18 skipped, 82 deselected, 0 failed
- `ruff check src/popolaloom tests/`：All checks passed
- `popolaloom.handoff` 模块覆盖率：100% (line + branch)
- 累计 commits 在 `fix/v0.7.1-cancel-orphan-and-rehydrate-spawn-aborted` 分支：10（3 BUG fix + 4 feat + 4 release，含此 v0.8.0 commit）

## Status

| Capability | Status |
|---|---|
| `popolaloom.handoff` module (HandoffEnvelope + FeedbackEnvelope + writer + archive + loader) | stable ✓ |
| `Popolad.dispatch_with_envelope` (E3 internal unification) | stable ✓ |
| `Popolad.dispatch_task` (kwargs surface, backward compat) | stable ✓ |
| C5 env channel (POPOLA_HANDOFF_FILE / POPOLA_HANDOFF_ID) | stable ✓ |
| C5 flag channel (`--popola-handoff-file`) | stable ✓ (opt-in) |
| `popola handoff list / show / archive` CLI | stable ✓ |
| `popola dispatch --replay <id>` | stable ✓ |
| `to_handoff_envelope(relay_env)` legacy bridge | stable ✓ |
| `popola feedback ... --persist` (live HITL wiring) | deferred → v0.8.x patches |
| Native v0.8.0 envelope in `relay()` primitive (deprecate `RelayHandoffEnvelope`) | deferred → v0.9.0 |
| Auto-archive on terminal state | deferred → v0.8.x patches |
| 1597 default-lane tests / 94.42% coverage | ✓ |
| 1380+ tests / 94%+ coverage（v0.7.0 capability） | ✓ (1597+) |

## Upgrade notes

- **No breaking changes**：`pip install -U popolaloom` and re-run `popola init` once to refresh the `~/.cursor/skills/popola-loom/SKILL.md` (or equivalent) marker file.
- Existing `dispatch_task(prompt, cli)` callers keep working unchanged — internally the call now goes through `dispatch_with_envelope`, but the surface contract is preserved.
- Each dispatch now drops a small Markdown file in `.local/.agent/handoff/` (gitignored). Override location with `$POPOLA_HANDOFF_DIR`.
- The legacy `RelayHandoffEnvelope` is **not** deprecated yet — it's the wire schema the `relay()` primitive emits. Migration to native v0.8.0 envelope schema in `relay()` is on the v0.9.0 roadmap; until then use `to_handoff_envelope(relay_env)` to bridge into the new schema for file-based audit.

## Files changed since v0.7.0

| Slice | Files |
|---|---|
| BUG-A/B (v0.7.1) | `src/popolaloom/daemon/server.py`, `src/popolaloom/daemon/rpc.py`, `tests/test_repository.py` |
| BUG-C (v0.7.1) | `src/popolaloom/cli/main.py`, `tests/cli/test_attach_no_follow_eof.py` |
| Handoff foundation (v0.7.1) | `src/popolaloom/handoff/{__init__,envelope,hash,writer,archive}.py` (5 NEW), `tests/handoff/{__init__,test_envelope,test_hash,test_writer,test_archive}.py` (5 NEW) |
| dispatch_with_envelope (v0.7.2) | `src/popolaloom/daemon/server.py` (+178 lines: dispatch_with_envelope + _resolve_handoff_path + _call_adapter post-processing) |
| Loader (v0.7.2) | `src/popolaloom/handoff/loader.py` (NEW) |
| Handoff CLI (v0.7.2) | `src/popolaloom/cli/handoff_cmd.py` (NEW), `src/popolaloom/cli/main.py` (registration) |
| Replay (v0.7.3) | `src/popolaloom/cli/main.py` (+90 lines: `_resolve_replay`, dispatch arg refactor) |
| FeedbackEnvelope (v0.7.3) | `src/popolaloom/handoff/feedback.py` (NEW) |
| Relay bridge (v0.7.3) | `src/popolaloom/daemon/primitives/relay.py` (+92 lines: `to_handoff_envelope`) |
| Test isolation | `tests/conftest.py` (session-scoped autouse `$POPOLA_HANDOFF_DIR` fixture) |
| Tests (NEW total since v0.7.0) | 76+ new tests across `tests/handoff/`, `tests/daemon/`, `tests/cli/`, `tests/test_relay_bridge.py` |
| Docs | `README.md` ("Hands-off envelope" section), `docs/USER_GUIDE.md` (full hands-off envelope chapter), `src/popolaloom/skills/popola-loom/SKILL.md` (+4 commands rows + Q reference), `CHANGELOG.md` (v0.7.1/2/3/0.8.0 sections), `RELEASE_NOTES.md` (this file) |

## Next steps (v0.8.x backlog)

1. `popola feedback ... --persist` — wire `FeedbackEnvelope` into the live HITL feedback CLI flow (currently writer + schema only; CLI doesn't auto-write).
2. Auto-archive on terminal state — popolad's wait-thread should call `archive_envelope` when a task reaches `task.completed` / `failed` / `canceled` (currently archive is explicit via `popola handoff archive`).
3. `relay()` primitive native v0.8.0 schema — emit `HandoffEnvelope` directly instead of legacy `RelayHandoffEnvelope`; deprecate the legacy class with a `DeprecationWarning` in v0.9.0.
4. `popola doctor` adds a check for the active handoff dir's writability + size budget.
5. Web UI: `popola handoff list / show` rendered as a small NiceGUI page (already a stretch goal in the v0.7.0 docs roadmap).

## Branch + PR

This release lands on branch `fix/v0.7.1-cancel-orphan-and-rehydrate-spawn-aborted` (named back when it was originally a v0.7.1 hotfix branch; the scope grew to consume v0.7.2 + v0.7.3 + v0.8.0). Per the workspace's "Protected Branch Workflow" rule, **the branch is NOT pushed to `main` directly** — it should land via a PR after user review.

Suggested PR title: `release: v0.8.0 — hands-off envelope (BUG-A/B/C + handoff foundation + dispatch_with_envelope + replay + FeedbackEnvelope + relay bridge + docs)`.
