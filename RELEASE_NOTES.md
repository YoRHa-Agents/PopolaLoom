> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.8.8 — Multi-run + Cost + Quota + Auto Relay

<!-- updated: 2026-05-08 -->

> Released: 2026-05-08

> **⚠️ WARNING — Behavior change: relay defaults to AUTO** (Q-C-4 lock)
>
> v0.8.8 changes `popola relay <task_a>` from "default human-confirm" (the v0.8.7 baseline) to **default auto-dispatch** (Q-C-4 偏离默认 — deliberate deviation from the safer default in `decision-matrices-zh.md` Q-C-4). Operators MUST opt out with `--no-confirm` (refuse) or `--dry-run` (preview-only) when this is undesired.
>
> Five mandatory mitigations enforce this safely. **Read before upgrading** if your team handles cross-org relays:
>
> 1. **Repo allowlist** is **default-empty** (`[cloud.relay] repo_allowlist = []`) — BLOCKS all relays. Set entries in `popolad.toml`, or pass `--confirm-allowlist` to override per-invocation (the override is forensically recorded as `gate_decision="override_confirm_allowlist"`).
> 2. **Audit log** at `.local/.agent/archive/relay/<task_a>.jsonl` (mode `0o600`, parent dir `0o700`) captures every relay attempt — `auto` / `confirmed` / `dry-run` / `rejected_*` / `secret_detected` / `cloud_*_error`. The audit row precedes the cloud `POST` so a crash mid-call leaves a `dispatch_inflight` row.
> 3. **Secret-redaction pre-flight** scans the prompt and envelope for AWS / GitHub PAT / Stripe / JWT / Slack / high-entropy shapes (6 token shapes catalogued; primary `detect-secrets` v1.5.0+, fallback regex with WARN-on-import-fail); a hit rejects the relay with exit 1 and `outcome="secret_detected"`.
> 4. **This RELEASE_NOTES callout** (M4) — top-of-block warning enforced by `tests/docs/test_release_notes_callout.py` (presence, position above first `##` H2, link resolution).
> 5. **CI isolation tests** in `tests/cli/test_relay_safety.py` cover allowlist accept/reject paths, secret rejection (all 6 shapes S1..S6), the audit-row shape (with `0o600` mode assertion), and `--dry-run` isolation (no outbound HTTP, mocked via `respx` in default `pytest -m "not real_cursor_cloud"` lane).
>
> To preserve old behavior: set `[cloud.relay] mode = "confirm"` in `popolad.toml`, OR run `popola relay <task> --dry-run` to preview before each invocation.
>
> Spec: [`relay-auto-safety.md`](.local/research/v0.8.8_multi_run/relay-auto-safety.md) (research note, local-only — `.local/` is gitignored, no public URL is expected).
> Decision: `decision-matrices-zh.md` Q-C-4 — "auto + opt-out (lock)".

## Theme

v0.8.8 ships four layered cloud-runtime improvements on top of v0.8.7's Cloud HITL production tier. **No breaking changes** for existing local `--cli=cursor` callers (the entire v0.8.8 surface is opt-in cloud-runtime); existing `--cli=cursor-cloud` callers see additive event types only, plus the schema extension from the v0.8.6 quintuple to a sextuple `(task_id, run_id, run_index, stream_session_id, sse_id, seq)` (legacy v0.8.6 envelopes treated as `run_index=0`):

1. **Multi-run support** — a single Cursor cloud agent (durable `agent.id`) hosts N sequential follow-up runs via `POST /v1/agents/{id}/runs`; `popola attach --follow` renders chronologically with `[run-N]` prefixes + run-boundary dividers; replay determinism via `(time, run_index, seq)` lex sort; new default-visible `cloud.run_started` / `cloud.run_finished` event brackets; new `popola cloud runs <task>` Q-C-1 偏离默认 subcommand for paginated history.
2. **Cost transparency** — opt-in `popola status --verbose` flag surfaces a curated 5-field cost block (`cost: n/a` honest disclosure + `model` + `mode: max` segment + `wall: NN.Ns` + `link`) per the locked Q-C-2 design. The Cursor Cloud Agents v1 API does NOT document any per-run cost or token usage on the public REST/SSE wire — `cost: n/a` is the only honest value in v0.8.8 (no fabricated numbers).
3. **Quota-aware retry** — new `[cloud.backoff]` config (`max_retries=5`, `base_backoff_ms=500`, `max_backoff_ms=30_000`, `jitter_pct=25`, `honor_retry_after=true`) replaces the v0.8.5–v0.8.7 hard-coded schedule; new `[cloud.busy_strategy]` async-queue (default `mode = "queue"`) handles `409 agent_busy` transparently per Q-C-5; new default-visible `cloud.queued_quota_exceeded` / `cloud.queue_exit` / `cloud.busy_*` events per Q-C-7.
4. **Cross-PR relay** — new `popola relay <task_a>` subcommand turns the output of one cloud run into the input of a brand-new run, defaulting to **auto-dispatch** (Q-C-4 偏离默认; see callout above) on top of 5 mandatory safety mitigations (repo allowlist + audit log + secret pre-flight + RELEASE_NOTES callout + CI isolation tests).

