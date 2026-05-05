"""v0.5.0 Stage S5 e2e smoke — `examples/quickstart.sh` runs end-to-end.

Single slow-marked test that exercises the new 6-step
``examples/quickstart.sh`` (Stage S5.E rewrite) against a fresh
``$POPOLA_HOME`` under ``tmp_path``. Asserts the script exits 0
within 60 s — every step (``popola init --dry-run`` × 2,
``popola popolad start``, ``popola dispatch``, ``popola list``,
``popola status``, ``popola doctor``, ``popola popolad stop``) must
succeed. ``popola init --dry-run`` is used so the test process never
writes to the developer's real ``~/.cursor/`` directory.

Marked ``slow`` because spawning a real popolad subprocess takes
~ 1 s; runs in the slow lane only (``pytest -m slow``).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.slow
def test_quickstart_v050_smoke_runs_without_error(tmp_path: Path) -> None:
    """e2e smoke: ``examples/quickstart.sh`` runs end-to-end on a fresh tmp_path.

    Verifies that ``popola init --dry-run`` + ``popola popolad start``
    + ``popola dispatch echo`` + ``popola list`` + ``popola status``
    + ``popola doctor`` + ``popola popolad stop`` all succeed without
    errors. Uses ``--dry-run`` for ``popola init`` to avoid actually
    writing to ``~/.cursor/`` from the test process.
    """
    repo_root = Path(__file__).resolve().parents[2]
    quickstart = repo_root / "examples" / "quickstart.sh"
    assert quickstart.exists(), f"missing {quickstart} (Stage S5.E)"

    env = {**os.environ, "POPOLA_HOME": str(tmp_path / ".popola")}
    result = subprocess.run(
        ["bash", str(quickstart)],
        env=env,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"quickstart.sh exited {result.returncode}; "
        f"stdout:\n{result.stdout}\n--STDERR--\n{result.stderr}"
    )
