"""Per-dimension PopolaLoom-nines scorers (v0.3.0 F1 — real measurement).

This package houses the 8 ``DimensionScorer`` implementations, one per
file, that replace the v0.2.0 mvp estimators.  Each scorer is a
standalone module so:

- v0.3.0+ can swap a single dimension (e.g. F4's
  ``token_budget_compliance`` → ``hitl_handleability``) without touching
  the other 7 (per v0.3.0-plan.md D3.1 in
  ``.local/memory/specs/popolaloom/``)
- Each dimension has its own evidence pipeline + unit tests
- The aggregation surface (``DIMENSIONS`` list, ``DimensionScorer`` Protocol)
  remains unchanged from v0.2.0 — :mod:`popola_dimensions` re-exports
  these so external imports stay stable

The 8 dimensions (with v0.2.0 → v0.3.0 measurement upgrades):

1. :class:`DispatchIsolation` — daemon vs CLI subprocess **PGID** check
   via ``os.getpgid(pid)`` (was: bool flag from evidence dict)
2. :class:`CycleConvergence` — assert ≤ 2 iters in subgraph_dev_test
   with deterministic [0.5, 0.9] score sequence (was: read iters from
   evidence dict)
3. :class:`HitlLatency` — compute median round-trip ms from
   ``hitl_round_trips`` list, scale 1.0@1000ms → 0.0@10000ms (was:
   single ``seconds`` field, threshold buckets)
4. :class:`AttachCorrectness` — compare event_log file line count vs
   ``tail()`` count (was: ratio of complete attaches)
5. :class:`CrossCliHandoff` — F1 stub returns 0.5 (real measurement
   requires F5 cross-CLI handoff which is later)
6. :class:`SingleThreadedWrites` — grep src for ``threading.Lock`` in
   event_log + state_store + server (was: introspection of `_lock`
   attribute via co_names)
7. :class:`EventLogCompleteness` — SHA256 hash of dispatched events
   sequence vs attached events sequence (was: count match)
8. :class:`TokenBudgetCompliance` — parse ``claude --output-format
   stream-json`` events for ``usage`` field (was: violations count)

Workspace rule "No Silent Failures": every scorer documents its
failure modes; sentinel ``0.5`` (placeholder) is reserved for the
"insufficient evidence" case as v0.2.0 (preserves backward-compat
diffing of nines.toml outputs).
"""

from __future__ import annotations

from popolaloom.evaluation.dimensions.attach_correctness import AttachCorrectness
from popolaloom.evaluation.dimensions.cross_cli_handoff import CrossCliHandoff
from popolaloom.evaluation.dimensions.cycle_convergence import CycleConvergence
from popolaloom.evaluation.dimensions.dispatch_isolation import DispatchIsolation
from popolaloom.evaluation.dimensions.event_log_completeness import EventLogCompleteness
from popolaloom.evaluation.dimensions.hitl_latency import HitlLatency
from popolaloom.evaluation.dimensions.single_threaded_writes import SingleThreadedWrites
from popolaloom.evaluation.dimensions.token_budget_compliance import TokenBudgetCompliance

__all__ = [
    "AttachCorrectness",
    "CrossCliHandoff",
    "CycleConvergence",
    "DispatchIsolation",
    "EventLogCompleteness",
    "HitlLatency",
    "SingleThreadedWrites",
    "TokenBudgetCompliance",
]
