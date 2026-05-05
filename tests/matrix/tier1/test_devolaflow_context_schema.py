"""Tier 1 schema tests for ``popolaloom.evolution.WorkflowContext`` Pydantic model.

Per testing-matrix.md §1.1 + §11.4 — locks down the prompt-prefix schema
that v0.3.0 F2.5 will prepend onto every L3 sub-task dispatch.

5 invariant cases (per task spec D):

1. Happy path — all required fields valid.
2. ``round_num`` < 1 raises (ge=1).
3. ``round_num`` > ``max_rounds`` raises (model_validator).
4. ``prior_nines`` outside [0, 1] raises (ge/le).
5. ``reinforcement_rules`` > 5 entries raises (top-5 promotion rule).

Plus: gate_threshold default 0.85; render() output contains the
required keys.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from popolaloom.evolution import (
    DEFAULT_GATE_THRESHOLD,
    MAX_REINFORCEMENT_RULES,
    WorkflowContext,
)


def test_happy_path_all_required_fields_valid() -> None:
    """Case 1: WorkflowContext accepts a fully-populated valid input."""
    ctx = WorkflowContext(
        round_num=2,
        max_rounds=5,
        prior_nines=0.872,
        reinforcement_rules=[
            "Always emit composite_score in 3-section output.",
            "Include at least 1 finding per severity bucket.",
        ],
        gate_threshold=0.85,
        plan_id="plan-001",
    )
    assert ctx.round_num == 2
    assert ctx.max_rounds == 5
    assert ctx.prior_nines == pytest.approx(0.872)
    assert len(ctx.reinforcement_rules) == 2
    assert ctx.gate_threshold == pytest.approx(0.85)
    assert ctx.plan_id == "plan-001"


def test_round_num_must_be_at_least_one() -> None:
    """Case 2: round_num=0 raises ValidationError (ge=1)."""
    with pytest.raises(ValidationError) as excinfo:
        WorkflowContext(round_num=0, max_rounds=5, prior_nines=0.5)
    assert "round_num" in str(excinfo.value).lower()


def test_round_num_cannot_exceed_max_rounds() -> None:
    """Case 3: round_num > max_rounds raises (model_validator)."""
    with pytest.raises(ValidationError) as excinfo:
        WorkflowContext(round_num=6, max_rounds=5, prior_nines=0.5)
    msg = str(excinfo.value).lower()
    assert "round_num" in msg or "max_rounds" in msg


def test_prior_nines_outside_zero_one_raises() -> None:
    """Case 4a: prior_nines=1.5 (out of [0,1]) raises ValidationError (le=1)."""
    with pytest.raises(ValidationError) as excinfo:
        WorkflowContext(round_num=1, max_rounds=5, prior_nines=1.5)
    assert "prior_nines" in str(excinfo.value).lower()


def test_prior_nines_negative_raises() -> None:
    """Case 4b: prior_nines=-0.1 raises ValidationError (ge=0)."""
    with pytest.raises(ValidationError):
        WorkflowContext(round_num=1, max_rounds=5, prior_nines=-0.1)


def test_reinforcement_rules_max_five_entries() -> None:
    """Case 5: 6+ reinforcement_rules entries raise ValidationError."""
    rules = [f"rule-{i}" for i in range(MAX_REINFORCEMENT_RULES + 1)]
    with pytest.raises(ValidationError) as excinfo:
        WorkflowContext(
            round_num=1,
            max_rounds=5,
            prior_nines=0.5,
            reinforcement_rules=rules,
        )
    assert "reinforcement_rules" in str(excinfo.value).lower()


def test_gate_threshold_defaults_to_constant() -> None:
    """Bonus: gate_threshold defaults to DEFAULT_GATE_THRESHOLD (0.85)."""
    ctx = WorkflowContext(round_num=1, max_rounds=5, prior_nines=0.0)
    assert ctx.gate_threshold == pytest.approx(DEFAULT_GATE_THRESHOLD)


def test_render_includes_all_required_fields() -> None:
    """Bonus: WorkflowContext.render() output contains the required key names."""
    ctx = WorkflowContext(
        round_num=3,
        max_rounds=5,
        prior_nines=0.7,
        reinforcement_rules=["rule-A", "rule-B"],
        plan_id="P-1",
    )
    out = ctx.render()
    for must_have in (
        "## Workflow Context (devola-flow)",
        "round_num: 3",
        "max_rounds: 5",
        "prior_nines: 0.7",
        "gate_threshold: 0.85",
        "reinforcement_rules:",
        "rule-A",
        "rule-B",
        "plan_id: P-1",
    ):
        assert must_have in out, f"render output missing {must_have!r}: got\n{out}"


def test_empty_reinforcement_rule_string_raises() -> None:
    """Bonus: a blank rule string raises ValidationError (no silent allow)."""
    with pytest.raises(ValidationError):
        WorkflowContext(
            round_num=1,
            max_rounds=5,
            prior_nines=0.5,
            reinforcement_rules=["valid", "  "],
        )


def test_max_rounds_zero_raises() -> None:
    """Bonus: max_rounds=0 raises ValidationError (ge=1)."""
    with pytest.raises(ValidationError):
        WorkflowContext(round_num=1, max_rounds=0, prior_nines=0.5)


def test_extra_fields_forbidden() -> None:
    """Bonus: extra fields raise ValidationError (extra='forbid')."""
    with pytest.raises(ValidationError):
        WorkflowContext(  # type: ignore[call-arg]
            round_num=1,
            max_rounds=5,
            prior_nines=0.5,
            unknown_field="bad",
        )