## Highlights

### Multi-run cloud agents

- **`POST /v1/agents/{id}/runs` follow-up dispatch** (`src/popolaloom/adapters/cursor_cloud.py` — `CloudCursorClient.create_followup_run`; ~97 lines) — a single Cursor cloud agent (durable `agent.id`, `bc-*` prefix) now hosts N sequential follow-up runs. Per Cursor's API contract — *"Only one run can be active per agent. Calling this while another run is `CREATING` or `RUNNING` returns `409 agent_busy`. Wait for the existing run to terminate, or cancel it."* — multi-run is strictly sequential; v0.8.8's new `[cloud.busy_strategy] mode = "queue"` (default) handles the conflict transparently rather than failing fast.
- **Sextuple identity** (`adapters/cursor_cloud.py:SSEReader._envelope` extension; `daemon/cloud_poller.py` — `_emit_run_status` + terminal `task.*` paths; `daemon/state.py` — `TaskHandle.cloud_runs[run_id].run_index` field, persisted via ArkTower; ~142 lines `cloud_poller.py`, ~36 lines `state.py`) — every event envelope now carries `data.run_index` so downstream consumers (replay, ArkTower archival, attach renderers) dedup + re-order deterministically. Legacy v0.8.6 envelopes lacking `run_index` are treated as `run_index=0`.
- **Two new event types** (`daemon/cloud_events.py` NEW — `record_run_started` / `record_run_finished` typed wrappers; ~141 lines) — `cloud.run_started` (once per run, at creation; carries `task_id, agent_id, run_id, run_index, started_at, parent_run_id?, prompt_digest?`) and `cloud.run_finished` (once per run, at terminal phase; carries `task_id, agent_id, run_id, run_index, terminal_phase, ended_at, exit_code`). Both are dedup-immune (emitted by popolad code, not synthesised from SSE) and bracket the inner `cloud.sse.*` / `cloud.run_status` stream.
- **`attach --follow` chronological-intermix rendering** (`cli/main.py` extension) — every line is prefixed with `[run-N]`; a single divider `─── follow-up: run-N (parent=run-(N-1)) ───` precedes events whose `run_index` differs from the last-rendered one. Dividers are renderer-only (NOT appended to EventLog); replay reconstructs them from `cloud.run_started` metadata.

### Cost transparency on `popola status --verbose`

- **`popola status --verbose` flag** (`cli/main.py` extension; `daemon/rpc.py` `get_status` response shape extension; `daemon/log_redact.py` NEW — `scrub_cost_fields` deep-copy + key-strip helper; ~67 lines `cli/main.py`, ~108 lines `log_redact.py`) — opt-in `--verbose` flag surfaces a one-line text format `cost: n/a  model: <id|->  [mode: max]  wall: NN.Ns  link: <agent.url>` and a `--json --verbose` block with 10 keys (`cost_estimate_usd: null`, `model_id`, `model_mode`, `tokens_input: null`, `tokens_output: null`, `tokens_total: null`, `wall_clock_s`, `agent_status`, `agent_url`, `doc_anchor`).
- **Honest disclosure: `cost: n/a` is the only value in v0.8.8** — the Cursor Cloud Agents v1 API publishes NO per-run cost or token usage fields on the public REST or SSE wire. Run JSON is just `{id, agentId, status, createdAt, updatedAt}`. The Admin API has hourly `chargedCents` but no documented `runId` join key — heuristic matching of money is unsafe. PopolaLoom v0.8.8 prints `cost: n/a` rather than fabricating a number from token deltas × per-model rate-card.
- **Logging policy enforced** — `scrub_cost_fields` strips `usage` / `tokens_*` / `cacheReadTokens` / `cacheWriteTokens` / `chargedCents` / `totalCents` / `tokenUsage` / `cursorTokenFee` / `spendCents` / `cost_estimate_usd` keys before INFO/WARNING emit; `EventLog.append` calls `os.chmod(path, 0o600)` after rotation/creation; CI lint guard greps `logger.info(.*\busage\b)` + `logger.info(.*\bcost\b)` outside `tests/`.

### Quota-aware retry (`[cloud.backoff]` + `[cloud.busy_strategy]`)

