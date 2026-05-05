"""cross_cli_handoff — placeholder pending F5 (v0.3.0 F1.5).

**Intentional placeholder for v0.3.0 F1.**

Real cross-CLI handoff measurement requires running an actual
cursor → claude → codex chain end-to-end and verifying the
handoff_envelope schema travels intact through the relay primitive
(F2) and the supervisor pipeline (F4).  That end-to-end is wired in
F5 (S5 self-bootstrap real version), which depends on F2 + F3 + F4
landing first.

For F1 we deliberately return :data:`PLACEHOLDER_SCORE` (``0.5``) so
the composite isn't dragged down by a feature that is acknowledged as
"not yet measurable in this stage".  The accompanying
``v0.3.0-plan.md §4 Stage F5`` is the source of truth for when this
gets upgraded.

Evidence keys consumed (when supplied by F5):

- ``handoff_chain_intact`` (bool|None)        — full trace traversed
- ``handoff_owned_files_disjoint`` (bool|None) — owned_files invariant
- ``handoff_successful_count`` (int|None)     — # successful handoffs

When any of those is ``True`` we still return ``1.0`` so this scorer
is forward-compatible with the F5 evidence pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

PLACEHOLDER_SCORE: float = 0.5
"""F1 deliberate placeholder; F5 lifts this to a real measurement."""


class CrossCliHandoff:
    """Cross-CLI handoff (cursor → claude → codex sequencing).

    F1 stub: returns 0.5 unconditionally unless explicit success
    evidence is supplied.  F5 will replace this with a real
    measurement that traces handoff_envelope through the relay +
    supervise primitives end-to-end.
    """

    name = "cross_cli_handoff"

    def score(self, evidence: dict[str, Any]) -> float:
        """Return 0.5 placeholder unless F5-style success evidence is present."""
        if evidence.get("handoff_chain_intact") and evidence.get(
            "handoff_owned_files_disjoint"
        ):
            return 1.0

        success = evidence.get("handoff_successful_count")
        if success is not None:
            try:
                count = int(success)
            except (TypeError, ValueError):
                return PLACEHOLDER_SCORE
            if count > 0:
                return 1.0
            return 0.0

        return PLACEHOLDER_SCORE
