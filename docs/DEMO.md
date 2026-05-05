# PopolaLoom — DEMO walkthrough (v0.3.5 → v0.5.5)

> 5-minute setup, 6-step automation, 8-dim self-evaluation, multi-IDE
> Skill install + `popola doctor` aggregate health check, and (v0.5.5+)
> an interactive setup wizard.

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
loop's first 5 minutes — see the per-version release notes
(`release-notes-v0.5.{1,2,3,4,5}.md`) for the closure ledger and the
verification commands.

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
  - cursor (project) → /repo/.cursor/skills/popolaloom/SKILL.md
  - claude (project) → /repo/.claude/skills/popolaloom/SKILL.md
  - local (project) → /repo/.local

Proceed with this plan? [Y/n]: y

  Cursor (project) -> /repo/.cursor/skills/popolaloom/SKILL.md
  OK   /repo/.cursor/skills/popolaloom/SKILL.md
  ...
Interactive setup complete.
```

The wizard is mutually-exclusive with `--list` and verb subcommands:
mixing them raises a `BadParameter` (the non-interactive path is
still the default for CI scripts; `--interactive` is a deliberate
human-driven UX surface).

## v0.5.0 Skill installation walkthrough

The fastest path from a fresh checkout to "task running in Cursor +
Lark notification on completion" is the new 6-step flow that ships with
v0.5.0:

```bash
# 1. install (vendored ArkTower — no sibling clone required)
$ pip install popolaloom
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
  OK   /home/agent/.cursor/skills/popolaloom/SKILL.md
  OK   /home/agent/.cursor/skills/popolaloom/.popolaloom-version

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
  cursor global  /home/agent/.cursor/skills/popolaloom/SKILL.md  OK     v0.5.0
  cursor project <repo>/.cursor/skills/popolaloom/SKILL.md       MISS   expected v0.5.0
  claude global  /home/agent/.claude/skills/popolaloom/SKILL.md  MISS   expected v0.5.0
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
[`release-notes-v0.4.1.md`](../release-notes-v0.4.1.md)):

| Trigger | Default | Card colour |
|---|---|---|
| `task.completed` | ON (`LARK_NOTIFY_ON_COMPLETED=1`) | green |
| `task.failed` | ON (`LARK_NOTIFY_ON_FAILED=1`) | red |
| `task.canceled` | ON (`LARK_NOTIFY_ON_CANCELED=1`) | yellow |
| `cancel → SIGKILL` | OFF (`LARK_NOTIFY_ON_CANCEL_ESCALATED=0`) | orange |

> **Screenshots (placeholder for v0.5.1)**:
> `docs/screenshots/popola-doctor-output.png`,
> `docs/screenshots/lark-completion-card.png`,
> `docs/screenshots/cursor-skill-discover.png` — to be added in a
> v0.5.1 doc-only PR alongside the deferred curl-installer (per Q5-5
> lock). The text/output captures above are the ground truth for now.

## Quickstart walkthrough

The fastest way to see PopolaLoom working is the
[`examples/quickstart.sh`](../examples/quickstart.sh) script.  It
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

> **Screenshots (placeholder)**: `docs/screenshots/quickstart-runthrough.png`,
> `docs/screenshots/popola-list-rich-table.png`,
> `docs/screenshots/nines-toml-output.png` — to be added in a follow-up
> doc-only PR.  The exact tooling for capturing them is
> [asciinema](https://asciinema.org/) + [terminalizer](https://terminalizer.com/);
> the demo script above is the ground truth.

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

The 8 dimensions are documented in [`nines.toml`](../nines.toml) +
[`src/popolaloom/evaluation/dimensions/`](../src/popolaloom/evaluation/dimensions/).
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
[`evidence/round-{1..5}-evidence.md`](../evidence/) — each ledger
shows the inner composite, outer Δ, decision (RELEASE / ROLLBACK),
and findings list.

## Where to next

- **NFR slow lane**: `pytest -m slow` runs the cross-process tests
  (NFR-1/2/3/5/8/9 + chaos).
- **Self-bootstrap**: `pytest tests/self_bootstrap -m slow` runs S1..S5.
- **Evidence ledgers**: `evidence/round-1-evidence.md` … `round-5-evidence.md`.
- **GA release notes**: [`release-notes-v0.4.0.md`](../release-notes-v0.4.0.md)
  (after the v0.4.0 bump).
