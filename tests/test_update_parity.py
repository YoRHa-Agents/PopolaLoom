"""Parity test: ``popola update`` ↔ ``install.sh update`` flag matrix (v1.4.0).

The Python orchestrator in
:mod:`popolaloom.evolution.self_update.resolve_install_spec` and the
bash helper inside ``install.sh:verb_update`` (lines 502-525) *must*
produce byte-identical pip specs for the same flag inputs — otherwise
operators see different behaviour depending on which entry point they
used.

Implementation note: rather than ``source``-ing ``install.sh`` (which
runs ``main "$@"`` at the bottom of the file) or extracting just the
``resolve_install_spec`` function, we invoke ``install.sh update
--dry-run`` for each fixture row and parse the resolved spec out of
the ``[install.sh] step 1/3: pip install --upgrade <spec>`` log line.
This matches the strategy of
:mod:`tests.cli.test_install_script` and keeps the test honest — both
the bash function and the calling sequence around it are exercised.

Skipped on Windows because ``install.sh`` requires bash.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from popolaloom.evolution.self_update import resolve_install_spec

INSTALL_SCRIPT = Path(__file__).resolve().parent.parent / "install.sh"

_STEP1_LINE_RE = re.compile(
    r"\[install\.sh\] step 1/3: pip install --upgrade (?P<spec>.+)$"
)


pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="install.sh requires bash (unix-only)",
)


def _shell_resolve_spec(
    *,
    from_: str = "git",
    ref: str | None = None,
    version: str | None = None,
    with_credentials: bool = False,
) -> str:
    """Invoke ``install.sh update --dry-run`` and return the resolved spec.

    Parses the line emitted by :func:`verb_update`::

        [install.sh] step 1/3: pip install --upgrade <spec>

    Uses ``--no-skills`` so the dry-run doesn't even pretend to call
    ``popola skill upgrade`` (we only care about the pip spec).
    """
    args: list[str] = [
        str(INSTALL_SCRIPT),
        "update",
        "--dry-run",
        "--no-skills",
        f"--from={from_}",
    ]
    if ref is not None:
        args.append(f"--ref={ref}")
    if version is not None:
        args.append(f"--version={version}")
    if with_credentials:
        args.append("--with-credentials")

    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        pytest.fail(
            f"install.sh update --dry-run exited {result.returncode}; "
            f"stdout={result.stdout!r}; stderr={result.stderr!r}"
        )
    for line in result.stdout.splitlines():
        match = _STEP1_LINE_RE.search(line)
        if match:
            return match.group("spec").strip()
    pytest.fail(
        f"could not find 'step 1/3: pip install --upgrade ...' line in "
        f"install.sh stdout={result.stdout!r}"
    )


# Each row: (from_, ref, version, with_credentials).  Covers every
# combination install.sh supports (git / pypi / local-path × extras
# on/off × ref/version pin).  Drift in either implementation fails
# this matrix.
_PARITY_MATRIX: tuple[tuple[str, str | None, str | None, bool], ...] = (
    ("git", None, None, False),
    ("git", "v1.4.0", None, False),
    ("git", None, None, True),
    ("git", "v1.4.0", None, True),
    ("pypi", None, None, False),
    ("pypi", None, "1.4.0", False),
    ("pypi", None, None, True),
    ("pypi", None, "1.4.0", True),
    ("/abs/path/to/wheel.whl", None, None, False),
    ("/abs/path/to/wheel.whl", None, None, True),
)


@pytest.mark.parametrize(
    "from_, ref, version, with_credentials",
    _PARITY_MATRIX,
)
def test_resolve_install_spec_parity_with_install_sh(
    from_: str,
    ref: str | None,
    version: str | None,
    with_credentials: bool,
) -> None:
    """Python ``resolve_install_spec`` produces the same spec as install.sh.

    Walks every cell of the 3 (sources) × 2 (extras) × ref/version pin
    matrix.  A future flag rename in either implementation fails fast.
    """
    py_spec = resolve_install_spec(
        from_=from_, ref=ref, version=version, with_credentials=with_credentials
    )
    sh_spec = _shell_resolve_spec(
        from_=from_, ref=ref, version=version, with_credentials=with_credentials
    )
    assert py_spec == sh_spec, (
        f"resolve_install_spec parity drift for "
        f"(from_={from_!r}, ref={ref!r}, version={version!r}, "
        f"with_credentials={with_credentials}); "
        f"py={py_spec!r} != sh={sh_spec!r}"
    )


def test_install_sh_update_help_advertises_same_flags() -> None:
    """``install.sh --help`` lists the same flags that ``popola update`` accepts."""
    result = subprocess.run(
        [str(INSTALL_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0
    help_text = result.stdout
    expected = ["--scope", "--target", "--from", "--ref", "--version", "--with-credentials"]
    missing = [f for f in expected if f not in help_text]
    assert not missing, f"install.sh --help missing flags: {missing}"


def test_popola_update_help_advertises_same_flags() -> None:
    """``popola update --help`` lists the cross-checked flag set."""
    from typer.testing import CliRunner

    from popolaloom.cli.update_cmd import app

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    help_text = result.stdout
    expected = ["--scope", "--target", "--from", "--ref", "--version", "--with-credentials"]
    missing = [f for f in expected if f not in help_text]
    assert not missing, f"popola update --help missing flags: {missing}"
