"""v1.1.1 ``popola init`` skill-drift detection tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from popolaloom import __version__
from popolaloom.cli.init_cmd import _install_target, app as init_app


@pytest.fixture
def isolated_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, Path]]:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path / "popola"))

    yield cwd, fake_home


def _combined_output(result: object) -> str:
    stdout = getattr(result, "stdout", "") or ""
    try:
        stderr = getattr(result, "stderr", "") or ""
    except (ValueError, AttributeError):
        stderr = ""
    output = getattr(result, "output", "") or ""
    return stdout + stderr + output


def test_install_target_existing_no_marker_uses_legacy_skip(
    isolated_home: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    cwd, _fake_home = isolated_home
    target = cwd / ".cursor" / "skills" / "popola-loom" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("operator edit\n", encoding="utf-8")

    action = _install_target("cursor", scope="project", cwd=cwd, dry_run=False)

    out = capsys.readouterr().out
    assert action == "SKIP"
    assert "SKIP" in out
    assert "already installed" in out
    assert target.read_text(encoding="utf-8") == "operator edit\n"


def test_install_target_existing_current_marker_uses_versioned_skip(
    isolated_home: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    cwd, _fake_home = isolated_home
    target = cwd / ".cursor" / "skills" / "popola-loom" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("operator edit\n", encoding="utf-8")
    (target.parent / ".popola-loom-version").write_text(f"{__version__}\n", encoding="utf-8")

    action = _install_target("cursor", scope="project", cwd=cwd, dry_run=False)

    out = capsys.readouterr().out
    assert action == "SKIP"
    assert f"already at v{__version__}" in out
    assert target.read_text(encoding="utf-8") == "operator edit\n"


def test_install_target_marker_drift_warns_without_overwriting(
    isolated_home: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    cwd, _fake_home = isolated_home
    target = cwd / ".cursor" / "skills" / "popola-loom" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("operator edit\n", encoding="utf-8")
    (target.parent / ".popola-loom-version").write_text("1.0.0\n", encoding="utf-8")

    action = _install_target("cursor", scope="project", cwd=cwd, dry_run=False)

    out = capsys.readouterr().out
    assert action == "DRIFT"
    assert "DRIFT" in out
    assert "popola skill upgrade --target=cursor --project" in out
    assert "--upgrade-on-drift" in out
    assert target.read_text(encoding="utf-8") == "operator edit\n"


def test_upgrade_on_drift_overwrites_file_and_marker(
    isolated_home: tuple[Path, Path],
) -> None:
    cwd, _fake_home = isolated_home
    target = cwd / ".cursor" / "skills" / "popola-loom" / "SKILL.md"
    marker = target.parent / ".popola-loom-version"
    target.parent.mkdir(parents=True)
    target.write_text("operator edit\n", encoding="utf-8")
    marker.write_text("1.0.0\n", encoding="utf-8")

    result = CliRunner().invoke(init_app, ["--upgrade-on-drift", "cursor", "--project"])

    out = _combined_output(result)
    assert result.exit_code == 0, out
    assert "OK" in out
    assert target.read_text(encoding="utf-8") != "operator edit\n"
    assert marker.read_text(encoding="utf-8").strip() == __version__
