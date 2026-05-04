"""F2.5 reinforcement tests (≥3 cases per acceptance criteria).

Per v0.3.0-plan.md §4 Stage F2.5 + D3.4 — verifies severity-based
filtering, Markdown rendering with prefix, and top-5 truncation.
"""

from __future__ import annotations

import pytest

from popolaloom.evolution.reinforcement import (
    Finding,
    ReinforcementInjector,
    collect_findings_from_round,
    render_reinforcement_section,
)


def test_collect_findings_filters_by_severity_minimum() -> None:
    """severity_min='major' excludes minor; includes major / critical / blocker."""
    evidence = {
        "findings": [
            {"severity": "minor", "text": "tiny style nit"},
            {"severity": "major", "text": "API contract gap"},
            {"severity": "critical", "text": "data loss risk"},
            {"severity": "blocker", "text": "security exploit"},
        ]
    }
    found = collect_findings_from_round(evidence, severity_min="major")
    found_set = set(found)
    assert "tiny style nit" not in found_set
    assert "API contract gap" in found_set
    assert "data loss risk" in found_set
    assert "security exploit" in found_set


def test_collect_findings_filters_at_critical_minimum() -> None:
    """severity_min='critical' yields only critical + blocker findings."""
    evidence = {
        "findings": [
            {"severity": "minor", "text": "a"},
            {"severity": "major", "text": "b"},
            {"severity": "critical", "text": "c"},
            {"severity": "blocker", "text": "d"},
        ]
    }
    out = collect_findings_from_round(evidence, severity_min="critical")
    assert set(out) == {"c", "d"}


def test_render_reinforcement_section_includes_severity_prefix() -> None:
    """Rendered section has '- [severity] (round N): text' bullets."""
    findings = [
        Finding(severity="blocker", text="must fix asap", round_num=1),
        Finding(severity="major", text="should fix", round_num=1),
    ]
    out = render_reinforcement_section(findings, round_num=2)
    assert "## Reinforcement Rules" in out
    assert "[blocker]" in out
    assert "[major]" in out
    assert "must fix asap" in out
    assert "should fix" in out


def test_top_5_finding_limit_enforced() -> None:
    """When > 5 findings supplied, only top-5 are rendered."""
    findings = [
        Finding(severity="blocker", text=f"finding {i}", round_num=1)
        for i in range(7)
    ]
    out = render_reinforcement_section(findings, round_num=2)
    bullet_lines = [line for line in out.splitlines() if line.startswith("- ")]
    assert len(bullet_lines) == 5


def test_top_5_finding_pool_extension_through_injector() -> None:
    """ReinforcementInjector deduplicates by text + ranks by severity."""
    injector = ReinforcementInjector()
    evidence = {
        "findings": [
            {"severity": "blocker", "text": "X1", "round_num": 1},
            {"severity": "blocker", "text": "X1", "round_num": 1},
            {"severity": "major", "text": "X2", "round_num": 1},
            {"severity": "minor", "text": "X3", "round_num": 1},
        ]
    }
    out = injector.collect_findings_from_round(evidence, severity_min="major")
    assert "X1" in out
    assert "X2" in out
    assert "X3" not in out
    assert out.count("X1") == 1


def test_render_handles_string_findings_without_severity_prefix() -> None:
    """String findings (vs Finding objects) render without [severity] tag."""
    out = render_reinforcement_section(["plain text finding"], round_num=2)
    assert "- plain text finding" in out
    assert "[major]" not in out


def test_collect_findings_rejects_unknown_severity_in_args() -> None:
    """severity_min='nonsense' raises ValueError (No Silent Failures)."""
    with pytest.raises(ValueError, match="severity_min"):
        collect_findings_from_round({"findings": []}, severity_min="not-a-grade")


def test_collect_findings_invalid_input_type_raises() -> None:
    """round_evidence not dict/list raises ValueError."""
    with pytest.raises(ValueError, match="dict or list"):
        collect_findings_from_round("not-a-list-or-dict")  # type: ignore[arg-type]


def test_collect_findings_truncates_long_text() -> None:
    """Findings > 200 chars are truncated with ellipsis."""
    long_text = "X" * 500
    out = collect_findings_from_round(
        [{"severity": "blocker", "text": long_text}]
    )
    assert len(out) == 1
    assert len(out[0]) <= 200
    assert out[0].endswith("...")


def test_collect_findings_skips_dict_without_text() -> None:
    """Dict without text/finding key is logged + skipped."""
    findings = collect_findings_from_round(
        [{"severity": "blocker", "extra": "no text key"}]
    )
    assert findings == []


def test_collect_findings_unrecognised_type_skipped() -> None:
    """Numeric/other types get skipped (logged at warning)."""
    findings = collect_findings_from_round(
        [{"severity": "blocker", "text": "ok"}, 12345]
    )
    assert findings == ["ok"]


def test_render_reinforcement_section_uses_internal_pool_when_none() -> None:
    """render(None, ...) renders from injector's accumulated pool."""
    injector = ReinforcementInjector()
    injector.collect_findings_from_round(
        [
            {"severity": "blocker", "text": "B", "round_num": 1},
            {"severity": "critical", "text": "C", "round_num": 1},
        ]
    )
    output = injector.render_reinforcement_section(round_num=2)
    assert "B" in output
    assert "C" in output


def test_collect_findings_unknown_severity_in_data_skipped() -> None:
    """Findings with unknown severity get skipped (not raised)."""
    findings = collect_findings_from_round(
        [
            {"severity": "blocker", "text": "real"},
            {"severity": "weirdness", "text": "skipped"},
        ]
    )
    assert "real" in findings
    assert "skipped" not in findings


def test_render_reinforcement_section_at_round_zero() -> None:
    """round_num=0 → header without (from round N) suffix."""
    out = render_reinforcement_section(["finding"], round_num=0)
    assert "## Reinforcement Rules" in out
    assert "from round" not in out
