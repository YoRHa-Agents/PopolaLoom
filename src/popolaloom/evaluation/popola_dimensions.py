"""8-dimension PopolaLoom-nines scorers (v0.3.0 F1 — refactored).

v0.3.0 F1 split each dimension into its own module under
:mod:`popolaloom.evaluation.dimensions`; this module is now a thin
**re-export shim** that:

- re-exports the 8 scorer classes for backward compat (existing
  ``from popolaloom.evaluation.popola_dimensions import ...`` imports
  in tests + downstream code keep working unchanged)
- holds the canonical ordered :data:`DIMENSIONS` list (matches
  ``nines.toml [eval] dimensions``)
- defines the :class:`DimensionScorer` Protocol (single source of
  truth for the scorer ABC)

See :mod:`popolaloom.evaluation.dimensions` package docstring for the
real measurement details per dimension.

Score conventions (v0.3.0 — same as v0.2.0 for backward compat):

- ``1.0`` — capability is present and demonstrably correct in the
  evidence (e.g. dispatch_isolation: daemon PGID and CLI PGID differ).
- ``0.5`` — placeholder used when the evidence is insufficient to
  conclude either way (e.g. running ``popola eval run`` against an
  empty events_dir).  Avoids unfairly penalising the composite when
  the runner is invoked outside a real session.
- ``0.0`` — capability is missing or evidence shows a regression.

Evidence schema (v0.3.0 keys consumed; v0.2.0 keys still tolerated):

``daemon_pid`` (int|None) — popolad PID (used to ``os.getpgid()``).
``cli_pid`` (int|None) — sample CLI subprocess PID.
``daemon_pgid`` / ``cli_pgid`` (int|None) — pre-computed PGIDs (test).
``cycle_demo_present`` (bool) — Stage B Gen-Verifier subgraph_dev_test
    module imports cleanly + has the converged final state in last run.
``cycle_demo_iters`` (int|None) — number of iters in last demo run.
``hitl_round_trips`` (list[float]|None) — observed round-trip ms.
``hitl_round_trip_seconds`` (float|None) — single round-trip seconds (legacy).
``attach_event_log_paths`` (list[Path]|None) — event log file paths.
``attach_tail_counts`` (list[int]|None) — corresponding tail() counts.
``attach_complete_count`` / ``attach_total_count`` (int|None) — legacy.
``locks_present`` (set[str]|None) — names of all locks (legacy).
``dispatched_event_hash`` (str|None) — SHA256 of dispatch-side ids.
``attached_event_hash`` (str|None) — SHA256 of attach-side ids.
``event_count_after_recovery`` / ``event_count_before_recovery`` /
``recovered_count`` — legacy.
``token_usage_events`` (list[dict]|None) — claude stream-json usage.
``token_max_budget`` (int|None) — per-task token budget.
``token_budget_violations`` (int|None) — legacy.
``handoff_chain_intact`` (bool|None) — F5 forward-compat.
``handoff_owned_files_disjoint`` (bool|None) — F5 forward-compat.
``handoff_successful_count`` (int|None) — F5 forward-compat.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from popolaloom.evaluation.dimensions import (
    AttachCorrectness,
    CrossCliHandoff,
    CycleConvergence,
    DispatchIsolation,
    EventLogCompleteness,
    HitlLatency,
    SingleThreadedWrites,
)
from popolaloom.evaluation.dimensions.hitl_handleability import HitlHandleability

# Backward-compat: F1 created token_budget_compliance.py; F4 swaps it
# with hitl_handleability via this module's :data:`DIMENSIONS` list +
# nines.toml `[eval] dimensions` re-ordering.  ``TokenBudgetCompliance``
# remains importable so v0.2.x tests can still construct it for
# regression testing — but it is NO LONGER in the canonical
# :data:`DIMENSIONS` list.
try:  # pragma: no cover - F1 may or may not have created the file yet
    from popolaloom.evaluation.dimensions.token_budget_compliance import (
        TokenBudgetCompliance,
    )
except ImportError:  # pragma: no cover - safe fallback
    TokenBudgetCompliance = None  # type: ignore[assignment,misc]


@runtime_checkable
class DimensionScorer(Protocol):
    """Per-dimension PopolaLoom-nines scorer Protocol.

    Implementations MUST:

    - Expose a stable :attr:`name` matching ``nines.toml [eval] dimensions``.
    - Implement :meth:`score` — ideally a *pure* function of ``evidence``
      (some F1 scorers, e.g. CycleConvergence, do live invocation when
      evidence is missing — they document this clearly).
    - Return a float in ``[0.0, 1.0]``.  The runner enforces this with
      ``min(1.0, max(0.0, ...))`` clamp at the call site.
    """

    name: str

    def score(self, evidence: dict[str, Any]) -> float:
        """Map evidence to a normalised score in ``[0.0, 1.0]``."""
        ...


def _placeholder() -> float:
    """Module-level neutral score (used when evidence is insufficient).

    Re-exported for backward compat with v0.2.x tests that imported
    :func:`_placeholder` directly.  Always returns ``0.5`` — the v0.2.0
    "midpoint until real evidence overrides it" sentinel.
    """
    return 0.5


PLACEHOLDER_SCORE: float = 0.5
"""Backward-compat sentinel for the v0.2.0 mvp ``insufficient evidence``
neutral score; re-exported here so v0.2.x tests that imported it from
this module via :data:`PLACEHOLDER_SCORE` keep working.  The
per-dimension scorers each have their own ``PLACEHOLDER_SCORE``
constant — this re-export mirrors their value but the dimension
files are the source of truth."""


DIMENSIONS: list[DimensionScorer] = [
    DispatchIsolation(),
    CycleConvergence(),
    HitlLatency(),
    AttachCorrectness(),
    CrossCliHandoff(),
    SingleThreadedWrites(),
    EventLogCompleteness(),
    HitlHandleability(),
]
"""Canonical ordered list of all 8 PopolaLoom-nines scorers.

Order matches ``nines.toml [eval] dimensions``; consumers iterating
this list can rely on stable indices for diff displays and weighting.

v0.3.0 F4.E completed: ``token_budget_compliance`` ↔
``hitl_handleability`` swap (D3.10 1:1 with weight 0.10 retained).
``TokenBudgetCompliance`` is still importable from
:mod:`popolaloom.evaluation.dimensions.token_budget_compliance` for
backward-compat tests but is no longer in the active list.
"""


__all__ = [
    "DIMENSIONS",
    "PLACEHOLDER_SCORE",
    "AttachCorrectness",
    "CrossCliHandoff",
    "CycleConvergence",
    "DimensionScorer",
    "DispatchIsolation",
    "EventLogCompleteness",
    "HitlHandleability",
    "HitlLatency",
    "SingleThreadedWrites",
    "TokenBudgetCompliance",
    "_placeholder",
]
