"""Default-lane tests for :mod:`popolaloom.evolution.skill_install` (Stage S4 of v0.5.0).

Per the v0.5.0-plan §S4.G test contract — covers the install API
contract (happy path, dry-run, idempotency, registry shape) plus the
internal helpers (frontmatter version parsing, scope fallback) so the
default-lane coverage stays ≥ 91 %.

All tests use ``tmp_path`` + ``monkeypatch`` so they never touch the
developer's real ``~/.cursor/`` etc.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from popolaloom.evolution.skill_inject import SKILL_TARGETS
from popolaloom.evolution.skill_install import (
    VERSION_MARKER_FILENAME,
    InstallOutcome,
    _parse_frontmatter_version,
    _resolve_scope_with_fallback,
    _write_marker,
    install_all_skills,
    install_skill,
)


@pytest.fixture
def isolated_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, Path]]:
    """Yield ``(cwd, fake_home)`` with ``Path.home()``/``Path.cwd()`` patched.

    Mirrors the fixture used by ``tests/cli/test_init_cmd.py`` so the
    install tests run in the same sandbox style — no CODEX_HOME leak,
    real ``~/`` untouched.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("CODEX_HOME", raising=False)

    yield cwd, fake_home


def test_install_skill_writes_skill_md_and_marker(
    isolated_home: tuple[Path, Path],
) -> None:
    """``install_skill('cursor', scope='global')`` writes the SKILL.md + a version marker."""
    _cwd, fake_home = isolated_home
    outcome = install_skill("cursor", scope="global")

    expected_path = fake_home / ".cursor" / "skills" / "popolaloom" / "SKILL.md"

    assert isinstance(outcome, InstallOutcome)
    assert outcome.installed is True
    assert outcome.skipped is False
    assert outcome.would_write is None
    assert outcome.target == "cursor"
    assert outcome.scope == "global"
    assert outcome.target_path == expected_path
    assert outcome.bytes is not None and outcome.bytes > 0

    assert expected_path.is_file()
    body = expected_path.read_text(encoding="utf-8")
    assert body.startswith("---\nname: popolaloom\n")

    marker = expected_path.parent / VERSION_MARKER_FILENAME
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").strip()


def test_install_skill_dry_run_does_not_write(
    isolated_home: tuple[Path, Path],
) -> None:
    """``dry_run=True`` populates ``would_write`` and never writes the SKILL.md."""
    _cwd, fake_home = isolated_home
    outcome = install_skill("claude", scope="global", dry_run=True)

    expected_path = fake_home / ".claude" / "skills" / "popolaloom" / "SKILL.md"

    assert outcome.installed is False
    assert outcome.skipped is False
    assert outcome.would_write == expected_path
    assert outcome.target_path == expected_path
    assert outcome.bytes is None

    assert not expected_path.exists()
    assert not (expected_path.parent / VERSION_MARKER_FILENAME).exists()


def test_install_skill_idempotent_second_run_skips(
    isolated_home: tuple[Path, Path],
) -> None:
    """Second ``install_skill`` on byte-identical content returns ``skipped=True``."""
    _cwd, fake_home = isolated_home

    first = install_skill("cursor", scope="global")
    assert first.installed is True

    target = fake_home / ".cursor" / "skills" / "popolaloom" / "SKILL.md"
    mtime_before = target.stat().st_mtime

    second = install_skill("cursor", scope="global")
    assert second.installed is False
    assert second.skipped is True
    assert second.target_path == target
    assert second.bytes == first.bytes
    assert "already installed" in (second.reason or "")

    # Idempotent re-install must not bump the mtime (workspace rule:
    # idempotent ops are observable as no-ops).
    assert target.stat().st_mtime == mtime_before


def test_skill_targets_registry_shape() -> None:
    """:data:`SKILL_TARGETS` registry exposes the documented (target × scope) matrix.

    Per v0.5.0-plan §S4.A:

    * ``cursor`` and ``claude`` support both ``global`` and ``project``.
    * ``codex`` supports only ``global`` (uses ``$CODEX_HOME`` or ``~/.codex``).
    * ``copilot`` supports only ``project`` (single-file in
      ``<cwd>/.github/copilot-instructions.md``).

    Plus: every leaf is callable (zero-args) and returns a Path.
    """
    assert set(SKILL_TARGETS) == {"cursor", "claude", "codex", "copilot"}

    assert set(SKILL_TARGETS["cursor"]) == {"global", "project"}
    assert set(SKILL_TARGETS["claude"]) == {"global", "project"}
    assert set(SKILL_TARGETS["codex"]) == {"global"}
    assert set(SKILL_TARGETS["copilot"]) == {"project"}

    for target, scopes in SKILL_TARGETS.items():
        for scope, resolver in scopes.items():
            assert callable(resolver), (
                f"SKILL_TARGETS[{target!r}][{scope!r}] must be callable; "
                f"got {type(resolver).__name__}"
            )
            result = resolver()
            assert isinstance(result, Path), (
                f"SKILL_TARGETS[{target!r}][{scope!r}]() must return Path; "
                f"got {type(result).__name__}"
            )


