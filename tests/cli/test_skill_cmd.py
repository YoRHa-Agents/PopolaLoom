"""Default-lane tests for ``popola skill install / doctor / upgrade`` (Stage S4 of v0.5.0).

Per v0.5.0-plan §S4.G the suite exercises:

* one happy-path test per verb × per primary target (3 verbs × 3 targets);
* the ``--target=all`` aggregator path;
* the ``--dry-run`` no-write contract;
* the ``--json`` machine-readable output;
* the ``--help`` discoverability surface.

All tests use ``tmp_path`` + ``monkeypatch`` so no developer IDE is
ever touched.  We import ``skill_app`` directly via the cli package
re-export to invoke the Typer commands through ``CliRunner`` without
spinning up a daemon (matches the existing init-cmd suite style).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from popolaloom.cli.skill_cmd import app as skill_app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, Path]]:
    """Yield ``(cwd, fake_home)`` with ``Path.home()`` / ``Path.cwd()`` patched."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("CODEX_HOME", raising=False)

    yield cwd, fake_home


def _combined(result: object) -> str:
    """Return ``result.stdout`` + best-effort ``result.stderr`` (click 8.x compat)."""
    stdout = getattr(result, "stdout", "") or ""
    try:
        stderr = getattr(result, "stderr", "") or ""
    except (ValueError, AttributeError):
        stderr = ""
    output = getattr(result, "output", "") or ""
    return stdout + stderr + output


