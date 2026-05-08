> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.8.7 — Cloud HITL Production

<!-- updated: 2026-05-08 -->

> Released: 2026-05-08  
> Theme: v0.8.7 wraps the v0.8.5 `cloud_bridge` REST RPC triad in a single MCP tool — `popolaloom_cloud_hitl_request` — that lets a Cursor Cloud Agent defer a high-stakes decision to a human via Lark, then renders the prompt as a versioned card (`cloud_hitl_request_card_v1`) supporting single-approver, two-approver-serial, and timeout state machines. **γ — Self-Hosted Worker stdio MCP** is the first-class deployment mode (per Q-B-1); **β — HTTP MCP backend-proxied** is the backup for teams without a self-hosted pool. The hard contract is **blocking + 30-min default timeout** (configurable via `popolad.toml [hitl.cloud]`) with explicit `error.code: "timeout"` returns and a full audit chain (`cloud_hitl.{requested,answered,failed,transition}` NDJSON events). **No breaking changes** — existing `--cli=cursor-cloud` callers see no behaviour change unless they explicitly invoke the new MCP tool; idempotency dedup (1 h window + sha256 key) and the mis-route defense at the answer boundary close the v0.8.5 cross-tenant gaps without touching any pre-v0.8.7 code paths.

## Research + scope rationale

Wave 1.1 produced **4 research artefacts** in `.local/research/v0.8.7_hitl/` covering the v0.8.7 design surface end-to-end, then Wave 1.2 synthesised them into a Stage 2 plan + decisions log + security checklist:

| File | Purpose |
|---|---|
| [`long-tool-call-probe.md`](.local/research/v0.8.7_hitl/long-tool-call-probe.md) | Long-tool-call timeout probe protocol; OQ-1 outcome bound for v0.8.7.1 patch if H1 (≤ 30 s hard max) lands |
| [`mcp-tool-contract.md`](.local/research/v0.8.7_hitl/mcp-tool-contract.md) | `popolaloom_cloud_hitl_request` schema + wire mapping + 6 error codes + idempotency design + acceptance checklist |
| [`deployment-modes.md`](.local/research/v0.8.7_hitl/deployment-modes.md) | γ + β topology, prerequisites, install steps, lateral-movement checklist (10 items), minimal-connectivity host list |
| [`lark-card-spec.md`](.local/research/v0.8.7_hitl/lark-card-spec.md) | `cloud_hitl_request_card_v1` template structure, P0 scenarios (S1 / S2 / S3 state machines), versioning policy, security checks |
| [`PLAN.md`](.local/.agent/active/v0.8.7-cloud-hitl-prod/PLAN.md) + [`DECISIONS.md`](.local/.agent/active/v0.8.7-cloud-hitl-prod/DECISIONS.md) + [`SECURITY_CHECKLIST.md`](.local/.agent/active/v0.8.7-cloud-hitl-prod/SECURITY_CHECKLIST.md) | Stage 2 wave / task table; Q-B-1..Q-B-7 locked decisions + L0 resolutions for OQ-1 / OQ-2; release-gate security review (26 line-items across 6 sections) |

Directive driver: v0.8.6 release-time roadmap commitment to ship cloud HITL as production-grade after the v0.8.5 transport correction (research note `.local/research/v0.8.5_cloud_agent/03-cloud-hitl-transport-correction.md`); v0.8.7 closes that follow-up by elevating the v0.8.5 REST bridge to an MCP-tool surface that Cursor Cloud Agents can call directly over the γ (Worker stdio) or β (HTTP backend-proxied) transports.

## Highlights

### Wave 2.1 — foundational (3 parallel slices)

