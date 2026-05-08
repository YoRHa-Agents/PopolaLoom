---
layout: default
title: Known limitations
description: Documented limitations of PopolaLoom — workarounds and tracking links.
---

# Known limitations

This page collects **documented limitations** of PopolaLoom that have known
workarounds and are tracked for resolution in a future release. Each entry
states the symptom, the recommended workaround, and links to the design
note and backlog row that own the fix. Items here are *expected to be
addressed in future versions* — file an issue under
[`.local/feedbacks/`](../.local/feedbacks/) if you hit one and the
listed workaround does not unblock you.

## v0.8.6 — Cloud task hydration after daemon restart

<!-- updated: 2026-05-08 -->

**Limitation.** PopolaLoom cloud-runtime tasks
(`popola dispatch ... --cli=cursor-cloud`) carry two pieces of in-memory
state inside the `popolad` daemon process:

1. The `CloudPollLoop` thread (`src/popolaloom/daemon/cloud_poller.py`)
   that drives `TaskHandle.cloud_phase` updates by polling
   `GET /v1/agents/{id}/runs/{run_id}` every `interval_s` (default 2 s).
2. Any active SSE attach session — the `SSEReader` pump opened by
   `popola attach --follow` against
   `GET /v1/agents/{id}/runs/{run_id}/stream`, including its
   `Last-Event-ID` resume cursor.

Both are **lost when `popolad` restarts**. The persistent state in sqlite
(the `TaskHandle` row, the `popola_dispatch` row, and the appended
`event_log.jsonl` events) survives the restart, so the task itself is not
orphaned, but the poll loop and any SSE attach session must be
re-initialized manually before the daemon resumes emitting
`cloud.run_status` and `cloud.sse.*` events for that task.

**Symptoms.**

- After `popolad restart`, `popola list` still shows the cloud task with
  its prior `runtime=cloud` and last-known `cloud_phase`, but no new
  `cloud.run_status` events arrive until the next poll cycle (cold start).
- `popola attach <task_id>` immediately after a restart may show no new
  events until that next poll cycle completes; any pre-restart SSE session
  must be re-established because the server-side stream was dropped along
  with the daemon process.

**Workaround.**

- Re-issue `popola attach <task_id>` after the restart. The daemon's
  rehydration logic (introduced in v0.7.1+; see
  `Supervisor._rehydrate_from_persistence`) resumes the poll loop on the
  next dispatch event, and the new `attach` opens a fresh SSE session
  (subject to the Cursor server's stream-replay window).
- For batch or unattended workflows that cannot tolerate the cold-start
  gap, prefer keeping `popolad` up across the task lifetime, or wait for
  the persistent-cursor work tracked under `BL-v0.8.6-1` (see *Tracking*
  below).

**Design references.**

- [`state-source-of-truth.md` §5 failure mode #6](../.local/research/v0.8.6_sse/state-source-of-truth.md#5-failure-modes) —
  *"Stream open but poller dies (e.g., daemon-thread crash)"*: the same
  diagnosis applies to a full daemon restart, with the planned
  `cloud.poller_lost` heartbeat as the follow-up signal.
- [`state-source-of-truth.md` §8 out-of-scope](../.local/research/v0.8.6_sse/state-source-of-truth.md#8-out-of-scope) —
  a daemon-resident persistent SSE subscription is explicitly deferred to
  ≥v0.8.7 if user-driven need emerges; cross-session dedup persisted to
  disk likewise stays in-memory only for v0.8.6.

**Tracking.** Backlog row
[`BL-v0.8.6-1` in `.local/feedbacks/TRACKER.md`](../.local/feedbacks/TRACKER.md)
covers the cursor-persistence + SSE `last_event_id` snapshot work that
would close this limitation.
