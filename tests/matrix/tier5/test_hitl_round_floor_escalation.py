"""Tier 5 — HITL round_floor escalation scenario tests (v0.3.0 F4).

Per testing-matrix.md §1.5 + roadmap §12.6 + v0.3.0-plan §4 Stage F4.

≥ 3 cases — exercise the full round_floor escalation contract:
3-option escalation card on all 5 channels, ``defer`` as default.
"""

from __future__ import annotations

from popolaloom.hitl.triggers import create_round_floor_prompt

## v0.3.0 F4: factory unit tests are fast enough for the default lane
## (no subprocess, no DB); the @e2e marker was originally for the
## roundtrip variant in tier4. The escalation test is pure-Python.


def test_round_floor_prompt_has_three_options_and_defer_default() -> None:
    prompt = create_round_floor_prompt(
        round_num=2,
        blockers=["test_quality 0.65", "blocker R-EVO-3"],
        evidence_paths=["/tmp/round-2.jsonl"],
    )
    assert prompt.trigger == "round_floor"
    assert {o.id for o in prompt.options} == {"override", "rollback", "defer"}
    assert prompt.default_option_id == "defer"


def test_round_floor_prompt_uses_all_five_channels() -> None:
    prompt = create_round_floor_prompt(
        round_num=3,
        blockers=["x"],
        evidence_paths=["/tmp/x.jsonl"],
    )
    assert set(prompt.channels) == {"lark", "ide", "cli", "email", "signal"}


def test_round_floor_prompt_includes_blocker_summary_in_why() -> None:
    prompt = create_round_floor_prompt(
        round_num=2,
        blockers=["fail-1", "fail-2", "fail-3"],
        evidence_paths=[],
    )
    assert "Round 2" in prompt.why
    assert "fail-1" in prompt.why


def test_round_floor_prompt_attaches_evidence_paths() -> None:
    prompt = create_round_floor_prompt(
        round_num=2,
        blockers=["b"],
        evidence_paths=["/tmp/r2.jsonl", "/tmp/r2-other.jsonl"],
    )
    assert len(prompt.artifacts) == 2
    assert all(a.type == "event_log" for a in prompt.artifacts)
