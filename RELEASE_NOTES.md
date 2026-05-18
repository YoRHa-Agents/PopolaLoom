> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md).

# PopolaLoom v1.6.0 — single-path self-hosted dispatch

<!-- updated: 2026-05-18 -->

## v1.6.0 callouts

> **Single canonical self-hosted dispatch path.** v1.6.0 closes the 6 hard constraints in [`feedback_for_v1.5.2.md`](.local/feedbacks/feedback_for_v1.5.2.md) by collapsing self-hosted dispatch (`popola dispatch ... --cloud-target=self-hosted`) to exactly ONE path — Path-B JWT direct via `cursor-cloud-internal`. Zero auto-decision fallback. Managed cloud (`--cloud-target=cursor-managed`) and local CLI dispatch (`--cli=cursor|claude|codex|...`) are unchanged.

> **`--pool` / `--pool-name` removed from `popola cloud worker {start,debug}`** (constraint #1). The popola layer no longer wraps Self-Hosted Pool mode. My Machines is now the ONLY supported worker mode at the popola layer; operators on Cursor Enterprise pools can continue to use `agent worker start --pool` directly against the upstream Cursor CLI without going through popola.

> **`--cloud-target=self-hosted --auth-mode=rest` is rejected** (constraint #5). Operators who explicitly passed `--auth-mode=rest` with `--cloud-target=self-hosted` previously dispatched via Path-A REST. v1.6.0 hard-fails with exit 2 and a bilingual hint pointing at `--auth-mode=session-jwt`. When `--auth-mode` is omitted the CLI silently upgrades to `session-jwt` with a one-line `[prefs] forcing --auth-mode=session-jwt ...` stderr note (No-Silent-Failures: the upgrade is visible).

> **`--allow-fallback` is a no-op + WARN for self-hosted** (constraint #2). Per locked decision Q-4 in the v1.6.0 plan, the flag stays available for non-self-hosted CLIs (`cursor-managed`, local `cursor|claude|codex|copilot`) but becomes a no-op + bilingual stderr WARN when `cloud_target=self-hosted`. The resolver NEVER walks `[user_preferences.routing].fallback_chain` on the self-hosted path.

> **`view: https://cursor.com/agents/<bcId>` printed at dispatch time** (constraint #4). Every cloud dispatch (managed + self-hosted) now prints a `view:` URL on stdout after the task_id so operators get web-side observability immediately. Path-A REST `cloud.queued` event carries the URL derived from the agent_id; Path-B was already emitting it.

> **GitHub-App preflight skipped for self-hosted** (constraint #3). `check_github_app_installed(..., target='self-hosted')` short-circuits to `installed=None` because the registered self-hosted worker holds its own workspace clone — the upstream Cursor GitHub-App is not required.

> **Both SKILL.md copies + the Copilot mirror bumped to v1.6.0**, byte-identical across `src/popolaloom/skills/popola-loom/SKILL.md`, `.claude/skills/popola-loom/SKILL.md`, and `.github/copilot-instructions.md` (constraint #6). Workflow 6 / 10 / 12 rewritten to a SINGLE self-hosted example; new `Verifying a self-hosted dispatch` section documents the `view:` URL contract; the No-Silent-Fallback table compressed from 6 rows to 4.

## Highlights

| Item | v1.6.0 resolution |
|---|---|
| Pool mode (constraint #1) | `--pool` / `--pool-name` flags REMOVED from `popola cloud worker {start,debug}`; supervisor rejects `extra.env.type='pool'` for self-hosted with `error_kind="pool_forbidden_self_hosted"`. |
| Local CLI fallback (constraint #2) | `--allow-fallback` is a no-op + bilingual stderr WARN when `cloud_target=self-hosted`; resolver never consults `fallback_chain` on the self-hosted path. |
| GitHub-App preflight (constraint #3) | `check_github_app_installed(..., target='self-hosted')` short-circuits to `installed=None` without calling `_request_json`. |
| Dashboard URL (constraint #4) | `_print_dashboard_url_or_warn` polls `$POPOLA_HOME/events/<task_id>.jsonl` for ~2 s waiting for `cloud.queued.dashboard_url`; prints `view: <url>` on observe, bilingual WARN on timeout. |
| Single canonical path (constraint #5) | `_apply_path_b_flags` forces `auth_mode=session-jwt` for self-hosted; explicit `--auth-mode=rest` exits 2. Supervisor rejects `extra.__auth_mode__ != 'session-jwt'` for self-hosted with `error_kind="invalid_auth_mode_for_self_hosted"`. |
| Skill enforcement (constraint #6) | Both SKILL.md copies + `.github/copilot-instructions.md` bumped to v1.6.0, byte-identical; Workflows 6/10/12 rewritten; install-popola SKILL gains "Self-hosted setup (one-path)" subsection. |
| Tests | NEW `tests/contract/test_self_hosted_single_path.py` (15 cases, 1:1 mapping to the 6 constraints); NEW `tests/cli/test_dispatch_dashboard_url.py` (6 cases); modifications + inverted assertions across `test_cloud_worker_cmd.py`, `test_no_silent_fallback.py`, `test_dispatch_cloud_target_flags.py`, `test_cloud_worker_dispatch_worker_existence.py`. |

## Migration notes (v1.5.x → v1.6.0)

- **Operators using `popola cloud worker start --pool`** must drop the flag and either accept My Machines mode (the v1.6.0 popola contract) OR invoke `agent worker start --pool` directly against the upstream Cursor CLI outside popola.
- **Operators using `popola dispatch --cloud-target=self-hosted --auth-mode=rest`** must remove the explicit `--auth-mode=rest` (the v1.6.0 default is `session-jwt` for self-hosted) AND run `cursor login` once to populate `~/.config/cursor/auth.json`. No more `CURSOR_API_KEY` needed for self-hosted dispatch.
- **Operators using `popola dispatch --allow-fallback` with `--cloud-target=self-hosted`** should drop the flag — it's a no-op + WARN under v1.6.0. The flag still works for non-self-hosted dispatches (managed cloud + local CLI).

## Upgrade

```bash
# Existing PopolaLoom installation:
popola update

# Or fresh install:
pip install --upgrade git+https://github.com/YoRHa-Agents/PopolaLoom@v1.6.0

popola version  # → popolaloom 1.6.0
```

## Known limitations carry-over

- **Cursor Connect-RPC server-side `env=machine→pool` downgrade** ([docs/known-issues.md](docs/known-issues.md)) — Cursor's `StartBackgroundComposerFromSnapshot` silently downgrades `env={"type":"machine","name":X}` → `env={"type":"pool"}` server-side. PopolaLoom CANNOT fix server-side routing; constraint #1 is satisfied at the popola layer (worker process is My Machines only; daemon rejects pool routing for self-hosted), but operators on a multi-worker account may see a different worker claim the task than the named one. Workaround: run one worker per repo.
- **GPT-5.5 + `long_running_agent_mode`** — Cursor's path-B server rejects bare `gpt-5.5` when `long_running_agent_mode=true`. The escape hatch `--cli-flag model_id_override=gpt-5.5-high` remains the documented workaround.
- **JWT auto-refresh** — JWT exp is 1 h; popolaloom currently warns at boundary but doesn't refresh (`BL-v1.4.x-jwt-auto-refresh`).
- **Coverage 94% floor** — temporarily at 93%; soak on `cloud_worker_cmd.py` / `cursor_cloud.py` error paths still pending (`BL-v1.0.x-coverage-94-restore`).

## Next steps (deferred to v1.6.x / v1.7.0)

- BL-v1.6.x-cursor-env-machine-to-pool — track Cursor's server-side fix for the `env=machine→pool` downgrade; popola will pick up named-worker routing automatically once the upstream regression is resolved.
- BL-v1.3.x-bc-model-whitelist-sync — probe-and-cache the path-B model list (escape hatch landed in v1.5.0 via `--cli-flag model_id_override=<id>`).
- BL-v1.3.x-path-b-non-github-routing — Cursor server-side hard constraint (cursor-managed cloud + non-GitHub repos).
- Anything filed in `.local/feedbacks/feedback_for_v1.6.0.md` after the v1.6.0 Stage T live probe.
