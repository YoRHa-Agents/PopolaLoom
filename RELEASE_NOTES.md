> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.8.4 — unified install script

> Released: 2026-05-07
> Theme: builds on v0.8.3 by shipping a one-line bash bootstrap installer (`install.sh`) for fresh machines, plus a matching `popola skill uninstall` Typer verb so operators have a complete install / update / uninstall surface across pip + Skills × global / project × cursor / claude / codex / copilot. The previous installer surface was the `popola init` Typer command (still works) and the LLM-driven `install-popola` Skill (still works); the new bash script is a one-liner bootstrap so a fresh machine reaches "installed + Skills registered + daemon optional" without an agent CLI in the loop.

## Why v0.8.4 right after v0.8.3?

Direct user feedback in `feedback_for_v0.8.3.md` listed two items:

1. Install / update / uninstall script for PopolaLoom + its Skills.
2. Global vs project install support across cursor / codex / claude / copilot.

v0.8.4 closes both as a cohesive feature patch — one shell script (`install.sh`) for the cross-cutting verbs and a symmetric `popola skill uninstall` Typer verb so the bash script's `uninstall` path can surgically remove the Skills before `pip uninstall popolaloom`.

## Highlights

### 1. `install.sh` — one-line bootstrap

The unified bash installer at the repo root (`install.sh`) wraps the previous four-step manual workflow (`pip install` → `popola skill install` → `popola popolad start` → `popola doctor`) into a single shell command. Symmetric `update` and `uninstall` verbs round out the surface.

```bash
# Fresh install for every IDE at user-home scope (typical first run)
curl -fsSL https://raw.githubusercontent.com/YoRHa-Agents/PopolaLoom/main/install.sh | bash

# Same, with explicit options
curl -fsSL https://raw.githubusercontent.com/YoRHa-Agents/PopolaLoom/main/install.sh \
  | bash -s -- install --scope=global --target=all

# After a clone — same script, local invocation
./install.sh install --scope=project --target=cursor
./install.sh update
./install.sh uninstall --yes --purge
```

**Verbs**: `install` (default) / `update` / `uninstall` / `version` / `help`.

**Options**: `--scope=global|project`, `--target=cursor|claude|codex|copilot|all`, `--from=pypi|git|<path>`, `--version=X.Y.Z`, `--python=<bin>`, `--no-skills`, `--no-daemon`, `--purge`, `--yes`/`-y`, `--dry-run`, `--quiet`/`-q`, `--help`/`-h`.

The script is idempotent (re-running the same command is safe), auto-detects Python 3.11+ across `python3.12 → python3.11 → python3 → python`, and obeys the workspace "No Silent Failures" rule — every external command runs through a `run_cmd()` helper that prints the command and aborts on non-zero exit from critical steps (the only best-effort step is the post-install daemon boot, which logs the skip reason so the operator can manually retry `popola popolad start` if it failed).

`--from=` source resolution: `pypi` (default) → `pip install popolaloom`; `pypi` + `--version=X.Y.Z` → `pip install popolaloom==X.Y.Z`; `git` → `pip install git+https://github.com/YoRHa-Agents/PopolaLoom.git`; any other value (filesystem path) → `pip install <path>` (works for local clones, wheel files, and tarballs).

### 2. `popola skill uninstall` verb

Symmetric to `popola skill install` / `doctor` / `upgrade`. Backed by the new library API in `src/popolaloom/evolution/skill_uninstall.py` (256 lines), the verb removes the SKILL.md (and the sibling `.popola-loom-version` marker for cursor / claude / codex; copilot has no marker since it ships as a single `copilot-instructions.md` file) and prunes the now-empty `popola-loom/` leaf directory.

```bash
popola skill uninstall --target=cursor --global
popola skill uninstall --target=all --project
popola skill uninstall --target=all --global --dry-run     # preview
popola skill uninstall --target=all --global --json        # machine-readable
```

Idempotent on a clean home — re-running prints `ABSENT` rather than failing. The unified `install.sh uninstall` verb composes this with `popola popolad stop` (best-effort) + `pip uninstall popolaloom` (gated on confirmation) + optional `rm -rf $POPOLA_HOME` (when `--purge` is set) for one-shell-command teardown.

### 3. 23 new default-lane tests

| File | Tests | Coverage |
|---|---|---|
| `tests/evolution/test_skill_uninstall.py` (NEW) | 10 | library API: happy path / dry-run / absent / copilot single-file / `uninstall_all_skills` aggregator + per-target outcome shape |
| `tests/cli/test_skill_cmd.py` (extended) | +6 | `uninstall --target=cursor --global` removes SKILL.md + marker, `--target=all` is idempotent on clean home, `--dry-run` does not unlink, `--json` is machine-readable, `--global` + `--project` simultaneously raises `BadParameter` |
| `tests/cli/test_install_script.py` (NEW) | 13 | bash subprocess tests: `--help`, `version`, `install --dry-run` (default verb / version pin / `--from=git`), `update --dry-run`, `uninstall --dry-run --yes`, invalid verb / scope / target error paths, `--version=X.Y.Z` × `--from=git` mutual conflict |

Plus the `test_skill_help_lists_three_verbs` → `test_skill_help_lists_four_verbs` rename to match the new `popola skill --help` surface.

### 4. Documentation refresh

