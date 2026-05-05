"""hitl_handleability — HITL stack health dimension (v0.3.0 F4.E).

Replaces the v0.2.0 ``token_budget_compliance`` dimension at the same
0.10 weight per v0.3.0-plan §4 Stage F4.14 + D3.10.

Per roadmap §12.7 the formula combines four sub-scores:

    hitl_handleability = (schema_completeness × 0.3)
                       + (reply_parse_success_rate × 0.3)
                       + (cross_channel_sync_rate × 0.2)
                       + (lark_health × 0.2)

Where each sub-score is in ``[0.0, 1.0]``:

- ``schema_completeness``: every emitted ``HITLPrompt`` has all required
  fields (trigger, why, what, options ≥ 2, channels ≥ 2, deadline,
  default_option_id matching options).  Sourced from
  ``evidence.hitl_prompts_emitted`` + ``evidence.hitl_schema_failures``.
- ``reply_parse_success_rate``: fraction of incoming reply events
  (Lark card.action / message.receive / popola CLI feedback) that
  parsed cleanly into a :class:`HITLReply`.  Sourced from
  ``evidence.hitl_replies_received`` + ``evidence.hitl_replies_parsed``.
- ``cross_channel_sync_rate``: fraction of ``mark_answered`` calls
  that returned ``ok=True`` on the FIRST renderer (vs racing
  duplicates that lost the race).  Sourced from
  ``evidence.cross_channel_sync_total`` +
  ``evidence.cross_channel_sync_winners``.
- ``lark_health``: composite of:
    - Lark send success rate × 0.5
    - listener subprocess uptime ratio × 0.3
    - out/in round-trip P95 ≤ 10s ratio × 0.2

Sentinel ``0.5`` is used when evidence is insufficient (preserves
v0.2.0 mvp ``_placeholder()`` semantics so empty-events_dir runs do
not artificially deflate the composite).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

PLACEHOLDER_SCORE: float = 0.5
"""Neutral score when no HITL evidence is available."""

WEIGHT_SCHEMA: float = 0.3
"""Per roadmap §12.7 — schema_completeness sub-weight."""

WEIGHT_REPLY_PARSE: float = 0.3
"""reply_parse_success_rate sub-weight."""

WEIGHT_CROSS_SYNC: float = 0.2
"""cross_channel_sync_rate sub-weight."""

WEIGHT_LARK: float = 0.2
"""lark_health sub-weight."""

LARK_SEND_WEIGHT: float = 0.5
"""lark_health internal: send success rate weight."""

LARK_UPTIME_WEIGHT: float = 0.3
"""lark_health internal: listener uptime ratio weight."""

LARK_LATENCY_WEIGHT: float = 0.2
"""lark_health internal: out/in P95 ≤ 10s ratio weight."""


def _safe_ratio(numerator: Any, denominator: Any) -> float | None:
    """Return ``num/den`` clamped to ``[0, 1]`` or ``None`` on bad input.

    Used for every sub-rate in this dimension.  ``None`` signals
    "insufficient evidence" so the caller can fall back to
    :data:`PLACEHOLDER_SCORE`.
    """
    try:
        n = float(numerator)
        d = float(denominator)
    except (TypeError, ValueError):
        return None
    if d <= 0:
        return None
    return max(0.0, min(1.0, n / d))


def _compute_lark_health(evidence: dict[str, Any]) -> float | None:
    """Compute the lark_health sub-score (0..1) or None when no evidence."""
    send_total = evidence.get("lark_send_total")
    send_ok = evidence.get("lark_send_ok")
    uptime_total_s = evidence.get("lark_listener_uptime_total_s")
    uptime_alive_s = evidence.get("lark_listener_uptime_alive_s")
    rt_total = evidence.get("lark_roundtrip_total")
    rt_under_10s = evidence.get("lark_roundtrip_under_10s")

    components: list[tuple[float, float]] = []  # (weight, value)

    send_rate = _safe_ratio(send_ok, send_total)
    if send_rate is not None:
        components.append((LARK_SEND_WEIGHT, send_rate))

    uptime_ratio = _safe_ratio(uptime_alive_s, uptime_total_s)
    if uptime_ratio is not None:
        components.append((LARK_UPTIME_WEIGHT, uptime_ratio))

    latency_ratio = _safe_ratio(rt_under_10s, rt_total)
    if latency_ratio is not None:
        components.append((LARK_LATENCY_WEIGHT, latency_ratio))

    if not components:
        return None
    total_w = sum(w for w, _ in components)
    if total_w <= 0:
        return None
    return sum(w * v for w, v in components) / total_w


class HitlHandleability:
    """HITL stack handle-ability composite (v0.3.0 F4.E real measurement).

    Per roadmap §12.7 + v0.3.0-plan D3.10 — replaces
    :class:`popolaloom.evaluation.dimensions.token_budget_compliance.TokenBudgetCompliance`
    at the same 0.10 weight in ``nines.toml``.
    """

    name = "hitl_handleability"

    def score(self, evidence: dict[str, Any]) -> float:
        """Return the weighted composite score in ``[0.0, 1.0]``.

        Returns :data:`PLACEHOLDER_SCORE` when no HITL evidence is
        present (e.g. running ``popola eval run`` against an empty
        events_dir before any HITL prompt was issued).
        """
        components: list[tuple[float, float]] = []  # (weight, value)

        schema_emitted = evidence.get("hitl_prompts_emitted")
        schema_failures = evidence.get("hitl_schema_failures")
        if schema_emitted is not None and schema_failures is not None:
            try:
                emitted = int(schema_emitted)
                failures = int(schema_failures)
            except (TypeError, ValueError):
                emitted = 0
                failures = 0
            if emitted > 0:
                schema_score = max(0.0, min(1.0, 1.0 - failures / emitted))
                components.append((WEIGHT_SCHEMA, schema_score))

        reply_total = evidence.get("hitl_replies_received")
        reply_parsed = evidence.get("hitl_replies_parsed")
        reply_rate = _safe_ratio(reply_parsed, reply_total)
        if reply_rate is not None:
            components.append((WEIGHT_REPLY_PARSE, reply_rate))

        sync_total = evidence.get("cross_channel_sync_total")
        sync_wins = evidence.get("cross_channel_sync_winners")
        sync_rate = _safe_ratio(sync_wins, sync_total)
        if sync_rate is not None:
            components.append((WEIGHT_CROSS_SYNC, sync_rate))

        lark_health = _compute_lark_health(evidence)
        if lark_health is not None:
            components.append((WEIGHT_LARK, lark_health))

        if not components:
            return PLACEHOLDER_SCORE

        total_w = sum(w for w, _ in components)
        if total_w <= 0:
            return PLACEHOLDER_SCORE
        return sum(w * v for w, v in components) / total_w


__all__ = [
    "LARK_LATENCY_WEIGHT",
    "LARK_SEND_WEIGHT",
    "LARK_UPTIME_WEIGHT",
    "PLACEHOLDER_SCORE",
    "WEIGHT_CROSS_SYNC",
    "WEIGHT_LARK",
    "WEIGHT_REPLY_PARSE",
    "WEIGHT_SCHEMA",
    "HitlHandleability",
]
