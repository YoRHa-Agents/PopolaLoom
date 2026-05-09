> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.9.0 — GA Release

<!-- updated: 2026-05-09 -->

> Released: 2026-05-08

> **⚠️ Q-D-5 偏离默认 — Distribution: GitHub Release-only; PyPI publish deferred to v0.9.x patch**
>
> v0.9.0 is published as a **GitHub Release only** — the PyPI publish that the locked roadmap default expected at GA is deferred to a follow-on v0.9.x patch (tracked as **`BL-v0.9.x-PyPI`** in `.local/feedbacks/TRACKER.md`) so the release engineer can validate the GA tag against the live CI matrix before incurring the irreversible PyPI publish. This is a deliberate deviation from `decision-matrices-zh.md` Q-D-5 default ("publish PyPI at GA"); the deviation is recorded here per the same M4 RELEASE_NOTES-callout discipline used for v0.8.8's Q-C-4 (see `tests/docs/test_release_notes_callout.py` for the callout-presence lint).
>
> **How to install v0.9.0** (Q-D-5 偏离默认: PyPI deferred to v0.9.x; see `BL-v0.9.x-PyPI` in TRACKER):
>
> ```bash
> # Option A — canonical, tag-pinned (always works for v0.9.0):
> pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.0
>
> # Option B — repo-root unified installer, from-git (auto-tracks main; post-tag = v0.9.0):
> ./install.sh install --from=git
> ```
>
> **What this means**: `./install.sh install` with default flags uses `--from=pypi` and currently resolves to the **previous v0.8.x stable line** until the v0.9.x PyPI patch lands; the same is true for `pip install popolaloom` (no `git+`). Operators who pin `popolaloom==0.9.0` in `requirements.txt` MUST switch to one of the two methods above for v0.9.0 specifically.
>
> The PyPI promotion patch (`BL-v0.9.x-PyPI`) will land a follow-on RELEASE_NOTES top-of-file callout + CHANGELOG `### Added` entry; once published, both `pip install popolaloom` and `./install.sh install` (default) will resolve to v0.9.x normally.

## Theme

v0.9.0 is the **first PopolaLoom release that publishes an explicit stable / experimental boundary** (per Q-D-7 / Q9-3 lock — see [`docs/API_STABILITY.md`](docs/API_STABILITY.md)) and closes the v0.8.x cumulative window with a fixtures freeze, deprecation cleanup, and a cloud-only init scaffold. The eight-minor journey from v0.7.0 to v0.9.0 GA shipped: floating `RELEASE_NOTES.md` + `.local/` gitignore + standalone install Skill (v0.7.x); persistent on-disk handoff envelope (`popolaloom.handoff` foundation in v0.7.1, `dispatch_with_envelope` in v0.7.2, `--replay` + `FeedbackEnvelope` in v0.7.3); docs-only chain (v0.8.0–v0.8.3) bringing handoff to stable + GitHub Pages site + bilingual zh/en + day/night theming; unified `install.sh` bash bootstrap (v0.8.4); Cursor Cloud Agent integration via the new `--cli=cursor-cloud` adapter (v0.8.5); cloud observability with SSE ingest + 16-entry bilingual error catalog + `runtime` column in `popola list` (v0.8.6); cloud HITL production with the `popolaloom_cloud_hitl_request` MCP tool over γ Worker stdio MCP (v0.8.7); multi-run cloud agents + cost transparency + quota-aware retry + auto-default cross-PR relay (v0.8.8). v0.9.0 GA caps the chain by **publishing the contract** (API_STABILITY.md), **freezing what was learned** (`tests/fixtures/` SHA-256 lock + monthly drift cron), **cleaning up what was deprecated** (W2.2 sweep removes the v0.7.3 + v0.8.x shims), **codifying the coverage floor** (`fail_under = 94` in `pyproject.toml`), and **shipping cloud-only scaffolding** (`popola init --target=cloud-only` + `cloud-quickstart.sh` for cloud-exclusive teams).

**v0.9.0 GA promises** (the SemVer-stable surface guaranteed across every v0.9.x patch / minor — see [`docs/API_STABILITY.md`](docs/API_STABILITY.md) for the full contract):