def test_skill_help_lists_three_verbs(runner: CliRunner) -> None:
    """``popola skill --help`` shows install / doctor / upgrade (acceptance #1)."""
    result = runner.invoke(skill_app, ["--help"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "install" in out
    assert "doctor" in out
    assert "upgrade" in out


def test_skill_install_cursor_global_writes_skill(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``install --target=cursor --global`` writes ~/.cursor/skills/popola-loom/SKILL.md."""
    _cwd, fake_home = isolated_home
    result = runner.invoke(skill_app, ["install", "--target=cursor", "--global"])
    assert result.exit_code == 0, _combined(result)
    target = fake_home / ".cursor" / "skills" / "popola-loom" / "SKILL.md"
    assert target.is_file()
    assert target.read_text(encoding="utf-8").startswith("---\nname: popola-loom\n")


def test_skill_install_claude_project_writes_skill(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``install --target=claude --project`` writes <cwd>/.claude/skills/popola-loom/SKILL.md."""
    cwd, _fake_home = isolated_home
    result = runner.invoke(skill_app, ["install", "--target=claude", "--project"])
    assert result.exit_code == 0, _combined(result)
    target = cwd / ".claude" / "skills" / "popola-loom" / "SKILL.md"
    assert target.is_file()


def test_skill_install_codex_uses_home_fallback(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``install --target=codex --global`` writes ~/.codex/skills/popola-loom/SKILL.md."""
    _cwd, fake_home = isolated_home
    result = runner.invoke(skill_app, ["install", "--target=codex", "--global"])
    assert result.exit_code == 0, _combined(result)
    target = fake_home / ".codex" / "skills" / "popola-loom" / "SKILL.md"
    assert target.is_file()


def test_skill_install_target_all_installs_every_target(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``popola skill install --target=all --project`` installs every supported target.

    Exercise of the ``all`` aggregator: cursor + claude under
    ``<cwd>/.<ide>/...``, codex falls through to its own ``--global``
    fallback, copilot writes its single-file project install.
    """
    cwd, fake_home = isolated_home
    result = runner.invoke(skill_app, ["install", "--target=all", "--project"])
    assert result.exit_code == 0, _combined(result)

    assert (cwd / ".cursor" / "skills" / "popola-loom" / "SKILL.md").is_file()
    assert (cwd / ".claude" / "skills" / "popola-loom" / "SKILL.md").is_file()
    assert (fake_home / ".codex" / "skills" / "popola-loom" / "SKILL.md").is_file()
    assert (cwd / ".github" / "copilot-instructions.md").is_file()


def test_skill_install_dry_run_does_not_write(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``popola skill install --target=cursor --project --dry-run`` (acceptance #3)."""
    cwd, _fake_home = isolated_home
    result = runner.invoke(
        skill_app,
        ["install", "--target=cursor", "--project", "--dry-run"],
    )
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "DRY" in out
    assert not (cwd / ".cursor" / "skills" / "popola-loom" / "SKILL.md").exists()


def test_skill_install_json_emits_machine_readable_array(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``popola skill install --target=cursor --json`` returns a parseable JSON array."""
    _cwd, _fake_home = isolated_home
    result = runner.invoke(
        skill_app,
        ["install", "--target=cursor", "--global", "--json"],
    )
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["target"] == "cursor"
    assert payload[0]["scope"] == "global"
    assert payload[0]["installed"] is True


def test_skill_doctor_runs_and_reports_table(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``popola skill doctor`` prints the audit table (no daemon required)."""
    _cwd, fake_home = isolated_home

    result = runner.invoke(skill_app, ["doctor"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "popola skill doctor" in out
    assert "cursor" in out
    assert "claude" in out
    assert "codex" in out
    assert "copilot" in out


def test_skill_upgrade_overwrites_existing_skill(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``popola skill upgrade`` force-replaces stale SKILL.md content."""
    _cwd, fake_home = isolated_home
    target = fake_home / ".cursor" / "skills" / "popola-loom" / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\nname: popola-loom\nversion: 0.0.1-stale\n---\nold body\n",
        encoding="utf-8",
    )

    result = runner.invoke(skill_app, ["upgrade", "--target=cursor", "--global"])
    assert result.exit_code == 0, _combined(result)
    body = target.read_text(encoding="utf-8")
    assert "0.0.1-stale" not in body
    assert body.startswith("---\nname: popola-loom\n")


def test_skill_install_invalid_target_errors(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """Invalid ``--target=`` values exit non-zero with a BadParameter message (S-5)."""
    result = runner.invoke(skill_app, ["install", "--target=bogus"])
    assert result.exit_code != 0
    assert "must be one of" in _combined(result)


def test_skill_install_global_and_project_conflict_errors(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``--global`` + ``--project`` simultaneously is a BadParameter (S-5)."""
    result = runner.invoke(
        skill_app,
        ["install", "--target=cursor", "--global", "--project"],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in _combined(result)


def test_skill_doctor_json_emits_array(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``popola skill doctor --json`` returns a parseable array, one row per slot."""
    _cwd, _fake_home = isolated_home
    result = runner.invoke(skill_app, ["doctor", "--json"])
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    assert isinstance(payload, list)
    assert all("target" in r for r in payload)
    assert all("scope" in r for r in payload)


def test_skill_doctor_target_specific(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``popola skill doctor --target=cursor`` emits only cursor rows."""
    _cwd, _fake_home = isolated_home
    result = runner.invoke(skill_app, ["doctor", "--target=cursor", "--json"])
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    assert {row["target"] for row in payload} == {"cursor"}


def test_skill_upgrade_dry_run_does_not_write(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``popola skill upgrade --target=cursor --global --dry-run`` writes nothing."""
    _cwd, fake_home = isolated_home
    result = runner.invoke(
        skill_app,
        ["upgrade", "--target=cursor", "--global", "--dry-run"],
    )
    assert result.exit_code == 0, _combined(result)
    assert "DRY" in _combined(result)
    assert not (fake_home / ".cursor" / "skills" / "popola-loom" / "SKILL.md").exists()


def test_skill_upgrade_json_emits_array(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``popola skill upgrade --target=cursor --json`` returns a parseable array."""
    _cwd, _fake_home = isolated_home
    result = runner.invoke(
        skill_app,
        ["upgrade", "--target=cursor", "--global", "--json"],
    )
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    assert len(payload) == 1
    row = payload[0]
    assert row["target"] == "cursor"
    assert row["installed"] is True
    assert row["new_version"]


def test_skill_upgrade_up_to_date_when_content_matches(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """A second ``upgrade`` on byte-identical content reports ``UP-TO-DATE``."""
    _cwd, _fake_home = isolated_home
    first = runner.invoke(skill_app, ["upgrade", "--target=cursor", "--global"])
    assert first.exit_code == 0
    second = runner.invoke(
        skill_app,
        ["upgrade", "--target=cursor", "--global", "--json"],
    )
    assert second.exit_code == 0, _combined(second)
    payload = json.loads(_combined(second).strip().splitlines()[-1])
    assert payload[0]["up_to_date"] is True
