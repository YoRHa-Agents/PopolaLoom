# PopolaLoom

> **v0.8.5** — Same local-first weave as earlier releases plus **Cursor Cloud Agents** wiring via **`--cli=cursor-cloud`** (REST + httpx, Option α research). Tasks can run on Cursor’s cloud surface (`https://cursor.com/dashboard/cloud-agents`) instead of (`or in parallel with`) a local subprocess. **Requires non-empty `CURSOR_API_KEY` for cloud**. The stable hands-off envelope (`popolaloom.handoff`) persists every dispatch as a `cat`-friendly Markdown envelope under `.local/.agent/handoff/<id>.md`, injects it into the spawned sub-CLI's environment where applicable (local adapters), and makes replay deterministic via `popola dispatch --replay <id>`.

[![Status](https://img.shields.io/badge/status-pre--alpha-orange.svg)](#status) [![Coverage](https://img.shields.io/badge/coverage-94%25%2B-brightgreen.svg)](#status) [![License](https://img.shields.io/badge/license-MIT-blue.svg)](#license) [![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

## What is PopolaLoom?

PopolaLoom is a local-first **meta-orchestrator** that sits on top of every agent CLI on your machine — Cursor, Claude Code, Codex, Kimi, GitHub Copilot — and gives them a single dispatch surface, a persistent task bus, and a unified HITL channel. Think of it as the "loom" (织机) that weaves N agent CLIs into one coherent run-graph: each per-task strand is its own subprocess managed by the `popolad` UDS daemon, and the loom keeps the whole tapestry surviving across terminals, SSH sessions, and machine reboots.

It is the multi-task / multi-CLI sibling of [DevolaFlow](https://github.com/YoRHa-Agents/DevolaFlow) (the per-task quality framework) and ships **vendored** with the relevant subset of [ArkTower](https://github.com/YoRHa-Agents/ArkTower) (the task pool / SQLite persistence / EventBus). The end result: `pip install popolaloom` on a fresh machine gives you `popola dispatch <prompt> --cli=cursor` + `popola attach <task_id> --follow` + `popola doctor` with zero sibling clones, plus a Skill that auto-discovers in any host agent so the CLI is also reachable as natural-language verbs ("派发任务给 cursor 跑 X", "list my running agents", "popola doctor").

## Why PopolaLoom?

- **Single dispatch surface** for every local agent CLI — `popola dispatch "..." --cli=cursor|claude|codex|kimi|copilot` instead of N separate command shapes.
- **Cross-terminal task survival** via the `popolad` UDS daemon (`start_new_session=True`, persistent SQLite task pool) — close your shell, SSH disconnect, machine reboot; the task survives until you `popola attach` from anywhere.
- **Vendored ArkTower** task pool + EventBus + 4 SQL migrations under `popolaloom._vendored.arktower` — no sibling repo required for `pip install`.
- **HITL across 5 channels** (Lark / IDE / CLI / MCP / Web) — LangGraph `interrupt()` broadcasts to all 5; first responder wins via cross-channel atomic `mark_answered`.
- **Auto-discovery via Skill convention** — `popola init` writes the canonical `SKILL.md` into every detected IDE (`~/.cursor/skills/popola-loom/`, `~/.claude/skills/popola-loom/`, `$CODEX_HOME/skills/popola-loom/`, `<repo>/.github/copilot-instructions.md`), and host agents auto-load it.
- **8-dim self-eval baseline** (`popola eval run`) — PopolaLoom-nines composite over `dispatch_isolation / cycle_convergence / hitl_latency / attach_correctness / cross_cli_handoff / single_threaded_writes / event_log_completeness / hitl_handleability`, with per-dimension evidence pipelines.
- **MCP-native** — exposes 9 dispatch / inspect / HITL verbs over stdio (`popola_submit / popola_list / popola_status / popola_attach_stream / popola_cancel / popola_relay / popola_supervise / popola_supply_feedback / popola_inject_subtask`) so any MCP-aware IDE can call them as tools.

## 5-minute Quickstart

```bash
pip install popolaloom
popola init                          # auto-detect Cursor / Claude / Codex / Copilot
popola popolad start                 # boot the UDS daemon
popola dispatch "echo hello popola" --cli=cursor
popola list                          # see active tasks
popola attach <task_id> --follow     # tail SSE event stream
popola doctor                        # 4-subsystem health check
```

Or run the automated 6-step smoke (now includes `popola init --dry-run` at Step 0):

```bash
bash examples/quickstart.sh
```

For the long version with explanations, see [`docs/QUICKSTART.md`](docs/QUICKSTART.md).

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

<!-- updated: 2026-05-08 -->

PopolaLoom now speaks Cursor’s Background Agent REST alongside the historical local CLIs:

- **Invocation**: start `popolad`, export `CURSOR_API_KEY`, then `popola dispatch "<prompt>" --cli=cursor-cloud --cli-flag repo_url=https://github.com/you/repo [--cli-flag model=composer-2 ...]`.
- **Why daemon still matters**: the supervisor swaps `Popen` for `CloudCursorClient.create_agent(...)`, persists `cursor_agent_id` / `cursor_run_id`, and tracks phases via the cloud poller so `attach`/`status`/`cancel` semantics stay cohesive with local workloads.
- **Observability**: browser dashboard **https://cursor.com/dashboard/cloud-agents** complements `popola status <task>` (shows `runtime=cloud`). v0.8.6+ also surfaces a default-on `runtime` column in `popola list` so local vs cloud rows are distinguishable at a glance — pass `--no-runtime` to hide it.
- **Live attach (v0.8.6+)**: `popola attach <id> --follow` for `runtime=cloud` tasks now ingests Cursor's SSE stream by default and auto-falls back to the legacy poll-only view on `410 stream_expired` / network errors; pass `--no-stream` to force the legacy path. See [`docs/USER_GUIDE.md#sse-ingest-v086`](docs/USER_GUIDE.md#sse-ingest-v086) for the full contract (incl. ≤3 s tolerated divergence between SSE and `cloud_phase`).
- **Opt-in QA**: `pytest tests/real_cursor_cloud/ -m real_cursor_cloud` only after exporting the API key — four cheap tests (create+cancel sentinel, metadata GETs, bogus-key auth assertion); default CI lane **deselects** `-m real_cursor_cloud`.

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

**v0.8.5 — Cursor Cloud Agents + daemon poller + cloud HITL bridge.** Ships the sibling **`cursor-cloud` adapter**, non-terminal **`QUEUED` / `STARTING`** states with enriched `popola status` telemetry (agent + run identifiers + Cursor phase mapping), deterministic REST cancellations, Lark-friendly `/hitl/cloud/*` bridging, Tier-4+ opt-in **`real_cursor_cloud`** smoke tests, and exhaustive documentation bumps (see Decision matrix `.local/research/v0.8.5_cloud_agent/00-decision-matrix-zh.md` §7). **Unified install tooling from v0.8.4 remains unchanged** (`install.sh` / `popola skill uninstall`).

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
| 1729 default-lane tests / 94%+ coverage | OK live |

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

### One-line install (v0.8.4+)

```bash
curl -fsSL https://raw.githubusercontent.com/YoRHa-Agents/PopolaLoom/main/install.sh | bash
# or, with options:
curl -fsSL https://raw.githubusercontent.com/YoRHa-Agents/PopolaLoom/main/install.sh | bash -s -- install --scope=global --target=all
```

The unified `install.sh` (v0.8.4+) wraps `pip install` + `popola skill install` + `popola popolad start` + `popola doctor` in a single shell command. Options: `--scope=global|project`, `--target=cursor|claude|codex|copilot|all`, `--from=pypi|git|<path>`, `--version=X.Y.Z`, `--no-skills`, `--no-daemon`, `--dry-run`. Run `./install.sh --help` for the full matrix; the script is idempotent and safe to re-run for upgrades.

### Update / Uninstall

```bash
./install.sh update                       # pip upgrade + popola skill upgrade
./install.sh uninstall --yes              # remove Skills + pip uninstall popolaloom
./install.sh uninstall --yes --purge      # also delete ~/.popola/ daemon state
```

### Manual install (alternative)

```bash
pip install popolaloom
# OR from a clone (development):
pip install -e ".[dev]"
```

Verify the install:

```bash
python -c "import popolaloom; print(popolaloom.__version__)"   # → 0.8.5
which popola                                                    # → /usr/local/bin/popola (or similar)
popola version                                                  # → "popolaloom 0.8.5"
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
