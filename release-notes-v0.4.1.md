# PopolaLoom v0.4.1 — Lark proactive-notification minor

> Released: 2026-05-05
> Phase 1 close-out: v0.4.0 GA + 1 minor patch
> Theme: turn the Lark channel from "HITL-only" into "every terminal
> state surfaces a card", and close the latent `task.canceled`
> contract bug so the runner / nines pipeline reads truthfully.

## Summary

PopolaLoom v0.4.1 closes the Lark gap left open by v0.4.0
([release-notes-v0.4.0.md "Known limitations" §6](release-notes-v0.4.0.md))
in two strokes:

1. **Daemon→user notifications**: every task that reaches a terminal
   state (`COMPLETED`, `FAILED`, `CANCELED`) now triggers a Lark
   interactive card delivered to the operator's chat — the daemon is
   no longer "silent until the user looks at `popola tail`".
2. **Contract repair**: `task.canceled` NDJSON events are now actually
   emitted from the supervisor wait-thread (the L1 stage of this
   minor); `evaluation/runner.py:325-331`'s `lark_send_total` /
   `dispatch_isolation` calculations are no longer permanently zero
   on the cancel side.

The minor stays inside the v0.4.0 envelope: 9 spec modules, no new
ADR, no `pyproject.toml` dependency change, version `0.4.0` →
`0.4.1`, default-lane coverage `91.36 %` → **`91.38 %`**.

## Closures (v0.4.1 plan §G.2 5/5 must-haves)

| # | Must-have (research §G.2) | Closure |
|---|---|---|
| 1 | `task.canceled` actually emitted by supervisor | **L1** — `Supervisor._resolve_terminal_event` now consults `StateStore.get(task_id)` and emits `task.canceled` with `{exit_code, pid, sigkill_escalated}` when the handle is `CANCELED`. Backward-compat: optional `state_store=` kwarg, default `None` falls back to v0.4.0 two-way emit. |
| 2 | 5 new card builders (terminal taxonomy) | **L1** — `build_completion_card` (green), `build_failure_card` (red), `build_canceled_card` (yellow), `build_cancel_escalated_card` (orange), `build_skill_missing_card` (yellow). Each renders the workspace `来源标注` footer via `footer_with_origin_note`. |
| 3 | `_on_subprocess_exit` proactively notifies Lark | **L2.B** — after the StateStore update succeeds, the wait-thread schedules `lark.notifier.send_terminal_notification(...)` on the daemon's asyncio loop via `asyncio.run_coroutine_threadsafe`. The notifier returns a frozen `NotificationOutcome(ok, skipped, reason)` for v0.5.0 doctor introspection. |
| 4 | `_build_default_popolad` auto-starts `LarkSupervisor` | **L2.C** — when `is_lark_runtime_available() == True` AND `lark_target_open_id() is not None`, the daemon constructs `LarkSupervisor(LarkListener(callbacks=...))` and schedules its `start()` as a background task. Failure modes (env unset / cli missing / start raises) all log `lark.supervisor.skipped reason=...` or `lark.supervisor.start_failed` (No Silent Failures). |
| 5 | release-notes-v0.4.1.md + CHANGELOG + version bump | **L2.F** — this document, `CHANGELOG.md` `[0.4.1]` entry, `pyproject.toml` `0.4.0` → `0.4.1`, `src/popolaloom/__init__.py` ditto, `tests/test_smoke.py` smoke version updated. |

## What changed (file-by-file)

### Source (5 files)

- `src/popolaloom/daemon/supervisor.py` (L1) — wait-thread now
  consults `StateStore` to pick `task.completed` / `task.failed` /
  `task.canceled` (Option 2 fix per research §F.3).
- `src/popolaloom/daemon/server.py` (L1 + L2.B) — `cancel_task` flips
  the `cancel_escalated_to_sigkill` flag before the SIGKILL syscall;
  `_on_subprocess_exit` consults StateStore before deciding the new
  state (CANCELED-clobber guard); same method now schedules
  `send_terminal_notification` on the bound asyncio loop. New
  `attach_loop(loop)` method + `lark_supervisor` property on `Popolad`.
- `src/popolaloom/daemon/state.py` (L1) — `TaskHandle` gained
  `cancel_escalated_to_sigkill: bool = False`; `StateStore.update`
  exposes the new keyword.
- `src/popolaloom/daemon/main.py` (L2.C) — `_build_default_popolad`
  calls a new `_maybe_wire_lark_supervisor(popolad)` helper that
  builds `LarkListener(LarkEventCallbacks(...))` + `LarkSupervisor`
  and schedules `supervisor.start()` as a background task on the
  current asyncio loop. New `_safe_supervisor_start`,
  `_build_lark_callbacks`, `_extract_sender_open_id`,
  `_make_supervisor_event_logger` helpers. **Only the
  `_build_default_popolad` area touched** (per the L0 owned-files
  contract); main(), get_popola_home(), get_*_path() all unchanged.
