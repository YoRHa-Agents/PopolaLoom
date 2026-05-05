"""F2.5 dual_gate tests (≥6 cases per acceptance criteria).

Per v0.3.0-plan.md §4 Stage F2.5 — verifies the 4 dual-gate verdicts
+ L3 stdout parsing (happy + sad path) + composite_score weighted
computation.
"""

from __future__ import annotations

import pytest

from popolaloom.evolution.dual_gate import (
    DEFAULT_WEIGHTS,
    L3Sections,
    compute_inner_score,
    evaluate_dual_gate,
    parse_l3_output,
)

# ── Dual-gate verdict matrix (4 cases) ────────────────────────────────────


def test_dual_gate_pass_pass_returns_pass() -> None:
    """Inner ≥ 0.85 + Outer ≥ prior + 0.02 → 'pass'."""
    verdict = evaluate_dual_gate(
        inner_scores=[0.9, 0.92],
        outer_score=0.88,
        prior_outer_score=0.85,
    )
    assert verdict == "pass"


def test_dual_gate_inner_fail_outer_pass_returns_inner_fail() -> None:
    """Inner < 0.85 + Outer ≥ prior + 0.02 → 'inner_fail'."""
    verdict = evaluate_dual_gate(
        inner_scores=[0.78, 0.92],
        outer_score=0.88,
        prior_outer_score=0.85,
    )
    assert verdict == "inner_fail"


def test_dual_gate_inner_pass_outer_fail_returns_outer_fail() -> None:
    """Inner ≥ 0.85 + Outer < prior + 0.02 → 'outer_fail'."""
    verdict = evaluate_dual_gate(
        inner_scores=[0.9, 0.91],
        outer_score=0.85,
        prior_outer_score=0.85,
    )
    assert verdict == "outer_fail"


def test_dual_gate_both_fail_returns_both_fail() -> None:
    """Inner < 0.85 + Outer < prior + 0.02 → 'both_fail'."""
    verdict = evaluate_dual_gate(
        inner_scores=[0.6, 0.7],
        outer_score=0.80,
        prior_outer_score=0.85,
    )
    assert verdict == "both_fail"


# ── parse_l3_output happy + sad path ──────────────────────────────────────


def test_parse_l3_output_happy_path_three_sections() -> None:
    """Canonical L3 output with all 3 sections parses cleanly."""
    output = """[devola-flow:round=2]

## Acceptance Verification
All AC met; coverage ≥ 90%.

## Gate Score Components
- test_quality: 0.92
- code_review: 0.88
- architecture: 0.90
- benchmark: 0.85

## Findings
- [major] minor refactor opportunity in helper module
- [minor] unused import in utils.py
"""
    sections = parse_l3_output(output)
    assert "All AC met" in sections.acceptance_verification
    assert sections.gate_score_components["test_quality"] == 0.92
    assert sections.gate_score_components["code_review"] == 0.88
    assert sections.gate_score_components["architecture"] == 0.90
    assert sections.gate_score_components["benchmark"] == 0.85
    assert len(sections.findings) == 2


def test_parse_l3_output_missing_acceptance_verification_raises_value_error() -> None:
    """L3 stdout without ## Acceptance Verification raises clearly."""
    output = """## Gate Score Components
- test_quality: 0.9

## Findings
- nothing
"""
    with pytest.raises(ValueError, match="Acceptance Verification"):
        parse_l3_output(output)


def test_parse_l3_output_empty_input_raises_value_error() -> None:
    """Empty L3 output raises ValueError per No Silent Failures rule."""
    with pytest.raises(ValueError, match="Acceptance Verification"):
        parse_l3_output("")


# ── compute_inner_score weighted formula ──────────────────────────────────


def test_compute_inner_score_weighted_formula() -> None:
    """compute_inner_score applies the 0.30 / 0.30 / 0.20 / 0.20 weights."""
    sections = L3Sections(
        acceptance_verification="all AC pass",
        gate_score_components={
            "test_quality": 0.90,
            "code_review": 0.90,
            "architecture": 0.80,
            "benchmark": 0.80,
        },
    )
    score = compute_inner_score(sections)
    expected = 0.90 * 0.30 + 0.90 * 0.30 + 0.80 * 0.20 + 0.80 * 0.20
    assert abs(score - expected) < 1e-9


def test_compute_inner_score_returns_zero_when_components_missing() -> None:
    """Missing weighted components → 0.0 (strict policy per roadmap §11.2)."""
    sections = L3Sections(
        acceptance_verification="ok",
        gate_score_components={"test_quality": 0.95},
    )
    score = compute_inner_score(sections)
    assert score == 0.0


def test_compute_inner_score_with_custom_weights() -> None:
    """Custom weights override the standard profile."""
    sections = L3Sections(
        acceptance_verification="ok",
        gate_score_components={"only_dim": 0.75},
    )
    score = compute_inner_score(sections, weights={"only_dim": 1.0})
    assert score == 0.75


def test_dual_gate_empty_inner_scores_returns_inner_fail() -> None:
    """Empty inner_scores → inner_fail (no scores ≥ threshold)."""
    verdict = evaluate_dual_gate(
        inner_scores=[],
        outer_score=0.90,
        prior_outer_score=0.80,
    )
    assert verdict == "inner_fail"


def test_default_weights_sum_to_one() -> None:
    """Sanity: standard profile weights sum to 1.00 (per roadmap §11.2)."""
    assert abs(sum(DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9
