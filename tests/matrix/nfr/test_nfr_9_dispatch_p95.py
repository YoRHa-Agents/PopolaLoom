"""NFR-9 — ``POST /dispatch`` p95 latency ≤ 1 s (spec §6 NFR-9).

Per v0.3.0-plan §6 risk register: "NFR-9 dispatch p95 had no quantitative
gate in v0.2.2; v0.3.x must add a benchmark before v0.4.0 GA".

NFR-9 measures the wall-clock time from when a client posts to
``/dispatch`` until the daemon responds with the new ``task_id`` (i.e.
the synchronous portion: receive request → record task in ArkTower →
spawn subprocess → return). The downstream subprocess work continues
asynchronously and is *not* part of NFR-9.

Strategy
--------

1. Spawn a real popolad via the Tier 3 fixture (cursor shim wired in).
2. Dispatch ``N=20`` tasks back-to-back, recording each RTT.
3. Compute p95 from the sample list with :func:`statistics.quantiles`.
4. Assert ``p95 < 1.0 s`` (and report mean / median / min / max).
5. Cancel the spawned tasks at teardown so we don't leak shim sleepers.

Why p95 (and not mean)
----------------------

Mean is hidden behind GC pauses and ArkTower migrations on the first
call; the spec gives p95 as the user-visible "tail latency" target so
the long pole (Popen + ArkTower insert + UDS write) stays under 1 s
even with a noisy host.

v0.5.2 Loop 2 §L2.C extension
-----------------------------

The L2.C task asks for a 100-sample dispatch p95 measurement (vs the
existing 20).  We add ``test_nfr_9_dispatch_100_samples_mean_p95``
that runs 100 dispatches with mean / p95 budgets per the L2.C spec
(``mean < 100ms, p95 < 200ms`` — note these are the **CLI** budgets
the L2.C task documents; the existing 20-sample test uses the
**daemon** budget of 1 s p95 from spec §6).  We also add a mocked-
daemon benchmark that isolates the CLI / serialization overhead so
regressions in httpx / Pydantic are catchable without spawning a
real daemon.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

import httpx
import pytest

from tests.fixtures.real_popolad import RealPopoladHandle

pytestmark = pytest.mark.slow


_NFR_9_P95_TARGET_S: float = 1.0
"""Hard p95 cap per spec §6 NFR-9."""

_NFR_9_MEAN_TARGET_S: float = 0.5
"""Generous mean target = half the p95.  This is the looser bound; the
p95 is the canonical NFR-9 gate."""

_NFR_9_SAMPLES: int = 20
"""Per testing-matrix.md §9 quick benchmark profile.  20 samples give
a stable p95 inside the test container's memory budget without
overwhelming ArkTower with too many child task rows."""

_NFR_9_L2C_SAMPLES: int = 100
"""v0.5.2 Loop 2 §L2.C task spec — 100-sample budget for the CLI-side
benchmark (matches the NFR-2 100-sample budget).  Each sample writes
one ArkTower row, so this is roughly 5x the volume of the canonical
20-sample p95 test; we cancel each spawned task at the end of the run
so the daemon's child table doesn't blow up between cases."""

_NFR_9_L2C_MEAN_TARGET_MS: float = 100.0
"""L2.C CLI-side mean budget.  The existing 20-sample p95 test uses
0.5 s mean (the daemon-side budget per spec §6); the L2.C
extension uses the tighter 100 ms mean target documented in the
v0.5.2 task spec."""

_NFR_9_L2C_P95_TARGET_MS: float = 200.0
"""L2.C CLI-side p95 budget — 2x the L2.C mean target."""


def _dispatch_and_measure(client: Any, prompt: str) -> tuple[str, float]:
    """POST /dispatch; return (task_id, RTT seconds)."""
    t0 = time.perf_counter()
    response = client.post(
        "/dispatch",
        json={"cli": "cursor", "prompt": prompt},
    )
    rtt_s = time.perf_counter() - t0
    response.raise_for_status()
    body = response.json()
    task_id = body.get("task_id")
    assert isinstance(task_id, str) and task_id, body
    return task_id, rtt_s


def _cancel_quietly(client: Any, task_ids: list[str]) -> None:
    """Best-effort cancel — failures don't fail the test (already terminal)."""
    for tid in task_ids:
        try:
            client.post(f"/cancel/{tid}", timeout=2.0)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            continue