- **`[cloud.backoff]` config schema** (`adapters/cursor_cloud.py` — `_retrying_request` helper wrapping `_request_json`; `daemon/main.py` — `load_popolad_config` extension; ~418 lines `cursor_cloud.py`, ~249 lines `daemon/main.py`) — `max_retries ∈ [0, 20]` (default 5; 0 disables retry), `base_backoff_ms ∈ [50, 60_000]` (default 500), `max_backoff_ms ∈ [base_backoff_ms, 600_000]` (default 30_000), `jitter_pct ∈ [0, 100]` (default 25), `honor_retry_after: bool` (default true). Type-strict (`bool` rejected for any int field; string rejected for any int field); range-strict (no clamping); inter-key invariant `max_backoff_ms ≥ base_backoff_ms`; unknown keys WARN. The `Retry-After` parser handles both delta-seconds integer and HTTP-date forms (per RFC 7231 §7.1.3); garbled headers log a `WARNING` and fall through to the local schedule. With defaults, un-jittered schedule is `500 ms → 1 s → 2 s → 4 s → 8 s → 16 s` (cumulative ≈ 31.5 s); ±25% jitter window `[23.6 s, 39.4 s]` fits inside Cursor's per-minute rate-limit window.
- **`[cloud.busy_strategy]` async-queue** (default `mode = "queue"` per Q-C-5) — on 409 `agent_busy`, daemon enqueues to `PendingDispatchQueue` (FIFO, keyed by `agent_id`); CLI receives `202 + notify_when_ready=true`, exits 0 with stderr `QUEUED: agent=<id> position=<n> deadline=<iso>`; drainer polls `GET /runs/{latest_run_id}` every `queue_poll_interval_s`; on terminal phase pops + re-issues + emits `cloud.busy_dispatched`; `queue_max_wait_s` expiry → `cloud.busy_timeout` + caller exit 75 (NOT 102 — the wait expired, not the agent).
- **Default-visible quota events per Q-C-7** — `cloud.queued_quota_exceeded` (first 429 / quota-class 409 in a backoff sequence; fires **once per backoff sequence**, not per attempt), `cloud.queue_exit` (end of backoff sequence with `outcome ∈ {"success","exhausted","cancelled"}`), `cloud.busy_queued` / `cloud.busy_dispatched` / `cloud.busy_timeout` (queue path) — all surfaced default-visible in both `popola status` (single-line summary) and `popola attach` (inline alongside SSE), NOT filtered by a `--debug` flag.

### Cross-PR relay (`popola relay`)

- **`popola relay <task_a>` subcommand** (`cli/relay_cmd.py` NEW — Typer subcommand with 7 flags `--dry-run` / `--no-confirm` / `--target-repo` / `--confirm-allowlist` / `--message` / `--idempotency-key` / `--json`; `daemon/main.py` — `[cloud.relay]` config; `daemon/rpc.py` — `relay_dispatch` RPC method; ~240 lines `relay_cmd.py`, ~80 lines `daemon/main.py`). The CLI turns the **output** of one terminal cloud run into the **input** of a brand-new cloud run (reads `task_a` via `get_run` / `get_agent`, materialises a follow-up dispatch payload `{prompt, repos[0].url, model, autoCreatePR=False}`, dispatches through the same daemon pipeline as `popola dispatch --cli=cursor-cloud`).
- **Default-auto + Q-C-4 偏离默认 lock** — see callout at top of file. The 5 mandatory mitigations (M1..M5) enforce safety:
  - **M1 — Repo allowlist** (`[cloud.relay] repo_allowlist`, default `[]`). Default-empty list **BLOCKS all relays out-of-the-box**; match is full string equality on canonicalised `<org>/<repo>` (no regex, no glob); override per-invocation with `--confirm-allowlist`.
  - **M2 — Append-only audit log** at `.local/.agent/archive/relay/<task_a_id>.jsonl` (mode `0o600`, parent dir `0o700`; `relay/audit.py` NEW — `RelayAuditWriter` with `os.fsync` + `os.chmod(path, 0o600)` + `os.makedirs(parent, mode=0o700, exist_ok=True)`; ~236 lines). 14 mandatory keys per row; the audit row precedes the cloud `POST`; the prompt body is NEVER stored (only `payload_sha256`).
  - **M3 — Secret-redaction pre-flight scanner** (`relay/secrets.py` NEW — primary `detect-secrets` v1.5.0+, fallback regex; ~532 lines). 6 token shapes — S1 AWS Access Key (`AKIA…` / `ASIA…` / `ABIA…` / `ACCA…`), S2 GitHub PAT (`ghp_` / `github_pat_` / `gho_` / `ghu_` / `ghs_` / `ghr_`), S3 Stripe API Key (`sk_(?:live|test)_` / `rk_(?:live|test)_`), S4 JWT (`eyJ…\.eyJ…\..*{20+}`), S5 Slack Token (`xox[baprs]-…`), S6 generic high-entropy (Shannon ≥ 4.5 bits/char). Hit → exit 1 + `outcome="secret_detected"` + redaction to `…<last4>` (full token NEVER appears anywhere; redaction is fixed-length so an attacker cannot infer the original length from the audit row).
  - **M4 — This RELEASE_NOTES callout** (top of file). `tests/docs/test_release_notes_callout.py` lint enforces presence + position + link resolution.
  - **M5 — CI isolation tests** in `tests/cli/test_relay_safety.py` (default lane; httpx mocked via `respx`; never crosses orgs).
