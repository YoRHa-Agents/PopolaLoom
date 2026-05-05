"""Tier 5 — v0.5.0 quickstart smoke test (updated from v0.3.5 5-step contract).

Per v0.5.0 Stage S5: ``examples/quickstart.sh`` is the canonical 6-min demo
that exercises the new ``popola init`` + ``popola doctor`` surface in
addition to the existing daemon / dispatch / list / status flow:

0. ``popola init`` Skill installer dry-run for cursor + claude (NEW).
1. popolad daemon starts (UDS bind under tmp ``$POPOLA_HOME``).
2. ``popola dispatch`` returns a task_id.
3. ``popola list --all`` includes that task_id.
4. ``popola status <task_id>`` reports a state + exit_code.
5. ``popola doctor --json`` aggregates skill / daemon / lark / arktower (NEW).
6. popolad shuts down cleanly.

The test is marked ``@pytest.mark.slow`` because spawning a daemon takes ~1 s;
it lives in tier 5 alongside the e2e self-bootstrap suite. The replacement
e2e at ``tests/integration/test_quickstart_v050.py`` exercises the same path
with a tighter assertion set.

The script must be runnable as ``bash examples/quickstart.sh`` from the repo
root with no additional setup beyond the editable install + ArkTower
migrations available at the standard fallback path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow


_REPO_ROOT: Path = Path(__file__).resolve().parents[3]
_QUICKSTART_SCRIPT: Path = _REPO_ROOT / "examples" / "quickstart.sh"
_QUICKSTART_TIMEOUT_S: float = 60.0
"""Generous wall-clock budget — the script's own teardown handles
daemon shutdown but if the daemon hangs (e.g. shim survives) the
test should fail rather than wedge the whole suite."""


def test_quickstart_script_exists_and_is_executable() -> None:
    """Sanity: the script ships with the package + has the +x bit set."""
    assert _QUICKSTART_SCRIPT.is_file(), (
        f"missing examples/quickstart.sh — run from {_REPO_ROOT}"
    )
    assert os.access(_QUICKSTART_SCRIPT, os.X_OK), (
        f"{_QUICKSTART_SCRIPT} is not executable; run `chmod +x` on it"
    )


def test_quickstart_6_step_smoke(tmp_path: Path) -> None:
    """End-to-end: bash examples/quickstart.sh → all 6 v0.5.0 steps PASS.

    Uses an isolated tmp ``$POPOLA_HOME`` so the test never touches
    the developer's real ``~/.popola``. Asserts the script's exit code
    is 0 and that each of the 6 step markers (Step 0/6 .. Step 6/6)
    appears in stdout, plus the final ``all 6 steps PASS`` message.

    v0.5.0 changes from the old 5-step v0.3.5 contract:
    - Step 0/6 NEW: ``popola init`` Skill installer dry-run.
    - Step 4/6 changed: ``popola status`` (was ``popola eval run``).
    - Step 5/6 NEW: ``popola doctor`` aggregate health check.
    - Step 6/6 (was Step 5/5): popolad stop — same.
    - The nines.toml assertion is removed; ``popola eval run`` is no longer
      part of the canonical quickstart (kept in DEMO.md for ad-hoc use).
      The old eval-output assertion lives in ``tests/test_evaluation.py``.
    """
    if shutil.which("bash") is None:
        pytest.skip("bash not on PATH; quickstart.sh is bash-specific")

    home = tmp_path / "popola_home"
    home.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["POPOLA_HOME"] = str(home)
    arktower_migrations = Path("/home/agent/reference/ArkTower/migrations")
    if arktower_migrations.is_dir():
        env.setdefault("POPOLA_ARKTOWER_MIGRATIONS_DIR", str(arktower_migrations))

    completed = subprocess.run(
        ["bash", str(_QUICKSTART_SCRIPT)],
        env=env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=_QUICKSTART_TIMEOUT_S,
        check=False,
    )

    full_output = completed.stdout + "\n--STDERR--\n" + completed.stderr
    assert completed.returncode == 0, (
        f"quickstart.sh exited {completed.returncode}; output:\n{full_output}"
    )
    for step in (
        "Step 0/6: Skill installer dry-run",
        "Step 1/6: starting popolad",
        "Step 2/6: dispatching echo task",
        "Step 3/6: confirming task appears",
        "Step 4/6: querying popola status",
        "Step 5/6: running popola doctor",
        "Step 6/6: stopping popolad",
        "all 6 steps PASS",
    ):
        assert step in completed.stdout, (
            f"missing marker {step!r} in quickstart.sh output:\n{full_output}"
        )


def test_quickstart_script_uses_popola_home_env_var() -> None:
    """The script honours ``$POPOLA_HOME`` (so user's real ~/.popola
    is never touched by `bash examples/quickstart.sh`).

    Static check — read the script and assert it sources POPOLA_HOME.
    """
    text = _QUICKSTART_SCRIPT.read_text(encoding="utf-8")
    assert 'POPOLA_HOME="${POPOLA_HOME:-' in text, (
        "quickstart.sh must default POPOLA_HOME to a tmp dir, not "
        "fall through to the user's real ~/.popola"
    )
    assert "trap cleanup EXIT" in text, (
        "quickstart.sh must set up an EXIT trap to ensure popolad is "
        "stopped + tmp dir is cleaned up even on script failure"
    )


def test_quickstart_referenced_from_readme() -> None:
    """README points to ``examples/quickstart.sh`` so users can find it."""
    readme = _REPO_ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert "examples/quickstart.sh" in text


def test_demo_md_exists_with_v050_sections() -> None:
    """``docs/DEMO.md`` exists and references the v0.5.0 sections.

    v0.3.5 markers (Quickstart walkthrough / popola dispatch / popola eval run
    / 8 dimensions) are kept since they are still present in the existing
    DEMO body. v0.5.0 added a "Skill installation walkthrough" section that
    mentions ``popola init`` + ``popola doctor`` — assert at least one
    Skill-section anchor so a missing v0.5.0 doc-refresh would fail this
    test rather than silently passing on the legacy markers alone.
    """
    demo = _REPO_ROOT / "docs" / "DEMO.md"
    assert demo.is_file(), f"missing {demo} — run round-5 evidence write step"
    text = demo.read_text(encoding="utf-8")
    for marker in (
        "Quickstart walkthrough",
        "popola dispatch",
        "popola eval run",
        "8 dimensions",
        "popola init",
        "popola doctor",
    ):
        assert marker in text, f"DEMO.md missing section / marker {marker!r}"


def test_python_version_for_quickstart() -> None:
    """The quickstart.sh embeds python -c snippets; ensure the python
    version is 3.11+ so ``import tomllib`` works (it's stdlib only on
    3.11+).
    """
    assert sys.version_info >= (3, 11), (
        f"quickstart.sh expects python ≥ 3.11 (for tomllib); "
        f"got {sys.version_info}"
    )
