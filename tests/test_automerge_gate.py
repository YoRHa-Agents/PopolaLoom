"""F3 auto-merge gate tests — 5 AND conditions (v0.3.0 Stage F3).

Per v0.3.0-plan §4 Stage F3 + roadmap §4.2 Stage F3.

≥ 6 cases as required by AC #1 of the v0.3.0 task spec:

1. happy path: all 5 conditions PASS → verdict="pass"
2. devolaflow_composite below threshold → "fail"
3. nines_delta below threshold (regression) → "fail"
4. blocker_count > 0 → "fail"
5. test_pass=false → "fail"
6. paths blocked (pyproject.toml in changed files) → "fail"
7. paths not in allowed list → "fail"
8. gate self-test: PR touching src/popolaloom/gate/** is blocked
9. evidence missing required fields → "fail"
10. config loader rejects malformed YAML / unknown keys
11. CLI module entry: PASS exits 0, FAIL exits 2

Workspace rule "Mandatory Verification": each new module under
src/popolaloom/gate/ has corresponding tests here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from popolaloom.gate import (
    AutomergeConfig,
    AutomergeResult,
    GateThresholds,
    PathPolicy,
    evaluate_automerge,
    load_config,
)
from popolaloom.gate.automerge import main as gate_main


def _default_config() -> AutomergeConfig:
    """Return an AutomergeConfig matching .workflow/automerge.yaml defaults."""
    return AutomergeConfig(
        gate_thresholds=GateThresholds(),
        required_paths=PathPolicy(
            allowed=[
                "src/popolaloom/**/*.py",
                "tests/**/*.py",
                "*.md",
            ],
            blocked=[
                "pyproject.toml",
                ".github/workflows/automerge.yml",
                "src/popolaloom/gate/**",
            ],
        ),
    )


def _passing_evidence() -> dict[str, object]:
    """Evidence dict that satisfies all 5 AND conditions."""
    return {
        "devolaflow_composite": 0.90,
        "nines_current": 0.92,
        "nines_prior": 0.85,
        "blocker_count": 0,
        "test_pass": True,
        "coverage": 92.5,
        "pr_paths": ["src/popolaloom/foo.py", "tests/test_foo.py"],
    }


# ── Case 1: happy path ──────────────────────────────────────────────────


def test_happy_path_all_five_conditions_pass() -> None:
    """All 5 conditions met → verdict='pass', reason mentions all 5."""
    config = _default_config()
    result = evaluate_automerge(config, _passing_evidence())
    assert result.verdict == "pass", result.model_dump()
    assert "all 5 conditions pass" in result.reason
    assert len(result.conditions) == 5
    assert all(c.verdict == "pass" for c in result.conditions)


# ── Case 2: devolaflow composite below threshold ────────────────────────


def test_fail_when_devolaflow_composite_below_threshold() -> None:
    """devolaflow_composite < 0.85 → fail with explicit reason."""
    config = _default_config()
    evidence = _passing_evidence()
    evidence["devolaflow_composite"] = 0.83
    result = evaluate_automerge(config, evidence)
    assert result.verdict == "fail"
    assert result.failures()[0].name == "devolaflow_composite"
    assert "0.8300" in result.failures()[0].observed
    assert "below threshold" in result.failures()[0].reason


# ── Case 3: nines_delta below threshold (no improvement) ────────────────


def test_fail_when_nines_did_not_improve_enough() -> None:
    """current - prior < 0.02 → fail."""
    config = _default_config()
    evidence = _passing_evidence()
    evidence["nines_prior"] = 0.91
    evidence["nines_current"] = 0.92
    result = evaluate_automerge(config, evidence)
    assert result.verdict == "fail"
    failed = {c.name for c in result.failures()}
    assert "nines_delta" in failed


def test_fail_when_nines_regressed() -> None:
    """current < prior → strictly fail."""
    config = _default_config()
    evidence = _passing_evidence()
    evidence["nines_current"] = 0.80
    evidence["nines_prior"] = 0.85
    result = evaluate_automerge(config, evidence)
    assert result.verdict == "fail"
    nines_row = next(c for c in result.conditions if c.name == "nines_delta")
    assert nines_row.verdict == "fail"


# ── Case 4: blocker_count > 0 ───────────────────────────────────────────


def test_fail_when_blocker_findings_present() -> None:
    """Any blocker finding → immediate fail."""
    config = _default_config()
    evidence = _passing_evidence()
    evidence["blocker_count"] = 1
    result = evaluate_automerge(config, evidence)
    assert result.verdict == "fail"
    blocker_row = next(c for c in result.conditions if c.name == "blocker_max")
    assert blocker_row.verdict == "fail"
    assert "1 blocker" in blocker_row.reason


# ── Case 5: tests not green / coverage below floor ──────────────────────


def test_fail_when_tests_not_green() -> None:
    """test_pass=false → fail with reason mentioning tests."""
    config = _default_config()
    evidence = _passing_evidence()
    evidence["test_pass"] = False
    result = evaluate_automerge(config, evidence)
    assert result.verdict == "fail"
    test_row = next(c for c in result.conditions if c.name == "test_pass")
    assert test_row.verdict == "fail"
    assert "tests not green" in test_row.reason


def test_fail_when_coverage_below_floor() -> None:
    """coverage < coverage_min (90) → fail."""
    config = _default_config()
    evidence = _passing_evidence()
    evidence["coverage"] = 88.0
    result = evaluate_automerge(config, evidence)
    assert result.verdict == "fail"
    test_row = next(c for c in result.conditions if c.name == "test_pass")
    assert test_row.verdict == "fail"
    assert "coverage 88.00" in test_row.reason


# ── Case 6: paths in blocked list ──────────────────────────────────────


def test_fail_when_pr_modifies_blocked_path() -> None:
    """pyproject.toml in changed files → blocked → fail."""
    config = _default_config()
    evidence = _passing_evidence()
    evidence["pr_paths"] = ["src/popolaloom/foo.py", "pyproject.toml"]
    result = evaluate_automerge(config, evidence)
    assert result.verdict == "fail"
    paths_row = next(c for c in result.conditions if c.name == "paths")
    assert paths_row.verdict == "fail"
    assert "pyproject.toml" in paths_row.reason


# ── Case 7: paths not in allowed list ──────────────────────────────────


def test_fail_when_pr_includes_path_not_in_allowed() -> None:
    """A PR file outside the allowed globs → fail."""
    config = _default_config()
    evidence = _passing_evidence()
    evidence["pr_paths"] = ["src/popolaloom/foo.py", "/etc/secret.conf"]
    result = evaluate_automerge(config, evidence)
    assert result.verdict == "fail"
    paths_row = next(c for c in result.conditions if c.name == "paths")
    assert paths_row.verdict == "fail"


# ── Case 8: gate self-test (gate code change requires human review) ────


def test_gate_self_test_blocks_changes_to_gate_code() -> None:
    """PR touching src/popolaloom/gate/** must be blocked.

    This is the F3.4 self-test rule (v0.3.0-plan §4 Stage F3): without
    it, a buggy gate could merge a change that disables itself
    (R-EVO-5 mitigation).
    """
    config = _default_config()
    evidence = _passing_evidence()
    evidence["pr_paths"] = ["src/popolaloom/gate/automerge.py"]
    result = evaluate_automerge(config, evidence)
    assert result.verdict == "fail"
    paths_row = next(c for c in result.conditions if c.name == "paths")
    assert paths_row.verdict == "fail"
    assert "blocked" in paths_row.reason.lower() or "gate" in paths_row.reason.lower()


# ── Case 9: missing evidence fields surface as failure (No Silent Failures) ──


def test_missing_devolaflow_composite_fails_explicitly() -> None:
    """No evidence.devolaflow_composite → explicit fail, not silent skip."""
    config = _default_config()
    evidence = _passing_evidence()
    del evidence["devolaflow_composite"]
    result = evaluate_automerge(config, evidence)
    assert result.verdict == "fail"
    failed = next(c for c in result.failures() if c.name == "devolaflow_composite")
    assert "required" in failed.reason


def test_missing_nines_prior_fails_explicitly() -> None:
    """nines_prior missing → fail (cannot compute delta)."""
    config = _default_config()
    evidence = _passing_evidence()
    del evidence["nines_prior"]
    result = evaluate_automerge(config, evidence)
    assert result.verdict == "fail"


def test_missing_pr_paths_fails_explicitly() -> None:
    """pr_paths empty → fail (gate cannot decide on no paths)."""
    config = _default_config()
    evidence = _passing_evidence()
    evidence["pr_paths"] = []
    result = evaluate_automerge(config, evidence)
    assert result.verdict == "fail"
    paths_row = next(c for c in result.conditions if c.name == "paths")
    assert paths_row.verdict == "fail"


# ── Case 10: config loader ─────────────────────────────────────────────


def test_load_config_from_yaml_file(tmp_path: Path) -> None:
    """Round-trip load from YAML; defaults preserved."""
    config_path = tmp_path / "automerge.yaml"
    config_path.write_text(
        """
gate_thresholds:
  devolaflow_composite: 0.90
  nines_delta: 0.05
  blocker_max: 0
  test_pass: true
  coverage_min: 95.0
required_paths:
  allowed:
    - "src/popolaloom/**/*.py"
  blocked:
    - "pyproject.toml"
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.gate_thresholds.devolaflow_composite == 0.90
    assert config.gate_thresholds.coverage_min == 95.0
    assert "src/popolaloom/**/*.py" in config.required_paths.allowed


