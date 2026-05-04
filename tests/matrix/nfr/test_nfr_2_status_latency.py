"""NFR-2 — ``GET /status/{task_id}`` round-trip latency ≤ 200 ms.

Per spec §6 NFR-2 ("status 查询 RTT ≤ 200ms (UDS 本地)") and the
v0.3.0-plan §6 risk register entry "NFR-2 + NFR-9 had no quantitative
gate in v0.2.2; v0.3.x rounds will add benchmarks before v0.4.0 GA".

Strategy
--------

1. Spawn a real popolad via the existing :class:`RealPopoladHandle`
   fixture (Tier 3 conformant — fresh ``$POPOLA_HOME``, fresh DB).
2. Dispatch a single short task via the cursor shim (already wired
   into ``real_popolad`` in ``tests/matrix/conftest.py``) so the
   status endpoint has a real task_id to look up.
3. Sample ``GET /status/{task_id}`` 50 times back-to-back over the
   already-warm UDS connection (the connection is reused; only the
   request RTT is measured).
4. Assert ``mean < 200 ms`` and ``p95 < 400 ms`` (= 2× the mean
   target — generous head-room for CI noise; the local-machine target
   from spec §6 is the mean).

Why not pytest-benchmark.pedantic
---------------------------------

We need the explicit p95 + p99 percentiles for this NFR; pytest-benchmark
only exposes ``mean / median / min / max`` per round in its default JSON
shape.  We compute percentiles by hand with :mod:`statistics` and still
publish a single mean sample to ``benchmark`` so trend tracking works
on CI.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

import pytest

from tests.fixtures.real_popolad import RealPopoladHandle

pytestmark = pytest.mark.slow


_NFR_2_MEAN_TARGET_MS: float = 200.0
"""Hard mean RTT cap per spec §6 NFR-2."""

_NFR_2_P95_TARGET_MS: float = 400.0
"""p95 cap = 2× mean target — generous head-room for CI noise."""

_NFR_2_SAMPLES: int = 50
"""Sample count.  50 hits give a stable p95 even on noisy CI runners
without drowning the test in IO; matches the testing-matrix.md §9 quick
benchmarks profile."""


def _dispatch_one_task(handle: RealPopoladHandle) -> str:
    """POST /dispatch via the daemon's UDS; return the task_id."""
    with handle.make_sync_client(timeout=10.0) as client:
        response = client.post(
            "/dispatch",
            json={"cli": "cursor", "prompt": "nfr-2 status latency probe"},
        )
        response.raise_for_status()
        body = response.json()
    task_id = body.get("task_id")
    assert isinstance(task_id, str) and task_id, body
    return task_id


def _measure_status_rtt_ms(handle: RealPopoladHandle, task_id: str) -> list[float]:
    """Sample :data:`_NFR_2_SAMPLES` ``GET /status`` RTTs (in milliseconds)."""
    samples: list[float] = []
    with handle.make_sync_client(timeout=5.0) as client:
        client.get(f"/status/{task_id}")
        for _ in range(_NFR_2_SAMPLES):
            t0 = time.perf_counter()
            response = client.get(f"/status/{task_id}")
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            response.raise_for_status()
            samples.append(elapsed_ms)
    return samples


def test_nfr_2_status_endpoint_mean_rtt_under_200ms(
    real_popolad: RealPopoladHandle,
) -> None:
    """50 ``GET /status`` RTTs → mean < 200 ms (NFR-2)."""
    task_id = _dispatch_one_task(real_popolad)
    samples = _measure_status_rtt_ms(real_popolad, task_id)

    mean_ms = statistics.fmean(samples)
    median_ms = statistics.median(samples)
    p95_ms = statistics.quantiles(samples, n=20)[-1] if len(samples) >= 20 else max(samples)
    p_max = max(samples)
    p_min = min(samples)

    print(
        f"\nNFR-2 status RTT (n={len(samples)}):"
        f" mean={mean_ms:.2f}ms median={median_ms:.2f}ms"
        f" p95={p95_ms:.2f}ms min={p_min:.2f}ms max={p_max:.2f}ms"
        f" target_mean<{_NFR_2_MEAN_TARGET_MS}ms target_p95<{_NFR_2_P95_TARGET_MS}ms"
    )

    assert mean_ms < _NFR_2_MEAN_TARGET_MS, (
        f"NFR-2 mean violated: {mean_ms:.2f}ms exceeds target "
        f"{_NFR_2_MEAN_TARGET_MS}ms; samples: {samples}"
    )
    assert p95_ms < _NFR_2_P95_TARGET_MS, (
        f"NFR-2 p95 violated: {p95_ms:.2f}ms exceeds target "
        f"{_NFR_2_P95_TARGET_MS}ms; samples: {samples}"
    )


def test_nfr_2_status_endpoint_pytest_benchmark_trend(
    real_popolad: RealPopoladHandle,
    benchmark: Any,
) -> None:
    """Single-iteration pytest-benchmark wrapper for trend tracking.

    pytest-benchmark publishes the timing into ``--benchmark-json``
    so PR-over-PR regressions can be inspected; the underlying RTT is
    the same as :func:`_measure_status_rtt_ms` but only one sample is
    fed through pedantic to keep the harness lightweight.
    """
    task_id = _dispatch_one_task(real_popolad)
    with real_popolad.make_sync_client(timeout=5.0) as client:
        client.get(f"/status/{task_id}")

        def _one_status_call() -> None:
            response = client.get(f"/status/{task_id}")
            response.raise_for_status()

        benchmark.pedantic(
            _one_status_call,
            rounds=10,
            iterations=5,
            warmup_rounds=1,
        )
    mean_s = benchmark.stats["mean"]
    assert mean_s * 1000.0 < _NFR_2_MEAN_TARGET_MS, (
        f"NFR-2 trend violated: pytest-benchmark mean "
        f"{mean_s * 1000.0:.2f}ms exceeds {_NFR_2_MEAN_TARGET_MS}ms"
    )


def test_nfr_2_status_endpoint_404_path_also_fast(
    real_popolad: RealPopoladHandle,
) -> None:
    """The 404 (task-not-found) path must be just as fast as the 200 path.

    A slow 404 path implies the daemon is doing extra IO when the task
    is missing; that's a correctness smell as well as a perf bug.
    """
    samples: list[float] = []
    with real_popolad.make_sync_client(timeout=5.0) as client:
        client.get("/status/missing-task-id")
        for i in range(20):
            t0 = time.perf_counter()
            response = client.get(f"/status/never-existed-{i}")
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            assert response.status_code == 404
            samples.append(elapsed_ms)

    mean_ms = statistics.fmean(samples)
    assert mean_ms < _NFR_2_MEAN_TARGET_MS, (
        f"NFR-2 404-path violated: mean {mean_ms:.2f}ms exceeds "
        f"{_NFR_2_MEAN_TARGET_MS}ms; samples: {samples}"
    )
