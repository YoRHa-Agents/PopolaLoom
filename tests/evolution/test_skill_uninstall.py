"""Default-lane tests for :mod:`popolaloom.evolution.skill_uninstall` (v0.8.4).

Per the v0.8.4 acceptance contract — covers the uninstall API
(happy path, idempotency, dry-run, scope fallback, all-target
aggregator, marker-removal contract, unknown-target error path,
dataclass invariants) so the new surface lands with the same
≥ 8-test floor as the v0.5.0 install / upgrade suites.

All tests use ``tmp_path`` + ``monkeypatch`` so they never touch the
developer's real ``~/.cursor/`` etc. — same fixture shape as
``tests/cli/test_skill_cmd.py`` so the two suites stay in lock-step.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path

import pytest

from popolaloom.evolution.skill_inject import SKILL_TARGETS
from popolaloom.evolution.skill_install import (
    VERSION_MARKER_FILENAME,
    install_all_skills,
    install_skill,
)
from popolaloom.evolution.skill_uninstall import (
    UninstallOutcome,
    uninstall_all_skills,
    uninstall_skill,
)


@pytest.fixture
def isolated_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, Path]]:
    """Yield ``(cwd, fake_home)`` with ``Path.home()``/``Path.cwd()`` patched."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("CODEX_HOME", raising=False)

    yield cwd, fake_home


def test_uninstall_cursor_global_removes_skill(
    isolated_home: tuple[Path, Path],
) -> None:
    """install → uninstall removes both the SKILL.md and the version marker."""
    _cwd, fake_home = isolated_home

    install_skill("cursor", scope="global")
    target = fake_home / ".cursor" / "skills" / "popola-loom" / "SKILL.md"
    marker = target.parent / VERSION_MARKER_FILENAME
    assert target.is_file()
    assert marker.is_file()

    outcome = uninstall_skill("cursor", scope="global")

    assert outcome.uninstalled is True
    assert outcome.skipped is False
    assert outcome.would_remove is None
    assert outcome.target == "cursor"
    assert outcome.scope == "global"
    assert outcome.target_path == target
    assert outcome.bytes_removed is not None and outcome.bytes_removed > 0
    assert outcome.removed_marker is True

    assert not target.exists()
    assert not marker.exists()


def test_uninstall_idempotent_when_absent(
    isolated_home: tuple[Path, Path],
) -> None:
    """Uninstalling a target that was never installed returns ``skipped=True``."""
    _cwd, fake_home = isolated_home

    outcome = uninstall_skill("cursor", scope="global")

    assert outcome.uninstalled is False
    assert outcome.skipped is True
    assert outcome.would_remove is None
    assert outcome.bytes_removed is None
    assert outcome.removed_marker is False
    reason = outcome.reason or ""
    assert "absent" in reason.lower() or "already" in reason.lower()
    expected_path = fake_home / ".cursor" / "skills" / "popola-loom" / "SKILL.md"
    assert outcome.target_path == expected_path
    assert not expected_path.exists()


def test_uninstall_dry_run_reports_path_no_io(
    isolated_home: tuple[Path, Path],
) -> None:
    """``dry_run=True`` populates ``would_remove`` and never unlinks the file."""
    _cwd, fake_home = isolated_home

    install_skill("claude", scope="global")
    target = fake_home / ".claude" / "skills" / "popola-loom" / "SKILL.md"
    marker = target.parent / VERSION_MARKER_FILENAME
    assert target.is_file()
    assert marker.is_file()

    outcome = uninstall_skill("claude", scope="global", dry_run=True)

    assert outcome.uninstalled is False
    assert outcome.skipped is False
    assert outcome.would_remove == target
    assert outcome.target_path == target
    assert outcome.bytes_removed is None
    assert outcome.removed_marker is False

    assert target.is_file()
    assert marker.is_file()


def test_uninstall_copilot_scope_fallback(
    isolated_home: tuple[Path, Path],
) -> None:
    """``uninstall_skill('copilot', scope='global')`` resolves to project + records reason."""
    cwd, _fake_home = isolated_home

    install_skill("copilot", scope="project")
    expected_path = cwd / ".github" / "copilot-instructions.md"
    assert expected_path.is_file()

    outcome = uninstall_skill("copilot", scope="global")

    assert outcome.scope == "project"
    assert outcome.target_path == expected_path
    assert outcome.uninstalled is True
    assert outcome.reason is not None
    reason_lc = outcome.reason.lower()
    assert "fall" in reason_lc or "back" in reason_lc

    assert not expected_path.exists()


