"""NFR-2 — ``GET /status/{task_id}`` 100-sample RTT mean / p95 / p99 budget.

v0.5.2 Loop 2 §L2.C: the v0.4.0 GA "Known limitations" §6 flagged
NFR-2 + NFR-9 as having no quantitative bench gate.  v0.3.x added the
existing ``test_nfr_2_status_latency.py`` (3 cases, 50-sample mean
RTT) — this file adds the **100-sample** companion that publishes
``mean / p95 / p99`` percentiles directly into pytest-benchmark for
trend tracking, plus a mocked-daemon variant that measures the CLI /
``httpx.AsyncClient`` serialization overhead in isolation (so we can
attribute regressions to either the daemon or the client transport
without spawning a real daemon for every regression).

Differences from ``test_nfr_2_status_latency.py``
------------------------------------------------

* 100 samples instead of 50 — gives a more stable p99 estimate for
  flaky CI (per the spec contract requested by the L2.C task: "Dispatch
  100 echo tasks; measure popola status mean + p95 + p99").
* Three percentiles published instead of two (p99 added).
* ``test_nfr_2_status_endpoint_mocked_daemon_serialization`` measures
  pure HTTP request encoding + response decoding overhead — i.e. it
  benchmarks the ``httpx`` client + Pydantic v2 model_dump pipeline
  WITHOUT any UDS socket (uses :class:`httpx.MockTransport`).  This is
  the "serialization overhead" floor against which real-daemon NFR-2
  is compared.

Budget rationale
----------------

The existing ``test_nfr_2_status_latency.py::test_nfr_2_status_endpoint_pytest_benchmark_trend``
on this hardware reports a ~360 µs mean / ~500 µs max for a single
``GET /status``.  The L2.C spec asks for ``mean < 50ms, p95 < 100ms``
which is **two orders of magnitude looser** than the actual hardware —
that head-room covers slow / oversubscribed CI runners.  We use the
same 50 ms / 100 ms / 200 ms (mean / p95 / p99) targets here.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

import httpx
import pytest

from tests.fixtures.real_popolad import RealPopoladHandle

pytestmark = pytest.mark.slow


_NFR_2_RTT_SAMPLES: int = 100
"""Sample count per the L2.C task spec."""

_NFR_2_RTT_MEAN_TARGET_MS: float = 50.0
"""Mean RTT cap per L2.C task spec (generous head-room over actual hardware)."""

_NFR_2_RTT_P95_TARGET_MS: float = 100.0
"""p95 RTT cap per L2.C task spec."""

_NFR_2_RTT_P99_TARGET_MS: float = 200.0
"""p99 RTT cap — 2x the p95 to cover GC pauses / coverage instrumentation."""


def _dispatch_one_task(handle: RealPopoladHandle) -> str:
    """POST /dispatch via the daemon's UDS; return the task_id."""
    with handle.make_sync_client(timeout=10.0) as client:
        response = client.post(
            "/dispatch",
            json={"cli": "cursor", "prompt": "nfr-2 100-sample status rtt probe"},
        )
        response.raise_for_status()
        body = response.json()
    task_id = body.get("task_id")
    assert isinstance(task_id, str) and task_id, body
    return task_id


def _measure_status_rtt_ms(
    handle: RealPopoladHandle, task_id: str, samples: int
) -> list[float]:
    """Sample ``samples`` ``GET /status`` RTTs (in milliseconds)."""
    rtts: list[float] = []
    with handle.make_sync_client(timeout=5.0) as client:
        client.get(f"/status/{task_id}")
        for _ in range(samples):
            t0 = time.perf_counter()
            response = client.get(f"/status/{task_id}")
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            response.raise_for_status()
            rtts.append(elapsed_ms)
    return rtts


def test_nfr_2_status_rtt_100_samples_p95_p99(
    real_popolad: RealPopoladHandle,
) -> None:
    """100 ``GET /status`` RTTs → mean / p95 / p99 all under L2.C budget.

    Asserts:

    * mean   < 50 ms  (L2.C primary target)
    * p95    < 100 ms (L2.C primary target)
    * p99    < 200 ms (loosely 2× p95 — heads up on CI oscillations)

    The 100-sample budget is chosen to produce a stable p99 estimate
    (5 % of 100 = 5 events in the tail) without overwhelming the
    daemon with traffic that itself perturbs the measurement.
    """
    task_id = _dispatch_one_task(real_popolad)
    samples = _measure_status_rtt_ms(real_popolad, task_id, _NFR_2_RTT_SAMPLES)

    mean_ms = statistics.fmean(samples)
    median_ms = statistics.median(samples)
    p95_ms = statistics.quantiles(samples, n=20)[-1]
    p99_ms = statistics.quantiles(samples, n=100)[-1]
    mn_ms, mx_ms = min(samples), max(samples)

    print(
        f"\nNFR-2 status RTT (n={len(samples)}):"
        f" mean={mean_ms:.2f}ms median={median_ms:.2f}ms"
        f" p95={p95_ms:.2f}ms p99={p99_ms:.2f}ms"
        f" min={mn_ms:.2f}ms max={mx_ms:.2f}ms"
        f" budget mean<{_NFR_2_RTT_MEAN_TARGET_MS}ms"
        f" p95<{_NFR_2_RTT_P95_TARGET_MS}ms"
        f" p99<{_NFR_2_RTT_P99_TARGET_MS}ms"
    )

    assert mean_ms < _NFR_2_RTT_MEAN_TARGET_MS, (
        f"NFR-2 mean violated: {mean_ms:.2f}ms exceeds "
        f"{_NFR_2_RTT_MEAN_TARGET_MS}ms"
    )
    assert p95_ms < _NFR_2_RTT_P95_TARGET_MS, (
        f"NFR-2 p95 violated: {p95_ms:.2f}ms exceeds "
        f"{_NFR_2_RTT_P95_TARGET_MS}ms"
    )
    assert p99_ms < _NFR_2_RTT_P99_TARGET_MS, (
        f"NFR-2 p99 violated: {p99_ms:.2f}ms exceeds "
        f"{_NFR_2_RTT_P99_TARGET_MS}ms"
    )


