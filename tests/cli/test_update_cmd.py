"""Default-lane Typer tests for ``popola update`` (v1.4.0).

Exercises the verb end-to-end via :class:`typer.testing.CliRunner` with
the pip subprocess + install-kind detector mocked out.  Covers:

* Happy path — ``popola update --dry-run --json`` emits a JSON envelope
  with all expected keys.
* Flag validation — bad ``--target`` / ``--scope`` / ``--from`` /
  ``--ref``+``--from=pypi`` / ``--version``+``--from=git`` exit non-zero.
* Unsafe install refusal — ``editable`` / ``pipx`` exits ``2`` with a
  remediation hint on stderr.
* pip subprocess failure — exits ``1`` with the captured stderr tail.
* ``--no-skills`` / ``--no-doctor`` short-circuits.
* Quiet mode — only warnings reach stderr.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from popolaloom.cli.update_cmd import app
from popolaloom.evolution import self_update
from popolaloom.evolution.self_update import (
    InstallKind,
    _InstallProbe,
)


@pytest.fixture
def runner() -> CliRunner:
    """Typer CliRunner — modern Typer/click separates stdout / stderr by default."""
    return CliRunner()


@pytest.fixture
def mock_regular_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force :func:`detect_install_kind` to return REGULAR — the only safe kind."""
    monkeypatch.setattr(
        self_update,
        "detect_install_kind",
        lambda: _InstallProbe(
            kind=InstallKind.REGULAR,
            location=Path("/fake/site-packages"),
            editable_project_location=None,
            notes=[],
        ),
    )


@pytest.fixture
def mock_pip_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub :func:`subprocess.run` so pip never actually runs and reports exit 0."""
    monkeypatch.setattr(
        "subprocess.run",
        mock.MagicMock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"installed\n", stderr=b""
            )
        ),
    )


def test_update_dry_run_json_emits_full_envelope(
    runner: CliRunner,
    mock_regular_install: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``popola update --dry-run --json`` emits the full outcome envelope."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("POPOLA_HOME", raising=False)

    result = runner.invoke(app, ["--dry-run", "--json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["install_kind"] == "regular"
    assert payload["spec"].startswith("git+")
    assert payload["pip"]["dry_run"] is True
    assert payload["config"]["dry_run"] is True
    assert payload["config"]["scope"] == "both"
    assert payload["config"]["target"] == "all"
    assert isinstance(payload["skills"], list)
    assert payload["doctor"] == []  # dry-run skips doctor


def test_update_invalid_target_exits_non_zero(runner: CliRunner) -> None:
    """``--target=garbage`` rejected by Typer BadParameter (exit 2)."""
    result = runner.invoke(app, ["--target", "garbage", "--dry-run"])
    assert result.exit_code != 0
    combined = (result.stdout + result.stderr).lower()
    assert "garbage" in combined or "target" in combined


def test_update_invalid_scope_exits_non_zero(runner: CliRunner) -> None:
    """``--scope=quad`` is rejected (only global / project / both accepted)."""
    result = runner.invoke(app, ["--scope", "quad", "--dry-run"])
    assert result.exit_code != 0
    combined = (result.stdout + result.stderr).lower()
    assert "scope" in combined


def test_update_ref_with_pypi_rejected(runner: CliRunner) -> None:
    """``--ref=v1.4.0 --from=pypi`` rejected (ref only valid with --from=git)."""
    result = runner.invoke(
        app, ["--from", "pypi", "--ref", "v1.4.0", "--dry-run"]
    )
    assert result.exit_code != 0
    combined = result.stdout + result.stderr
    assert "ref" in combined.lower()


def test_update_version_with_git_rejected(runner: CliRunner) -> None:
    """``--version=1.4.0 --from=git`` rejected (version only valid with --from=pypi)."""
    result = runner.invoke(
        app, ["--from", "git", "--version", "1.4.0", "--dry-run"]
    )
    assert result.exit_code != 0
    combined = result.stdout + result.stderr
    assert "version" in combined.lower()


def _flatten(text: str) -> str:
    """Collapse Rich line wraps so substring assertions don't hinge on terminal width.

    Rich wraps stderr at the runner's terminal width, which differs between
    local dev (≥120 cols) and GitHub Actions (~80 cols). The hint strings
    end up split across lines like ``pipx\\nupgrade`` on narrow runners,
    failing literal substring asserts. Collapsing newlines + whitespace
    runs makes the assertions width-agnostic.
    """
    return " ".join(text.split())


def test_update_editable_refuses_with_exit_2(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """EDITABLE install refusal → exit 2 + remediation hint on stderr."""
    monkeypatch.setattr(
        self_update,
        "detect_install_kind",
        lambda: _InstallProbe(
            kind=InstallKind.EDITABLE,
            location=Path("/repo/src/popolaloom"),
            editable_project_location=Path("/repo"),
            notes=[],
        ),
    )
    result = runner.invoke(app, ["--dry-run"])
    assert result.exit_code == 2
    flat = _flatten(result.stderr)
    assert "editable" in flat.lower()
    assert "git pull" in flat or "popola skill upgrade" in flat


def test_update_pipx_refuses_with_exit_2(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PIPX install refusal → exit 2 + ``pipx upgrade`` hint."""
    monkeypatch.setattr(
        self_update,
        "detect_install_kind",
        lambda: _InstallProbe(
            kind=InstallKind.PIPX,
            location=Path("/root/.local/pipx/venvs/popolaloom/lib/python3.12"),
            editable_project_location=None,
            notes=[],
        ),
    )
    result = runner.invoke(app, ["--dry-run"])
    assert result.exit_code == 2
    flat = _flatten(result.stderr)
    assert "pipx" in flat.lower()
    assert "pipx upgrade" in flat