- **`pyproject.toml` extension** — `[project.optional-dependencies] relay-secrets = ["detect-secrets>=1.5.0"]` makes `detect-secrets` an optional extra; minimal install ships without it (the fallback regex catalogue provides a hard floor with a WARN log directing operators to `pip install popolaloom[relay-secrets]` for full coverage — per No Silent Failures, NOT a silent ImportError).
- **Exit codes** (strict subset of existing `cursor_cloud.py` codes, no new codes introduced) — `0` success / `1` policy-denied / `2` invalid-args / `75` cloud-API / `77` cloud-auth / `78` feature-unavailable / `100` not-found / `102` conflict (when `mode = "fail_fast"`).

### `popola cloud runs` — list cloud-agent run history (Q-C-1 偏离默认)

- **`popola cloud runs <task>` subcommand** (`cli/cloud_cmd.py` NEW — Typer sub-app `popola cloud` registered alongside `popolad` / `init` / `skill` / `handoff`; `cli/main.py` — `_register_subcommand_groups` extension; `adapters/cursor_cloud.py` — `CloudCursorClient.list_runs` method + `_request_json` `params` extension; `tests/cli/fixtures/cloud_runs_v1.json` JSON schema fixture; ~892 lines `cloud_cmd.py`). Wraps Cursor's `GET /v1/agents/{id}/runs` REST endpoint directly (bypasses local cache so listings are always a fresh authoritative read).
- **Default 6-column table** (`run_id` truncated 16 chars + `…` / `run_index` derived newest=highest / `state` lowercased / `created_at` verbatim ISO-8601 / `wall_clock` `HH:MM:SS` or `N.Ns` with `…` suffix for live runs / `model` from cached `get_agent`); `--limit > 100` clamped to 100 with stderr WARN; `--cursor` round-trip honored verbatim; `--json` outputs full un-truncated `run_id` per the JSON schema; `--include-events` slow path adds per-row `events_summary` via `GET /runs/{run_id}` (per-row failure → `null` + stderr WARN, per No-Silent-Failures).
- **Two-step call structure** — daemon-bound `GET /status/{task_id}` (UDS) resolves `cursor_agent_id` and validates `runtime=cloud`, then cloud-direct `GET /v1/agents/{id}/runs` (Cursor REST). No caching layer between (1) and (2).
- **Error matrix** — 8 cases (404 → exit 4 per OQ-1 disposition; 401/403 → exit 77 per OQ-2 catalog alignment; 403 plan_required → 78; 429 / 5xx → 75; missing `CURSOR_API_KEY` → 77; daemon-down → 1; local-runtime task → 1; missing task → 4) with bilingual hints from `_ERROR_CATALOG`.

## Tests

- **+~250 new default-lane tests** across the v0.8.8 surface; default lane: **2325+ tests passing**. Per-package smoke breakdown:
  - **22** in `tests/cloud/test_multi_run.py` (T2.1.1; covers I-7..I-12 invariants — per-run seq monotonicity, cross-run lex monotonicity, replay idempotency over `hypothesis` permutations, `cloud.run_started` brackets, `run_index` uniqueness per agent, sequentiality soft-assert).
  - **27** in `tests/cli/test_status_cost.py` + `tests/daemon/test_log_redact.py` (T2.1.2; rendering ON/OFF, JSON schema validation, redaction fuzz, 0o600 mode assertion).
  - **55** in `tests/cloud/test_backoff_config.py` + `tests/daemon/test_config_backoff_loader.py` (T2.1.3; schedule pinning, `Retry-After` parser both forms, jitter ±25%, type-strict config rejections).
  - **≈40** in `tests/cli/test_relay_safety.py` + `tests/daemon/test_config_relay_loader.py` (T2.2.1; the 5 named tests + 6 parametrized M3 cases per `relay-auto-safety.md` §7).
  - **≈40** in `tests/daemon/test_busy_queue.py` + `tests/cli/test_status_busy_visibility.py` (T2.2.2; enqueue/drain/timeout/`mode = "fail_fast"` fallback / Q-C-7 default-visible binding).
  - **47** in `tests/relay/test_audit_writer.py` + `tests/relay/test_secrets_scan.py` (T2.3.3; file mode + parent dir mode + append-only + fsync invariants, 6 token shapes S1..S6, fallback regex path WARN-on-import-fail).
  - **33** in `tests/cli/test_cloud_runs.py` (T2.4.1; help text, default 6-column table, `--limit 200` clamp, `--cursor` round-trip, `--json` schema, error matrix mocked via `respx`, regression on `popola list` / `popola status`).
  - **≥ 2** in `tests/docs/test_release_notes_callout.py` NEW (Q-C-4 M4 lint enforcement).