- `README.md` — new "One-line install (v0.8.4+)" + "Update / Uninstall" subsections; v0.8.4 row added to the Status table (and a row for the new `popola skill uninstall` verb); install snippets bumped from `0.8.3` → `0.8.4`.
- `docs/USER_GUIDE.md` — new `## install.sh — bash bootstrap installer (v0.8.4+)` reference section with the verb × flag matrix, source-resolution table, examples, idempotency contract, and "when to use `install.sh` vs `popola init`" guidance.
- `src/popolaloom/skills/install-popola/SKILL.md` — new "Step 0 — one-line install (preferred, v0.8.4+)" subsection and "Uninstall path (v0.8.4+)" subsection, plus seven new triggers (`update popola`, `uninstall popola`, `更新 popolaloom` / `更新 popola-loom`, `卸载 popolaloom` / `卸载 popola-loom`, `/update-popola`, `/uninstall-popola`).

## Files changed (v0.8.4)

| Slice | Files |
|---|---|
| New code | `install.sh`, `src/popolaloom/evolution/skill_uninstall.py` |
| CLI surface | `src/popolaloom/cli/skill_cmd.py` (adds `cmd_uninstall` + helpers) |
| Tests | `tests/evolution/test_skill_uninstall.py` (NEW), `tests/cli/test_install_script.py` (NEW), `tests/cli/test_skill_cmd.py` (extended; +6 tests + rename) |
| Docs | `README.md`, `docs/QUICKSTART.md`, `docs/USER_GUIDE.md`, `docs/DEMO.md`, `docs/index.md`, `docs/zh/QUICKSTART.md`, `docs/zh/USER_GUIDE.md`, `docs/assets/i18n/en.json`, `docs/assets/i18n/zh.json`, `src/popolaloom/skills/install-popola/SKILL.md` |
| Web banners | `docs/assets/js/i18n.js`, `docs/assets/js/theme.js`, `docs/assets/js/extras.js` |
| Bump | `pyproject.toml`, `src/popolaloom/__init__.py`, `src/popolaloom/skills/popola-loom/SKILL.md`, `src/popolaloom/skills/popola-loom/.popola-loom-version`, `src/popolaloom/skills/install-popola/SKILL.md` (frontmatter), `docs/_config.yml`, `docs/_includes/footer.html`, `tests/test_smoke.py`, `tests/cli/test_install_script.py` (one version-pin literal), `CHANGELOG.md`, `RELEASE_NOTES.md` |
| Removed | `.github/.popolaloom-version` (orphan; stale `0.5.0` marker from pre-rename copilot install testing — canonical marker is now `.popola-loom-version` next to each installed SKILL.md, not at the repo root) |

## Verification

- `pytest tests/test_smoke.py tests/docs/test_docs_contract.py -q` → 8 passed (smoke + docs contract: package version ↔ `docs/_config.yml` `popola_version` ↔ SKILL.md frontmatter sync verified)
- `pytest tests/evolution/test_skill_uninstall.py tests/cli/test_skill_cmd.py tests/cli/test_install_script.py -q` → 45 passed (23 new + extended tests for the uninstall + install.sh surfaces)
- `pytest tests/ -m "not slow and not real_graph and not e2e and not nightly and not real_cli and not real_lark" -q` → 1632 passed, 18 skipped, 82 deselected (default lane, ~20s wall time)
- `ruff check src/popolaloom tests/` → clean
- `mypy src/popolaloom` → clean (83 source files)
- `grep -rn '"0\.8\.3"' src/popolaloom tests pyproject.toml` → empty (no stale literals)

## Status

| Capability | Status |
|---|---|
| ALL v0.8.3 capabilities | unchanged |
| `install.sh` unified bash installer (install / update / uninstall × global / project × cursor / claude / codex / copilot) | OK live (v0.8.4+) |
| `popola skill uninstall --target=<...>` Typer verb | OK live (v0.8.4+) |
| `popola skill --help` lists four verbs (install / doctor / upgrade / uninstall) | OK live (v0.8.4+) |
| `install-popola` Skill triggers cover update / uninstall verbs (EN + ZH) | OK live (v0.8.4+) |
| README + USER_GUIDE + install-popola SKILL.md install/uninstall reference | OK live (v0.8.4+) |
| 23 new default-lane tests (uninstall library + CLI + bash subprocess matrix) | OK live (v0.8.4+) |
| Coverage gate `fail_under = 94` | unchanged |

## Upgrade notes

- **No breaking changes.** `pip install -U popolaloom` continues to work; the existing `popola init` family + `popola skill install` / `doctor` / `upgrade` verbs are untouched. The new `install.sh` is purely additive — operators who do not want the bash bootstrap can skip it and stick to the manual `pip install` + `popola init` workflow that has shipped since v0.5.0.
- For an end-to-end one-command upgrade, the recommended path is now: `./install.sh update` (which runs `pip install --upgrade popolaloom` + `popola skill upgrade --target=<...>` + `popola doctor`). The manual `pip install --upgrade popolaloom` + `popola skill upgrade --target=all` workflow still works.
- For uninstall, the recommended path is now: `./install.sh uninstall --yes` (or `--yes --purge` to also delete `${POPOLA_HOME:-$HOME/.popola}` daemon state). The `--purge` flag is gated on the destructive prompt — pass `--yes` only after backing up anything you need.

## Branch / PR

- Branch: `feature/v0.8.4-install-script`. Merged via squash PR into `main` after CI green per the workspace "Protected Branch Workflow" rule.
- Suggested release PR title: `release: v0.8.4 — unified install.sh + popola skill uninstall`.
