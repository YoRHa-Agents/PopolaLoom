---
layout: default
title: Demo
description: Product walkthrough, example outputs, design rationale, and implementation flow for PopolaLoom v0.9.7.
lang: en
translation_url: /zh/DEMO.html
---

# PopolaLoom — Product Demo

<!-- updated: 2026-05-10 -->

> One local sidecar daemon turns Cursor, Claude, Codex, Kimi, and Copilot
> into a persistent task bus with auditable handoff files and human-in-the-loop
> fanout.

## Pick your scenario

<div class="scenario-grid">
  <a class="scenario-card" href="#local-single-cli">
    <span class="scenario-card__badge">v0.1.0+</span>
    <h3>Local single-CLI</h3>
    <p>Install, init, dispatch to Cursor, and attach to the durable event stream.</p>
    <span class="scenario-card__link">Jump to path</span>
  </a>
  <a class="scenario-card" href="#cross-cli-handoff">
    <span class="scenario-card__badge">v0.7.0+</span>
    <h3>Cross-CLI handoff</h3>
    <p>Use the Markdown handoff envelope as the audit trail between agents.</p>
    <span class="scenario-card__link">Jump to path</span>
  </a>
  <a class="scenario-card" href="#hitl-pause">
    <span class="scenario-card__badge">v0.4.1+</span>
    <h3>HITL pause</h3>
    <p>Watch Lark, IDE, CLI, MCP, and Web race toward one atomic answer.</p>
    <span class="scenario-card__link">Jump to path</span>
  </a>
  <a class="scenario-card" href="#cloud-agent">
    <span class="scenario-card__badge">v0.8.5+</span>
    <h3>Cloud Agent</h3>
    <p>See where the Cursor Cloud path plugs into the same daemon bus.</p>
    <span class="scenario-card__link">Jump to path</span>
  </a>
  <a class="scenario-card" href="#self-hosted-worker">
    <span class="scenario-card__badge">v0.9.1+</span>
    <h3>Self-hosted worker</h3>
    <p>Start from the worker handoff mental model before opening the visual page.</p>
    <span class="scenario-card__link">Jump to path</span>
  </a>
  <a class="scenario-card" href="#cross-pr-relay">
    <span class="scenario-card__badge">v0.8.8+</span>
    <h3>Cross-PR relay</h3>
    <p>Connect relay history back to file-backed handoff and cloud runs.</p>
    <span class="scenario-card__link">Jump to path</span>
  </a>
</div>

For terminal-recording-style flows across all six scenarios, open the visual [`Demo Page`](demo-page.html).

## What this demo proves

PopolaLoom is not another IDE. It is a local-first control plane over the
agent CLIs already installed on your machine:

1. **One dispatch surface** — `popola dispatch "..." --cli=cursor|claude|codex|copilot`.
2. **Persistent task state** — `popolad` owns process lifetime, task state, and event logs across terminals.
3. **File-backed handoff** — every dispatch writes a Markdown envelope under `.local/.agent/handoff/`.
4. **HITL fanout** — Lark, IDE, CLI, MCP, and Web channels race to provide one atomic answer.

<a id="local-single-cli"></a>

## Five-minute path

```bash
./install.sh install
popola init
popola popolad start
popola dispatch "echo hello from popola" --cli=cursor
popola list --all
popola doctor
```

Expected shape:

```text
cursor-23e74ec18917
Summary: 4/4 subsystems checked. 0 FAIL.
```

<a id="cloud-agent"></a>
<a id="self-hosted-worker"></a>

## Design and implementation flow

```text
popola CLI / MCP tool
  -> UDS RPC
  -> popolad daemon
  -> HandoffEnvelope written to .local/.agent/handoff/
  -> adapter builds cursor/claude/codex argv
  -> subprocess emits stdout/stderr/state events
  -> NDJSON event log + optional Lark card
  -> attach/status/list read the same durable state
```