- **Final verification** (default lane, all `real_*` markers deselected): `pytest -m "not slow and not real_graph and not e2e and not nightly and not real_cli and not real_lark and not real_cursor_cloud and not real_cloud_hitl" -q` → 2325+ passed.

## Documentation

- **`docs/USER_GUIDE.md`** — adds 5 new disjoint sections covering the v0.8.8 surface end-to-end:
  - "Multi-run cloud agents" (≥ 60 lines) — covers `cloud.run_started` / `cloud.run_finished` event taxonomy, `[run-N]` chronological-intermix rendering, replay determinism (I-9 invariant), the run-boundary divider, and lazy reconciliation against manual follow-ups.
  - "Cost transparency — `status --verbose`" (≥ 40 lines) — explains the `cost: n/a` honest disclosure rationale, the 5 documented fields, the `--json --verbose` schema with `doc_anchor`, and the file-permission + log-redaction enforcement.
  - "Cross-PR relay — `popola relay`" (≥ 80 lines) — Q-C-4 deviation prominently called out at the top; lists the 5 mitigations + minimal `[cloud.relay] repo_allowlist` config example + 7-flag synopsis + exit-code matrix.
  - "Quota-aware retry (`[cloud.backoff]` / `[cloud.busy_strategy]`)" (≥ 50 lines) — default schedule, `Retry-After` parser, `409 agent_busy` async-queue UX, validation rules.
  - "`popola cloud runs` — list cloud-agent run history" (≥ 50 lines) — synopsis + 4 options + 6-column table layout + pagination + `--json` schema + error matrix + 2 walkthrough scenarios + a "compared to `popola status --verbose`" comparison table.
- **`README.md`** — adds a NEW "v0.8.8 highlights" block with three bullets (multi-run / cost-verbose / relay-auto-with-mitigations); **quickstart unchanged** (does NOT mention HITL prerequisites — Q-B-2 split-tier maintained).
- **`src/popolaloom/skills/popola-loom/SKILL.md`** — adds Workflow 8 (Cross-PR relay walkthrough showing `--dry-run` → real auto-dispatch → cross-org `--confirm-allowlist` override; cross-links to the 5 mitigation list) and Workflow 9 (`popola cloud runs` example with a full-flow walkthrough: dispatch → wait/attach → `cloud runs` → `--json` scripting → `--include-events` slow path; cross-link to `runs-subcommand-spec.md` for wire-level details). The token-budget cap in `tests/cli/test_skill_md_canonical.py` is bumped 28 000 → 32 000 to accommodate the additions (~ 2 800 chars added; documented in the test docstring history).
- **`docs/known-issues.md`** — unchanged in this release; the v0.8.7 anti-patterns section continues to enforce the Cloud HITL transport story, and CI guard `tests/conftest.py::test_misleading_wording_guard` enforces zero in-tree drift outside the explicit callout.

## Files changed (v0.8.8)