def test_load_config_rejects_unknown_keys(tmp_path: Path) -> None:
    """Pydantic extra=forbid: unknown keys must raise."""
    config_path = tmp_path / "bad.yaml"
    config_path.write_text(
        "gate_thresholds:\n  unknown_field: 1\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="validation failed|extra"):
        load_config(config_path)


def test_load_config_missing_file() -> None:
    """File not found → FileNotFoundError, not silent default."""
    with pytest.raises(FileNotFoundError):
        load_config(Path("/nonexistent/automerge.yaml"))


def test_repo_workflow_automerge_yaml_loads_cleanly() -> None:
    """The shipped .workflow/automerge.yaml must validate."""
    repo_root = Path(__file__).resolve().parent.parent
    config_path = repo_root / ".workflow" / "automerge.yaml"
    assert config_path.is_file(), f"missing repo config: {config_path}"
    config = load_config(config_path)
    assert config.gate_thresholds.devolaflow_composite == 0.85
    assert config.gate_thresholds.nines_delta == 0.02
    assert config.gate_thresholds.coverage_min == 90.0
    ## Ensure self-test rule is wired: gate paths blocked.
    assert any("gate" in g for g in config.required_paths.blocked)
    assert "pyproject.toml" in config.required_paths.blocked


# ── Case 11: CLI module entry exit codes ───────────────────────────────


def test_cli_main_pass_exits_zero(tmp_path: Path) -> None:
    """python -m popolaloom.gate.automerge with PASS evidence → exit 0."""
    config_path = tmp_path / "automerge.yaml"
    config_path.write_text(
        """
gate_thresholds:
  devolaflow_composite: 0.85
  nines_delta: 0.02
  blocker_max: 0
  test_pass: true
  coverage_min: 90.0
required_paths:
  allowed:
    - "src/popolaloom/**/*.py"
    - "tests/**/*.py"
  blocked: []
""",
        encoding="utf-8",
    )
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(_passing_evidence()), encoding="utf-8")
    output_path = tmp_path / "result.json"

    rc = gate_main([
        "--config", str(config_path),
        "--evidence", str(evidence_path),
        "--output", str(output_path),
    ])
    assert rc == 0
    payload = json.loads(output_path.read_text())
    assert payload["verdict"] == "pass"


