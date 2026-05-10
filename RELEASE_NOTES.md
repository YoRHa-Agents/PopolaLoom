> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md).

# PopolaLoom v1.0.0-pre.1 — Cloud dispatch clarity

<!-- updated: 2026-05-11 -->

> Released: 2026-05-11

> **How to install v1.0.0-pre.1** (Q-D-5 偏离默认 carries forward; PyPI promotion remains tracked as `BL-v0.9.x-PyPI`):
>
> ```bash
> ./install.sh install
> ./install.sh install --ref=v1.0.0-pre.1
> ./install.sh install --with-credentials
> ./install.sh install --ref=v1.0.0-pre.1 --with-credentials
> pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v1.0.0-pre.1
> ```

## Theme

v1.0.0-pre.1 is the first **pre-release on the road to GA `1.0.0`**. It pivots the Cursor Cloud Agents adapter to the live `env: {type, name?}` REST schema (the v0.9.x `usePrivateWorker:true + labels.worker:X` body 400s on Cursor's gateway — confirmed by 22 successful 2xx live probes), deletes the v0.9.9 `account_class` hard-fail pre-flight gate (Spike-0's BRANCH_B verdict was disconfirmed by the new live evidence), installs a worker-existence pre-flight gate in its place, and surfaces first-class `--cloud-target` / `--worker-name` flags on `popola dispatch`. The release also enforces, end-to-end, the user's verbatim "no silent local fallback" contract from [`feedback_for_v0.10.0.md`](.local/feedbacks/feedback_for_v0.10.0.md): when `cloud-target=self-hosted` and the named worker is missing, popola exits 78 with a hint pointing at the actual fix — it never silently re-routes to a local `cursor-agent` subprocess.