def test_uninstall_all_skills_iterates_registry(
    isolated_home: tuple[Path, Path],
) -> None:
    """``uninstall_all_skills`` returns one outcome per registry key + every file is gone."""
    install_all_skills(scope="project")

    outcomes = uninstall_all_skills(scope="project")
    assert {o.target for o in outcomes} == set(SKILL_TARGETS)
    for outcome in outcomes:
        assert outcome.uninstalled is True
        assert not outcome.target_path.exists()


def test_uninstall_removes_marker_alongside_skill(
    isolated_home: tuple[Path, Path],
) -> None:
    """Marker is removed for cursor/claude/codex; copilot has no marker.

    Confirms the per-target marker contract: the four IDE installs
    fall into two shapes — directory installs (cursor / claude /
    codex) ship a SKILL.md plus a sibling ``.popola-loom-version``
    marker, and the single-file install (copilot) has only the flat
    ``copilot-instructions.md`` at ``.github/`` with no marker.
    """
    cwd, fake_home = isolated_home

    install_skill("cursor", scope="global")
    install_skill("claude", scope="global")
    install_skill("codex", scope="global")
    install_skill("copilot", scope="project")

    cursor_outcome = uninstall_skill("cursor", scope="global")
    claude_outcome = uninstall_skill("claude", scope="global")
    codex_outcome = uninstall_skill("codex", scope="global")
    copilot_outcome = uninstall_skill("copilot", scope="project")

    assert cursor_outcome.removed_marker is True
    assert claude_outcome.removed_marker is True
    assert codex_outcome.removed_marker is True
    assert copilot_outcome.removed_marker is False

    cursor_marker = fake_home / ".cursor" / "skills" / "popola-loom" / VERSION_MARKER_FILENAME
    claude_marker = fake_home / ".claude" / "skills" / "popola-loom" / VERSION_MARKER_FILENAME
    codex_marker = fake_home / ".codex" / "skills" / "popola-loom" / VERSION_MARKER_FILENAME
    assert not cursor_marker.exists()
    assert not claude_marker.exists()
    assert not codex_marker.exists()
    assert not (cwd / ".github" / "copilot-instructions.md").exists()


def test_uninstall_unknown_target_raises_keyerror(
    isolated_home: tuple[Path, Path],
) -> None:
    """Unknown target names raise :class:`KeyError` (S-5: explicit error)."""
    with pytest.raises(KeyError) as excinfo:
        uninstall_skill("nonexistent-ide", scope="global")
    assert "valid targets" in str(excinfo.value)


def test_uninstall_outcome_dataclass_immutable() -> None:
    """``UninstallOutcome`` is frozen and exposes the documented fields."""
    outcome = UninstallOutcome(
        target="cursor",
        scope="global",
        target_path=Path("/tmp/popola-test/SKILL.md"),
        uninstalled=True,
        bytes_removed=512,
        removed_marker=True,
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.uninstalled = False  # type: ignore[misc]

    expected_fields = {
        "target",
        "scope",
        "target_path",
        "uninstalled",
        "skipped",
        "would_remove",
        "bytes_removed",
        "removed_marker",
        "reason",
    }
    assert {f.name for f in dataclasses.fields(outcome)} == expected_fields


def test_uninstall_prunes_empty_popola_loom_directory(
    isolated_home: tuple[Path, Path],
) -> None:
    """After the SKILL.md + marker are gone, the empty ``popola-loom/`` leaf dir is removed.

    Documents the explicit best-effort rmdir behaviour from the
    :mod:`popolaloom.evolution.skill_uninstall` module docstring: we
    prune the empty leaf so a clean uninstall doesn't leave an
    orphan empty dir behind (matches DevolaFlow plugin removal
    cleanliness).
    """
    _cwd, fake_home = isolated_home

    install_skill("cursor", scope="global")
    skill_dir = fake_home / ".cursor" / "skills" / "popola-loom"
    assert skill_dir.is_dir()

    uninstall_skill("cursor", scope="global")

    assert not skill_dir.exists()
    parent = fake_home / ".cursor" / "skills"
    # Parent ``skills/`` directory is left alone (we only prune the leaf).
    assert parent.is_dir()


def test_uninstall_leaves_non_popola_files_alone(
    isolated_home: tuple[Path, Path],
) -> None:
    """When the parent ``popola-loom/`` dir contains non-Skill files, do not rmdir."""
    _cwd, fake_home = isolated_home

    install_skill("cursor", scope="global")
    skill_dir = fake_home / ".cursor" / "skills" / "popola-loom"
    operator_note = skill_dir / "OPERATOR_NOTES.md"
    operator_note.write_text("custom note\n", encoding="utf-8")

    outcome = uninstall_skill("cursor", scope="global")

    assert outcome.uninstalled is True
    assert skill_dir.is_dir()
    assert operator_note.is_file()
    assert not (skill_dir / "SKILL.md").exists()
    assert not (skill_dir / VERSION_MARKER_FILENAME).exists()
