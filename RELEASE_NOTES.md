> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.8.5 — Cursor Cloud Agent (Background Agent) via Option α

> Released: 2026-05-08  
> Theme: Ships **--cli=cursor-cloud**, a sibling adapter backed by Cursor’s **Cloud Agents REST API** (`https://api.cursor.com/v1/agents`). Remote runs surface on **https://cursor.com/agents** and **https://cursor.com/dashboard/cloud-agents**. The daemon **does not spawn a subprocess** when the sentinel `CLOUD_BUILD_COMMAND_MARKER` is present; instead it calls `CloudCursorClient` via httpx while a **poll loop** maps Cursor run statuses to EventLog semantics. **Human-in-the-loop** for cloud workloads is routed through **`cloud` HITL channel** bridging + three new authenticated RPC lanes on `popolad`.

## Research + scope rationale

Structured analysis + Option α (**separate `--cli=` instead of merging into local `cursor` argv**) landed in Wave 2 at `.local/research/v0.8.5_cloud_agent/` (**4 artefacts** totaling **551 lines**):

| File | Purpose |
|---|---|
| [`research.md`](.local/research/v0.8.5_cloud_agent/research.md) | Canonical English deliverable (`§6 Option α`; REST semantics; release slice) |
| [`00-decision-matrix-zh.md`](.local/research/v0.8.5_cloud_agent/00-decision-matrix-zh.md) | User-judgment matrix (`§7` locked Q1–Q6 incl. **`smoke_real` via `CURSOR_API_KEY`** for Tier-4+) |
| [`01-external-research.md`](.local/research/v0.8.5_cloud_agent/01-external-research.md) | External API corroboration |
| [`02-integration-analysis.md`](.local/research/v0.8.5_cloud_agent/02-integration-analysis.md) | Daemon + HITL integration notes |

Directive driver: `.local/feedbacks/feedback_for_v0.8.4.md` — explore whether dispatcher can surface workloads as **Cursor-side cloud agents**.

## Highlights

### Stage 1 — Foundation

- **`src/popolaloom/adapters/cursor_cloud.py`**: synchronous `CloudCursorClient` (**HTTP Basic**, API key-as-username password empty); `CursorCloudAdapter.build_command()` returns JSON-bearing sentinel argv; exhaustive error mapping raises `CursorCloudError` subclasses (incl. `CursorCloudAuthError`).
- **`TaskState`** adds **`QUEUED` / `STARTING`** (+ property tests updated); **`TaskHandle`** records `runtime` / **`cursor_*`** ids / `cloud_phase`.
- Dependencies: **`httpx>=0.27`**, dev **`respx>=0.21`**; **`real_cursor_cloud`** pytest marker.

### Stage 2 — Daemon integration

- **`cloud_poller.py`**: backoff + capped poll loop aligning Cursor phases with EventLog semantics.
- **`Supervisor.spawn` marker gate** → **`_spawn_cloud()`** preserves local **`Popen` path** for ordinary argv.
- **Cancel parity**: cloud branch invokes REST cancel semantics (409 best-effort, explicit failure events logged — **No Silent Failures**).

### Stage 3 — HITL bridge

- **`cloud_bridge.py`** + RPC:
  - `POST /hitl/cloud/request`
  - `GET /hitl/cloud/wait/{hitl_id}`
  - `POST /hitl/cloud/answer/{hitl_id}`
- **`HITLChannel` literal** gains **`cloud`** migration (`006_popola_hitl.sql` constraint).
- **`HITLStore` per-connection `RLock`** shields sqlite from concurrent **`asyncio.to_thread`** callers.

### Tests (+97 default lane · 1729 passing)

Foundation **30**, daemon/cloud **47**, HITL bridge **21** — opt-in **`tests/real_cursor_cloud/`** (4 cases, marker **`real_cursor_cloud`**, gated on **`CURSOR_API_KEY`**; **skipped** cleanly when absent).

### Documentation + Skills

