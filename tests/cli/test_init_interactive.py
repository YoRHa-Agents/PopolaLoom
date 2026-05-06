"""Default-lane tests for ``popola init --interactive`` (v0.5.5 L5.B).

The ``--interactive`` flag triggers an interactive setup wizard
(``popolaloom.cli.init_cmd._run_interactive_wizard``). Because the
wizard is just :func:`typer.confirm` + :func:`typer.prompt` chained
together, we exercise it via :class:`typer.testing.CliRunner` with
stdin injection (``input="..."``) per the
[Typer testing guide](https://typer.tiangolo.com/tutorial/testing/).

Suite shape (6 cases):

1. ``--interactive`` lone happy path — accept defaults for cursor +
   claude + copilot, decline the rest, default-yes on local. Confirms
   the wizard wrote the SKILL.md to <cwd>/.cursor + <cwd>/.claude
   + <cwd>/.github + scaffolded ``.local/``.
2. Wizard with operator declining ALL prompts produces no writes
   (idempotency contract: declining the wizard is safe).
3. ``--interactive`` combined with a verb subcommand → BadParameter
   (mutual-exclusion guard).
4. Wizard offers ``project`` + ``global`` scope and the operator
   choosing ``G`` lands the SKILL.md under ``~/.cursor`` (global
   path) instead of ``<cwd>/.cursor``.
5. Wizard's "Proceed?" no-confirm path leaves the filesystem
   untouched (operator can back out at the final confirm).
6. Wizard with no IDEs detected + operator confirming cursor lands
   the SKILL.md (defaults are reasonable on a fresh repo).
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
    """Yield ``(cwd, fake_home)`` with ``Path.home`` + ``Path.cwd`` patched.

    Mirrors the ``tests/cli/test_init_cmd.py`` fixture so wizard tests
    never touch the developer's real ``~/.cursor`` or ``~/.claude``.
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
    """Return ``result.stdout`` + best-effort ``result.stderr`` + ``output``."""
    stdout = getattr(result, "stdout", "") or ""
    try:
        stderr = getattr(result, "stderr", "") or ""
    except (ValueError, AttributeError):
        stderr = ""
    output = getattr(result, "output", "") or ""
    return stdout + stderr + output


def _build_input(answers: list[str]) -> str:
    """Join wizard answers with newlines; trailing newline closes prompt."""
    return "\n".join(answers) + "\n"


# ── 1. happy-path: accept defaults for cursor+claude+copilot, decline codex ──


