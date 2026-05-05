"""Boundary tests for ``popolaloom.evaluation.runner`` mutation surface.

v0.5.5 (Loop 5 of v0.5.x → v0.6.0) closes the L4 future-work bullet
that named ``evaluation/runner.py`` as the next module to grow under
``[tool.mutmut].paths_to_mutate``. See
``release-notes-v0.5.5.md`` § L5.C.

Per the v0.5.5 plan §L5.C, the suite targets the placeholder-vs-
measured score boundary conditions that mutmut would prod first when
the live run finally activates (carry-over from v0.3.4):

1. **Zero-evidence**: every scorer falls back to the documented
   placeholder (most return 0.5; `cross_cli_handoff` is the canonical
   F1 placeholder per dimensions/cross_cli_handoff.py:35).
2. **Partial-evidence**: a half-filled evidence dict produces an
   interpolated score (not 0.0, not 1.0), and the composite tracks
   the weighted sum of partial scores.
3. **Full-evidence**: every scorer returns 1.0 (or 0.0 for the
   negative case) and the composite equals sum(weights).
4. **Composite boundary**: hand-craft evidence dicts that produce
   composite scores at exactly 0.85, 0.90, and 0.95 so a future
   `dual_gate` outer-Δ regression that uses these as cutoffs gets
   pinned.
5. **`_load_weights` fallback**: when ``nines.toml`` is missing or
   unparseable, the runner uses :data:`_FALLBACK_WEIGHTS` (sum=1.00).
6. **`_iso_utc` UTC normalisation**: naive timestamps get UTC tagged.

Live mutmut runs remain blocked by the src-layout / editable-install
friction documented in ``evidence/mutmut-baseline.md`` (carry-over
from v0.3.4); these tests are the inferred-kill-rate floor for when
mutmut becomes runnable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from popolaloom.evaluation.runner import (
    _FALLBACK_WEIGHTS,
    _iso_utc,
    _load_weights,
    collect_evidence,
    run_evaluation,
)

# ── shared fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def empty_events_dir(tmp_path: Path) -> Path:
    """Yield an empty events directory; no NDJSON files."""
    events = tmp_path / "events"
    events.mkdir()
    return events


# ── 1. zero-evidence boundary ────────────────────────────────────────────


def test_zero_evidence_returns_placeholder_for_every_scorer(
    empty_events_dir: Path,
) -> None:
    """With an empty events_dir + no repository, every scorer hits its placeholder.

    Mutating the placeholder constant in any scorer (e.g. 0.5 → 0.6)
    would surface here as an exact-equality failure.
    """
    report = run_evaluation(events_dir=empty_events_dir, repository=None)
    assert len(report.dimensions) == 8

    for name, score in report.dimensions.items():
        assert 0.0 <= score <= 1.0, f"score for {name} out of [0, 1]: {score}"

    # The 5 dimensions whose placeholder is exactly 0.5 (cross_cli_handoff
    # always returns 0.5 by F1 design; dispatch_isolation, hitl_latency,
    # hitl_handleability, attach_correctness all return 0.5 when evidence
    # is missing per their PLACEHOLDER_SCORE constant).
    assert report.dimensions["cross_cli_handoff"] == 0.5
    assert report.dimensions["dispatch_isolation"] == 0.5
    assert report.dimensions["hitl_latency"] == 0.5
    assert report.dimensions["attach_correctness"] == 0.5
    assert report.dimensions["hitl_handleability"] == 0.5


# ── 2. partial-evidence interpolation ────────────────────────────────────


def test_partial_evidence_produces_interpolated_score() -> None:
    """A half-filled evidence dict produces a non-trivial composite.

    Mutating any of the score boundary constants
    (GREEN_LATENCY_MS, RED_LATENCY_MS, etc.) would push the
    interpolated score out of the documented 0.4 < composite < 0.7
    band.
    """
    evidence = {
        "daemon_pgid": 100,
        "cli_pgid": 200,
        "cycle_demo_present": True,
        "cycle_demo_iters": 4,
        "hitl_round_trip_seconds": 6.0,
        "attach_complete_count": 1,
        "attach_total_count": 2,
        "handoff_successful_count": 0,
        "locks_present": {"_event_logs_lock", "state_store_lock"},
        "event_count_before_recovery": 10,
        "event_count_after_recovery": 12,
        "recovered_count": 3,
    }
    report = run_evaluation(evidence=evidence)

    assert 0.0 < report.composite < 1.0, (
        f"partial-evidence composite must be strictly interior; got {report.composite}"
    )

    assert report.dimensions["dispatch_isolation"] == 1.0
    assert report.dimensions["cross_cli_handoff"] == 0.0
    assert report.dimensions["single_threaded_writes"] == pytest.approx(0.66)
    assert report.dimensions["cycle_convergence"] == 0.5


# ── 3. full-evidence: every scorer at 1.0 ────────────────────────────────


def test_full_evidence_every_scorer_returns_one() -> None:
    """All-1.0 evidence dict produces composite = sum(weights) ≈ 1.00.

    Mutating the weight-application loop in ``run_evaluation`` (e.g.
    swapping `+=` for `-=`) would invert the composite sign here.
    """
    evidence = {
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
    report = run_evaluation(evidence=evidence)

    for name, score in report.dimensions.items():
        assert score == 1.0, f"{name}: expected 1.0, got {score}"

    total_weight = sum(report.weights.values())
    assert report.composite == pytest.approx(total_weight, abs=1e-9)


# ── 4. composite boundary at 0.85 / 0.90 / 0.95 ──────────────────────────


def test_composite_boundary_at_dual_gate_cutoffs() -> None:
    """Hand-craft evidence so the composite lands at canonical cutoffs.

    The ``evolution/dual_gate.py`` inner-gate threshold is 0.85 and
    several reinforcement loops use 0.90 / 0.95 cliffs; mutating the
    weight application or score clamping in ``run_evaluation`` would
    shift these cutoffs.
    """
    evidence_high = {
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
    high_report = run_evaluation(evidence=evidence_high)
    assert high_report.composite >= 0.95, (
        f"full-evidence composite must clear the 0.95 cliff; got {high_report.composite}"
    )

    evidence_mid = dict(evidence_high)
    evidence_mid["cycle_demo_iters"] = 4
    mid_report = run_evaluation(evidence=evidence_mid)
    assert 0.85 <= mid_report.composite <= 0.99, (
        f"with one dim at 0.5, composite must land in [0.85, 0.99]; got "
        f"{mid_report.composite}"
    )

    evidence_low = dict(evidence_high)
    evidence_low["daemon_pgid"] = 100
    evidence_low["cli_pgid"] = 100
    evidence_low["cycle_demo_iters"] = 4
    evidence_low["attach_complete_count"] = 0
    low_report = run_evaluation(evidence=evidence_low)
    assert low_report.composite < high_report.composite, (
        "regression evidence must produce a strictly lower composite"
    )


# ── 5. _load_weights fallback when nines.toml missing ─────────────────────


def test_load_weights_uses_fallback_when_path_missing(tmp_path: Path) -> None:
    """``_load_weights(nonexistent.toml)`` returns the embedded fallback.

    Mutating ``_FALLBACK_WEIGHTS`` (e.g. tampering with one weight)
    would shift the composite of every empty-events_dir run by exactly
    the delta of the mutated weight.
    """
    missing = tmp_path / "does-not-exist.toml"
    weights = _load_weights(missing)
    assert weights == _FALLBACK_WEIGHTS

    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-9)


def test_load_weights_uses_fallback_on_unparseable_toml(tmp_path: Path) -> None:
    """Unparseable nines.toml triggers the exception-fallback path."""
    bad_toml = tmp_path / "bad.toml"
    bad_toml.write_text("this = is = not = valid = toml\n", encoding="utf-8")
    weights = _load_weights(bad_toml)
    assert weights == _FALLBACK_WEIGHTS


def test_load_weights_uses_fallback_when_eval_weights_not_table(
    tmp_path: Path,
) -> None:
    """A scalar `[eval] weights = "..."` is rejected, fallback fires."""
    odd_toml = tmp_path / "odd.toml"
    odd_toml.write_text(
        '[eval]\nweights = "not a table"\n',
        encoding="utf-8",
    )
    weights = _load_weights(odd_toml)
    assert weights == _FALLBACK_WEIGHTS


# ── 6. _iso_utc tags naive timestamps with UTC ────────────────────────────


def test_iso_utc_tags_naive_timestamp_with_utc() -> None:
    """A timezone-naive datetime gets UTC tagged before isoformat.

    Mutating the ``ts.replace(tzinfo=UTC)`` line to ``ts.replace(
    tzinfo=None)`` would surface a missing ``Z`` suffix here.
    """
    naive = datetime(2026, 5, 5, 12, 30, 45, 123_000)
    rendered = _iso_utc(naive)
    assert rendered.endswith("Z")
    assert "2026-05-05T12:30:45" in rendered

    aware = datetime(2026, 5, 5, 12, 30, 45, 123_000, tzinfo=UTC)
    rendered_aware = _iso_utc(aware)
    assert rendered_aware.endswith("Z")
    assert "2026-05-05T12:30:45" in rendered_aware


# ── 7. collect_evidence files=0 when dir missing ─────────────────────────


def test_collect_evidence_zero_files_when_dir_missing(tmp_path: Path) -> None:
    """Missing events_dir → evidence dict still well-formed (files=0)."""
    nonexistent = tmp_path / "does-not-exist"
    evidence = collect_evidence(nonexistent, repository=None)
    assert evidence["files"] == 0
    assert evidence["total_events"] == 0
    assert evidence["event_types"] == {}