The important implementation choice is that complex task context is a file
contract, not an argv string. The daemon owns the file, task state, and event
log; each agent CLI stays isolated and native.

<a id="cross-cli-handoff"></a>

## Hands-off envelope walkthrough

Every `popola dispatch` persists a file-based payload that is auditable,
replayable, addressable by content-derived slug-hash, and delivered to the
spawned sub-CLI through environment variables.

```bash
$ popola dispatch "fix the bug in foo.py — there's a NoneType error around line 42" --cli=cursor
# → cursor-1f0a2b8d4e5c   (popola task_id)

# behind the scenes: a Markdown front-matter envelope is written
$ ls .local/.agent/handoff/
cursor-fix-the-bug-in-foo-py-3a7f9c1d.md

$ cat .local/.agent/handoff/cursor-fix-the-bug-in-foo-py-3a7f9c1d.md
---
schema_version: '1'
handoff_id: cursor-fix-the-bug-in-foo-py-3a7f9c1d
created_at: '2026-05-07T10:30:00+00:00'
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

The spawned `cursor-agent` subprocess sees `POPOLA_HANDOFF_FILE=<abs path>`
and `POPOLA_HANDOFF_ID=<slug-hash>` in its env. The agent inside cursor
can `cat $POPOLA_HANDOFF_FILE` to inspect the original dispatch including
audit-only fields (`reason`, `tags`) that don't fit into a single argv
prompt.

### Replay a prior dispatch

```bash
$ popola dispatch --replay cursor-fix-the-bug-in-foo-py-3a7f9c1d
# → cursor-2a8e3f4c5d6e   (new task_id, but same dispatch payload)

# inline overrides emit a stderr warning (No Silent Failures)
$ popola dispatch new-prompt --cli=claude --replay cursor-fix-the-bug-in-foo-py-3a7f9c1d
warning: --replay overrides inline prompt='new-prompt', --cli='claude' with envelope values
# → cursor-9b1c4e7a2d5f   (still cursor + original prompt; warning told you why)
```

### Inspect / list / archive envelopes (no daemon required)

```bash
$ popola handoff list
# Active handoff envelopes
# ┃ handoff_id                                  ┃ size  ┃ mtime               ┃
# │ cursor-fix-the-bug-in-foo-py-3a7f9c1d       │ 412 B │ 2026-05-07 10:30:00 │

$ popola handoff list --json | jq .[0].handoff_id
"cursor-fix-the-bug-in-foo-py-3a7f9c1d"

$ popola handoff show cursor-fix-the-bug-in-foo-py-3a7f9c1d --json | jq .prompt
"fix the bug in foo.py — there's a NoneType error around line 42"

# snapshot a finished task's envelope to .local/.agent/archive/<task_id>/
$ popola handoff archive cursor-fix-the-bug-in-foo-py-3a7f9c1d cursor-1f0a2b8d4e5c
/repo/.local/.agent/archive/cursor-1f0a2b8d4e5c/cursor-fix-the-bug-in-foo-py-3a7f9c1d.md
```

### Why this matters

- **Argv limits dodged** — prompts > 16 KB no longer blow `MAX_ARG_STRLEN`.
- **Audit trail** — `cat`-friendly Markdown receipt for every dispatch.
- **Deterministic replay** — slug-hash addressing means same content → same id.
- **Cross-CLI handoff** — the existing `relay()` primitive (cursor → claude → codex chain) gets a stable on-disk audit trail per hop via the v0.7.3 `to_handoff_envelope()` bridge.
- **HITL feedback companion** — `popolaloom.handoff.FeedbackEnvelope` (Q7=yes, v0.7.3+) mirrors the dispatch envelope schema for HITL answers; foundation slice ships, live `popola feedback ... --persist` wiring scheduled for v0.8.x.

<a id="hitl-pause"></a>

## HITL walkthrough

When a LangGraph node needs human input, it calls `interrupt()` and the daemon
emits a `task.elicited` event:

```text
task.elicited
  -> Lark card
  -> IDE chooser
  -> CLI pending/feedback
  -> MCP elicitation
  -> Web surface
