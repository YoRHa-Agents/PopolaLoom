"""PopolaLoom-nines self-evaluation tests (v0.2.0 Stage E E5).

Coverage targets per the v0.2.0 Stage E plan (≥ 3 cases):

1. ``test_8_dimensions_present`` — :data:`DIMENSIONS` has exactly 8
   instances and matches the canonical names in ``nines.toml``.
2. ``test_composite_weighted_correctly`` — given fake scores, the
   composite folds as ``sum(score × weight)``.
3. ``test_run_evaluation_produces_toml_output`` — running on a tmp
   events_dir produces a TOML file that ``tomllib`` can re-parse and
   contains all 8 dimensions.

Plus bonus cases that increase confidence:

4. ``test_dispatch_isolation_scores`` — DispatchIsolation.score on
   different evidence shapes returns the documented values.
5. ``test_event_log_completeness_scores`` — counts maths verified.
6. ``test_cli_eval_run_command_smoke`` — Typer CliRunner test that the
   ``popola eval run`` end-to-end pipeline succeeds + writes a file.
"""

from __future__ import annotations

import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from popolaloom.cli import app as popola_root_app
from popolaloom.evaluation import (
    DIMENSIONS,
    AttachCorrectness,
    CrossCliHandoff,
    CycleConvergence,
    DimensionScorer,
    DispatchIsolation,
    EventLogCompleteness,
    HitlLatency,
    NinesReport,
    SingleThreadedWrites,
    collect_evidence,
    run_evaluation,
    toml_serialize,
)

# ── 1. DIMENSIONS canonical list ────────────────────────────────────────


def test_8_dimensions_present() -> None:
    """``DIMENSIONS`` exposes exactly the 8 PopolaLoom-nines dimensions.

    Names must match ``nines.toml [eval] dimensions`` exactly so the
    runner's weight lookup works without rename mappings.
    """
    # v0.3.0 F4.E: token_budget_compliance → hitl_handleability swap (D3.10).
    expected_names = {
        "dispatch_isolation",
        "cycle_convergence",
        "hitl_latency",
        "attach_correctness",
        "cross_cli_handoff",
        "single_threaded_writes",
        "event_log_completeness",
        "hitl_handleability",
    }
    assert len(DIMENSIONS) == 8, f"expected 8 dimensions, got {len(DIMENSIONS)}"

    actual_names = {dim.name for dim in DIMENSIONS}
    assert actual_names == expected_names, (
        f"dimension names mismatch:\n"
        f"  expected: {sorted(expected_names)}\n"
        f"  actual:   {sorted(actual_names)}"
    )

    for dim in DIMENSIONS:
        assert isinstance(dim, DimensionScorer), (
            f"{type(dim).__name__} does not satisfy DimensionScorer Protocol"
        )

    from popolaloom.evaluation.dimensions.hitl_handleability import (
        HitlHandleability,
    )

    type_classes = {
        DispatchIsolation,
        CycleConvergence,
        HitlLatency,
        AttachCorrectness,
        CrossCliHandoff,
        SingleThreadedWrites,
        EventLogCompleteness,
        HitlHandleability,
    }
    actual_classes = {type(dim) for dim in DIMENSIONS}
    assert actual_classes == type_classes


# ── 2. composite weighted correctly ─────────────────────────────────────


def test_composite_weighted_correctly() -> None:
    """``run_evaluation`` folds per-dim scores as ``sum(score × weight)``.

    Hand-craft an evidence dict whose every dimension scores ``1.0``
    (for exact reproducibility), confirm the composite is the sum of
    weights (which is 1.0 by construction in ``nines.toml``).
    """
    handoff_evidence = {
        "daemon_pgid": 100,
        "cli_pgid": 200,
        "cycle_demo_present": True,
        "cycle_demo_iters": 1,
        "hitl_round_trip_seconds": 0.5,
        "attach_complete_count": 5,
        "attach_total_count": 5,
        "handoff_successful_count": 1,
        "locks_present": {"_event_logs_lock", "state_store_lock", "event_log_lock"},
        "event_count_before_recovery": 10,
        "event_count_after_recovery": 13,
        "recovered_count": 3,
        # v0.3.0 F4.E: hitl_handleability evidence keys (per dimensions/hitl_handleability.py).
        "hitl_prompts_emitted": 10,
        "hitl_schema_failures": 0,
        "hitl_replies_received": 10,
        "hitl_replies_parsed": 10,
        "cross_channel_sync_total": 10,
        "cross_channel_sync_winners": 10,
        "lark_send_total": 10,
        "lark_send_ok": 10,
        "lark_listener_uptime_total_s": 100,
        "lark_listener_uptime_alive_s": 100,
        "lark_roundtrip_total": 10,
        "lark_roundtrip_under_10s": 10,
    }

    report = run_evaluation(evidence=handoff_evidence)

    for name, score in report.dimensions.items():
        assert score == 1.0, f"expected 1.0 for {name}, got {score}"

    total_weight = sum(report.weights.values())
    assert abs(report.composite - total_weight) < 1e-9, (
        f"composite={report.composite} should equal sum(weights)={total_weight}"
    )


