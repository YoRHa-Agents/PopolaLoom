> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.9.1 — Self-hosted worker handoff

<!-- updated: 2026-05-09 -->

> Released: 2026-05-09

> **How to install v0.9.1** (Q-D-5 偏离默认 carries forward; PyPI promotion remains tracked as `BL-v0.9.x-PyPI`):
>
> ```bash
> # Option A — canonical, tag-pinned (always works for v0.9.1):
> pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.1
>
> # Option B — repo-root unified installer, from-git (auto-tracks main; post-tag = v0.9.1):
> ./install.sh install --from=git
> ```

## Theme

v0.9.1 is the **first SemVer-additive patch on the v0.9.x line** since the v0.9.0 GA contract published on 2026-05-08. It closes the v0.9.0 user-feedback item recorded at `.local/feedbacks/feedback_for_v0.9.0.md` ("cloud agent 模式要补充制定一个修复计划，以支持使用 self-hosted agent 即当前环境启动 agent worker 的形式启动并在网页端能实现将任务启动到云端 agent. 调研并设计实现路径，完成后升级到 0.9.1 版本.") by shipping a thin, opinionated CLI wrapper around Cursor's upstream `agent worker` CLI so an operator on this machine can register the box with the [Cloud Agents UI](https://cursor.com/agents), run health diagnostics without an API key, and emit a copy-paste-ready Cloud Agents handoff envelope — without confusing this flow with the existing `popola dispatch --cli=cursor-cloud` REST path. This release is **strictly additive**: no existing CLI verb, daemon RPC route, public Python API, or Skill front-matter key is renamed, removed, or repurposed; every existing v0.9.0 GA contract surface continues to work byte-for-byte.

The patch ships under the v0.9.x SemVer additive contract published in [`docs/API_STABILITY.md`](docs/API_STABILITY.md) §1: the new `popola cloud worker` Typer subapp + four verbs (`debug`, `start`, `status`, `handoff`) join the stable CLI surface alongside the v0.9.0 verbs without altering them; the new `worker handoff` JSON envelope (`kind: popola.cloud.worker.handoff`, `version: v0.9.1`) is a NEW additive shape governed by the same SemVer rules as `popola cloud runs --json`. The companion test surface `tests/cli/test_cloud_worker_cmd.py` adds 47 default-lane cases (47 / 47 green) on top of the v0.9.0 baseline, lifting the default-lane test count from 2659 → 2670 with the coverage gate `fail_under = 94` unchanged.

The mental-model contribution is the explicit three-lane dispatch contract enforced in CLI text + `docs/USER_GUIDE.md` + the canonical `popola-loom` Skill (Workflow 10): **(1) local agent** = `popola dispatch --cli=cursor` (subprocess, no Cloud Agents UI presence, no API key); **(2) Cloud REST** = `popola dispatch --cli=cursor-cloud` (REST-created run, popola-tracked task id, requires `CURSOR_API_KEY`); **(3) Self-hosted worker** = `popola cloud worker start` + browser/Slack/GitHub trigger (Cursor-orchestrated run, tool calls execute on this machine, popola does NOT create a task id). The `worker handoff` envelope's `popola_task_id: null` invariant + the `note` block reproducing the contract sentence-for-sentence are the No-Silent-Failures discipline applied to the dispatch lane the v0.9.0 surface intentionally did not cover.

## Highlights

### `popola cloud worker` — four-verb subcommand group (NEW; v0.9.1)

The new `popola cloud worker` Typer subapp lives at [`src/popolaloom/cli/cloud_worker_cmd.py`](src/popolaloom/cli/cloud_worker_cmd.py) and is registered under the existing `popola cloud` group via `_register_worker_subapp()` in [`src/popolaloom/cli/cloud_cmd.py`](src/popolaloom/cli/cloud_cmd.py). All four verbs route through three monkeypatchable seams (`_resolve_agent_binary`, `_run_subprocess`, `_fetch_management_endpoint`) so the test suite is hermetic — no real subprocess spawn, no real network IO in default lane.