```

The first response wins through `hitl/sync.py:mark_answered`; the state
writeback emits `state.resumed`, and late responders see the already-answered
result instead of racing the task twice.

<a id="cross-pr-relay"></a>

## Historical appendix

### What v0.8.0 added (rolled up from v0.7.1 → v0.7.3)

| Slice | Theme | Test count delta | Coverage |
|---|---|---|---|
| v0.7.1 | 3 v0.7.0 BUG fixes (cancel orphan / rehydrate spawn-aborted / attach `--no-follow` EOF) + handoff foundation (schema/hash/writer/archive) | +114 | 100 % on `popolaloom.handoff.*` |
| v0.7.2 | `Popolad.dispatch_with_envelope` (E3 internal unification) + `_call_adapter` post-process flag injection (C5 双通道) + `popola handoff list/show/archive` CLI + `popolaloom.handoff.loader` | +30 | 100 % maintained |
| v0.7.3 | `popola dispatch --replay` + `FeedbackEnvelope` (Q7=yes HITL foundation) + legacy `RelayHandoffEnvelope` bridge + comprehensive docs | +46 | 100 % maintained |
| v0.8.0 | Documentation-only minor bump — promote v0.7.x foundation to stable surface | 0 | 94.42 % overall |

Total: **76+ new tests since v0.7.0**, default-lane test count 1380 → 1597, `popolaloom.handoff.*` 100 % line + branch coverage.

## v0.7.0 polish

The v0.7.0 minor closes the 4 user-feedback items from v0.6.1 in a
single docs + skill consolidation release. **No source-code logic
changes** — no daemon primitives, no public Python APIs, no schema
migrations. The four threads:

1. **`.local/` is now a strictly local-only workspace surface** —
   gitignored from v0.7.0 onward (NOT deleted; on-disk files are
   preserved by intent so local agent workflows that read
   `.local/feedbacks/`, `.local/memory/specs/`, `.local/eval_reports/`,
   `.local/.agent/` keep working unchanged).
2. **Single floating release notes** — all 10 per-version release-note
   files at the repo root (v0.4.0 → v0.6.1) are removed; their content
   is preserved verbatim in [`CHANGELOG.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/CHANGELOG.md). The new
   [`RELEASE_NOTES.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/RELEASE_NOTES.md) at the repo root is
   overwritten on every release going forward.
3. **Comprehensive docs refresh** — [`README.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/README.md) is
   rewritten as a polished landing page; new
   [`docs/QUICKSTART.md`](QUICKSTART.md) (5-minute onboarding) +
   [`docs/USER_GUIDE.md`](USER_GUIDE.md) (full reference); a
   GitHub Pages-ready Jekyll site under `docs/index.md` +
   `docs/_config.yml` (the Pages site surface; enable in repo Settings → Pages →
   Source = `docs/`).
4. **NEW `install-popola` Skill** — at
   `src/popolaloom/skills/install-popola/SKILL.md` (~165 lines, Tier 1,
   opt-in). Triggers on `install popola` / `/install-popola` /
   `安装 popolaloom`. Walks pip install + per-IDE registration +
   daemon boot + `popola doctor` smoke. Mirrors the conventional
   `/install-devola-flow` slash-command workflow used to install
   DevolaFlow globally. From any host agent (Cursor / Claude / Codex /
   Copilot), say `install popola` and the host walks you through it.

Want the operator-level "what changed" summary? Read
[`RELEASE_NOTES.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/RELEASE_NOTES.md). Want the version-by-version
archive? Read [`CHANGELOG.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/CHANGELOG.md).

## v0.5.x evolution walkthrough (Loops 1–5)

The five v0.5.x patches form a deliberate self-improvement chain
between the v0.5.0 baseline and the forthcoming v0.6.0 minor. The
table below shows what each loop pushed forward + the cumulative
test / coverage delta for the default lane (per the lane filter
``-m "not slow and not nightly and not real_cli and not real_lark"``):