- **`popolaloom_cloud_hitl_request` MCP tool** (`src/popolaloom/mcp/cloud_hitl_tool.py`, NEW; 883 lines incl. tests) — single MCP verb registered in `TOOL_DEFINITIONS` that wraps the v0.8.5 cloud bridge REST triad. Maps `tool_call.input → POST /hitl/cloud/request` per `mcp-tool-contract.md` §6.1 (renames `agent_id → cursor_agent_id`, `run_id → cursor_run_id`, `question_text → prompt_body`); inner long-poll loop wraps `GET /hitl/cloud/wait/{hitl_id}?timeout_s=55` (60-s daemon cap minus 5-s slack) until `total_elapsed ≥ timeout_s`; auto-derives `idempotency_key = sha256(task_id|agent_id|run_id|question_text)[:32]` when caller omits it; returns `CallToolResult(isError=True, content=json(error_envelope))` for all 6 `error.code` values per §3.3. The dashboard-registered command is `popolaloom-mcp` (no extra args; v0.8.7's default entry already wires the cloud HITL bridge); the SECURITY L2 env-allowlist (only `PATH`, `POPOLAD_BASE_URL`, `POPOLAD_API_KEY` reach the MCP child) is **operator-managed via the systemd / launchd unit** that supervises `popolaloom-mcp`, so no shell env / git creds / cloud creds leak into the child.
- **`cloud_hitl_request_card_v1` Lark card renderer** (`src/popolaloom/lark/cloud_hitl_card.py`, NEW; 714 lines incl. tests + security tests) — versioned 4-block card per `lark-card-spec.md` §2.3 (header + B1 verbatim question + B2 truncated context with `[Expand →]` link + B3 metadata footer + A1 action buttons). Reuses `LARK_NOTIFY_PROMPT_TRUNCATE = 200` for B2; B1 (the question) is **never** truncated (questions ≥ 2 000 chars rejected at builder boundary with `ValueError`, per No Silent Failures). `card_metadata` carries the **12 keys per spec §2.4** (the full allowlist `template_version`, `template_id`, `hitl_id`, `task_id`, `cursor_agent_id`, `cursor_run_id`, `idempotency_key`, `expiration_at`, `timeout_seconds`, `responder_policy`, `first_approver_open_id`, `first_approver_at`). State-machine mutators (S1 / S2 / S3) implemented as `mutate_card_for_*` helpers; per OQ-2 in `DECISIONS.md`, mutations use **full-replace via `lark-cli im +update`** for v0.8.7 (latency cost documented; OpenAPI patch deferred to v0.8.8+). Security tests assert `CURSOR_API_KEY` / `LARK_APP_SECRET` are NOT in `json.dumps(card)`.
- **`cloud_bridge` context alignment + idempotency-key persistence** (`src/popolaloom/hitl/cloud_bridge.py` + `src/popolaloom/daemon/rpc.py` surgical patch + `migrations/007_popola_hitl_metadata.sql`) — `submit_request` accepts `idempotency_key: str | None` keyword (auto-derives via sha256 when None); persists into `popola_hitl.metadata` JSON column. Daemon RPC handler queries `metadata->>'idempotency_key' = ? AND created_at > now() - interval '1 hour'` and short-circuits to return the existing row with `deduped: true`. Mis-route defense: `submit_answer` rejects with HTTP 400 when the inbound `hitl_id` does not match the row's stored `(cursor_agent_id, cursor_run_id)` tuple. SQLite is the **single source of dedup truth** — survives `popolad` restarts (per SECURITY R3).

### Wave 2.2 — integration (2 parallel slices)

- **Timeout config + explicit failed-tool returns + audit-log wiring** (`src/popolaloom/daemon/main.py` `[hitl.cloud]` section + `src/popolaloom/hitl/cloud_bridge.py` extension + audit emission) — `popolad.toml` accepts `[hitl.cloud] timeout_seconds = 1800` (default 30 min), `idempotency_window_s = 3600` (1 h), `max_concurrent_per_run = 1`. Loader clamps `timeout_seconds` to `[60, 86400]` and rejects out-of-range values with a clear error (No Silent Failures). Bridge emits `cloud_hitl.requested` (8 keys), `cloud_hitl.answered` (6 keys), `cloud_hitl.failed` (5 keys), `cloud_hitl.transition` (5 keys) NDJSON events per SECURITY §6 — `failed` event lands **before** the MCP tool returns the error envelope (per invariant I-6).
- **Q-B-5 misleading-wording cleanup + CI guard** (`docs/known-issues.md` v0.8.7 anti-patterns section + `tests/conftest.py` session-scope grep guard) — `tests/conftest.py::test_misleading_wording_guard` greps `src/popolaloom/`, `docs/`, `README.md`, and `RELEASE_NOTES.md` for the regex `(?i)(public\s+ip|port[- ]?forward|residential\s+NAT|inbound\s+port|VPN\s+tunnel)` and asserts hits **only** appear in `docs/known-issues.md` (the explicit "do NOT do this" callout). The 80-line `## v0.8.7 — Cloud HITL transport (anti-patterns)` section enumerates the 5 forbidden modes + cites `deployment-modes.md` §1 + §4 row D for the supported alternatives.

### Wave 2.3 — E2E + docs (3 parallel slices)

- **Mock E2E** (`tests/e2e/test_cloud_hitl_mock.py`, NEW) — full happy path (`MCP → bridge → mock Lark notifier → mock human approve → MCP tool returns answer`) using `httpx.MockTransport` for MCP↔popolad and a `_NoopCloudLarkNotifier` for the Lark side; covers timeout, replay-dedup, audit-log assertions across ≥ 4 parametrised cases. Runs in default CI lane (`pytest -m "not real_cloud_hitl and not real_cursor_cloud"`) per Q-B-6.
- **Real E2E with `real_cloud_hitl` marker** (`tests/real_cloud_hitl/test_e2e.py`, NEW + `tests/real_cloud_hitl/conftest.py` env-skip plumbing + `pyproject.toml` marker registration) — opt-in marker for tests requiring `CURSOR_API_KEY` + `LARK_HITL_TARGET_OPEN_ID` + `POPOLAD_BASE_URL`; `pytest -m real_cloud_hitl` collects the test in any environment but skips when env vars are missing. Default `pytest` (no marker) ignores it. Per Q-B-6, runs only in manual or monthly cadence lane.
- **USER_GUIDE / README / SKILL split-tier docs (Q-B-2) + CHANGELOG / RELEASE_NOTES** (this slice) — broad-audience tier (`README.md` quickstart + existing USER_GUIDE Cloud chapter from v0.8.6) does NOT mention HITL prerequisites; new Enterprise sub-page in `docs/USER_GUIDE.md §"Cloud HITL (Enterprise / Self-Hosted)"` documents γ install steps, β fallback, the §6 minimal-connectivity host list, the L3 quarterly rotation runbook, the L6 team-follow-ups callout, the L8 operational hygiene callout, and the L10 network access policy recommendation; `popola-loom` Skill gains Workflow 7 (Cloud HITL γ mode 6-step example).

### Tests

- **+~130 new default-lane tests** across the v0.8.7 surface:
  - **14** in `tests/mcp/test_cloud_hitl_tool.py` (T2.1.1; 883-line product+test slice) — happy path, timeout returns explicit `error.code: "timeout"`, daemon-unreachable, lark-unreachable, replay returns `deduped: true`, invalid_context (empty `question_text`), reject-is-not-an-error (per §7 row 5 — `option_id: "reject"` returns success), idempotency-key opacity, env-allowlist guard.
  - **24 functional + 11 security = 35** in `tests/lark/test_cloud_hitl_card.py` + `tests/lark/test_cloud_hitl_card_security.py` (T2.1.2; 714-line product+test slice) — 3 P0 scenarios (S1 single, S2 serial-two, S3 timeout), `card_metadata` 12-key shape (per spec §2.4 allowlist), B2 truncate to 200 chars, B1 reject-on-overflow, security: `CURSOR_API_KEY` / `LARK_APP_SECRET` not in `json.dumps(card)`, footer-with-origin-note appended.
  - **17** in `tests/hitl/test_cloud_bridge_context.py` + `tests/hitl/test_cloud_bridge_replay.py` (T2.1.3; +`cloud_bridge.py` extension + migration 007) — persist + lookup happy path, replay-within-window short-circuits, replay-after-1h creates new row, restart-then-replay still short-circuits (R3), mis-routed `hitl_id` rejected, missing context → invalid_context.
  - **18 timeout + 17 audit = 35** in `tests/hitl/test_timeout.py` + `tests/hitl/test_cloud_audit.py` (T2.2.1; backfilled with `main.py [hitl.cloud]` config + audit emission) — config load happy, config out-of-range rejected, A1 row keys complete, A2 row keys complete (Lark + API channels), A3 row keys complete for all 6 error_kinds (parameterised), A4 transitions emitted for S1/S2/S3 paths.
  - **1** session-scope conftest guard fixture in `tests/conftest.py` (T2.2.2; paired with the +80-line `docs/known-issues.md` v0.8.7 anti-patterns section) — misleading-wording grep guard per SECURITY M1.
  - **≥4** in `tests/e2e/test_cloud_hitl_mock.py` (T2.3.1) — full mock E2E happy + timeout + replay + audit chain assertions.
  - Real E2E scaffolded in `tests/real_cloud_hitl/test_e2e.py` (T2.3.2; runs only under `pytest -m real_cloud_hitl` with the env vars set).
- **Final verification** (default lane): per-package smoke runs all green; `pytest tests/conftest.py -q` (M1 misleading-wording guard) 1/1.

## Documentation + Skills

- **`docs/USER_GUIDE.md`** — adds NEW sub-page `## Cloud HITL (Enterprise / Self-Hosted)` (~ 280 lines) with: γ + β prerequisites tables; reused-verbatim Mermaid topology diagrams from `deployment-modes.md` §2.2 (γ) and §3.2 (β); 6-step γ install steps + 4-step β install steps; decision matrix (γ vs β vs neither); Egress allowlist host table per §6 (`api2.cursor.sh`, `api2direct.cursor.sh`, `cloud-agent-artifacts.s3.us-east-1.amazonaws.com`, `open.larksuite.com` / `open.feishu.cn`, git host, package registries); L3 quarterly rotation runbook (Q1 1/15, Q2 4/15, Q3 7/15, Q4 10/15); L6 team-follow-ups callout box; L8 operational hygiene "do not commit MCP blob" callout + sample pre-commit hook; L9 worker hardening (`runAsNonRoot: true`); L10 "Allowlist only" Cursor Cloud network access policy recommendation; Approver ACL (P1) + S2 anti-self-approval (P2); Replay safety (R1 / R2); `[hitl.cloud]` config section; tool-call return shape (success + timeout); audit log table (4 event classes × required keys).
- **`README.md`** — Cloud Agent dispatch (v0.8.5+) section gains a single Enterprise/Self-Hosted callout link to the new USER_GUIDE sub-page; quickstart section is unchanged (no HITL prerequisites — covers REST cloud dispatch only per Q-B-2 split-tier).
- **`src/popolaloom/skills/popola-loom/SKILL.md`** — adds Workflow 7 "Cloud HITL approval via MCP tool (γ mode, v0.8.7+)" with the architecture ASCII diagram + 6-step worked example + key safety constraints (L3 / L6 / L8 / L10) + β differences callout + idempotency / dedup behaviour. Quick reference table gains a row for the `popolaloom_cloud_hitl_request` MCP tool.
- **`docs/known-issues.md`** — extended in T2.2.2 with the 80-line `## v0.8.7 — Cloud HITL transport (anti-patterns)` section listing the 5 forbidden modes (public IP, port-forward, residential NAT, inbound port, VPN tunnel) + the supported alternatives (γ outbound-only worker / β backend-proxied gateway) + cross-references to `deployment-modes.md` §1 + §4 row D and `SECURITY_CHECKLIST.md` §8 M1.

## Files changed (v0.8.7)

| Slice | Files |
|---|---|
| Product | `src/popolaloom/mcp/cloud_hitl_tool.py` (NEW; T2.1.1 — 883-line product+test slice), `src/popolaloom/lark/cloud_hitl_card.py` (NEW; T2.1.2 — 714-line product+test slice), `src/popolaloom/hitl/cloud_bridge.py` (T2.1.3 idempotency persist + audit emission + mis-route defense; ~+150 lines), `src/popolaloom/daemon/rpc.py` (T2.1.3 dedup short-circuit; ~+40 lines), `src/popolaloom/daemon/main.py` (T2.2.1 `[hitl.cloud]` config; ~+40 lines), `migrations/007_popola_hitl_metadata.sql` (NEW) |
| Tests | `tests/mcp/test_cloud_hitl_tool.py` (NEW; 14 tests), `tests/lark/test_cloud_hitl_card.py` (NEW; 24 functional tests), `tests/lark/test_cloud_hitl_card_security.py` (NEW; 11 security tests), `tests/hitl/test_cloud_bridge_context.py` (NEW), `tests/hitl/test_cloud_bridge_replay.py` (NEW; 17 tests across the two files), `tests/hitl/test_timeout.py` (NEW; 18 tests), `tests/hitl/test_cloud_audit.py` (NEW; 17 tests), `tests/conftest.py` (T2.2.2 misleading-wording guard fixture; ~+50 lines), `tests/e2e/test_cloud_hitl_mock.py` (NEW; ≥4 parametrised E2E cases), `tests/real_cloud_hitl/test_e2e.py` (NEW; opt-in marker), `tests/real_cloud_hitl/conftest.py` (NEW), `pyproject.toml` (single-line `real_cloud_hitl` marker registration; T2.3.2) |
| Meta | `docs/known-issues.md` (T2.2.2 v0.8.7 anti-patterns section; ~+80 lines), `docs/USER_GUIDE.md` (T2.3.3 NEW Enterprise sub-page), `README.md` (T2.3.3 callout link), `src/popolaloom/skills/popola-loom/SKILL.md` (T2.3.3 Workflow 7), `.github/workflows/cloud-hitl-smoke.yml` (NEW; manual `workflow_dispatch` lane), `CHANGELOG.md`, `RELEASE_NOTES.md` |
| Research | `.local/research/v0.8.7_hitl/{long-tool-call-probe.md, mcp-tool-contract.md, deployment-modes.md, lark-card-spec.md}` (4 files), `.local/.agent/active/v0.8.7-cloud-hitl-prod/{PLAN.md,DECISIONS.md,SECURITY_CHECKLIST.md}` (3 files) |

## Verification

- Default lane (`real_cloud_hitl` + `real_cursor_cloud` deselected): `pytest tests/ -m "not slow and not real_graph and not e2e and not nightly and not real_cli and not real_lark and not real_cursor_cloud and not real_cloud_hitl" -q` → green.
- Per-package smoke runs:
  - `pytest tests/mcp/test_cloud_hitl_tool.py -q` → 14 passed (T2.1.1)
  - `pytest tests/lark/test_cloud_hitl_card.py tests/lark/test_cloud_hitl_card_security.py -q` → 35 passed (T2.1.2; 24 functional + 11 security)
  - `pytest tests/hitl/test_cloud_bridge_context.py tests/hitl/test_cloud_bridge_replay.py -q` → 17 passed (T2.1.3)
  - `pytest tests/hitl/test_timeout.py tests/hitl/test_cloud_audit.py -q` → 35 passed (T2.2.1; 18 timeout + 17 audit)
  - `pytest tests/conftest.py::test_misleading_wording_guard -q` → 1 passed (T2.2.2)
  - `pytest tests/e2e/test_cloud_hitl_mock.py -q` → ≥4 passed (T2.3.1)
- Mock E2E (default lane, no real Cloud Agent):

  ```bash
  pytest tests/e2e/test_cloud_hitl_mock.py -q
  # → ≥4 parametrised cases green: happy + timeout + replay + audit chain
  ```

- Real E2E (manual / monthly, requires `CURSOR_API_KEY` + `LARK_HITL_TARGET_OPEN_ID` + `POPOLAD_BASE_URL`):

  ```bash
  export CURSOR_API_KEY="cr_..."
  export LARK_HITL_TARGET_OPEN_ID="ou_..."
  export POPOLAD_BASE_URL="http://127.0.0.1:9999"
  pytest -m real_cloud_hitl tests/real_cloud_hitl/ -v
  # → manual click-through on Lark card; round-trip asserted
  ```

- Manual cloud-HITL-smoke (release engineers only): GitHub Actions UI → `cloud-hitl-smoke` workflow → `Run workflow`. Skips silently with `"skipping: CURSOR_API_KEY or LARK_HITL_TARGET_OPEN_ID not set"` log line when secrets are missing; otherwise runs `pytest -m real_cloud_hitl -k "e2e"` against live `popolad` + Lark.
- Lint / types: `ruff check src/popolaloom tests/` clean; `mypy src/popolaloom` clean.
- Packaging: `python -c "import popolaloom; print(popolaloom.__version__)"` → still `0.8.6` *(version bump deferred to Stage 5 release task per program plan)*; `pytest tests/test_smoke.py -q` clean.

## Status

| Capability | Status |
|---|---|
| Local `--cli=cursor` subprocess path | **unchanged / byte-compatible** |
| `--cli=cursor-cloud` REST poller lifecycle (v0.8.5) | OK live (`v0.8.5+`) |
| `popola attach --follow` SSE ingest for cloud tasks (v0.8.6) | OK live (`v0.8.6+`) |
| **γ — Worker stdio MCP** (first-class transport for cloud HITL) | OK live (`v0.8.7+`) |
| **β — HTTP MCP backend-proxied** (backup transport for cloud HITL) | OK live, runtime-verification deferred (`v0.8.7+`; `popola doctor --cloud --mode beta` → `BL-v0.8.7-1` for v0.8.8) |
| `popolaloom_cloud_hitl_request` MCP tool (single verb, blocking, 30-min default cap) | OK live (`v0.8.7+`) |
| Lark `cloud_hitl_request_card_v1` (versioned 4-block + S1/S2/S3 state machines) | OK live (`v0.8.7+`) |
| `[hitl.cloud]` config section in `popolad.toml` (timeout_seconds clamp [60, 86400]) | OK live (`v0.8.7+`) |
| Idempotency dedup (sha256 key + 1 h SQLite-backed window) | OK live (`v0.8.7+`) |
| Mis-route defense at `submit_answer` boundary (HITP 400 on cross-run mismatch) | OK live (`v0.8.7+`) |
| Audit chain (`cloud_hitl.{requested,answered,failed,transition}` NDJSON events) | OK live (`v0.8.7+`) |
| Mock E2E in default CI lane (`tests/e2e/test_cloud_hitl_mock.py`) | OK live (`v0.8.7+`) |
| Real E2E behind `real_cloud_hitl` marker (manual / monthly cadence) | OK live (`v0.8.7+`) |
| Manual `cloud-hitl-smoke` GitHub Actions workflow (`workflow_dispatch`, secret-gated) | OK live (`v0.8.7+`) |
| Q-B-5 misleading-wording cleanup (CI guard in `tests/conftest.py`) | OK live (`v0.8.7+`) |

## Upgrade notes

1. **No action required for existing `--cli=cursor-cloud` callers** — the broad-audience cloud REST dispatch path is unchanged from v0.8.6. The new `popolaloom_cloud_hitl_request` MCP tool is opt-in: cloud agents that don't call it see no behaviour change.
2. **`popolad.toml` config additions** (optional — defaults are sensible):

   ```toml
   [hitl.cloud]
   timeout_seconds      = 1800   # default 30 min; clamped to [60, 86400]; out-of-range rejected
   idempotency_window_s = 3600   # 1 h; replays inside the window short-circuit
   max_concurrent_per_run = 1
   ```

   Existing `[hitl]` section continues to work unchanged; `[hitl.cloud]` is a strict superset.
3. **Enterprise / Self-Hosted setup for cloud HITL** — see [`docs/USER_GUIDE.md#cloud-hitl-enterprise--self-hosted`](docs/USER_GUIDE.md#cloud-hitl-enterprise--self-hosted) for the γ install steps (Cursor Self-Hosted Pool worker + `popolad` + `popolaloom-mcp` + dashboard MCP registration), the β fallback, the egress allowlist, the L3 quarterly secret rotation runbook, and the L6 / L8 / L10 hardening callouts. The new sub-page is gated as Enterprise per Q-B-2 (split-tier docs).
4. **β real-traffic verification deferred** — `popola doctor --cloud --mode beta` is referenced in `deployment-modes.md` §3.3 but not yet implemented in v0.8.7. γ ships first-class; β adopters verify out-of-band for v0.8.7 (`BL-v0.8.7-1` for v0.8.8).
5. **Secret hygiene** — every Lark webhook secret rotation is **quarterly** (Q1 1/15, Q2 4/15, Q3 7/15, Q4 10/15) per SECURITY §3 L3. Both γ and β share the same rotation procedure. Treat the MCP config blob (env / headers) as a secret per L8 — do not commit to git or paste into chat. Set Cursor Cloud Agent network access to "Allowlist only" for HITL-handling agents per L10.
6. **Continues from v0.8.6** SSE / poller observability — `CloudPollLoop` remains the sole writer of `TaskHandle.cloud_phase`; SSE is append-only on `cloud.sse.*`. The new `cloud_hitl.*` audit events are an additional EventLog namespace and do not interact with the v0.8.6 invariants.
7. **Continues from v0.8.5** Cloud Agent integration — `CURSOR_API_KEY` is still mandatory for cloud workloads; local-only operators (`--cli=cursor`) can ignore the entire cloud surface and see no behaviour change.

## Known limitations

- **Cloud HITL transport anti-patterns** — see [`docs/known-issues.md` §"v0.8.7 — Cloud HITL transport (anti-patterns)"](docs/known-issues.md#v087--cloud-hitl-transport-anti-patterns). Five configurations (public IP, port-forward, residential NAT, inbound port, VPN tunnel) are explicitly NOT supported for v0.8.7 cloud HITL; the broad-audience `--cli=cursor-cloud` REST path remains fully usable without these — only the human-approval-over-Lark sub-flow has the γ / β prerequisites. CI guard in `tests/conftest.py::test_misleading_wording_guard` enforces zero in-tree drift outside the explicit callout. See also [`.local/research/v0.8.5_cloud_agent/03-cloud-hitl-transport-correction.md`](.local/research/v0.8.5_cloud_agent/03-cloud-hitl-transport-correction.md) for the upstream transport correction context.
- **Long-tool-call probe deferred** (`OQ-1` from T1.1.1) — the maintainer probe requires `CURSOR_API_KEY` + Worker access not present in the agent env. Per `DECISIONS.md` OQ-1, v0.8.7 ships the **blocking + 30-min cap** default and a `v0.8.7.1` patch will fold in the phased fallback if H1 (≤ 30 s hard max) lands later. The MCP tool contract is forward-compatible: phased mode is a non-breaking superset (same input schema; output adds `phase: "queued" | "answered"`).
- **β real-traffic verification deferred** — `popola doctor --cloud --mode beta` is documented in `deployment-modes.md` §3.3 but the doctor command extension is tracked as `BL-v0.8.7-1` for v0.8.8. γ ships first-class so v0.8.7 release is not blocked.
- **`popola doctor --cloud` Lark-secret age warning deferred** (`F1` in `SECURITY_CHECKLIST.md` §11) — the L3 quarterly rotation runbook ships in v0.8.7 (USER_GUIDE Cloud HITL §"Webhook secret rotation"); the in-product `popola doctor --cloud` warning at >100 days lives in v0.8.7.1 patch (or v0.8.8 if it slips). Calendar reminders are the v0.8.7 enforcement mechanism.
- **Mobile rendering of `Custom…` button modal** (`OQ-3` non-blocking) — Lark mobile may surface `open_input` as a bottom sheet rather than a modal; T2.3.2's manual SMOKE.md trace covers iOS + Android click-through (per SECURITY C6). If rendering breaks, downgrade `Custom…` to a "reply-in-thread" prompt in v0.8.7.1.

## Branch / PR readiness

Suggested release PR title: **`release: v0.8.7 — Cloud HITL production (MCP tool + Lark card v1 + γ first-class + 30-min default timeout + audit chain)`**.

Branch (current spike): `feature/v0.8.7-cloud-hitl-prod` — aligns with Protected Branch Workflow (no direct protected-branch pushes; squash-merge into `main` via PR after Stage 5 release task lands the version bump in `pyproject.toml`).
