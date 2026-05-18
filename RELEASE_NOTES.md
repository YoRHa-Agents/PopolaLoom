> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md).

# PopolaLoom v1.5.1 — cloud-cancel race window + Cursor REST schema docs

<!-- updated: 2026-05-18 -->

## v1.5.1 callouts

> **Cloud-cancel race window closed.** `popola cancel` of a `cursor-`-prefixed task that races the supervisor's cloud-handle hydration no longer trips the LOCAL "has no pid yet" guard or the cloud "cloud_cancel_no_handle" guard. `Popolad.cancel_task(...)` gains a new `cloud_cancel_grace_s: float = 3.0` keyword that bounds a 50-ms-tick polling loop until either the supervisor populates `runtime="cloud"` + `cursor_agent_id` (cancel proceeds), the task hits a terminal state mid-wait (raise the L1047 terminal guard's `RuntimeError`), or the deadline expires (emit structured `task.failed` event with `error_kind="cloud_cancel_race_window_exceeded"` + raise `RuntimeError`). No silent fallback. Daemon shutdown clamps the grace at 1.0 s to keep total shutdown bounded.

> **G3 verification oracle replaced (Cursor REST `is_in_use` always null for named workers).** The v1.5.0 oracle "self-hosted-worker dispatch routed correctly when `worker.is_in_use=true active_bc_id=<bc>`" is unusable — Stage T live probe (5 mid-run snapshots at 30 s intervals against `api.cursor.com` on 2026-05-18) showed `is_in_use`, `active_bc_id`, AND `lastActivityAt` ALL stayed `null` even while the agent ran end-to-end. The recommended G3 oracle is now **`agent.env`** from `GET /v1/agents/<bc-id>` — a named-worker dispatch sees `env: {"type": "machine", "name": "<your-worker>"}` durably for the agent's lifetime (NOTE: `agent.target` is `null` in this response; the routing target lives in `env`) — **PLUS** the Prometheus metric `cursor_self_hosted_worker_last_activity_unix_seconds` from the worker's own `--management-addr` `/metrics`. Documented in both SKILL.md mirrors.

> **G5 verification oracle replaced (Cursor REST `model_details=null` for path-A agents).** `GET /v1/agents/<bc-id>` now returns `model_details=null` for agents created via path-A REST `POST /v1/agents`, even when `--cli-flag model_id_override=<X>` was honored. The v1.5.0 oracle "`agent.model_details.model_name ends with '-high'`" no longer fires for path-A. The recommended G5 oracle is now **absence of the Cursor server error `Model '<X>' does not support long-running agent mode`** in the agent's run-event NDJSON log, with successful terminal state.

## Highlights

| Item | v1.5.1 resolution |
|---|---|
| `popola cancel` cloud race window | New `cloud_cancel_grace_s: float = 3.0` kwarg on `Popolad.cancel_task`; 50 ms polling loop with structured `task.failed` event on deadline. |
| Daemon shutdown bound | `popolaloom.daemon.rpc.lifespan` shutdown loop clamps `cloud_cancel_grace_s=1.0`. |
| G3 oracle (worker routing) | `agent.env` (`{type:"machine",name:"<X>"}`) + worker `/metrics` `cursor_self_hosted_worker_last_activity_unix_seconds` (replaces `is_in_use`, which stayed null mid-run during Stage T live probe). |
| G5 oracle (model wiring) | Absence of `Model 'X' does not support long-running agent mode` error on terminal state (replaces null `model_details`). |
| `.claude` SKILL mirror docs catch-up | `.claude/skills/popola-loom/SKILL.md` brings forward the v1.5.0 No-Silent-Fallback / popolad env / path-B sections it was missing. |
| Tests | 5 new race-window contract tests pinning the v1.5.1 `cloud_cancel_grace_s` contract. |

## Backward compatibility

`cloud_cancel_grace_s=0.0` (NEW v1.5.1 default-opt-out) preserves the v1.5.0 immediate-fail semantics — the deadline check fires on the FIRST polling iteration and the path emits the structured event + raises `RuntimeError` without blocking. The error message string changes from the v1.5.0 LOCAL `task <id> has no pid yet (race window between dispatch and spawn)` guard to the v1.5.1 `task <id> cloud handle not populated within 0.00s grace window` — both loud failures with no silent fallback. External scripts that grep the old error text should broaden the match.

The default of `3.0 s` is conservative and effectively transparent for normal-flow callers — by the time an operator runs `popola cancel`, `supervisor.spawn` has already had >50 ms to flip `runtime="cloud"` and populate `cursor_agent_id`. The grace-window code path only fires for genuinely-racy cancels.

## Upgrade

```bash
# Existing PopolaLoom installation:
popola update

# Or fresh install:
pip install --upgrade git+https://github.com/YoRHa-Agents/PopolaLoom@v1.5.1

popola version  # → popolaloom 1.5.1
```

## Known limitations carry-over from v1.5.0

- **Path-B server-side pool downgrade** — Cursor's `StartBackgroundComposerFromSnapshot` Connect-RPC SILENTLY downgrades `env={"type":"machine","name":X}` to `env={"type":"pool"}` server-side. Use `--auth-mode=rest` for precise named-worker routing. (Documented in SKILL.md ⚠️ Path-B server-side routing limitation.)
- **GPT-5.5 + `long_running_agent_mode`** — Cursor's path-B server rejects bare `gpt-5.5` when `long_running_agent_mode=true`. The escape hatch `--cli-flag model_id_override=gpt-5.5-high` remains the documented workaround.
- **JWT auto-refresh** — JWT exp is 1 h; popolaloom currently warns at boundary but doesn't refresh (`BL-v1.4.x-jwt-auto-refresh`).
- **Coverage 94% floor** — temporarily at 93%; soak on `cloud_worker_cmd.py` / `cursor_cloud.py` error paths still pending (`BL-v1.0.x-coverage-94-restore`).

## Next steps (deferred to v1.5.2 / v1.6.0)

- BL-v1.3.x-bc-model-whitelist-sync — probe-and-cache the path-B model list (escape hatch landed in v1.5.0 via `--cli-flag model_id_override=<id>`).
- BL-v1.3.x-path-b-non-github-routing — Cursor server-side hard constraint (cursor-managed cloud + non-GitHub repos).
- Anything filed in `.local/feedbacks/feedback_for_v1.5.1.md` after the v1.5.1 Stage T live probe.