| Loop | Version | Closure focus                                             | Tests Δ          | Coverage Δ        |
|------|---------|-----------------------------------------------------------|------------------|-------------------|
| 1    | v0.5.1  | CI runner-writable fix + 90 error-path tests              | 1104 → 1194 (+90)| 91.15 % → 92.56 % |
| 2    | v0.5.2  | NFR-2 / NFR-9 benchmarks + Lark supervisor shutdown       | 1194 → 1258 (+64)| 92.56 % → 93.37 % |
| 3    | v0.5.3  | vendored arktower CI imports + ruff lint + SKILL `--cli-flag` docs | 1258 → 1258 (+0) | 93.37 % → 93.37 % |
| 4    | v0.5.4  | mutmut surface 1 → 4 + 63 edge / mutation tests           | 1258 → 1321 (+63)| 93.37 % → 93.94 % |
| 5    | v0.5.5  | `popola init --interactive` wizard + mutmut 4 → 5 + vendored migrations + coverage push | 1321 → 1368 (+47)| 93.94 % → 94.60 % |

Each loop's "Known limitations / deferred" section feeds the next
loop's first 5 minutes — see [`CHANGELOG.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/CHANGELOG.md) for
the per-version closure ledger (the historical `[0.5.1]` …
`[0.5.5]` entries) and the verification commands.

## v0.5.5 interactive wizard (NEW)

```bash
$ popola init --interactive
PopolaLoom interactive setup wizard
-----------------------------------
Auto-detected: cursor, claude
Install for Cursor? [Y/n]: y
  Scope for cursor [G=global / P=project] [P]: P
Install for Claude? [Y/n]: y
  Scope for claude [G=global / P=project] [P]: P
Install for Copilot? [y/N]: n
Install for Codex? [y/N]: n
Scaffold .local/ workspace? [Y/n]: y

Install plan:
  - cursor (project) → /repo/.cursor/skills/popola-loom/SKILL.md
  - claude (project) → /repo/.claude/skills/popola-loom/SKILL.md
  - local (project) → /repo/.local

Proceed with this plan? [Y/n]: y

  Cursor (project) -> /repo/.cursor/skills/popola-loom/SKILL.md
  OK   /repo/.cursor/skills/popola-loom/SKILL.md
  ...
Interactive setup complete.
```

The wizard is mutually-exclusive with `--list` and verb subcommands:
mixing them raises a `BadParameter` (the non-interactive path is
still the default for CI scripts; `--interactive` is a deliberate
human-driven UX surface).

## v0.5.0 Skill installation walkthrough

> Note: paths shown reflect the current `popola-loom` naming (renamed
> from `popolaloom` post-v0.7.0). The historical v0.5.0 release shipped
> the older `popolaloom/` skill directory; the rename was applied
> uniformly across this DEMO for clarity.

The fastest path from a fresh checkout to "task running in Cursor +
Lark notification on completion" is the new 6-step flow that ships with
v0.5.0:

```bash
# 1. install (vendored ArkTower — no sibling clone required)
$ pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.5.0
Successfully installed popolaloom-0.5.0

# 2. inspect detected IDEs (read-only — no writes)
$ popola init --list
Detected install targets:
  cursor    project=present  global=missing
  claude    project=present  global=missing
  codex     CODEX_HOME=/home/agent/.codex (present)
  copilot   project=present (single-file)
  local     scaffold=missing

# 3. install Skill into Cursor globally (idempotent)
$ popola init cursor --global
  OK   /home/agent/.cursor/skills/popola-loom/SKILL.md
  OK   /home/agent/.cursor/skills/popola-loom/.popola-loom-version

# 4. start the daemon
$ popola popolad start
popolad started, PID=12345
socket: /home/agent/.popola/popolad.sock
log:    /home/agent/.popola/log/popolad.log

# 5. dispatch a task to Cursor
$ popola dispatch "refactor module X for clarity, add tests" --cli=cursor --json
{"task_id": "cursor-23e74ec18917", "events_log": "/home/.../events/cursor-23e74ec18917.jsonl", "cli": "cursor"}

# 6. one-shot health check across skill + daemon + lark + ArkTower
$ popola doctor
PopolaLoom Doctor Report

Skill audit
  cursor global  /home/agent/.cursor/skills/popola-loom/SKILL.md  OK     v0.5.0
  cursor project <repo>/.cursor/skills/popola-loom/SKILL.md       MISS   expected v0.5.0
  claude global  /home/agent/.claude/skills/popola-loom/SKILL.md  MISS   expected v0.5.0
  ...
Daemon audit
  socket   /home/agent/.popola/popolad.sock                       OK     pid=12345 uptime=3.4s
Lark audit
  lark-cli /usr/local/bin/lark-cli                                OK     binary on PATH
  notify   LARK_HITL_TARGET_OPEN_ID                              WARN   env unset
ArkTower audit
  module   popolaloom._vendored.arktower                         OK     importable
  005 mig  <repo>/migrations/005_popolaloom_extensions.sql        OK     present
  006 mig  <repo>/migrations/006_popola_hitl.sql                  OK     present

Summary: 4/4 subsystems checked. 1 WARN, 0 DRIFT, 0 FAIL.
```

The `popola doctor` exit code is `0` by default (WARN / DRIFT / MISS
are informational); pass `--strict` to escalate any FAIL into a
non-zero exit so CI scripts can hard-gate on it. The `--json` flag
emits a 4-section envelope (`skill` / `daemon` / `lark` / `arktower`
+ a `summary` rollup) for programmatic consumers.

### Lark notification subsection (v0.4.1+)

When `lark-cli` is installed AND `LARK_HITL_TARGET_OPEN_ID` is set,
the daemon proactively sends interactive cards on every terminal
state (per the v0.4.1 minor; see
[`CHANGELOG.md` §0.4.1](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/CHANGELOG.md)):

| Trigger | Default | Card colour |
|---|---|---|
| `task.completed` | ON (`LARK_NOTIFY_ON_COMPLETED=1`) | green |
| `task.failed` | ON (`LARK_NOTIFY_ON_FAILED=1`) | red |
| `task.canceled` | ON (`LARK_NOTIFY_ON_CANCELED=1`) | yellow |
| `cancel → SIGKILL` | OFF (`LARK_NOTIFY_ON_CANCEL_ESCALATED=0`) | orange |

## Quickstart walkthrough

The fastest way to see PopolaLoom working is the
[`examples/quickstart.sh`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/examples/quickstart.sh) script.  It
exercises the 6 canonical demo steps (Steps 0–5: `popola init` dry-run
→ daemon start → dispatch → list → status → `popola doctor` → daemon
stop) in ~10 seconds. The historical 5-step output below is preserved
as a v0.3.5 reference; for the v0.5.0 6-step variant see the
"v0.5.0 Skill installation walkthrough" section above.

```bash
$ bash examples/quickstart.sh
[quickstart] Step 1/5: starting popolad in POPOLA_HOME=/tmp/popolaloom-quickstart-4YGM4j
popolad started, PID=3396800
socket: /tmp/popolaloom-quickstart-4YGM4j/popolad.sock
log:    /tmp/popolaloom-quickstart-4YGM4j/log/popolad.log

[quickstart] Step 2/5: dispatching echo task via cursor adapter
{"task_id": "cursor-518d80e12754", "events_log": "/tmp/.../cursor-518d80e12754.jsonl", "cli": "cursor"}
[quickstart] dispatched task_id=cursor-518d80e12754

[quickstart] Step 3/5: confirming task appears in popola list
[quickstart]  ✓ task_id 'cursor-518d80e12754' present in list

[quickstart] Step 4/5: running popola eval run → /tmp/.../quickstart-nines.toml
composite=0.725 → /tmp/.../quickstart-nines.toml
[quickstart]  ✓ 8/8 dimensions present, composite=0.725
[quickstart]    attach_correctness       1.000
[quickstart]    cross_cli_handoff        0.500
[quickstart]    cycle_convergence        1.000
[quickstart]    dispatch_isolation       0.500
[quickstart]    event_log_completeness   1.000
[quickstart]    hitl_handleability       0.500
[quickstart]    hitl_latency             0.500
[quickstart]    single_threaded_writes   1.000

[quickstart] Step 5/5: stopping popolad
sending SIGTERM to popolad PID=3396800
popolad PID=3396800 exited gracefully
[quickstart] all 5 steps PASS — popolaloom v0.3.5 ready
```

