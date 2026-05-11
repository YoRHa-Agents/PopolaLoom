"""v1.1.1 init UX tests: preferences footer/wizard and sourceable fallback env."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from popolaloom import credentials as cred_mod
from popolaloom.cli import init_cmd
from popolaloom.cli.init_cmd import app as init_app


@pytest.fixture
def isolated_init_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, Path]]:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()
    popola_home = tmp_path / "popola"

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("POPOLA_HOME", str(popola_home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv(cred_mod.CURSOR_API_KEY_ENV, raising=False)

    yield cwd, popola_home


def _combined_output(result: object) -> str:
    stdout = getattr(result, "stdout", "") or ""
    try:
        stderr = getattr(result, "stderr", "") or ""
    except (ValueError, AttributeError):
        stderr = ""
    output = getattr(result, "output", "") or ""
    return stdout + stderr + output


def test_noninteractive_init_prints_preferences_footer_when_unconfigured(
    isolated_init_home: tuple[Path, Path],
) -> None:
    """Root ``popola init`` prints the optional preferences footer when absent."""
    result = CliRunner().invoke(init_app, [])

    out = _combined_output(result)
    assert result.exit_code == 0, out
    assert "NOTE: [user_preferences] not configured" in out
    assert "popola init prefs --wizard" in out


def test_with_preferences_wizard_invokes_step_when_tty(
    isolated_init_home: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--with-preferences-wizard`` invokes the Step 6 wizard on a TTY."""
    calls: list[bool] = []
    monkeypatch.setattr(init_cmd, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(init_cmd, "_run_preferences_wizard_step", lambda: calls.append(True))

    result = CliRunner().invoke(init_app, ["--with-preferences-wizard"])

    out = _combined_output(result)
    assert result.exit_code == 0, out
    assert calls == [True]


def test_dry_run_with_preferences_wizard_does_not_invoke_step(
    isolated_init_home: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` suppresses the optional preferences wizard."""
    calls: list[bool] = []
    monkeypatch.setattr(init_cmd, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(init_cmd, "_run_preferences_wizard_step", lambda: calls.append(True))

    result = CliRunner().invoke(init_app, ["--dry-run", "--with-preferences-wizard"])

    out = _combined_output(result)
    assert result.exit_code == 0, out
    assert calls == []


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ("CURSOR_API_KEY=crsr_legacy\n", "crsr_legacy"),
        ("export CURSOR_API_KEY=crsr_exported\n", "crsr_exported"),
    ],
)
def test_env_fallback_loader_accepts_legacy_and_export_forms(
    isolated_init_home: tuple[Path, Path],
    contents: str,
    expected: str,
) -> None:
    """Daemon auto-source parser accepts both legacy and sourceable env files."""
    path = cred_mod._env_fallback_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o600)

    loaded = cred_mod.load_env_fallback_into_environ()

    assert loaded is True
    assert os.environ[cred_mod.CURSOR_API_KEY_ENV] == expected


def test_write_env_fallback_writes_sourceable_export(
    isolated_init_home: tuple[Path, Path],
) -> None:
    """The fallback writer emits a shell-sourceable ``export`` assignment."""
    path = cred_mod.write_env_fallback("crsr_written")

    assert path.read_text(encoding="utf-8").startswith("export CURSOR_API_KEY=")
