"""Edge-case tests for ``popola init`` (v0.5.4 Loop 4 — L4.B).

Per release-notes-v0.5.4.md L4.B: the round-4 mutation-surface expansion
adds ``cli/init_cmd.py`` to ``[tool.mutmut].paths_to_mutate``. This test
file targets the previously-undertested branches the live mutmut run
would prod first:

1. Auto-detect on a fresh repo with no detected IDEs falls back to
   cursor (line 552-553).
2. ``--list`` combined with a verb subcommand raises BadParameter
   (line 541).
3. ``--list`` shows ``Detected by auto-detect: (none)`` when nothing
   is detected.
4. ``--list`` does not write the install paths.
5. Auto-detect picks up ``copilot`` when ``.github`` exists (line 162-163).
6. Auto-detect picks up ``codex`` when ``~/.codex`` exists (line 164-165).
7. Auto-detect target ``local`` is dispatched into ``_install_local``
   when present (line 557-558 branch).
8. ``init all`` with ``--global`` plus existing files only writes the
   missing ones (idempotency holds across all four IDEs).
9. ``--mode=full`` with ``--with-examples`` also seeds the example task
   when both flags are set (explicit-with-examples honours mode-full).
10. ``--no-with-examples`` overrides ``--mode=full`` (explicit-beats-
    implicit, opposite direction from the existing
    ``test_init_local_explicit_with_examples_overrides_mode_core``).
11. ``init copilot --dry-run`` prints DRY but doesn't write a file.
12. ``init local --dry-run`` prints DRY paths and writes nothing.
13. ``init`` (no args, no detection) message shows the cursor fallback
    explicitly.
14. ``init local`` second run prints SKIP for the marker file (not
    just dirs).

Each test runs in an isolated tmp_path with monkey-patched
``Path.home`` + ``Path.cwd``; total cost is ~ 1 s for the file.
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
    """Yield ``(cwd, fake_home)`` with ``Path.home()`` + ``Path.cwd()`` patched."""
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
    """Return ``result.stdout`` + best-effort ``result.stderr``."""
    stdout = getattr(result, "stdout", "") or ""
    try:
        stderr = getattr(result, "stderr", "") or ""
    except (ValueError, AttributeError):
        stderr = ""
    output = getattr(result, "output", "") or ""
    return stdout + stderr + output


# ── auto-detect edge cases ──────────────────────────────────────────────


def test_init_auto_detect_no_ides_falls_back_to_cursor(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """`popola init` with NO detected IDEs falls back to cursor + prints the
    "No AI tools detected" message (lines 551-553).

    The fixture's ``cwd`` has no ``.cursor / .claude / .github`` and no
    ``~/.codex/`` either; ``_auto_detect`` returns only ``["local"]``
    because ``.local`` is absent (line 166-167) — but ``init`` strips
    ``local`` from the auto-detect default (it's opt-in via
    ``init local``) ... actually no: re-reading
    ``init_cmd.py:550-560`` the callback dispatches ``local`` to
    ``_install_local`` and other names to ``_install_target``. The
    "no detected" message ONLY fires when ``detected`` is fully empty.
    To force that we must also create a ``.local/`` so the fallback
    branch has ZERO entries to hit.
    """
    cwd, _fake_home = isolated_home
    (cwd / ".local").mkdir()

    result = runner.invoke(init_app, [])
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "No AI tools detected" in out
    assert (cwd / ".cursor" / "skills" / "popolaloom" / "SKILL.md").is_file()


def test_init_auto_detect_picks_up_github_dir_for_copilot(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """`.github/` directory triggers copilot auto-detect (line 162-163)."""
    cwd, _fake_home = isolated_home
    (cwd / ".github").mkdir()
    (cwd / ".local").mkdir()

    result = runner.invoke(init_app, [])
    assert result.exit_code == 0, _combined_output(result)
    assert (cwd / ".github" / "copilot-instructions.md").is_file()


def test_init_auto_detect_picks_up_dot_codex_dir(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``~/.codex`` directory triggers codex auto-detect (line 164-165)."""
    _cwd, fake_home = isolated_home
    (fake_home / ".codex").mkdir()
    (_cwd / ".local").mkdir()

    result = runner.invoke(init_app, [])
    assert result.exit_code == 0, _combined_output(result)
    target = fake_home / ".codex" / "skills" / "popolaloom" / "SKILL.md"
    assert target.is_file()


