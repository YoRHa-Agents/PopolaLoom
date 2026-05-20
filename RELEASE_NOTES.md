> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md).

# PopolaLoom v1.6.1 — agent CLI rename + auth.json pre-flight

Released: 2026-05-19

<!-- updated: 2026-05-19 -->

## Theme

v1.6.1 closes the three Stage-T live-probe findings filed in
[`feedback_for_v1.6.0.md`](.local/feedbacks/feedback_for_v1.6.0.md):
(a) the upstream Cursor CLI was renamed from `cursor` to `agent` in
2026.05 and every operator-facing hint, skill copy, and copilot
instruction in v1.6.0 still spelt the JWT bootstrap command `cursor
login`; (b) the `CursorAdapter` default binary still resolved to the
legacy `cursor-agent` name first; (c) the v1.6.0 worker boot path
deferred the missing-`~/.config/cursor/auth.json` failure until the
worker subprocess emitted its own `Authentication required for worker
mode` error line, hiding the cause behind upstream log noise. No new
features; no daemon contract changes; no migrations. The
v1.6.0 single-path self-hosted dispatch contract and the six hard
constraints from `feedback_for_v1.5.2.md` remain intact.

## What changed

- **`agent login` standardisation.** Every operator-facing string that
  previously instructed the legacy `cursor`-prefixed login command now
  reads `agent login`. Scope:
  the 4 source touches (`src/popolaloom/cloud/internal/jwt_auth.py`,
  `src/popolaloom/cloud/internal/cursor_cloud_internal.py`,
  `src/popolaloom/cli/main.py`, `src/popolaloom/cli/init_cmd.py`), both
  Skill copies under `src/popolaloom/skills/popola-loom/SKILL.md` and
  `.claude/skills/popola-loom/SKILL.md` (byte-identical mirror enforced
  by the new compliance test), the `install-popola` Skill, both English
  and Chinese `USER_GUIDE.md` copies, the `.github/copilot-instructions.md`
  mirror, and the `cloud_worker_cmd.py` worker-start hint string. The
  historical CHANGELOG `[1.6.0]` block intentionally keeps its legacy
  login-command references because they document what shipped in
  v1.6.0; the new `[1.6.1]` block uses `agent login` exclusively.


- **`CursorAdapter` binary resolver flip.** The default binary for
  `CursorAdapter` is now `"agent"` (was `"cursor-agent"`). A new
  `_DEFAULT_CURSOR_BINARIES = ("agent", "cursor-agent")` tuple plus
  `CursorAdapter._resolve_binary()` classmethod prefers `agent`,
  falls back to `cursor-agent`, and lets the existing `binary=` override
  pin a specific spelling for tests / multi-version hosts. Both
  `build_command()` and `is_available()` route through the resolver so
  the v1.6.1 default works on every PATH layout the v1.6.0 release
  supported.
- **`popola cloud worker start` pre-flight + `--allow-missing-auth`.**
  `worker_start_cmd` now eagerly checks `~/.config/cursor/auth.json`
  before launching the worker subprocess. When the file is missing,
  the command exits 1 with an `agent login` hint instead of letting
  the worker subprocess fail later with the harder-to-diagnose
  `Authentication required for worker mode` log line. The new
  `--allow-missing-auth` flag skips the gate for CI smoke tests that
  intentionally omit the JWT step; `--dry-run` also bypasses the
  check (the dry-run path never spawns a worker).

## Upgrade notes

- **No command-line changes required** for operators already on v1.6.0.
  The binary resolver tries both `agent` and `cursor-agent`, so existing
  shells that have `cursor-agent` on PATH continue to work; if both are
  present, `agent` wins. The CLI surface, daemon RPC, dispatch contract,
  and `--auth-mode` semantics are byte-identical to v1.6.0.
- **Re-run `popola skill install --target=cursor --global --force`** if
  you previously installed an older Skill copy under
  `~/.cursor/skills/popola-loom/SKILL.md`. The new v1.6.1 Skill spells
  the JWT bootstrap command `agent login` everywhere; the installer
  refuses to overwrite a customised copy without `--force`, so an
  explicit re-install is the cleanest way to pick up the rewrite. The
  byte-identical mirror under `.claude/skills/popola-loom/SKILL.md`
  picks up the rewrite the same way via `--target=claude`.

## Breaking changes

- **`popola cloud worker start` now requires `~/.config/cursor/auth.json`
  to exist** unless `--allow-missing-auth` or `--dry-run` is passed.
  Operators who previously ran `popola cloud worker start` on a fresh
  machine and let the worker subprocess prompt for login MUST either
  run `agent login` first (the documented bootstrap) OR pass
  `--allow-missing-auth` to suppress the pre-flight. The new flag is
  the documented escape hatch for CI environments that mint the JWT
  out-of-band or test the worker boot path without a real Cursor
  session.

## Constraint regression

All 6 hard constraints from `feedback_for_v1.5.2.md` remain pinned:

- **#1 pool flag removed** — `tests/cli/test_cloud_worker_cmd.py::test_pool_flag_does_not_exist_on_worker_{start,debug}` (unchanged from v1.6.0).
- **#2 no local-CLI fallback for self-hosted** — `tests/cli/test_no_silent_fallback.py::test_allow_fallback_is_noop_for_self_hosted_cloud_target` (unchanged).
- **#3 no GitHub-App preflight** — `tests/cloud/test_preflight.py::test_check_github_app_installed_skipped_for_self_hosted` (unchanged).
- **#4 dashboard URL surfaced** — `tests/cli/test_dispatch_dashboard_url.py` (unchanged).
- **#5 single explicit Path-B JWT** — `tests/contract/test_self_hosted_single_path.py` (unchanged).
- **#6 Skill enforces 1-5** — `tests/skills/test_skill_self_hosted_compliance.py` (NEW in v1.6.1; 6 cases parametrised over both SKILL.md copies, including the byte-identical-mirror gate and the `agent login` rewrite check).

## Known limitations

- **`agent worker` shutdown deletes `~/.config/cursor/auth.json`** — the
  upstream Cursor CLI's `agent worker start` subprocess deletes the
  operator's session JWT as part of its shutdown cleanup. PopolaLoom
  cannot fix this client-side; v1.6.1's `popola cloud worker start`
  pre-flight surfaces the failure at the popola boundary so operators
  see the `agent login` hint immediately on the next dispatch attempt.
  See [`docs/known-issues.md` §v1.6.1](docs/known-issues.md) for the
  full description, workaround, and the
  `BL-v1.6.x-worker-shutdown-auth-deletion` tracking row in
  `CHANGELOG.md §[Unreleased]`.
- All v1.6.0 carry-over limitations remain valid (Cursor Connect-RPC
  `env=machine→pool` downgrade; GPT-5.5 `long_running_agent_mode`
  escape hatch; JWT auto-refresh deferral; coverage floor temporarily
  at 93%).

## Upgrade

```bash
popola update

pip install --upgrade git+https://github.com/YoRHa-Agents/PopolaLoom@v1.6.1
popola skill install --target=cursor --global --force
popola skill install --target=claude --global --force

popola version  # → popolaloom 1.6.1
```