## Step-by-step deep dive

### 1. Daemon lifecycle

`popola popolad start` spawns `python -m popolaloom.daemon` with
`start_new_session=True` (per spec §6 NFR-5: cross-terminal survival)
and binds a Unix Domain Socket at `$POPOLA_HOME/popolad.sock`.

```bash
$ popola popolad start
popolad started, PID=12345
socket: /home/user/.popola/popolad.sock
log:    /home/user/.popola/log/popolad.log

$ popola popolad status
popolad PID=12345  uptime=12.3s  socket=/home/user/.popola/popolad.sock  active_tasks=0
```

NFR-1 quantitative gate: cold-start UDS-up time ≤ 2 s (median ~250 ms
on dev container).  See `tests/matrix/nfr/test_nfr_1_startup_latency.py`.

### 2. Task dispatch — `popola dispatch`

The 7 dispatch verbs are all available via the CLI + the MCP server.
The simplest is `popola dispatch`:

```bash
$ popola dispatch "find all TODOs in src/" --cli cursor --wait --timeout 30
{
  "task_id": "cursor-deadbeef",
  "exit_code": 0,
  "completed_at": "2026-05-04T12:00:42.123Z"
}
```

NFR-9 quantitative gate: `POST /dispatch` p95 ≤ 1 s (measured ~80 ms
median on dev container).  See `tests/matrix/nfr/test_nfr_9_dispatch_p95.py`.

### 3. Task inspection — `popola list` / `popola status` / `popola attach`

```bash
$ popola list --all
┃ task_id            ┃ cli    ┃ state     ┃ pid    ┃ started_at        ┃
┃ cursor-deadbeef    ┃ cursor ┃ completed ┃ 12888  ┃ 2026-05-04 12:00  ┃
┃ claude-cafebabe    ┃ claude ┃ running   ┃ 12999  ┃ 2026-05-04 12:01  ┃

$ popola status cursor-deadbeef
state: completed
pid: 12888
exit_code: 0
events_log: /home/user/.popola/events/cursor-deadbeef.jsonl
arktower_task_id: 0192abcd-...
persisted: true

$ popola attach claude-cafebabe --follow   # SSE stream of NDJSON events
2026-05-04T12:01:05.000Z  task.dispatched   {prompt: ...}
2026-05-04T12:01:05.123Z  process.started   {pid: 12999}
2026-05-04T12:01:08.456Z  task.elicited     {hitl_id: hitl-x, why: ...}
...
```

NFR-2 quantitative gate: `GET /status` mean RTT ≤ 200 ms (measured
~0.35 ms on dev container).  See `tests/matrix/nfr/test_nfr_2_status_latency.py`.

### 4. 8-dim self-evaluation — `popola eval run`

```bash
$ popola eval run --output ./nines.toml
composite=0.92 → ./nines.toml

$ cat ./nines.toml
version = "0.3.5"
timestamp = "2026-05-04T12:30:00.000Z"
composite = 0.92

[dimensions]
dispatch_isolation = 0.95
cycle_convergence = 1.00
hitl_latency = 0.90
attach_correctness = 1.00
cross_cli_handoff = 0.85
single_threaded_writes = 1.00
event_log_completeness = 0.95
hitl_handleability = 0.88

[weights]
dispatch_isolation = 0.15
cycle_convergence = 0.15
hitl_latency = 0.15
attach_correctness = 0.10
cross_cli_handoff = 0.15
single_threaded_writes = 0.10
event_log_completeness = 0.10
hitl_handleability = 0.10
```

