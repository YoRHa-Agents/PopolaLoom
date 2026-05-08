> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.8.6 — Cloud Observability + SSE

> Released: 2026-05-08  
> Theme: Layers a **server-sent-events (SSE) ingest** path on top of v0.8.5's REST poller (`GET https://api.cursor.com/v1/agents/{id}/runs/{run_id}/stream`), so `popola attach --follow` on cloud-runtime tasks surfaces assistant deltas / tool calls / terminal `result` events within ≤ 1 s instead of the prior 2 s poll cycle, while keeping the **poller as the sole writer of `cloud_phase`** (SSE only appends to `EventLog` under the new `cloud.sse.*` namespace). Also adds a `runtime` column in `popola list`, a 16-entry **bilingual error hint catalog** with 10 new `CursorCloudError` subclasses, a manual `workflow_dispatch` cloud-smoke CI workflow, and a CI static-grep guard that blocks any future PR from violating the sole-writer invariant. **No breaking changes** — existing `--cli=cursor-cloud` callers see the new column and richer errors automatically; `--no-stream` is the deterministic escape hatch for SSE-restricted networks.

## Research + scope rationale

Wave 1 produced **3 research artefacts** in `.local/research/v0.8.6_sse/` covering the v0.8.6 design surface end-to-end, then Wave 1.2 synthesised them into a Stage 2 plan:

| File | Purpose |
|---|---|
| [`sse-event-schema.md`](.local/research/v0.8.6_sse/sse-event-schema.md) | Canonical SSE event mapping (8 types) + `Last-Event-ID` resume protocol + `410 stream_expired` deterministic fallback |
| [`state-source-of-truth.md`](.local/research/v0.8.6_sse/state-source-of-truth.md) | Sole-writer rule for `cloud_phase` (poller writes; SSE appends), 6 cross-task invariants (I-1 → I-6), failure-mode catalog including hydration debt |
| [`422-error-catalog.md`](.local/research/v0.8.6_sse/422-error-catalog.md) | 16-entry bilingual hint catalog with `(error.code → message regex → HTTP status)` selector precedence and per-entry `cli_exit` codes |
| [`PLAN.md`](.local/.agent/active/v0.8.6-cloud-sse/PLAN.md) + [`DECISIONS.md`](.local/.agent/active/v0.8.6-cloud-sse/DECISIONS.md) | Stage 2 wave / task table; locked decisions Q-A-1 / Q-A-3 / Q-A-4 / Q-A-8 + L0 resolutions for OQ-4 (`409 agent_busy` retry off) and OQ-6 (`cloud.sse.stream_expired` canonical) |

Directive driver: v0.8.5 ship-time `## Risks acknowledged` paragraph that flagged streaming follow-up runs (SSE) and PR-creation ergonomics as stretch / follow-up — v0.8.6 closes the SSE half of that follow-up commitment.

## Highlights

### Wave 2.1 — foundational

- **`SSEReader` in `src/popolaloom/adapters/cursor_cloud.py`** — chunked SSE consumer that parses 8 event types (`assistant_chunk`, `tool_call`, `tool_result`, `result`, `status_*`, `error`, `keepalive`) from `GET /v1/agents/{id}/runs/{run_id}/stream`, emits the `(task_id, run_id, stream_session_id, sse_id, seq)` idempotency quintuple on every `EventLog` write, sends `Last-Event-ID` on resume, raises `CursorCloudStreamExpiredError` on HTTP 410 then exits without reconnecting (per Q-A-4). The reader **holds no `StateStore` reference** at all — enforced at construction time by mypy + a runtime assert (I-1 contract from `state-source-of-truth.md` §1.2 + §6).
- **`runtime` column in `popola list`** (default-on; `--no-runtime` to hide) — table renders `task_id, runtime, cli, state, pid, started_at`. The RPC `list_tasks` summary already carried `runtime` since v0.8.5; this slice surfaces it in the table renderer and adds the escape-hatch flag.
- **16-entry bilingual error catalog + 10 new `CursorCloudError` subclasses** — every catalog row has `hint_en` + `hint_zh` (each ≤ 2 sentences, ≥ 1 `https://...` URL each) and a `cli_exit` code; selector follows precedence `(error.code → message regex → HTTP status)`. New subclasses: `CursorCloudApiKeyRevokedError`, `CursorCloudPlanRequiredError`, `CursorCloudFeatureUnavailableError`, `CursorCloudNotFoundError`, `CursorCloudStreamExpiredError`, `CursorCloudStreamInvalidLastEventIdError`, `RepoAllowlistError`, `GithubAppMissingError`, `GithubAppPermissionError`, `CursorCloudValidationError`, plus `CursorCloudRateLimitError` (429-retryable). Existing `CursorCloudAuthError` / `CursorCloudConflictError` from v0.8.5 are preserved untouched. Per OQ-4 in `DECISIONS.md`, `409 agent_busy` ships `retry: false` in v0.8.6 (queue + notify deferred to v0.8.8).

