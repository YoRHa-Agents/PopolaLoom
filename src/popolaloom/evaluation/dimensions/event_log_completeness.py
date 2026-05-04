"""event_log_completeness — SHA256 of dispatched vs attached events (v0.3.0 F1.7).

Real measurement (v0.3.0 upgrade from v0.2.0 mvp):

The v0.2.0 mvp compared event count before/after recovery to verify the
``popolad.recovered`` event sequence.  v0.3.0 prefers a stronger
**SHA256 hash comparison** of the dispatched event ID sequence vs the
attached event ID sequence — this catches both count mismatches and
ordering bugs that count alone misses.

Score grid (per task spec F1.7):

- ``1.0`` — SHA256(dispatched_event_ids) == SHA256(attached_event_ids)
- ``0.0`` — hashes differ
- :data:`PLACEHOLDER_SCORE` — hashes not supplied + counts not supplied

Evidence keys consumed (in priority order):

1. ``dispatched_event_hash`` (str) + ``attached_event_hash`` (str)
   — preferred v0.3.0 form (computed by runner from event log + tail)
2. ``event_count_before_recovery`` + ``event_count_after_recovery``
   + ``recovered_count`` — v0.2.0 fallback
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

PLACEHOLDER_SCORE: float = 0.5
"""Neutral score when neither hashes nor counts are available."""


def hash_event_sequence(event_ids: Iterable[str]) -> str:
    """Return SHA256 hex digest over a sequence of event id strings.

    Each id is joined with ``\\n`` so ordering matters.  Used by the
    runner + tests to produce comparable hashes from dispatch-side and
    attach-side event sequences.
    """
    hasher = hashlib.sha256()
    for event_id in event_ids:
        hasher.update(str(event_id).encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


class EventLogCompleteness:
    """Event log completeness: dispatched vs attached event sequence parity.

    v0.3.0 F1.7 real measurement: SHA256 hash comparison of event id
    sequences captured at dispatch time and at attach time.
    """

    name = "event_log_completeness"

    def score(self, evidence: dict[str, Any]) -> float:
        """``1.0`` when dispatched / attached event sequences hash-match."""
        dispatched_hash = evidence.get("dispatched_event_hash")
        attached_hash = evidence.get("attached_event_hash")
        if dispatched_hash is not None and attached_hash is not None:
            return 1.0 if dispatched_hash == attached_hash else 0.0

        before = evidence.get("event_count_before_recovery")
        after = evidence.get("event_count_after_recovery")
        recovered = evidence.get("recovered_count")
        if before is None or after is None or recovered is None:
            return PLACEHOLDER_SCORE
        try:
            before_i = int(before)
            after_i = int(after)
            recovered_i = int(recovered)
        except (TypeError, ValueError):
            return PLACEHOLDER_SCORE
        expected = before_i + recovered_i
        if after_i >= expected:
            return 1.0
        if before_i == 0:
            return 0.0
        ratio = max(0.0, after_i / expected) if expected > 0 else 0.0
        return max(0.0, min(1.0, ratio))
