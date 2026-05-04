"""F1 real measurement tests for the 8 PopolaLoom-nines dimensions.

Per v0.3.0-plan.md §4 Stage F1, each dimension scorer has its own
evidence pipeline + at least one real-measurement test (no mvp /
estimate / placeholder semantics).  This file covers:

- 1 case per dimension (8 cases) verifying real measurement against
  fabricated evidence
- 2 integration cases: full ``run_evaluation`` on (a) tmp_path empty
  events_dir → composite ≥ 0.5; (b) synthesized event log fixtures
  → composite ≥ 0.85
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from popolaloom.evaluation.dimensions import (
    AttachCorrectness,
    CrossCliHandoff,
    CycleConvergence,
    DispatchIsolation,
    EventLogCompleteness,
    HitlLatency,
    SingleThreadedWrites,
    TokenBudgetCompliance,
)
from popolaloom.evaluation.dimensions.event_log_completeness import hash_event_sequence
from popolaloom.evaluation.runner import collect_evidence, run_evaluation

# ── F1.1 — DispatchIsolation real PGID lookup ─────────────────────────────


def test_dispatch_isolation_real_pgid_difference() -> None:
    """Spawn a subprocess in its own session, verify scorer detects PGID split.

    Real measurement: the daemon and a setsid-spawned CLI subprocess
    MUST have different PGIDs.  The scorer calls :func:`os.getpgid` on
    each and returns 1.0 when they differ.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        start_new_session=True,
    )
    try:
        evidence = {
            "daemon_pid": os.getpid(),
            "cli_pid": proc.pid,
        }
        score = DispatchIsolation().score(evidence)
        assert score == 1.0, (
            f"daemon vs setsid CLI must have distinct PGIDs (got score={score})"
        )
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# ── F1.2 — CycleConvergence runs subgraph for real ────────────────────────


def test_cycle_convergence_runs_real_subgraph() -> None:
    """Score with empty evidence → triggers live subgraph run; assert convergence.

    Real measurement: with no ``cycle_demo_iters`` in evidence the
    scorer invokes :func:`build_dev_test_subgraph` with the canonical
    ``[0.5, 0.9]`` score sequence and checks the verifier converged
    in ≤ 2 iters.
    """
    evidence: dict[str, object] = {"cycle_demo_present": True}
    score = CycleConvergence().score(evidence)
    assert score == 1.0, (
        f"deterministic [0.5, 0.9] sequence must converge in 2 iters (got {score})"
    )


# ── F1.3 — HitlLatency median computation from list ───────────────────────


def test_hitl_latency_median_from_list() -> None:
    """``hitl_round_trips`` list → median ms → linear scale 1.0@1000 → 0.0@10000."""
    fast = HitlLatency().score({"hitl_round_trips": [500.0, 800.0, 1000.0]})
    assert fast == 1.0, f"median 800ms → 1.0; got {fast}"

    mid = HitlLatency().score({"hitl_round_trips": [4500.0, 5500.0, 5500.0]})
    assert 0.4 < mid < 0.6, f"median 5500ms → ~0.5; got {mid}"

    slow = HitlLatency().score({"hitl_round_trips": [12_000.0]})
    assert slow == 0.0, f"median 12s → 0.0; got {slow}"


# ── F1.4 — AttachCorrectness file vs tail count ───────────────────────────


def test_attach_correctness_file_vs_tail_match(tmp_path: Path) -> None:
    """File line count == in-memory tail count → 1.0; mismatch → 0.0."""
    log = tmp_path / "task.jsonl"
    log.write_text("line1\nline2\nline3\n", encoding="utf-8")

    matched = AttachCorrectness().score(
        {"attach_event_log_paths": [str(log)], "attach_tail_counts": [3]}
    )
    assert matched == 1.0, f"file=3 lines vs tail=3 → 1.0; got {matched}"

    mismatched = AttachCorrectness().score(
        {"attach_event_log_paths": [str(log)], "attach_tail_counts": [5]}
    )
    assert mismatched == 0.0, f"file=3 vs tail=5 → 0.0; got {mismatched}"


# ── F1.5 — CrossCliHandoff F1 placeholder ─────────────────────────────────


def test_cross_cli_handoff_returns_placeholder_in_f1() -> None:
    """F1 stub returns 0.5 unless explicit success evidence supplied."""
    score = CrossCliHandoff().score({})
    assert score == 0.5, f"F1 stub MUST return 0.5; got {score} (F5 will lift this)"


# ── F1.6 — SingleThreadedWrites grep src ──────────────────────────────────


def test_single_threaded_writes_finds_locks_in_src() -> None:
    """Live source-tree grep MUST find threading.Lock in 3 critical modules.

    Per v0.3.0 F1.6 + workspace No Silent Failures rule: event_log,
    state_store, and server source files MUST each contain at least
    one ``threading.Lock`` allocation.
    """
    score = SingleThreadedWrites().score({})
    assert score == 1.0, (
        f"all 3 (event_log/state/server) MUST have threading.Lock; got {score}"
    )