def test_composite_partial_scores() -> None:
    """A mixed evidence dict produces the documented partial composite."""
    evidence = {
        "daemon_pgid": 100,
        "cli_pgid": 100,
        "cycle_demo_present": True,
        "cycle_demo_iters": 4,
        "hitl_round_trip_seconds": 6.0,
        "attach_complete_count": 1,
        "attach_total_count": 2,
        "handoff_successful_count": 0,
        "locks_present": {"_event_logs_lock", "state_store_lock", "event_log_lock"},
        "event_count_before_recovery": 10,
        "event_count_after_recovery": 12,
        "recovered_count": 3,
        "token_budget_violations": 1,
    }
    report = run_evaluation(evidence=evidence)
    assert report.dimensions["dispatch_isolation"] == 0.0
    assert report.dimensions["cycle_convergence"] == 0.5
    assert report.dimensions["hitl_latency"] == 0.5
    assert report.dimensions["attach_correctness"] == 0.5
    assert report.dimensions["cross_cli_handoff"] == 0.0
    assert report.dimensions["single_threaded_writes"] == 1.0
    assert 0.0 < report.dimensions["event_log_completeness"] < 1.0
    # v0.3.0 F4.E: with no hitl evidence, hitl_handleability returns
    # the placeholder 0.5 (mirrors v0.2.0 token_budget_compliance fallback).
    assert report.dimensions["hitl_handleability"] == 0.5


# ── 3. run_evaluation produces TOML output ──────────────────────────────


