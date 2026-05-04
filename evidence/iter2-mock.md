# PopolaLoom v0.2.0 — Iter-2 Closure Dry-Run (Stub)

**Status**: stubbed (real Iter-2 deferred to human / CI invocation)

**Why this is a stub** (and not a silent skip per workspace "No Silent
Failures" rule): the real Iter-2 closure command requires the
`claude` CLI binary, an authenticated `claude login`, and ~20 minutes
of LLM wall-clock time — none of which the v0.2.0 Stage E task agent
can satisfy in a single session. We therefore **explicitly document**
the deferred work and the exact command + expected outputs so it can
be invoked manually after merge.

## Real Iter-2 command (to be run by human / CI)

```bash
cd /home/agent/workspace/PopolaLoom

# 1. Start popolad daemon (creates ~/.popola/popolad.sock + popolad.pid)
popola popolad start

# 2. Verify daemon is healthy
popola popolad status

# 3. Dispatch the Iter-2 self-evaluation prompt
popola dispatch \
  "用 nines + devola-flow 评估 popolaloom v0.2.0; 输出 markdown 报告 + 14 issue 复测 + 新发现 issue list" \
  --cli claude \
  --wait \
  --timeout 1800 \
  --json \
  | tee /tmp/popola_iter2_dispatched_stdout.txt

# 4. Inspect resulting NDJSON event log
cat ~/.popola/events/iter2-<task_id>.jsonl
```

## Expected outputs

- `~/.popola/events/<task_id>.jsonl` — full per-task NDJSON with:
  - `task.dispatched` (initial envelope)
  - 1+ `process.stdout` lines containing claude's incremental output
  - `task.completed` with `exit_code: 0` (or `task.failed` if claude
    raised)
- `/tmp/popola_iter2_dispatched_stdout.txt` — the JSON dispatch
  response: `{"task_id": "claude-<12hex>", "events_log": "...",
  "cli": "claude"}`
- A markdown report (claude's stdout) listing:
  - 14 Iter-1 issues retest results (PASS / FAIL per R-001 .. R-014)
  - Newly-discovered issues (target ≤ 5 for v0.2.0 DoD §4)

## DoD §4 acceptance threshold

> Iter-2 闭环 (popola dispatch ... --cli claude --wait) 新发现 issue
> ≤ 5 (基准: Iter-1 = 14 个 issue; v0.2.0 修了 ≥ 11 条, 新发现 ≤ 5
> 表示 v0.0.1 issue 没被引入新场景)

If new issues > 5: enter Iter-3 (refactoring + feature-enhancement
composite per `09-iter1-self-eval.md` §6.1). v0.2.0 is **NOT**
released until Iter-2 closes with ≤ 5 new issues.

## What v0.2.0 Stage E actually delivered (for the audit trail)

The Stage E task agent (this session) closed:

- **R-009** Adapter Protocol split — `CommandBuilder` + `Runtime` in
  `src/popolaloom/adapters/base.py`; `AdapterCallback` in
  `daemon/server.py` is now the strict 4-arg signature.
- **R-014 finalisation** — `dispatch_task` honors
  `extra["__events_dir"]` per-task; verified by
  `tests/test_daemon.py::test_dispatch_honors_events_dir_advisory_hint`.
- **R-002 closure** — `popolad.recovered` event emitted by
  `Popolad._emit_recovered_events` after `rehydrate_from_persistence`;
  rehydrate filter widened to all non-terminal ArkTower statuses
  (was previously `IN_PROGRESS+INPUT_REQUIRED` only).
- **S1 self-bootstrap** — `tests/self_bootstrap/test_s1_crash_recovery.py`
  PASS (3s wall clock).
- **S3 self-bootstrap** — `tests/self_bootstrap/test_s3_recursive_dispatch.py`
  PASS (2s wall clock).
- **PopolaLoom-nines runner mvp** — 8-dim scorer set + runner +
  `popola eval run / show` CLI; tests at `tests/test_evaluation.py`
  (9 cases PASS).

## What v0.2.0 explicitly defers to v0.3.0

- **R-010** — `systemd-run --user --scope` full backend (currently
  `subprocess.Popen + start_new_session=True` is sufficient for NFR-5
  ≥99% cross-terminal survival).
- spec §3.4.1 **S2 / S4 / S5** self-bootstrap real versions
  (interrupt+resume, 8-hour offline, 5 concurrent CLIs).
- **Lark HITL bridge** (real `lark-cli` subprocess).
- **Auto-merge gate** (v0.4.0).
- **Textual TUI** / **NiceGUI Web** increments.
- **Prometheus / OTel** observability surface.

## Forensic command — replaying this stub

If reviewing this PR in v0.3.0+ and wondering why Iter-2 wasn't
captured live: re-run the command above against the **same
commit hash** that ships v0.2.0; the results will be the
authoritative Iter-2 closure record for the v0.2.0 release tag.
