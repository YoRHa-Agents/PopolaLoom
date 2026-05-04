"""token_budget_compliance — parse claude stream-json usage (v0.3.0 F1.8).

Real measurement (v0.3.0 upgrade from v0.2.0 mvp):

The v0.2.0 mvp scored on a binary ``token_budget_violations`` count.
v0.3.0 inspects the live event log for ``claude --output-format
stream-json`` envelopes that include a ``usage`` field (input_tokens /
output_tokens / cache_read_input_tokens) and verifies the cumulative
usage stays within ``max_tokens`` budget.

Score grid (per task spec F1.8):

- ``1.0`` — usage events present + within budget
- ``0.0`` — usage events present + over budget
- ``0.5`` — no usage events (e.g. mock CLI; placeholder unchanged
  from v0.2.0 mvp)

Evidence keys consumed (priority order):

1. ``token_usage_events`` (list[dict]|None) — preferred v0.3.0; each
   dict has ``input_tokens`` / ``output_tokens`` keys.  Optional
   ``token_max_budget`` (int) sets the budget; default 200_000.
2. ``token_budget_violations`` (int|None) — v0.2.0 fallback.

Important: this dimension WILL be replaced in F4 by
``hitl_handleability.py`` per [v0.3.0-plan.md D3.10](
../../../../.local/memory/specs/popolaloom/v0.3.0-plan.md
).  The current implementation must therefore stay self-contained
(no shared state with other scorers) so the file move is zero-touch.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

PLACEHOLDER_SCORE: float = 0.5
"""Neutral score when no usage events are observed (mock CLI path)."""

DEFAULT_TOKEN_BUDGET: int = 200_000
"""Per-task token budget; aligns with claude default context window."""


def _sum_usage_tokens(usage_events: list[dict[str, Any]]) -> int:
    """Sum input + output tokens across stream-json usage events.

    Tolerant to missing keys: any envelope without an ``input_tokens``
    or ``output_tokens`` integer contributes ``0``.
    """
    total = 0
    for event in usage_events:
        if not isinstance(event, dict):
            continue
        for key in ("input_tokens", "output_tokens"):
            value = event.get(key)
            if value is None:
                continue
            try:
                total += int(value)
            except (TypeError, ValueError):
                continue
    return total


class TokenBudgetCompliance:
    """Token budget compliance via claude stream-json usage parsing.

    v0.3.0 F1.8 real measurement: parses ``token_usage_events`` from
    the evidence dict (filled by the runner from claude stream-json
    output); falls back to v0.2.0 ``token_budget_violations`` count.
    """

    name = "token_budget_compliance"

    def score(self, evidence: dict[str, Any]) -> float:
        """``1.0`` if cumulative usage ≤ budget; ``0.0`` if over."""
        usage_events = evidence.get("token_usage_events")
        if usage_events is not None:
            if not isinstance(usage_events, list) or not usage_events:
                return PLACEHOLDER_SCORE
            try:
                budget = int(evidence.get("token_max_budget", DEFAULT_TOKEN_BUDGET))
            except (TypeError, ValueError):
                budget = DEFAULT_TOKEN_BUDGET
            total = _sum_usage_tokens(usage_events)
            return 1.0 if total <= budget else 0.0

        violations = evidence.get("token_budget_violations")
        if violations is None:
            return PLACEHOLDER_SCORE
        try:
            return 1.0 if int(violations) == 0 else 0.0
        except (TypeError, ValueError):
            return PLACEHOLDER_SCORE