| Slice | Files |
|---|---|
| Product | `src/popolaloom/cli/relay_cmd.py` (NEW), `src/popolaloom/cli/cloud_cmd.py` (NEW), `src/popolaloom/daemon/cloud_events.py` (NEW), `src/popolaloom/daemon/log_redact.py` (NEW), `src/popolaloom/relay/__init__.py` (NEW), `src/popolaloom/relay/audit.py` (NEW), `src/popolaloom/relay/secrets.py` (NEW), `src/popolaloom/adapters/cursor_cloud.py` (`create_followup_run` + `SSEReader._envelope` `run_index` stamp + `_retrying_request` helper + `list_runs` method + `_request_json` `params` extension), `src/popolaloom/daemon/cloud_poller.py` (`_emit_run_status` + terminal `task.*` paths stamp `run_index` + `PendingDispatchQueue` drainer), `src/popolaloom/daemon/state.py` (`TaskHandle.cloud_runs[run_id].run_index`), `src/popolaloom/daemon/main.py` (`[cloud.backoff]` + `[cloud.relay]` + `[cloud.busy_strategy]` config sections), `src/popolaloom/daemon/rpc.py` (`get_status` verbose extension + `relay_dispatch` RPC method), `src/popolaloom/daemon/event_log.py` (typed `record_busy_*` wrappers), `src/popolaloom/cli/main.py` (`status --verbose` + busy-line summary + `_register_subcommand_groups` cloud sub-app), `pyproject.toml` (`relay-secrets` optional extra) |
| Tests | `tests/cloud/test_multi_run.py` (NEW; 22 tests), `tests/cloud/test_backoff_config.py` (NEW), `tests/cli/test_status_cost.py` (NEW), `tests/cli/test_relay_safety.py` (NEW), `tests/cli/test_cloud_runs.py` (NEW; 33 tests), `tests/cli/test_status_busy_visibility.py` (NEW), `tests/cli/fixtures/cloud_runs_v1.json` (NEW), `tests/daemon/test_log_redact.py` (NEW), `tests/daemon/test_config_backoff_loader.py` (NEW), `tests/daemon/test_config_relay_loader.py` (NEW), `tests/daemon/test_busy_queue.py` (NEW), `tests/relay/test_audit_writer.py` (NEW), `tests/relay/test_secrets_scan.py` (NEW), `tests/docs/test_release_notes_callout.py` (NEW), `tests/cli/test_skill_md_canonical.py` (cap bumped 28000 → 32000) |
| Meta | `docs/USER_GUIDE.md` (5 new sections), `README.md` (v0.8.8 highlights block), `src/popolaloom/skills/popola-loom/SKILL.md` (Workflow 8 + Workflow 9), `CHANGELOG.md`, `RELEASE_NOTES.md` (this file — overwritten per v0.7.0+ policy) |
| Research | `.local/research/v0.8.8_multi_run/{event-merge-spec.md, cost-fields.md, relay-primitive.md, relay-auto-safety.md, quota-config.md, runs-subcommand-spec.md}` (6 files), `.local/.agent/active/v0.8.8-multi-run/{PLAN.md, DECISIONS.md}` (2 files) |

## Verification

- Default lane (all `real_*` markers deselected): `pytest -m "not slow and not real_graph and not e2e and not nightly and not real_cli and not real_lark and not real_cursor_cloud and not real_cloud_hitl" -q` → 2325+ passed (green).
- Per-package smoke runs:
  - `pytest tests/cloud/test_multi_run.py -q` → 22 passed (T2.1.1)
  - `pytest tests/cli/test_status_cost.py tests/daemon/test_log_redact.py -q` → 27 passed (T2.1.2)
  - `pytest tests/cloud/test_backoff_config.py tests/daemon/test_config_backoff_loader.py -q` → 55 passed (T2.1.3)
  - `pytest tests/cli/test_relay_safety.py tests/daemon/test_config_relay_loader.py -q` → ≈40 passed (T2.2.1)
  - `pytest tests/daemon/test_busy_queue.py tests/cli/test_status_busy_visibility.py -q` → ≈40 passed (T2.2.2)
  - `pytest tests/relay/test_audit_writer.py tests/relay/test_secrets_scan.py -q` → 47 passed (T2.3.3)
  - `pytest tests/cli/test_cloud_runs.py -q` → 33 passed (T2.4.1)
  - `pytest tests/docs/test_release_notes_callout.py -q` → ≥ 2 passed (M4 lint)
  - `pytest tests/cli/test_skill_md_canonical.py -q` → 6 passed (cap bumped per Workflow 8 + 9 additions)
- Lint / types: `ruff check src/popolaloom tests/` clean.
- Packaging: `python -c "import popolaloom; print(popolaloom.__version__)"` → still `0.8.7` *(version bump deferred to Stage 5 release task per program plan)*; `pytest tests/test_smoke.py -q` clean.

## Status

| Capability | Status |
|---|---|
| Local `--cli=cursor` subprocess path | **unchanged / byte-compatible** |
| `--cli=cursor-cloud` REST poller lifecycle (v0.8.5) | OK live (`v0.8.5+`) |
| `popola attach --follow` SSE ingest for cloud tasks (v0.8.6) | OK live (`v0.8.6+`) |
| **γ — Worker stdio MCP** (Cloud HITL first-class transport, v0.8.7) | OK live (`v0.8.7+`) |
| **β — HTTP MCP backend-proxied** (Cloud HITL backup, v0.8.7) | OK live, `popola doctor --cloud --mode beta` deferred (`BL-v0.8.7-1`) |
| **Multi-run cloud agents** (sextuple identity + `cloud.run_*` brackets) | OK live (`v0.8.8+`) |
| **`popola cloud runs <task>` subcommand** (Q-C-1 偏离默认) | OK live (`v0.8.8+`) |
| **`popola status --verbose` cost surface** (Q-C-2 honest disclosure) | OK live (`v0.8.8+`) |
| **`[cloud.backoff]` 429 retry schedule** (Q-C-3 configurable) | OK live (`v0.8.8+`) |
| **`[cloud.busy_strategy]` async-queue** (Q-C-5 default `mode = "queue"`) | OK live (`v0.8.8+`) |
| **`[cloud.relay]` config + `popola relay` subcommand** (Q-C-4 偏离默认 + 5 mitigations) | OK live (`v0.8.8+`) |
| **Default-visible quota events** (Q-C-7 `cloud.queued_quota_exceeded` + `cloud.busy_*`) | OK live (`v0.8.8+`) |
| **`detect-secrets` optional extra** (`pip install popolaloom[relay-secrets]`) | OK live (`v0.8.8+`); fallback regex catalogue ships in core |