def test_run_evaluation_produces_toml_output(tmp_path: Path) -> None:
    """``run_evaluation`` against an empty tmp events_dir → valid TOML report."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()

    report = run_evaluation(events_dir=events_dir)

    assert isinstance(report, NinesReport)
    assert isinstance(report.timestamp, datetime)
    assert report.timestamp.tzinfo is UTC or report.timestamp.tzinfo is not None
    assert report.version
    assert 0.0 <= report.composite <= 1.0
    assert len(report.dimensions) == 8

    serialised = toml_serialize(report)
    parsed = tomllib.loads(serialised)
    assert parsed["version"] == report.version
    assert "composite" in parsed
    assert isinstance(parsed["dimensions"], dict)
    assert set(parsed["dimensions"]) == {dim.name for dim in DIMENSIONS}
    assert "timestamp" in parsed

    output_path = tmp_path / "nines-iter2.toml"
    output_path.write_text(serialised, encoding="utf-8")
    re_read = tomllib.loads(output_path.read_text(encoding="utf-8"))
    assert re_read == parsed


# ── 4. DispatchIsolation scoring matrix ─────────────────────────────────


def test_dispatch_isolation_scores() -> None:
    """DispatchIsolation: PGIDs differ → 1.0; same → 0.0; missing → 0.5."""
    scorer = DispatchIsolation()
    assert scorer.score({"daemon_pgid": 100, "cli_pgid": 200}) == 1.0
    assert scorer.score({"daemon_pgid": 100, "cli_pgid": 100}) == 0.0
    assert scorer.score({"daemon_pid": 1, "cli_pid": 2}) == 1.0
    assert scorer.score({"daemon_pid": 1, "cli_pid": 1}) == 0.0
    assert scorer.score({}) == 0.5


# ── 5. EventLogCompleteness counts math ─────────────────────────────────


def test_event_log_completeness_scores() -> None:
    """EventLogCompleteness: after ≥ before+recovered → 1.0."""
    scorer = EventLogCompleteness()
    assert (
        scorer.score(
            {
                "event_count_before_recovery": 5,
                "event_count_after_recovery": 8,
                "recovered_count": 3,
            }
        )
        == 1.0
    )
    assert (
        scorer.score(
            {
                "event_count_before_recovery": 5,
                "event_count_after_recovery": 10,
                "recovered_count": 3,
            }
        )
        == 1.0
    )
    partial = scorer.score(
        {
            "event_count_before_recovery": 5,
            "event_count_after_recovery": 6,
            "recovered_count": 3,
        }
    )
    assert 0.0 < partial < 1.0
    assert scorer.score({}) == 0.5


# ── 6. CLI smoke: popola eval run ───────────────────────────────────────


def test_cli_eval_run_command_smoke(tmp_path: Path) -> None:
    """``popola eval run --output ... --events-dir ...`` writes a parseable TOML."""
    events = tmp_path / "events"
    events.mkdir()
    output = tmp_path / "report.toml"

    runner = CliRunner()
    r = runner.invoke(
        popola_root_app,
        [
            "eval",
            "run",
            "--output",
            str(output),
            "--events-dir",
            str(events),
            "--json",
        ],
    )
    assert r.exit_code == 0, f"exit={r.exit_code} stdout={r.stdout!r} exc={r.exception!r}"
    assert output.exists(), "output TOML was not written"

    parsed = tomllib.loads(output.read_text(encoding="utf-8"))
    assert "composite" in parsed
    assert isinstance(parsed["dimensions"], dict)
    assert len(parsed["dimensions"]) == 8

    json_lines = [line for line in r.stdout.splitlines() if line.strip().startswith("{")]
    assert json_lines, f"no JSON line found in stdout: {r.stdout!r}"
    payload = json.loads(json_lines[-1])
    assert payload["composite"] == pytest.approx(parsed["composite"], rel=1e-6)
    assert set(payload["dimensions"]) == set(parsed["dimensions"])


# ── 7. CLI eval show command ────────────────────────────────────────────


def test_cli_eval_show_command() -> None:
    """``popola eval show`` lists 8 dimensions with their nines.toml weights."""
    runner = CliRunner()
    r = runner.invoke(popola_root_app, ["eval", "show"])
    assert r.exit_code == 0, f"exit={r.exit_code} stdout={r.stdout!r}"
    for dim in DIMENSIONS:
        assert dim.name in r.stdout, f"dimension {dim.name!r} missing from output"


# ── 8. collect_evidence walks event logs correctly ──────────────────────


def test_collect_evidence_counts_event_types(tmp_path: Path) -> None:
    """``collect_evidence`` reads NDJSON files + counts types accurately."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    sample = events_dir / "task-aaaaaaaa.jsonl"
    lines = [
        '{"specversion":"1.0","id":"e1","source":"popola/x","type":"task.dispatched","time":"2026-05-04T00:00:00.000Z","data":{}}',
        '{"specversion":"1.0","id":"e2","source":"popola/x","type":"process.stdout","time":"2026-05-04T00:00:01.000Z","data":{"line":"hi"}}',
        '{"specversion":"1.0","id":"e3","source":"popola/x","type":"task.completed","time":"2026-05-04T00:00:02.000Z","data":{"exit_code":0}}',
        '{"specversion":"1.0","id":"e4","source":"popola/x","type":"popolad.recovered","time":"2026-05-04T00:00:03.000Z","data":{"recovered_count":1,"task_ids":["task-aaaaaaaa"]}}',
    ]
    sample.write_text("\n".join(lines) + "\n", encoding="utf-8")

    evidence = collect_evidence(events_dir)
    assert evidence["files"] == 1
    assert evidence["total_events"] == 4
    assert evidence["event_types"]["task.dispatched"] == 1
    assert evidence["event_types"]["popolad.recovered"] == 1
    assert evidence["recovered_count"] == 1
    assert evidence["event_count_after_recovery"] == 4
    assert evidence["event_count_before_recovery"] == 3
    assert evidence["attach_complete_count"] == 1
    assert evidence["attach_total_count"] == 1
