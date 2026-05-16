> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md).

# PopolaLoom v1.5.0 — JWT-direct self-hosted worker dispatch + No-Silent-Fallback

<!-- updated: 2026-05-17 -->

## v1.5.0 callouts

> **JWT-direct dispatch onto a registered self-hosted Cursor worker now works end-to-end.** The path-B body emits `env={"type":"machine","name":<your-worker>}` so Cursor's `BackgroundComposerService` routes the run to the named worker, bypassing GitHub / GitLab authentication entirely. Fixes G1–G4 of `feedback_for_v1.4.0.md`.

> **No-Silent-Fallback invariant landed.** popola no longer switches the dispatched CLI adapter, auth-mode, or path-B knob without explicit operator consent. `--cli=cursor` on a system without Cursor installed now hard-fails (exit 1) instead of silently walking `fallback_chain`. Use `--allow-fallback` to opt back into the chain (per-dispatch; not persisted). SSE → poll observability fallbacks are explicitly out of scope.

> **`popola popolad start` env injection chain** (4-tier; G8 + G9). The daemon child process picks up `CURSOR_API_KEY` via: operator `os.environ` > `--env-file <path>` > `~/.popola/cursor_api_key.env` > `<cwd>/.local/.secrets/cursor_user_api_key.secret` > `<cwd>/.env`. All file sources require mode 0o600. The new `--reload-env` flag is a convenience equivalent for `stop && start <same flags>`.

> **`popola init` JWT auto-detect.** The interactive wizard detects `~/.config/cursor/auth.json` and prompts to set `[user_preferences.cursor-cloud].default_auth_mode = "session-jwt"` so subsequent cursor-cloud dispatches use the JWT path by default.

## Highlights

| Item | v1.5.0 resolution |
|---|---|
| Self-hosted worker dispatch via path-B | `build_start_composer_request(target_machine_name=<X>, env_emit_mode="machine")` emits `env={type:machine,name:X}` on the Connect-RPC body |
| Skip git-host auth | Four new Typer toggles: `--no-auto-branch` / `--no-auto-create-pr` / `--work-on-current-branch` / `--skip-reviewer-request` |
| Response shape compatibility | Client now accepts `background_composer_id` (snake_case), `backgroundComposerId` (camelCase), AND `composer.bcId` (v1.4.0+ nested envelope); also surfaces `initial_run_id` for SSE correlation |
| Escape-hatch knobs | `--cli-flag env_emit_mode=label\|none` (drops env field) + `--cli-flag model_id_override=<id>` (GPT-5.5 dual-naming case) |
| No-Silent-Fallback rule | `_select_available_local_cli` defaults to hard-fail; `--allow-fallback` is the per-dispatch opt-in (visible "fallback consent acknowledged" log line on switch) |
| Reword 8 hint sites | "fall back to --auth-mode=rest" → "re-dispatch with --auth-mode=rest (popola does NOT auto-switch transports)" |
| popolad env injection | 4-tier chain in `popolad start`; `--env-file <path>` + `--reload-env` flags |
| `cursor.cli_args` propagation | v1.3.0 regression resolved (was being dropped silently) |
| `missing_api_key` hint copy | "0o600 `.env`" → "0o600 `~/.popola/cursor_api_key.env`" |
| `default_auth_mode` pref | New `[user_preferences.cursor-cloud].default_auth_mode` field; `popola init` JWT auto-detect prompt |

## End-to-end dispatch shape

```bash
popola dispatch \
  --cli=cursor-cloud \
  --auth-mode=session-jwt \
  --cloud-target=self-hosted \
  --worker-name=<your-worker> \
  --model=gpt-5.5 --thinking-level=high \
  --preset=grind \
  --no-auto-branch --no-auto-create-pr --work-on-current-branch \
  "<prompt>"
```

