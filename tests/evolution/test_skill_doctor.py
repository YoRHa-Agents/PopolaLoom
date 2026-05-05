"""Default-lane tests for :mod:`popolaloom.evolution.skill_doctor` (Stage S4 of v0.5.0).

Three cases per the v0.5.0-plan §S4.G test contract:

1. drift detection             — file with stale frontmatter version → ``drift=True``.
2. missing skill detection     — no on-disk file → ``exists=False``, notes carry "missing".
3. frontmatter version parse   — happy path matches ``popolaloom.__version__``.

Plus one extra (default-lane is cheap): ``targets=[]`` returns an empty
list (caller subset is honoured).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from popolaloom import __version__
from popolaloom.evolution.skill_doctor import (
    DoctorReport,
    check_skill_health,
    format_target_label,
)


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


def _write_skill_with_version(path: Path, version: str) -> None:
    """Write a minimal SKILL.md with ``version: <version>`` frontmatter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "---\n"
        "name: popolaloom\n"
        f"version: {version}\n"
        'description: "drift fixture"\n'
        "---\n"
        "\n"
        "# fixture body\n"
    )
    path.write_text(body, encoding="utf-8")


def test_check_skill_health_detects_version_drift(
    isolated_home: tuple[Path, Path],
) -> None:
    """A SKILL.md with a stale version flips ``drift=True`` + records a notes entry."""
    _cwd, fake_home = isolated_home
    target = fake_home / ".cursor" / "skills" / "popolaloom" / "SKILL.md"
    _write_skill_with_version(target, "0.0.1-stale")

    reports = check_skill_health(targets=["cursor"])
    cursor_global = next(r for r in reports if r.scope == "global")

    assert isinstance(cursor_global, DoctorReport)
    assert cursor_global.exists is True
    assert cursor_global.version == "0.0.1-stale"
    assert cursor_global.drift is True
    assert any("drift" in note.lower() for note in cursor_global.notes)


def test_check_skill_health_flags_missing_skill(
    isolated_home: tuple[Path, Path],
) -> None:
    """Targets without an on-disk SKILL.md report ``exists=False`` + a missing note."""
    _cwd, _fake_home = isolated_home

    reports = check_skill_health(targets=["claude"])
    assert reports, "claude has at least one (target, scope) slot"
    for report in reports:
        assert report.exists is False
        assert report.bytes is None
        assert report.version is None
        assert report.drift is False
        assert any("missing" in note.lower() for note in report.notes)


def test_check_skill_health_parses_frontmatter_version(
    isolated_home: tuple[Path, Path],
) -> None:
    """A SKILL.md whose frontmatter version matches the wheel reports ``drift=False``."""
    cwd, _fake_home = isolated_home
    target = cwd / ".github" / "copilot-instructions.md"
    _write_skill_with_version(target, __version__)

    reports = check_skill_health(targets=["copilot"])
    assert len(reports) == 1
    report = reports[0]
    assert report.target == "copilot"
    assert report.scope == "project"
    assert report.exists is True
    assert report.version == __version__
    assert report.drift is False
    assert report.bytes == target.stat().st_size


def test_check_skill_health_skips_unknown_targets(
    isolated_home: tuple[Path, Path],
) -> None:
    """Unknown target names in the whitelist are silently dropped (no exception)."""
    reports = check_skill_health(targets=["nonexistent-ide"])
    assert reports == []


def test_format_target_label_padding(
    isolated_home: tuple[Path, Path],
) -> None:
    """``format_target_label`` returns left-padded ``{target} {scope}`` for table output."""
    cwd, _fake_home = isolated_home
    target = cwd / ".github" / "copilot-instructions.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\nversion: x\n---\n", encoding="utf-8")
    reports = check_skill_health(targets=["copilot"])
    label = format_target_label(reports[0])
    assert "copilot" in label
    assert "project" in label
    assert label.startswith("copilot ")


def test_check_skill_health_handles_missing_frontmatter(
    isolated_home: tuple[Path, Path],
) -> None:
    """A SKILL.md without a ``---`` block reports no version + a parser warning."""
    cwd, _fake_home = isolated_home
    target = cwd / ".github" / "copilot-instructions.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("body without frontmatter\n", encoding="utf-8")

    reports = check_skill_health(targets=["copilot"])
    assert len(reports) == 1
    report = reports[0]
    assert report.exists is True
    assert report.version is None
    assert report.drift is False
    assert any("frontmatter missing" in note for note in report.notes)


def test_check_skill_health_handles_empty_version_value(
    isolated_home: tuple[Path, Path],
) -> None:
    """A frontmatter with an empty ``version:`` value reports None + a parser warning."""
    cwd, _fake_home = isolated_home
    target = cwd / ".github" / "copilot-instructions.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\nname: popolaloom\nversion: ''\n---\nbody\n",
        encoding="utf-8",
    )

    reports = check_skill_health(targets=["copilot"])
    report = reports[0]
    assert report.version is None
    assert any("empty version" in note for note in report.notes)


def test_check_skill_health_handles_no_version_key(
    isolated_home: tuple[Path, Path],
) -> None:
    """Frontmatter without a ``version:`` key reports None + a parser warning."""
    cwd, _fake_home = isolated_home
    target = cwd / ".github" / "copilot-instructions.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\nname: popolaloom\ndescription: no version\n---\nbody\n",
        encoding="utf-8",
    )

    reports = check_skill_health(targets=["copilot"])
    report = reports[0]
    assert report.version is None
    assert any("missing version key" in note for note in report.notes)


def test_check_skill_health_handles_non_utf8_content(
    isolated_home: tuple[Path, Path],
) -> None:
    """A binary SKILL.md flips ``version=None`` + records a non-utf8 note."""
    cwd, _fake_home = isolated_home
    target = cwd / ".github" / "copilot-instructions.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xfe\x00\x00binary garbage")

    reports = check_skill_health(targets=["copilot"])
    report = reports[0]
    assert report.exists is True
    assert report.version is None
    assert any("non-utf8" in note for note in report.notes)


def test_check_skill_health_default_walks_all_targets(
    isolated_home: tuple[Path, Path],
) -> None:
    """``targets=None`` walks every entry in :data:`SKILL_TARGETS`."""
    from popolaloom.evolution.skill_inject import SKILL_TARGETS

    reports = check_skill_health()
    target_set = {r.target for r in reports}
    assert target_set == set(SKILL_TARGETS)
    expected_count = sum(len(scopes) for scopes in SKILL_TARGETS.values())
    assert len(reports) == expected_count
