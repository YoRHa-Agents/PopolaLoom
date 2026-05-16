> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md).

# PopolaLoom v1.4.0 — `popola update` Python verb

<!-- updated: 2026-05-17 -->

## v1.4.0 callouts

> **`popola update` is the recommended Python-side update path.** New top-level verb wraps `pip install --upgrade <spec>` + `popola skill upgrade --target=all` (BOTH global and project scopes in a single invocation) + `popola doctor` into one command. `install.sh update` remains the canonical *bootstrap* path (used by `curl ... | bash` before Python is on PATH); `popola update` is the *post-install* path operators reach for after the wheel is installed. The flag matrix is byte-identical between the two.

> **Refuses to run on editable / pipx-managed installs by default.** `pip install -U git+...` over an editable checkout corrupts both copies on `sys.path`; pipx loses track of the pinned version when an inner pip silently changes the wheel. `popola update` detects both via PEP 610 `direct_url.json` + `sys.executable` parts and exits `2` with a remediation hint (`git pull && popola skill upgrade --target=all --global --project` or `pipx upgrade popolaloom`). Pass `--force` to override.

> **Daemon NOT auto-restarted after upgrade.** When `popolad.sock` is present after the wheel upgrade, `popola update` appends a stderr `warn:` line suggesting `popola popolad stop && popola popolad start` rather than auto-restarting. Auto-restart was rejected because in-flight tasks would die mid-flight.

## Highlights

| Item | v1.4.0 resolution |
|---|---|
| One-shot Python update path | **`popola update`** verb registered in `popolaloom.cli.update_cmd` (Typer) — full flag matrix mirrors `install.sh:verb_update` lines 502-525 |
| Editable / pipx safety net | `popolaloom.evolution.self_update.detect_install_kind` classifies the running install via PEP 610 `direct_url.json` + `sys.executable.parts` and refuses unsafe upgrades |
| pip subprocess wrapper | `popolaloom.evolution.self_update.run_pip_upgrade` raises `PipUpgradeError` with full stderr capture — No Silent Failures |
| Both scopes in one invocation | New `--scope=both` (default) walks every `(target, scope)` pair the IDE supports — single command, no two-pass dance |
| Cross-implementation parity test | `tests/test_update_parity.py` invokes `install.sh update --dry-run` for a 10-row fixture matrix and compares the resolved spec line to `resolve_install_spec()` byte-for-byte |
| SKILL.md drift catch | PR companion test `test_tracked_project_skill_version_matches_package` (parametrised over `.claude/` and `.github/copilot-instructions.md`) fails default-lane CI when a future minor bump forgets to refresh tracked project skills — closes the v1.1.1 / v1.3.0 release-process oversight reported in `popola doctor` |

## New verb shape

```bash
popola update [--target=cursor|claude|codex|copilot|all]
              [--scope=global|project|both]
              [--from=git|pypi|<PATH>]
              [--ref=<tag|branch|sha>]
              [--version=X.Y.Z]
              [--python=<bin>]
              [--no-skills]
              [--no-doctor]
              [--with-credentials]
              [--force]
              [--dry-run]
              [--quiet]
              [--json]
```

Defaults: `--target=all --scope=both --from=git` — tracks `main`, matches `install.sh` v0.9.6+ default.

## New CLI exit codes

| Code | Meaning |
|---|---|
| `0` | Update applied (or dry-run plan rendered); skill doctor reported clean |
| `1` | pip subprocess failed (full stderr tail rendered to stderr) OR spec validation failed (e.g. `--ref` with `--from=pypi`) |
| `2` | Refused unsafe install (editable / pipx); remediation hint on stderr |
| `3` | Skill doctor still reports DRIFT/MISS after the upgrade (rare; usually a permission error on a target dir) |

## Verification

```bash
# Default-lane test surface added in v1.4.0:
pytest tests/cli/test_update_cmd.py \
       tests/evolution/test_self_update.py \
       tests/test_update_parity.py \
       tests/cli/test_skill_md_canonical.py
# 62 passed in 0.45s (29 self_update + 12 update_cmd + 12 parity + 9 canonical)

# Live dry-run sanity check:
popola update --dry-run --json | jq
popola version           # popolaloom 1.4.0
popola doctor             # OK across all 6 skill rows after upgrade
```

## Migration notes

- Anyone calling `popola skill upgrade --target=all --global` AND `popola skill upgrade --target=all --project` from a wrapper script can replace both with `popola update --no-skills=false --no-doctor` (or just `popola update` for the full pipeline).
- Editable / pipx users who want the old "just trust me" behaviour: pass `--force`. The orchestrator still appends warnings to stderr explaining the trade-off.
- `install.sh update` continues to work unchanged — the two paths are now contract-equivalent (parity-tested) but each is appropriate to its context (bash bootstraps before Python; Python serves day-to-day operators).
