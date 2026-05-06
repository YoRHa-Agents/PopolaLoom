# PopolaLoom — User Guide (v0.7.0)

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
| `popola skill upgrade --target=<ide>` | **Overwrite** installed SKILL.md from the wheel (after `.popolaloom-bak.<ts>` backup) | `popola skill upgrade --target=cursor` |
| `popola skill upgrade --target=all` | Cycle every detected install | `popola skill upgrade --target=all` |
| `popola skill doctor` | Skill-only audit (subset of `popola doctor`) | `popola skill doctor` |

Per-IDE install paths:

| IDE | Scope | Install path |
|---|---|---|
| Cursor | global | `~/.cursor/skills/popolaloom/SKILL.md` |
| Cursor | project | `<repo>/.cursor/skills/popolaloom/SKILL.md` |
| Claude Code | global | `~/.claude/skills/popolaloom/SKILL.md` |
| Claude Code | project | `<repo>/.claude/skills/popolaloom/SKILL.md` |
| Codex | global | `$CODEX_HOME/skills/popolaloom/SKILL.md` (default `~/.codex/`) |
| Copilot | project-only | `<repo>/.github/copilot-instructions.md` (single-file flatten) |
| local | scaffold | `<repo>/.local/` (workspace surface) |

`popola init` differs from `popola skill upgrade` in two ways: (1) `init` is **idempotent** — second invocation prints `SKIP <path> (already installed)`; `upgrade` **always overwrites** (after writing a `.popolaloom-bak.<ts>` backup). (2) `init` is the first-time-installer entry point; `upgrade` is the post-`pip install --upgrade popolaloom` refresh entry point.

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

1. **Skill** — every `(target, scope)` slot from `SKILL_TARGETS`; reports `OK` / `MISS` / `DRIFT` (drift = installed `.popolaloom-version` ≠ wheel version).
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
| **Web** | Browser dashboard (deferred to v0.7.x web surface) | `web/` (placeholder; not yet wired) |

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

For the visual ASCII-diagram architecture overview, see [`README.md#architecture-tldr`](../README.md#architecture-tldr) — the diagram covers the same surfaces above. For session walkthroughs (with example outputs of `popola init` / `popola doctor` / etc.), see [`DEMO.md`](DEMO.md).

## Reference

- **Canonical Skill** (loaded by every host agent post-`popola init`): `src/popolaloom/skills/popolaloom/SKILL.md`
- **Installer Skill** (v0.7.0+; opt-in for fresh install): `src/popolaloom/skills/install-popola/SKILL.md`
- **Latest release**: [`../RELEASE_NOTES.md`](../RELEASE_NOTES.md)
- **Historical archive**: [`../CHANGELOG.md`](../CHANGELOG.md)
- **Vendoring policy + ArkTower refresh procedure**: [`../VENDORING.md`](../VENDORING.md)
- **Quickstart smoke script**: [`../examples/quickstart.sh`](../examples/quickstart.sh)
- **Self-evolution evidence ledgers** (v0.3.x rounds): `evidence/round-{1..5}-evidence.md`
- **Sibling project**: [ArkTower](https://github.com/YoRHa-Agents/ArkTower) (task pool / FSM / SQL migrations source)
- **Per-task quality framework**: [DevolaFlow](https://github.com/YoRHa-Agents/DevolaFlow) (Skill coexists with PopolaLoom's; install both)
