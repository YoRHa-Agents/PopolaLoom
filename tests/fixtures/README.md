# `tests/fixtures/` — captured external API contracts

> **Stage** v0.9.0 GA — Stage 2 Wave 2.1 (T2.1.1 / T2.1.2).
> **Spec**  `.local/research/v0.9.0_ga/fixtures-strategy.md`
> **Lock**  `tests/fixtures/checksums.json` + `tests/test_fixtures_locked.py`
> **Drift** `.github/workflows/cloud-fixtures-drift-check.yml` (monthly + dispatch)

This directory is the single source of truth for **what a downstream
external API looks like to PopolaLoom**. Every committed file is a
captured response shape (JSON or SSE chunk), versioned by suffix
(`_vN`) per `fixtures-strategy.md` §3, and locked by SHA-256 in
`checksums.json`. The lock test in
`tests/test_fixtures_locked.py::test_fixtures_match_checksums` walks
this tree on every PR and refuses to merge a fixture mutation that
forgets to re-run `python scripts/regen_fixture_checksums.py`.

The contents are intentionally **fixture-only seeds** in v0.9.0 W2.1
(not yet replaced by live captures); v0.9.x patches will refresh
each file from a real `CURSOR_API_KEY` capture run via the
`cloud-fixtures-drift-check` workflow's `workflow_dispatch` lever.

## Conventions (excerpts from `fixtures-strategy.md` §3)

- One JSON object per file. No top-level arrays.
- Versioned suffix `_vN` bumped only on a schema-incompatible change.
- SSE chunks live as `.txt` (CRLF / blank-line framing is byte-significant).
- Comments live in `__comment` / `__captured_at` / `__source` keys
  (loaders strip any `__`-prefixed key before passing the body downstream).
- Error fixtures lead with the HTTP status (`401_`, `422_`, ...).

## Excluded from the lock walker

`tests/test_fixtures_locked.py` walks `**/*.json` and `**/*.txt` under
this directory, **excluding** the index files and pre-existing test
helpers per `fixtures-strategy.md` §4.2:

- `checksums.json` (it is the lock manifest itself)
- `README.md` (this file — markdown, not walked anyway)
- `__init__.py`, `real_popolad.py` (test-helper Python; not data captures)
- everything under `mock_cli/` (the `cursor`/`claude`/`codex` test doubles)

To extend the lock walker scope when authoring a new fixture sub-tree,
add it under `tests/fixtures/<surface>/` with `_vN.{json,txt}`
extensions and re-run `python scripts/regen_fixture_checksums.py`.

## Registry

Tests load a fixture by relative path; the lock test only verifies
on-disk SHA-256s. As of v0.9.0 W2.1 the registry ships **0 bound test
files** — wiring `httpx.MockTransport` against these fixtures is
deferred to v0.9.x patches (per the user-query AC §T2.1.1 (b)).

| File path                                  | Endpoint / event                  | Captured from                         | Bound test(s) | Notes                                             |
| ------------------------------------------ | --------------------------------- | ------------------------------------- | ------------- | ------------------------------------------------- |
| `cloud/agents/create_agent_v0.json`        | `POST /v1/agents` 200             | fixture-only seed, 2026-05-08         | (none yet)    | minimal happy path; ships the `agent` + `run` envelope |
| `cloud/agents/get_agent_v0.json`           | `GET /v1/agents/{id}` 200         | fixture-only seed, 2026-05-08         | (none yet)    | minimal happy path; reuses the `bc-fixture-201` id |
| `cloud/agents/list_runs_v0.json`           | `GET /v1/agents/{id}/runs` 200    | fixture-only seed, 2026-05-08         | (none yet)    | upstream `items[]` shape; popola CLI mapping schema lives at `tests/cli/fixtures/cloud_runs_v1.json` |
| `cloud/runs/get_run_v0.json`               | `GET /v1/agents/{id}/runs/{runId}` 200 | fixture-only seed, 2026-05-08    | (none yet)    | terminal run; `cost: null` placeholder            |
| `cloud/runs/stream_assistant_v0.txt`       | SSE chunk (`event: assistant`)    | fixture-only seed, 2026-05-08         | (none yet)    | trailing blank line + `id:` framing are byte-significant |
| `cloud/errors/401_unauthorized_v0.json`    | any `/v1/*` 401                   | fixture-only seed, 2026-05-08         | (none yet)    | maps to `UnauthorizedError`                       |
| `cloud/errors/422_repo_allowlist_v0.json`  | `POST /v1/agents` 422             | fixture-only seed, 2026-05-08         | (none yet)    | one of the 16 entries from the v0.8.6 422 catalog |

## Related — schemas (NOT moved here)

`tests/cli/fixtures/cloud_runs_v1.json` is a **JSON Schema** for
`popola cloud runs --json` output (added in v0.8.8 T2.4.1). It stays
in place per `fixtures-strategy.md` §2 ("the existing
`tests/cli/fixtures/cloud_runs_v1.json` ... is a schema, not a
captured response. It is referenced from `tests/fixtures/README.md`
... but it is not moved.").

## Refreshing this directory

Two paths, never auto-run by CI:

1. **Edit a fixture by hand** (typo fix, comment polish): re-run
   `python scripts/regen_fixture_checksums.py`, commit both the
   fixture and the regenerated `checksums.json` together.
2. **Capture from live API** (after Cursor announces an API change):
   trigger `.github/workflows/cloud-fixtures-drift-check.yml` via
   `workflow_dispatch`; review the auto-filed `fixtures-drift` issue;
   land the refresh in a v0.9.x patch.
