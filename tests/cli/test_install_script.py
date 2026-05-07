"""Default-lane subprocess tests for ``install.sh`` (v0.8.4 unified installer).

Per the v0.8.4 acceptance contract — covers the bash bootstrapper's
CLI surface (``--help`` / ``version``), each verb's dry-run output
(``install`` / ``update`` / ``uninstall``), and the validation error
paths (unknown verb / invalid target / invalid scope).

Each test runs the script via :func:`subprocess.run` with
``cwd=tmp_path`` so the script never pollutes the repo.  Every test
uses ``--dry-run`` to keep the I/O footprint at zero — the script is
expected to print the would-be commands and return 0 (or non-zero
for explicit error paths) without invoking pip / popola.

Skipped on Windows since bash is required; the project as a whole
targets unix-like hosts.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

INSTALL_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "install.sh"

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="install.sh requires bash (unix-only)",
)


def _run(
    args: list[str],
    *,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    """Run ``install.sh`` with ``args`` from ``cwd``; capture text output."""
    cmd = [str(INSTALL_SCRIPT_PATH), *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_install_script_exists_and_executable() -> None:
    """``install.sh`` lives at the repo root and is executable."""
    assert INSTALL_SCRIPT_PATH.is_file(), (
        f"expected install.sh at {INSTALL_SCRIPT_PATH}"
    )
    import os

    assert os.access(INSTALL_SCRIPT_PATH, os.X_OK), (
        f"install.sh at {INSTALL_SCRIPT_PATH} is not executable"
    )


def test_install_script_help_returns_zero(tmp_path: Path) -> None:
    """``install.sh --help`` exits 0 and prints the verb / option matrix."""
    result = _run(["--help"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "install" in out
    assert "update" in out
    assert "uninstall" in out
    assert "--scope" in out
    assert "--target" in out
    assert "--dry-run" in out


def test_install_script_version_returns_zero(tmp_path: Path) -> None:
    """``install.sh version`` exits 0 and prints the script version."""
    result = _run(["version"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "install.sh" in result.stdout
    assert "0.8.4" in result.stdout


def test_install_script_unknown_verb_returns_nonzero_with_message(tmp_path: Path) -> None:
    """An unknown verb exits non-zero with a helpful message on stderr."""
    result = _run(["bogusverb", "--dry-run"], cwd=tmp_path)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "unknown verb" in combined.lower() or "expected" in combined.lower()


def test_install_script_install_dry_run_prints_pip_command(tmp_path: Path) -> None:
    """``install --dry-run`` prints the would-be ``pip install popolaloom`` command."""
    result = _run(
        ["install", "--dry-run", "--no-daemon", "--no-skills"],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "pip install" in out
    assert "popolaloom" in out


def test_install_script_install_dry_run_with_version_pin(tmp_path: Path) -> None:
    """``install --dry-run --version=X.Y.Z`` includes the pin in the pip command."""
    result = _run(
        [
            "install",
            "--dry-run",
            "--no-daemon",
            "--no-skills",
            "--version=0.8.4",
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "popolaloom==0.8.4" in out


def test_install_script_install_dry_run_with_git_source(tmp_path: Path) -> None:
    """``install --from=git --dry-run`` resolves to the GitHub URL."""
    result = _run(
        [
            "install",
            "--dry-run",
            "--no-daemon",
            "--no-skills",
            "--from=git",
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "git+https://github.com/YoRHa-Agents/PopolaLoom.git" in out


def test_install_script_update_dry_run_prints_upgrade_command(tmp_path: Path) -> None:
    """``update --dry-run`` prints the would-be ``pip install --upgrade`` command."""
    result = _run(
        ["update", "--dry-run", "--no-skills"],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "pip install --upgrade" in out
    assert "popolaloom" in out


def test_install_script_uninstall_dry_run_prints_pip_uninstall(tmp_path: Path) -> None:
    """``uninstall --dry-run --yes`` prints the would-be ``pip uninstall`` command."""
    result = _run(
        ["uninstall", "--dry-run", "--yes", "--no-skills"],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "pip uninstall" in out
    assert "popolaloom" in out


def test_install_script_invalid_target_returns_nonzero(tmp_path: Path) -> None:
    """``install --target=bogus`` exits non-zero with a helpful message."""
    result = _run(["install", "--target=bogus", "--dry-run"], cwd=tmp_path)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "target" in combined.lower()
    assert "cursor" in combined.lower() or "expected" in combined.lower()


def test_install_script_invalid_scope_returns_nonzero(tmp_path: Path) -> None:
    """``install --scope=bogus`` exits non-zero with a helpful message."""
    result = _run(["install", "--scope=bogus", "--dry-run"], cwd=tmp_path)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "scope" in combined.lower()
    assert "global" in combined.lower() or "expected" in combined.lower()


def test_install_script_pin_version_outside_pypi_errors(tmp_path: Path) -> None:
    """``--version=X.Y.Z`` requires ``--from=pypi``; other sources error out."""
    result = _run(
        [
            "install",
            "--dry-run",
            "--from=git",
            "--version=0.8.4",
        ],
        cwd=tmp_path,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "version" in combined.lower()
    assert "pypi" in combined.lower()


def test_install_script_install_dry_run_default_verb_no_args(tmp_path: Path) -> None:
    """When no verb is supplied, ``install`` is the default."""
    result = _run(
        ["--dry-run", "--no-daemon", "--no-skills"],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "verb=install" in result.stdout