# ── F1.7 — EventLogCompleteness SHA256 hash compare ───────────────────────


def test_event_log_completeness_sha256_match() -> None:
    """SHA256(dispatched_ids) == SHA256(attached_ids) → 1.0."""
    ids = ["e1", "e2", "e3"]
    h = hash_event_sequence(ids)
    score = EventLogCompleteness().score(
        {"dispatched_event_hash": h, "attached_event_hash": h}
    )
    assert score == 1.0, f"matching hashes → 1.0; got {score}"

    mismatch = EventLogCompleteness().score(
        {
            "dispatched_event_hash": h,
            "attached_event_hash": hash_event_sequence(["e1", "e2"]),
        }
    )
    assert mismatch == 0.0, f"different hashes → 0.0; got {mismatch}"


# ── F1.8 — TokenBudgetCompliance parses usage events ──────────────────────


def test_token_budget_compliance_parses_stream_json_usage() -> None:
    """``token_usage_events`` list within budget → 1.0; over → 0.0."""
    within = TokenBudgetCompliance().score(
        {
            "token_usage_events": [
                {"input_tokens": 1000, "output_tokens": 500},
                {"input_tokens": 2000, "output_tokens": 1500},
            ],
            "token_max_budget": 10_000,
        }
    )
    assert within == 1.0, f"5000 ≤ 10000 → 1.0; got {within}"

    over = TokenBudgetCompliance().score(
        {
            "token_usage_events": [
                {"input_tokens": 60_000, "output_tokens": 50_000},
            ],
            "token_max_budget": 100_000,
        }
    )
    assert over == 0.0, f"110_000 > 100_000 → 0.0; got {over}"

    no_usage = TokenBudgetCompliance().score({"token_usage_events": []})
    assert no_usage == 0.5, f"empty list → 0.5 placeholder; got {no_usage}"


# ── F1 integration — empty events_dir composite ≥ 0.5 ────────────────────


def test_full_run_empty_events_dir_composite_above_floor(tmp_path: Path) -> None:
    """Empty events_dir → composite ≥ 0.5 (mostly placeholders + locks 1.0)."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    report = run_evaluation(events_dir=events_dir)
    assert report.composite >= 0.5, (
        f"empty events_dir composite must stay ≥ 0.5 floor; got {report.composite:.4f} "
        f"(dimensions={report.dimensions})"
    )


# ── F1 integration — synthesized fixtures composite ≥ 0.85 ───────────────


def _write_synthetic_event_log(path: Path) -> None:
    """Write a synthetic NDJSON event log that scores high on all dimensions."""
    base = datetime.now(UTC).replace(microsecond=0)
    lines = []
    events = [
        ("task.dispatched", {}),
        ("process.stdout", {"line": "hello"}),
        ("task.elicited", {"prompt_id": "h1"}),
        ("human.responded", {"prompt_id": "h1", "option_id": "yes"}),
        ("claude.stream", {"usage": {"input_tokens": 100, "output_tokens": 50}}),
        ("task.completed", {"exit_code": 0}),
    ]
    for offset, (type_, data) in enumerate(events):
        envelope = {
            "specversion": "1.0",
            "id": f"evt-{offset}",
            "source": "popola/synth",
            "type": type_,
            "time": (base + timedelta(milliseconds=offset * 200)).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            "data": data,
        }
        lines.append(json.dumps(envelope, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_full_run_synthesized_fixture_composite_high(tmp_path: Path) -> None:
    """Synthesized event log → composite ≥ 0.85 (target for healthy daemon).

    This validates the F1 evidence pipeline end-to-end: the runner's
    :func:`collect_evidence` reads the synthetic log, computes hashes
    + tails + HITL round-trip + token usage, and the 8 dimensions
    score high.
    """
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    _write_synthetic_event_log(events_dir / "task-001.jsonl")

    evidence = collect_evidence(events_dir)
    evidence["daemon_pgid"] = 100
    evidence["cli_pgid"] = 200
    evidence["handoff_chain_intact"] = True
    evidence["handoff_owned_files_disjoint"] = True

    report = run_evaluation(evidence=evidence)
    assert report.composite >= 0.85, (
        f"synthesized fixture composite must be ≥ 0.85; got {report.composite:.4f} "
        f"(dimensions={report.dimensions})"
    )
    assert report.dimensions["dispatch_isolation"] == 1.0
    assert report.dimensions["single_threaded_writes"] == 1.0
    assert report.dimensions["hitl_latency"] == 1.0


# ── F1 collect_evidence enriched fields ──────────────────────────────────


def test_collect_evidence_enriches_v030_keys(tmp_path: Path) -> None:
    """``collect_evidence`` exposes all v0.3.0 enriched evidence keys."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    _write_synthetic_event_log(events_dir / "task-002.jsonl")

    evidence = collect_evidence(events_dir)
    assert evidence["dispatched_event_hash"] is not None
    assert evidence["attached_event_hash"] == evidence["dispatched_event_hash"]
    assert evidence["attach_event_log_paths"] is not None
    assert evidence["attach_tail_counts"] is not None
    assert len(evidence["attach_event_log_paths"]) == 1
    assert evidence["hitl_round_trips"] is not None
    assert evidence["hitl_round_trips"][0] >= 0.0
    assert evidence["token_usage_events"] is not None


