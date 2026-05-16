> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md).

# PopolaLoom v1.3.0 — Self-hosted dispatch hardening + Path-B observability

<!-- updated: 2026-05-16 -->

## v1.3.0 callouts

> **Path-B wire format is now camelCase.** `build_start_composer_request` and the supervisor's Path-B body construction emit camelCase JSON keys (e.g. `startingRef`, `modelDetails`, `agentMode`, `snapshotNameOrId`, `devcontainerStartingPoint`) to match Cursor's Connect-Protocol server expectation. Python kwargs on `build_start_composer_request` remain snake_case — only the on-wire serialization flips. The v1.1.1 snake_case body was returning HTTP 400 `invalid_argument` upstream; v1.3.0 unblocks live Path-B dispatches.

> **`popola cloud worker start --detach` is the recommended foreground replacement.** The new flag double-forks + `setsid()` the worker so the grandchild has PPID=1 and survives IDE/SSH session close. The legacy `setsid nohup popola cloud worker start ... &` recipe still works but is no longer needed.

> **Path-B presets persistable.** `popola init prefs --set cursor-cloud.default_preset=grind` (and `default_mode` / `default_effort` / `default_max_mode` / `default_long_running` / `default_auto_proceed_after_plan` / `default_time_budget` / `default_thinking_level`) plus `cursor.default_model=gpt-5.5` now round-trip through `~/.popola/popolad.toml`. `popola dispatch --auth-mode=session-jwt` falls through to these defaults when the per-task flag is absent.

## Highlights — mapped to user feedback

Source: [`.local/feedbacks/feedback_for_v1.2.0.md`](.local/feedbacks/feedback_for_v1.2.0.md) (mis-named — actually a v1.1.1 field-test report).

| User feedback | v1.3.0 resolution |
|---|---|
| (1) self-hosted skips github/gitlab auth | Already supported via `--cli=cursor` (Workflow 1); v1.3.0 docs reinforce that local subprocess dispatch is the canonical self-hosted path |
| (2) worker should start in background | **P1 — `popola cloud worker start --detach`** |
| (3) JWT for self-hosted | Already supported via `--auth-mode=session-jwt`; **P4 + P5** unblock its actual use by fixing the Path-B `_post_rpc` mis-reporting and the missing wire fields |
| (4) model selection (gpt-5.5 high mode) + thinking-depth | `--model` already shipped in v1.0.0; `--max-mode` already shipped in v1.0.0; **P2 adds `--thinking-level low\|medium\|high`**; **P6** makes them all persistable |
| (5) grind mode | `--preset=grind` already shipped in v1.1.0; **P6** makes `default_preset=grind` persistable |

## Bug fixes

- **P3** — `popola cloud worker stop --name <X>` / `popola cloud worker stop --worker-dir <X>` now matches Node-wrapped `agent worker start` processes (modern installs ship the binary as a Node shim, so `argv[0]="node"` and v1.1.1's basename check rejected them). Feedback §7 reported `error: no matching worker found` for live workers.
- **P4** — `cursor_cloud_internal._post_rpc` now parses the upstream Connect-Protocol error envelope (`code` / `message` / `details[i].debug.details.detail`) on 4xx responses and surfaces it via the new `connect_code` / `connect_message` / `details_summary` / `error_kind` fields on `CursorCloudInternalError`. Feedback §2 reported "Path-B 接口假死" because the v1.1.1 code reported 400 `invalid_argument` field-required errors as 404 "method may have moved".
- **P5** — `build_start_composer_request` adds 11 previously-missing Connect-Protocol body fields (`snapshotNameOrId`, `devcontainerStartingPoint`, `repositoryInfo`, `snapshotWorkspaceRootPath`, `autoBranch`, `returnImmediately`, `repoUrl` mirror, `conversationHistory`, `source`, `bcId`, `addInitialMessageToResponses`, `usePrivateWorker`) and flips JSON serialization to camelCase. Feedback §2 reverse-engineered the actual wire-format that works via direct curl; v1.3.0 implements it verbatim.

## Verification

```bash
# Test suite (new + existing)
pytest tests/cli/test_user_prefs_path_b_defaults.py \
       tests/cli/test_dispatch_path_b_flags.py \
       tests/cli/test_worker_start_detach.py \
       tests/cli/test_worker_stop_matching.py \
       tests/cloud/internal/test_build_composer_camel.py \
       tests/cloud/internal/test_post_rpc_4xx.py \
       tests/cloud/internal/test_rpc_mock.py \
       tests/daemon/test_supervisor_path_b_branch.py -v

# Broader regression sweep
pytest tests/cli/ tests/cloud/ tests/daemon/ --timeout=120

# Version check
python -c "import popolaloom; print(popolaloom.__version__)"   # → 1.3.0

# Smoke: detach worker dry-run
popola cloud worker start --detach --dry-run --worker-dir /tmp

# Smoke: thinking-level flag visible in help
popola dispatch --help | grep -- --thinking-level

# Smoke: persistable preset
popola init prefs --set cursor-cloud.default_preset=grind
popola init prefs show | grep default_preset

# Smoke: build_start_composer_request emits camelCase
python -c "
from popolaloom.cloud.internal.cursor_cloud_internal import build_start_composer_request
b = build_start_composer_request(prompt='hi', repo_url='https://github.com/o/r')
assert 'snapshotNameOrId' in b and 'devcontainerStartingPoint' in b
print('OK')
"
```

## Out of scope (deferred to backlog)

- `BL-v1.3.x-path-b-non-github-routing` — Cursor server-side hard constraint; cannot fix client-side (feedback §3/§4).
- `BL-v1.3.x-bc-model-whitelist-sync` — BackgroundComposer model list ≠ REST `/v1/models` list (feedback §2 model whitelist table); needs a one-shot probe + cache.
- `BL-v1.3.x-jwt-auto-refresh` — JWT exp is 1h; popolaloom currently warns but doesn't auto-refresh.

## Known pre-existing test fragility (not v1.3.0 regressions)

5 tests in the broader sweep fail in environments where `~/.popola/popolad.toml` exists OR `CURSOR_API_KEY` resolves from a fallback (keyring / `cursor_api_key.env`):

- `tests/cli/test_attach_sse_fallback.py::test_missing_api_key_skips_cloud_sse_with_notice`
- `tests/cli/test_dispatch_replay.py::test_dispatch_without_replay_still_requires_cli`
- `tests/daemon/test_cloud_spawn_failpaths.py::test_missing_api_key_when_env_unset_and_no_extra_override`
- `tests/daemon/test_supervisor_cloud_branch.py::test_missing_api_key_emits_task_failed`
- `tests/daemon/test_supervisor_cloud_branch.py::test_spawn_cloud_failed_path_still_tags_runtime_cloud`

Independently verified against the v1.1.1 baseline commit `823a46b` — all 5 failed there too. Fix tracked separately.