README + **`docs/USER_GUIDE.md`** cloud sections; **`popola-loom` SKILL.md Workflow 6**; **`install-popola` SKILL prerequisite** snippet for **`CURSOR_API_KEY`**.

## Files changed (v0.8.5)

| Slice | Files |
|---|---|
| Product | `src/popolaloom/adapters/cursor_cloud.py` (NEW), `src/popolaloom/daemon/cloud_poller.py` (NEW), `daemon/supervisor.py`, `daemon/server.py`, `daemon/state.py`, `daemon/rpc.py`, `hitl/cloud_bridge.py`, `hitl/__init__.py`, `hitl/sync.py`, `migrations/006_popola_hitl.sql` |
| Tests | Multiple new **`tests/`** modules + **`tests/real_cursor_cloud/`** opt-in quartet |
| Meta | `pyproject.toml`, `src/popolaloom/__init__.py`, SKILL frontmatter/version markers (`popola-loom`, `install-popola`), `docs/_config.yml`, `CHANGELOG.md`, `RELEASE_NOTES.md`, `README.md`, `docs/USER_GUIDE.md`, `.local/feedbacks/TRACKER.md` |

## Verification

- Default lane (`real_cursor_cloud` **deselected**): `pytest tests/ -m "not slow and not real_graph and not e2e and not nightly and not real_cli and not real_lark and not real_cursor_cloud" -q`
- Smoke module opt-in semantics:  
  `pytest tests/real_cursor_cloud/ --co -q` → four collected nodes  
  `pytest tests/real_cursor_cloud/ -m real_cursor_cloud -q` (**no key**) → skipped (not failures)
- Packaging: `python -c "import popolaloom; print(popolaloom.__version__)"` → `0.8.5`; `pytest tests/test_smoke.py -q`.
- Lint/types: `ruff check src/popolaloom tests/`; `mypy src/popolaloom`.

## Status

| Capability | Status |
|---|---|
| Local `--cli=cursor` subprocess path | **unchanged / byte-compatible** |
| `--cli=cursor-cloud` (+ `CURSOR_API_KEY`) | OK live (`v0.8.5+`) |
| `popola status` exposes `cursor_agent_id`, `cursor_run_id`, `cloud_phase` | OK live (`v0.8.5+`) |
| Cloud cancel + poller parity | OK live (`v0.8.5+`) |
| `cloud` HITL RPC triad (`/hitl/cloud/*`) | OK live (`v0.8.5+`) |
| 1729 default-lane tests / coverage floor `≥94 %` | OK live (`v0.8.5+`) |

## Upgrade notes

1. **`CURSOR_API_KEY` is mandatory** whenever you intend to schedule **cloud** workloads through PopolaLoom (**HTTP Basic**, key as username, empty password per Cursor docs). Local-only operators can ignore — nothing changes unless you pass **`--cli=cursor-cloud`**.
2. Inspect runs via **https://cursor.com/dashboard/cloud-agents** alongside `popola list` / `popola attach`.
3. For optional **quota-sensitive** smoke proving HTTP auth mapping + basic CRUD, export **`CURSOR_API_KEY`** locally and invoke `pytest tests/real_cursor_cloud/ -m real_cursor_cloud`; otherwise they remain skipped in CI/neutral workstations.
4. Continues from **v0.8.4** installer story — `./install.sh` still works untouched.

## Risks acknowledged (documented upstream + mitigations)

Streaming follow-up runs (**SSE**) + automatic PR creation ergonomics flagged as **stretch / follow-ups** (see **`research.md` §Deferrals** — targeted **v0.8.6+**). Current slice prioritises **lifecycle parity + deterministic cancel semantics + deterministic HITL bridging**.

## Branch / PR readiness

Suggested release PR title: **`release: v0.8.5 — Cursor Cloud Agent integration (+ cloud HITL bridge)`**.

Branch (**current spike**): `feature/v0.8.5-cloud-agent` — aligns with Protected Branch Workflow (no direct protected-branch pushes).

