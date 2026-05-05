"""hitl_latency — HITL round-trip wall time (v0.3.0 F1.3).

Real measurement (v0.3.0 upgrade from v0.2.0 mvp):

The v0.2.0 mvp read ``hitl_round_trip_seconds`` (a single scalar) and
bucketed it into 1.0/0.7/0.5/0.3/0.0 thresholds.  v0.3.0 prefers a
``hitl_round_trips`` *list* of millisecond-granularity round-trip
times and computes the **median**, then linearly scales:

- ``≤ 1000ms``    → ``1.0``  (sub-second; excellent)
- ``1000-10000ms`` → linearly scale (``score = 1 - (median-1000)/9000``)
- ``≥ 10000ms``   → ``0.0``

Evidence keys consumed:

- ``hitl_round_trips`` (list[float]|None)  — preferred; ms each
- ``hitl_round_trip_seconds`` (float|None) — v0.2.0 fallback; seconds

Returns :data:`PLACEHOLDER_SCORE` (``0.5``) when neither is supplied.

The new list form lets v0.3.0 collect statistics across multiple HITL
round-trips in a single popolad session (e.g. the F4 supervise +
cross-channel sync test cases).  Median (not mean) keeps the score
robust to outliers (e.g. one HITL took 30s because human was at lunch).
"""

from __future__ import annotations

import logging
import statistics
from typing import Any

logger = logging.getLogger(__name__)

PLACEHOLDER_SCORE: float = 0.5
"""Neutral score when no HITL round-trips were observed."""

GREEN_LATENCY_MS: float = 1000.0
"""≤ 1s round-trip → score 1.0 (sub-second is excellent)."""

RED_LATENCY_MS: float = 10000.0
"""≥ 10s round-trip → score 0.0 (poor responsiveness)."""


def _median_round_trip_ms(rounds: Any) -> float | None:
    """Compute the median ms from a ``hitl_round_trips`` list.

    Returns ``None`` when the list is empty or contains no numerics.
    """
    if rounds is None:
        return None
    try:
        ms_values = [float(v) for v in rounds]
    except (TypeError, ValueError):
        logger.debug("hitl_latency: hitl_round_trips contains non-numeric")
        return None
    if not ms_values:
        return None
    return float(statistics.median(ms_values))


def _legacy_score_from_seconds(seconds: float) -> float:
    """v0.2.0 bucketed scoring (preserved for backward-compat tests)."""
    if seconds < 1.0:
        return 1.0
    if seconds < 5.0:
        return 0.7
    if seconds < 30.0:
        return 0.5
    if seconds < 300.0:
        return 0.3
    return 0.0


class HitlLatency:
    """HITL ``interrupt() → supply_feedback`` round-trip wall time.

    v0.3.0 F1.3 real measurement: when ``hitl_round_trips`` (list of
    ms) is supplied, compute the median and linearly scale 1.0@1000ms
    → 0.0@10000ms.  When only the legacy ``hitl_round_trip_seconds``
    scalar is supplied, fall back to v0.2.0 bucketed scoring (kept for
    backward-compat with v0.2.x tests + downstream nines.toml diffs).
    """

    name = "hitl_latency"

    def score(self, evidence: dict[str, Any]) -> float:
        """Return latency-derived score in ``[0.0, 1.0]``."""
        median_ms = _median_round_trip_ms(evidence.get("hitl_round_trips"))
        if median_ms is not None:
            if median_ms <= GREEN_LATENCY_MS:
                return 1.0
            if median_ms >= RED_LATENCY_MS:
                return 0.0
            span = RED_LATENCY_MS - GREEN_LATENCY_MS
            return max(0.0, min(1.0, 1.0 - (median_ms - GREEN_LATENCY_MS) / span))

        seconds = evidence.get("hitl_round_trip_seconds")
        if seconds is None:
            return PLACEHOLDER_SCORE
        try:
            return _legacy_score_from_seconds(float(seconds))
        except (TypeError, ValueError):
            return PLACEHOLDER_SCORE
