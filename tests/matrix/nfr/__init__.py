"""NFR-1 / NFR-3 / NFR-5 / NFR-8 quantitative benchmarks (Tier 3).

Per testing-matrix.md §9 + spec §6 NFR-1..12.  Each NFR has its own
file with at least one ``@pytest.mark.benchmark`` (or a sampled-mean
manual benchmark when pytest-benchmark would over-spawn daemons).

Files:

* :mod:`test_nfr_1_startup_latency` — daemon boot wallclock < 2 s.
* :mod:`test_nfr_3_event_log_latency_v2` — NDJSON append < 5 ms mean.
* :mod:`test_nfr_5_cross_terminal_survival` — daemon survives parent
  shell SIGHUP / SIGTERM.
* :mod:`test_nfr_8_recovery_rate` — ≥ 95% rehydrate success rate over
  N trials (N=5 for CI throughput; spec target N=20).
"""

from __future__ import annotations
