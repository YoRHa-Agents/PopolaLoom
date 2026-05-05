"""dispatch_isolation — daemon vs CLI subprocess PGID isolation (v0.3.0 F1.1).

Real measurement (v0.3.0 upgrade from v0.2.0 mvp):

- When evidence supplies ``daemon_pid`` + ``cli_pid``, look up
  ``os.getpgid(pid)`` for both **at scoring time** to confirm the live
  process group ids actually differ.  This catches the regression where
  the supervisor forgot to call ``setsid``/``start_new_session=True``
  even though the in-memory bookkeeping looks right.
- Fall back to evidence-supplied ``daemon_pgid`` / ``cli_pgid`` when
  the PIDs aren't available (e.g. tests that fabricate PGIDs directly).
- Return :data:`PLACEHOLDER_SCORE` (``0.5``) when neither a live PGID
  lookup nor pre-computed PGIDs are available — preserves the v0.2.0
  "insufficient evidence" sentinel so empty events_dir runs don't
  artificially deflate the composite.

Score grid (per task spec F1.1):

- ``1.0`` — daemon PGID ≠ CLI PGID (process groups isolated)
- ``0.0`` — same PGID (regression: SIGTERM at daemon would propagate)
- ``0.5`` — no PIDs / PGIDs in evidence
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

PLACEHOLDER_SCORE: float = 0.5
"""Neutral score when evidence is insufficient (v0.2.0 mvp sentinel)."""


def _safe_getpgid(pid: int | None) -> int | None:
    """Return ``os.getpgid(pid)`` or ``None`` if the PID is gone/invalid.

    Using ``os.getpgid`` deliberately (not ``os.getpgrp`` which is
    limited to the current process) so we can introspect daemon /
    subprocess PGIDs from any caller.  ProcessLookupError → ``None``
    (No Silent Failures: log at debug, caller decides what to do).
    """
    if pid is None:
        return None
    try:
        return os.getpgid(int(pid))
    except (ProcessLookupError, PermissionError, OSError):
        logger.debug("dispatch_isolation: getpgid(%s) failed", pid, exc_info=True)
        return None
    except (TypeError, ValueError):
        logger.debug("dispatch_isolation: invalid pid %r", pid)
        return None


class DispatchIsolation:
    """popolad daemon vs CLI subprocess process / PGID isolation.

    v0.3.0 F1.1 real measurement: looks up live PGIDs via
    :func:`os.getpgid` when evidence provides PIDs, with PGID-only
    fallback for unit tests that don't have real processes.
    """

    name = "dispatch_isolation"

    def score(self, evidence: dict[str, Any]) -> float:
        """``1.0`` iff daemon and CLI run in distinct OS process groups.

        Resolution order:

        1. Evidence supplies ``daemon_pid`` AND ``cli_pid`` → live
           ``os.getpgid`` lookup (real measurement).  When both lookups
           succeed, score on PGID equality.
        2. Otherwise fall back to evidence-supplied ``daemon_pgid`` /
           ``cli_pgid`` (test path; preserves v0.2.0 contract).
        3. No PIDs and no PGIDs → :data:`PLACEHOLDER_SCORE`.
        """
        daemon_pid = evidence.get("daemon_pid")
        cli_pid = evidence.get("cli_pid")

        if daemon_pid is not None and cli_pid is not None:
            daemon_live_pgid = _safe_getpgid(daemon_pid)
            cli_live_pgid = _safe_getpgid(cli_pid)
            if daemon_live_pgid is not None and cli_live_pgid is not None:
                return 1.0 if daemon_live_pgid != cli_live_pgid else 0.0

        daemon_pgid = evidence.get("daemon_pgid")
        cli_pgid = evidence.get("cli_pgid")
        if daemon_pgid is not None and cli_pgid is not None:
            return 1.0 if daemon_pgid != cli_pgid else 0.0

        if daemon_pid is not None and cli_pid is not None:
            return 1.0 if daemon_pid != cli_pid else 0.0

        return PLACEHOLDER_SCORE
