> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md).

# PopolaLoom v1.1.1 — Cloud HITL wheel hotfix + init/auth/prefs polish

<!-- updated: 2026-05-12 -->

## v1.1.1 callout

> **Breaking:** Cloud HITL migrations are now FAIL-loud at daemon startup. Any
> `popolad` installed from a wheel older than `popolaloom>=1.1.1` cannot start
> under the v1.1.1 daemon without re-running install/upgrade so packaged
> migrations `005`/`006`/`007` are present. Reinstall `popolaloom>=1.1.1` or run
> `popola doctor` for the audited path.

## Highlights

- P1 (§1): moved PopolaLoom migrations into `popolaloom.migrations`, verified
  wheel packaging, and added typed `MigrationsMissingError` with
  `popolad.migrations_missing` emission.
- P2 (§2 §3): added the optional preferences footer / TTY wizard flag and made
  `cursor_api_key.env` directly sourceable via `export CURSOR_API_KEY=...`.
- P3 (§4 §5): added skill-drift detection / `--upgrade-on-drift` plus
  `popola auth cursor status` fallback-file reporting with unsafe-mode refusal.
- P4 (§6 §7 §8): escaped Rich preference headings, fixed Workflow numbering,
  linted heading uniqueness, and stamped/rendered preferences metadata.

## Verification

Run:

```bash
pytest
pytest --cov=src/popolaloom --cov-report=term-missing
```
