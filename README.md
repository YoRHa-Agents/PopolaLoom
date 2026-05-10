# PopolaLoom

<!-- updated: 2026-05-10 -->

> **v0.9.7 (2026-05-10)** — Current GitHub Release. v0.9.7 is a strictly additive patch on top of v0.9.6 that closes [`./.local/feedbacks/feedback_for_v0.9.4.md`](.local/feedbacks/feedback_for_v0.9.4.md) line 1 ("popola 不使用 pip 修正安装方式" + "init 阶段给出，本地需要能存储并加密"). Four production WARN / error paths used to recommend `pip install popolaloom[credentials]` whenever the OS keyring extra was missing — conflicting with the workspace rule against surfacing raw `pip install` commands. v0.9.7 introduces `./install.sh install --with-credentials` (rolls the optional `keyring>=25` extra into the same install via PEP 508 `pkg[extras] @ <url>`) and rewrites every production WARN / error path to point at it; headless containers without a SecretService backend get an explicit `CURSOR_API_KEY` env / 0o600 `.env` fallback hint (`credentials.py` precedence #2). `POPOLA_INSTALL_SCRIPT_VERSION` bumps `0.9.6 → 0.9.7`. **GitHub Release-only** (PyPI publish still deferred per `BL-v0.9.x-PyPI`) — install v0.9.7 via `./install.sh install` (canonical), `./install.sh install --ref=v0.9.7` (pinned), `./install.sh install --with-credentials` (canonical + keyring extra), or `pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.7` (manual fallback) — see [`RELEASE_NOTES.md`](RELEASE_NOTES.md). v0.9.6 still works via `./install.sh install --ref=v0.9.6`. Upgrades from v0.7.x still walk through [`docs/MIGRATION_v07_to_v09.md`](docs/MIGRATION_v07_to_v09.md).

[![Status](https://img.shields.io/badge/status-GA-brightgreen.svg)](#status) [![Coverage](https://img.shields.io/badge/coverage-94%25%2B-brightgreen.svg)](#status) [![License](https://img.shields.io/badge/license-MIT-blue.svg)](#license) [![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

## What is PopolaLoom?

PopolaLoom is a local-first **meta-orchestrator** that sits on top of every agent CLI on your machine — Cursor, Claude Code, Codex, Kimi, GitHub Copilot — and gives them a single dispatch surface, a persistent task bus, and a unified HITL channel. Think of it as the "loom" (织机) that weaves N agent CLIs into one coherent run-graph: each per-task strand is its own subprocess managed by the `popolad` UDS daemon, and the loom keeps the whole tapestry surviving across terminals, SSH sessions, and machine reboots.

It is the multi-task / multi-CLI sibling of [DevolaFlow](https://github.com/YoRHa-Agents/DevolaFlow) (the per-task quality framework) and ships **vendored** with the relevant subset of [ArkTower](https://github.com/YoRHa-Agents/ArkTower) (the task pool / SQLite persistence / EventBus). The end result: the tag-pinned install on a fresh machine gives you `popola dispatch <prompt> --cli=cursor` + `popola attach <task_id> --follow` + `popola doctor` with zero sibling clones, plus a Skill that auto-discovers in any host agent so the CLI is also reachable as natural-language verbs ("派发任务给 cursor 跑 X", "list my running agents", "popola doctor").

## Why PopolaLoom?

- **Single dispatch surface** for every local agent CLI — `popola dispatch "..." --cli=cursor|claude|codex|kimi|copilot` instead of N separate command shapes.
- **Cross-terminal task survival** via the `popolad` UDS daemon (`start_new_session=True`, persistent SQLite task pool) — close your shell, SSH disconnect, machine reboot; the task survives until you `popola attach` from anywhere.
- **Vendored ArkTower** task pool + EventBus + 4 SQL migrations under `popolaloom._vendored.arktower` — no sibling repo required for `pip install`.
- **HITL across 5 channels** (Lark / IDE / CLI / MCP / Web) — LangGraph `interrupt()` broadcasts to all 5; first responder wins via cross-channel atomic `mark_answered`.
- **Auto-discovery via Skill convention** — `popola init` writes the canonical `SKILL.md` into every detected IDE (`~/.cursor/skills/popola-loom/`, `~/.claude/skills/popola-loom/`, `$CODEX_HOME/skills/popola-loom/`, `<repo>/.github/copilot-instructions.md`), and host agents auto-load it.
- **8-dim self-eval baseline** (`popola eval run`) — PopolaLoom-nines composite over `dispatch_isolation / cycle_convergence / hitl_latency / attach_correctness / cross_cli_handoff / single_threaded_writes / event_log_completeness / hitl_handleability`, with per-dimension evidence pipelines.
- **MCP-native** — exposes 9 dispatch / inspect / HITL verbs over stdio (`popola_submit / popola_list / popola_status / popola_attach_stream / popola_cancel / popola_relay / popola_supervise / popola_supply_feedback / popola_inject_subtask`) so any MCP-aware IDE can call them as tools.

## 5-minute Quickstart

<!-- updated: 2026-05-10 -->

```bash
# v0.9.6 install — Q-D-5 偏离默认 carries forward: PyPI deferred to v0.9.x; see BL-v0.9.x-PyPI in TRACKER.
# v0.9.6 flips the install.sh default from --from=pypi to --from=git so a fresh
# ./install.sh install works without PyPI (closes feedback_for_v0.9.4 lines 2-5).
./install.sh install                 # canonical (default --from=git, tracks main)
./install.sh install --ref=v0.9.6    # canonical tag-pinned (recommended for v0.9.6)
# OR (manual fallback):
pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.6

popola init                          # auto-detect Cursor / Claude / Codex / Copilot
popola popolad start                 # boot the UDS daemon
popola dispatch "echo hello popola" --cli=cursor
popola list                          # see active tasks
popola attach <task_id> --follow     # tail SSE event stream
popola doctor                        # 4-subsystem health check
```

Or run the automated 6-step local-CLI smoke (now includes `popola init --dry-run` at Step 0):

```bash
bash examples/quickstart.sh
```

For cloud-only teams (no local CLIs) the copy-paste-ready cloud-agent walkthrough is shipped at the repo root:

```bash
export CURSOR_API_KEY="cr_..."          # required for cloud dispatch
./cloud-quickstart.sh                   # init --target=cloud-only → daemon → dispatch → attach → cloud runs
```

For the long version with explanations, see [`docs/QUICKSTART.md`](docs/QUICKSTART.md). For v0.7.x → v0.9.0 upgraders: [`docs/MIGRATION_v07_to_v09.md`](docs/MIGRATION_v07_to_v09.md). For the v0.9.x SemVer contract: [`docs/API_STABILITY.md`](docs/API_STABILITY.md).

## CLI verbs at a glance

| Verb | Purpose | Quick example |
|---|---|---|
| `popola dispatch <prompt> --cli=<name>` | Spawn a task on the named adapter | `popola dispatch "fix bug in foo.py" --cli=cursor` |
| `popola list [--all]` | List active (or all) tasks | `popola list --all` |
| `popola status <task_id>` | Single-task state envelope | `popola status cursor-23e74ec18917` |
| `popola attach <task_id> --follow` | Tail the SSE / NDJSON event stream | `popola attach <id> --follow` |
| `popola cancel <task_id>` | SIGTERM → 5s grace → SIGKILL | `popola cancel <id>` |
| `popola probe` | Daemon liveness (pid + uptime + active count) | `popola probe` |
| `popola popolad {start,stop,status}` | Daemon lifecycle | `popola popolad start` |
| `popola init [<ide>] [--global / --project]` | Multi-IDE Skill installer (idempotent) | `popola init cursor --global` |
| `popola init --interactive` | Human-driven setup wizard (v0.5.5+) | `popola init --interactive` |
| `popola auth cursor {set,status,clear}` | Secure Cursor API key storage in the OS keyring (v0.9.2+; install the optional extra via `./install.sh install --with-credentials` v0.9.7+ or `pip install 'popolaloom[credentials]'`) | `popola auth cursor set --validate` |
| `popola doctor [--strict] [--json]` | 4-subsystem health (skill / daemon / lark / arktower) | `popola doctor --strict` |
| `popola eval run --output PATH` | 8-dim PopolaLoom-nines self-eval | `popola eval run -o /tmp/nines.toml` |
| `popola version` | Print `popolaloom <version>` | `popola version` |

Full reference + `--cli-flag KEY=VAL` adapter passthrough table: [`docs/USER_GUIDE.md#cli-verb-reference`](docs/USER_GUIDE.md#cli-verb-reference).

### Adapter passthrough (`--cli-flag KEY=VAL`)

Each agent CLI exposes its own flags; PopolaLoom passes them through verbatim via `--cli-flag KEY=VAL` (repeatable; values are JSON-parsed first, then fall back to strings):

```bash
# Cursor stream-json output mode
popola dispatch "design caching layer" --cli=cursor \
  --cli-flag output_format=stream-json

# Claude with a pre-allocated session_id + max-turns cap
popola dispatch "refactor module X" --cli=claude \
  --cli-flag session_id="$(uuidgen)" \
  --cli-flag max_turns=10

# Codex sandboxed to read-only (review tasks)
popola dispatch "review src/foo.py for bugs" --cli=codex \
  --cli-flag sandbox=read-only
```

Supported KEYs per adapter: see [`docs/USER_GUIDE.md#adapter-passthrough`](docs/USER_GUIDE.md#adapter-passthrough).

### Cloud Agent dispatch (v0.8.5+)

<!-- updated: 2026-05-09 -->

PopolaLoom now speaks Cursor’s Background Agent REST alongside the historical local CLIs:

- **Invocation**: start `popolad`, configure a Cursor API key (`export CURSOR_API_KEY=...` OR `popola auth cursor set` for OS-keyring storage; v0.9.2+), then `popola dispatch "<prompt>" --cli=cursor-cloud --cli-flag repo_url=https://github.com/you/repo [--cli-flag model=composer-2 ...]`.
- **Self-hosted routing**: add `--cli-flag pool_name=<pool>`, `--cli-flag worker_name=<worker>`, or `--cli-flag 'labels={"pool":"<pool>"}'` to request Cursor REST `usePrivateWorker=true` and label-based worker selection.
- **Why daemon still matters**: the supervisor swaps `Popen` for `CloudCursorClient.create_agent(...)`, persists `cursor_agent_id` / `cursor_run_id`, and tracks phases via the cloud poller so `attach`/`status`/`cancel` semantics stay cohesive with local workloads.
- **Observability**: browser dashboard **https://cursor.com/dashboard/cloud-agents** complements `popola status <task>` (shows `runtime=cloud`). v0.8.6+ also surfaces a default-on `runtime` column in `popola list` so local vs cloud rows are distinguishable at a glance — pass `--no-runtime` to hide it.
- **Live attach (v0.8.6+)**: `popola attach <id> --follow` for `runtime=cloud` tasks now ingests Cursor's SSE stream by default and auto-falls back to the legacy poll-only view on `410 stream_expired` / network errors; pass `--no-stream` to force the legacy path. See [`docs/USER_GUIDE.md#sse-ingest-v086`](docs/USER_GUIDE.md#sse-ingest-v086) for the full contract (incl. ≤3 s tolerated divergence between SSE and `cloud_phase`).
- **Opt-in QA**: `pytest tests/real_cursor_cloud/ -m real_cursor_cloud` only after exporting the API key — four cheap tests (create+cancel sentinel, metadata GETs, bogus-key auth assertion); default CI lane **deselects** `-m real_cursor_cloud`.

### Self-hosted worker handoff (v0.9.1+)

<!-- updated: 2026-05-10 -->

`popola cloud worker` wraps the upstream Cursor `agent worker` CLI so an operator on this machine can register the box with Cursor's [Cloud Agents UI](https://cursor.com/agents), hand off a dashboard prompt, or directly create a PopolaLoom-tracked REST dispatch that targets the workspace worker. `start` is reuse-first per workspace: when `--name` is omitted PopolaLoom passes a deterministic name like `popolaloom-<repo>-<hash>`, and a second start for the same resolved `--worker-dir` reuses the running worker unless `--allow-duplicate` is explicitly passed.

- `popola cloud worker debug` — preflight (auth method, repo label, visibility probe).
- `popola cloud worker start` — launch the worker (My Machines mode by default; `--pool` requires a service-account `CURSOR_API_KEY` per Cursor's Self-Hosted Pool contract).
- `popola cloud worker status` — read `/healthz` + `/readyz` + `/metrics` from the worker's optional management server (loopback only; no API key needed).
- `popola cloud worker handoff` — emit a copy-paste-ready prompt + URL envelope for the dashboard handoff (the envelope explicitly notes that no popola task id is created).
- `popola cloud worker dispatch` — directly dispatch through `popolad` with `cli=cursor-cloud` and `worker_name=<name>` routing extras; use `--print-only` / `--dry-run` to preview the equivalent command.

`popola dispatch --cli=cursor-cloud` remains the generic REST path; `popola cloud worker dispatch` is the workspace-worker-targeted convenience wrapper around the same daemon endpoint. Full reference: [`docs/USER_GUIDE.md#self-hosted-worker-handoff-popola-cloud-worker-v091`](docs/USER_GUIDE.md#self-hosted-worker-handoff-popola-cloud-worker-v091).

> **Enterprise / Self-Hosted (v0.8.7+) — production cloud HITL with Lark approvals.** The broad-audience cloud-dispatch path above does not require any HITL prerequisites. To wire a Cursor Cloud Agent to defer high-stakes decisions to a human via Lark over the new MCP tool `popolaloom_cloud_hitl_request`, you need a Cursor **Self-Hosted Pool worker** (γ — first-class) or a **public HMAC-protected HTTPS gateway** (β — backup) per the v0.8.7 deployment-modes contract. See [`docs/USER_GUIDE.md#cloud-hitl-enterprise--self-hosted`](docs/USER_GUIDE.md#cloud-hitl-enterprise--self-hosted) for the install steps, topology diagrams, egress allowlist, secret-rotation runbook, and the L6 / L8 / L10 hardening callouts. If you have neither a self-hosted worker option nor a public HTTPS gateway, residential NAT / port-forward setups are **not supported** — see [`docs/known-issues.md` §"v0.8.7 — Cloud HITL transport (anti-patterns)"](docs/known-issues.md#v087--cloud-hitl-transport-anti-patterns).

Example `popola list` rendering with the v0.8.6+ `runtime` column (between `task_id` and `cli`):

```text
$ popola list
┃ task_id              ┃ runtime ┃ cli           ┃ state    ┃ pid    ┃ started_at                  ┃
│ task-local-001       │ local   │ cursor        │ running  │ 4242   │ 2026-05-08T10:00:00.000+00:00│
│ task-cloud-002       │ cloud   │ cursor-cloud  │ running  │ -      │ 2026-05-08T10:01:00.000+00:00│
```

Hands-off envelopes still publish for bookkeeping, but spawned remote agents consume prompt via Cursor’s infra — see [`RELEASE_NOTES.md`](RELEASE_NOTES.md) §Highlights.

## Hands-off envelope (v0.7.1+ foundation, v0.7.2+ E3, v0.7.3+ replay/feedback)

Starting in v0.7.2, every `popola dispatch` call writes a **file-based handoff envelope** to `.local/.agent/handoff/<handoff_id>.md` (gitignored) and injects `POPOLA_HANDOFF_FILE` + `POPOLA_HANDOFF_ID` into the spawned sub-CLI's environment. The envelope is a Markdown file with YAML front-matter (cat-friendly debugging) and the prompt as body — auditable, replayable, addressable by content-derived id (`<cli>-<slug>-<8hex>`).

```bash
# Each dispatch writes an envelope (and the sub-CLI sees it via env var)
popola dispatch "fix bug in foo.py" --cli=cursor
popola handoff list
# ┃ handoff_id                             ┃ size  ┃ mtime               ┃ ...
# │ cursor-fix-bug-in-foo-py-3a7f9c1d      │ 412 B │ 2026-05-06 14:30:00 │
popola handoff show cursor-fix-bug-in-foo-py-3a7f9c1d
# (raw Markdown — front-matter + prompt body)

# Replay the same dispatch later (v0.7.3+) — exact re-run, no retyping
popola dispatch --replay cursor-fix-bug-in-foo-py-3a7f9c1d

# Snapshot a finished task's envelope to .local/.agent/archive/<task_id>/
popola handoff archive cursor-fix-bug-in-foo-py-3a7f9c1d cursor-23e74ec18917
```

The envelope is the **single source of truth** for dispatch payloads (E3 internal unification — `dispatch_task` is now a thin wrapper that builds an envelope and delegates to `dispatch_with_envelope`). HITL feedback travels in the same shape via `FeedbackEnvelope` (Q7=yes, v0.7.3+). The legacy `RelayHandoffEnvelope` (v0.3.0) is bridge-converted via `to_handoff_envelope()` so existing relay-based code paths keep working unchanged. Full design: [`docs/USER_GUIDE.md#hands-off-envelope`](docs/USER_GUIDE.md#hands-off-envelope).

## Documentation

| Doc | Audience | Purpose |
|---|---|---|
| [`docs/QUICKSTART.md`](docs/QUICKSTART.md) | First-time users | 5-minute onboarding (install → first task) |
| [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) | Operators | Full reference (CLI verbs, MCP, HITL, Lark, config) |
| [`docs/DEMO.md`](docs/DEMO.md) | Evaluators | Product demo, example outputs, design rationale, implementation flow |
| [`docs/index.md`](docs/index.md) | Web visitors | GitHub Pages landing page (`https://YoRHa-Agents.github.io/PopolaLoom/`) |
| [`RELEASE_NOTES.md`](RELEASE_NOTES.md) | Release watchers | LATEST version's release notes (overwritten per release) |
| [`CHANGELOG.md`](CHANGELOG.md) | Archaeology | Full historical archive of every version |
| [`VENDORING.md`](VENDORING.md) | Maintainers | ArkTower vendoring policy + refresh procedure |

## Status

**v0.9.7 — current release (2026-05-10).** v0.9.7 is a strictly additive patch on the v0.9.0 GA SemVer contract in [`docs/API_STABILITY.md`](docs/API_STABILITY.md): only the credential WARN / install surface is touched — four production WARN / error paths (`credentials._keyring_set`, `cli.init_cmd._persist_cursor_api_key_noninteractive`, `cli.init_cmd._offer_cursor_credential_setup`, `cli.auth_cmd._fail_no_keyring`) used to recommend `pip install popolaloom[credentials]`; v0.9.7 introduces `./install.sh install --with-credentials` (rolls the optional `keyring>=25` extra into the same install via PEP 508 `pkg[extras] @ <url>`) and rewrites every WARN to point at it, with an explicit `CURSOR_API_KEY` env / 0o600 `.env` fallback hint for headless containers without a SecretService backend. `POPOLA_INSTALL_SCRIPT_VERSION` 0.9.6 → 0.9.7. Every other v0.9.0 / v0.9.1 / v0.9.2 / v0.9.3 / v0.9.4 / v0.9.5 / v0.9.6 stable CLI verb, daemon RPC route, public Python API, and Skill front-matter key remains intact. v0.7.x → v0.9.0 migration remains [`docs/MIGRATION_v07_to_v09.md`](docs/MIGRATION_v07_to_v09.md). **For v0.9.7**: install via `./install.sh install` (canonical), `./install.sh install --ref=v0.9.7` (pinned), `./install.sh install --with-credentials` (canonical + keyring extra), or `pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.7` (manual fallback). PyPI publish remains deferred to a v0.9.x patch per Q-D-5 偏离默认 (see `BL-v0.9.x-PyPI` in TRACKER); pass `--from=pypi --version=0.9.x` once that patch lands.

| Capability | Status |
|---|---|
| popolad daemon (UDS RPC, 7 dispatch verbs) | OK live |
| 7 dispatch primitives (dispatch / attach / probe / relay / supervise / federate / cancel) | OK live |
| MCP stdio server (Cursor / Claude IDE) | OK live |
| LangGraph dev/test subgraph + HITL `interrupt()` | OK live |
| ArkTower task pool persistence (cross-restart rehydrate) | OK live |
| HITL handle-ability (5 channels: lark / ide / cli / mcp / web) | OK live |
| Lark bidirectional (out: `+send --card`, in: `event consume` listener) | OK live |
| 8-dim PopolaLoom-nines self-eval | OK live |
| `popola init` 8 verbs + 8 modifiers (Cursor / Claude / Codex / Copilot / local / all) | OK live |
| `popola init --interactive` wizard | OK live (v0.5.5+) |
| Canonical SKILL.md auto-loaded by host agents | OK live |
| `install-popola` Skill (mirrors `/install-devola-flow`) | OK live (v0.7.0+) |
| Single floating `RELEASE_NOTES.md` (per-version files retired) | OK live (v0.7.0+) |
| `.local/` is gitignored (local-only working surface) | OK live (v0.7.0+) |
| GitHub Pages site (`docs/index.md` + `docs/_config.yml`) | OK live |
| **v0.7.1**: BUG-A/B/C fixed (cancel orphan, rehydrate spawn-aborted, attach `--no-follow` EOF) | OK live |
| **v0.7.2 / v0.8.0**: `popolaloom.handoff` module (HandoffEnvelope schema_v1 + writer + archive + loader) | OK live (100% cov) |
| **v0.7.2 / v0.8.0**: `Popolad.dispatch_with_envelope` (E3 internal unification, all dispatch goes through one path) | OK live |
| **v0.7.2 / v0.8.0**: C5 双通道 (env primary + flag opt-in forward-compat) | OK live |
| **v0.7.2 / v0.8.0**: `popola handoff list / show / archive` CLI | OK live |
| **v0.7.3 / v0.8.0**: `popola dispatch --replay <handoff_id>` | OK live |
| **v0.7.3 / v0.8.0**: `FeedbackEnvelope` (Q7=yes HITL feedback foundation) | OK live (writer only; live `--persist` deferred) |
| **v0.7.3 / v0.8.0**: `to_handoff_envelope(relay_env)` legacy bridge | OK live |
| **v0.8.4**: `install.sh` unified installer (install / update / uninstall × global / project × cursor/claude/codex/copilot) | OK live |
| **v0.8.4**: `popola skill uninstall --target=<...>` Typer verb | OK live |
| **v0.8.5**: `--cli=cursor-cloud` + daemon cloud poller + `/hitl/cloud/*` RPC + `CURSOR_API_KEY` | OK live (`cursor` local path unchanged) |
| **v0.8.5**: Tier-4+ `pytest -m real_cursor_cloud` quartet (skipped unless env var opt-in) | OK live |
| **v0.8.6**: SSE ingest, `runtime` column in `popola list`, 16-entry bilingual error catalog | OK live |
| **v0.8.7**: `popolaloom_cloud_hitl_request` MCP tool + `cloud_hitl_request_card_v1` Lark card + `[hitl.cloud]` config | OK live (γ first-class) |
| **v0.8.8**: Multi-run sextuple identity + `popola cloud runs` + `popola status --verbose` cost + `[cloud.backoff]` / `[cloud.busy_strategy]` + `popola relay` (auto-default + 5 mitigations) | OK live |
| **v0.9.0 GA**: API stability boundary ([`docs/API_STABILITY.md`](docs/API_STABILITY.md)) + v0.7.x → v0.9.0 migration guide ([`docs/MIGRATION_v07_to_v09.md`](docs/MIGRATION_v07_to_v09.md)) | OK live |
| **v0.9.0 GA**: `popola init --target=cloud-only` (Q-D-4 偏离默认) + `cloud-quickstart.sh` + `tests/fixtures/` SHA-256 hash-lock + scheduled drift workflow + `--cov-fail-under=94` codified | OK live |
| **v0.9.1**: `popola cloud worker {debug,start,status,handoff}` self-hosted worker handoff + three-lane dispatch model | OK live |
| **v0.9.2**: `popola auth cursor {set,status,clear}` + OS-keyring-backed Cursor API key resolver | OK live |
| **v0.9.3**: workspace worker singleton reuse + direct `popola cloud worker dispatch` + private-worker routing extras | OK live |
| 2325+ default-lane tests / 94%+ coverage (codified in `pyproject.toml`) | OK live |

## Architecture (TL;DR)

```text
Cursor / Claude / Codex / Copilot IDE  ─┐
   ↑ SKILL.md auto-discovery            ├─→ popolaloom-mcp (stdio)  ─┐
   ↑ (popola init writes here)          ┘                              ├─→ popolad daemon (UDS)
                                                                       │      ├─ ArkTower task pool (vendored, SQLite)
$ popola CLI  ────────────────────────────────────────────────────────┘      ├─ LangGraph subgraph + interrupt()
$ lark-cli (out: +send --card) ←─── HITL renderer + Lark notifier ◄───┐      ├─ NDJSON event log (CloudEvents)
$ lark-cli event consume (in)  ───→ LarkSupervisor ──────────────────┤      └─ 8-dim self-eval runner
                                                                       └────────► popola doctor (skill / daemon / lark / arktower)
```

See [`docs/DEMO.md`](docs/DEMO.md) for example outputs and full session walkthroughs; see [`docs/USER_GUIDE.md#architecture-deep-dive`](docs/USER_GUIDE.md#architecture-deep-dive) for the prose deep dive.

## Install

### v0.9.6 — canonical install (install.sh default --from=git; Q-D-5 偏离默认)

> **v0.9.6 recipe** — PyPI publish remains deferred to a v0.9.x patch (Q-D-5 偏离默认; see `BL-v0.9.x-PyPI` in TRACKER), but starting in v0.9.6 the official `./install.sh` bootstrap **no longer defaults to PyPI**: `--from` defaults to `git` and the canonical tag-pinned recipe is `./install.sh install --ref=v0.9.6` (closes [`./.local/feedbacks/feedback_for_v0.9.4.md`](.local/feedbacks/feedback_for_v0.9.4.md) lines 2-5). A fresh `./install.sh install` works without PyPI on Chinese pip mirrors that don't carry `popolaloom` yet. Both shapes are also documented in [`docs/MIGRATION_v07_to_v09.md`](docs/MIGRATION_v07_to_v09.md#tldr) for v0.7.x upgraders.

```bash
# Canonical v0.9.6 install (default --from=git, tracks main):
./install.sh install

# Canonical tag-pinned v0.9.6 install (recommended for reproducible installs):
./install.sh install --ref=v0.9.6

# Manual fallback — directly via pip (always-works, tag-pinned):
pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.6
```

### One-line install (v0.8.4+; v0.9.6 default --from=git)

```bash
# v0.9.6 default — tracks latest main from GitHub (no PyPI required)
curl -fsSL https://raw.githubusercontent.com/YoRHa-Agents/PopolaLoom/main/install.sh | bash
# or, with options:
curl -fsSL https://raw.githubusercontent.com/YoRHa-Agents/PopolaLoom/main/install.sh | bash -s -- install --scope=global --target=all
# or, tag-pinned for reproducibility:
curl -fsSL https://raw.githubusercontent.com/YoRHa-Agents/PopolaLoom/main/install.sh | bash -s -- install --ref=v0.9.6
```

The unified `install.sh` (script version v0.9.6+) wraps `pip install` + `popola skill install` + `popola popolad start` + `popola doctor` in a single shell command. Options: `--scope=global|project`, `--target=cursor|claude|codex|copilot|all`, `--from=git|pypi|<path>` (default: `git`), `--ref=<tag|branch|sha>` (only with `--from=git`), `--version=X.Y.Z` (only with `--from=pypi`), `--no-skills`, `--no-daemon`, `--dry-run`. Run `./install.sh --help` for the full matrix; the script is idempotent and safe to re-run for upgrades. **For v0.9.6 specifically use `./install.sh install` (canonical), `./install.sh install --ref=v0.9.6` (pinned), or `pip install git+...@v0.9.6` (manual fallback)** — `--version=` requires `--from=pypi` (which delivers v0.8.x today; v0.9.x once `BL-v0.9.x-PyPI` lands).

### Update / Uninstall

```bash
./install.sh update                       # pip upgrade + popola skill upgrade
./install.sh uninstall --yes              # remove Skills + pip uninstall popolaloom
./install.sh uninstall --yes --purge      # also delete ~/.popola/ daemon state
```

### Manual install (alternative)

```bash
# v0.9.6 canonical path — install directly from the GitHub Release tag.
# Q-D-5 偏离默认: PyPI deferred to v0.9.x; see BL-v0.9.x-PyPI in TRACKER.
pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.6

# OR from a clone (development):
git clone https://github.com/YoRHa-Agents/PopolaLoom.git && cd PopolaLoom
pip install -e ".[dev]"
```

> Once v0.9.x publishes to PyPI (`BL-v0.9.x-PyPI`), `pip install popolaloom` will work directly. **For v0.9.6 users, use the git URL above** — `pip install popolaloom` (no `git+`) currently resolves to the previous v0.8.x line.

### Cloud-only install (v0.9.0+)

For teams running exclusively on Cursor Cloud Agents (no local CLIs), use the cloud-only scaffold + the bundled cloud quickstart:

```bash
mkdir my-cloud-project && cd my-cloud-project
popola init --target=cloud-only
# OR run the copy-paste-ready quickstart shipped at repo root:
./cloud-quickstart.sh                     # idempotent; checks CURSOR_API_KEY + popola on PATH
```

See [USER_GUIDE — `popola init --target=cloud-only`](docs/USER_GUIDE.md#popola-init---targetcloud-only-v090) for the 3-file scaffold (`popolad.toml` / `.env.example` / `Makefile`), and [`cloud-quickstart.sh`](cloud-quickstart.sh) for the full dispatch → attach → cloud runs walkthrough.

Verify the install:

```bash
python -c "import popolaloom; print(popolaloom.__version__)"   # → 0.9.6
which popola                                                    # → /usr/local/bin/popola (or similar)
popola version                                                  # → "popolaloom 0.9.6"
```

If `popola: command not found` after install, your shell's PATH may not include `~/.local/bin`. Quick fix:

```bash
export PATH="$HOME/.local/bin:$PATH"
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

After pip install, register the Skill into every IDE you use via the existing `popola init` wizard (works alongside `install.sh`):

```bash
popola init                  # auto-detect Cursor / Claude / Codex / Copilot
popola init all --global     # explicit: install for every IDE at user-home scope
```

For step-by-step install with troubleshooting, see [`docs/QUICKSTART.md`](docs/QUICKSTART.md). For an LLM-driven install workflow, ask any host agent (Cursor / Claude / Codex / Copilot) `install popola` — the `install-popola` Skill (v0.7.0+; see `src/popolaloom/skills/install-popola/SKILL.md`) walks them through it.

> **Packaging note**: PopolaLoom vendors the ArkTower subset required for task persistence, so a wheel install does not need a sibling ArkTower checkout. If ArkTower later becomes a normal package dependency, [`VENDORING.md`](VENDORING.md) documents how to retire the vendored copy.

## v0.9.7 highlights

<!-- updated: 2026-05-10 -->

v0.9.7 closes [`./.local/feedbacks/feedback_for_v0.9.4.md`](.local/feedbacks/feedback_for_v0.9.4.md) line 1 ("popola 不使用 pip 修正安装方式" + "init 阶段给出，本地需要能存储并加密"). Four production WARN / error paths in `popolaloom.credentials` / `popolaloom.cli.init_cmd` / `popolaloom.cli.auth_cmd` used to recommend `pip install popolaloom[credentials]` whenever the OS keyring extra was missing — conflicting with the workspace rule that says PopolaLoom should not surface raw `pip install` commands to operators. v0.9.7 introduces a single new installer flag and rewrites every WARN to point at it:

- **New `./install.sh install --with-credentials` flag** — opt-in flag that appends the optional `[credentials]` extra (Python `keyring>=25`) to the resolved install spec via PEP 508 `pkg[extras] @ <url>`. Composes with all three `--from` modes (PyPI / git / local path) and is also accepted by `update`. `--with-credentials` is rejected on `uninstall` (loud-fail per **No Silent Failures**, mirrors `--ref` / `--version` semantics).
- **Four production WARN / error paths now drop `pip install popolaloom[credentials]`** — `credentials._keyring_set` (the `CredentialBackendError` raised from `popola auth cursor set` / init-time persistence), `init_cmd._persist_cursor_api_key_noninteractive` (the WARN operators hit when running `popola init --cursor-api-key-file <path>` without a keyring backend), `init_cmd._offer_cursor_credential_setup` (the interactive `popola init --target=cloud-only --configure-cursor-auth` walkthrough), and `auth_cmd._fail_no_keyring` (called from `popola auth cursor {set,clear,status --json}`). Every replacement points operators at `./install.sh install --with-credentials` AND surfaces the `CURSOR_API_KEY` env / 0o600 `.env` fallback (`credentials.py` precedence #2). Headless Linux containers without a SecretService backend get an explicit "the install path succeeds but the keyring lookup still misses" sentence.
- **`POPOLA_INSTALL_SCRIPT_VERSION` 0.9.6 → 0.9.7** — bash bootstrap surface change advertised explicitly so operators know which behavior they're getting from `install.sh version`.
- **Strictly additive patch** — every v0.9.0 / v0.9.1 / v0.9.2 / v0.9.3 / v0.9.4 / v0.9.5 / v0.9.6 stable CLI verb, daemon RPC route, public Python API, and Skill front-matter key remains intact. The v0.9.5 `popola init --cursor-api-key` / `--cursor-api-key-file` flags continue to work byte-for-byte; v0.9.6's `--from=git` default + `--ref=<tag|branch|sha>` flag are unchanged. Full details: [`CHANGELOG.md`](CHANGELOG.md) and [`RELEASE_NOTES.md`](RELEASE_NOTES.md).

## v0.9.3 highlights

<!-- updated: 2026-05-10 -->

v0.9.3 closes the self-hosted worker singleton + direct dispatch feedback from `.local/feedbacks/feedback_for_v0.9.1.md`:

- **Workspace worker singleton** — `popola cloud worker start` auto-names workers as `popolaloom-<repo>-<hash>` and reuses the existing worker for the resolved `--worker-dir`; `--allow-duplicate` is required to start another one deliberately.
- **Direct worker dispatch** — `popola cloud worker dispatch "<prompt>"` posts through `popolad` with `cli=cursor-cloud` and `worker_name=<workspace-worker>` routing extras, so the result is a normal PopolaLoom task with `status`, `attach`, and `cancel`; `--print-only` / `--dry-run` previews without creating a task.
- **Cursor Cloud routing extras** — generic `popola dispatch --cli=cursor-cloud` accepts `use_private_worker`, `labels`, `worker_name`, `machine_name`, and `pool_name` via `--cli-flag`; convenience keys merge into labels and request private-worker routing automatically.
- **Strictly additive patch** — no v0.9.0 / v0.9.1 / v0.9.2 stable CLI verb, daemon RPC route, public Python API, or Skill front-matter key is renamed, removed, or repurposed. Full details: [`RELEASE_NOTES.md`](RELEASE_NOTES.md) and [`docs/USER_GUIDE.md#self-hosted-worker-handoff-popola-cloud-worker-v091`](docs/USER_GUIDE.md#self-hosted-worker-handoff-popola-cloud-worker-v091).

## v0.9.1 highlights

<!-- updated: 2026-05-09 -->

v0.9.1 adds a **self-hosted worker handoff** lane for teams that want this machine to execute Cursor Cloud Agent tool calls while Cursor owns cloud orchestration:

- **Three dispatch lanes made explicit** — local subprocess (`popola dispatch --cli=cursor`), popola-tracked Cloud REST (`popola dispatch --cli=cursor-cloud`, requires `CURSOR_API_KEY`), and self-hosted worker handoff (`popola cloud worker start` + dashboard / Slack / GitHub trigger, no popola task id).
- **`popola cloud worker` wrapper** — `debug` preflights the upstream `agent worker` CLI, `start` launches or reuses the workspace worker, `status` probes loopback management endpoints, `handoff` emits a copy-paste-ready dashboard prompt envelope with `popola_task_id: null`, and `dispatch` routes through `popolad` to that worker in v0.9.3+.
- **Strictly additive patch** — no v0.9.0 stable CLI verb, daemon RPC route, public Python API, or Skill front-matter key is renamed, removed, or repurposed. Full details: [`CHANGELOG.md`](CHANGELOG.md) and [`docs/USER_GUIDE.md#self-hosted-worker-handoff-popola-cloud-worker-v091`](docs/USER_GUIDE.md#self-hosted-worker-handoff-popola-cloud-worker-v091).

## v0.9.0 GA highlights

<!-- updated: 2026-05-09 -->

v0.9.0 is the **first Generally Available release**. It freezes the cumulative v0.8.x cloud surface as a stable SemVer contract, codifies test infrastructure for v0.9.x patches, and ships one new operator-visible feature:

- **API stability boundary** — [`docs/API_STABILITY.md`](docs/API_STABILITY.md) defines what is **stable** (12 CLI verbs + 16 daemon RPC paths + 5 `popolaloom.*` Python symbols + 3 Skill front-matter keys) vs **experimental** (`popola cloud runs` table layout, `--verbose` cost block, `[cloud.relay]` defaults, `cloud.sse.*` sub-types, `_*`-prefixed internals). v0.9.x patches ship no user-observable changes; v0.9.x minors may add new flags / fields / endpoints; breaking changes deferred to v0.10.0 with a 1-minor `DeprecationWarning` cycle first. v0.7.x → v0.9.0 migration recipes: [`docs/MIGRATION_v07_to_v09.md`](docs/MIGRATION_v07_to_v09.md).
- **`popola init --target=cloud-only`** (Q-D-4 偏离默认) — minimal cloud-dispatch-only project skeleton (3 files: `popolad.toml` / `.env.example` / `Makefile`), no IDE skill installs, no `.local/` workspace. The right starting point for teams running exclusively on Cursor Cloud Agents; the default `--target=full` profile is preserved byte-for-byte. See [USER_GUIDE](docs/USER_GUIDE.md#popola-init---targetcloud-only-v090) and the copy-paste-ready [`cloud-quickstart.sh`](cloud-quickstart.sh).
- **Fixtures freeze + drift detection** — `tests/fixtures/` hash-locked via SHA-256 manifest (`tests/fixtures/checksums.json`); a scheduled monthly workflow (`.github/workflows/cloud-fixtures-drift-check.yml`, also `workflow_dispatch`) replays the Tier-4 `tests/real_*` suites against live Cursor REST + SSE and opens a `fixtures-drift` issue with a unified diff if the captured shape drifts. PR runs only verify the cheap SHA-256 lock — no live API quota burned by default.
- **Coverage gate codified** — `pyproject.toml` pins `--cov-fail-under=94` (Q-D-6 lock); regressions auto-red the default lane.
- **v0.8.x deprecation sweep** — legacy `RelayHandoffEnvelope`, `POST /relay` (v0.3.0 envelope body), `to_handoff_envelope` migration helper, `cloud.run_status` event sub-type, and the static `_ERROR_CATALOG["rate_limit"]["backoff"]` block are all **removed in v0.9.0** (Q-D-3); see [MIGRATION §Breaking changes](docs/MIGRATION_v07_to_v09.md#v090--ga-deprecation-removals-pr-pending) for the operator-side replacement matrix.

> **Q-D-5 偏离默认 install note** — v0.9.0 is GitHub Release-only; PyPI publish is deferred to a v0.9.x patch (`BL-v0.9.x-PyPI` in `.local/feedbacks/TRACKER.md`). For v0.9.0 specifically install via `pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.0` (or `./install.sh install --from=git`). The `./install.sh install` default uses `--from=pypi` and currently resolves to the prior v0.8.x stable line; that surface will return v0.9.x only after the v0.9.x PyPI patch lands.

## v0.8.8 highlights

<!-- updated: 2026-05-08 -->

v0.8.8 adds three layered cloud-runtime improvements on top of v0.8.7's Cloud HITL production tier — each is opt-in and documented in [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md):

- **Multi-run support** for `--cli=cursor-cloud` — a single Cursor cloud agent (durable `agent.id`) now hosts N sequential follow-up runs via `POST /v1/agents/{id}/runs`; `popola attach --follow` renders chronologically with `[run-N]` prefixes + run-boundary dividers, and the EventLog NDJSON file is the durable cross-run history (per the Cursor SSE retention contract). New default-visible events `cloud.run_started` / `cloud.run_finished` bracket every run. New Q-C-1 deviation subcommand `popola cloud runs <task>` enumerates run history with paging, `--json`, and `--include-events`. See [`docs/USER_GUIDE.md#multi-run-cloud-agents-v088`](docs/USER_GUIDE.md#multi-run-cloud-agents-v088) and [`docs/USER_GUIDE.md#popola-cloud-runs--list-cloud-agent-run-history-v088`](docs/USER_GUIDE.md#popola-cloud-runs--list-cloud-agent-run-history-v088).
- **Cost transparency on `popola status --verbose`** — opt-in `--verbose` flag surfaces a curated 5-field cost block (`cost: n/a` honest disclosure + `model` + `mode: max` segment + `wall` + `link`) per the locked Q-C-2 design. The Cursor Cloud Agents v1 API does not document any per-run cost or token usage on the public REST/SSE wire, so `cost: n/a` is the only honest value in v0.8.8 (no fabricated numbers); a `doc_anchor` URL in `--json --verbose` lets readers verify field provenance independent of PopolaLoom version. Default `popola status` output is unchanged. See [`docs/USER_GUIDE.md#cost-transparency--status---verbose-v088`](docs/USER_GUIDE.md#cost-transparency--status---verbose-v088).
- **Auto-default `popola relay` with 5 mandatory mitigations** (Q-C-4 偏离默认) — new `popola relay <task_a>` subcommand turns the output of one cloud run into the input of a brand-new run, defaulting to **auto-dispatch** (deviates from the safer human-confirm default) on top of repo allowlist (`[]` blocks all relays out-of-the-box) + append-only audit log (`0o600`) + `detect-secrets` pre-flight scan covering 6 token shapes + a top-of-RELEASE_NOTES callout + CI isolation tests in the default lane. New `[cloud.backoff]` / `[cloud.busy_strategy]` config sections close the v0.8.7 quota observability gap with default-visible `cloud.queued_quota_exceeded` / `cloud.busy_*` events. See [`docs/USER_GUIDE.md#cross-pr-relay--popola-relay-v088`](docs/USER_GUIDE.md#cross-pr-relay--popola-relay-v088) and [`docs/USER_GUIDE.md#quota-aware-retry-cloudbackoff--cloudbusy_strategy-v088`](docs/USER_GUIDE.md#quota-aware-retry-cloudbackoff--cloudbusy_strategy-v088).

## Skills (v0.5.0+, two Skills as of v0.7.0)

PopolaLoom ships TWO Skills that auto-load in host agents:

| Skill | Path | When it triggers |
|---|---|---|
| `popola-loom` (canonical, v0.5.0+; renamed from `popolaloom` in v0.7.1+) | `src/popolaloom/skills/popola-loom/SKILL.md` | User says "dispatch a task to cursor", "list my agents", "popola doctor", etc. — every popola CLI verb |
| `install-popola` (NEW, v0.7.0+) | `src/popolaloom/skills/install-popola/SKILL.md` | User says "install popola", "/install-popola", "安装 popolaloom" — fresh install / upgrade workflow |

Both ship in the wheel; `popola init` installs the canonical one to per-IDE Skill directories. The installer Skill is opt-in and authored manually (deferred automation noted in [`RELEASE_NOTES.md`](RELEASE_NOTES.md) §"Known limitations").

### Per-IDE install paths

| IDE | Scope | Install path |
|---|---|---|
| Cursor | global | `~/.cursor/skills/popola-loom/SKILL.md` |
| Cursor | project | `<repo>/.cursor/skills/popola-loom/SKILL.md` |
| Claude Code | global | `~/.claude/skills/popola-loom/SKILL.md` |
| Claude Code | project | `<repo>/.claude/skills/popola-loom/SKILL.md` |
| Codex | global | `$CODEX_HOME/skills/popola-loom/SKILL.md` (default `~/.codex/`) |
| Copilot | project-only | `<repo>/.github/copilot-instructions.md` (single-file flatten) |

Every install verb is **idempotent**: a second invocation prints `SKIP <path> (already installed)` instead of overwriting operator edits. A `.popola-loom-version` marker is written beside the SKILL.md so `popola doctor` can detect drift (`v0.4.1 (expected v0.7.0)` etc.) when you upgrade the wheel without re-running install.

The `popola skill` verb group (since v0.5.0) ships four sibling commands for fine-grained Skill management:

```bash
popola skill install   --target=cursor --global   # write SKILL.md + version marker
popola skill upgrade   --target=all --global      # force re-install (overwrites operator edits)
popola skill doctor                               # audit every (target, scope) slot
popola skill uninstall --target=all --global      # remove SKILL.md + marker (v0.8.4+)
```

`popola skill uninstall` is the inverse of `install`: idempotent on a clean home (prints `ABSENT`), removes the sibling `.popola-loom-version` marker for cursor/claude/codex (copilot has no marker since it ships as a single `copilot-instructions.md` file), and prunes the now-empty `popola-loom/` leaf directory. The unified `install.sh uninstall` verb composes this with `pip uninstall popolaloom` for one-shell-command teardown.

For the upgrade workflow, the `popola doctor` audit explanation, and the full list of `popola init` verbs / modifiers, see [`docs/USER_GUIDE.md#ide-integration`](docs/USER_GUIDE.md#ide-integration).

## Lark notifications (v0.4.1+)

PopolaLoom sends proactive Lark interactive cards on every task terminal state. Set the env vars below (no daemon restart needed) and `popola popolad start`:

| Env var | Purpose | Default |
|---|---|---|
| `LARK_HITL_TARGET_OPEN_ID` | recipient open_id (HITL prompts + terminal cards) | (unset → Lark silent) |
| `LARK_NOTIFY_ON_COMPLETED` | `task.completed` → green card | `1` (ON) |
| `LARK_NOTIFY_ON_FAILED` | `task.failed` → red card | `1` (ON) |
| `LARK_NOTIFY_ON_CANCELED` | `task.canceled` → yellow card | `1` (ON) |
| `LARK_NOTIFY_ON_CANCEL_ESCALATED` | `cancel → SIGKILL` → orange card | `0` (OFF) |

When `lark-cli` is missing or the target open_id is unset, the daemon silently degrades to NDJSON-only event logging (per the "degrade gracefully" + "No Silent Failures" double constraint — every skip emits a single `lark.supervisor.skipped reason=...` INFO line). For the full HITL flow + the complete env-var table, see [`docs/USER_GUIDE.md#lark-integration`](docs/USER_GUIDE.md#lark-integration).

## Configuration (env vars)

| Env var | Purpose | Default |
|---|---|---|
| `POPOLA_HOME` | daemon socket / events / sqlite / pid root | `~/.popola/` |
| `POPOLA_USE_GRAPH` | enable LangGraph subgraph (v0.3.0+) | `1` |
| `CODEX_HOME` | Codex Skill directory | `~/.codex/` |
| `LARK_HITL_TARGET_OPEN_ID` | Lark recipient open_id (HITL + terminal cards) | (unset → Lark silent) |
| `LARK_PRIORITY_BOT_ID` | which bot to send via (multi-bot setup) | (unset → default bot) |
| `POPOLA_ARKTOWER_MIGRATIONS_DIR` | override vendored ArkTower migrations dir | (unset → use vendored) |

Full list with notes per variable: [`docs/USER_GUIDE.md#configuration`](docs/USER_GUIDE.md#configuration).

## MCP integration

PopolaLoom's MCP server exposes the same dispatch / inspect / HITL verbs to IDE Agents over stdio:

```jsonc
// ~/.cursor/mcp.json
{
  "mcpServers": {
    "popolaloom": {
      "command": "python",
      "args": ["-m", "popolaloom.mcp.server"]
    }
  }
}
```

After restarting your IDE, the host agent sees `popola_submit / popola_list / popola_status / popola_attach_stream / popola_cancel / popola_relay / popola_supervise / popola_federate / popola_supply_feedback / popola_inject_subtask`. The elicitation builder (`popolaloom.mcp.elicitation`) renders pending HITL prompts as form-mode requests so the IDE can surface them as a chooser UI. See [`docs/USER_GUIDE.md#mcp-integration`](docs/USER_GUIDE.md#mcp-integration) for the full setup.

## HITL across 5 channels

When a task hits `await interrupt(prompt)` inside a LangGraph subgraph, the daemon broadcasts the prompt to **5 channels simultaneously** — Lark / IDE / CLI / MCP / Web. Whichever responder wins the cross-channel sync race gets to answer (atomic `mark_answered` per `hitl/sync.py`). Operators commonly answer via:

- **CLI**: `popola pending` lists open prompts; `popola feedback <hitl_id> <answer> --reason "..."` submits.
- **Lark**: `lark-cli im +send --card '<json>'` ships an interactive card to `LARK_HITL_TARGET_OPEN_ID`; user taps "通过" / "拒绝" in the Lark app; the `LarkSupervisor` writes the reply back into LangGraph state and the task resumes.
- **MCP**: the IDE renders the prompt as a structured elicitation request (form-mode); the agent surfaces it as a choice UI.

Full architecture (5-channel broadcast → first-responder wins → state.resumed event): [`docs/USER_GUIDE.md#hitl-workflow`](docs/USER_GUIDE.md#hitl-workflow).

## Sibling project

PopolaLoom and [ArkTower](https://github.com/YoRHa-Agents/ArkTower) live under the `YoRHa-Agents` org. Since v0.5.0 PopolaLoom **vendors** ArkTower's relevant subset (TaskService / EventBus / SqliteTaskRepository / 4 schema migrations) under `popolaloom._vendored.arktower` so `pip install popolaloom` works on a fresh machine without an ArkTower clone. Refresh procedure: see [`VENDORING.md`](VENDORING.md).

PopolaLoom's per-task quality framework counterpart is [DevolaFlow](https://github.com/YoRHa-Agents/DevolaFlow) — the two Skills coexist (you can install both into the same IDE), and PopolaLoom can dispatch tasks that internally use the DevolaFlow per-task gates / convergence loops.

## Contributing

Contributions welcome. See [`CHANGELOG.md`](CHANGELOG.md) for the v0.0.1 → v0.7.0 development history (the release shape is one minor per "self-improvement loop" with a hard-gated metric — coverage / NFR / mutation / UX). PRs should target `main` after CI green; the default lane gate is `pytest --cov-fail-under=94` plus `ruff check src/popolaloom tests/` lint clean. Slow / nightly / real-CLI / real-Lark lanes are opt-in via `pytest -m`.

## License

MIT
