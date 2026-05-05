"""Tier 1 — HITL trigger factory tests (v0.3.0 F4.A).

Per v0.3.0-plan §4 Stage F4.A + AC #2 of the v0.3.0 task spec, each of
the 5 trigger factories must produce a valid :class:`HITLPrompt` with
the documented invariants enforced.
"""

from __future__ import annotations

import pytest

from popolaloom.hitl import HITLOption, HITLPrompt
from popolaloom.hitl.triggers import (
    create_ambiguous_fix_prompt,
    create_critical_error_prompt,
    create_interrupt_prompt,
    create_persistent_regression_prompt,
    create_round_floor_prompt,
)

# ── create_interrupt_prompt ──────────────────────────────────────────────


def test_create_interrupt_prompt_happy_path() -> None:
    options = [
        HITLOption(id="postgres", label="Postgres"),
        HITLOption(id="sqlite", label="SQLite"),
    ]
    prompt = create_interrupt_prompt(
        graph_state={"db_choice_pending": True, "task": "init_storage"},
        question="Pick the storage backend",
        options=options,
    )
    assert isinstance(prompt, HITLPrompt)
    assert prompt.trigger == "info_request"
    assert prompt.what == "Pick the storage backend"
    assert {o.id for o in prompt.options} == {"postgres", "sqlite"}
    assert prompt.default_option_id in {"postgres", "sqlite"}
    assert "lark" in prompt.channels
    assert "graph" in prompt.why or "interrupt" in prompt.why.lower()


def test_create_interrupt_prompt_rejects_one_option() -> None:
    with pytest.raises(ValueError, match=r"≥ 2 options"):
        create_interrupt_prompt(
            graph_state={},
            question="bad",
            options=[HITLOption(id="only", label="Only")],
        )


def test_create_interrupt_prompt_explicit_default() -> None:
    options = [
        HITLOption(id="a", label="A"),
        HITLOption(id="b", label="B", default=True),
    ]
    prompt = create_interrupt_prompt(
        graph_state={},
        question="Q",
        options=options,
        default_option_id="a",
    )
    assert prompt.default_option_id == "a"


def test_create_interrupt_prompt_default_falls_back_to_first() -> None:
    options = [HITLOption(id="a", label="A"), HITLOption(id="b", label="B")]
    prompt = create_interrupt_prompt(graph_state={}, question="Q", options=options)
    assert prompt.default_option_id == "a"


def test_create_interrupt_prompt_picks_marked_default() -> None:
    options = [
        HITLOption(id="a", label="A"),
        HITLOption(id="b", label="B", default=True),
    ]
    prompt = create_interrupt_prompt(graph_state={}, question="Q", options=options)
    assert prompt.default_option_id == "b"


def test_create_interrupt_prompt_invalid_explicit_default() -> None:
    options = [HITLOption(id="a", label="A"), HITLOption(id="b", label="B")]
    with pytest.raises(ValueError, match=r"not in option ids"):
        create_interrupt_prompt(
            graph_state={},
            question="Q",
            options=options,
            default_option_id="c",
        )


# ── create_round_floor_prompt ────────────────────────────────────────────


def test_create_round_floor_prompt_happy_path() -> None:
    prompt = create_round_floor_prompt(
        round_num=2,
        blockers=["test_quality 0.65 < 0.85", "blocker finding R-EVO-3"],
        evidence_paths=["~/.popola/round-2/findings.md"],
    )
    assert prompt.trigger == "round_floor"
    assert prompt.default_option_id == "defer"
    assert {o.id for o in prompt.options} == {"override", "rollback", "defer"}
    assert set(prompt.channels) == {"lark", "ide", "cli", "email", "signal"}
    assert "Round 2" in prompt.why
    assert any(a.type == "event_log" for a in prompt.artifacts)


def test_create_round_floor_prompt_rejects_round_zero() -> None:
    with pytest.raises(ValueError, match=r"round_num"):
        create_round_floor_prompt(round_num=0, blockers=["x"], evidence_paths=[])


def test_create_round_floor_prompt_requires_blockers() -> None:
    with pytest.raises(ValueError, match=r"blocker"):
        create_round_floor_prompt(round_num=1, blockers=[], evidence_paths=[])


# ── create_critical_error_prompt ─────────────────────────────────────────