## Upgrade notes

1. **No action required for existing local `--cli=cursor` callers** — the entire v0.8.8 surface is opt-in cloud-runtime; local subprocess dispatch is unchanged.
2. **Existing `--cli=cursor-cloud` callers see additive event types only** — the v0.8.8 sextuple identity extension stamps `data.run_index` on every envelope (legacy v0.8.6 envelopes treated as `run_index=0`); the new `cloud.run_started` / `cloud.run_finished` / `cloud.queued_quota_exceeded` / `cloud.queue_exit` / `cloud.busy_*` events are all default-visible but downstream consumers can ignore them. No schema migration is required for the EventLog NDJSON file.
3. **`popolad.toml` config additions** (all optional — defaults are sensible):

   ```toml
   [cloud.backoff]
   max_retries        = 5
   base_backoff_ms    = 500
   max_backoff_ms     = 30000
   jitter_pct         = 25
   honor_retry_after  = true

   [cloud.busy_strategy]
   mode                  = "queue"
   queue_poll_interval_s = 5
   queue_max_wait_s      = 1800
   notify_on_dispatch    = true

   [cloud.relay]
   mode                  = "auto"     # "auto" (default; Q-C-4 deviation) | "confirm" (restores v0.8.7 human gate)
   repo_allowlist        = []         # MUST be configured before relay is usable in production
   prompt_size_cap_bytes = 16384
   idempotency_window_s  = 3600
   audit_root            = ""         # default ".local/.agent/archive/relay/"
   ```

   Existing `[hitl.cloud]` section continues to work unchanged.
4. **`popola relay` is opt-in but the auto-default is operative once invoked** — see the WARNING callout at the top of this file. Operators wanting the v0.8.7 default flip back globally by setting `[cloud.relay] mode = "confirm"`; per-invocation override with `--no-confirm` re-enables auto on a `mode = "confirm"` deployment. The default `repo_allowlist = []` BLOCKS all relays — operators MUST configure consciously before relay is usable in production.
5. **`detect-secrets` is an optional dependency** — `pip install popolaloom[relay-secrets]` installs `detect-secrets>=1.5.0` for the M3 secret pre-flight scanner. Without it, the fallback regex catalogue (S1..S6) provides a hard floor; the CLI emits a `WARNING` log on import fail directing operators to install the optional extra (per No Silent Failures, NOT a silent ImportError).
6. **Cross-verb exit-code difference for "task not found"** — `popola dispatch --cli=cursor-cloud` retains `cli_exit=100` for `CursorCloudNotFoundError` (v0.8.6 backwards compatibility); `popola cloud runs <task>` 404 → exit 4 (matches local-side "task not found" ergonomics per Q-C-1 OQ-1 resolution). CI scripts that branch on exit code MUST be aware of this cross-verb difference; `case $? in 4) ... ;; esac` matches both `popola dispatch <local-task>` and `popola cloud runs <missing-cloud>`.
7. **Continues from v0.8.7** Cloud HITL — the v0.8.7 `popolaloom_cloud_hitl_request` MCP tool, the γ / β deployment topology, the L3 quarterly Lark webhook secret rotation, and the L6 / L8 / L10 hardening callouts are unchanged. The new `cloud.relay.*` audit events are an additional EventLog namespace and do not interact with the v0.8.7 Cloud HITL invariants.
8. **Continues from v0.8.6** SSE / poller observability — `CloudPollLoop` remains the **sole writer** of `TaskHandle.cloud_phase` (per `state-source-of-truth.md` §1.2 rule 1); SSE remains append-only on `cloud.sse.*`. The new sextuple identity extension stamps `data.run_index` on existing envelope types without changing any other field.

## Known limitations