# ── Coverage extension cases ──────────────────────────────────────────────


def test_dispatch_isolation_pid_only_fallback() -> None:
    """When PIDs supplied but PGID lookup fails (process gone), use PID compare."""
    score = DispatchIsolation().score({"daemon_pid": 99999999, "cli_pid": 99999998})
    assert score == 1.0


def test_attach_correctness_no_paths_returns_placeholder() -> None:
    """No event log paths supplied → 0.5 placeholder."""
    score = AttachCorrectness().score(
        {"attach_event_log_paths": [], "attach_tail_counts": []}
    )
    assert score == 0.5


def test_attach_correctness_unreadable_file_skipped(tmp_path: Path) -> None:
    """Non-existent file is skipped (counts toward checked=0 → placeholder)."""
    fake = tmp_path / "does-not-exist.jsonl"
    score = AttachCorrectness().score(
        {"attach_event_log_paths": [str(fake)], "attach_tail_counts": [0]}
    )
    assert score == 0.5


def test_hitl_latency_invalid_list_falls_back_to_seconds() -> None:
    """Non-numeric hitl_round_trips → fall back to hitl_round_trip_seconds."""
    score = HitlLatency().score(
        {
            "hitl_round_trips": ["bad", "data"],
            "hitl_round_trip_seconds": 0.5,
        }
    )
    assert score == 1.0


def test_token_budget_compliance_invalid_max_budget_falls_back_to_default() -> None:
    """Non-numeric token_max_budget → use default 200_000."""
    score = TokenBudgetCompliance().score(
        {
            "token_usage_events": [{"input_tokens": 100}],
            "token_max_budget": "not-a-number",
        }
    )
    assert score == 1.0


def test_cross_cli_handoff_chain_intact_only_returns_placeholder() -> None:
    """Only chain_intact set (no owned_files_disjoint) → placeholder."""
    score = CrossCliHandoff().score({"handoff_chain_intact": True})
    assert score == 0.5


def test_event_log_completeness_partial_recovery() -> None:
    """Partial recovery (after_count < before+recovered) → ratio score."""
    score = EventLogCompleteness().score(
        {
            "event_count_before_recovery": 10,
            "event_count_after_recovery": 11,
            "recovered_count": 3,
        }
    )
    assert 0.0 < score < 1.0


def test_single_threaded_writes_partial_locks_evidence() -> None:
    """Subset of locks_present → graded score."""
    score = SingleThreadedWrites().score(
        {"locks_present": {"_event_logs_lock", "state_store_lock"}}
    )
    assert score == 0.66


def test_cycle_convergence_live_subgraph_path() -> None:
    """Trigger the live subgraph invocation path (no cycle_demo_iters in evidence)."""
    score = CycleConvergence().score({"cycle_demo_present": True})
    assert score == 1.0, "deterministic [0.5, 0.9] sequence converges in 2 iters"


def test_cycle_convergence_invalid_iters_returns_placeholder() -> None:
    """Non-numeric cycle_demo_iters → 0.5 placeholder."""
    score = CycleConvergence().score(
        {"cycle_demo_present": True, "cycle_demo_iters": "bad"}
    )
    assert score == 0.5


def test_token_budget_compliance_invalid_violations_returns_placeholder() -> None:
    """Non-numeric token_budget_violations falls back to placeholder."""
    score = TokenBudgetCompliance().score({"token_budget_violations": "bad"})
    assert score == 0.5


def test_dispatch_isolation_falls_back_to_pid_when_pgid_zero() -> None:
    """daemon_pgid=0 + cli_pgid=0 → still treated as supplied (compare equal → 0)."""
    score = DispatchIsolation().score({"daemon_pgid": 0, "cli_pgid": 0})
    assert score == 0.0


def test_event_log_completeness_invalid_counts() -> None:
    """Non-numeric counts → 0.5 placeholder."""
    score = EventLogCompleteness().score(
        {
            "event_count_before_recovery": "bad",
            "event_count_after_recovery": 1,
            "recovered_count": 1,
        }
    )
    assert score == 0.5


def test_attach_correctness_legacy_invalid_total() -> None:
    """Legacy attach_complete_count with non-numeric → 0.5."""
    score = AttachCorrectness().score(
        {"attach_complete_count": "bad", "attach_total_count": "x"}
    )
    assert score == 0.5