def test_init_auto_detect_dispatches_local_when_missing_local_dir(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """When ``.local`` is absent, auto-detect adds it AND dispatches to
    ``_install_local`` (line 557-558 branch).
    """
    cwd, _fake_home = isolated_home
    result = runner.invoke(init_app, [])
    assert result.exit_code == 0, _combined_output(result)
    assert (cwd / ".local" / "feedbacks").is_dir()
    assert (cwd / ".local" / "tasks").is_dir()
    assert (cwd / ".local" / "memory").is_dir()


# ── --list edge cases ────────────────────────────────────────────────────


def test_init_list_combined_with_verb_subcommand_errors(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``popola init --list cursor`` raises BadParameter (line 541-543).

    ``--list`` is mutually exclusive with verb subcommands; passing both
    must surface explicitly.
    """
    result = runner.invoke(init_app, ["--list", "cursor"])
    assert result.exit_code != 0
    assert "cannot be combined" in _combined_output(result)


def test_init_list_on_fresh_repo_shows_no_detected(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``popola init --list`` on a fresh repo (no IDEs, .local present)
    shows ``Detected by auto-detect: (none)``.
    """
    cwd, _fake_home = isolated_home
    (cwd / ".local").mkdir()

    result = runner.invoke(init_app, ["--list"])
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "(none)" in out
    assert "popola init — detected targets" in out
    assert not (cwd / ".cursor").exists()
    assert not (cwd / ".claude").exists()


# ── modifier + scope edge cases ──────────────────────────────────────────


def test_init_copilot_dry_run_no_write(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``init copilot --dry-run`` prints DRY but does NOT write the file."""
    cwd, _fake_home = isolated_home
    result = runner.invoke(init_app, ["copilot", "--dry-run"])
    assert result.exit_code == 0, _combined_output(result)
    assert "DRY" in _combined_output(result)
    assert not (cwd / ".github" / "copilot-instructions.md").exists()


def test_init_local_dry_run_no_write(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``init local --dry-run`` prints DRY paths and writes nothing."""
    cwd, _fake_home = isolated_home
    result = runner.invoke(init_app, ["local", "--dry-run"])
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "DRY" in out
    assert not (cwd / ".local" / "feedbacks").exists()
    assert not (cwd / ".local" / "tasks").exists()


def test_init_local_no_with_examples_overrides_mode_full(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``--no-with-examples`` overrides ``--mode=full`` (explicit-beats-implicit
    in the OPPOSITE direction from
    ``test_init_local_explicit_with_examples_overrides_mode_core``).
    """
    cwd, _fake_home = isolated_home
    result = runner.invoke(
        init_app,
        ["local", "--mode=full", "--no-with-examples"],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert (cwd / ".local" / "feedbacks").is_dir()
    assert not (cwd / ".local" / "tasks" / "example-dispatch.md").exists()


def test_init_local_mode_full_with_explicit_with_examples_seeds(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``--mode=full --with-examples`` (both set) keeps the seed (mode-full
    default + explicit-True is consistent, not a conflict).
    """
    cwd, _fake_home = isolated_home
    result = runner.invoke(
        init_app,
        ["local", "--mode=full", "--with-examples"],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert (cwd / ".local" / "tasks" / "example-dispatch.md").is_file()


def test_init_copilot_idempotent_user_overwrites_preserved(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """Second ``popola init copilot`` run with user content present prints
    SKIP and preserves the user's content (pinpoints
    ``_write_skill`` line 237-239 SKIP branch for the copilot path
    specifically).
    """
    cwd, _fake_home = isolated_home
    first = runner.invoke(init_app, ["copilot"])
    assert first.exit_code == 0
    target = cwd / ".github" / "copilot-instructions.md"
    target.write_text("HUMAN OVERRODE\n", encoding="utf-8")

    second = runner.invoke(init_app, ["copilot"])
    assert second.exit_code == 0
    out = _combined_output(second)
    assert "SKIP" in out
    assert "already installed" in out
    assert target.read_text(encoding="utf-8") == "HUMAN OVERRODE\n"


def test_init_all_idempotent_second_run_skips_all(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``init all`` second run prints SKIP for all four IDEs (locks in
    that the per-IDE idempotency dispatcher fires for every entry in
    ``_install_all`` rather than short-circuiting after the first SKIP).
    """
    cwd, fake_home = isolated_home
    first = runner.invoke(init_app, ["all", "--project"])
    assert first.exit_code == 0
    second = runner.invoke(init_app, ["all", "--project"])
    assert second.exit_code == 0, _combined_output(second)
    out2 = _combined_output(second)
    skip_count = out2.count("SKIP")
    assert skip_count >= 4, (
        f"expected ≥ 4 SKIP messages (cursor + claude + copilot + codex), got {skip_count}\n"
        f"output:\n{out2}"
    )
    for path in (
        cwd / ".cursor" / "skills" / "popolaloom" / "SKILL.md",
        cwd / ".claude" / "skills" / "popolaloom" / "SKILL.md",
        cwd / ".github" / "copilot-instructions.md",
        fake_home / ".codex" / "skills" / "popolaloom" / "SKILL.md",
    ):
        assert path.is_file()


# ── error path: unknown target via direct helper ─────────────────────────


def test_install_target_rejects_unknown_string() -> None:
    """``_install_target`` rejects an unknown target string with BadParameter
    (line 287-288 raise branch).

    This branch is unreachable via the Typer wiring (the verbs are fixed
    via ``@app.command``) but is a defensive guard for direct callers.
    Mutmut's ``raise → pass`` mutation on this line would otherwise
    survive without a direct unit test.
    """
    import typer

    from popolaloom.cli.init_cmd import _install_target

    with pytest.raises(typer.BadParameter, match="unknown install target"):
        _install_target("kimi", scope="project", cwd=Path("/tmp"), dry_run=True)


# ── _write_marker dry-run + already-exists branches ──────────────────────


def test_write_marker_dry_run_does_not_write(
    tmp_path: Path,
) -> None:
    """``_write_marker(..., dry_run=True)`` prints DRY but does NOT write
    (lines 256-258 dry-run early-return branch).
    """
    from popolaloom.cli.init_cmd import _write_marker

    install_dir = tmp_path / "skills" / "popolaloom"
    install_dir.mkdir(parents=True)
    _write_marker(install_dir, dry_run=True)
    assert not (install_dir / ".popolaloom-version").exists()


def test_write_marker_skips_when_already_exists(
    tmp_path: Path,
) -> None:
    """``_write_marker`` is a no-op when the marker file already exists
    (line 259-260 already-exists guard).
    """
    from popolaloom.cli.init_cmd import _write_marker

    install_dir = tmp_path / "skills" / "popolaloom"
    install_dir.mkdir(parents=True)
    marker = install_dir / ".popolaloom-version"
    marker.write_text("0.0.0-pinned\n", encoding="utf-8")
    _write_marker(install_dir, dry_run=False)
    assert marker.read_text(encoding="utf-8") == "0.0.0-pinned\n"


# ── _install_target copilot --global warning branch ──────────────────────


def test_install_target_copilot_global_warning(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``init all --global`` triggers the copilot fallback warning
    (line 281-283).

    Copilot is project-only; ``--global`` triggers a fallback warning
    + still writes the project-local file.
    """
    cwd, _fake_home = isolated_home
    result = runner.invoke(init_app, ["all", "--global"])
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "Copilot does not support --global" in out
    assert (cwd / ".github" / "copilot-instructions.md").is_file()


# ── _scaffold_path dry-run dir + file branches ───────────────────────────


def test_scaffold_path_dry_run_dir_no_create(
    tmp_path: Path,
) -> None:
    """``_scaffold_path(..., is_dir=True, dry_run=True)`` prints DRY without
    creating the directory (lines 343-345 dry-run dir branch).
    """
    from popolaloom.cli.init_cmd import _scaffold_path

    target = tmp_path / "fakedir"
    result = _scaffold_path(target, is_dir=True, dry_run=True)
    assert result == "DRY"
    assert not target.exists()


def test_scaffold_path_dry_run_file_no_create(
    tmp_path: Path,
) -> None:
    """``_scaffold_path(..., is_dir=False, dry_run=True)`` prints DRY without
    creating the file.
    """
    from popolaloom.cli.init_cmd import _scaffold_path

    target = tmp_path / "fakefile.md"
    result = _scaffold_path(
        target, is_dir=False, content="hello", dry_run=True
    )
    assert result == "DRY"
    assert not target.exists()


def test_scaffold_path_skip_when_dir_exists(
    tmp_path: Path,
) -> None:
    """``_scaffold_path(..., is_dir=True)`` returns SKIP for an existing dir
    (line 348-349 SKIP branch).
    """
    from popolaloom.cli.init_cmd import _scaffold_path

    target = tmp_path / "existing-dir"
    target.mkdir()
    result = _scaffold_path(target, is_dir=True, dry_run=False)
    assert result == "SKIP"


# ── _resolve_scope default branch ────────────────────────────────────────


def test_resolve_scope_default_is_project_when_neither_flag() -> None:
    """``_resolve_scope(False, False)`` returns the documented default
    (``project``); pins line 577 default-return branch.
    """
    from popolaloom.cli.init_cmd import _resolve_scope

    assert _resolve_scope(False, False) == "project"
    assert _resolve_scope(False, False, default="global") == "global"