| Acceptance gate | What it verifies |
|---|---|
| G1 | Terminal state ≠ `failed`; dashboard does not report "Environment error" |
| G2 | `popola cloud worker start --detach` → `/readyz.connected == true` |
| G3 | Worker `/readyz.claimed == true` + metrics `cursor_self_hosted_worker_session_active == 1.0` |
| G4 | Argv contains NO `--repo-url=https://github.com/...` strong-dep; body routes via `env={type:machine,name:X}` |
| G5 | dashboard shows `GPT-5.5 ... High` |
| G6 | Event log carries `long_running_agent_mode=true`, `effort_mode=EFFORT_MODE_HIGH`, `time_budget_seconds=14400`, `auto_proceed_after_planning=true` |
| G7 | Connect-RPC sees `Authorization: Bearer <jwt>`; `cloud.queued` event has `auth_mode="session-jwt"` |
| G8 | `popola popolad start` injects `CURSOR_API_KEY` from `<cwd>/.local/.secrets/cursor_user_api_key.secret` into the daemon child env (CLI-side only; does NOT enter the dispatch credential resolver) |
| G9 | (a) `popolad start --reload-env` re-injects without the operator dropping in-flight events; (b) `popola init` doesn't silently swallow env-set failures |

## Verification

```bash
# Phase I — default-lane test surface added in v1.5.0:
pytest \
  tests/cloud/internal/test_rpc_mock.py \
  tests/cloud/internal/test_build_composer_camel.py \
  tests/cli/test_dispatch_path_b_flags.py \
  tests/cli/test_dispatch_cmd.py \
  tests/cli/test_popolad_start_env_file.py \
  tests/cli/test_no_silent_fallback.py \
  tests/daemon/test_supervisor_path_b_branch.py \
  tests/test_credentials.py
# 169 passed (4 new test files + 5 extended)

# Phase K — local acceptance (G1–G9). See .cursor/plans/v1.5.0_jwt_local_worker_dispatch_*.plan.md §验收
popola popolad stop || true; unset CURSOR_API_KEY; popola popolad start
popola cloud worker start --worker-dir $PWD --name <your-worker> --detach --management-addr 127.0.0.1:39231
TASK_ID=$(popola dispatch --cli=cursor-cloud --auth-mode=session-jwt --cloud-target=self-hosted \
  --worker-name=<your-worker> --model=gpt-5.5 --thinking-level=high --preset=grind \
  --no-auto-branch --no-auto-create-pr --work-on-current-branch \
  "Reply with: PASS v1.5.0 path-B dispatch." --json | jq -r '.task_id')
curl -fsSL http://127.0.0.1:39231/readyz | jq '.claimed'
popola status $TASK_ID --wait --timeout 1800
```

## Migration notes

- **`fallback_chain` no longer silent**: a `--cli=cursor` dispatch on a system without Cursor installed now hard-fails (exit 1). Restore old behaviour for a single dispatch with `--allow-fallback`; there's deliberately no persisted opt-in.
- **JWT auto-detect prompt on `popola init`**: when `~/.config/cursor/auth.json` exists, the wizard offers to set `default_auth_mode = "session-jwt"`. Decline if you prefer the explicit per-dispatch `--auth-mode=session-jwt`.
- **Path-B body now emits `env`**: dispatches with `--cli=cursor-cloud --auth-mode=session-jwt --cloud-target=self-hosted --worker-name=<X>` previously dropped `worker_name`; v1.5.0 emits `env={"type":"machine","name":<X>}`. If Cursor's server rejects this shape with `path_b_rpc_400_invalid_argument`, opt into `--cli-flag env_emit_mode=label` or `env_emit_mode=none`. popola does NOT auto-shift.
- **`popolad start` env chain**: pre-v1.5.0 the daemon child only saw `os.environ`. v1.5.0 walks 4 tiers; existing setups continue to work (operator's shell still wins). Use `--reload-env` after editing `~/.popola/cursor_api_key.env` to push the new value into a running daemon.