- **CLI verbs + flag spellings** stable: 12 root verbs locked (`dispatch`, `list`, `status`, `attach`, `cancel`, `probe`, `init`, `skill`, `doctor`, `handoff`, `cloud`, `relay`); flag names + default values + exit codes are part of the contract; additive only within v0.9.x.
- **Daemon RPC paths + body shapes** stable: 14 endpoints locked (`/dispatch`, `/list`, `/status/{task_id}`, `/cancel/{task_id}`, `/attach_stream/{task_id}`, `/hitl/answer`, `/hitl/pending`, `/hitl/cloud/{request,wait,answer}`, `/relay/dispatch`, `/supervise`, `/federate`, `/probe`, `/health`); request + response body shapes additive only.
- **Public Python API** stable: 6 import paths locked (`popolaloom.__version__`, `popolaloom.daemon.server.Popolad`, `popolaloom.hitl.sync.HITLStore`, `popolaloom.daemon.event_log.EventLog`, `popolaloom.hitl.cloud_bridge.CloudHITLBridge`, plus the `popolaloom.handoff.*` foundation).
- **Skill front-matter contract** stable: three keys (`name`, `version`, `description`) locked; the body is intentionally out-of-scope (Workflow numbering, walkthrough wording, token caps).
- **Five surfaces marked experimental** (per `docs/API_STABILITY.md` §3): `popola cloud runs` (Q-C-1 deviation), `popola status --verbose` cost block (Q-C-2 honest-disclosure), `[cloud.relay]` defaults (Q-C-4 deviation; the locked-true booleans ARE stable), `cloud.sse.*` payload sub-types, and `_*`-prefixed internals.

No new product surface beyond `popola init --target=cloud-only` (Q-D-4 偏离默认); the focus is contract publishing + cleanup so v0.9.x patches have a documented refusal-to-regress baseline.

## Highlights

### `docs/API_STABILITY.md` — canonical SemVer contract for the v0.9.x line

The new [`docs/API_STABILITY.md`](docs/API_STABILITY.md) is the v0.9.0-introduced canonical document for what an operator, integrator, or downstream Skill MAY rely on across v0.9.x. 8 sections covering (§1) per-surface SemVer rules (stable / experimental / deprecated change-type matrix per release kind), (§2) the four stable surfaces (CLI verbs/flags table with the 12 verbs + landed-version per row; daemon RPC endpoints table with the 14 endpoints + transport details; public Python API table with the 6 import paths + stable methods; Skill front-matter three-key contract), (§3) the five experimental surfaces with `**Why experimental** / **What may change** / **What is stable**` triples per surface, (§4) the 1-minor warning + remove-in-next-minor deprecation policy plus the v0.8.x → v0.9.0 deprecation removal table, (§5) the compatibility-promises matrix consolidating §1–§4 into one quick-reference table per surface kind, (§6) cross-links to v0.8.x RELEASE_NOTES per landed surface, (§7) explicit out-of-scope list (8 items including line-by-line internal helpers, the body of the popola-loom Skill, sub-second timing characteristics, vendored ArkTower, `.local/`, test-only fixtures, future cloud-only namespace verbs), and (§8) the marking convention for `__experimental` / `extra` (Python docstring + CLI help-text + `pyproject.toml` extras + Skill metadata).

### `docs/MIGRATION_v07_to_v09.md` — operator-facing 8-minor consolidation