def test_cli_main_fail_exits_two(tmp_path: Path) -> None:
    """python -m popolaloom.gate.automerge with FAIL evidence → exit 2."""
    config_path = tmp_path / "automerge.yaml"
    config_path.write_text(
        """
gate_thresholds:
  devolaflow_composite: 0.85
  nines_delta: 0.02
  blocker_max: 0
  test_pass: true
  coverage_min: 90.0
required_paths:
  allowed:
    - "src/popolaloom/**/*.py"
  blocked: []
""",
        encoding="utf-8",
    )
    evidence = _passing_evidence()
    evidence["test_pass"] = False
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    rc = gate_main([
        "--config", str(config_path),
        "--evidence", str(evidence_path),
    ])
    assert rc == 2


def test_cli_main_missing_config_exits_one() -> None:
    """Missing --config file → exit 1 (error vs gate fail)."""
    rc = gate_main([
        "--config", "/nonexistent/automerge.yaml",
        "--devolaflow-composite", "0.9",
        "--nines-current", "0.9",
        "--nines-prior", "0.85",
        "--test-pass", "true",
        "--coverage", "95.0",
        "--pr-paths", "src/popolaloom/foo.py",
    ])
    assert rc == 1


def test_cli_invocation_via_subprocess_module_entry(tmp_path: Path) -> None:
    """Verify ``python -m popolaloom.gate.automerge`` runs as a module."""
    config_path = tmp_path / "automerge.yaml"
    config_path.write_text(
        """
gate_thresholds:
  devolaflow_composite: 0.85
  nines_delta: 0.02
  blocker_max: 0
  test_pass: true
  coverage_min: 90.0
required_paths:
  allowed: ["src/popolaloom/**/*.py", "tests/**/*.py"]
  blocked: []
""",
        encoding="utf-8",
    )
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(_passing_evidence()), encoding="utf-8")
    cmd = [
        sys.executable, "-m", "popolaloom.gate.automerge",
        "--config", str(config_path),
        "--evidence", str(evidence_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    payload = json.loads(result.stdout)
    assert payload["verdict"] == "pass"


# ── Bonus: AutomergeResult shape contract ───────────────────────────────


def test_automerge_result_has_all_five_condition_rows() -> None:
    """Every result MUST list all 5 conditions (no silent skips)."""
    config = _default_config()
    result = evaluate_automerge(config, {})
    assert isinstance(result, AutomergeResult)
    assert len(result.conditions) == 5
    names = {c.name for c in result.conditions}
    assert names == {
        "devolaflow_composite",
        "nines_delta",
        "blocker_max",
        "test_pass",
        "paths",
    }


def test_pr_paths_accepts_comma_separated_string() -> None:
    """CLI/CI may pass --pr-paths as a single comma-separated string."""
    config = _default_config()
    evidence = _passing_evidence()
    evidence["pr_paths"] = "src/popolaloom/foo.py,tests/test_foo.py"
    result = evaluate_automerge(config, evidence)
    assert result.verdict == "pass"


def test_blocker_count_default_zero_when_missing() -> None:
    """blocker_count omitted → defaults to 0 (not a fail)."""
    config = _default_config()
    evidence = _passing_evidence()
    del evidence["blocker_count"]
    result = evaluate_automerge(config, evidence)
    blocker_row = next(c for c in result.conditions if c.name == "blocker_max")
    assert blocker_row.verdict == "pass"
