"""Default-lane subprocess tests for repo-root ``cloud-quickstart.sh``.

Covers the v0.9.0 GA acceptance criteria for the new copy-paste-ready
Cloud Agent quickstart script:

- (w) the script lives at the repo root (executable on POSIX);
- (x) the header comments include shebang + license + last-updated +
  usage;
- (y) steps 1..5 (init / dispatch / attach / cloud runs) are referenced;
- (z) defensive: exits non-zero with helpful messages when
  ``CURSOR_API_KEY`` is missing OR ``popola`` is not on PATH;
- (aa) idempotent and safe to re-run (validated via ``--no-init`` +
  ``--dry-run``);
- (bb) ``bash -n`` syntax check passes; the required marker strings
  ``popola dispatch --cli=cursor-cloud`` and ``CURSOR_API_KEY`` are
  literally present so a future edit cannot silently drop the cloud
  entrypoint.

Skipped on Windows since the script is bash-only.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLOUD_QUICKSTART_PATH = REPO_ROOT / "cloud-quickstart.sh"


pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="cloud-quickstart.sh requires bash (unix-only)",
)


def _bash_bin() -> str:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not on PATH")
    return bash


def test_cloud_quickstart_exists_at_repo_root() -> None:
    """``cloud-quickstart.sh`` ships at the repo root and is a regular file (acceptance w)."""
    assert CLOUD_QUICKSTART_PATH.is_file(), (
        f"expected cloud-quickstart.sh at {CLOUD_QUICKSTART_PATH} (repo root)"
    )
    assert CLOUD_QUICKSTART_PATH.stat().st_size > 0, (
        "cloud-quickstart.sh must not be empty"
    )


def test_cloud_quickstart_is_executable() -> None:
    """``cloud-quickstart.sh`` is executable on POSIX (acceptance w)."""
    import os

    assert os.access(CLOUD_QUICKSTART_PATH, os.X_OK), (
        f"cloud-quickstart.sh at {CLOUD_QUICKSTART_PATH} is not executable; "
        "run `chmod +x cloud-quickstart.sh`"
    )


def test_cloud_quickstart_has_bash_shebang() -> None:
    """First line is a bash shebang (acceptance x)."""
    first_line = CLOUD_QUICKSTART_PATH.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#!"), (
        f"cloud-quickstart.sh first line {first_line!r} is not a shebang"
    )
    assert "bash" in first_line, (
        f"cloud-quickstart.sh shebang {first_line!r} must invoke bash "
        "(e.g. '#!/usr/bin/env bash')"
    )


def test_cloud_quickstart_header_has_license_and_last_updated() -> None:
    """Header comments include license + last-updated + usage (acceptance x)."""
    text = CLOUD_QUICKSTART_PATH.read_text(encoding="utf-8")
    assert "License:" in text or "license" in text.lower(), (
        "cloud-quickstart.sh header must declare a license"
    )
    assert (
        "Last updated:" in text
        or "last_updated" in text.lower()
        or "updated:" in text.lower()
    ), "cloud-quickstart.sh header must include a last-updated marker"
    assert "Usage:" in text or "usage:" in text.lower(), (
        "cloud-quickstart.sh header must include a Usage block"
    )


def test_cloud_quickstart_uses_strict_mode() -> None:
    """Script enables bash strict mode per the task brief hint."""
    text = CLOUD_QUICKSTART_PATH.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text, (
        "cloud-quickstart.sh must use bash strict mode (`set -euo pipefail`)"
    )


def test_cloud_quickstart_syntax_check_passes() -> None:
    """``bash -n cloud-quickstart.sh`` parses cleanly (acceptance bb)."""
    bash = _bash_bin()
    result = subprocess.run(
        [bash, "-n", str(CLOUD_QUICKSTART_PATH)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"bash -n cloud-quickstart.sh failed with stderr={result.stderr!r}"
    )
    assert result.stderr == "", (
        f"bash -n cloud-quickstart.sh produced unexpected stderr: {result.stderr!r}"
    )


def test_cloud_quickstart_mentions_cloud_dispatch_marker() -> None:
    """Script literally mentions ``popola dispatch --cli=cursor-cloud`` (acceptance bb)."""
    text = CLOUD_QUICKSTART_PATH.read_text(encoding="utf-8")
    assert "popola dispatch --cli=cursor-cloud" in text, (
        "cloud-quickstart.sh must mention the cloud dispatch entrypoint "
        "literally so a future edit cannot silently drop the cloud path"
    )


def test_cloud_quickstart_mentions_cursor_api_key_marker() -> None:
    """Script literally mentions ``CURSOR_API_KEY`` (acceptance bb + z)."""
    text = CLOUD_QUICKSTART_PATH.read_text(encoding="utf-8")
    assert "CURSOR_API_KEY" in text, (
        "cloud-quickstart.sh must mention CURSOR_API_KEY literally so the "
        "pre-flight env-var check is enforceable"
    )


def test_cloud_quickstart_mentions_popola_attach_step() -> None:
    """Script references ``popola attach`` step (acceptance y, step 4)."""
    text = CLOUD_QUICKSTART_PATH.read_text(encoding="utf-8")
    assert "popola attach" in text, (
        "cloud-quickstart.sh must reference 'popola attach' (step 4 in the brief)"
    )


def test_cloud_quickstart_mentions_popola_cloud_runs_step() -> None:
    """Script references ``popola cloud runs`` step (acceptance y, step 5)."""
    text = CLOUD_QUICKSTART_PATH.read_text(encoding="utf-8")
    assert "popola cloud runs" in text, (
        "cloud-quickstart.sh must reference 'popola cloud runs' (step 5 in the brief)"
    )


def test_cloud_quickstart_mentions_init_cloud_only_step() -> None:
    """Script references ``popola init --target=cloud-only`` step (acceptance y, step 1)."""
    text = CLOUD_QUICKSTART_PATH.read_text(encoding="utf-8")
    assert "popola init --target=cloud-only" in text, (
        "cloud-quickstart.sh must reference 'popola init --target=cloud-only' "
        "(step 1 in the brief — Q-D-4 偏离默认 scaffold)"
    )


def test_cloud_quickstart_help_exits_zero(tmp_path: Path) -> None:
    """``cloud-quickstart.sh --help`` exits 0 and prints usage."""
    bash = _bash_bin()
    result = subprocess.run(
        [bash, str(CLOUD_QUICKSTART_PATH), "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"--help should exit 0; stderr={result.stderr!r}"
    )
    assert "cloud-quickstart" in result.stdout.lower()
    assert "--prompt" in result.stdout
    assert "--no-init" in result.stdout
    assert "--dry-run" in result.stdout


def test_cloud_quickstart_version_exits_zero(tmp_path: Path) -> None:
    """``cloud-quickstart.sh version`` exits 0 and prints the script version."""
    bash = _bash_bin()
    result = subprocess.run(
        [bash, str(CLOUD_QUICKSTART_PATH), "version"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"version should exit 0; stderr={result.stderr!r}"
    )
    assert "cloud-quickstart.sh" in result.stdout
    assert "v0.9" in result.stdout, (
        f"version output must include the v0.9.x line; got {result.stdout!r}"
    )


def test_cloud_quickstart_missing_cursor_api_key_exits_one(tmp_path: Path) -> None:
    """Script exits 1 with a helpful message when ``CURSOR_API_KEY`` is unset (acceptance z)."""
    bash = _bash_bin()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
    }
    # Inherit other variables but explicitly UNSET CURSOR_API_KEY by not putting it in env.
    result = subprocess.run(
        [bash, str(CLOUD_QUICKSTART_PATH), "--no-init", "--dry-run"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 1, (
        f"missing CURSOR_API_KEY must exit 1; got {result.returncode} "
        f"(stdout={result.stdout!r}, stderr={result.stderr!r})"
    )
    combined = result.stdout + result.stderr
    assert "CURSOR_API_KEY" in combined, (
        f"error message must mention CURSOR_API_KEY; got {combined!r}"
    )


def test_cloud_quickstart_unknown_option_exits_nonzero(tmp_path: Path) -> None:
    """An unknown option exits non-zero with a helpful message."""
    bash = _bash_bin()
    result = subprocess.run(
        [bash, str(CLOUD_QUICKSTART_PATH), "--nonexistent-flag"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode != 0, (
        f"unknown option must exit non-zero; got {result.returncode}"
    )
    combined = result.stdout + result.stderr
    assert "unknown" in combined.lower() or "expected" in combined.lower()


def test_cloud_quickstart_dry_run_with_key_exits_zero(tmp_path: Path) -> None:
    """``--dry-run --no-init`` with the key set walks all 5 steps without I/O (acceptance aa)."""
    bash = _bash_bin()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CURSOR_API_KEY": "test_key_for_dry_run",
    }
    # Ensure popola is on PATH for the dry-run, otherwise the pre-flight rejects.
    popola_bin = shutil.which("popola")
    if popola_bin is None:
        pytest.skip("popola is not on PATH; cannot exercise the post-pre-flight dry-run path")
    env["PATH"] = f"{Path(popola_bin).parent}:{env['PATH']}"

    result = subprocess.run(
        [bash, str(CLOUD_QUICKSTART_PATH), "--no-init", "--dry-run", "--prompt", "smoke"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"dry-run with key should exit 0; stderr={result.stderr!r} stdout={result.stdout!r}"
    )
    out = result.stdout
    assert "Step 0/5" in out and "Step 5/5" in out, (
        f"dry-run should walk all 5 steps; got: {out!r}"
    )
    assert "popola dispatch --cli=cursor-cloud" in out, (
        "dry-run preview must include the cloud dispatch command"
    )


def test_cloud_quickstart_idempotent_dry_run(tmp_path: Path) -> None:
    """Re-running the dry-run twice in the same dir produces the same exit code (acceptance aa)."""
    bash = _bash_bin()
    popola_bin = shutil.which("popola")
    if popola_bin is None:
        pytest.skip("popola is not on PATH; cannot exercise the post-pre-flight dry-run path")
    env = {
        "PATH": f"{Path(popola_bin).parent}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CURSOR_API_KEY": "test_key_for_dry_run",
    }
    cmd = [
        bash,
        str(CLOUD_QUICKSTART_PATH),
        "--no-init",
        "--dry-run",
        "--prompt",
        "smoke",
        "--quiet",
    ]
    first = subprocess.run(
        cmd,
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    second = subprocess.run(
        cmd,
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert first.returncode == second.returncode == 0, (
        "idempotent re-run should exit 0 both times; "
        f"first={first.returncode}, second={second.returncode}, "
        f"first.stderr={first.stderr!r}, second.stderr={second.stderr!r}"
    )
