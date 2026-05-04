"""popolaloom-evaluation — PopolaLoom-nines self-evaluation runner (v0.2.0 Stage E E5).

This subpackage implements the **8-dimension PopolaLoom-nines** evaluation
framework defined in :file:`nines.toml` and spec §3.4.1 self-bootstrap +
06 §6.1 Day-6 (PopolaLoom-nines mvp).

Public surface:

- :class:`DimensionScorer` — :class:`typing.Protocol` (``runtime_checkable``)
  every per-dimension scorer satisfies.  ``score(evidence) -> 0..1``.
- :data:`DIMENSIONS` — list of 8 :class:`DimensionScorer` instances in the
  same order as ``nines.toml [eval] dimensions``; iteration order is
  stable so consumers (e.g. the CLI runner) can rely on it for reporting.
- :class:`NinesReport` — frozen dataclass capturing per-dim scores +
  weighted composite + run metadata (timestamp / popolaloom version).
- :func:`run_evaluation` — orchestrator: collect evidence from
  ``$POPOLA_HOME/events`` + optional :class:`TaskPersistence`, score
  every dimension, fold into composite by ``nines.toml`` weights.
- :func:`collect_evidence` — pure helper that walks event logs and the
  ArkTower SQLite task pool to produce the raw ``dict`` consumed by
  the scorers.  Exposed for unit tests that pre-fabricate evidence.
- :func:`toml_serialize` — render a :class:`NinesReport` as TOML for
  the ``popola eval run --output`` command.

Dimensions (8 total, matching ``nines.toml``):

1. ``dispatch_isolation`` — popolad daemon vs CLI subprocess process /
   PGID isolation (real: setsid + cross-terminal survival).
2. ``cycle_convergence`` — Gen-Verifier subgraph dev↔test loop converges
   in ≤ 2 iterations (mvp: based on Stage B demo).
3. ``hitl_latency`` — ``interrupt() → supply_feedback`` round-trip wall
   time (mvp: 0 unless evidence provides a measured value).
4. ``attach_correctness`` — cross-process attach completeness (mvp:
   based on whether attach SSE delivered all events).
5. ``cross_cli_handoff`` — defaults to 0 in v0.2.0 (multi-CLI handoff
   is a v0.3.0 feature; nines.toml weight 0.15 still counts so the
   composite is honestly limited until the feature lands).
6. ``single_threaded_writes`` — ``_event_logs_lock`` + ``StateStore._lock``
   coverage; ``EventLog._lock`` always present.
7. ``event_log_completeness`` — S1 self-bootstrap "event count after
   restart ≥ event count before restart + N (recovered)".
8. ``token_budget_compliance`` — N/A in v0.2.0 (planned for v0.3.0+
   when token budget tracking arrives); defaults to 0.5 placeholder
   so the composite isn't artificially crippled.

The mvp scorers operate on a small evidence dict so the runner can
work without spawning real subprocesses or requiring live persistence
— ideal for ``popola eval run`` invocations from CI/CD.  Real
evidence (e.g. PGID compare, real HITL round-trip times) is collected
by :func:`collect_evidence` when running against a real
``$POPOLA_HOME``.
"""

from popolaloom.evaluation.popola_dimensions import (
    DIMENSIONS,
    AttachCorrectness,
    CrossCliHandoff,
    CycleConvergence,
    DimensionScorer,
    DispatchIsolation,
    EventLogCompleteness,
    HitlHandleability,
    HitlLatency,
    SingleThreadedWrites,
    TokenBudgetCompliance,
)
from popolaloom.evaluation.runner import (
    NinesReport,
    collect_evidence,
    run_evaluation,
    toml_serialize,
)

__all__ = [
    "DIMENSIONS",
    "AttachCorrectness",
    "CrossCliHandoff",
    "CycleConvergence",
    "DimensionScorer",
    "DispatchIsolation",
    "EventLogCompleteness",
    "HitlHandleability",
    "HitlLatency",
    "NinesReport",
    "SingleThreadedWrites",
    "TokenBudgetCompliance",
    "collect_evidence",
    "run_evaluation",
    "toml_serialize",
]
