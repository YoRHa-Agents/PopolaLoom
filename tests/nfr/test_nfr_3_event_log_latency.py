"""Stage C C5 — NFR-3 baseline: EventLog.append() < 5ms mean for 1000 calls.

Per :doc:`spec` §6 NFR-3 (event-log append latency target ≤ 5 ms) and
v0.2.0-plan §4 Stage C C4 acceptance criterion #3.

Strategy
--------

- Sample 1000 :meth:`EventLog.append` invocations with
  :func:`time.perf_counter`; record per-call latency.
- Drop the first 100 samples as warmup (interpreter / fd cold-start
  asymmetry — first append also has to create the file).
- Assert ``mean < 5 ms`` (NFR-3 hard target) and ``p95 < 10 ms``
  (defensive: a tail spike isn't NFR-3 worthy alone, but a wide tail
  would suggest a regression in the buffered-write path).
- Marked ``@pytest.mark.slow`` because 1000 iterations + tmp file
  creation runs ~50-100 ms wall clock (over the 1 s nightly threshold
  is unlikely but the marker keeps fast-loop CI lean).

Bonus benchmark using ``pytest-benchmark`` if available — exposes the
sample distribution to the benchmark harness so trends across PRs are
visible (testing-matrix.md §9 "NFR-3 量化基线").
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path
from typing import Any

import pytest

from popolaloom.daemon import EventLog

_WARMUP_SAMPLES: int = 100
_TOTAL_SAMPLES: int = 1000
_NFR_3_TARGET_MS: float = 5.0
_P95_DEFENSIVE_TARGET_MS: float = 10.0


def _measure_append_latencies(log: EventLog, n_total: int) -> list[float]:
    """Run *n_total* appends, returning per-call latency in milliseconds."""
    latencies: list[float] = []
    for i in range(n_total):
        t0 = time.perf_counter()
        log.append("benchmark.tick", {"i": i, "payload": "x" * 64})
        latencies.append((time.perf_counter() - t0) * 1000.0)
    return latencies


# ── Manual timer assertion (always runs) ────────────────────────────────


@pytest.mark.slow
def test_nfr_3_event_log_append_under_5ms_average(tmp_path: Path) -> None:
    """1000 appends → mean latency < 5ms (NFR-3 hard requirement)."""
    log_path = tmp_path / "nfr3.jsonl"
    log = EventLog(log_path)
    try:
        latencies = _measure_append_latencies(log, _TOTAL_SAMPLES)
    finally:
        log.close()

    samples = latencies[_WARMUP_SAMPLES:]
    assert len(samples) == _TOTAL_SAMPLES - _WARMUP_SAMPLES

    mean_ms = statistics.fmean(samples)
    p95_ms = statistics.quantiles(samples, n=20, method="exclusive")[18]
    p99_ms = statistics.quantiles(samples, n=100, method="exclusive")[98]

    print(
        f"\nNFR-3 latency report (n={len(samples)}):"
        f" mean={mean_ms:.3f}ms"
        f" median={statistics.median(samples):.3f}ms"
        f" p95={p95_ms:.3f}ms"
        f" p99={p99_ms:.3f}ms"
        f" target_mean<{_NFR_3_TARGET_MS}ms"
    )

    assert mean_ms < _NFR_3_TARGET_MS, (
        f"NFR-3 violated: mean append latency {mean_ms:.3f}ms exceeds "
        f"target {_NFR_3_TARGET_MS}ms; samples histogram: "
        f"min={min(samples):.3f}ms, p50={statistics.median(samples):.3f}ms, "
        f"p95={p95_ms:.3f}ms, max={max(samples):.3f}ms"
    )
    assert p95_ms < _P95_DEFENSIVE_TARGET_MS, (
        f"NFR-3 defensive p95 violated: {p95_ms:.3f}ms exceeds "
        f"{_P95_DEFENSIVE_TARGET_MS}ms — possible regression in the "
        f"buffered-write path even though mean still under target"
    )


# ── pytest-benchmark integration (optional) ─────────────────────────────


@pytest.mark.slow
def test_nfr_3_event_log_append_pytest_benchmark(
    tmp_path: Path,
    benchmark: Any,
) -> None:
    """Same NFR-3 check piped through pytest-benchmark for trend tracking.

    Skipped automatically when ``pytest-benchmark`` is not installed.
    The benchmark fixture runs ``EventLog.append`` many times under its
    own statistical regime (calibration_precision, multiple rounds) and
    publishes the results to ``--benchmark-json`` when CI requests it.
    """
    log_path = tmp_path / "nfr3_bench.jsonl"
    log = EventLog(log_path)
    counter = {"i": 0}

    def _do_append() -> None:
        counter["i"] += 1
        log.append("benchmark.tick", {"i": counter["i"], "payload": "x" * 64})

    try:
        benchmark(_do_append)
    finally:
        log.close()

    stats = benchmark.stats.stats
    mean_ms = stats.mean * 1000.0
    assert mean_ms < _NFR_3_TARGET_MS, (
        f"pytest-benchmark mean append latency {mean_ms:.3f}ms exceeds "
        f"NFR-3 target {_NFR_3_TARGET_MS}ms"
    )
