"""single_threaded_writes — grep src for threading.Lock usage (v0.3.0 F1.6).

Real measurement (v0.3.0 upgrade from v0.2.0 mvp):

The v0.2.0 mvp used :func:`code-object introspection <co_names>` to
detect the existence of locks attribute names.  v0.3.0 walks the actual
``src/popolaloom`` source tree with :func:`grep`-equivalent
``rg -F`` semantics and counts ``threading.Lock`` allocations in the
3 "single-threaded write" critical sections:

- ``daemon/event_log.py``  — per-task NDJSON event log writer
- ``daemon/state.py``       — :class:`StateStore` shared task handles
- ``daemon/server.py``      — :class:`Popolad` ``_event_logs`` dict

Score grid (per task spec F1.6):

- ``1.0`` — all 3 modules import + use ``threading.Lock``
- ``0.66`` — 2 of 3 present
- ``0.33`` — 1 of 3 present
- ``0.0`` — none present (regression: race conditions possible)

Evidence override: ``locks_present`` set (v0.2.0 form) is honored when
supplied by tests so they don't need to ship a real source tree.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REQUIRED_FILES: tuple[str, ...] = (
    "daemon/event_log.py",
    "daemon/state.py",
    "daemon/server.py",
)
"""Three modules that MUST contain ``threading.Lock()`` allocations."""


def _count_locks_in_file(file_path: Path) -> int:
    """Return # of ``threading.Lock`` mentions; 0 if file unreadable.

    We check for the literal substring ``threading.Lock`` (the most
    common form) — if a future refactor uses ``from threading import
    Lock`` directly we'll need to extend this match.  The current
    daemon source uses the qualified form consistently.
    """
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        logger.debug(
            "single_threaded_writes: could not read %s", file_path, exc_info=True
        )
        return 0
    return text.count("threading.Lock")


class SingleThreadedWrites:
    """``threading.Lock`` present in event_log + state_store + server modules.

    v0.3.0 F1.6 real measurement: walks src/popolaloom and counts
    ``threading.Lock`` mentions in the 3 critical-section modules.
    """

    name = "single_threaded_writes"

    def score(self, evidence: dict[str, Any]) -> float:
        """``1.0`` when all 3 required modules contain a ``threading.Lock``.

        Resolution order:

        1. Evidence-supplied ``locks_present`` set (v0.2.0 form) → use
           the legacy graded scoring (matches v0.2.0 ``test_evaluation``
           tests verbatim so they keep passing).
        2. Otherwise walk the live ``src/popolaloom`` tree.
        """
        locks_evidence = evidence.get("locks_present")
        if locks_evidence is not None:
            try:
                present = set(locks_evidence)
            except TypeError:
                return 0.5
            required = {"_event_logs_lock", "state_store_lock", "event_log_lock"}
            missing = required - present
            if not missing:
                return 1.0
            if len(missing) == 1:
                return 0.66
            if len(missing) == 2:
                return 0.33
            return 0.0

        try:
            import popolaloom

            popola_root = Path(popolaloom.__file__).resolve().parent
        except (ImportError, AttributeError, OSError):
            logger.debug(
                "single_threaded_writes: cannot locate popolaloom package",
                exc_info=True,
            )
            return 0.0

        present_count = sum(
            1 for relpath in _REQUIRED_FILES if _count_locks_in_file(popola_root / relpath) > 0
        )
        return present_count / len(_REQUIRED_FILES)
