"""Default-lane tests for ``popola init`` (Stage S2 of v0.5.0).

Covers the 14-row DevolaFlow-style verb + modifier matrix per Q5-2 lock.
Each test runs in an isolated ``tmp_path`` and monkey-patches
:func:`pathlib.Path.home` and :func:`pathlib.Path.cwd` so the install
verbs never touch the developer's real ``~/.cursor/`` or ``~/.claude/``
directories.

Suite shape (16 cases):

* 8 happy-path verb tests (cursor / claude / copilot / codex / local /
  all / --list / auto-detect with --dry-run).
* 5 modifier tests (--global / --project, --mode={core,standard,full}).
* 2 idempotency tests (cursor + local — second run is a no-op).
* 1 error-path test (invalid --mode → BadParameter exit).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from popolaloom.cli.init_cmd import app as init_app


@pytest.fixture
def isolated_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, Path]]:
    """Yield ``(cwd, fake_home)`` with ``Path.home()`` + ``Path.cwd()`` patched.

    The patches are bidirectional: install verbs that use ``Path.home()``
    (cursor --global, claude --global, codex) hit ``fake_home`` and
    install verbs that use ``Path.cwd()`` (cursor --project, local) hit
    ``cwd``.  The ``CODEX_HOME`` environment variable is also unset so
    ``codex_target_path()`` falls back to ``Path.home() / .codex``.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("CODEX_HOME", raising=False)

    yield cwd, fake_home


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _combined_output(result: object) -> str:
    """Return ``result.stdout`` + best-effort ``result.stderr``.

    Newer ``click`` releases changed the ``CliRunner`` API: ``mix_stderr``
    is gone and ``result.stderr`` is sometimes only populated when
    ``mix_stderr=False`` is supported.  This helper papers over the
    differences so a single test asserts ``"some text" in
    _combined_output(result)`` against either click 8.x line.
    """
    stdout = getattr(result, "stdout", "") or ""
    try:
        stderr = getattr(result, "stderr", "") or ""
    except (ValueError, AttributeError):
        stderr = ""
    output = getattr(result, "output", "") or ""
    return stdout + stderr + output


# ── verb happy-paths ──────────────────────────────────────────────────────