def test_update_pip_failure_exits_1_with_stderr_tail(
    runner: CliRunner,
    mock_regular_install: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pip subprocess non-zero → exit 1 + stderr tail rendered."""
    proc = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout=b"",
        stderr=b"ERROR: No matching distribution found for popolaloom==99.99.99\n",
    )
    monkeypatch.setattr("subprocess.run", mock.MagicMock(return_value=proc))

    result = runner.invoke(
        app,
        ["--from", "pypi", "--version", "99.99.99", "--no-skills", "--no-doctor"],
    )
    assert result.exit_code == 1
    flat = _flatten(result.stderr)
    assert "pip install --upgrade failed" in flat
    assert "No matching distribution" in flat


def test_update_no_skills_skips_skill_phase_in_json(
    runner: CliRunner,
    mock_regular_install: None,
    mock_pip_success: None,
) -> None:
    """``--no-skills`` produces an empty ``skills`` array."""
    result = runner.invoke(
        app, ["--no-skills", "--no-doctor", "--json"]
    )
    assert result.exit_code in (0, 3), result.stderr
    payload = json.loads(result.stdout)
    assert payload["skills"] == []
    assert payload["config"]["no_skills"] is True


def test_update_quiet_mode_no_table_only_warnings(
    runner: CliRunner,
    mock_regular_install: None,
    mock_pip_success: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``--quiet`` suppresses the Rich table; warnings still go to stderr."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.delenv("POPOLA_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)

    result = runner.invoke(
        app, ["--quiet", "--no-skills", "--no-doctor", "--dry-run"]
    )
    assert result.exit_code == 0
    # stdout should NOT contain the rich table headers.
    assert "step 1" not in result.stdout
    assert "step 2" not in result.stdout


def test_update_default_invocation_runs_skill_upgrades(
    runner: CliRunner,
    mock_regular_install: None,
    mock_pip_success: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No-flag invocation runs pip + skill upgrade for every (target, scope)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("POPOLA_HOME", raising=False)

    result = runner.invoke(app, ["--no-doctor", "--json"])
    payload = json.loads(result.stdout)
    # Skill outcomes: cursor (global+project) + claude (global+project) +
    # codex (global) + copilot (project) = 6 entries.
    assert len(payload["skills"]) == 6
    assert {s["target"] for s in payload["skills"]} == {
        "cursor",
        "claude",
        "codex",
        "copilot",
    }


def test_update_force_overrides_editable_refusal(
    runner: CliRunner,
    mock_pip_success: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--force`` lets the verb run on editable installs (escape hatch)."""
    monkeypatch.setattr(
        self_update,
        "detect_install_kind",
        lambda: _InstallProbe(
            kind=InstallKind.EDITABLE,
            location=Path("/repo/src/popolaloom"),
            editable_project_location=Path("/repo"),
            notes=[],
        ),
    )
    result = runner.invoke(
        app,
        ["--force", "--dry-run", "--json", "--no-doctor"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["install_kind"] == "editable"