def test_nfr_9_dispatch_p95_under_1s(
    real_popolad: RealPopoladHandle,
) -> None:
    """20 dispatches → p95 < 1 s (NFR-9)."""
    samples: list[float] = []
    task_ids: list[str] = []
    with real_popolad.make_sync_client(timeout=10.0) as client:
        for i in range(_NFR_9_SAMPLES):
            tid, rtt = _dispatch_and_measure(client, f"nfr-9 dispatch probe {i}")
            samples.append(rtt)
            task_ids.append(tid)
        _cancel_quietly(client, task_ids)

    mean_s = statistics.fmean(samples)
    median_s = statistics.median(samples)
    p_min = min(samples)
    p_max = max(samples)
    p95_s = statistics.quantiles(samples, n=20)[-1] if len(samples) >= 20 else p_max

    print(
        f"\nNFR-9 dispatch RTT (n={len(samples)}):"
        f" mean={mean_s * 1000:.1f}ms median={median_s * 1000:.1f}ms"
        f" p95={p95_s * 1000:.1f}ms min={p_min * 1000:.1f}ms max={p_max * 1000:.1f}ms"
        f" target_p95<{_NFR_9_P95_TARGET_S * 1000:.0f}ms"
    )

    assert p95_s < _NFR_9_P95_TARGET_S, (
        f"NFR-9 p95 violated: {p95_s * 1000:.1f}ms exceeds target "
        f"{_NFR_9_P95_TARGET_S * 1000:.0f}ms; samples (ms): "
        f"{[round(s * 1000, 1) for s in samples]}"
    )
    assert mean_s < _NFR_9_MEAN_TARGET_S, (
        f"NFR-9 mean violated: {mean_s * 1000:.1f}ms exceeds target "
        f"{_NFR_9_MEAN_TARGET_S * 1000:.0f}ms; samples (ms): "
        f"{[round(s * 1000, 1) for s in samples]}"
    )


def test_nfr_9_dispatch_first_call_warms_arktower(
    real_popolad: RealPopoladHandle,
) -> None:
    """Sanity check: the **first** dispatch (cold ArkTower migrations)
    must still satisfy the p95 budget by itself.

    A common regression vector is the daemon delaying ArkTower migrations
    until first /dispatch — that turns the first call into a 5-10 s
    blocking operation.  We verify the daemon eagerly applies migrations
    at startup by asserting the **single first dispatch RTT** is < 1 s.
    """
    with real_popolad.make_sync_client(timeout=10.0) as client:
        tid, rtt = _dispatch_and_measure(client, "nfr-9 first-call cold path")
        client.post(f"/cancel/{tid}", timeout=2.0)

    assert rtt < _NFR_9_P95_TARGET_S, (
        f"NFR-9 first-call regression: cold-path RTT {rtt * 1000:.1f}ms "
        f"exceeds target {_NFR_9_P95_TARGET_S * 1000:.0f}ms — "
        f"ArkTower migrations may be deferred to first /dispatch"
    )


# ─────────────────────────────────────────────────────────────────────────
# v0.5.2 Loop 2 §L2.C — 100-sample CLI-side dispatch RTT + mocked floor
# ─────────────────────────────────────────────────────────────────────────