The 8 dimensions are documented in [`nines.toml`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/nines.toml) +
[`src/popolaloom/evaluation/dimensions/`](https://github.com/YoRHa-Agents/PopolaLoom/tree/main/src/popolaloom/evaluation/dimensions).
Each scorer has a per-dimension evidence pipeline (round-3 v0.3.3
finally wired `lark_health` to real fixture-driven measurement).

### 5. HITL with Lark + IDE notifications

The `hitl/` module ships 5 channel renderers (lark / ide / cli / mcp /
web).  When a task hits `await interrupt(prompt)` inside a LangGraph
subgraph, the daemon broadcasts the prompt to all 5 channels
simultaneously; whichever responder wins the cross-channel sync race
gets to answer (atomic `mark_answered` per `hitl/sync.py`).

```bash
$ popola pending          # show prompts awaiting human response
hitl_id     trigger     why                       options    deadline
hitl-abc12  approval    Confirm destructive merge yes,no     5h30m

$ popola feedback hitl-abc12 yes --reason "verified backup taken"
ok=true  via=cli
```

The Lark out path uses `lark-cli im +send --card '<json>'
--metadata-key hitl_id=hitl-abc12 ...` with the mandatory
`---\n本消息由飞书工具 Lark-Cli 发送` footer (workspace rule); the in
path uses a `lark-cli event consume <events>` listener subprocess
managed by `LarkSupervisor` (≤ 3 restarts → escalate per round-3
chaos test).

## MCP integration

PopolaLoom's MCP server exposes the same 7 dispatch verbs to IDE
Agents via stdio:

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

After restart, Cursor sees `popola_submit / popola_list / popola_status /
popola_attach_stream / popola_cancel / popola_relay / popola_supervise /
popola_federate / popola_supply_feedback / popola_inject_subtask`.  The
elicitation builder (`popolaloom.mcp.elicitation`) renders pending HITL
prompts as form-mode requests so the IDE can surface them as a chooser
UI.

## Self-evolution loop

```bash
# 1. devola-flow design + dispatch
popola dispatch "用 nines + devola-flow 评估 popolaloom 当前版本" --cli claude --wait

# 2. inner gate + outer gate (dual_gate.py)
#   inner = parsed L3 stdout's "## Acceptance Verification + Gate Score
#           Components + Findings" sections, composite ≥ 0.85
#   outer = popola eval run delta vs prior round, ≥ +0.02

# 3. auto-merge gate (gate/automerge.py — 5 AND conditions)

# 4. round bump (e.g. v0.3.0 → v0.3.1) + CHANGELOG + evidence/round-N-evidence.md
```

The 5-round v0.3.x cycle is fully documented in
[`evidence/round-{1..5}-evidence.md`](https://github.com/YoRHa-Agents/PopolaLoom/tree/main/evidence) — each ledger
shows the inner composite, outer Δ, decision (RELEASE / ROLLBACK),
and findings list.

## Where to next

- **NFR slow lane**: `pytest -m slow` runs the cross-process tests
  (NFR-1/2/3/5/8/9 + chaos).
- **Self-bootstrap**: `pytest tests/self_bootstrap -m slow` runs S1..S5.
- **Evidence ledgers**: `evidence/round-1-evidence.md` … `round-5-evidence.md`.
- **Latest release notes**: [`RELEASE_NOTES.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/RELEASE_NOTES.md)
  (overwritten per release; v0.7.0+ policy).
- **Historical archive (v0.0.1 → present)**:
  [`CHANGELOG.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/CHANGELOG.md) — search for `## [0.4.0]` for the
  GA release notes; `[0.4.1]` for the Lark notification minor; etc.