def test_interactive_wizard_happy_path_writes_detected_targets(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """Wizard with auto-detected IDEs + accept-all dispatches every target."""
    cwd, _fake_home = isolated_home
    (cwd / ".cursor").mkdir()
    (cwd / ".claude").mkdir()
    (cwd / ".github").mkdir()

    answers = [
        "y",   # Install for Cursor?  (default Y because detected)
        "P",   # Scope for cursor (default P because detected)
        "y",   # Install for Claude?
        "P",   # Scope for claude
        "y",   # Install for Copilot? (no scope prompt — project-only)
        "n",   # Install for Codex?  (default N — not detected)
        "y",   # Scaffold .local/?   (default Y — .local/ missing)
        "y",   # Proceed with this plan?
    ]
    result = runner.invoke(init_app, ["--interactive"], input=_build_input(answers))
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "PopolaLoom interactive setup wizard" in out
    assert "Install plan:" in out
    assert "Interactive setup complete." in out

    assert (cwd / ".cursor" / "skills" / "popola-loom" / "SKILL.md").is_file()
    assert (cwd / ".claude" / "skills" / "popola-loom" / "SKILL.md").is_file()
    assert (cwd / ".github" / "copilot-instructions.md").is_file()
    assert (cwd / ".local" / "feedbacks").is_dir()


# ── 2. decline-all: no writes ─────────────────────────────────────────────


def test_interactive_wizard_decline_all_writes_nothing(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """Operator declining every prompt leaves the filesystem untouched."""
    cwd, _fake_home = isolated_home
    answers = [
        "n",  # Install for Cursor?
        "n",  # Install for Claude?
        "n",  # Install for Copilot?
        "n",  # Install for Codex?
        "n",  # Scaffold .local/?
    ]
    result = runner.invoke(init_app, ["--interactive"], input=_build_input(answers))
    assert result.exit_code == 0, _combined_output(result)
    assert "Nothing selected" in _combined_output(result)
    assert not (cwd / ".cursor").exists()
    assert not (cwd / ".claude").exists()
    assert not (cwd / ".github").exists()
    assert not (cwd / ".local").exists()


# ── 3. --interactive with verb → BadParameter ─────────────────────────────


def test_interactive_with_verb_subcommand_errors(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``popola init --interactive cursor`` exits with a BadParameter."""
    result = runner.invoke(init_app, ["--interactive", "cursor"])
    assert result.exit_code != 0
    assert "cannot be combined with a verb subcommand" in _combined_output(result)


# ── 4. global-scope path exercise ──────────────────────────────────────────


def test_interactive_wizard_global_scope_lands_under_home(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """Operator picking ``G`` for cursor lands the SKILL.md in ~/.cursor."""
    cwd, fake_home = isolated_home
    answers = [
        "y",  # Install for Cursor?
        "G",  # Scope for cursor → global
        "n",  # Install for Claude?
        "n",  # Install for Copilot?
        "n",  # Install for Codex?
        "n",  # Scaffold .local/?
        "y",  # Proceed?
    ]
    result = runner.invoke(init_app, ["--interactive"], input=_build_input(answers))
    assert result.exit_code == 0, _combined_output(result)
    target = fake_home / ".cursor" / "skills" / "popola-loom" / "SKILL.md"
    assert target.is_file()
    project_target = cwd / ".cursor" / "skills" / "popola-loom" / "SKILL.md"
    assert not project_target.exists()


# ── 5. operator backs out at the final "Proceed?" prompt ───────────────────


def test_interactive_wizard_operator_aborts_proceed(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """Operator answering ``n`` to "Proceed?" cancels the entire plan."""
    cwd, _fake_home = isolated_home
    (cwd / ".cursor").mkdir()
    answers = [
        "y",  # Install for Cursor?
        "P",  # Scope project
        "n",  # Install for Claude?
        "n",  # Install for Copilot?
        "n",  # Install for Codex?
        "n",  # Scaffold .local/?
        "n",  # Proceed?
    ]
    result = runner.invoke(init_app, ["--interactive"], input=_build_input(answers))
    assert result.exit_code == 0, _combined_output(result)
    assert "Aborted by operator" in _combined_output(result)
    assert not (cwd / ".cursor" / "skills" / "popola-loom" / "SKILL.md").exists()


# ── 6. fresh repo (nothing detected) → cursor-friendly defaults ───────────


def test_interactive_wizard_no_detection_offers_cursor_default(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """With no IDE detected, the wizard's default-Yes for cursor lets a single
    Enter sequence install cursor (project)."""
    cwd, _fake_home = isolated_home
    # Pre-create .local/ so _auto_detect doesn't suggest "local" — then the
    # wizard truly sees "no IDEs detected" and falls back to cursor-friendly
    # defaults (the ``ide == "cursor" and not detected`` branch fires).
    (cwd / ".local").mkdir()
    answers = [
        "y",  # Install for Cursor? (default Y because cursor-fallback)
        "P",  # Scope for cursor (default P)
        "n",  # Install for Claude?
        "n",  # Install for Copilot?
        "n",  # Install for Codex?
        "n",  # Scaffold .local/?  (already exists)
        "y",  # Proceed?
    ]
    result = runner.invoke(init_app, ["--interactive"], input=_build_input(answers))
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "Auto-detected: (none" in out
    assert (cwd / ".cursor" / "skills" / "popola-loom" / "SKILL.md").is_file()
