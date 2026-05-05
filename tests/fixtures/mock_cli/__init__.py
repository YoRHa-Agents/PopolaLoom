"""Mock CLI library for PopolaLoom Tier 2-5 tests (v0.2.3).

Per testing-matrix.md §4 — replaces the real ``cursor-agent`` /
``claude`` / ``codex`` binaries with deterministic Python implementations
that emit the **devola-flow 3-section L3 output contract** (per
testing-matrix.md §4.4 + roadmap §11.1):

1. First line: ``[devola-flow:round=N]``
2. Body content (configurable)
3. Tail trio:
   - ``## Acceptance Verification``
   - ``## Gate Score Components`` (with composite_score)
   - ``## Findings`` (severity-classified)

All three mocks share the same trailing structure but match the **argv
shape** of the real CLI they replace:

- :mod:`mock_cursor` — ``cursor-agent agent --print [--output-format text|stream-json]``
- :mod:`mock_claude` — ``claude -p <prompt> --output-format stream-json``
- :mod:`mock_codex`  — ``codex exec [--sandbox <mode>] <prompt>``

This module re-exports the three callable APIs so test code can do::

    from tests.fixtures.mock_cli import run_mock_cursor, run_mock_claude, run_mock_codex

Each ``run_*`` function returns a :class:`subprocess.CompletedProcess`
shape (when ``capture=True`` — the default), or invokes the mock as if
spawned by ``Popen`` so adapter integration tests can intercept stdout.

For environment-based control (PopolaLoom dispatch path needs a real
binary on ``$PATH``), use :func:`install_mock_binaries` from this
module — it materialises the 3 mocks as executable scripts in the given
``bin_dir`` so ``shutil.which("cursor-agent")`` resolves to the mock.

See ``tests/fixtures/mock_cli/README.md`` for the full behavioural
contract + env-var configuration matrix.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from tests.fixtures.mock_cli.mock_claude import run_mock_claude
from tests.fixtures.mock_cli.mock_codex import run_mock_codex
from tests.fixtures.mock_cli.mock_cursor import run_mock_cursor

__all__ = [
    "install_mock_binaries",
    "run_mock_claude",
    "run_mock_codex",
    "run_mock_cursor",
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
"""Workspace root — needed so the mock-binary shims can prepend
``src/`` onto ``$PYTHONPATH`` when they re-exec into the python module."""


def install_mock_binaries(bin_dir: Path) -> dict[str, Path]:
    """Materialise mock_cursor/mock_claude/mock_codex as executables in ``bin_dir``.

    Each shim is a tiny Python script that ``exec``'s the corresponding
    ``mock_<name>.main()`` entry point.  Used by Tier 4 / Tier 5 tests
    that drive a real popolad subprocess (so ``shutil.which("cursor-agent")``
    has to actually find a file on ``$PATH``).

    Args:
        bin_dir: Directory in which to write the shim files.  Created
            if missing.

    Returns:
        Mapping from CLI name (``"cursor-agent" / "claude" / "codex"``)
        to the shim ``Path``.  Tests typically prepend ``bin_dir`` onto
        ``$PATH`` in the daemon's env.

    Notes:
        - ``cursor-agent`` is installed (not ``cursor``) because that's
          the real binary name CursorAdapter looks for.
        - ``claude`` and ``codex`` use their unprefixed names.
        - Each shim re-execs ``sys.executable -m
          tests.fixtures.mock_cli.<module> [args...]`` with
          ``PYTHONPATH`` inheriting from caller (which is why the
          install-side test ensures ``src/`` is already on PYTHONPATH).
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shims: dict[str, Path] = {}
    name_to_module = {
        "cursor-agent": "tests.fixtures.mock_cli.mock_cursor",
        "claude": "tests.fixtures.mock_cli.mock_claude",
        "codex": "tests.fixtures.mock_cli.mock_codex",
    }
    src_path = _REPO_ROOT / "src"
    test_root = _REPO_ROOT
    for binary_name, module in name_to_module.items():
        path = bin_dir / binary_name
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys, runpy\n"
            f"_extra = {str(src_path)!r} + os.pathsep + {str(test_root)!r}\n"
            "_existing = os.environ.get('PYTHONPATH', '')\n"
            "if _existing:\n"
            "    os.environ['PYTHONPATH'] = _extra + os.pathsep + _existing\n"
            "else:\n"
            "    os.environ['PYTHONPATH'] = _extra\n"
            "sys.path.insert(0, " + repr(str(src_path)) + ")\n"
            "sys.path.insert(0, " + repr(str(test_root)) + ")\n"
            f"runpy.run_module({module!r}, run_name='__main__', alter_sys=True)\n",
            encoding="utf-8",
        )
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        shims[binary_name] = path
    return shims


if __name__ == "__main__":  # pragma: no cover - smoke entry
    print(f"mock_cli module {__name__}; sys.argv={sys.argv}")