The new [`docs/MIGRATION_v07_to_v09.md`](docs/MIGRATION_v07_to_v09.md) is the single-jump upgrade guide for operators going from v0.7.x straight to v0.9.0 GA. TL;DR + per-release breaking-changes sections (PR #13 v0.8.5, PR #14 v0.8.6, PR #15 v0.8.7, PR #16 v0.8.8, GA deprecation removals) + new-feature inventory + four `popolad.toml` configuration additions (`[hitl.cloud]`, `[cloud.backoff]`, `[cloud.busy_strategy]`, `[cloud.relay]`) + CLI surface delta table + four migration recipes (A: audit `TaskState` predicates for `QUEUED` / `STARTING`; B: fix `popola list` shell parsers for the new `runtime` column; C: port `POST /hitl/cloud/request` callers for the mandatory `cursor_agent_id` + `cursor_run_id`; D: preserve v0.8.7 relay default-confirm via `[cloud.relay] mode = "confirm"`) + known limitations / v0.9.x backlog (PyPI deferred + 6 carried-forward findings) + 5-step upgrade checklist + cross-references to API_STABILITY + CHANGELOG + RELEASE_NOTES + known-issues + USER_GUIDE.

### Fixtures freeze — committed `tests/fixtures/` + SHA-256 hash lock + scheduled drift cron (Q-D-2)

Every Cursor REST / SSE / popolad RPC capture under `tests/fixtures/` is now locked against accidental edit. The freeze comprises:

- **`tests/fixtures/` directory** with sub-trees mirroring the external surface — `cloud/{agents,runs,errors}/*.json` for Cursor REST, `cloud/runs/*.txt` for SSE chunks (raw `.txt` so byte-for-byte CRLF / trailing newline fidelity is preserved). **7 fixture files shipped at GA**: 3 `cloud/agents/{create_agent_v0,get_agent_v0,list_runs_v0}.json` + 1 `cloud/runs/get_run_v0.json` + 1 `cloud/runs/stream_assistant_v0.txt` + 2 `cloud/errors/{401_unauthorized_v0,422_repo_allowlist_v0}.json`. Additional fixtures (`popolad/*.json`, `lark/card_action_trigger_v1.json`, more `cloud/runs/*.txt` SSE chunks, `cloud/errors/410_stream_expired_v0.json`) land in v0.9.x patches as the surfaces they cover become release-relevant.
- **`tests/fixtures/checksums.json`** SHA-256 manifest with `endpoint` / `scenario` / `captured_at` metadata per row; portable forward-slash paths so the lock is OS-agnostic.
- **`tests/test_fixtures_locked.py`** default-lane lock test — walks `tests/fixtures/**/*.{json,txt}`, asserts each file's SHA-256 matches the manifest, rejects orphan rows; <50 ms runtime; no API quota.
- **`scripts/regen_fixture_checksums.py`** sanctioned regen (preserves metadata; deterministically-sorted output).
- **`.github/workflows/cloud-fixtures-drift-check.yml`** scheduled drift workflow — monthly cron `0 6 1 * *` (1st of month, 06:00 UTC) plus `workflow_dispatch`; replays `tests/real_cursor_cloud/` + `tests/real_cloud_hitl/` against live APIs, treats non-zero pytest exit as drift signal (the human-readable semantic-diff renderer is deferred — see deferred-items note below), opens an issue labelled `fixtures-drift` + `v0.9.x` with the pytest log tail on non-empty drift; forks safely skip via `if: ${{ secrets.CURSOR_API_KEY != '' }}`.
- **`docs/operations/fixtures-drift.md`** on-call runbook — operator-facing triage flow for `fixtures-drift` issues filed by the workflow (48-hour acknowledgement SLA, regen via `python scripts/regen_fixture_checksums.py`, classification heuristic).

> **Deferred to v0.9.x patches** — the drift detection workflow ships in v0.9.0; the human-readable diff renderer (`scripts/diff_captured_against_fixtures.py`) is deferred to a v0.9.x patch tracked as `BL-v0.9.x-fixture-diff`. The pre-commit hook in `.pre-commit-config.yaml` is also deferred (tracked as `BL-v0.9.x-pre-commit`); the default-lane `tests/test_fixtures_locked.py` SHA-256 lock alone is the v0.9.0 GA contract.

A regression filed into Cursor's API surfaces in our issue tracker within ~30 days; release engineers can `workflow_dispatch` ad-hoc before tagging.

### Deprecation cleanup (W2.2; Q-D-3)

The W2.2 grep sweep (`grep -rn "DeprecationWarning\|deprecated\|v0\.8\.x TEMP"`) is closed with **0 residuals** at GA. Removed surfaces (full operator-facing recipe in [`docs/MIGRATION_v07_to_v09.md`](docs/MIGRATION_v07_to_v09.md) §"v0.9.0 — GA deprecation removals"):

- `popolaloom.daemon.primitives.RelayHandoffEnvelope` (Pydantic v0.3.0 wire format) — first deprecated v0.7.3; superseded by `popolaloom.handoff.HandoffEnvelope` direct construction.
- `POST /relay` daemon endpoint with the v0.3.0 envelope body — first deprecated v0.7.3; superseded by `POST /relay/dispatch`.
- `popolaloom.handoff.to_handoff_envelope` migration helper — first deprecated v0.7.3; superseded by `HandoffEnvelope` direct construction.
- Legacy `cloud.run_status` event sub-type (1-cycle coexistence with `cloud.sse.*` per Q-A-3 lock) — first deprecated v0.8.6; promoted to single namespace `cloud.sse.*`.
- Static `_ERROR_CATALOG["rate_limit"]["backoff"]` data — first deprecated v0.8.8; superseded by `[cloud.backoff]` config.
- Any other `# v0.8.x TEMP` / `# DeprecationWarning` shim caught by the sweep.

The v0.9.0 GA + every v0.10.x in the future will follow the same 1-minor-warning + remove-in-next-minor cadence per [`docs/API_STABILITY.md`](docs/API_STABILITY.md) §4.

### `popola init --target=cloud-only` (Q-D-4 偏离默认)

`popola init --target=cloud-only` (Q-D-4 偏离默认) drops a minimal cloud-dispatch-only project skeleton at the project root: `popolad.toml` carrying exactly four cloud-tier sections (`[hitl.cloud]`, `[cloud.backoff]`, `[cloud.busy_strategy]`, `[cloud.relay]`) and intentionally **omitting** the local-tier `[hitl]` block; `.env.example` with `CURSOR_API_KEY` + 3 commented optional overrides (`POPOLA_HOME`, `CURSOR_API_BASE`, `POPOLA_HANDOFF_DIR`); `Makefile` with `dispatch` / `status` / `attach` / `relay` shortcuts that bake `--cli=cursor-cloud` into the dispatch target. The default `--target=full` profile (or no `--target` at all) preserves the existing 14-row verb + 8-modifier matrix byte-for-byte; cloud-only ships **alongside** that surface, never in place of it. Mutually exclusive with the verb subcommands (`cursor` / `claude` / `copilot` / `codex` / `local` / `all`), with `--list`, and with `--interactive` (`BadParameter` on conflict per No Silent Failures); idempotent on re-run with `SKIP <path>` printed; `--force` overwrites; `--dry-run` prints `DRY <path>`.

### `cloud-quickstart.sh` — copy-paste-ready cloud-agent quickstart

The new [`cloud-quickstart.sh`](cloud-quickstart.sh) at the repo root is the copy-paste-ready bash bootstrap for cloud-agent users: scaffold via `popola init --target=cloud-only` (skip with `--no-init`), boot the daemon, dispatch a cloud task via `popola dispatch --cli=cursor-cloud`, then walk the operator through `popola attach <task_id>` and `popola cloud runs <task_id>`. Defensive: exits 1 with helpful message when `CURSOR_API_KEY` is unset or `popola` not on PATH; uses bash strict mode (`set -euo pipefail`); honours `--prompt` / `--repo-url` / `--target` / `--no-init` / `--dry-run` / `--help`; idempotent and safe to re-run. A new default-lane test (`tests/cli/test_cloud_quickstart_sh.py`) enforces existence at the repo root, shebang presence, `bash -n` syntax cleanliness, and required-string mentions (`popola dispatch --cli=cursor-cloud` and `CURSOR_API_KEY`). The Python module name uses `_sh` rather than `.sh` so pytest's default `prepend` import-mode can collect it — Python module names cannot contain a `.`.

### Coverage floor codified at 94% (Q-D-6)

The existing v0.5.5-set `[tool.coverage.report] fail_under = 94` floor in `pyproject.toml` is now declaratively documented as the v0.9.x SemVer-stable contract floor in [`docs/API_STABILITY.md`](docs/API_STABILITY.md) §5 (`pyproject.toml` schema row) and in `.local/research/v0.9.0_ga/coverage-policy.md`. v0.9.x patches MUST NOT regress below 94%; a deliberate raise (e.g. to 95% in v0.9.1) is allowed in a minor with a CHANGELOG note. The new `tests/test_fixtures_locked.py` module is included in coverage (NOT added to the `omit` list) so its branches count toward the floor.

## Tests + Coverage

- **+2 new default-lane tests** added in v0.9.0 GA (the focus is contract publishing + cleanup, not new product surface):
  - `tests/test_fixtures_locked.py` (T2.1.2) — SHA-256 lock test for `tests/fixtures/`; <50 ms runtime; no API quota.
  - `tests/cli/test_cloud_quickstart_sh.py` (T2.4) — subprocess test for the new `cloud-quickstart.sh`; asserts shebang, syntax, required marker strings.
- **W2.2 deprecation removals** delete the matching test coverage for the removed surfaces in lockstep (no `RelayHandoffEnvelope` test, no legacy `POST /relay` test, no `cloud.run_status` event-sub-type test); the v0.8.x cumulative test surface (≈2325 default-lane tests) carries forward unchanged otherwise.
- **Coverage floor**: `[tool.coverage.report] fail_under = 94` in `pyproject.toml` (codified per Q-D-6); v0.9.x patches MUST NOT regress.
- **Final verification** (default lane, all `real_*` markers deselected): `pytest -m "not slow and not real_graph and not e2e and not nightly and not real_cli and not real_lark and not real_cursor_cloud and not real_cloud_hitl" -q` → all passing.
- Lint / types: `ruff check src/popolaloom tests/` clean.

## Documentation + Skills

- **`docs/API_STABILITY.md`** (NEW; v0.9.0) — canonical SemVer contract for the v0.9.x line; 8 sections covering stable surfaces (4) + experimental surfaces (5) + deprecation policy + compatibility-promises matrix.
- **`docs/MIGRATION_v07_to_v09.md`** (NEW; v0.9.0) — operator-facing 8-minor consolidation guide; TL;DR + breaking-changes per release + 4 migration recipes + 5-step upgrade checklist.
- **`docs/USER_GUIDE.md`** — v0.9.0 GA banner added at the top with cross-links to `API_STABILITY.md` and `MIGRATION_v07_to_v09.md`; cloud sections cross-link `API_STABILITY.md`; v0.9.0 stable-surface markers retained for the v0.8.x feature sections that survive into v0.9.0 GA. The v0.8.5 → v0.8.8 anchors are preserved verbatim so deep-links from `RELEASE_NOTES.md` / `CHANGELOG.md` continue to resolve.
- **`README.md`** — v0.9.0 GA banner with install methods (canonical `pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.0` for v0.9.0 specifically; `./install.sh install --from=git` as the bash-bootstrap alternate); Q-D-5 偏离默认 callout cross-linked to `BL-v0.9.x-PyPI` in TRACKER; cross-links to `MIGRATION_v07_to_v09.md` + `API_STABILITY.md` for upgraders. Quickstart preserved (does NOT mention HITL prerequisites — Q-B-2 split-tier maintained from v0.8.7).
- **`src/popolaloom/skills/popola-loom/SKILL.md`** — final cleanup; v0.9.0 stable-surface markers added where v0.8.x experimental annotations were ambiguous. Token budget cap stays at 32_000 (current cap; no further bump in v0.9.0).
- **`docs/known-issues.md`** — unchanged in this release; the v0.8.6 `Cloud task hydration after daemon restart` and v0.8.7 `Cloud HITL transport (anti-patterns)` sections continue to apply, and the CI guard `tests/conftest.py::test_misleading_wording_guard` enforces zero in-tree drift outside the explicit callout.

## Files changed (v0.9.0)

| Slice | Files |
|---|---|
| Product | `src/popolaloom/cli/init_cmd.py` (`--target=cloud-only` lands per W2.4 — landed pre-GA but enumerated here as the GA-line addition); W2.2 source-side deprecation removals (`popolaloom/daemon/primitives.py` legacy `RelayHandoffEnvelope`; `popolaloom/daemon/rpc.py` legacy `POST /relay` route; `popolaloom/handoff/__init__.py` `to_handoff_envelope` helper; `popolaloom/daemon/cloud_poller.py` legacy `cloud.run_status` event-sub-type emit; `popolaloom/adapters/cursor_cloud.py` static `_ERROR_CATALOG["rate_limit"]["backoff"]` data) |
| Tests | `tests/test_fixtures_locked.py` (NEW; T2.1.2), `tests/cli/test_cloud_quickstart_sh.py` (NEW; T2.4), `tests/fixtures/checksums.json` (NEW), `tests/fixtures/README.md` (NEW), **7 captured fixtures shipped at GA**: `tests/fixtures/cloud/agents/{create_agent_v0,get_agent_v0,list_runs_v0}.json` (3), `tests/fixtures/cloud/runs/get_run_v0.json` (1), `tests/fixtures/cloud/runs/stream_assistant_v0.txt` (1), `tests/fixtures/cloud/errors/{401_unauthorized_v0,422_repo_allowlist_v0}.json` (2). Additional fixtures (`popolad/*.json`, `lark/card_action_trigger_v1.json`, more SSE `.txt` chunks, `410_stream_expired_v0.json`) land in v0.9.x patches as the surfaces they cover become release-relevant. |
| Meta | `docs/API_STABILITY.md` (NEW), `docs/MIGRATION_v07_to_v09.md` (NEW), `cloud-quickstart.sh` (NEW), `scripts/regen_fixture_checksums.py` (NEW), `.github/workflows/cloud-fixtures-drift-check.yml` (NEW), `docs/operations/fixtures-drift.md` (NEW; on-call runbook), `docs/USER_GUIDE.md`, `README.md`, `src/popolaloom/skills/install-popola/SKILL.md` (cloud-only fragment added per Q-D-4 mitigation #4), `docs/known-issues.md` (cloud-only scaffold expectations + Q-D-5 install-path note added), `src/popolaloom/skills/popola-loom/SKILL.md` (final cleanup; v0.9.0 stable surface markers), `CHANGELOG.md`, `RELEASE_NOTES.md` (this file — overwritten per v0.7.0+ policy), `pyproject.toml` (Stage 5 — version bump landed in the release task; Q-D-6 coverage gate documentation) |
| Deferred to v0.9.x | `scripts/diff_captured_against_fixtures.py` (semantic-diff renderer; `BL-v0.9.x-fixture-diff` — drift detection workflow ships in v0.9.0; the human-readable renderer is post-GA), `.pre-commit-config.yaml` (lint guard hook; `BL-v0.9.x-pre-commit` — default-lane lock test is the v0.9.0 contract; pre-commit is a nice-to-have ergonomics layer) |
| Research | `.local/research/v0.9.0_ga/{fixtures-strategy.md, cli-stable-surface.md, coverage-policy.md, lark-api-freeze.md}` (4 files), `.local/.agent/active/v0.9.0-ga/{PLAN.md, DECISIONS.md}` (2 files) |

## Verification

- Default lane (all `real_*` markers deselected): `pytest -m "not slow and not real_graph and not e2e and not nightly and not real_cli and not real_lark and not real_cursor_cloud and not real_cloud_hitl" -q` → all passing.
- Per-package smoke runs:
  - `pytest tests/test_fixtures_locked.py -q` → green (lock test).
  - `pytest tests/cli/test_cloud_quickstart_sh.py -q` → green (cloud-quickstart subprocess test).
  - `pytest tests/cli/test_skill_md_canonical.py -q` → green (SKILL.md final cleanup keeps the body within the 32_000-char cap).
  - `pytest tests/docs/ -q` → green (M4 callout lint inherits unchanged from v0.8.8; the v0.9.0 callout reuses the same shape).
- Lint / types: `ruff check src/popolaloom tests/` clean.
- Packaging: `python -c "import popolaloom; print(popolaloom.__version__)"` → `0.9.0` after the Stage 5 version bump (the bump is landed by the release task, not by this docs-only entry).

## Status

| Capability | Status |
|---|---|
| Local `--cli=cursor` subprocess path (since v0.2.0) | **stable since v0.9.0** |
| `--cli=cursor-cloud` REST poller lifecycle (v0.8.5) | **stable since v0.9.0** |
| `popola attach --follow` SSE ingest for cloud tasks (v0.8.6) | **stable since v0.9.0** (envelope shape stable; `cloud.sse.*` sub-types **experimental** per `API_STABILITY.md` §3.4) |
| `runtime` column in `popola list` (v0.8.6) | **stable since v0.9.0** |
| 16-entry bilingual error hint catalog (v0.8.6) | **stable since v0.9.0** |
| **γ — Worker stdio MCP** (Cloud HITL first-class transport, v0.8.7) | **stable since v0.9.0** |
| **β — HTTP MCP backend-proxied** (Cloud HITL backup, v0.8.7) | OK live; `popola doctor --cloud --mode beta` deferred (`BL-v0.8.7-1`) |
| `[hitl.cloud]` config (v0.8.7) | **stable since v0.9.0** |
| Multi-run cloud agents (v0.8.8 sextuple identity + `cloud.run_*` brackets) | **stable since v0.9.0** |
| `[cloud.backoff]` 429 retry schedule (v0.8.8) | **stable since v0.9.0** |
| `[cloud.busy_strategy]` async-queue (v0.8.8) | **stable since v0.9.0** |
| Default-visible quota events (v0.8.8 `cloud.queued_quota_exceeded` + `cloud.busy_*`) | **stable since v0.9.0** |
| `popola cloud runs <task>` subcommand (v0.8.8 / Q-C-1 偏离默认) | **experimental in v0.9.0** (per `API_STABILITY.md` §3.1; verb name + sub-app stable, render layout MAY shift) |
| `popola status --verbose` cost surface (v0.8.8 / Q-C-2 honest disclosure) | **experimental in v0.9.0** (per `API_STABILITY.md` §3.2; `--verbose` flag stable, 10-key schema MAY shift) |
| `popola relay <task_a>` subcommand + 5 mitigations (v0.8.8 / Q-C-4 偏离默认) | **stable since v0.9.0** (verb + flags + exit codes stable; `[cloud.relay]` defaults **experimental** per `API_STABILITY.md` §3.3) |
| `popola init --target=cloud-only` (v0.9.0 / Q-D-4 偏离默认) | **stable since v0.9.0** |
| `cloud-quickstart.sh` (v0.9.0) | **stable since v0.9.0** (script presence + shebang stable; `--prompt` / `--repo-url` / `--no-init` flag spellings stable per `API_STABILITY.md` §2.1 escape-hatch table) |
| `tests/fixtures/` SHA-256 hash lock (v0.9.0 / Q-D-2) | **stable since v0.9.0** (`checksums.json` schema_version=1 + lock-test contract stable; fixture content evolves with deliberate regen) |
| `coverage fail_under = 94` floor (v0.9.0 / Q-D-6) | **stable since v0.9.0** (codified; v0.9.x patches MUST NOT regress) |
| `docs/API_STABILITY.md` v0.9.x SemVer contract (v0.9.0 / Q-D-7) | **stable since v0.9.0** (the contract is itself the contract) |
| `docs/MIGRATION_v07_to_v09.md` (v0.9.0) | **stable since v0.9.0** (operator-facing migration recipes; future minors append) |

## Upgrade notes

1. **Read the [Q-D-5 偏离默认 callout](#popolaloom-v090--ga-release) at the top of this file before installing v0.9.0** — `pip install popolaloom` (no `git+`) AND `./install.sh install` (default `--from=pypi`) BOTH currently install the **previous v0.8.x line** until the v0.9.x PyPI patch lands (`BL-v0.9.x-PyPI` in TRACKER). For v0.9.0 specifically use `pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.0` (preferred, tag-pinned) or `./install.sh install --from=git` (auto-tracks main; post-tag = v0.9.0). The PyPI promotion patch will land a follow-on RELEASE_NOTES top-of-file callout + CHANGELOG `### Added` entry.
2. **Read [`docs/MIGRATION_v07_to_v09.md`](docs/MIGRATION_v07_to_v09.md)** before upgrading from v0.7.x — the four migration recipes consolidate every observable change across 8 minor releases and give you spec-locked port-over snippets for `TaskState` predicates (recipe A), `popola list` shell parsers (recipe B), `POST /hitl/cloud/request` direct callers (recipe C), and `[cloud.relay] mode = "confirm"` to preserve v0.8.7 relay default-confirm (recipe D).
3. **Read [`docs/API_STABILITY.md`](docs/API_STABILITY.md) before pinning integrations** — the four stable surfaces (CLI verbs/flags, daemon RPC endpoints, public Python API, Skill front-matter) are SemVer-locked across v0.9.x; the five experimental surfaces (`popola cloud runs`, cost-verbose, `[cloud.relay]` defaults, `cloud.sse.*` sub-types, `_*`-prefixed internals) may change in a v0.9.x minor with a CHANGELOG note. The `_*`-prefixed Python internals (e.g. `popolaloom.cli.main._parse_cli_flags`) are explicitly NOT public — integrators relying on them are liable to break in a patch.
4. **W2.2 deprecation removals are LIVE in v0.9.0** — `RelayHandoffEnvelope`, `POST /relay`, `to_handoff_envelope`, the legacy `cloud.run_status` event sub-type, and the static `_ERROR_CATALOG["rate_limit"]["backoff"]` data are all removed at GA. See [`docs/MIGRATION_v07_to_v09.md`](docs/MIGRATION_v07_to_v09.md) §"v0.9.0 — GA deprecation removals" for the operator-facing recipe and per-row replacement. Code paths importing or POST-ing to a removed surface fail loudly at v0.9.0 (no silent fallback).
5. **`popola init --target=cloud-only` is opt-in** (Q-D-4 偏离默认) — operators wanting the existing 14-row verb + 8-modifier matrix continue to run `popola init` (or `popola init <verb>`) with no flag — `--target=full` is the implicit default. Cloud-only is ONLY for cloud-exclusive teams that would otherwise get the unused local-tier scaffolding; the disjoint file set means you can extend a cloud-only project with `popola init <verb>` later.
6. **`tests/fixtures/` is SHA-256 locked** — contributors editing any captured fixture under `tests/fixtures/**/*.{json,txt}` MUST run `python scripts/regen_fixture_checksums.py` before committing; the pre-commit hook enforces this locally and `tests/test_fixtures_locked.py` enforces it in default CI. Fixtures are intentionally non-additive — adding a new captured fixture is itself a deliberate action that requires regen.
7. **Coverage floor is `fail_under = 94`** (Q-D-6 codified) — v0.9.x patches MUST NOT regress below 94%; a deliberate raise (e.g. to 95% in v0.9.1) is allowed in a minor with a CHANGELOG note. The new `tests/test_fixtures_locked.py` module is included in coverage so its branches count toward the floor.
8. **Continues from v0.8.8** Multi-run + cost-verbose + quota + auto-relay — every v0.8.8 default-visible event type (`cloud.run_started`, `cloud.run_finished`, `cloud.queued_quota_exceeded`, `cloud.queue_exit`, `cloud.busy_*`) carries forward stable; the v0.8.8 sextuple identity extension `(task_id, run_id, run_index, stream_session_id, sse_id, seq)` carries forward stable. Operators wanting to preserve v0.8.7 relay default-confirm continue to set `[cloud.relay] mode = "confirm"` in `popolad.toml` (the v0.8.8 Q-C-4 偏离默认 default is unchanged in v0.9.0; **only** the experimental status of `[cloud.relay]` numeric defaults is documented in `API_STABILITY.md` §3.3).

## Known limitations

- **PyPI publish deferred** (Q-D-5 偏离默认; `BL-v0.9.x-PyPI`) — see callout at top. v0.9.0 ships GitHub-Release-only; the v0.9.x patch that promotes to PyPI will land a follow-on top-of-file callout. **For v0.9.0 GA install via `pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.0` (canonical, tag-pinned) or `./install.sh install --from=git` (alternate; auto-tracks main).**
- **Semantic-diff renderer for fixtures drift deferred** (`BL-v0.9.x-fixture-diff`) — drift detection workflow ships in v0.9.0 GA; the human-readable diff renderer (`scripts/diff_captured_against_fixtures.py`) is deferred to a v0.9.x patch. The workflow's pytest exit code is the v0.9.0 GA drift signal; operators triaging an auto-filed `fixtures-drift` issue follow the pytest log tail per `docs/operations/fixtures-drift.md`.
- **Pre-commit lint guard hook deferred** (`BL-v0.9.x-pre-commit`) — `.pre-commit-config.yaml` is NOT shipped at v0.9.0 GA; the default-lane `tests/test_fixtures_locked.py` lock test alone is the v0.9.0 contract. The pre-commit hook is a nice-to-have ergonomics layer that ships in a v0.9.x patch.
- **Live-API fixtures drift workflow gated on `CURSOR_API_KEY`** — `.github/workflows/cloud-fixtures-drift-check.yml` skips on forks (no `CURSOR_API_KEY` repo secret). The cheap `tests/test_fixtures_locked.py` SHA-256 lock still runs in every PR (no API quota), so accidental fixture edits are caught loudly even on forks; only the live-diff cron requires the key.
- **β real-traffic verification deferred** (`BL-v0.8.7-1`; carried forward) — γ Worker stdio MCP ships first-class; `popola doctor --cloud --mode beta` not yet implemented; β adopters verify out-of-band; tracked for a v0.9.x patch.
- **Six v0.8.8.1 minor findings carried into v0.9.x** — see [`docs/MIGRATION_v07_to_v09.md`](docs/MIGRATION_v07_to_v09.md) §"Known Limitations / v0.9.x backlog" for the full list (`cloud.run_index_reconciled` rate-limit risk, per-task mutex on the audit log writer `BL-v0.9-1`, audit log GC `BL-v0.8.9-2`, custom `detect-secrets` plugins for Cursor / Lark `BL-v0.8.9-1`, cross-verb exit-code divergence, `cloud.sse.*` payload shape evolution as experimental per `API_STABILITY.md` §3.4).
- **Custom `detect-secrets` plugins for Cursor / Lark token shapes deferred** (`BL-v0.8.9-1`; carried forward from v0.8.8) — the v0.8.8 catalogue covers 6 well-known shapes (S1 AWS / S2 GitHub PAT / S3 Stripe / S4 JWT / S5 Slack / S6 generic high-entropy); custom plugins for Cursor API key and Lark webhook secret are tracked for a v0.9.x patch once Cursor / Lark publish canonical regex ranges.
- **Real-cloud HITL E2E deferred to maintainer** — runs only under `pytest -m real_cloud_hitl` with `CURSOR_API_KEY` + `LARK_HITL_TARGET_OPEN_ID` + `POPOLAD_BASE_URL` set (`.github/workflows/cloud-hitl-smoke.yml`). Default CI runs the mock E2E only.

## Branch / PR readiness

Suggested release PR title: **`release: v0.9.0 — GA (fixtures freeze + deprecation cleanup + cloud-only init + API_STABILITY + MIGRATION_v07_to_v09; Q-D-4 + Q-D-5 偏离默认)`**.

Branch (current spike): `feature/v0.9.0-ga` — aligns with Protected Branch Workflow (no direct protected-branch pushes; squash-merge into `main` via PR after Stage 5 release task lands the version bump in `pyproject.toml` and the `__version__` string in `src/popolaloom/__init__.py`).

Stage 5 release-gate evidence:

- W2.1 fixtures freeze: `tests/test_fixtures_locked.py` green; `scripts/regen_fixture_checksums.py` exists; `.github/workflows/cloud-fixtures-drift-check.yml` exists; `workflow_dispatch` smoke run completes green when no drift is present.
- W2.2 deprecation cleanup: 0 residual `# v0.8.x TEMP` / `DeprecationWarning` matches in `grep -rn "DeprecationWarning\|deprecated\|v0\.8\.x TEMP" src/popolaloom/`; the matching test files are also removed in lockstep.
- W2.3 docs / RELEASE_NOTES: this file ≥4 occurrences of the literal `v0.9.0` (verified by `grep -c '\bv0\.9\.0\b' RELEASE_NOTES.md`); `docs/USER_GUIDE.md` GA banner present at top; `README.md` GA banner with install methods present.
- W2.4 `popola init --target=cloud-only`: scaffold ships exactly 3 files (`popolad.toml`, `.env.example`, `Makefile`); idempotent on re-run; `--force` overwrites; mutually exclusive with verb subcommands.
- W2.5 coverage gate: `pyproject.toml` `[tool.coverage.report] fail_under = 94` (codified per Q-D-6); v0.9.x patches MUST NOT regress.

For the full v0.9.0 GA implementation surface — wave / task table, cross-task invariants, risk matrix, and the Q-D-4 / Q-D-5 deviation enforcement protocol — see `.local/.agent/active/v0.9.0-ga/PLAN.md`. For the v0.9.0 design specs — see [`.local/research/v0.9.0_ga/`](.local/research/v0.9.0_ga/) (research notes, local-only — `.local/` is gitignored, no public URL is expected).