def test_install_skill_copilot_global_falls_back_to_project(
    isolated_home: tuple[Path, Path],
) -> None:
    """``install_skill('copilot', 'global')`` downgrades to ``project`` + records reason."""
    cwd, _fake_home = isolated_home
    outcome = install_skill("copilot", scope="global")

    expected_path = cwd / ".github" / "copilot-instructions.md"

    assert outcome.scope == "project"
    assert outcome.target_path == expected_path
    assert outcome.installed is True
    assert outcome.reason is not None
    assert "fall" in outcome.reason.lower() or "back" in outcome.reason.lower()

    assert expected_path.is_file()


def test_install_skill_unknown_target_raises_keyerror(
    isolated_home: tuple[Path, Path],
) -> None:
    """Unknown target names raise :class:`KeyError` (S-5: explicit error)."""
    with pytest.raises(KeyError) as excinfo:
        install_skill("nonexistent-ide", scope="global")
    assert "valid targets" in str(excinfo.value)


def test_install_all_skills_iterates_every_target(
    isolated_home: tuple[Path, Path],
) -> None:
    """``install_all_skills`` returns one outcome per :data:`SKILL_TARGETS` key."""
    outcomes = install_all_skills(scope="project")
    assert {o.target for o in outcomes} == set(SKILL_TARGETS)
    for outcome in outcomes:
        assert outcome.target_path.is_file()


def test_install_all_skills_dry_run_does_not_write(
    isolated_home: tuple[Path, Path],
) -> None:
    """``install_all_skills(dry_run=True)`` returns ``would_write`` without writing."""
    outcomes = install_all_skills(scope="global", dry_run=True)
    assert len(outcomes) == len(SKILL_TARGETS)
    for outcome in outcomes:
        assert outcome.would_write == outcome.target_path
        assert not outcome.target_path.exists()


def test_parse_frontmatter_version_handles_no_frontmatter() -> None:
    """When content lacks a leading ``---``, the parser returns the wheel version."""
    from popolaloom import __version__

    assert _parse_frontmatter_version("body without frontmatter\n") == __version__


def test_parse_frontmatter_version_handles_unterminated_block() -> None:
    """Frontmatter with a missing closing ``---`` falls back to the wheel version."""
    from popolaloom import __version__

    assert _parse_frontmatter_version("---\nname: x\nversion: 1.2.3\n") == __version__


def test_parse_frontmatter_version_handles_no_version_line() -> None:
    """Frontmatter without a ``version:`` line falls back to the wheel version."""
    from popolaloom import __version__

    text = "---\nname: popolaloom\ndescription: x\n---\nbody\n"
    assert _parse_frontmatter_version(text) == __version__


def test_parse_frontmatter_version_strips_quotes() -> None:
    """Quoted frontmatter version values are unwrapped."""
    text = "---\nname: popolaloom\nversion: '1.2.3'\n---\nbody\n"
    assert _parse_frontmatter_version(text) == "1.2.3"


def test_resolve_scope_with_fallback_unknown_target_raises() -> None:
    """``_resolve_scope_with_fallback`` raises KeyError on unknown target."""
    with pytest.raises(KeyError):
        _resolve_scope_with_fallback("nonexistent", "global")


def test_write_marker_skips_when_already_correct(tmp_path: Path) -> None:
    """``_write_marker`` is a no-op when the marker already holds the right value."""
    marker = tmp_path / VERSION_MARKER_FILENAME
    marker.write_text("1.2.3\n", encoding="utf-8")
    mtime_before = marker.stat().st_mtime

    _write_marker(tmp_path, "1.2.3")

    assert marker.stat().st_mtime == mtime_before
    assert marker.read_text(encoding="utf-8") == "1.2.3\n"


def test_write_marker_overwrites_on_version_change(tmp_path: Path) -> None:
    """``_write_marker`` rewrites the marker when the version differs."""
    marker = tmp_path / VERSION_MARKER_FILENAME
    marker.write_text("0.9.0\n", encoding="utf-8")

    _write_marker(tmp_path, "1.2.3")

    assert marker.read_text(encoding="utf-8") == "1.2.3\n"


def test_install_skill_dry_run_idempotent_does_not_create_dirs(
    isolated_home: tuple[Path, Path],
) -> None:
    """Two consecutive ``install_skill(..., dry_run=True)`` calls leave disk untouched."""
    _cwd, fake_home = isolated_home
    first = install_skill("cursor", scope="global", dry_run=True)
    second = install_skill("cursor", scope="global", dry_run=True)
    assert first.would_write == second.would_write
    assert not (fake_home / ".cursor").exists()
