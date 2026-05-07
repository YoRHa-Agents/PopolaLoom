---
layout: default
title: User Guide
description: Comprehensive reference for the popola CLI, MCP integration, HITL flows, and configuration.
lang: en
translation_url: /zh/USER_GUIDE.html
---

# PopolaLoom — User Guide (v0.8.5)

> Comprehensive reference for the `popola` CLI, MCP integration, HITL flows, Lark notifications, and the configuration surface. For first-time users, start with [`QUICKSTART.md`](QUICKSTART.md). For walkthroughs and example outputs, see [`DEMO.md`](DEMO.md).

## Table of Contents

- [Mental model](#mental-model)
- [CLI verb reference](#cli-verb-reference)
  - [Task lifecycle](#task-lifecycle)
  - [Daemon management](#daemon-management)
  - [IDE integration](#ide-integration)
  - [Self-evaluation](#self-evaluation)
  - [Health + diagnostics](#health--diagnostics)
- [MCP integration](#mcp-integration)
- [HITL workflow](#hitl-workflow)
- [Lark integration](#lark-integration)
- [Adapter passthrough (`--cli-flag`)](#adapter-passthrough)
- [Cloud Agent dispatch (v0.8.5+)](#cloud-agent-dispatch-v085)
- [Hands-off envelope (v0.8.0+)](#hands-off-envelope)
- [Configuration (env vars)](#configuration)
- [Troubleshooting](#troubleshooting)
- [Architecture deep-dive](#architecture-deep-dive)
- [Reference](#reference)

## Mental model

PopolaLoom is a **sidecar daemon** (`popolad`) plus a **CLI** (`popola`) plus a **stdio MCP server** (`popolaloom-mcp`). The daemon binds a Unix Domain Socket at `$POPOLA_HOME/popolad.sock`, serves the dispatch / inspect / cancel / probe RPC, and owns:

- the **task pool** (vendored ArkTower `SqliteTaskRepository` — persistent across daemon restarts),
- the **NDJSON event log** under `$POPOLA_HOME/events/<task_id>.jsonl` (CloudEvents 1.0 envelope per event),
- the **LangGraph subgraph runner** (per-task; uses `interrupt()` for HITL pauses),
- the optional **`LarkSupervisor`** (manages a `lark-cli event consume` subprocess for inbound Lark replies, restart watchdog, ≤ 3 restarts → escalate).

Each task is a separate child subprocess of the agent CLI you specify (`cursor-agent`, `claude`, `codex`, …). The daemon watches the subprocess via a wait-thread, serializes its stdout/stderr into NDJSON events on the bus, broadcasts state transitions, and on terminal state writes a final `task.completed / task.failed / task.canceled` event plus an optional Lark notification card.

The `popola` CLI is a thin client over the UDS RPC: every verb (`dispatch`, `list`, `status`, `attach`, `cancel`, `probe`, `popolad`, `init`, `skill`, `doctor`, `eval`, `pending`, `feedback`) translates to one or more HTTP/JSON calls. The MCP server is a stdio bridge that exposes the same dispatch verbs as MCP tools so any MCP-aware IDE can call them as tool invocations.

The end result: you can `popola dispatch "..."` from any terminal, close that terminal, SSH in from another machine, run `popola attach <id> --follow`, and watch the task continue. HITL prompts are broadcast across **5 channels** (Lark, IDE, CLI, MCP, Web); the first responder wins via cross-channel sync (`hitl/sync.py:mark_answered` is atomic).

## CLI verb reference

### Task lifecycle

| Verb | Purpose | Example |
|---|---|---|
| `popola dispatch <prompt> --cli=<name>` | Spawn a new task on the named adapter; returns `task_id` | `popola dispatch "fix the bug in foo.py" --cli=cursor` |
| `popola dispatch ... --wait --timeout=120` | Block until terminal state (default 60s) | `popola dispatch "..." --cli=claude --wait --timeout=120` |
| `popola dispatch ... --cli-flag KEY=VAL` | Pass adapter-specific flags (repeatable; JSON-parsed) | `popola dispatch "..." --cli=cursor --cli-flag output_format=stream-json` |
| `popola dispatch ... --cwd <path>` | Spawn the subprocess with `Popen(cwd=...)` | `popola dispatch "refactor X" --cli=cursor --cwd ~/proj` |
| `popola dispatch ... --json` | Emit machine-readable JSON envelope | `popola dispatch "..." --cli=cursor --json` |
| `popola list` | List **non-terminal** tasks (default) | `popola list` |
| `popola list --all` | Include completed / failed / canceled | `popola list --all` |
| `popola list --json` | Machine-readable; pipes to `jq` | `popola list --all --json \| jq '.[] \| .task_id'` |
| `popola status <task_id>` | Single-task full state envelope | `popola status cursor-23e74ec18917` |
| `popola status <id> --json` | JSON envelope (state / pid / exit_code / events_log / arktower_task_id) | `popola status cursor-23e74ec18917 --json` |
| `popola attach <task_id> --follow` | Tail the SSE / NDJSON event stream (default `--follow`) | `popola attach cursor-23e74ec18917 --follow` |
| `popola attach <id> --no-follow` | One-shot dump of all events seen so far | `popola attach <id> --no-follow` |
| `popola cancel <task_id>` | SIGTERM → 5s grace → SIGKILL escalation | `popola cancel cursor-23e74ec18917` |
| `popola list-cli` | Print registered adapters + whether each binary is on PATH | `popola list-cli` |

The full lifecycle: `dispatched → running → (interrupted ↔ running)* → completed / failed / canceled`. The `interrupted` state is set when a LangGraph node calls `await interrupt(prompt)`; the daemon emits `task.elicited` on the event bus with the prompt body and broadcasts to the 5 HITL channels.

### Daemon management

| Verb | Purpose | Example |
|---|---|---|
| `popola popolad start` | Spawn `python -m popolaloom.daemon` with `start_new_session=True` | `popola popolad start` |
| `popola popolad start --foreground` | Run in the foreground (debugging mode) | `popola popolad start --foreground` |
| `popola popolad status` | Daemon socket + pid + probe roll-up | `popola popolad status` |
| `popola popolad stop` | SIGTERM → 5s → SIGKILL; clean pid + sock | `popola popolad stop` |
| `popola probe` | Lightweight liveness — pid + uptime + active task count | `popola probe` |

The daemon binds the UDS at `$POPOLA_HOME/popolad.sock` (default `~/.popola/popolad.sock`); the pidfile is `$POPOLA_HOME/popolad.pid`; the log is `$POPOLA_HOME/log/popolad.log`. Cold-start UDS-up time has a NFR-1 gate of ≤ 2s (median ~250ms on the dev VM).

### IDE integration

| Verb | Purpose | Example |
|---|---|---|
| `popola init` | Auto-detect every IDE present + install the canonical Skill | `popola init` |
| `popola init <ide>` | Install for one IDE (cursor / claude / codex / copilot / local) | `popola init cursor --global` |
| `popola init all --global` | Install for every detected IDE except local, with global scope | `popola init all --global` |
| `popola init --interactive` | Human-driven wizard (v0.5.5+; `typer.confirm` per IDE + scope) | `popola init --interactive` |
| `popola init --list` | Print every detected target + install path (no writes) | `popola init --list` |
| `popola init <ide> --dry-run` | Preview writes without touching disk | `popola init cursor --project --dry-run` |
| `popola skill install --target=<ide>` | Same as `popola init <ide>`, sub-verb form | `popola skill install --target=cursor` |
| `popola skill upgrade --target=<ide>` | **Overwrite** installed SKILL.md from the wheel (after `.popola-loom-bak.<ts>` backup) | `popola skill upgrade --target=cursor` |
| `popola skill upgrade --target=all` | Cycle every detected install | `popola skill upgrade --target=all` |
| `popola skill doctor` | Skill-only audit (subset of `popola doctor`) | `popola skill doctor` |
| `popola skill uninstall --target=<ide>` | Remove SKILL.md + marker (v0.8.4+) | `popola skill uninstall --target=cursor --global` |
| `popola skill uninstall --target=all` | Remove every Skill across IDEs | `popola skill uninstall --target=all --global` |

Per-IDE install paths:

| IDE | Scope | Install path |
|---|---|---|
| Cursor | global | `~/.cursor/skills/popola-loom/SKILL.md` |
| Cursor | project | `<repo>/.cursor/skills/popola-loom/SKILL.md` |
| Claude Code | global | `~/.claude/skills/popola-loom/SKILL.md` |
| Claude Code | project | `<repo>/.claude/skills/popola-loom/SKILL.md` |
| Codex | global | `$CODEX_HOME/skills/popola-loom/SKILL.md` (default `~/.codex/`) |
| Copilot | project-only | `<repo>/.github/copilot-instructions.md` (single-file flatten) |
| local | scaffold | `<repo>/.local/` (workspace surface) |

`popola init` differs from `popola skill upgrade` in two ways: (1) `init` is **idempotent** — second invocation prints `SKIP <path> (already installed)`; `upgrade` **always overwrites** (after writing a `.popola-loom-bak.<ts>` backup). (2) `init` is the first-time-installer entry point; `upgrade` is the post-`pip install --upgrade popolaloom` refresh entry point.

### `install.sh` — bash bootstrap installer (v0.8.4+)

The unified bash installer at the repo root (`install.sh`) wraps the four-step manual workflow (`pip install` → `popola skill install` → `popola popolad start` → `popola doctor`) into a single shell command. The same script also drives the inverse path: `install.sh uninstall` removes the Skills and uninstalls the package; `install.sh update` upgrades the wheel and refreshes the on-disk SKILL.md.

```bash
# Pull from GitHub and run as a one-liner
curl -fsSL https://raw.githubusercontent.com/YoRHa-Agents/PopolaLoom/main/install.sh | bash

# Same, with explicit options
curl -fsSL https://raw.githubusercontent.com/YoRHa-Agents/PopolaLoom/main/install.sh \
  | bash -s -- install --scope=global --target=all

# After a clone — same script, local invocation
./install.sh install --scope=project --target=cursor
./install.sh update
./install.sh uninstall --yes --purge
```

#### Verbs

| Verb | Purpose |
|---|---|
| `install` (default) | `pip install popolaloom` → `popola skill install` → `popola popolad start` (best-effort) → `popola doctor` (best-effort) |
| `update` | `pip install --upgrade popolaloom` → `popola skill upgrade --target=<...>` → `popola doctor` |
| `uninstall` | `popola popolad stop` (best-effort) → `popola skill uninstall --target=<...>` → `pip uninstall popolaloom` (gated on confirmation) → optional `rm -rf $POPOLA_HOME` when `--purge` is set |
| `version` | Print `install.sh v0.8.4` and exit |
| `help` / `--help` / `-h` | Print usage and exit |

#### Flag matrix

| Flag | Applies to | Purpose |
|---|---|---|
| `--scope=<global\|project>` | install / update / uninstall | Skill scope (default: `global`) |
| `--target=<cursor\|claude\|codex\|copilot\|all>` | install / update / uninstall | Which IDE Skill (default: `all`) |
| `--from=<pypi\|git\|PATH>` | install / update | Install source (default: `pypi`) |
| `--version=<X.Y.Z>` | install / update | Pin a PyPI version (requires `--from=pypi`) |
| `--python=<bin>` | all | Python interpreter (default: search `python3.12 → python3.11 → python3 → python`) |
| `--no-skills` | all | Skip the Skill install / uninstall step |
| `--no-daemon` | install | Skip `popola popolad start` after pip install |
| `--purge` | uninstall | Also `rm -rf ${POPOLA_HOME:-$HOME/.popola}` after pip uninstall (gated on confirmation; **destructive**) |
| `--yes` / `-y` | uninstall | Skip interactive prompts (required for non-tty / scripted runs) |
| `--dry-run` | all | Print every command that would be run; no I/O |
| `--quiet` / `-q` | all | Suppress informational output |
| `--help` / `-h` | all | Print usage and exit |

#### `--from=` source resolution

| Value | Translates to |
|---|---|
| `pypi` (default) | `pip install popolaloom` |
| `pypi` + `--version=X.Y.Z` | `pip install popolaloom==X.Y.Z` |
| `git` | `pip install git+https://github.com/YoRHa-Agents/PopolaLoom.git` |
| any other value (filesystem path) | `pip install <path>` (works for local clones, wheel files, and tarballs) |

For example: `./install.sh install --from=./dist/popolaloom-0.8.4-py3-none-any.whl` installs from a locally-built wheel.

#### Examples

```bash
# Fresh install for every IDE at user-home scope (the typical first run)
./install.sh install

# Install only for Cursor at project scope, from the latest main on GitHub
./install.sh install --target=cursor --scope=project --from=git

# Install pinned to a specific PyPI version
./install.sh install --version=0.8.4

# Update only the package without touching Skill files
./install.sh update --no-skills

# Uninstall everything in one shot (interactive prompt before pip uninstall)
./install.sh uninstall

# Same, scripted (non-tty) — skip the prompt and purge ~/.popola/
./install.sh uninstall --yes --purge

# See exactly what would happen without touching disk
./install.sh install --dry-run
./install.sh uninstall --dry-run --yes
```

> **Destructive flag warning**: `install.sh uninstall --purge` deletes `${POPOLA_HOME:-$HOME/.popola}` (daemon socket, NDJSON event log, vendored ArkTower SQLite, daemon pidfile). The script gates the deletion behind a `[y/N]` prompt; pass `--yes` only when you have backed up anything you need.

#### Idempotency contract

- Re-running `install` with the same flags is safe (`pip install` is idempotent; `popola skill install` prints `SKIP` for byte-identical SKILL.md content).
- Re-running `uninstall` after the package is gone returns `popolaloom not installed; nothing to do` and exits 0.
- Re-running `uninstall --target=cursor` after the Skill is gone produces an `ABSENT` outcome from `popola skill uninstall` (no error).

#### When to use `install.sh` vs `popola init`

The two surfaces are **complementary**, not competing:

- `install.sh` is the **first-time bootstrap** — run it on a fresh machine to get popolaloom installed end-to-end (pip + Skills + daemon + doctor) in one command. Also the recommended path for upgrade and uninstall.
- `popola init` is the **post-install IDE wizard** — run it after `install.sh` (or `pip install popolaloom`) to add additional IDEs, scaffold the `.local/` workspace surface, or run the interactive setup wizard.

### Self-evaluation

| Verb | Purpose | Example |
|---|---|---|
| `popola eval run --output PATH` | Run the 8-dim PopolaLoom-nines self-eval; write TOML | `popola eval run --output /tmp/nines.toml` |
| `popola eval show` | Show the dimension weights + last-run scores | `popola eval show` |
| `popola eval show --json` | JSON envelope (per-dim score + weight + composite) | `popola eval show --json` |

The 8 dimensions: `dispatch_isolation / cycle_convergence / hitl_latency / attach_correctness / cross_cli_handoff / single_threaded_writes / event_log_completeness / hitl_handleability`. Each scorer collects evidence from `~/.popola/events/*.jsonl` (NDJSON receipts) and emits a 0.0–1.0 score; the composite is a weighted sum (weights sum to 1.0; tunable in `nines.toml`).

### Health + diagnostics

| Verb | Purpose | Example |
|---|---|---|
| `popola doctor` | 4-subsystem audit; human-readable table; exit 0 by default | `popola doctor` |
| `popola doctor --strict` | Exit 1 on any FAIL (CI-friendly) | `popola doctor --strict` |
| `popola doctor --json` | 4-section envelope (skill / daemon / lark / arktower) + summary | `popola doctor --json` |
| `popola pending` | Show prompts awaiting human response | `popola pending` |
| `popola feedback <hitl_id> <answer> --reason "..."` | Submit a CLI-channel HITL response | `popola feedback hitl-abc12 yes --reason "verified backup"` |
| `popola version` | Print `popolaloom <version>` | `popola version` |

The `popola doctor` 4 subsystems:

1. **Skill** — every `(target, scope)` slot from `SKILL_TARGETS`; reports `OK` / `MISS` / `DRIFT` (drift = installed `.popola-loom-version` ≠ wheel version).
2. **Daemon** — `GET /probe` over the popolad UDS socket; `OK` (pid + uptime) when the daemon is up, `FAIL` otherwise.
3. **Lark** — `lark-cli` on PATH + `LARK_HITL_TARGET_OPEN_ID` env var; `OK` (both present), `WARN` (binary on PATH, env unset), `OFF` (binary missing — informational, not a fail).
4. **ArkTower** — vendored module imports cleanly + the two PopolaLoom migrations (`005_popolaloom_extensions.sql` / `006_popola_hitl.sql`) are on disk; `WARN` when migrations are missing (the daemon falls back to a no-op runner per the "degrade gracefully" constraint).

Exit code is `0` by default (WARN / DRIFT / MISS are informational); `--strict` escalates any FAIL into a non-zero exit.

## MCP integration

PopolaLoom's MCP server exposes the dispatch / inspect / HITL verbs to any MCP-aware IDE over stdio. To wire it into Cursor:

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

For Claude Code:

```jsonc
// ~/.claude/mcp.json — same shape; the IDE auto-restarts the MCP child on save
{
  "mcpServers": {
    "popolaloom": {
      "command": "python",
      "args": ["-m", "popolaloom.mcp.server"]
    }
  }
}
```

After restart, the host agent sees the 9-verb stdio bridge:

- `popola_submit` — equivalent to `popola dispatch` (returns `task_id`)
- `popola_list` — equivalent to `popola list --all`
- `popola_status` — equivalent to `popola status <task_id>`
- `popola_attach_stream` — equivalent to `popola attach <task_id> --follow` (streams events over MCP)
- `popola_cancel` — equivalent to `popola cancel <task_id>`
- `popola_relay` — chain a follow-up task onto the prior task's output (per `mcp/tools.py`)
- `popola_supervise` — register a long-running supervised cycle
- `popola_federate` — multi-task fan-out with cross-task handoff envelopes
- `popola_supply_feedback` — equivalent to `popola feedback <hitl_id> <answer>`
- `popola_inject_subtask` — inject a child subtask under a parent

The elicitation builder (`popolaloom.mcp.elicitation`) renders pending HITL prompts as form-mode requests so the IDE can surface them as a chooser UI (a button-style picker for enum prompts; a freeform text field for open prompts).

## HITL workflow

Tasks pause at `await interrupt(prompt)` inside their LangGraph subgraph. The daemon broadcasts the prompt to **5 channels simultaneously**:

| Channel | How users respond | Code path |
|---|---|---|
| **Lark** | Tap "通过" / "拒绝" on the interactive card | `lark/listener.py` (button event → `LarkSupervisor` → state writeback) |
| **IDE** | Click in the host IDE's chooser UI (Cursor / Claude / Codex) | MCP elicitation form (form-mode tool result) |
| **CLI** | `popola pending` then `popola feedback <hitl_id> <answer>` | `cli/feedback_cmd.py` |
| **MCP** | Programmatic via `popola_supply_feedback` tool call | `mcp/tools.py:popola_supply_feedback` |
| **Web** | Static docs entry point and future dashboard channel | GitHub Pages now; browser dashboard remains a tracked roadmap surface |

The first responder wins via the atomic `mark_answered` in `hitl/sync.py`; the late responders see "already answered (via=<channel>)" and back off. The state writeback emits a `state.resumed` event; the LangGraph subgraph picks up where it left off.

A typical Lark approval flow:

```bash
export LARK_HITL_TARGET_OPEN_ID=ou_xxx           # your Lark open_id
popola dispatch "drop the staging.users table" --cli=cursor
# → cursor-deadbeef

# Inside the task, the adapter detects a destructive operation and calls
# `interrupt(prompt="ok to delete prod table?", options=["yes", "no"])`.
# The daemon broadcasts; you see a Lark card in your IM client + the
# task pauses at the `interrupt()` point.

# In Lark, you tap "通过" → LarkSupervisor catches the button event,
# writes "yes" back into the LangGraph state, the subgraph resumes.

popola attach cursor-deadbeef --follow            # see the resume + completion
```

## Lark integration

### Outbound (proactive notifications)

PopolaLoom sends interactive Lark cards on every terminal state transition. Defaults are 3 ON / 2 OFF (v0.4.1+):

| Trigger | Default | Card colour |
|---|---|---|
| `task.completed` | ON (`LARK_NOTIFY_ON_COMPLETED=1`) | green |
| `task.failed` | ON (`LARK_NOTIFY_ON_FAILED=1`) | red |
| `task.canceled` | ON (`LARK_NOTIFY_ON_CANCELED=1`) | yellow |
| `cancel → SIGKILL` | OFF (`LARK_NOTIFY_ON_CANCEL_ESCALATED=0`) | orange |

Cards include the prompt summary (truncated to `LARK_NOTIFY_PROMPT_TRUNCATE` chars; default 200), the task_id, the exit_code, and a deep-link to the events log path.

### Inbound (HITL replies + listener supervision)

The `LarkSupervisor` manages a `lark-cli event consume <events>` subprocess; it watches for "button click" events on cards with the `--metadata-key hitl_id=<id>` annotation and writes the chosen answer back into LangGraph state. If the listener subprocess dies, the supervisor restarts it (≤ 3 consecutive restarts → escalate to operator-visible HITL).

Per workspace rule, every Lark write operation appends the `\n---\n本消息由飞书工具 Lark-Cli 发送` footer.

### Gating

When `lark-cli` is missing OR `LARK_HITL_TARGET_OPEN_ID` is unset, the daemon silently degrades to NDJSON-only event logging. Every skip emits a single `lark.supervisor.skipped reason=...` INFO line so you can audit the gating decision in `popolad.log`.

## Adapter passthrough

`--cli-flag KEY=VAL` (repeatable) passes adapter-specific arguments to the underlying agent CLI. Values are JSON-parsed first (`true` / `123` / `"foo"`); on parse failure they fall back to a string (`output_format=text` is equivalent to `output_format="text"`). The dispatch handler collects them into an `extra: dict` which each adapter's `build_command` translates into the final `argv`.

| Adapter | KEY | Type | Becomes | Notes |
|---|---|---|---|---|
| `cursor` | `output_format` | str | `--output-format <val>` | Whitelist: `text` (default) / `stream-json`; violation → ValueError |
| `cursor` | `cwd_flag` | bool | `--cwd <cwd>` if true | Default false (let supervisor's `Popen(cwd=...)` control) |
| `cursor` | `session_id` | str | `--session-id <chatId>` | Reuse a `cursor-agent create-chat` pre-allocated chat |
| `cursor` | `cli_args` / `cmd_args` | str | passed through verbatim | Cursor `--trust` / `--no-color` flags etc. (v0.6.0+) |
| `claude` | `session_id` | str (UUID) | `--session-id <UUID>` | "Allocate ID first, then spawn" form |
| `claude` | `max_turns` | int | `--max-turns <n>` | Limits conversation rounds |
| `codex` | `sandbox` | str | `--sandbox <val>` | Whitelist: `read-only` / `workspace-write` / `danger-full-access` |

Common patterns:

```bash
# 1. Cursor stream-json output (lets supervisor parse tool-call events line-by-line)
popola dispatch "design caching layer" --cli=cursor \
  --cli-flag output_format=stream-json

# 2. Claude with a pre-allocated UUID + max_turns guard
SESSION=$(python -c "import uuid; print(uuid.uuid4())")
popola dispatch "refactor module X" --cli=claude \
  --cli-flag session_id="$SESSION" \
  --cli-flag max_turns=10

# 3. Codex sandbox locked to read-only
popola dispatch "review src/foo.py for bugs" --cli=codex \
  --cli-flag sandbox=read-only

# 4. Repeatable flags (cursor: stream-json + a session_id at once)
popola dispatch "..." --cli=cursor \
  --cli-flag output_format=stream-json \
  --cli-flag session_id=$CHAT_ID
```

Unknown KEYs are silently ignored by the adapter (forward-compat for newer adapter versions); the value-parse rules raise on whitelist violations (No Silent Failures for typed knobs).

## Cloud Agent dispatch (v0.8.5+)

### Prerequisites

1. **Daemon** — identical to other adapters: `popola popolad start` (Unix socket RPC).
2. **API key** — export a non-empty **`CURSOR_API_KEY`**. PopolaLoom authenticates with Cursor’s Cloud Agents REST using **HTTP Basic** (username = API key, password empty) through `CloudCursorClient`.

Without the key, `--cli=cursor-cloud` is rejected at adapter availability checks; the historical `--cli=cursor` subprocess path is unchanged.

### Dispatch

```bash
export CURSOR_API_KEY="cr_..."   # example shape only — use your Cursor dashboard key material
popola dispatch "Plan database migration scaffolding" \
  --cli=cursor-cloud \
  --cwd ~/workspace/acme-backend \
  --cli-flag repo_url=https://github.com/acme/monorepo \
  --cli-flag starting_ref=main \
  --cli-flag model=composer-2 \
  --cli-flag auto_create_pr=false
```

The adapter (`CursorCloudAdapter`) packs your prompt + validated `extra` keys into JSON behind `CLOUD_BUILD_COMMAND_MARKER`. `Supervisor.spawn` recognises the sentinel and calls **`_spawn_cloud()`** instead of `Popen`.

### Behaviour vs local adapters

| Surface | Local `cursor` / other CLIs | `cursor-cloud` |
|---|---|---|
| Execution | subprocess on workstation | Cursor-managed cloud workload |
| `popola cancel` | SIGTERM escalation | REST `cancel_run`; explicit EventLog tails on HTTP failures |
| `popola status` | `pid`, `runtime=local`, etc. | `runtime=cloud`, `cursor_agent_id` (`bc-*` prefix observed in API), `cursor_run_id`, `cloud_phase` |
| Browser tooling | IDE local session | Inspect runs at **`https://cursor.com/dashboard/cloud-agents`** (+ agents index) |

The cloud poller thread maps remote phases (`CREATING`, `RUNNING`, `FINISHED`, `ERROR`, `CANCELLED`, `EXPIRED`, …) into existing EventLog / FSM semantics so SSE consumers behave consistently.

### HITL bridging endpoints

Automations that run **outside** the IDE but still want first-responder HITL can call the authenticated daemon HTTP API:

| Verb | Route | Purpose |
|---|---|---|
| POST | `/hitl/cloud/request` | Register a structured question for cloud-hosted agents |
| GET | `/hitl/cloud/wait/{hitl_id}` | Block until answered / deadline (long-poll friendly) |
| POST | `/hitl/cloud/answer/{hitl_id}` | Submit a human-authored answer (paired with Lark + SQLite store) |

These compose with existing Lark fan-out: the **`cloud`** `HITLChannel` participates in identical first-responder-wins bookkeeping as Lark / MCP / IDE channels (`mark_answered`).

### Operational notes

1. **`auto_create_pr` defaults false** (`--cli-flag auto_create_pr=true` opts in) per release decision matrix.
2. Prefer **narrow prompts** — every dispatch still records the Markdown handoff envelope for audit, but quota accrues on Cursor’s side.
3. Regression / smoke coverage lives under `tests/real_cursor_cloud/` with marker **`real_cursor_cloud`**; exporting `CURSOR_API_KEY` runs four cheap live tests (`create` + immediate `cancel`, metadata GETs, bogus-key sentinel). Omit the env var locally or in CI for **skipped-not-failed** semantics.

Canonical design references:

- `.local/research/v0.8.5_cloud_agent/research.md`
- `.local/research/v0.8.5_cloud_agent/00-decision-matrix-zh.md`

## Hands-off envelope (v0.8.0+)

v0.7.1 introduced the `popolaloom.handoff` substrate (Pydantic v2 `HandoffEnvelope` + Markdown front-matter ser/deser + slug-hash addressing + atomic writer + active/archive 双层); v0.7.2 wired it into `Popolad.dispatch_with_envelope` (E3 internal unification) so every dispatch persists a file-based payload + injects `POPOLA_HANDOFF_FILE` / `POPOLA_HANDOFF_ID` into the spawn env (C5 双通道); v0.7.3 added `popola dispatch --replay`, the HITL `FeedbackEnvelope` companion (Q7=yes), and the legacy `RelayHandoffEnvelope → HandoffEnvelope` bridge.

### Why a file?

- **Length**: prompts above 16 KB blow argv on macOS / hit the kernel `MAX_ARG_STRLEN` cliff on Linux. A file dodges this entirely.
- **Audit**: every dispatch leaves a Markdown receipt under `.local/.agent/handoff/<id>.md` (gitignored as of v0.7.0); inspect with `cat`, search with `grep`, archive with `popola handoff archive`.
- **Replay**: `popola dispatch --replay <id>` re-issues the exact dispatch from disk — the slug-hash id is content-derived so the same prompt/cli/extra always maps to the same id.
- **Cross-CLI handoff**: the existing `relay()` primitive (cursor → claude → codex chain) gets a stable on-disk audit trail per hop (via `to_handoff_envelope` bridge).

### Envelope shape (Markdown front-matter)

```
---
schema_version: '1'
handoff_id: cursor-fix-bug-in-foo-py-3a7f9c1d
created_at: '2026-05-06T14:30:00+00:00'
source_cli: null
target_cli: cursor
parent_task_id: null
cwd: null
adapter_extra: {}
constraints: {}
reason: null
tags: []
---
fix the bug in foo.py — there's a NoneType error around line 42
```

The Pydantic v2 model `popolaloom.handoff.HandoffEnvelope` enforces `extra="forbid"` so unknown front-matter keys raise (No Silent Failures). The id format is `<target_cli>-<slug-from-prompt>-<8hex content hash>` where the hex covers `(target_cli, prompt, parent_task_id, adapter_extra, constraints)`.

### `popola handoff` CLI (filesystem-only — no daemon required)

| Verb | Purpose | Example |
|---|---|---|
| `popola handoff list [--json] [--handoff-dir DIR]` | List active envelopes (newest first) | `popola handoff list` |
| `popola handoff show <id> [--json] [--handoff-dir DIR]` | Print Markdown envelope (or JSON) | `popola handoff show cursor-fix-bug-foo-py-3a7f9c1d` |
| `popola handoff archive <id> <task_id> [--archive-root DIR]` | Snapshot to `<archive_root>/<task_id>/<id>.md` | `popola handoff archive <id> cursor-23e74ec18917` |
| `popola dispatch --replay <id>` | Re-run the exact prior dispatch | `popola dispatch --replay cursor-fix-bug-foo-py-3a7f9c1d` |

`--handoff-dir` overrides the active root resolution order: explicit arg > `$POPOLA_HANDOFF_DIR` env > `.local/.agent/handoff/` (default).

### Channel injection (C5 双通道)

The dispatch path injects two complementary channels:

1. **env (primary, always live)**: `POPOLA_HANDOFF_FILE=<abs path>` + `POPOLA_HANDOFF_ID=<slug-hash>` are added to the spawn env. The agent inside the sub-CLI (cursor-agent / claude / codex / ...) can `cat $POPOLA_HANDOFF_FILE` to inspect the original dispatch — including audit-only fields (`reason`, `tags`) that don't fit into a single argv prompt.
2. **flag (forward-compat, opt-in)**: when `--cli-flag popola_handoff_flag=true` is set, the cmd argv is post-processed to append `--popola-handoff-file <path>`. **Off by default** because vanilla `cursor-agent` / `claude` / `codex` don't recognise the flag yet — auto-injecting would break their argv parsing. The flag stays as a hook for sub-CLIs that gain native support later.

The env-channel always wins over caller-provided base_env keys with the same name (anti-impersonation invariant — see `tests/daemon/test_dispatch_with_envelope.py::test_handoff_with_envelope_overlay_overrides_caller_env`).

### HITL feedback envelope (v0.7.3+, Q7=yes — foundation slice)

`popolaloom.handoff.FeedbackEnvelope` mirrors the dispatch envelope's design choices for HITL answers (the user's typed reply to a `LangGraph.interrupt()` prompt). Filename pattern: `<task_id>-fb-<8hex>.md` (the `-fb-` infix marks feedback files distinct from dispatch envelopes in the same active dir). Schema fields: `feedback_id`, `task_id`, `hitl_id`, `answer` (body), `reason`, `tags`, `responder`, `channel` (which HITL channel — `cli`/`lark`/`ide`/`mcp`/`web` — submitted the reply).

v0.7.3 ships the writer + schema; the live `popola feedback ...` CLI flow does NOT yet auto-persist (avoiding daemon-side coordination risk). Callers can manually call `write_feedback(env)` from custom scripts. v0.8.x patches will add `popola feedback ... --persist` to wire it into the live HITL flow.

### Legacy `RelayHandoffEnvelope` bridge (v0.7.3+)

The v0.3.0 `popolaloom.daemon.primitives.RelayHandoffEnvelope` used by the relay primitive predates the v0.8.0 envelope schema. v0.7.3 ships `to_handoff_envelope(relay_env, prompt=..., cwd=...)` to convert old → new (mapping `source_task_id → parent_task_id`, folding `payload → adapter_extra["_relay_payload"]`, tagging with `"relay-bridged"`). The relay primitive itself still emits the legacy schema unchanged so v0.3.0–v0.7.2 consumers keep working; new code paths can call the bridge then `write_envelope` for file-based audit.

### Programmatic API

```python
from datetime import UTC, datetime
from popolaloom.handoff import (
    HandoffEnvelope,
    FeedbackEnvelope,
    generate_handoff_id,
    generate_feedback_id,
    write_envelope,
    write_feedback,
    list_active_envelopes,
    load_envelope,
    archive_envelope,
)

# Build + write a dispatch envelope manually (rarely needed — Popolad does this for you)
env = HandoffEnvelope(
    handoff_id=generate_handoff_id("cursor", "fix bug"),
    created_at=datetime.now(UTC),
    target_cli="cursor",
    prompt="fix bug",
    reason="user reported during code review",
    tags=["v0.7.x"],
)
path = write_envelope(env)

# Iterate active envelopes
for s in list_active_envelopes():
    print(s.handoff_id, s.path, s.size_bytes, s.mtime)

# Archive a finished task's envelope
archive_envelope(path, "cursor-23e74ec18917")
```

The full module surface (`from popolaloom.handoff import ...`):

| Symbol | Kind | Purpose |
|---|---|---|
| `HandoffEnvelope` | Pydantic v2 | Dispatch envelope schema |
| `FeedbackEnvelope` | Pydantic v2 | HITL feedback companion (v0.7.3+) |
| `HandoffSummary` | frozen dataclass | Lightweight listing entry (id/path/size/mtime) |
| `generate_handoff_id` | function | `<cli>-<slug>-<8hex>` |
| `generate_feedback_id` | function | `<task_id>-fb-<8hex>` |
| `slugify_prompt` | function | Prompt → safe ASCII slug |
| `content_hash` | function | Canonical-JSON SHA-256 first 8 hex |
| `write_envelope` | function | Atomic write to active root |
| `write_feedback` | function | Atomic write of feedback envelope |
| `envelope_path` / `feedback_path` | function | Canonical path resolution (no I/O) |
| `archive_envelope` | function | Copy active → `<archive_root>/<task_id>/` (D4) |
| `archive_dir_for` | function | Canonical archive dir (no I/O) |
| `list_active_envelopes` | function | Enumerate active envelopes |
| `load_envelope` | function | Read + parse a specific envelope |
| `resolve_envelope_path` | function | Canonical path resolution (no I/O) |
| `DEFAULT_HANDOFF_ROOT` | constant | `Path(".local/.agent/handoff")` |
| `DEFAULT_ARCHIVE_ROOT` | constant | `Path(".local/.agent/archive")` |
| `HANDOFF_SCHEMA_VERSION` / `FEEDBACK_SCHEMA_VERSION` | constant | Anchor for forward-compat schema evolution |

## Configuration

PopolaLoom uses environment variables for configuration (per ADR — explicit > implicit). Set them before `popola popolad start`; the daemon picks them up at boot.

| Env var | Purpose | Default |
|---|---|---|
| `POPOLA_HOME` | Daemon socket / events / sqlite / pid root | `~/.popola/` |
| `POPOLA_USE_GRAPH` | Enable the LangGraph subgraph (v0.3.0+; off → no HITL) | `1` |
| `POPOLA_ARKTOWER_MIGRATIONS_DIR` | Override the vendored ArkTower migrations dir | (unset → vendored auto-detect) |
| `CODEX_HOME` | Codex Skill directory + config root | `~/.codex/` |
| `LARK_HITL_TARGET_OPEN_ID` | Recipient `open_id` for HITL prompts + terminal cards | (unset → Lark silent) |
| `LARK_NOTIFY_TARGET_OPEN_ID` | Dedicated terminal-state recipient (split from HITL) | falls back to `LARK_HITL_TARGET_OPEN_ID` |
| `LARK_NOTIFY_ON_COMPLETED` | `task.completed` → green card | `1` (ON) |
| `LARK_NOTIFY_ON_FAILED` | `task.failed` → red card | `1` (ON) |
| `LARK_NOTIFY_ON_CANCELED` | `task.canceled` → yellow card | `1` (ON) |
| `LARK_NOTIFY_ON_CANCEL_ESCALATED` | `cancel → SIGKILL` → orange card | `0` (OFF) |
| `LARK_NOTIFY_PROMPT_TRUNCATE` | Prompt summary char cap (50–2000) | `200` |
| `LARK_PRIORITY_BOT_ID` | Which bot to send via (multi-bot setup) | (unset → default bot) |

`popola init` does **not** export env vars (operator manages `~/.bashrc` / `~/.zshrc` directly), but `popola doctor` displays the current values so you can audit what the daemon will do at next boot.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `popola: command not found` | `pip install` succeeded but `~/.local/bin` not on PATH | `export PATH="$HOME/.local/bin:$PATH"` (and append to shell rc) |
| `popolad failed to bind socket` | Stale socket from a prior daemon | `rm ~/.popola/popolad.sock` then `popola popolad start` |
| `popolad failed to bind socket: still running` | A previous daemon is still alive | `popola popolad stop` first |
| `popola doctor` reports `DRIFT` for a Skill | On-disk SKILL.md version ≠ wheel version (post-`pip install --upgrade` without re-install) | `popola skill upgrade --target=all` |
| `popola doctor` reports `MISS` for a Skill | Slot exists but file is missing | `popola init <ide> [--global \| --project]` |
| Cursor / Claude doesn't auto-load the Skill | Skill discovery happens at IDE startup | Restart the IDE (or open a new chat) |
| `Permission denied` installing globally | System-wide `pip install` requires root | `pip install --user popolaloom` or use a virtualenv |
| `lark-cli not found` (warning in `popola doctor`) | `lark-cli` is missing from PATH | Install lark-cli OR ignore (Lark integration is opt-in) |
| `ArkTower migrations dir not found` | `POPOLA_ARKTOWER_MIGRATIONS_DIR` points at a non-existent dir | Unset it (vendored migrations are auto-detected) OR fix the path |
| `popola list` is empty after dispatch | The dispatched subprocess failed at spawn (e.g. `cursor-agent` not on PATH) | `popola list --all` then `popola status <id>` to see the error in the `failed` envelope |
| `popola attach` shows no events | The task already terminated; the SSE stream replays the log then exits | Use `popola attach --no-follow` for a one-shot dump |
| Daemon log location | — | `$POPOLA_HOME/log/popolad.log` (default `~/.popola/log/popolad.log`) |

## Architecture deep-dive

The daemon is structured as four loosely-coupled subsystems that communicate via the in-process event bus:

1. **RPC server** (`daemon/rpc.py`) — FastAPI app over UDS; handles every CLI / MCP request. Lifespan hook rehydrates the task pool from SQLite on boot and tears down active tasks + the optional `LarkSupervisor` on shutdown.
2. **Task pool** (`popolaloom._vendored.arktower.SqliteTaskRepository`) — durable per-task state machine + the 4 schema migrations. Cross-restart rehydrate uses the SQLite snapshot; the daemon resumes any task whose subprocess pid is still alive after restart.
3. **Subprocess supervisor** (`daemon/supervisor.py`) — spawns each agent CLI with `Popen(start_new_session=True)` so the subprocess survives daemon restart; captures stdout/stderr line-by-line, emits NDJSON events on the bus, watches via a wait-thread, and on terminal state writes the `task.{completed,failed,canceled}` event.
4. **HITL bridge** (`hitl/`) — 5 renderers (lark / ide / cli / mcp / web) consume `task.elicited` events and present prompts. The `hitl/sync.py:mark_answered` is the atomic primitive; the LangGraph state-writeback is what unblocks the subgraph.

The optional `LarkSupervisor` (`lark/supervisor.py`) is wired only when `LARK_HITL_TARGET_OPEN_ID` is set + `lark-cli` is on PATH; it spawns a `lark-cli event consume` listener subprocess and watches it (≤ 3 consecutive deaths → escalate). NFR-2 (status RTT mean ≤ 200ms over 50 samples), NFR-9 (dispatch p95 ≤ 1s over 20 samples), and NFR-1 (cold-start UDS-up ≤ 2s) all have benchmarks under `tests/matrix/nfr/`.

For the visual ASCII-diagram architecture overview, see [`README.md#architecture-tldr`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/README.md#architecture-tldr) — the diagram covers the same surfaces above. For session walkthroughs (with example outputs of `popola init` / `popola doctor` / etc.), see [`DEMO.md`](DEMO.md).

## Reference

- **Canonical Skill** (loaded by every host agent post-`popola init`): `src/popolaloom/skills/popola-loom/SKILL.md`
- **Installer Skill** (v0.7.0+; opt-in for fresh install): `src/popolaloom/skills/install-popola/SKILL.md`
- **Latest release**: [`RELEASE_NOTES.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/RELEASE_NOTES.md)
- **Historical archive**: [`CHANGELOG.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/CHANGELOG.md)
- **Vendoring policy + ArkTower refresh procedure**: [`VENDORING.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/VENDORING.md)
- **Quickstart smoke script**: [`examples/quickstart.sh`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/examples/quickstart.sh)
- **Self-evolution evidence ledgers** (v0.3.x rounds): `evidence/round-{1..5}-evidence.md`
- **Sibling project**: [ArkTower](https://github.com/YoRHa-Agents/ArkTower) (task pool / FSM / SQL migrations source)
- **Per-task quality framework**: [DevolaFlow](https://github.com/YoRHa-Agents/DevolaFlow) (Skill coexists with PopolaLoom's; install both)
