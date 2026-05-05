"""Self-evolution schemas — v0.3.0 F2.5 prep, schema-only in v0.2.3.

Per `.local/memory/specs/popolaloom/testing-matrix.md` §11.2 + roadmap
§11.4 — this package occupies the schema surface that v0.3.0 F2.5
(devola-flow double gate + Workflow Context prepend) will consume.
Full wiring (skill_inject prepend, dual_gate parser, reinforcement
top-5 renderer) is deferred to v0.3.0.

Contents:

- :class:`WorkflowContext` — the prompt prefix prepended onto every L3
  sub-task dispatch (round_num / max_rounds / prior_nines /
  reinforcement_rules / gate_threshold) per testing-matrix.md §11.2.

Validation invariants (per workspace rule "No Silent Failures"):

- ``round_num`` ≥ 1 and ≤ ``max_rounds``.
- ``max_rounds`` ≥ 1 and ≤ 99 (sanity cap; 5-round self-evolution is
  the design target per spec §3.4).
- ``prior_nines`` ∈ ``[0, 1]`` (8-dimension composite score from the
  previous round's nines run).
- ``reinforcement_rules`` ≤ 5 entries (top-5 finding promotion per
  spec §11.2 — the L3 prompt MUST stay digestible).
- ``gate_threshold`` ∈ ``[0, 1]`` (default 0.85 per spec §4.1 row
  "gate" + ADR-0002 §2.1).

Use::

    from popolaloom.evolution import WorkflowContext

    ctx = WorkflowContext(
        round_num=3,
        max_rounds=5,
        prior_nines=0.872,
        reinforcement_rules=[
            "Always emit composite_score in 3-section output.",
            "Include at least 1 finding per severity bucket.",
        ],
        gate_threshold=0.85,
    )
    prompt_prefix = ctx.render()  # planned v0.3.0 — not yet wired.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_GATE_THRESHOLD: float = 0.85
"""Per spec §4.1 row "gate" + ADR-0002 §2.1 — verifier composite_score
above which the inner gate considers the L3 sub-task acceptable."""

MAX_REINFORCEMENT_RULES: int = 5
"""Per testing-matrix.md §11.2 + spec §3.4.4 — top-5 finding promotion
keeps the L3 Workflow Context prepend concise."""

MAX_ROUNDS: int = 99
"""Sanity cap on ``WorkflowContext.max_rounds`` to prevent absurd
configurations; 5-round self-evolution is the design target per spec."""


class WorkflowContext(BaseModel):
    """The prompt-prefix block prepended to every L3 sub-task dispatch.

    Per testing-matrix.md §11.2 — when v0.3.0 F2.5 wires this in, every
    PopolaLoom-driven sub-task prompt will start with a
    ``## Workflow Context (devola-flow)`` markdown section serialised
    from this model.  In v0.2.3 we only ship the schema so the Tier 1
    schema test (``tests/matrix/tier1/test_devolaflow_context_schema.py``)
    can lock down the contract.

    Attributes:
        round_num:           Current round (1-indexed, ≤ max_rounds).
        max_rounds:          Outer self-evolution cap (1..99).
        prior_nines:         Composite score from the previous round
                             (0..1; 0 on round 1).
        reinforcement_rules: ≤ 5 strings carried forward from the
                             previous round's findings (top-5 promotion
                             per spec §11.2).
        gate_threshold:      Inner-gate score floor (0..1; default
                             0.85 per spec §4.1).
        plan_id:             Optional plan identifier (set by the
                             outer dispatcher; nullable in v0.2.3 so
                             schema tests can be standalone).

    Workspace rule "No Silent Failures": all invariant violations
    raise :class:`pydantic.ValidationError` rather than warn-and-coerce.
    """

    model_config = ConfigDict(extra="forbid")

    round_num: int = Field(..., ge=1, le=MAX_ROUNDS)
    max_rounds: int = Field(..., ge=1, le=MAX_ROUNDS)
    prior_nines: float = Field(..., ge=0.0, le=1.0)
    reinforcement_rules: list[str] = Field(default_factory=list)
    gate_threshold: float = Field(default=DEFAULT_GATE_THRESHOLD, ge=0.0, le=1.0)
    plan_id: str | None = None

    @field_validator("reinforcement_rules")
    @classmethod
    def _max_five_rules(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_REINFORCEMENT_RULES:
            raise ValueError(
                f"WorkflowContext.reinforcement_rules must have ≤ {MAX_REINFORCEMENT_RULES} "
                f"entries (got {len(v)}); top-5 promotion per testing-matrix.md §11.2"
            )
        for i, rule in enumerate(v):
            if not isinstance(rule, str) or not rule.strip():
                raise ValueError(
                    f"WorkflowContext.reinforcement_rules[{i}] must be a non-empty string"
                )
        return v

    @model_validator(mode="after")
    def _round_within_max(self) -> WorkflowContext:
        if self.round_num > self.max_rounds:
            raise ValueError(
                f"WorkflowContext.round_num={self.round_num} cannot exceed "
                f"max_rounds={self.max_rounds}"
            )
        return self

    def render(self) -> str:
        """Render the Workflow Context block as a Markdown prefix.

        Planned to be prepended to every L3 sub-task prompt by the
        v0.3.0 F2.5 dispatcher.  In v0.2.3 we only need the renderer
        to be deterministic + schema-driven for the Tier 1 test.
        """
        rules_block = (
            "\n".join(f"  - {r}" for r in self.reinforcement_rules)
            if self.reinforcement_rules
            else "  - (none)"
        )
        plan_line = f"plan_id: {self.plan_id}\n" if self.plan_id else ""
        return (
            "## Workflow Context (devola-flow)\n"
            f"round_num: {self.round_num}\n"
            f"max_rounds: {self.max_rounds}\n"
            f"prior_nines: {self.prior_nines}\n"
            f"gate_threshold: {self.gate_threshold}\n"
            f"{plan_line}"
            "reinforcement_rules:\n"
            f"{rules_block}\n"
        )


__all__ = [
    "DEFAULT_GATE_THRESHOLD",
    "DoctorReport",
    "InstallOutcome",
    "MAX_REINFORCEMENT_RULES",
    "MAX_ROUNDS",
    "SKILL_TARGETS",
    "UpgradeOutcome",
    "WorkflowContext",
    "check_skill_health",
    "dual_gate",
    "install_all_skills",
    "install_skill",
    "reinforcement",
    "skill_doctor",
    "skill_inject",
    "skill_install",
    "skill_upgrade",
    "upgrade_skill",
]


# v0.5.0 Stage S4: re-export the new skill install / doctor / upgrade
# library APIs alongside the v0.3.0 dual_gate / reinforcement / skill_inject
# stack so callers can do ``from popolaloom.evolution import install_skill``
# without reaching into individual sub-modules.  Imports stay at the bottom
# of the file (after the WorkflowContext class definition) so the
# sub-modules can ``from popolaloom.evolution import WorkflowContext``
# without triggering a partial-import cycle.
from popolaloom.evolution import (  # noqa: E402
    dual_gate,
    reinforcement,
    skill_doctor,
    skill_inject,
    skill_install,
    skill_upgrade,
)
from popolaloom.evolution.skill_doctor import DoctorReport, check_skill_health  # noqa: E402
from popolaloom.evolution.skill_inject import SKILL_TARGETS  # noqa: E402
from popolaloom.evolution.skill_install import (  # noqa: E402
    InstallOutcome,
    install_all_skills,
    install_skill,
)
from popolaloom.evolution.skill_upgrade import UpgradeOutcome, upgrade_skill  # noqa: E402
