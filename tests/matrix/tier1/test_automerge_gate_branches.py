"""Tier 1 — auto-merge gate branch coverage gap-fillers (v0.3.0 F3 polish).

Targeted tests for argparse + evidence-loading branches in
:mod:`popolaloom.gate.automerge` not exercised by ``test_automerge_gate.py``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from popolaloom.gate.automerge import (
    AutomergeConfig,
    GateThresholds,
    PathPolicy,
    evaluate_automerge,
    parse_evidence,
)


def _make_args(**overrides) -> argparse.Namespace:
    """Build a default argparse.Namespace; overrides per-call."""
    base = {
        "evidence": None,
        "pr_paths": None,
        "devolaflow_composite": None,
        "nines_current": None,
        "nines_prior": None,
        "blocker_count": None,
        "test_pass": None,
        "coverage": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_parse_evidence_uses_cli_overrides() -> None:
    args = _make_args(
        pr_paths="src/popolaloom/foo.py",
        devolaflow_composite=0.9,
        nines_current=0.92,
        nines_prior=0.85,
        blocker_count=0,
        test_pass=True,
        coverage=92.0,
    )
    evidence = parse_evidence(args)
    assert evidence["devolaflow_composite"] == 0.9
    assert evidence["nines_current"] == 0.92
    assert evidence["nines_prior"] == 0.85
    assert evidence["blocker_count"] == 0
    assert evidence["test_pass"] is True
    assert evidence["coverage"] == 92.0
    assert evidence["pr_paths"] == "src/popolaloom/foo.py"


def test_parse_evidence_loads_from_json_file(tmp_path: Path) -> None:
    p = tmp_path / "evidence.json"
    p.write_text(json.dumps({"devolaflow_composite": 0.9}), encoding="utf-8")
    args = _make_args(evidence=str(p))
    evidence = parse_evidence(args)
    assert evidence["devolaflow_composite"] == 0.9


def test_parse_evidence_cli_overrides_json_file(tmp_path: Path) -> None:
    p = tmp_path / "evidence.json"
    p.write_text(json.dumps({"devolaflow_composite": 0.7}), encoding="utf-8")
    args = _make_args(evidence=str(p), devolaflow_composite=0.95)
    evidence = parse_evidence(args)
    assert evidence["devolaflow_composite"] == 0.95


def test_parse_evidence_missing_file_raises(tmp_path: Path) -> None:
    args = _make_args(evidence=str(tmp_path / "nonexistent.json"))
    with pytest.raises(FileNotFoundError):
        parse_evidence(args)


def test_parse_evidence_non_dict_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "evidence.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    args = _make_args(evidence=str(p))
    with pytest.raises(ValueError, match="JSON object"):
        parse_evidence(args)


def test_evaluate_automerge_test_pass_with_coverage_zero() -> None:
    """test_pass=true but coverage missing causes coverage check fail."""
    config = AutomergeConfig(
        gate_thresholds=GateThresholds(coverage_min=90.0),
        required_paths=PathPolicy(allowed=["**"], blocked=[]),
    )
    evidence = {
        "devolaflow_composite": 0.9,
        "nines_current": 0.92,
        "nines_prior": 0.85,
        "blocker_count": 0,
        "test_pass": True,
        "coverage": "garbage",
        "pr_paths": ["x.py"],
    }
    result = evaluate_automerge(config, evidence)
    fail_names = [c.name for c in result.failures()]
    assert "test_pass" in fail_names


def test_evaluate_automerge_devolaflow_non_numeric() -> None:
    """Non-numeric devolaflow_composite → fail with explanation."""
    config = AutomergeConfig()
    evidence = {
        "devolaflow_composite": "garbage",
        "nines_current": 0.9,
        "nines_prior": 0.85,
        "blocker_count": 0,
        "test_pass": True,
        "coverage": 95.0,
        "pr_paths": ["x.py"],
    }
    result = evaluate_automerge(config, evidence)
    fail_names = [c.name for c in result.failures()]
    assert "devolaflow_composite" in fail_names


def test_evaluate_automerge_nines_non_numeric() -> None:
    """Non-numeric nines_current or nines_prior → fail."""
    config = AutomergeConfig()
    evidence = {
        "devolaflow_composite": 0.9,
        "nines_current": "garbage",
        "nines_prior": 0.85,
        "blocker_count": 0,
        "test_pass": True,
        "coverage": 95.0,
        "pr_paths": ["x.py"],
    }
    result = evaluate_automerge(config, evidence)
    fail_names = [c.name for c in result.failures()]
    assert "nines_delta" in fail_names


def test_evaluate_automerge_blocker_non_int() -> None:
    """blocker_count non-int → explicit fail."""
    config = AutomergeConfig()
    evidence = {
        "devolaflow_composite": 0.9,
        "nines_current": 0.92,
        "nines_prior": 0.85,
        "blocker_count": "garbage",
        "test_pass": True,
        "coverage": 95.0,
        "pr_paths": ["x.py"],
    }
    result = evaluate_automerge(config, evidence)
    fail_names = [c.name for c in result.failures()]
    assert "blocker_max" in fail_names


def test_evaluate_automerge_paths_no_allowed_globs() -> None:
    """Empty allowed list with paths → fail (deliberately strict)."""
    config = AutomergeConfig(
        gate_thresholds=GateThresholds(),
        required_paths=PathPolicy(allowed=[], blocked=[]),
    )
    evidence = {
        "devolaflow_composite": 0.9,
        "nines_current": 0.92,
        "nines_prior": 0.85,
        "blocker_count": 0,
        "test_pass": True,
        "coverage": 95.0,
        "pr_paths": ["src/foo.py"],
    }
    result = evaluate_automerge(config, evidence)
    fail_names = [c.name for c in result.failures()]
    assert "paths" in fail_names


def test_path_policy_rejects_blank_globs() -> None:
    """Blank glob entries trigger validator (No Silent Failures)."""
    with pytest.raises(Exception, match="non-empty string"):
        PathPolicy(allowed=["src/foo.py", "  "])


def test_gate_thresholds_defaults() -> None:
    """Defaults match v0.3.0-plan §4 Stage F3 schema."""
    t = GateThresholds()
    assert t.devolaflow_composite == 0.85
    assert t.nines_delta == 0.02
    assert t.blocker_max == 0
    assert t.test_pass is True
    assert t.coverage_min == 90.0