def test_nfr_9_dispatch_100_samples_mean_p95(
    real_popolad: RealPopoladHandle,
) -> None:
    """100 ``POST /dispatch`` RTTs → mean / p95 within L2.C budget.

    Asserts:

    * mean < 100 ms (L2.C CLI-side primary target)
    * p95  < 200 ms (L2.C CLI-side primary target)

    The 100-sample budget gives a stable p95 estimate.  Each iteration
    dispatches a fresh cursor-shim task; we cancel them all at the end
    so we don't leak shim sleepers.  Mirrors the NFR-2 100-sample
    contract added in ``test_nfr_2_status_rtt.py``.
    """
    samples_ms: list[float] = []
    task_ids: list[str] = []
    with real_popolad.make_sync_client(timeout=10.0) as client:
        for i in range(_NFR_9_L2C_SAMPLES):
            tid, rtt = _dispatch_and_measure(client, f"nfr-9 100-sample probe {i}")
            samples_ms.append(rtt * 1000.0)
            task_ids.append(tid)
        _cancel_quietly(client, task_ids)

    mean_ms = statistics.fmean(samples_ms)
    median_ms = statistics.median(samples_ms)
    p95_ms = statistics.quantiles(samples_ms, n=20)[-1]
    p99_ms = statistics.quantiles(samples_ms, n=100)[-1]
    mn_ms, mx_ms = min(samples_ms), max(samples_ms)

    print(
        f"\nNFR-9 dispatch RTT (n={len(samples_ms)}):"
        f" mean={mean_ms:.2f}ms median={median_ms:.2f}ms"
        f" p95={p95_ms:.2f}ms p99={p99_ms:.2f}ms"
        f" min={mn_ms:.2f}ms max={mx_ms:.2f}ms"
        f" budget mean<{_NFR_9_L2C_MEAN_TARGET_MS}ms"
        f" p95<{_NFR_9_L2C_P95_TARGET_MS}ms"
    )

    assert mean_ms < _NFR_9_L2C_MEAN_TARGET_MS, (
        f"NFR-9 (L2.C) mean violated: {mean_ms:.2f}ms exceeds "
        f"{_NFR_9_L2C_MEAN_TARGET_MS}ms"
    )
    assert p95_ms < _NFR_9_L2C_P95_TARGET_MS, (
        f"NFR-9 (L2.C) p95 violated: {p95_ms:.2f}ms exceeds "
        f"{_NFR_9_L2C_P95_TARGET_MS}ms"
    )


def _build_mock_dispatch_client() -> httpx.Client:
    """Return an httpx.Client whose every ``POST /dispatch`` echoes back a stub.

    Uses :class:`httpx.MockTransport` so no UDS / no daemon / no
    ArkTower row insertion. Letss us measure the **client side** of
    NFR-9 (httpx encoding, JSON decoding, body building) without any
    kernel context switch.
    """
    counter = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/dispatch":
            counter["n"] += 1
            return httpx.Response(
                200,
                json={
                    "task_id": f"mock-tid-{counter['n']:04d}",
                    "events_log": "/tmp/popola/events/mock.jsonl",
                    "cli": "cursor",
                },
            )
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(_handler), base_url="http://x")


def test_nfr_9_dispatch_mocked_daemon_serialization_floor(
    benchmark: Any,
) -> None:
    """Pure-CPU benchmark of the CLI's ``POST /dispatch`` request/response cycle.

    The mocked transport short-circuits the UDS hop so the only work
    measured is:

    1. JSON encoding of the dispatch body.
    2. ``httpx.Client.post`` request encoding.
    3. ``MockTransport`` handler dispatch.
    4. ``httpx.Response`` JSON decoding.

    This establishes the serialization-cost FLOOR that real-daemon
    NFR-9 RTT can never beat. Asserts < 5 ms; protects against a
    regression in the httpx / json fastpath introduced by a future
    Pydantic upgrade or transport refactor.
    """
    client = _build_mock_dispatch_client()
    try:
        client.post(
            "/dispatch",
            json={"cli": "cursor", "prompt": "warm-up"},
        )

        def _one_dispatch_call() -> None:
            response = client.post(
                "/dispatch",
                json={"cli": "cursor", "prompt": "nfr-9 mocked floor probe"},
            )
            response.raise_for_status()
            body = response.json()
            assert body.get("task_id"), body

        benchmark.pedantic(
            _one_dispatch_call,
            rounds=20,
            iterations=10,
            warmup_rounds=1,
        )
    finally:
        client.close()

    mean_s = benchmark.stats["mean"]
    assert mean_s * 1000.0 < 5.0, (
        f"NFR-9 mocked-daemon serialization floor regressed: "
        f"mean {mean_s * 1000.0:.3f}ms exceeds 5ms"
    )