### Wave 2.2 — integration

- **`popola attach --follow` SSE-driven path** (`src/popolaloom/cli/main.py`) — cloud-runtime tasks open an SSE stream alongside the existing `/attach_stream` SSE consumer. On `410 stream_expired` / `httpx.ReadError` / `httpx.ConnectError` the renderer falls back to the poll-driven view without crashing, surfacing a `cloud.sse.stream_expired` event so operators see the transition. `--no-stream` flag forces the legacy poll-only path. Renderer never promotes `cloud_phase` from a `cloud.sse.*` event (see `state-source-of-truth.md` §4 reconciliation).
- **`cloud_poller.CloudPollLoop` `wake_event` parameter** (`src/popolaloom/daemon/cloud_poller.py`) — optional `threading.Event` replaces the inner `time.sleep` with `wake_event.wait(...); wake_event.clear()`. SSE-side terminal hints wake the poller within ≤ 200 ms instead of after a full poll interval (validates I-6 drift bound). Default `None` preserves v0.8.5 polling cadence — fully backwards-compatible.
- **`Supervisor._spawn_cloud` cloud bootstrap refactor** (`src/popolaloom/daemon/supervisor.py`) — seeds initial `cloud_phase` via the `TaskHandle` constructor instead of via an out-of-band `state_store.update(..., cloud_phase=...)` call, so the supervisor no longer trips the I-1 sole-writer guard while `CloudPollLoop` remains the canonical writer of every subsequent `cloud_phase` transition.
- **I-1 sole-writer CI static-grep guard** (`tests/conftest.py`) — session-scoped fixture greps `src/popolaloom/` for the regex `state[_\s]*store\.update\([^)]*cloud_phase\s*=` and asserts the only matching file is `daemon/cloud_poller.py`. Any future PR adding an out-of-band write fails CI with a fingerprinted error referencing `state-source-of-truth.md` §1.2 + §6 I-1.
- **`docs/known-issues.md`** (new file) + `BL-v0.8.6-1` row in `.local/feedbacks/TRACKER.md` — hydration debt registered as a known limitation per OQ-7 decision (docs-only registration; persistent-cursor + `Last-Event-ID` snapshot work deferred to ≥ v0.8.7).

### Wave 2.3 — CI + docs

- **`.github/workflows/cloud-smoke.yml`** (`workflow_dispatch` only) — runs `pytest -m real_cursor_cloud -k "smoke"` against the live Cursor REST + SSE surface; gated by `if: ${{ secrets.CURSOR_API_KEY != '' }}` so fork PRs and key-less runs log a friendly `"skipping: CURSOR_API_KEY not set"` line instead of a red X. Uses `python-version: "3.11"` to match the repo baseline; secret name matches the existing `CURSOR_API_KEY` convention from v0.8.5.
- **Docs sync** (T2.3.2 sibling slice) — `docs/USER_GUIDE.md` gains "SSE ingest" + "Cloud error hints" subsections; `README.md` mentions the `runtime` column in the `popola list` example output; `src/popolaloom/skills/popola-loom/SKILL.md` references the new SSE behaviour in its cloud workflow section. All three files carry the `<!-- updated: 2026-05-08 -->` Documentation Protocol marker.

### Tests

- **~79 new default-lane tests** across the v0.8.6 surface:
  - **17** in `tests/cloud/test_sse_reader.py` (T2.1.1) — chunked parsing, dedup-on-reconnect, `Last-Event-ID` resume, `410 stream_expired` no-reconnect, `invalid_last_event_id` drop-and-reconnect-once, idempotency quintuple shape, sequence-monotonicity property test (I-3), `__init__`-time assert that no `StateStore` is passed.
  - **8** in `tests/cli/test_list_runtime_column.py` (T2.1.2) — table render contains `runtime` column, JSON output round-trips the field, `--no-runtime` hides the column, header / row alignment.
  - **33** in `tests/cloud/test_422_hints.py` (T2.1.3) — selector precedence (16 catalog rows + heuristic 422 fall-back paths + status-only fall-through), every catalog hint contains a `https://...` URL, every catalog subclass resolves in-module, `409 agent_busy` retry-off in v0.8.6 (per OQ-4), legacy envelope shape tolerated, corrupt-JSON body falls back to status.
  - **14** in `tests/cli/test_attach_sse_fallback.py` (T2.2.1) — happy SSE path, `410 stream_expired` fallback to poll view, `httpx.ReadError` + `httpx.ConnectError` fallback, Ctrl-C clean exit, `--no-stream` flag forces poll-only, never promotes `cloud_phase` from a `cloud.sse.*` event.
  - **6** in `tests/daemon/test_sse_poller_coordination.py` + **1** I-1 static-grep guard fixture in `tests/conftest.py` (T2.2.2) — `wake_event` fast-wake (≤ 200 ms), I-2 append-only SSE pump, I-4 terminal closes stream within ≤ 250 ms, I-6 drift bound under 2 s poll interval, sole-writer rule fires on a synthetic violator file.
