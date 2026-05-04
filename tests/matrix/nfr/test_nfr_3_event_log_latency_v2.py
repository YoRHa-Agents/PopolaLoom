"""NFR-3 v2 — pytest-benchmark version of the EventLog append micro-benchmark.

Per testing-matrix.md §9.2 (canonical example) — the v0.2.0 baseline
:file:`tests/nfr/test_nfr_3_event_log_latency.py` already implements
the manual-sample variant; this v2 file adds the pytest-benchmark
``benchmark.pedantic`` form so trends across PRs are tracked in
``--benchmark-json`` output.

The two files are intentionally complementary; we keep both because:

* The manual-sample version asserts both ``mean`` and ``p95`` (catches
  long-tail regressions in the buffered-write path).
* The pytest-benchmark version drives the benchmark harness and
  publishes JSON for trend tooling (matches the spec's recommended
  ``benchmark.pedantic`` form with ``rounds=10`` × ``iterations=100``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from popolaloom.daemon.event_log import EventLog

pytestmark = pytest.mark.slow

_NFR_3_TARGET_S: float = 0.005
"""5 ms hard target per spec §6 NFR-3 + testing-matrix.md §9."""


def test_nfr_3_pytest_benchmark_pedantic_1000_iterations(
    tmp_path: Path,
    benchmark: Any,
) -> None:
    """``benchmark.pedantic`` 10 rounds × 100 iter = 1000 appends < 5 ms mean."""
    log_path = tmp_path / "events" / "T-bench-v2.jsonl"
    log = EventLog(log_path, fsync_interval_s=0.0)
    counter = {"i": 0}

    def append_one() -> None:
        counter["i"] += 1
        log.append(
            "task.heartbeat",
            {"task_id": "T-bench-v2", "i": counter["i"], "ts": "2026-05-04T00:00:00Z"},
        )

    try:
        benchmark.pedantic(
            append_one,
            rounds=10,
            iterations=100,
            warmup_rounds=2,
        )
    finally:
        log.close()

    mean_s = benchmark.stats.stats.mean
    assert mean_s < _NFR_3_TARGET_S, (
        f"NFR-3 violated: mean append {mean_s * 1000:.3f}ms exceeds "
        f"target {_NFR_3_TARGET_S * 1000}ms"
    )