The 12 design questions for this release are locked verbatim in [`./.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md`](.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md). The full per-decision implementation delta lives in [`CHANGELOG.md` §[1.0.0-pre.1]](CHANGELOG.md#100-pre1--2026-05-11).

## Highlights

- **Env-shape pivot on `POST /v1/agents` (Q-2)** — drops `usePrivateWorker` / `labels` / `autoGenerateBranch` from the request body; emits `env: {type, name?}` and `workOnCurrentBranch:true` instead. Both API-key classes (personal + service-account) route through the same body builder.
- **`account_class` hard-fail gate REMOVED (Q-4)** — the v0.9.9 `_enforce_account_class_pre_flight_gate()` is deleted. Replaced by `_enforce_self_hosted_worker_exists()`, which calls `GET /v0/private-workers` and surfaces a bilingual hint pointing at the actual fix when the named worker is missing.
- **First-class `--cloud-target` / `--worker-name` flags (Q-6)** — `popola dispatch --cloud-target=self-hosted --worker-name=W "<prompt>"` is now the discoverable, self-documenting form. Auto-set `cli=cursor-cloud` when `--cloud-target` is given AND `--cli` is empty. Backward-compat: `--cli-flag worker_name=W` still works via the same extras dict.
- **No silent local fallback (Q-7)** — when `cloud-target=self-hosted` and the named worker is missing, popola exits 78 with a bilingual hint that contains `popola cloud worker start --name <X> --worker-dir <repo-root>` and `Worker '<name>' 不存在`. The process NEVER spawns a local `cursor-agent` subprocess as a fallback.
- **Init wizard learns `default_cloud_target` (Q-5)** — `popola init --interactive` now asks for the default cloud target when `default_runtime ∈ {cloud, ask-each-time}`. Skipped entirely for `default_runtime=local`.
- **GitHub-App pre-flight + catalog regex extension (Q-9)** — `cursor_cloud.create_agent` now calls `GET /v1/repositories` before the dispatch when `repos[0].url` host is `github.com`, refusing early with a friendly hint instead of waiting for the gateway's confusing "Failed to verify existence of branch" 400. The error catalog grows from 16 to 18 entries and the existing `integration_github_app_branch_not_found` regex now also matches the second message variant ("Failed to determine repository default branch").
- **Tier-4 release-gate live smoke (Q-12)** — `tests/cloud/test_real_cursor_cloud_env_shape_v0_10_0.py` (gated by the existing `real_cursor_cloud` mark + `CURSOR_API_KEY`) is the new release-gate criterion: every release that touches the cloud schema must pass this smoke before tagging. Live cost cap ≤ 20 API calls per session.

## Copy-paste-ready example

```bash
# 1. Start a self-hosted worker for this workspace (one-time per repo)
popola cloud worker start --worker-dir "$(pwd)" --name my-team-worker

# 2. Set your Cursor API key (one-time per machine)
export CURSOR_API_KEY="cr_..."     # or: popola auth cursor set --validate

# 3. Dispatch a task to the named worker; the run shows up at cursor.com/agents
popola dispatch --cloud-target=self-hosted --worker-name=my-team-worker \
  "Refactor the caching layer and add unit tests"
```

The process exits with the new `task_id`. Watch progress with `popola attach <task_id> --follow`, or open the run in the browser at `https://cursor.com/agents` (filterable by `workerId`).

## Breaking changes

The four breaking-change items below are the operator-facing contract shifts. **If you used `popola dispatch --cli=cursor-cloud` in v0.9.x, please read each row before upgrading.**

- **Q-2 — Wire-shape pivot on `POST /v1/agents`** (`usePrivateWorker` / `labels.worker` → `env: {type, name?}`). One-release deprecation alias in `_normalize_cloud_extra` translates the legacy extras with a `DeprecationWarning`. **You must migrate** any script that hand-built `usePrivateWorker:true` raw HTTP payloads to the new `env` shape; the `popola dispatch --cli-flag worker_name=X` escape hatch keeps working unchanged via the new `--cloud-target=self-hosted --worker-name=X` aliasing.
- **Q-4 — `account_class` hard-fail pre-flight gate REMOVED**, replaced by a worker-existence pre-flight gate. **You must update** any CI script that grep'd for the v0.9.9 `account_class` exit-78 hint — the new bilingual hint contains the substring `popola cloud worker start --name <X> --worker-dir <repo-root>` and the Chinese fragment `Worker '<name>' 不存在`.
- **Q-7 — No silent local fallback** when `cloud-target=self-hosted` and the named worker is missing. **You must remove** any expectation that a missing worker "auto-falls-back" to local; if your team relied on that, install a real `popola cloud worker start` step earlier in your pipeline.
- **Q-11 — Adapter API: `CloudCursorClient.create_agent` signature change** — the `use_private_worker: bool` and `labels: dict` keyword arguments are deprecated in favour of `env: AgentEnv | None = None`. **You must migrate** any direct adapter consumers (the Python public API, not the CLI) to pass `env={"type": "machine", "name": "X"}` instead of `use_private_worker=True, labels={"worker": "X"}`.

The full migration checklist + rationale (with verbatim Q-* decisions) lives in [`CHANGELOG.md` §[1.0.0-pre.1] §Breaking changes](CHANGELOG.md#breaking-changes).

## Deprecations

- `--cli-flag use_private_worker=true` and `--cli-flag labels='{"worker":"X"}'` — translated to `env={...}` with a `DeprecationWarning`; removal scheduled for v1.1+.
- `--cli-flag autoGenerateBranch=...` — translated to a no-op (the gateway rejects this field); use `--cli-flag work_on_current_branch=true` instead.
- `[cursor].account_class` field on `credentials.toml` and `--account-class` flag on `popola auth cursor set` — kept for one-release backward compat; one-time `WARN` on read; removal targets v1.1+.
- `[user_preferences].cloud_target_priority` list — replaced by `default_cloud_target` (single value); kept for one-release backward compat; the resolver no longer consults the list.
- `CloudCursorClient.create_agent(use_private_worker=..., labels=...)` keyword arguments — replaced by `env: AgentEnv | None = None`. **(Breaking — see above.)**

## How to verify

The release gate criteria for v1.0.0-pre.1 (per [`PLAN.md` §"Acceptance criteria (release gate for v1.0.0-pre.1)"](.local/.agent/active/v0.10.0-cloud-dispatch-clarity/PLAN.md)):

```bash
# Tier-4 live smoke (release-gate criterion per Q-12).
# Requires CURSOR_API_KEY in the environment.
pytest tests/cloud/test_real_cursor_cloud_env_shape_v0_10_0.py -m real_cursor_cloud

# End-to-end smoke: dispatch a real run on a self-hosted worker.
# Requires a worker started in advance via `popola cloud worker start --name $WORKER`,
# a Cursor API key in the environment, and a github.com repo URL with the Cursor App installed.
popola dispatch --cloud-target=self-hosted --worker-name=$WORKER --repo-url=$REPO --cli=cursor-cloud "<prompt>"

# No-fallback contract spot-check: a missing worker name MUST exit 78
# (and stderr MUST point at `popola cloud worker start --name`).
popola dispatch --cloud-target=self-hosted --worker-name=ghost-worker "test prompt"
echo "exit_code=$?"

# Default-lane (deterministic, mock-only) regression:
pytest -m "not slow and not nightly and not real_cli and not real_lark and not real_cursor_cloud" -q --no-cov
```

The first command exercises Q-12's tier-4 smoke (5 tests; ≤ 20 live API calls; teardown archives every created agent). The second is the canonical real-world dispatch shape from the new user-guide section. The third pins the no-fallback contract from Q-7 (exit code MUST be `78`, stderr MUST contain `popola cloud worker start --name`). The fourth is the regression sweep that v0.9.x users already know — it MUST stay green after the env-shape pivot.

## Companion docs

- [`CHANGELOG.md`](CHANGELOG.md) §[1.0.0-pre.1] — full per-decision implementation delta + Q-1..Q-12 summaries
- [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md#cloud-dispatch-v100-pre1) — new "Cloud dispatch (v1.0.0-pre.1)" section (cursor-managed vs self-hosted paths, init UX, per-task override, no-fallback contract, GitHub-App prereq)
- [`docs/zh/USER_GUIDE.md`](docs/zh/USER_GUIDE.md#云端派发v100-pre1) — Chinese translation of the new user-guide section
- [`./.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md`](.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md) — Q-1..Q-12 decision log with verbatim research evidence
- [`./.local/.agent/active/v0.10.0-cloud-dispatch-clarity/PLAN.md`](.local/.agent/active/v0.10.0-cloud-dispatch-clarity/PLAN.md) — implementation plan (5 waves, 13 tasks)

## Known limitations

- **Service-account / pool-mode end-to-end claim test deferred** — research/01 PROBE_35/36 confirmed REST 201 for `env: {type:"pool"}` but did not verify a pool worker actually claims the run (no service-account key in the probe). Tracked as `BL-v1.0.0-pre.2-service-account-pool-claim`.
- **OpenAPI-vs-runtime drift on Cursor's side** — the Cursor REST gateway accepts `env: {type, name?}` + `workOnCurrentBranch:true`, but the published `cloud-agents-openapi.yaml` v1.0.0 still lists `usePrivateWorker` + `autoGenerateBranch`. PopolaLoom follows the runtime schema (validated by 22 live probes); a doc-only upstream issue is queued as `BL-v1.0.0-pre.2-openapi-upstream-issue`.
- **Non-`github.com` host pre-flight not implemented** — the GitHub-App pre-flight (Q-9) only covers `repos[0].url` hosts whose host is `github.com`. GitLab / Gitea / self-hosted git providers skip the pre-flight and fall through to the late-catch catalog rule. Tracked as `BL-v1.0.0-pre.2-non-github-host-preflight`.
- **PyPI publish remains deferred for the v0.9.x and v1.0.0-pre.x line** — use the GitHub install commands above (Q-D-5 偏离默认 carries forward from v0.9.0 GA; tracked as `BL-v0.9.x-PyPI`).

## Next steps

- v1.0.0-pre.2 will close the deferred items above (service-account pool-claim test, non-`github.com` pre-flight, OpenAPI upstream issue) and address any feedback collected on the env-shape pivot during the pre-release window.
- v1.0.0 GA tag (target: ≤ 14 days after v1.0.0-pre.1) will lock the env-shape schema as a stable surface and drop the deprecation aliases listed above (Q-2 + Q-11) on the v1.1+ schedule.