- **Final verification** after the supervisor refactor: `pytest tests/cli -q` 245/245, `pytest tests/cloud -q` 50/50, `pytest tests/daemon/test_sse_poller_coordination.py -q` 6/6, `pytest tests/conftest.py -q` (I-1 guard) 1/1; full default-lane suite green.

## Documentation + Skills

- **`docs/USER_GUIDE.md`** — adds "SSE ingest" subsection (default `attach --follow` SSE behaviour + `--no-stream` escape hatch + the up-to-3 s tolerated divergence note from `state-source-of-truth.md` §2.3) and a "Cloud error hints" subsection citing `RepoAllowlistError` + `CursorCloudPlanRequiredError` bilingual hints verbatim from `422-error-catalog.md` §3.2 (so future drift is caught by a hash diff).
- **`README.md`** — `popola list` example output gains the `runtime` column without breaking the v0.8.5 quickstart steps; status table gains a v0.8.6 row.
- **`src/popolaloom/skills/popola-loom/SKILL.md`** — references the new SSE behaviour in its cloud workflow section so agent CLIs know to mention `--no-stream` when documenting cloud usage.
- **`docs/known-issues.md`** (new) — operator-visible registration of the v0.8.6 cloud-task hydration limitation with symptoms, workaround (`popola attach <task_id>` after restart), design references to `state-source-of-truth.md` §5 / §8, and tracking link to `BL-v0.8.6-1`.

## Files changed (v0.8.6)

| Slice | Files |
|---|---|
| Product | `src/popolaloom/adapters/cursor_cloud.py` (`SSEReader` + `_ERROR_CATALOG` + 10 new exception subclasses; +1141 lines), `src/popolaloom/cli/main.py` (`runtime` column in `list_active` + cloud SSE attach in `attach` / `_attach_streaming` + `--no-stream` flag; +403 lines), `src/popolaloom/daemon/cloud_poller.py` (`wake_event: threading.Event \| None` parameter; +41 lines), `src/popolaloom/daemon/supervisor.py` (I-1-compliant `_spawn_cloud` seeding `cloud_phase` via `TaskHandle` constructor; +29 lines) |
| Tests | `tests/conftest.py` (I-1 sole-writer static-grep guard fixture + `test_invariant_i1_sole_writer_of_cloud_phase`; +148 lines), `tests/cloud/__init__.py` (NEW), `tests/cloud/test_sse_reader.py` (NEW; 17 tests), `tests/cloud/test_422_hints.py` (NEW; 33 tests, parametrised), `tests/cli/test_list_runtime_column.py` (NEW; 8 tests), `tests/cli/test_attach_sse_fallback.py` (NEW; 14 tests), `tests/daemon/test_sse_poller_coordination.py` (NEW; 6 tests) |
| Meta | `docs/known-issues.md` (NEW), `.github/workflows/cloud-smoke.yml` (NEW), `docs/USER_GUIDE.md` (T2.3.2 sibling), `README.md` (T2.3.2 sibling), `src/popolaloom/skills/popola-loom/SKILL.md` (T2.3.2 sibling), `.local/feedbacks/TRACKER.md` (`BL-v0.8.6-1` row), `CHANGELOG.md`, `RELEASE_NOTES.md` |
| Research | `.local/research/v0.8.6_sse/{sse-event-schema.md,state-source-of-truth.md,422-error-catalog.md}` (3 files), `.local/.agent/active/v0.8.6-cloud-sse/{PLAN.md,DECISIONS.md}` (2 files) |

## Verification

- Default lane (`real_cursor_cloud` deselected): `pytest tests/ -m "not slow and not real_graph and not e2e and not nightly and not real_cli and not real_lark and not real_cursor_cloud" -q` → green.
- Per-package smoke runs:
  - `pytest tests/cli -q` → 245 passed
  - `pytest tests/cloud -q` → 50 passed (17 SSE reader + 33 422 hints)
  - `pytest tests/daemon/test_sse_poller_coordination.py -q` → 6 passed
  - `pytest tests/conftest.py -q` (I-1 sole-writer guard) → 1 passed