def test_init_cursor_project_writes_skill(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """`popola init cursor --project` writes the SKILL.md to <cwd>/.cursor/."""
    cwd, _fake_home = isolated_home
    result = runner.invoke(init_app, ["cursor", "--project"])
    assert result.exit_code == 0, _combined_output(result)
    target = cwd / ".cursor" / "skills" / "popolaloom" / "SKILL.md"
    assert target.exists()
    body = target.read_text(encoding="utf-8")
    assert body.startswith("---\nname: popolaloom\n")
    marker = target.parent / ".popolaloom-version"
    assert marker.exists()
    assert "0." in marker.read_text(encoding="utf-8")


def test_init_claude_global_writes_to_home(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """`popola init claude --global` writes the SKILL.md to ~/.claude/."""
    _cwd, fake_home = isolated_home
    result = runner.invoke(init_app, ["claude", "--global"])
    assert result.exit_code == 0, _combined_output(result)
    target = fake_home / ".claude" / "skills" / "popolaloom" / "SKILL.md"
    assert target.exists()
    assert "name: popolaloom" in target.read_text(encoding="utf-8")


def test_init_copilot_writes_single_file(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """`popola init copilot` writes <cwd>/.github/copilot-instructions.md."""
    cwd, _fake_home = isolated_home
    result = runner.invoke(init_app, ["copilot"])
    assert result.exit_code == 0, _combined_output(result)
    target = cwd / ".github" / "copilot-instructions.md"
    assert target.exists()
    assert target.read_text(encoding="utf-8").startswith("---\nname: popolaloom\n")


def test_init_codex_uses_home_dot_codex(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """`popola init codex` writes ~/.codex/skills/popolaloom/SKILL.md."""
    _cwd, fake_home = isolated_home
    result = runner.invoke(init_app, ["codex"])
    assert result.exit_code == 0, _combined_output(result)
    target = fake_home / ".codex" / "skills" / "popolaloom" / "SKILL.md"
    assert target.exists()


def test_init_codex_honors_codex_home_env(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`popola init codex` honors $CODEX_HOME when set."""
    custom = tmp_path / "custom-codex"
    custom.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(custom))
    result = runner.invoke(init_app, ["codex"])
    assert result.exit_code == 0, _combined_output(result)
    target = custom / "skills" / "popolaloom" / "SKILL.md"
    assert target.exists()


def test_init_local_mode_core_creates_canonical_paths(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """`popola init local --mode=core` creates the 8 canonical paths."""
    cwd, _fake_home = isolated_home
    result = runner.invoke(init_app, ["local", "--mode=core"])
    assert result.exit_code == 0, _combined_output(result)

    expected_dirs = [
        cwd / ".local" / "feedbacks",
        cwd / ".local" / "tasks",
        cwd / ".local" / "memory",
        cwd / ".local" / ".agent" / "active",
        cwd / ".local" / ".agent" / "handoff",
        cwd / ".local" / ".agent" / "archive",
    ]
    for d in expected_dirs:
        assert d.is_dir(), f"missing dir: {d}"

    expected_files = [
        cwd / ".local" / "feedbacks" / "TRACKER.md",
        cwd / ".local" / "memory" / "MEMORY.md",
        cwd / ".local" / "index.md",
    ]
    for f in expected_files:
        assert f.is_file(), f"missing file: {f}"

    assert "SKIP compile" in _combined_output(result)
    assert not (cwd / ".local" / "tasks" / "example-dispatch.md").exists()


def test_init_local_mode_full_seeds_examples(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """`popola init local --mode=full` seeds the example task fixture."""
    cwd, _fake_home = isolated_home
    result = runner.invoke(init_app, ["local", "--mode=full"])
    assert result.exit_code == 0, _combined_output(result)
    example = cwd / ".local" / "tasks" / "example-dispatch.md"
    assert example.is_file()
    body = example.read_text(encoding="utf-8")
    assert "popola dispatch" in body


def test_init_all_installs_every_ide_target(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """`popola init all` installs cursor + claude + copilot + codex (no `local`)."""
    cwd, fake_home = isolated_home
    result = runner.invoke(init_app, ["all", "--project"])
    assert result.exit_code == 0, _combined_output(result)

    assert (cwd / ".cursor" / "skills" / "popolaloom" / "SKILL.md").is_file()
    assert (cwd / ".claude" / "skills" / "popolaloom" / "SKILL.md").is_file()
    assert (cwd / ".github" / "copilot-instructions.md").is_file()
    assert (fake_home / ".codex" / "skills" / "popolaloom" / "SKILL.md").is_file()
    assert not (cwd / ".local").exists()


def test_init_list_prints_table_no_writes(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """`popola init --list` prints a table and writes nothing."""
    cwd, _fake_home = isolated_home
    result = runner.invoke(init_app, ["--list"])
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "popola init — detected targets" in out
    assert "placeholder stub (S2)" in out
    assert not (cwd / ".cursor").exists()
    assert not (cwd / ".local").exists()


def test_init_dry_run_no_writes(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """`popola init cursor --dry-run` prints intent without writing."""
    cwd, _fake_home = isolated_home
    result = runner.invoke(init_app, ["cursor", "--project", "--dry-run"])
    assert result.exit_code == 0, _combined_output(result)
    assert "DRY" in _combined_output(result)
    assert not (cwd / ".cursor" / "skills" / "popolaloom" / "SKILL.md").exists()


def test_init_no_args_auto_detects_targets(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """`popola init` (no args) runs auto-detect and dispatches to detected targets."""
    cwd, _fake_home = isolated_home
    (cwd / ".cursor").mkdir()
    (cwd / ".claude").mkdir()

    result = runner.invoke(init_app, [])
    assert result.exit_code == 0, _combined_output(result)
    assert "auto-detected targets" in _combined_output(result)
    assert (cwd / ".cursor" / "skills" / "popolaloom" / "SKILL.md").is_file()
    assert (cwd / ".claude" / "skills" / "popolaloom" / "SKILL.md").is_file()


# ── modifier tests ──────────────────────────────────────────────────────


def test_init_cursor_global_and_project_conflict_errors(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """`popola init cursor --global --project` exits with a BadParameter error."""
    result = runner.invoke(init_app, ["cursor", "--global", "--project"])
    assert result.exit_code != 0
    assert "mutually exclusive" in _combined_output(result)


def test_init_local_invalid_mode_errors(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """`popola init local --mode=bogus` exits with a BadParameter error."""
    result = runner.invoke(init_app, ["local", "--mode=bogus"])
    assert result.exit_code != 0
    assert "must be one of" in _combined_output(result)


def test_init_local_explicit_with_examples_overrides_mode_core(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """`--with-examples` explicit-beats-implicit over `--mode=core`."""
    cwd, _fake_home = isolated_home
    result = runner.invoke(
        init_app,
        ["local", "--mode=core", "--with-examples"],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert (cwd / ".local" / "tasks" / "example-dispatch.md").is_file()


# ── idempotency ─────────────────────────────────────────────────────────


def test_init_cursor_idempotent_second_run_skips(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """Second `popola init cursor` run prints SKIP, never overwrites."""
    cwd, _fake_home = isolated_home
    first = runner.invoke(init_app, ["cursor", "--project"])
    assert first.exit_code == 0
    target = cwd / ".cursor" / "skills" / "popolaloom" / "SKILL.md"
    target.write_text("MARKER FROM USER\n", encoding="utf-8")
    mtime_before = target.stat().st_mtime

    second = runner.invoke(init_app, ["cursor", "--project"])
    assert second.exit_code == 0
    out2 = _combined_output(second)
    assert "SKIP" in out2
    assert "already installed" in out2
    assert target.read_text(encoding="utf-8") == "MARKER FROM USER\n"
    assert target.stat().st_mtime == mtime_before


def test_init_local_idempotent_second_run_skips(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """Second `popola init local` run prints SKIP for every existing path."""
    cwd, _fake_home = isolated_home
    first = runner.invoke(init_app, ["local", "--mode=standard"])
    assert first.exit_code == 0

    user_marker = cwd / ".local" / "feedbacks" / "TRACKER.md"
    user_marker.write_text("USER EDITED\n", encoding="utf-8")

    second = runner.invoke(init_app, ["local", "--mode=standard"])
    assert second.exit_code == 0
    assert "SKIP" in _combined_output(second)
    assert user_marker.read_text(encoding="utf-8") == "USER EDITED\n"