- **Q-C-4 deviation Stage 5 release-gate enforcement** — `tag v0.8.8 + GitHub Release` does NOT proceed until ALL 7 boxes (C1..C7) in [`relay-auto-safety.md`](.local/research/v0.8.8_multi_run/relay-auto-safety.md) §10 + [`PLAN.md`](.local/.agent/active/v0.8.8-multi-run/PLAN.md) §9 are checked with **0 deferred items**. The 5 mitigations (M1..M5) plus governance (4 sign-off comments — Architect / Security / Release Manager / QA-CI lead) plus SMOKE.md (one end-to-end `mode="auto"` + one end-to-end `mode="confirmed"` with `0o600` mode confirmed via `ls -l`) are all release-gate-blocking. The roadmap's "若选其他" cost was explicitly acknowledged when Q-C-4 was locked; the price for that lock is paid here in full.
- **Custom `detect-secrets` plugins for Cursor / Lark token shapes deferred** — the v0.8.8 catalogue covers 6 well-known shapes (S1 AWS / S2 GitHub PAT / S3 Stripe / S4 JWT / S5 Slack / S6 generic high-entropy); custom plugins for **Cursor API key** and **Lark webhook secret** are tracked as `BL-v0.8.9-1` once Cursor / Lark publish canonical regex ranges. The optional `--allow-secret-shape <name>` escape hatch is available as a per-shape (NOT global) bypass for legitimate test fixtures.
- **Per-task mutex on the audit log writer** — v0.8.8 only handles human-paced relay invocations (one `popola relay` per source task at a time); the audit log writer uses `O_APPEND` so two concurrent invocations on the same source task produce two atomic rows with no file lock. If v0.9 ever schedules relays from a daemon-side task graph, a per-task mutex MUST be added; tracked as `BL-v0.9-1`.
- **Cross-verb exit-code disposition divergence** — `popola cloud runs` 404 exit 4 vs `popola dispatch` 404 exit 100 (per Q-C-1 OQ-1 + DECISIONS.md). CI integrations that share `case $? in` switches across verbs MUST account for this; the divergence is documented in the CHANGELOG `### Changed` block and in this file's Upgrade notes §6.
- **Manual follow-ups bypass popolad's `run_index` counter** — when run history pre-exists popolad's view (e.g., the user manually launched a follow-up via the [Cloud Agents dashboard](https://cursor.com/agents)), the daemon reconciles **only on the missing-`run_index` path** at attach time, calling `GET /v1/agents/{id}/runs?limit=100` once and emitting `cloud.run_index_reconciled` for SRE visibility. The reconcile call rides the `[cloud.backoff]` schedule (T2.1.3). If Stage 4 observes ≥ 1 `cloud.run_index_reconciled` event per minute on any task, the cadence will tighten in v0.8.8.1 patch.
- **No automatic GC for the relay audit log** — `.local/.agent/archive/relay/<task_a>.jsonl` is forever-retention in v0.8.8 (no rotation); manual `rm` only. Forever-retention is the safe default for an audit surface; v0.9 may add a `--prune-older-than 90d` knob (`popola relay audit prune`) — tracked as `BL-v0.8.9-2`.
- **`cloud.run_index_reconciled` rate-limit risk** — if Cursor's per-team rate limit on `GET /runs` proves too tight for the lazy reconciliation path, the cadence will tighten via a per-task LRU of seen `run_id`s in v0.8.8.1 patch. The reconcile call rides the `[cloud.backoff]` schedule, so the worst-case bound is bounded.

## Branch / PR readiness

Suggested release PR title: **`release: v0.8.8 — multi-run + cost-verbose + quota-aware retry + auto-default cross-PR relay (Q-C-1 + Q-C-4 偏离默认 + 5 mitigations + 0 deferred)`**.

Branch (current spike): `feature/v0.8.8-multi-run` — aligns with Protected Branch Workflow (no direct protected-branch pushes; squash-merge into `main` via PR after Stage 5 release task lands the version bump in `pyproject.toml`).

Stage 5 release-gate evidence (per [`relay-auto-safety.md`](.local/research/v0.8.8_multi_run/relay-auto-safety.md) §10): C1 config-loader rejects forbidden values + C2 four named tests green + C3 six parametrized M3 secret-shape tests green + C4 RELEASE_NOTES callout + lint test green + C5 four sign-off comments quoted in this file's "Security sign-off" block (when added at Stage 5) + C6 SMOKE.md trace + C7 Security review of S1..S6 vs `detect-secrets v1.5.x`.

For the full v0.8.8 implementation surface — wave / task table, cross-task invariants I-7..I-12 + M1..M5 audit/no-leak invariants, risk matrix, and the Q-C-4 deviation enforcement protocol — see [`PLAN.md`](.local/.agent/active/v0.8.8-multi-run/PLAN.md). For the v0.8.8 design specs (event merge sextuple + cost catalog + relay primitive + auto-safety mitigations + quota config + runs subcommand) — see [`.local/research/v0.8.8_multi_run/`](.local/research/v0.8.8_multi_run/) (research notes, local-only — `.local/` is gitignored, no public URL is expected).