- Manual cloud-smoke (release engineers only): GitHub Actions UI → `cloud-smoke` workflow → `Run workflow`. Skips silently with `"skipping: CURSOR_API_KEY not set"` log line when the secret is missing; otherwise runs `pytest -m real_cursor_cloud -k "smoke"` against live Cursor REST + SSE.
- Lint / types: `ruff check src/popolaloom tests/` clean; `mypy src/popolaloom` clean.
- Packaging: `python -c "import popolaloom; print(popolaloom.__version__)"` → still `0.8.5` *(version bump deferred to Stage 5 release task per program plan)*; `pytest tests/test_smoke.py -q` clean.

## Status

| Capability | Status |
|---|---|
| Local `--cli=cursor` subprocess path | **unchanged / byte-compatible** |
| `--cli=cursor-cloud` REST poller lifecycle (v0.8.5) | OK live (`v0.8.5+`) |
| `popola attach --follow` SSE ingest for cloud tasks (auto-fallback to poll on 410 / network errors) | OK live (`v0.8.6+`) |
| `--no-stream` poll-only escape hatch on `popola attach` | OK live (`v0.8.6+`) |
| `runtime` column in `popola list` (default-on; `--no-runtime` to hide) | OK live (`v0.8.6+`) |
| 16-entry bilingual error hint catalog + 10 new `CursorCloudError` subclasses | OK live (`v0.8.6+`) |
| `cloud.sse.*` EventLog namespace (coexists with v0.8.5 `cloud.run_status` / `task.*` for one minor cycle) | OK live (`v0.8.6+`) |
| Sole-writer rule (I-1) — CI static-grep guard in `tests/conftest.py` | OK live (`v0.8.6+`) |
| `cloud_poller` `wake_event` for SSE → poller terminal-hint coordination | OK live (`v0.8.6+`) |
| Manual `cloud-smoke` GitHub Actions workflow (`workflow_dispatch`, `CURSOR_API_KEY`-gated) | OK live (`v0.8.6+`) |
| Cloud task hydration after `popolad` restart | **Documented limitation** (`BL-v0.8.6-1` — docs-only registration; shim ≥ v0.8.7) |

## Upgrade notes

1. **No action required** for existing `--cli=cursor-cloud` callers — `popola attach --follow` automatically opens the SSE stream and falls back to the v0.8.5 poller view on `410 stream_expired` / `httpx.ReadError` / `httpx.ConnectError`, so the upgrade is byte-compatible. Pass **`--no-stream`** to force the legacy poll-only path (deterministic for restricted networks / proxies that block long-lived SSE connections).
2. **`popola list` adds a `runtime` column** between `task_id` and `cli`. Pass `--no-runtime` to hide it (escape hatch for narrow terminals or scripts that grep specific column positions); `popola list --json` is unchanged because `runtime` was already in the JSON shape since v0.8.5.
3. **Manual cloud-smoke CI** lives at `.github/workflows/cloud-smoke.yml` and only runs via GitHub Actions UI → `cloud-smoke` → `Run workflow`. Set the `CURSOR_API_KEY` repo secret to enable it; without the secret the job logs a friendly skip line instead of failing red, so it is safe to land on forks.
4. **After `popolad restart`**, cloud tasks need a fresh `popola attach <task_id>` to resume `cloud.run_status` / `cloud.sse.*` event delivery — the `TaskHandle` row + `event_log.jsonl` history survive but the in-memory `CloudPollLoop` thread + SSE `Last-Event-ID` cursor are lost. See [`docs/known-issues.md`](docs/known-issues.md#v086--cloud-task-hydration-after-daemon-restart) and `BL-v0.8.6-1`.
5. Continues from **v0.8.5** Cloud Agent integration — `CURSOR_API_KEY` is still mandatory for cloud workloads (HTTP Basic, key-as-username, empty password); local-only operators (`--cli=cursor`) can ignore the entire cloud surface and see no behaviour change.
6. Continues from **v0.8.4** installer story — `./install.sh` continues to work untouched.

## Known limitations

- **Cloud task hydration after daemon restart** — see [`docs/known-issues.md` §"v0.8.6 — Cloud task hydration after daemon restart"](docs/known-issues.md#v086--cloud-task-hydration-after-daemon-restart). Tracked as `BL-v0.8.6-1` in [`.local/feedbacks/TRACKER.md`](.local/feedbacks/TRACKER.md). Persistent-cursor + SSE `Last-Event-ID` snapshot work deferred to ≥ v0.8.7 per `DECISIONS.md` OQ-7.

## Branch / PR readiness

Suggested release PR title: **`release: v0.8.6 — Cloud observability + SSE ingest (runtime column + 422 hint catalog + manual cloud-smoke CI)`**.

Branch (current spike): `feature/v0.8.6-cloud-sse` — aligns with Protected Branch Workflow (no direct protected-branch pushes; squash-merge into `main` via PR after Stage 5 release task lands the version bump in `pyproject.toml`).
