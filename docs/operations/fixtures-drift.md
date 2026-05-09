---
layout: default
title: Fixtures drift triage runbook
description: Operator-facing runbook for triaging cloud-fixtures-drift-check workflow issues.
---

# Fixtures drift triage runbook (v0.9.0+)

<!-- updated: 2026-05-09 -->

> **Audience**: on-call release engineer for PopolaLoom v0.9.x patches.
> **Scope**: triage `fixtures-drift` issues filed by the
> `.github/workflows/cloud-fixtures-drift-check.yml` workflow against
> the SHA-256-locked `tests/fixtures/` tree.
> **Companion**: full design spec at `.local/research/v0.9.0_ga/fixtures-strategy.md` (gitignored research note).

## When this runbook fires

A `fixtures-drift` GitHub issue is auto-filed by
`cloud-fixtures-drift-check.yml` (monthly cron `0 6 1 * *` + `workflow_dispatch`)
whenever the live-API replay against `tests/real_cursor_cloud/` +
`tests/real_cloud_hitl/` exits non-zero. The non-zero exit is the v0.9.0
GA drift signal — the human-readable semantic-diff renderer
(`scripts/diff_captured_against_fixtures.py`, tracked as
`BL-v0.9.x-fixture-diff`) is deferred to a v0.9.x patch.

## Triage SLA

- **Acknowledge**: within 1 week of issue creation (auto-labelled
  `fixtures-drift` + `v0.9.x`).
- **Classify**: within 1 week of acknowledgement (see classification
  below).
- **Resolve**: within the next v0.9.x patch (or sooner if the drift
  affects the default-lane lock test).

## Responsible parties

- **On-call release engineer** — primary owner; receives the GitHub
  notification on the auto-filed issue.
- **Fallback** — release engineer on rotation per
  `.local/feedbacks/TRACKER.md` (gitignored).

## Triage steps

1. **Read the auto-filed issue** for the pytest log tail (failing test names + first ~50 lines of pytest output as the drift signal).
2. **Reproduce locally**:
   ```bash
   export CURSOR_API_KEY=cr_...
   pytest -m real_cursor_cloud tests/real_cursor_cloud/ -q
   ```
3. **Classify the drift**:
   - **A. Live-API schema change** (Cursor added/dropped fields): regenerate via `python scripts/regen_fixture_checksums.py`, re-run `tests/test_fixtures_locked.py` to confirm green; file a v0.9.x patch CHANGELOG row.
   - **B. Local fixture corruption** (contributor edited a fixture without regen): revert from `git` and re-run the lock test.
   - **C. Workflow infra issue** (Python mismatch / API key expired / network flake): re-run `workflow_dispatch` once; if persistent, file a separate `infra` issue.
4. **Update the lock** (case A only): `python scripts/regen_fixture_checksums.py && git add tests/fixtures/checksums.json tests/fixtures/`.
5. **Land the patch**: open a PR titled `fixtures: refresh after Cursor API drift (<YYYY-MM>)` per the workspace Protected Branch Workflow rule (no direct pushes to `main`). Reference the closing GitHub issue number.

## Cross-references

- `.github/workflows/cloud-fixtures-drift-check.yml` — workflow definition
- `tests/test_fixtures_locked.py` — default-lane SHA-256 lock test
- `scripts/regen_fixture_checksums.py` — sanctioned regen path
- `tests/fixtures/README.md` — fixture inventory + naming convention
- `.local/research/v0.9.0_ga/fixtures-strategy.md` — full design spec (local-only)
