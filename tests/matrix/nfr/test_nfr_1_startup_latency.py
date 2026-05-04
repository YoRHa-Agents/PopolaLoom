"""NFR-1 — popolad cold-start latency ≤ 2 s.

Per spec §6 NFR-1 ("启动 daemon 时间 ≤ 2 s 从 systemd-run 触发到 unix
socket 监听") and testing-matrix.md §9.1.

Strategy
--------

We can't use :func:`pytest_benchmark.benchmark.pedantic` directly
because each iteration *spawns a new daemon subprocess* (~250 MB
imports + uvicorn bind), so the standard benchmark warmup pattern
would multiply RSS too quickly inside the test container's memcg.

Instead we sample manually:

1. Run 5 cold-starts (per NFR-1 spec §9.1 ``rounds=5``).
2. Per iteration: spawn ``python -m popolaloom.daemon`` with a fresh
   ``$POPOLA_HOME``; record :func:`time.monotonic` from spawn until the
   UDS file accepts a connection.
3. SIGTERM the daemon between iterations to release the socket.
4. Report ``mean`` / ``p95`` / ``min`` / ``max`` and assert
   ``mean < 2.0 s``.

A second, lighter test does only 1 iteration but pipes through
``benchmark.pedantic`` so trend tracking via pytest-benchmark JSON
output still works on CI.
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.real_popolad import (
    _spawn_daemon_process,
    _terminate_daemon,
    _wait_for_socket,
    make_isolated_env,
)

pytestmark = pytest.mark.slow


_NFR_1_TARGET_S: float = 2.0
"""Hard latency target per spec §6 NFR-1."""


def _measure_one_cold_start(home: Path, log_path: Path) -> float:
    """Spawn a daemon under ``home``; return wallclock until UDS up (seconds)."""
    env = make_isolated_env(home)
    socket_path = home / "popolad.sock"
    t0 = time.monotonic()
    proc = _spawn_daemon_process(env, log_path)
    try:
        ok = _wait_for_socket(socket_path, _NFR_1_TARGET_S * 5.0)
        elapsed = time.monotonic() - t0
        if not ok:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
            pytest.fail(
                f"daemon did not bind UDS within {_NFR_1_TARGET_S * 5}s; log:\n{log_text}"
            )
        return elapsed
    finally:
        _terminate_daemon(proc)


def test_nfr_1_daemon_cold_start_under_2s_mean_over_5_iterations(
    tmp_path: Path,
) -> None:
    """5 cold-starts → mean wallclock < 2 s (NFR-1)."""
    samples: list[float] = []
    for i in range(5):
        home = tmp_path / f"home_{i}"
        log_path = tmp_path / f"popolad_{i}.log"
        samples.append(_measure_one_cold_start(home, log_path))

    mean_s = statistics.fmean(samples)
    median_s = statistics.median(samples)
    p_max = max(samples)
    p_min = min(samples)
    print(
        f"\nNFR-1 startup latency (n={len(samples)}):"
        f" mean={mean_s:.3f}s median={median_s:.3f}s"
        f" min={p_min:.3f}s max={p_max:.3f}s target<{_NFR_1_TARGET_S}s"
    )

    assert mean_s < _NFR_1_TARGET_S, (
        f"NFR-1 violated: mean cold-start {mean_s:.3f}s exceeds "
        f"target {_NFR_1_TARGET_S}s; samples: {samples}"
    )


def test_nfr_1_daemon_single_cold_start_pytest_benchmark(
    tmp_path: Path,
    benchmark: Any,
) -> None:
    """Single-iteration pytest-benchmark wrapper for trend tracking.

    pytest-benchmark publishes the timing into ``--benchmark-json``
    so PR-over-PR regressions can be inspected; the underlying
    spawn-and-wait is the same as the manual sampler above.

    We use ``rounds=1, iterations=1`` to avoid spawning multiple
    daemons in parallel inside the benchmark harness (each one is
    ~250 MB and the test container's memcg can't take many).  The
    inner closure creates a unique home dir per call so successive
    invocations don't trip the "socket already exists" warning.
    """
    counter = {"i": 0}

    def _spawn_and_measure() -> float:
        counter["i"] += 1
        idx = counter["i"]
        home = tmp_path / f"bench_home_{idx}"
        log_path = tmp_path / f"bench_popolad_{idx}.log"
        return _measure_one_cold_start(home, log_path)

    result = benchmark.pedantic(
        _spawn_and_measure,
        rounds=1,
        iterations=1,
        warmup_rounds=0,
    )
    assert result < _NFR_1_TARGET_S, (
        f"NFR-1 violated: single cold-start {result:.3f}s exceeds {_NFR_1_TARGET_S}s"
    )