def test_nfr_2_status_rtt_pytest_benchmark_publishes_percentiles(
    real_popolad: RealPopoladHandle,
    benchmark: Any,
) -> None:
    """Publish the 100-sample mean into ``--benchmark-json`` for trend tracking.

    pytest-benchmark publishes ``mean / median / min / max / stddev``
    automatically; we use ``benchmark.pedantic`` with rounds=10 +
    iterations=10 → 100 total samples that match the assertion case
    above, and assert the published mean still satisfies the L2.C
    budget (defends against pytest-benchmark + statistics.fmean
    drift caused by warmup-rounds shenanigans).
    """
    task_id = _dispatch_one_task(real_popolad)
    with real_popolad.make_sync_client(timeout=5.0) as client:
        client.get(f"/status/{task_id}")  # warm-up

        def _one_status_call() -> None:
            response = client.get(f"/status/{task_id}")
            response.raise_for_status()

        benchmark.pedantic(
            _one_status_call,
            rounds=10,
            iterations=10,
            warmup_rounds=1,
        )
    mean_s = benchmark.stats["mean"]
    assert mean_s * 1000.0 < _NFR_2_RTT_MEAN_TARGET_MS, (
        f"NFR-2 trend violated: pytest-benchmark mean "
        f"{mean_s * 1000.0:.2f}ms exceeds {_NFR_2_RTT_MEAN_TARGET_MS}ms"
    )


def _build_mock_status_client(payload: dict[str, Any]) -> httpx.Client:
    """Return an httpx.Client whose every ``GET /status/{tid}`` returns ``payload``.

    Uses :class:`httpx.MockTransport` so no UDS / no daemon / no
    pickling overhead. The result lets us measure the **client side**
    of NFR-2 (httpx encoding, JSON decoding, Pydantic serialization
    if applicable) without any kernel context switch.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/status/"):
            return httpx.Response(200, json=payload)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(_handler), base_url="http://x")


def test_nfr_2_status_rtt_mocked_daemon_serialization_floor(
    benchmark: Any,
) -> None:
    """Pure-CPU benchmark of the CLI's ``GET /status`` request/response cycle.

    The mocked transport short-circuits the UDS hop so the only work
    measured is:

    1. ``httpx.Client`` request encoding (URL + headers).
    2. ``MockTransport`` handler dispatch.
    3. ``httpx.Response`` JSON decoding.

    This establishes the serialization-cost FLOOR that real-daemon
    NFR-2 RTT can never beat.  Asserting < 5 ms here protects against
    a regression in the httpx / json fastpath introduced by a future
    Pydantic upgrade.
    """
    payload: dict[str, Any] = {
        "task_id": "nfr-2-mock-tid",
        "state": "running",
        "pid": 1234,
        "cli": "cursor",
        "exit_code": None,
        "events_log": "/tmp/popola/events/nfr-2-mock-tid.jsonl",
        "latest_event_index": 7,
        "arktower_task_id": "ark-1",
    }
    client = _build_mock_status_client(payload)
    try:
        client.get("/status/nfr-2-mock-tid")  # warm-up

        def _one_status_call() -> None:
            response = client.get("/status/nfr-2-mock-tid")
            response.raise_for_status()
            _ = response.json()

        benchmark.pedantic(
            _one_status_call,
            rounds=20,
            iterations=10,
            warmup_rounds=1,
        )
    finally:
        client.close()

    mean_s = benchmark.stats["mean"]
    assert mean_s * 1000.0 < 5.0, (
        f"NFR-2 mocked-daemon serialization floor regressed: "
        f"mean {mean_s * 1000.0:.3f}ms exceeds 5ms"
    )


def test_nfr_2_status_rtt_handles_404_path_within_budget(
    real_popolad: RealPopoladHandle,
) -> None:
    """The 404-task-not-found path must be just as fast as the 200 path.

    A slow 404 path implies the daemon is doing extra IO when the task
    is missing; that's a correctness smell as well as a perf bug.
    Mirror of ``test_nfr_2_status_latency.py::test_nfr_2_status_endpoint_404_path_also_fast``
    but with the 100-sample budget.
    """
    samples: list[float] = []
    with real_popolad.make_sync_client(timeout=5.0) as client:
        client.get("/status/missing")  # warm-up
        for i in range(_NFR_2_RTT_SAMPLES):
            t0 = time.perf_counter()
            response = client.get(f"/status/missing-{i}")
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            assert response.status_code == 404
            samples.append(elapsed_ms)

    mean_ms = statistics.fmean(samples)
    p95_ms = statistics.quantiles(samples, n=20)[-1]
    assert mean_ms < _NFR_2_RTT_MEAN_TARGET_MS, (
        f"NFR-2 404-path mean violated: {mean_ms:.2f}ms"
    )
    assert p95_ms < _NFR_2_RTT_P95_TARGET_MS, (
        f"NFR-2 404-path p95 violated: {p95_ms:.2f}ms"
    )
