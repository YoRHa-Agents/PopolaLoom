---
layout: default
title: Migration Guide v0.7.x to v0.9.0
description: Operator-facing migration guide for PopolaLoom v0.7.x to v0.9.0 GA.
---

# PopolaLoom Migration Guide — v0.7.x → v0.9.0

<!-- updated: 2026-05-10 -->

> **Scope**: every operator-visible change between v0.7.0 and v0.9.0 GA
> — schema shifts, default-flips, new `popolad.toml` sections, CLI
> verbs / flags, and concrete recipes for code that pinned to a v0.7.x
> behaviour.
> **Last updated**: 2026-05-10

This guide is the operator-side companion to
[`docs/API_STABILITY.md`](API_STABILITY.md) (v0.9.x SemVer contract);
consolidates 8 minor releases plus the v0.9.0 GA deprecation cleanup.
Read top-to-bottom for a single-jump v0.7.x → v0.9.0; jump to the
per-version section in [§Breaking Changes](#breaking-changes) when
walking the chain release-by-release.

**Companion**: [`docs/API_STABILITY.md`](API_STABILITY.md) ·
[`CHANGELOG.md`](../CHANGELOG.md) ·
[`RELEASE_NOTES.md`](../RELEASE_NOTES.md) ·
[`docs/known-issues.md`](known-issues.md) ·
[`docs/USER_GUIDE.md`](USER_GUIDE.md).

---

## TL;DR

- **Cloud is now first-class.** v0.8.5 added `--cli=cursor-cloud`
  (REST), v0.8.6 SSE streaming, v0.8.7 the
  `popolaloom_cloud_hitl_request` MCP tool, v0.8.8 multi-run + cost +
  auto-relay. Local `--cli=cursor` callers see no breaking changes.
- **TaskState gained `QUEUED` / `STARTING`** in v0.8.5 (PR #13).
  Audit custom predicates shaped like
  `is_terminal = state in {RUNNING, ...}`
  ([recipe §A](#a-audit-custom-taskstate-predicates-v085)).
- **Three default-flips changed observable behaviour.** v0.8.6
  (PR #14): `popola list` renders `runtime` by default
  (`--no-runtime`) and `popola attach --follow` uses SSE by default
  (`--no-stream`). v0.8.8 (PR #16): `popola relay <task_a>` flipped
  from human-confirm to **auto-dispatch** (Q-C-4 偏离默认; preserve
  old: `[cloud.relay] mode = "confirm"`).
- **HITL via MCP requires Enterprise / γ-mode in production.** v0.8.7
  (PR #15) ships first-class via Self-Hosted Worker stdio MCP; β
  (HTTP MCP backend-proxied) is supported but
  `popola doctor --cloud --mode beta` is deferred to v0.8.7.1.
  Cloud HITL request bodies MUST carry `cursor_agent_id` and
  `cursor_run_id`.
- **v0.9.0 is GitHub Release-only** (Q-D-5 偏离默认; see
  `BL-v0.9.x-PyPI` in `.local/feedbacks/TRACKER.md`). PyPI deferred
  to v0.9.x. **For v0.9.0 specifically install via**
  `pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.0`
  (canonical, tag-pinned) **or** `./install.sh install --from=git`
  (auto-tracks main; post-tag = v0.9.0). The default
  `./install.sh install` and the bare package-name installer path both currently
  resolve to the prior v0.8.x stable line; that surface returns to
  v0.9.x only after the PyPI patch lands.

---

## Breaking Changes

Each row below cites the PR that landed the change (see
[`CHANGELOG.md`](../CHANGELOG.md) for the full per-release entry).

### v0.8.5 — Cursor Cloud Agent integration (PR #13)

- **`TaskState` added two non-terminal states**: `QUEUED` /
  `STARTING`. `TaskHandle` gained `runtime`, `cursor_agent_id`,
  `cursor_run_id`, `cloud_phase`; local `--cli=cursor` callers see
  `runtime="local"` and the cloud fields as `None`.
- **`HITLChannel` literal expanded to `"cloud"`.** Migration
  `006_popola_hitl.sql` widens the `CHECK` constraint; operators on
  pre-v0.8.5 schemas run `popola popolad start` once after upgrade.
- **`CursorCloudError` exception family** is the cloud adapter's new
  error surface; `Popolad.cancel_task` may raise `cloud_cancel_failed`
  / `cloud_cancel_network_error` events.
- **Impact**: low. Breaks only on exhaustive `TaskState` enumeration
  without a default branch
  ([recipe §A](#a-audit-custom-taskstate-predicates-v085)).

### v0.8.6 — Cloud observability + SSE ingest (PR #14)

- **Default-flip 1**: `popola list` renders the `runtime` column
  (`local` / `cloud`) **by default**, between `task_id` and `cli`.
  Opt-out: `--no-runtime` or `--json`.
- **Default-flip 2**: `popola attach --follow` opens an SSE stream
  (`GET /v1/agents/{id}/runs/{run_id}/stream`) **by default** for
  cloud-runtime tasks; auto-falls-back to polling on `410
  stream_expired` / `httpx.ReadError` / `httpx.ConnectError`. Opt-out:
  `--no-stream`.
- **`cloud.sse.*` event namespace** ships in 1-cycle coexistence with
  the legacy `cloud.*` prefix; legacy `cloud.run_status` is
  **removed in v0.9.0**.
- **Impact**: low for `--json` users; medium for shell-parsed table
  ([recipe §B](#b-fix-popola-list-shell-parsers-v086)).

### v0.8.7 — Cloud HITL production (PR #15)

- **HITL via MCP is Enterprise / γ-mode in production.** Recommended
  topology is **Self-Hosted Worker stdio MCP** (γ); β (HTTP MCP
  backend-proxied) supported but `popola doctor --cloud --mode beta`
  is deferred (`BL-v0.8.7-1`). Residential / port-forward / public-IP
  setups are unsupported (see [`docs/known-issues.md`](known-issues.md)).
- **`POST /hitl/cloud/request` MANDATORY fields**: `cursor_agent_id`
  and `cursor_run_id`. The MCP tool derives them; direct REST callers
  MUST populate them. Mis-routed `(hitl_id, cursor_run_id)` answers
  are rejected with HTTP 400.
- **Default 30-min HITL timeout**:
  `[hitl.cloud] timeout_seconds = 1800`, range-clamped to `[60, 86400]`.
- **Impact**: medium. Direct callers missing the two mandatory fields
  fail with `invalid_context`
  ([recipe §C](#c-port-callers-of-posthitlcloudrequest-v087)).

### v0.8.8 — Auto-default cross-PR relay (PR #16, Q-C-4 偏离默认)

> **Behavior change** — `popola relay <task_a>` defaults to
> **auto-dispatch** instead of v0.8.7's "default human-confirm".

- **Default-flip 3**: `popola relay <task_a>` no longer prompts for
  human confirmation (Q-C-4 偏离默认; see top-of-file callout in
  [`RELEASE_NOTES.md`](../RELEASE_NOTES.md)).
- **5 mandatory mitigations**: M1 repo allowlist (default `[]` blocks
  all relays), M2 append-only `0o600` audit log at
  `.local/.agent/archive/relay/<task_a>.jsonl`, M3 `detect-secrets`
  pre-flight (6 shapes S1..S6), M4 RELEASE_NOTES callout, M5 CI
  isolation tests.
- **Preserve v0.8.7 behaviour**: set `[cloud.relay] mode = "confirm"`
  ([recipe §D](#d-preserve-v087-relay-behaviour-v088)).
- **Impact**: HIGH for deployments wanting the v0.8.7 default. Read
  the [`RELEASE_NOTES.md`](../RELEASE_NOTES.md) callout before tagging.

### v0.9.0 — GA deprecation removals (PR pending)

The v0.9.0 GA closes Wave 2.2 (deprecation 清理). Surfaces
deprecated during v0.7.3 → v0.8.8 are **removed**:

| # | Removed surface | First deprecated | Replacement |
| --- | --- | --- | --- |
| 1 | `popolaloom.daemon.primitives.RelayHandoffEnvelope` | v0.7.3 | `popolaloom.handoff.HandoffEnvelope` |
| 2 | `POST /relay` (v0.3.0 envelope body) | v0.7.3 | `POST /relay/dispatch` ([API_STABILITY §2.2](API_STABILITY.md#22-daemon-rpc-endpoints)) |
| 3 | `popolaloom.handoff.to_handoff_envelope` | v0.7.3 | `HandoffEnvelope` direct construction |
| 4 | Legacy `cloud.run_status` event sub-type | v0.8.6 (Q-A-3) | `cloud.sse.*` namespace |
| 5 | Static `_ERROR_CATALOG["rate_limit"]["backoff"]` data | v0.8.8 | `[cloud.backoff]` ([§Configuration Additions](#configuration-additions)) |
| 6 | Any other `# v0.8.x TEMP` / `# DeprecationWarning` shim (W2.2 grep sweep) | v0.8.x | release-gate AC: **0 residuals** |

```python
# v0.9.0: ImportError on the removed import
from popolaloom.daemon.primitives import RelayHandoffEnvelope  # 🔴
# Replacement (stable since v0.7.3):
from popolaloom.handoff import HandoffEnvelope
```

---

## New Features (v0.7.x → v0.9.0)

Chronological list; each row anchors to its CHANGELOG entry.

| Version | Feature | PR / merge |
| --- | --- | --- |
| v0.7.0 | `install-popola` standalone Skill; floating `RELEASE_NOTES.md`; `.local/` gitignored. | [§0.7.0](../CHANGELOG.md) |
| v0.7.1 | `popolaloom.handoff` foundation — `HandoffEnvelope`, `generate_handoff_id`, `write_envelope`, `archive_envelope`; 3 bug fixes. | [§0.7.1](../CHANGELOG.md) |
| v0.7.2 | `Popolad.dispatch_with_envelope` canonical entry; `popola handoff list/show/archive` CLI; dual-channel injection. | [§0.7.2](../CHANGELOG.md) |
| v0.7.3 | `popola dispatch --replay <handoff_id>`; `FeedbackEnvelope`; `to_handoff_envelope` bridge for legacy relay. | [§0.7.3](../CHANGELOG.md) |
| v0.8.0–v0.8.3 | Docs-only chain: handoff envelope stable promotion; NieR-Popola GitHub Pages site (bilingual zh/en + day/night); UX polish; i18n. | [§0.8.0–0.8.3](../CHANGELOG.md) (PR #9) |
| v0.8.x | `install.sh` unified bash installer; `popola skill uninstall` Typer verb. | [§0.8.4](../CHANGELOG.md) |
| v0.8.5 | `--cli=cursor-cloud` adapter; `TaskState.QUEUED` / `STARTING`; cloud HITL bridge (`POST /hitl/cloud/{request,wait,answer}`). | [§0.8.5](../CHANGELOG.md) (PR #13) |
| v0.8.6 | SSE ingest (`cloud.sse.*`); `runtime` column; 16-entry bilingual error hint catalog; `--no-stream` escape hatch; manual `cloud-smoke` CI. | [§0.8.6](../CHANGELOG.md) (PR #14) |
| v0.8.7 | `popolaloom_cloud_hitl_request` MCP tool; `cloud_hitl_request_card_v1` Lark card; `[hitl.cloud]` config; idempotency dedup; mis-route defense. | [§0.8.7](../CHANGELOG.md) (PR #15) |
| v0.8.8 | Multi-run (sextuple identity); `popola status --verbose` cost; `[cloud.backoff]` + `[cloud.busy_strategy]`; `popola relay` (auto-default + 5 mitigations); `popola cloud runs`. | [§0.8.8](../CHANGELOG.md) (PR #16) |
| v0.9.0 | API stability boundary; fixtures freeze + hash lock + scheduled monthly check; coverage ≥94% in `pyproject.toml`; `popola init --target=cloud-only`; deprecation removal sweep (W2.2). | [§Unreleased](../CHANGELOG.md) (release PR pending) |

---

## Configuration Additions

All sections are **opt-in** with sensible defaults; existing
`popolad.toml` files continue to work unchanged.

### `[hitl.cloud]` — v0.8.7 (PR #15)

```toml
[hitl.cloud]
timeout_seconds        = 1800  # default 30 min; range [60, 86400]
idempotency_window_s   = 3600  # default 1 h; dedup window
max_concurrent_per_run = 1
```

Out-of-range values rejected (No Silent Failures).

### `[cloud.backoff]` — v0.8.8 (PR #16)

```toml
[cloud.backoff]
max_retries        = 5      # range [0, 20]; 0 disables retry
base_backoff_ms    = 500    # range [50, 60000]
max_backoff_ms     = 30000  # range [base_backoff_ms, 600000]
jitter_pct         = 25     # range [0, 100]
honor_retry_after  = true
```

Type-strict / range-strict; inter-key invariant
`max_backoff_ms ≥ base_backoff_ms`. Replaces the v0.8.5–v0.8.7
hard-coded schedule.

### `[cloud.busy_strategy]` — v0.8.8 (PR #16)

```toml
[cloud.busy_strategy]
mode                  = "queue"   # "queue" (default) | "fail_fast" (v0.8.7)
queue_poll_interval_s = 5         # range [1, 60]
queue_max_wait_s      = 1800      # range [60, 86400]; 0 disables
notify_on_dispatch    = true
```

On `409 agent_busy` + `mode = "queue"`, the daemon enqueues (FIFO,
keyed by `agent_id`); the CLI exits 0 with stderr
`QUEUED: agent=<id> position=<n> deadline=<iso>`.

### `[cloud.relay]` — v0.8.8 (PR #16, Q-C-4 偏离默认)

```toml
[cloud.relay]
mode                  = "auto"   # "auto" (Q-C-4 偏离默认) | "confirm" (v0.8.7)
repo_allowlist        = []       # default-empty BLOCKS all relays
prompt_size_cap_bytes = 16384    # range [1024, 1048576]
idempotency_window_s  = 3600     # range [60, 86400]
audit_root            = ""       # default ".local/.agent/archive/relay/"
```

Three loader-locked booleans cannot be `false`:
`require_confirm_allowlist_flag`, `secret_scan_enabled`,
`dry_run_emits_audit`. Audit-row schema documented in
`relay-auto-safety.md` §M2.

---

## CLI Surface Changes

| Version | Change | Type |
| --- | --- | --- |
| v0.8.x | New: `popola skill uninstall --target=<...>` Typer verb. | additive |
| v0.8.5 | New: `--cli=cursor-cloud` value for `popola dispatch --cli`. | additive |
| v0.8.6 | New: `popola list --no-runtime` (opt-out of default-on column). | escape hatch |
| v0.8.6 | New: `popola attach --no-stream` (opt-out of default-on SSE). | escape hatch |
| v0.8.8 | New: `popola status --verbose` flag (cost surface). | additive |
| v0.8.8 | New: `popola relay <task_a>` subcommand (7 flags: `--dry-run`, `--no-confirm`, `--target-repo`, `--confirm-allowlist`, `--message`, `--idempotency-key`, `--json`). | additive (default-flip) |
| v0.8.8 | New: `popola cloud` sub-app with `runs` verb (Q-C-1; experimental in v0.9.0 per [API_STABILITY §3.1](API_STABILITY.md#31-popola-cloud-runs-q-c-1)). | additive |
| v0.9.0 | New: `popola init --target=cloud-only` (Q-D-4). | additive |

All other `popola` verb names and flag spellings are **stable** under
the v0.9.x SemVer contract
([API_STABILITY §2.1](API_STABILITY.md#21-cli-commands-and-flags)).

---

## Migration Recipes

### A. Audit custom `TaskState` predicates (v0.8.5)

Pre-v0.8.5 code that exhaustively enumerated `TaskState` breaks when
it encounters `QUEUED` / `STARTING`. Add them to the non-terminal
branch:

```python
from popolaloom.daemon.state import TaskState

NON_TERMINAL_STATES = {
    TaskState.SUBMITTED,
    TaskState.RUNNING,
    TaskState.QUEUED,    # NEW in v0.8.5
    TaskState.STARTING,  # NEW in v0.8.5
}
```

Default-branch (`else: ...`) callers need no change.

### B. Fix `popola list` shell parsers (v0.8.6)

The new `runtime` column shifts every other column right by one
position. Two safe paths:

```bash
# Path 1 — keep v0.8.5 column layout
popola list --no-runtime | awk '{ print $1 }'  # task_id at column 1

# Path 2 — switch to --json (preferred; stable since v0.8.5)
popola list --json | jq -r '.[].task_id'
```

`--json` carries `runtime` since v0.8.5, so Path 2 also works on
pre-v0.8.6 daemons.

### C. Port callers of `POST /hitl/cloud/request` (v0.8.7)

The MCP tool `popolaloom_cloud_hitl_request` derives `cursor_agent_id`
and `cursor_run_id` from its tool-call context; direct REST callers
MUST populate them or the daemon returns `invalid_context`:

```python
import httpx

response = httpx.post(
    "http+unix://%2Fhome%2Fuser%2F.popola%2Fpopolad.sock/hitl/cloud/request",
    json={
        "task_id":         "cursor-fix-bug-3a7f9c1d",
        "cursor_agent_id": "bc-01h9k4...",   # MANDATORY in v0.8.7+
        "cursor_run_id":   "rn-01h9k4...",   # MANDATORY in v0.8.7+
        "prompt_body":     "Approve deploy?",
        "timeout_s":       1800,             # optional; from [hitl.cloud]
        "idempotency_key": None,             # optional; auto-derived
    },
    timeout=60.0,
)
response.raise_for_status()
hitl_id = response.json()["hitl_id"]
```

Mis-routed `(hitl_id, cursor_run_id)` answers are rejected with
HTTP 400. Production code SHOULD use the MCP tool path.

### D. Preserve v0.8.7 relay behaviour (v0.8.8)

Restore the v0.8.7 "default human-confirm" UX globally:

```toml
# popolad.toml — flip back to v0.8.7 default
[cloud.relay]
mode = "confirm"
```

Per-invocation preview (zero outbound HTTP, audit row
`outcome="dry-run"`):

```bash
popola relay cursor-fix-bug-3a7f9c1d --dry-run
```

> Note: on `mode = "auto"` (v0.8.8 default) no per-invocation flag
> forces human confirmation — the config flip is canonical.
> `--no-confirm` re-enables auto on a `mode = "confirm"` deployment
> (asymmetric by design).

---

## Known Limitations / v0.9.x backlog

- **PyPI publish deferred** (Q-D-5 偏离默认; `BL-v0.9.x-PyPI` in
  TRACKER) — v0.9.0 is GitHub-Release-only. For v0.9.0 install via
  `pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.0`
  (canonical) or `./install.sh install --from=git` (alternate). The
  default `./install.sh install` and bare package-name installer paths
  currently resolve to the prior v0.8.x stable line.
- **β real-traffic verification deferred** (`BL-v0.8.7-1`) — γ Worker
  stdio MCP ships first-class; `popola doctor --cloud --mode beta`
  not yet implemented.
- **6 v0.8.8.1 minor findings** carried into v0.9.x:
  1. `cloud.run_index_reconciled` rate-limit risk on the lazy
     reconciliation path.
  2. Per-task mutex on the audit log writer (`BL-v0.9-1`).
  3. Audit log GC (`BL-v0.8.9-2`) — forever-retention today.
  4. Custom `detect-secrets` plugins for Cursor API key / Lark
     webhook secret (`BL-v0.8.9-1`).
  5. Cross-verb exit-code divergence — `popola cloud runs` 404 → 4
     vs `popola dispatch` 404 → 100.
  6. `cloud.sse.*` payload shape evolution — **experimental** per
     [API_STABILITY §3.4](API_STABILITY.md#34-sse-event-sub-types-cloudsse).
- **Real-cloud HITL E2E deferred to maintainer** — runs only under
  `pytest -m real_cloud_hitl` with `CURSOR_API_KEY` +
  `LARK_HITL_TARGET_OPEN_ID` + `POPOLAD_BASE_URL` set
  (`.github/workflows/cloud-hitl-smoke.yml`). Default CI runs the
  mock E2E only.

---

## Upgrade Checklist

Five steps to walk a v0.7.x deployment up to v0.9.0 GA.

1. **Audit custom `TaskState` predicates** — add `QUEUED` /
   `STARTING` to any not-yet-running branch
   ([recipe §A](#a-audit-custom-taskstate-predicates-v085)).
2. **Update `popola list` parsers** — switch to `--no-runtime` or
   `--json` ([recipe §B](#b-fix-popola-list-shell-parsers-v086)).
3. **Add cloud HITL request fields** — populate `cursor_agent_id` +
   `cursor_run_id` in any direct `POST /hitl/cloud/request` caller
   ([recipe §C](#c-port-callers-of-posthitlcloudrequest-v087)).
4. **Decide on relay default** — read the
   [`RELEASE_NOTES.md`](../RELEASE_NOTES.md) callout; set
   `[cloud.relay] mode = "confirm"` to preserve v0.8.7, or configure
   `repo_allowlist` consciously (default `[]` BLOCKS all relays —
   [recipe §D](#d-preserve-v087-relay-behaviour-v088)).
5. **Replace removed v0.8.x deprecation shims** — purge
   `RelayHandoffEnvelope`, `to_handoff_envelope`, `POST /relay`
   legacy, and `cloud.run_status` event sub-type before running
   v0.9.0
   ([§v0.9.0 deprecation removals](#v090--ga-deprecation-removals-pr-pending)).

---

## Cross-references

- [`docs/API_STABILITY.md`](API_STABILITY.md) — v0.9.x SemVer contract.
- [`CHANGELOG.md`](../CHANGELOG.md) — full historical archive.
- [`RELEASE_NOTES.md`](../RELEASE_NOTES.md) — latest release only
  (currently v0.8.8 with the Q-C-4 callout).
- [`docs/known-issues.md`](known-issues.md) — operator-visible limits.
- [`docs/USER_GUIDE.md`](USER_GUIDE.md) — walkthrough through v0.8.8.

<!-- updated: 2026-05-10 -->