- `src/popolaloom/lark/notifier.py` (NEW, L2.A) — async
  `send_terminal_notification(popolad, task_id, terminal_state,
  exit_code) -> NotificationOutcome`; `LARK_NOTIFICATION_LOG_KEYS`
  constant; per-state env var gating
  (`LARK_NOTIFY_ON_{COMPLETED,FAILED,CANCELED,CANCEL_ESCALATED}`);
  prompt summary truncation (`LARK_NOTIFY_PROMPT_TRUNCATE`); explicit
  log line on every skip and every send.

### Card builders (1 file)

- `src/popolaloom/lark/card_templates.py` (L1) — 5 new builders +
  `_terminal_card_envelope` shared helper +
  `HEADER_COLOR_BY_TERMINAL_TRIGGER` palette extension (`green`,
  `orange`).

### Renderer wiring (1 file)

- `src/popolaloom/hitl/renderers/lark.py` (L1 + L2.D) — `kind:
  Literal["hitl","terminal","notification"]` parameter (default
  `"hitl"`); new `card_payload` parameter so terminal builders bypass
  HITLPrompt argv construction; new optional `event_log` parameter
  that writes `lark.send.{ok,failed}` NDJSON envelopes carrying the
  `kind` field.

### Package surface (1 file)

- `src/popolaloom/lark/__init__.py` — re-exports `notifier`,
  `send_terminal_notification`, `NotificationOutcome`,
  `LARK_NOTIFICATION_LOG_KEYS`, plus the existing
  `lark_target_open_id` and `lark_allowed_responders` helpers.

### Tests (3 new files + 3 L1 files)

L1 (already landed in commit `1935b44`):

- `tests/lark/test_card_templates_v041.py` (11 cases)
- `tests/daemon/test_supervisor_terminal_events.py` (3 cases)
- `tests/hitl/test_send_lark_card_kind.py` (1 case)

L2 (this stage):

- `tests/lark/test_notifier.py` (18 cases) — full
  `send_terminal_notification` matrix: 4 trigger × happy + skip
  paths (lark cli unavailable, target unset, env off, handle
  missing, non-terminal state, cancel-escalated env opt-out, state
  store missing, frozen dataclass, prompt summary override, env
  override, retry failure, card-build failure).
- `tests/daemon/test_lark_terminal_notify_e2e.py` (2 cases) —
  end-to-end `dispatch_task` → completion / cancel → `lark-cli`
  invoked with the right `--target-id` / `--metadata-key task_id=...`
  argv + NDJSON envelope written.
- `tests/daemon/test_lark_supervisor_wiring.py` (8 cases) —
  `_build_default_popolad` env-on / env-off / cli-unavailable
  branches; supervisor start exception swallow; supervisor on_event
  logger; `LarkEventCallbacks` routing into `HITLStore.fold_reply`
  (card_action + text_feedback + unauthorized + exception swallow);
  `_extract_sender_open_id` defensive shapes.

## Test counts + coverage

- **Default-lane**: **1023 pass / 0 fail / 18 skipped** (was 980 at
  v0.4.0, +43 from L1+L2). Tests run in ~22 s.
- **Coverage**: **91.38 %** (was 91.36 % at v0.4.0; L1 caused a
  temporary 91.28 % dip, L2.E recovers and exceeds).
- **No new lint or type errors** in any of the 9 owned source files.

## Behaviour deltas from v0.4.0

1. **Daemon startup** — when `lark-cli` is on PATH AND
   `LARK_HITL_TARGET_OPEN_ID` is set, the daemon now spawns the
   `lark-cli event consume ...` subprocess (under `LarkSupervisor`)
   automatically. Without those env vars / binary, behaviour is
   identical to v0.4.0 (single `lark.supervisor.skipped` INFO log).
2. **Task lifecycle** — every COMPLETED / FAILED / CANCELED transition
   now triggers a Lark card unless the matching env opt-out is set.
   The default for the 3 main triggers is ON; the `cancel_escalated`
   trigger is OFF by default to avoid double-cards on SIGKILL paths.
3. **NDJSON event log** — per-task NDJSON now contains
   `lark.send.{ok,failed}` envelopes alongside the existing
   `task.{dispatched,completed,failed,canceled}` envelopes; the new
   envelopes carry a `kind: "terminal"` field so downstream consumers
   can disambiguate (HITL sends carry `kind: "hitl"`).
