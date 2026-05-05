"""F2.5 skill_inject tests (≥3 cases per acceptance criteria).

Per v0.3.0-plan.md §4 Stage F2.5 — verifies the devola-flow skill
detection, Workflow Context prepend, and skill.missing degrade path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from popolaloom.evolution.skill_inject import (
    SkillCheckResult,
    check_skill_present,
    emit_skill_check_event,
    prepend_workflow_context,
)

# ── F2.5.A — skill detection (file system mock via tmp_path) ──────────────


def test_skill_present_detects_cursor_skill_md(tmp_path: Path) -> None:
    """check_skill_present(home=tmp) returns present=True when cursor SKILL.md exists."""
    cursor_dir = tmp_path / ".cursor" / "skills" / "devola-flow"
    cursor_dir.mkdir(parents=True)
    (cursor_dir / "SKILL.md").write_text("# devola-flow\n", encoding="utf-8")

    result = check_skill_present(home=tmp_path)
    assert isinstance(result, SkillCheckResult)
    assert result.present is True
    assert any("cursor" in str(p) for p in result.found_paths)


def test_skill_present_detects_claude_skill_md(tmp_path: Path) -> None:
    """claude SKILL.md alone is sufficient for present=True."""
    claude_dir = tmp_path / ".claude" / "skills" / "devola-flow"
    claude_dir.mkdir(parents=True)
    (claude_dir / "SKILL.md").write_text("# devola-flow\n", encoding="utf-8")

    result = check_skill_present(home=tmp_path)
    assert result.present is True
    assert any("claude" in str(p) for p in result.found_paths)


def test_skill_missing_degrades_with_warning(tmp_path: Path, caplog) -> None:
    """When neither location has SKILL.md, present=False + warning logged."""
    import logging

    with caplog.at_level(logging.WARNING):
        result = check_skill_present(home=tmp_path)
    assert result.present is False
    assert result.found_paths == []
    assert any("skill.missing" in r.message for r in caplog.records)


# ── F2.5.B — prepend_workflow_context renders all required fields ─────────


def test_prepend_workflow_context_includes_section_header() -> None:
    """The prepend output contains the canonical Workflow Context header."""
    out = prepend_workflow_context(
        "user prompt body",
        round_num=2,
        max_rounds=5,
        prior_nines=0.83,
        gate_threshold=0.85,
    )
    assert "## Workflow Context (devola-flow)" in out
    assert "round_num: 2" in out
    assert "max_rounds: 5" in out
    assert "prior_nines: 0.83" in out
    assert "gate_threshold: 0.85" in out
    assert "user prompt body" in out


def test_prepend_workflow_context_renders_reinforcement_block() -> None:
    """When reinforcement is non-empty, it appears between context and prompt."""
    reinforcement = (
        "## Reinforcement Rules (from round 1)\n"
        "- [blocker] fix the test_quality regression\n"
        "- [critical] address the architecture drift\n"
    )
    out = prepend_workflow_context(
        "next round prompt",
        round_num=2,
        max_rounds=5,
        prior_nines=0.83,
        reinforcement=reinforcement,
    )
    assert "Reinforcement" in out
    assert "blocker" in out
    assert "critical" in out
    assert "next round prompt" in out
    workflow_idx = out.index("## Workflow Context")
    reinforcement_idx = out.index("Reinforcement")
    prompt_idx = out.index("next round prompt")
    assert workflow_idx < reinforcement_idx < prompt_idx


def test_prepend_workflow_context_validates_round_num_range() -> None:
    """round_num > max_rounds raises ValueError (WorkflowContext invariant)."""
    from pydantic import ValidationError

    with pytest.raises((ValidationError, ValueError)):
        prepend_workflow_context(
            "p", round_num=10, max_rounds=5, prior_nines=0.5
        )


# ── F2.5.C — emit skill.missing event when not present ───────────────────


class _RecordingEventLog:
    """Tiny event log stub that records ``append`` calls for inspection."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def append(self, type_: str, data: dict) -> None:
        self.calls.append((type_, dict(data)))


def test_emit_skill_check_event_writes_skill_missing_when_absent(
    tmp_path: Path,
) -> None:
    """skill_present=False emits both skill.checked and skill.missing events."""
    log = _RecordingEventLog()
    result = check_skill_present(home=tmp_path)
    emit_skill_check_event(event_log=log, round_num=2, result=result)
    types = [t for t, _ in log.calls]
    assert "skill.checked" in types
    assert "skill.missing" in types


def test_emit_skill_check_event_only_writes_checked_when_present(
    tmp_path: Path,
) -> None:
    """skill_present=True emits skill.checked only (no skill.missing)."""
    cursor_dir = tmp_path / ".cursor" / "skills" / "devola-flow"
    cursor_dir.mkdir(parents=True)
    (cursor_dir / "SKILL.md").write_text("ok", encoding="utf-8")
    log = _RecordingEventLog()
    result = check_skill_present(home=tmp_path)
    emit_skill_check_event(event_log=log, round_num=1, result=result)
    types = [t for t, _ in log.calls]
    assert "skill.checked" in types
    assert "skill.missing" not in types
