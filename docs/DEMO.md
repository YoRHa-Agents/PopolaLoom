# PopolaLoom — DEMO walkthrough (v0.3.5 → v0.4.0 GA)

> 5-minute setup, 5-step automation, 8-dim self-evaluation.

## Quickstart walkthrough

The fastest way to see PopolaLoom working is the
[`examples/quickstart.sh`](../examples/quickstart.sh) script.  It
exercises the 5 canonical demo steps in ~10 seconds:

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
