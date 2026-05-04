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
"""

from __future__ import annotations

import statistics
import time
from typing import Any

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
