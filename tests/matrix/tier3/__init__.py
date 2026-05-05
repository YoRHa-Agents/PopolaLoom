"""Tier 3 (Hard, cross-process) tests — testing-matrix.md §1.3.

All cases under this package require a real ``python -m
popolaloom.daemon`` subprocess and are gated by ``@pytest.mark.slow``
(usually applied via ``pytestmark`` at module level).

v0.2.2 introduces 4 modules:

* :mod:`test_real_daemon_lifecycle` — boot, SIGTERM, SIGKILL, double-start.
* :mod:`test_cross_process_dispatch` — separate-interpreter consistency.
* :mod:`test_s1_crash_recovery_tier3` — richer S1 SIGKILL + OOM patterns.
* :mod:`test_attach_stream_sse` — SSE streaming + mid-stream disconnect.
"""

from __future__ import annotations
