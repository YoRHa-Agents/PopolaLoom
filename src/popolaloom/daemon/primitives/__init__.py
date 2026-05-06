"""popolad Conductor primitives — v0.3.0 F2 (relay / supervise / federate).

Per [spec §4.2](../../../../.local/memory/specs/popolaloom/spec.md), the
Conductor exposes 7 primitives:

1. ``dispatch``  — already in :mod:`popolaloom.daemon.server` (v0.2.0)
2. ``attach``    — already via SSE in :mod:`popolaloom.daemon.rpc` (v0.2.0)
3. ``probe``     — already via ``GET /probe`` (v0.2.0)
4. ``relay``     — F2 (this package)
5. ``supervise`` — F2 (this package)
6. ``federate``  — F2 (this package)
7. ``handoff``   — F4 (HITL handoff via HITLPrompt)

Design choices (per v0.3.0-plan.md D3.2 in
``.local/memory/specs/popolaloom/v0.3.0-plan.md``):

- **REST per-primitive** — each primitive gets its own RPC endpoint
  (``POST /relay`` / ``POST /supervise`` / ``POST /federate``) for
  OpenAPI clarity + independent error paths
- **Pydantic v2 schemas** — request/response models live alongside the
  primitive function so the schema is co-located with the behavior
- **Reuse existing dispatch_task** — relay + federate ultimately
  spawn child tasks via :meth:`Popolad.dispatch_task`, which gives
  them ArkTower persistence + LangGraph + supervisor for free

Workspace rule "No Silent Failures": each primitive validates its
inputs strictly + raises on parent task missing / invalid CLI / etc.
"""

from __future__ import annotations

from popolaloom.daemon.primitives.federate import (
    DEFAULT_FEDERATE_CLIS,
    MIN_FEDERATE_CLIS,
    FederateConfig,
    FederateResult,
    VoteOutcome,
    VotingStrategy,
    federate,
    tally_votes,
)
from popolaloom.daemon.primitives.relay import (
    RelayHandoffEnvelope,
    relay,
    to_handoff_envelope,
)
from popolaloom.daemon.primitives.supervise import (
    OnCompleteCallback,
    OnFailCallback,
    SubscriptionHandle,
    SuperviseRegistry,
    get_default_registry,
    reset_default_registry,
    supervise,
)

__all__ = [
    "DEFAULT_FEDERATE_CLIS",
    "FederateConfig",
    "FederateResult",
    "MIN_FEDERATE_CLIS",
    "OnCompleteCallback",
    "OnFailCallback",
    "RelayHandoffEnvelope",
    "SubscriptionHandle",
    "SuperviseRegistry",
    "VoteOutcome",
    "VotingStrategy",
    "federate",
    "get_default_registry",
    "relay",
    "reset_default_registry",
    "supervise",
    "tally_votes",
    "to_handoff_envelope",
]