| Verb | Purpose | Notes |
|---|---|---|
| `popola cloud worker debug` | Wraps upstream `agent worker debug` preflight | Forwards stdout/stderr verbatim. `--pool` requires `CURSOR_API_KEY` (Enterprise service-account key) — without it, exit 77 with a hint pointing at [Self-Hosted Pool docs](https://cursor.com/docs/cloud-agent/self-hosted-pool#authenticate-workers). |
| `popola cloud worker start` | Start the worker (foreground) | My Machines mode by default (browser-login auth via `agent login` is sufficient); `--pool` opts into Self-Hosted Pool. `--dry-run` prints argv with `shlex.quote` escaping and never spawns. `--management-addr <host:port>` / `:port` is opt-in (the upstream CLI doesn't bind by default). |
| `popola cloud worker status` | Probe `/healthz` + `/readyz` + `/metrics` | Default `--management-addr 127.0.0.1:39231`. Loopback-only; **no `CURSOR_API_KEY` needed**. Renders Rich table by default; `--json` emits a structured `{healthz, readyz, metrics, management_addr}` envelope. The Rich table includes `metrics.last_activity` rendered as ISO-8601 UTC so a stale heartbeat is immediately visible. |
| `popola cloud worker handoff` | Emit prompt + URL envelope | `--worker-id <uuid>` builds the canonical `https://cursor.com/agents#workerId=<uuid>` URL; `--worker-url <url>` overrides. `--prompt <text>` or `--prompt-file <path>` (mutually exclusive). Markdown by default; `--json` emits `{kind, version, title, worker_id, worker_url, prompt, popola_task_id, note}`. **`popola_task_id` is always `null`** — the explicit invariant. |

### Three dispatch lanes (mental model)

| Lane | What runs where | How you start it | Needs `CURSOR_API_KEY`? | Appears in Cloud Agents UI? | Popola task id? |
|---|---|---|---|---|---|
| Local agent | Local subprocess on this box | `popola dispatch --cli=cursor` | No | No | Yes |
| Cloud REST | Cursor-managed cloud workload | `popola dispatch --cli=cursor-cloud` (see [Cloud Agent dispatch](docs/USER_GUIDE.md#cloud-agent-dispatch-v085)) | Yes | Yes | Yes |
| Self-hosted worker (NEW v0.9.1) | Cursor cloud orchestration + tool calls executed on this box | `popola cloud worker start` + dashboard / Slack / GitHub trigger | Pool only (service-account key); My Machines accepts browser login | Yes | **No** (handoff envelope is side-effect-free) |

`popola cloud worker` deliberately does not create a Cloud Agent run — `agent worker start` registers this machine; the actual run is created from the [Cloud Agents dashboard](https://cursor.com/agents), a chat-surface trigger (Slack / GitHub / Linear), or the broad-audience `popola dispatch --cli=cursor-cloud` REST path. The `handoff` envelope makes that contract explicit so operators don't conflate it with the REST lane (`popola_task_id: null` + a verbatim `note` field). When you do want a popola-tracked task id (so `popola list` / `popola attach` work), use `popola dispatch --cli=cursor-cloud` instead — that path creates a run via REST, persists `cursor_agent_id` / `cursor_run_id` in the daemon, and surfaces the task in `popola list` with `runtime=cloud`.

### Pool-mode auth gate (No-Silent-Failures)

Cursor's upstream `agent worker --pool` is Enterprise-only and refuses anything other than a service-account API key. PopolaLoom mirrors that contract at the boundary so the failure surfaces at popola time (with a clear hint) rather than mid-spawn:

```text
$ popola cloud worker start --pool --pool-name popolaloom
error: --pool requires a Cursor service-account API key (Enterprise).
Export CURSOR_API_KEY=<service-account-key> and retry, OR drop --pool to launch
a shared 'My Machines' worker (works with `agent login`).
  see: https://cursor.com/docs/cloud-agent/self-hosted-pool#authenticate-workers
```

Exit code `77` aligns with the existing cloud-auth code used by `popola cloud runs` so scripted callers can branch on a single exit code regardless of which sub-verb hit the auth gap.

### Documentation + Skills

- **[`docs/USER_GUIDE.md`](docs/USER_GUIDE.md)** — new section *"Self-hosted worker handoff (`popola cloud worker`, v0.9.1+)"* between the v0.9.0 cloud-only init walkthrough and the v0.8.7 Cloud HITL block. Includes the three-lane mental-model table, verb reference, a 4-step bootstrap walkthrough (`debug` → `start` → `status` → `handoff`), the pool-auth-gate failure example, the `--json` status envelope shape, and the handoff envelope contract with the `popola_task_id: null` invariant called out explicitly. TOC entry added.
- **[`README.md`](README.md)** — new "Self-hosted worker handoff (v0.9.1+)" callout above the Enterprise / Self-Hosted HITL block; lists the four verbs in one line each and cross-links to the USER_GUIDE section.
- **[`src/popolaloom/skills/popola-loom/SKILL.md`](src/popolaloom/skills/popola-loom/SKILL.md)** — frontmatter `version` bumped 0.9.0 → 0.9.1 in lockstep with `popolaloom.__version__` (locked by `tests/cli/test_skill_md_canonical.py::test_skill_md_version_matches_package`). New row added to the Quick reference table; new **Workflow 10 — Self-hosted worker handoff** added in compressed form with the trigger phrases, three-lane mental model, four-verb summary, minimal command surface, and the `popola_task_id: null` invariant called out. The body-budget cap in `tests/cli/test_skill_md_canonical.py::test_skill_md_body_length_in_token_budget` is lifted **32 000 → 34 000** with an explicit v0.9.1 docstring entry (the v0.8.8 lockdown reserved this trim-vs-bump discussion for the next deliberate growth, which Workflow 10 is).
- **[`src/popolaloom/skills/install-popola/SKILL.md`](src/popolaloom/skills/install-popola/SKILL.md)** — frontmatter `version` bumped 0.9.0 → 0.9.1; both `.popola-loom-version` markers (`src/popolaloom/skills/popola-loom/.popola-loom-version` and `src/popolaloom/skills/install-popola/.popola-loom-version`) bumped 0.8.5 / 0.7.0 → 0.9.1 to match `popolaloom.__version__` (`popola doctor` drift detection now sees the synchronised value).

## Files changed (v0.9.1)

| Slice | Files |
|---|---|
| Product | `src/popolaloom/cli/cloud_worker_cmd.py` (NEW; ~700 LOC), `src/popolaloom/cli/cloud_cmd.py` (registers the new subapp via `_register_worker_subapp()`) |
| Tests | `tests/cli/test_cloud_worker_cmd.py` (NEW; 47 cases — argv construction, pool-without-key gate, `--dry-run` no-spawn, status Rich/JSON/unreachable/invalid-timeout, handoff Markdown/JSON/`worker_id` extraction, helper unit coverage, registration regression, default-addr unreachable hint), `tests/cli/test_skill_md_canonical.py` (body-budget cap 32 000 → 34 000), `tests/test_smoke.py` (asserts `__version__ == "0.9.1"` + both Skills at `version: 0.9.1`) |
| Meta | `pyproject.toml` (`version = "0.9.1"`), `src/popolaloom/__init__.py` (`__version__ = "0.9.1"`), `docs/_config.yml` (`popola_version: "0.9.1"` — pinned by `tests/docs/test_docs_contract.py::test_docs_config_version_matches_package_version`), `docs/USER_GUIDE.md` (new section + TOC entry), `README.md` (new callout), `src/popolaloom/skills/popola-loom/SKILL.md` (frontmatter version + Workflow 10 + Quick reference row), `src/popolaloom/skills/install-popola/SKILL.md` (frontmatter version), `src/popolaloom/skills/popola-loom/.popola-loom-version` (0.8.5 → 0.9.1), `src/popolaloom/skills/install-popola/.popola-loom-version` (0.7.0 → 0.9.1), `CHANGELOG.md` (new `[0.9.1]` entry), `RELEASE_NOTES.md` (this file — overwritten per v0.7.0+ policy) |

## Verification

- Default lane (all `real_*` markers deselected): `pytest -m "not slow and not real_graph and not e2e and not nightly and not real_cli and not real_lark and not real_cursor_cloud and not real_cloud_hitl" -q` → **2670 passed, 20 skipped, 87 deselected** (was 2659 at v0.9.0 GA; +11 new cases for the iteration round).
- New CLI suite: `pytest tests/cli/test_cloud_worker_cmd.py -q` → 47 / 47 green.
- Pinning regressions: `pytest tests/cli/test_skill_md_canonical.py tests/docs/ tests/test_smoke.py -q` → green (Skill version + body-budget + `_config.yml` `popola_version` + smoke test all aligned at 0.9.1).
- Lint / types: `ruff check src/popolaloom/cli/cloud_worker_cmd.py src/popolaloom/cli/cloud_cmd.py tests/cli/test_cloud_worker_cmd.py` clean (`# noqa: B008` annotations applied to `typer.Option(...)` defaults that contain mutable / callable inner values, mirroring the existing `init_cmd.py` pattern).
- Coverage gate: `[tool.coverage.report] fail_under = 94` unchanged from v0.9.0 GA; new module coverage holds the floor.
- Live smoke against a real `agent worker start` running on the dev host: `popola cloud worker debug` / `start --dry-run` / `start` (foreground) / `status --json` / `handoff --json` all confirmed — `agent worker debug` output forwards verbatim, `start` produces the upstream "Worker is now running ... Run agents: https://cursor.com/agents#workerId=<uuid>" output, `status` reports `connected: true / claimed: false / cursor_self_hosted_worker_connected: 1`, `handoff` emits the canonical envelope with `popola_task_id: null`.
- Live failure-path validation: `--pool` without `CURSOR_API_KEY` exits **77** with the canonical hint; unreachable management addr exits **1** with a default-aware hint; invalid args exit **2**.

## Status

| Capability | Status |
|---|---|
| `popola cloud worker debug` (v0.9.1; wraps `agent worker debug`) | **stable since v0.9.1** |
| `popola cloud worker start` (v0.9.1; My Machines + `--pool` modes; `--dry-run`) | **stable since v0.9.1** |
| `popola cloud worker status` (v0.9.1; `--management-addr` + `--json` + Rich table) | **stable since v0.9.1** |
| `popola cloud worker handoff` (v0.9.1; `--worker-id` / `--worker-url` + Markdown / `--json`) | **stable since v0.9.1** |
| `popola.cloud.worker.handoff` JSON envelope (`kind`/`version`/`worker_id`/`worker_url`/`prompt`/`popola_task_id`/`note`) | **stable since v0.9.1** |
| Three-lane dispatch contract (local agent / Cloud REST / self-hosted worker) | **stable since v0.9.1** (documented in USER_GUIDE + Skill Workflow 10) |
| Every v0.9.0 GA stable surface (CLI verbs, daemon RPC, public Python API, Skill front-matter) | **stable** (carries forward unchanged) |

## Upgrade notes

1. **v0.9.1 is strictly additive** — no v0.9.0 surface (CLI verb, daemon RPC route, public Python API, Skill front-matter key) is changed. Operators upgrading from v0.9.0 see zero behavioural drift in any existing flow.
2. **Install via `pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.1`** (canonical, tag-pinned) or `./install.sh install --from=git` (auto-tracks main; post-tag = v0.9.1). The Q-D-5 偏离默认 PyPI deferral carries forward — the v0.9.x patch that promotes to PyPI (`BL-v0.9.x-PyPI`) will land a follow-on top-of-file callout.
3. **`popola cloud worker` is opt-in** — it does NOT replace `popola dispatch --cli=cursor-cloud` (REST). The three-lane mental-model in [`docs/USER_GUIDE.md#self-hosted-worker-handoff-popola-cloud-worker-v091`](docs/USER_GUIDE.md#self-hosted-worker-handoff-popola-cloud-worker-v091) and Skill Workflow 10 explain when to use which.
4. **`--pool` requires a Cursor service-account API key** (Enterprise) — user / personal / team API keys do NOT authenticate pool workers per Cursor's [Self-Hosted Pool docs](https://cursor.com/docs/cloud-agent/self-hosted-pool#authenticate-workers). PopolaLoom enforces this at the boundary with exit `77`.
5. **`worker handoff` does NOT create a popola task id** — that's the explicit invariant. Use `popola dispatch --cli=cursor-cloud` for popola-tracked Cloud Agent runs.

## Known limitations (carry-forward + new)

- **PyPI publish still deferred** (Q-D-5 偏离默认; `BL-v0.9.x-PyPI`) — see callout at top of v0.9.0 RELEASE_NOTES history in [`CHANGELOG.md`](CHANGELOG.md). v0.9.1 ships GitHub-Release-only.
- **No daemon-side runtime state for self-hosted workers** — the v0.9.1 patch deliberately does not add `runtime=worker` to `popola list`; the worker has no popola task id and the `agent worker` lifecycle is owned by the upstream CLI. A future minor (v0.10.x) may revisit this once Cursor's Cloud Agents API exposes stable `usePrivateWorker` / `labels` REST routing fields beyond what the [public OpenAPI](https://cursor.com/docs-static/cloud-agents-openapi.yaml) surfaces today.
- **Six v0.8.8.1 minor findings carry forward** — see [`docs/MIGRATION_v07_to_v09.md`](docs/MIGRATION_v07_to_v09.md) §"Known Limitations / v0.9.x backlog" for the full list (`cloud.run_index_reconciled` rate-limit risk, per-task mutex on the audit log writer `BL-v0.9-1`, audit log GC `BL-v0.8.9-2`, custom `detect-secrets` plugins for Cursor / Lark `BL-v0.8.9-1`, cross-verb exit-code divergence, `cloud.sse.*` payload shape evolution as experimental per `API_STABILITY.md` §3.4).

## Branch / PR readiness

Suggested release PR title: **`release: v0.9.1 — Self-hosted worker handoff (popola cloud worker; closes feedback_for_v0.9.0)`**.

Branch: `release/v0.9.1` — squash-merge into `main` via PR after default-lane CI lights green; tag and ship via the v0.7.0+ GitHub Release flow.

For the user-feedback that anchors this release see `.local/feedbacks/feedback_for_v0.9.0.md`. The plan + decision matrix for the v0.9.1 implementation lives at `.local/.agent/active/v0.9.1-self-hosted-worker/PLAN.md` (research note, local-only — `.local/` is gitignored).