def test_create_critical_error_prompt_happy_path() -> None:
    options = [
        HITLOption(id="retry", label="Retry"),
        HITLOption(id="abort", label="Abort", default=True),
    ]
    prompt = create_critical_error_prompt(
        error_msg="LangGraph SqliteSaver write failed (disk full)",
        recovery_options=options,
    )
    assert prompt.trigger == "destructive_op"
    assert prompt.default_option_id == "abort"
    assert "disk full" in prompt.why


def test_create_critical_error_prompt_rejects_blank() -> None:
    with pytest.raises(ValueError, match=r"non-empty"):
        create_critical_error_prompt(
            error_msg="   ",
            recovery_options=[
                HITLOption(id="x", label="X"),
                HITLOption(id="y", label="Y"),
            ],
        )


# ── create_ambiguous_fix_prompt ──────────────────────────────────────────


def test_create_ambiguous_fix_prompt_happy_path() -> None:
    prompt = create_ambiguous_fix_prompt(
        scores={"fix_a": 0.91, "fix_b": 0.89, "fix_c": 0.88},
        paths=["diff_a.patch", "diff_b.patch", "diff_c.patch"],
    )
    assert prompt.trigger == "ambiguous_input"
    # Highest score is the default; abort always last.
    assert prompt.default_option_id == "fix_a"
    ids = [o.id for o in prompt.options]
    assert ids == ["fix_a", "fix_b", "fix_c", "abort"]
    # Three diff artifacts.
    assert len(prompt.artifacts) == 3
    assert all(a.type == "diff" for a in prompt.artifacts)


def test_create_ambiguous_fix_prompt_rejects_one_score() -> None:
    with pytest.raises(ValueError, match=r"≥ 2 candidate fixes"):
        create_ambiguous_fix_prompt(scores={"only": 0.9}, paths=[])


def test_create_ambiguous_fix_prompt_rejects_score_out_of_range() -> None:
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        create_ambiguous_fix_prompt(scores={"a": 1.5, "b": 0.5}, paths=[])


# ── create_persistent_regression_prompt ─────────────────────────────────


def test_create_persistent_regression_prompt_happy_path() -> None:
    history = [
        {"round": 1, "score": 0.72, "finding": "test_evolution flaky"},
        {"round": 2, "score": 0.71, "finding": "test_evolution flaky"},
        {"round": 3, "score": 0.70, "finding": "test_evolution flaky"},
    ]
    prompt = create_persistent_regression_prompt(
        r_id="R-EVO-3",
        round_history=history,
    )
    assert prompt.trigger == "round_floor"  # regression is a floor flavour
    assert prompt.default_option_id == "defer"
    assert "R-EVO-3" in prompt.why
    assert "round 1" in prompt.why and "round 3" in prompt.why
    assert set(prompt.channels) == {"lark", "ide", "cli", "email", "signal"}


def test_create_persistent_regression_rejects_short_history() -> None:
    with pytest.raises(ValueError, match=r"≥ 3 round history"):
        create_persistent_regression_prompt(
            r_id="R-EVO-3",
            round_history=[
                {"round": 1, "score": 0.7, "finding": "x"},
                {"round": 2, "score": 0.7, "finding": "x"},
            ],
        )


def test_create_persistent_regression_rejects_blank_r_id() -> None:
    with pytest.raises(ValueError, match=r"r_id must"):
        create_persistent_regression_prompt(
            r_id="",
            round_history=[
                {"round": 1}, {"round": 2}, {"round": 3},
            ],
        )


# ── Cross-cutting: every factory uses workspace-mandated channels ──────


@pytest.mark.parametrize(
    "factory_call",
    [
        lambda: create_round_floor_prompt(
            round_num=1, blockers=["b"], evidence_paths=[]
        ),
        lambda: create_critical_error_prompt(
            error_msg="x",
            recovery_options=[
                HITLOption(id="a", label="A"),
                HITLOption(id="b", label="B"),
            ],
        ),
        lambda: create_persistent_regression_prompt(
            r_id="R-X",
            round_history=[{"round": i, "score": 0.5, "finding": "f"} for i in range(3)],
        ),
    ],
)
def test_escalation_factories_use_all_five_channels(factory_call) -> None:
    """T2 (round_floor) and friends must fan out to all 5 channels (per §12.6)."""
    prompt = factory_call()
    expected = {"lark", "ide", "cli", "email", "signal"}
    assert set(prompt.channels) == expected
