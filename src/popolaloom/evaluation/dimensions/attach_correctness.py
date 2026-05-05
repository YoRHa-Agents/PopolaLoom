"""attach_correctness — file vs in-memory attach completeness (v0.3.0 F1.4).

Real measurement (v0.3.0 upgrade from v0.2.0 mvp):

The v0.2.0 mvp computed ``complete / total`` of attach session counts.
v0.3.0 instead **compares the on-disk NDJSON file line count** against
the in-memory ``EventLog.tail()`` count (the canonical source served
by the daemon's attach SSE endpoint).  Mismatch indicates either a
buffered-write that hasn't flushed or a read-side ordering bug — both
are real correctness regressions worth flagging.

Score grid (per task spec F1.4):

- ``1.0`` — file line count == in-memory tail count (perfect parity)
- ``0.0`` — counts differ (regression: SSE clients would see partial
  data vs forensic reads of the file)
- :data:`PLACEHOLDER_SCORE` — no event_log paths supplied

Evidence keys consumed (v0.3.0 form):

- ``attach_event_log_paths`` (list[Path|str]|None)  — preferred
- ``attach_tail_counts``     (list[int]|None)       — must be same length

The runner fills these from the daemon's ``self._event_logs`` dict
(per-task ``EventLog`` instances).  v0.2.0 evidence keys
(``attach_complete_count`` / ``attach_total_count``) are still
supported for backward compat.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PLACEHOLDER_SCORE: float = 0.5
"""Neutral score when no attach evidence is available."""


def _count_file_lines(path: Path) -> int | None:
    """Count non-empty lines in an NDJSON file; ``None`` on IOError.

    Reading line-by-line keeps memory bounded for large event logs.
    Empty lines are skipped (matches ``EventLog.tail`` semantics).
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        logger.debug("attach_correctness: could not read %s", path, exc_info=True)
        return None


class AttachCorrectness:
    """Cross-process attach completeness (file vs in-memory parity).

    v0.3.0 F1.4 real measurement: the SSE attach endpoint's truth source
    is the ``EventLog.tail()`` ring; the forensic truth source is the
    NDJSON file on disk.  When these diverge an attached client sees a
    different event sequence than ``cat events/<task>.jsonl`` would,
    which violates the "single source of truth" axiom.
    """

    name = "attach_correctness"

    def score(self, evidence: dict[str, Any]) -> float:
        """Score by file-vs-memory match across all observed task event logs."""
        log_paths = evidence.get("attach_event_log_paths")
        tail_counts = evidence.get("attach_tail_counts")

        if log_paths is not None and tail_counts is not None:
            if len(list(log_paths)) != len(list(tail_counts)):
                logger.warning(
                    "attach_correctness: attach_event_log_paths len != attach_tail_counts len"
                )
                return 0.0
            paths_list = [Path(p) for p in log_paths]
            tails_list = [int(t) for t in tail_counts]

            if not paths_list:
                return PLACEHOLDER_SCORE

            matches = 0
            checked = 0
            for path, tail_count in zip(paths_list, tails_list, strict=True):
                file_count = _count_file_lines(path)
                if file_count is None:
                    continue
                checked += 1
                if file_count == tail_count:
                    matches += 1

            if checked == 0:
                return PLACEHOLDER_SCORE
            return matches / checked

        complete = evidence.get("attach_complete_count")
        total = evidence.get("attach_total_count")
        if not total or complete is None:
            return PLACEHOLDER_SCORE
        try:
            ratio = float(complete) / float(total)
        except (TypeError, ValueError, ZeroDivisionError):
            return PLACEHOLDER_SCORE
        return max(0.0, min(1.0, ratio))
