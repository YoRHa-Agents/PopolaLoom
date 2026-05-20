"""Default-lane subprocess tests for repo-root ``install.sh``.

Covers ``--help`` / ``version`` output, verb dry-run lines, validation error
paths, and keeps ``pip`` uninvoked (``--dry-run`` everywhere).

Skipped on Windows since bash is required.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

INSTALL_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "install.sh"
PIP_EXTRA_INDEX_ARG = "--extra-index-url=https://pypi.org/simple"


def _install_script_shell_version_from_header() -> str:
    txt = INSTALL_SCRIPT_PATH.read_text(encoding="utf-8")
    marker = 'readonly POPOLA_INSTALL_SCRIPT_VERSION="'
    start = txt.find(marker)
    if start == -1:
        pytest.fail("install.sh must declare POPOLA_INSTALL_SCRIPT_VERSION")
    rest = txt[start + len(marker) :]
    end = rest.find('"')
    if end == -1:
        pytest.fail("Malformed POPOLA_INSTALL_SCRIPT_VERSION in install.sh")
    return rest[:end]


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
    """``install.sh --help`` exits 0 and prints the verb / option matrix.

    v0.9.6 (closes feedback_for_v0.9.4 lines 2-5): the help output must also
    advertise the new ``--ref=<tag|branch|sha>`` flag introduced alongside
    the default-source switch from ``pypi`` to ``git``.

    v0.9.7 (closes feedback_for_v0.9.4 line 1): help must also advertise the
    new ``--with-credentials`` flag, since the WARN paths in
    ``credentials.py`` / ``init_cmd.py`` / ``auth_cmd.py`` now point at
    that flag instead of a raw ``pip install popolaloom[credentials]``.
    """
    result = _run(["--help"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "install" in out
    assert "update" in out
    assert "uninstall" in out
    assert "--scope" in out
    assert "--target" in out
    assert "--dry-run" in out
    assert "--ref" in out
    assert "--with-credentials" in out


def test_install_script_version_returns_zero(tmp_path: Path) -> None:
    """``install.sh version`` exits 0 and prints the script version."""
    expected = _install_script_shell_version_from_header()
    result = _run(["version"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "install.sh" in result.stdout
    assert expected in result.stdout


def test_install_script_unknown_verb_returns_nonzero_with_message(tmp_path: Path) -> None:
    """An unknown verb exits non-zero with a helpful message on stderr."""
    result = _run(["bogusverb", "--dry-run"], cwd=tmp_path)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "unknown verb" in combined.lower() or "expected" in combined.lower()


def test_install_script_install_dry_run_prints_pip_command(tmp_path: Path) -> None:
    """``install --dry-run`` prints the would-be ``pip install`` command.

    v0.9.6 (closes ``.local/feedbacks/feedback_for_v0.9.4.md`` lines 2-5):
    the default ``--from`` flipped from ``pypi`` to ``git`` because PyPI
    publish remains deferred for the v0.9.x line (Q-D-5 偏离默认,
    BL-v0.9.x-PyPI). With no explicit ``--from`` the dry-run must therefore
    emit the GitHub URL, not the bare PyPI package name. The repo URL still
    carries ``PopolaLoom.git`` (CamelCase project name) so the popolaloom
    identity is reachable case-insensitively for grep-based smoke checks.
    """
    result = _run(
        ["install", "--dry-run", "--no-daemon", "--no-skills"],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "pip install" in out
    assert PIP_EXTRA_INDEX_ARG in out
    assert "using pip extra index for git-source build dependencies" in out
    assert "git+https://github.com/YoRHa-Agents/PopolaLoom.git" in out
    assert "popolaloom" in out.lower()


def test_install_script_install_dry_run_with_version_pin(tmp_path: Path) -> None:
    """``install --dry-run --from=pypi --version=X.Y.Z`` includes the pin.

    v0.9.6: with the default flipped to ``git`` (per
    ``.local/feedbacks/feedback_for_v0.9.4.md`` lines 2-5), version pinning
    must now be paired with an explicit ``--from=pypi`` because the pin is
    only valid for the PyPI source. The reverse case (``--version`` without
    ``--from=pypi`` errors out) is covered by
    ``test_install_script_pin_version_outside_pypi_errors`` below.
    """
    result = _run(
        [
            "install",
            "--dry-run",
            "--no-daemon",
            "--no-skills",
            "--from=pypi",
            "--version=9.9.9",
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "popolaloom==9.9.9" in out
    assert PIP_EXTRA_INDEX_ARG not in out


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
    assert PIP_EXTRA_INDEX_ARG in out
    assert "git+https://github.com/YoRHa-Agents/PopolaLoom.git" in out


def test_install_script_install_default_uses_git_source(tmp_path: Path) -> None:
    """With no ``--from`` flag, the dry-run produces the git URL.

    v0.9.6 (closes ``.local/feedbacks/feedback_for_v0.9.4.md`` lines 2-5):
    the default install source flipped from ``pypi`` to ``git`` because
    PyPI publish remains deferred for the v0.9.x line (Q-D-5 偏离默认,
    BL-v0.9.x-PyPI). This test pins the new default so a future regression
    that flips the default back to ``pypi`` (re-introducing the 404 on
    Chinese pip mirrors that don't carry popolaloom yet) fails fast.
    """
    result = _run(
        [
            "install",
            "--dry-run",
            "--no-daemon",
            "--no-skills",
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert PIP_EXTRA_INDEX_ARG in out
    assert "git+https://github.com/YoRHa-Agents/PopolaLoom.git" in out
    # Sanity: the unpinned PyPI form must not appear when default is git
    # (`popolaloom` still appears as the egg-name embedded in the URL,
    # so we anchor on the pin form to avoid a false positive).
    assert "popolaloom==" not in out


def test_install_script_local_source_omits_git_extra_index(tmp_path: Path) -> None:
    """Local path installs keep the user's pip index behavior unchanged."""
    result = _run(
        [
            "install",
            "--dry-run",
            "--no-daemon",
            "--no-skills",
            "--from=./dist/popolaloom.whl",
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "./dist/popolaloom.whl" in out
    assert PIP_EXTRA_INDEX_ARG not in out
    assert "using pip extra index for git-source build dependencies" not in out


def test_install_script_install_dry_run_with_ref_tag(tmp_path: Path) -> None:
    """``install --dry-run --from=git --ref=v0.9.6`` appends ``@v0.9.6`` to the URL.

    v0.9.6 NEW (closes ``.local/feedbacks/feedback_for_v0.9.4.md`` lines 2-5):
    the ``--ref`` flag is the canonical tag-pinned install for the v0.9.x
    line until PyPI promotion lands (BL-v0.9.x-PyPI). The flag must produce
    the standard pip-resolvable ``git+...@<ref>`` form so operators on
    air-gapped or firewalled hosts can pin exact tags via the same recipe.
    """
    result = _run(
        [
            "install",
            "--dry-run",
            "--no-daemon",
            "--no-skills",
            "--from=git",
            "--ref=v0.9.6",
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert PIP_EXTRA_INDEX_ARG in out
    assert "git+https://github.com/YoRHa-Agents/PopolaLoom.git@v0.9.6" in out


def test_install_script_ref_outside_git_errors(tmp_path: Path) -> None:
    """``--ref=<value>`` requires ``--from=git``; PyPI / path sources error out.

    v0.9.6: matches the ``--version=X.Y.Z requires --from=pypi`` rule
    introduced earlier (No Silent Failures — the operator gets a loud
    rejection instead of a silent ignore).
    """
    # With explicit --from=pypi the rejection is unambiguous.
    result_pypi = _run(
        [
            "install",
            "--dry-run",
            "--no-daemon",
            "--no-skills",
            "--from=pypi",
            "--ref=v0.9.6",
        ],
        cwd=tmp_path,
    )
    assert result_pypi.returncode != 0
    combined_pypi = result_pypi.stdout + result_pypi.stderr
    assert "ref" in combined_pypi.lower()
    assert "git" in combined_pypi.lower()

    # With a local path source the same gate fires.
    result_path = _run(
        [
            "install",
            "--dry-run",
            "--no-daemon",
            "--no-skills",
            "--from=./local/path",
            "--ref=v0.9.6",
        ],
        cwd=tmp_path,
    )
    assert result_path.returncode != 0
    combined_path = result_path.stdout + result_path.stderr
    assert "ref" in combined_path.lower()
    assert "git" in combined_path.lower()


def test_install_script_update_dry_run_prints_upgrade_command(tmp_path: Path) -> None:
    """``update --dry-run`` prints the would-be ``pip install --upgrade`` command.

    v0.9.6: the default ``--from=git`` flip applies to ``update`` too (both
    verbs share ``resolve_install_spec``), so the URL form is the canonical
    output. ``popolaloom`` lowercase only appears when the operator opts
    back into ``--from=pypi``; we keep the assertion case-insensitive so
    the upgrade smoke remains grep-friendly.
    """
    result = _run(
        ["update", "--dry-run", "--no-skills"],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "pip install --upgrade" in out
    assert PIP_EXTRA_INDEX_ARG in out
    assert "popolaloom" in out.lower()


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
            "--version=9.9.9",
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


# ── v0.9.7 --with-credentials (closes feedback_for_v0.9.4 line 1) ──────


def test_install_script_with_credentials_dry_run_pypi(tmp_path: Path) -> None:
    """``--with-credentials --from=pypi`` resolves to ``popolaloom[credentials]``.

    v0.9.7 NEW (closes ``.local/feedbacks/feedback_for_v0.9.4.md`` line 1):
    the previous WARN paths in ``credentials.py`` / ``init_cmd.py`` /
    ``auth_cmd.py`` told operators to run a separate
    ``pip install popolaloom[credentials]``. v0.9.7 rolls that into the
    same install via the new ``--with-credentials`` flag, and the WARN
    text is updated to point at the flag instead of a bare ``pip install``
    (per the workspace rule "popola 不使用 pip 修正安装方式").
    """
    result = _run(
        [
            "install",
            "--dry-run",
            "--no-daemon",
            "--no-skills",
            "--with-credentials",
            "--from=pypi",
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "popolaloom[credentials]" in out
    assert "with_credentials=1" in out


def test_install_script_with_credentials_dry_run_pypi_with_version(
    tmp_path: Path,
) -> None:
    """``--with-credentials --from=pypi --version=X.Y.Z`` resolves cleanly.

    v0.9.7: the spec must be ``popolaloom[credentials]==X.Y.Z`` (extras
    immediately after the package name; ``==`` after the closing bracket).
    """
    result = _run(
        [
            "install",
            "--dry-run",
            "--no-daemon",
            "--no-skills",
            "--with-credentials",
            "--from=pypi",
            "--version=9.9.9",
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "popolaloom[credentials]==9.9.9" in out


def test_install_script_with_credentials_dry_run_git_default(tmp_path: Path) -> None:
    """``--with-credentials`` (default git) emits the PEP 508 ``pkg @ url`` form.

    v0.9.7: modern pip (>=21) accepts ``popolaloom[credentials] @ git+...``
    directly; the deprecated ``#egg=popolaloom[credentials]`` form is
    intentionally not used.
    """
    result = _run(
        [
            "install",
            "--dry-run",
            "--no-daemon",
            "--no-skills",
            "--with-credentials",
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert (
        "popolaloom[credentials] @ git+https://github.com/YoRHa-Agents/PopolaLoom.git"
        in out
    )


def test_install_script_with_credentials_dry_run_git_with_ref(tmp_path: Path) -> None:
    """``--with-credentials --ref=<tag>`` keeps both the extra and the ``@<ref>``.

    v0.9.7: the resolved spec must be
    ``popolaloom[credentials] @ git+https://github.com/.../PopolaLoom.git@v0.9.6``
    so operators can pin a specific tag AND get the credentials extra in
    one install — matches the canonical tag-pinned recipe shape.
    """
    result = _run(
        [
            "install",
            "--dry-run",
            "--no-daemon",
            "--no-skills",
            "--with-credentials",
            "--ref=v0.9.6",
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert (
        "popolaloom[credentials] @ "
        "git+https://github.com/YoRHa-Agents/PopolaLoom.git@v0.9.6"
        in out
    )


def test_install_script_with_credentials_uninstall_errors(tmp_path: Path) -> None:
    """``uninstall --with-credentials`` is rejected (no Silent Failures).

    v0.9.7: ``--with-credentials`` only makes sense for install / update —
    the uninstall verb drops the package entirely, so a stray flag must
    fail loud rather than silently no-op (matches ``--ref`` + ``--version``
    semantics introduced in earlier patches).
    """
    result = _run(
        [
            "uninstall",
            "--with-credentials",
            "--dry-run",
            "--yes",
            "--no-skills",
        ],
        cwd=tmp_path,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "with-credentials" in combined.lower()
    assert "uninstall" in combined.lower()


def test_install_script_with_credentials_update_dry_run(tmp_path: Path) -> None:
    """``update --with-credentials`` shares the same resolver as install.

    v0.9.7: operators with an existing v0.9.6 install upgrade-in-place to
    add the keyring extra via ``./install.sh update --with-credentials``.
    The flag flows through ``resolve_install_spec`` so the upgrade spec
    matches the install spec verbatim.
    """
    result = _run(
        ["update", "--dry-run", "--no-skills", "--with-credentials"],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "pip install --upgrade" in out
    assert "popolaloom[credentials]" in out
    assert "with_credentials=1" in out


def test_install_script_default_install_omits_credentials_extra(
    tmp_path: Path,
) -> None:
    """Without ``--with-credentials`` the spec MUST NOT include ``[credentials]``.

    v0.9.7: the flag is opt-in. A fresh ``./install.sh install`` must
    behave exactly like v0.9.6 (no extras appended), so the surface
    remains additive and existing CI lanes do not pick up the optional
    ``keyring>=25`` dep without explicit consent.
    """
    result = _run(
        ["install", "--dry-run", "--no-daemon", "--no-skills"],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "[credentials]" not in out
    assert "with_credentials=0" in out