4. **State store** — `TaskHandle` gained
   `cancel_escalated_to_sigkill: bool = False`; cancel_task sets it
   before the SIGKILL syscall so the supervisor wait-thread can stamp
   the right value into the terminal event.

## Known limitations / deferred to v0.5.0

1. **Lark supervisor graceful shutdown** — the supervisor is started
   as a background task on the daemon loop; `daemon/rpc.py`'s
   lifespan exit handler is **not** modified by this minor (per the
   L0 owned-files contract). When the daemon process exits, the
   `lark-cli event consume` subprocess receives the inherited
   SIGTERM and exits naturally; explicit
   `await popolad._lark_supervisor.stop()` integration is deferred
   to v0.5.0 (where the cleanup also covers Skill / multi-IDE
   tear-down). For test environments, callers can invoke
   `await popolad.lark_supervisor.stop()` directly.
2. **Listener → HITL store wiring** — when `popolad.hitl_store` is
   unset (e.g. early in boot or in tests that don't wire HITL), the
   `LarkEventCallbacks` log + drop incoming card actions / text
   feedback at DEBUG. Wiring the HITL store at daemon startup is
   already covered by `daemon/rpc.py:create_app` lifespan — that
   path remains unchanged.
3. **Coverage 91.38 % vs 92 % aspirational target** — same as v0.4.0
   (0.62 pp gap, mostly CLI / RPC integration error paths). Still
   tracked for v0.5.0.
4. **Skill install / multi-IDE / `popola init`** — out of scope for
   this minor; the v0.5.0 plan
   ([`.local/memory/specs/popolaloom/v0.5.0-plan.md`](.local/memory/specs/popolaloom/v0.5.0-plan.md))
   picks them up next.
5. **`task.cancel_escalated` notification card** — defaults to OFF
   (`LARK_NOTIFY_ON_CANCEL_ESCALATED=0`) per research §E.2.1 noise
   analysis; opt-in by setting the env var to `1`.

## v0.5.0 hand-off contract (per v0.4.1 plan §0.5)

The v0.5.0 milestone (Skill install + multi-IDE + `popola doctor`)
relies on the following stable surfaces from v0.4.1:

```python
from popolaloom.lark.notifier import (
    LARK_NOTIFICATION_LOG_KEYS,   # tuple[str, str] — frozen
    NotificationOutcome,          # @dataclass(frozen=True)
    send_terminal_notification,   # async coroutine
)
```

These are exported from `popolaloom.lark.__init__` for convenience.
v0.5.0 `popola doctor` walks per-task NDJSON for events whose `type`
is in `LARK_NOTIFICATION_LOG_KEYS` and reads the `kind` /
`message_id` / `error` fields to compute Lark delivery health
without re-deriving from log lines.

## Verification commands

```bash
# 1. version
python -c "import popolaloom; assert popolaloom.__version__ == '0.4.1'"

# 2. default lane + coverage gate
pytest -m "not slow and not nightly and not real_cli and not real_lark" \
  --cov=src/popolaloom --cov-fail-under=91

# 3. ruff + mypy on the touched files
ruff check src/popolaloom/lark/notifier.py \
  src/popolaloom/daemon/server.py \
  src/popolaloom/daemon/main.py \
  src/popolaloom/hitl/renderers/lark.py \
  src/popolaloom/lark/__init__.py
mypy src/popolaloom/lark/notifier.py

# 4. spot-check the 3 new test files
pytest tests/lark/test_notifier.py \
  tests/daemon/test_lark_terminal_notify_e2e.py \
  tests/daemon/test_lark_supervisor_wiring.py -v

# 5. import-stability sanity for the v0.5.0 contract
python -c "from popolaloom.lark.notifier import LARK_NOTIFICATION_LOG_KEYS, NotificationOutcome, send_terminal_notification; print('OK', LARK_NOTIFICATION_LOG_KEYS)"

# 6. skip path (no env vars, no lark-cli) — daemon must boot OK
unset LARK_HITL_TARGET_OPEN_ID LARK_NOTIFY_TARGET_OPEN_ID
python -c "
import asyncio
from pathlib import Path
import tempfile
from popolaloom.daemon.main import _build_default_popolad

async def boot():
    with tempfile.TemporaryDirectory() as d:
        p = _build_default_popolad(Path(d))
        assert p.lark_supervisor is None  # silently skipped
        print('skip path OK')

asyncio.run(boot())
"
```

All six commands exit 0 on a clean v0.4.1 checkout.

---

**PopolaLoom v0.4.1 ships 2026-05-05.**
Phase 2 (v0.5.0: Skill install + multi-IDE + `popola doctor`) starts
on the next branch off `main`.
