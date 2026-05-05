"""Path resolution tests for ``popola init`` (Stage S2 of v0.5.0).

Covers the four per-IDE path resolvers in :mod:`popolaloom.cli.init_cmd`:

* :func:`cursor_target_path`
* :func:`claude_target_path`
* :func:`copilot_target_path`
* :func:`codex_target_path`

Plus a lightweight check that the skill source resolver returns the
S2 placeholder stub (since the Stage S3 canonical SKILL.md is not yet
bundled).  All tests use ``tmp_path`` + monkeypatch ``Path.home()`` so
no test ever touches the developer's real ``~/`` directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from popolaloom.cli._skill_source import (
    canonical_source_path,
    is_real_skill,
    render_stub,
    resolve_skill_source,
)
from popolaloom.cli.init_cmd import (
    claude_target_path,
    codex_target_path,
    copilot_target_path,
    cursor_target_path,
)


def test_cursor_target_path_global_and_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor: global → ~/.cursor/skills/popolaloom/SKILL.md; project → cwd."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    cwd = tmp_path / "proj"
    cwd.mkdir()

    g = cursor_target_path("global", cwd=cwd)
    p = cursor_target_path("project", cwd=cwd)

    assert g == fake_home / ".cursor" / "skills" / "popolaloom" / "SKILL.md"
    assert p == cwd / ".cursor" / "skills" / "popolaloom" / "SKILL.md"


def test_claude_target_path_global_and_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude: global → ~/.claude/skills/popolaloom/SKILL.md; project → cwd."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    cwd = tmp_path / "proj"
    cwd.mkdir()

    g = claude_target_path("global", cwd=cwd)
    p = claude_target_path("project", cwd=cwd)

    assert g == fake_home / ".claude" / "skills" / "popolaloom" / "SKILL.md"
    assert p == cwd / ".claude" / "skills" / "popolaloom" / "SKILL.md"


def test_copilot_target_path_is_always_project_local(
    tmp_path: Path,
) -> None:
    """Copilot: <cwd>/.github/copilot-instructions.md (single-file, no scope)."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    p = copilot_target_path(cwd=cwd)
    assert p == cwd / ".github" / "copilot-instructions.md"


def test_codex_target_path_honors_codex_home_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex: $CODEX_HOME if set, else ~/.codex/skills/popolaloom/SKILL.md."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    monkeypatch.delenv("CODEX_HOME", raising=False)
    default = codex_target_path()
    assert default == fake_home / ".codex" / "skills" / "popolaloom" / "SKILL.md"

    custom = tmp_path / "alt-codex"
    custom.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(custom))
    overridden = codex_target_path()
    assert overridden == custom / "skills" / "popolaloom" / "SKILL.md"


def test_skill_source_resolver_returns_stub_for_s2() -> None:
    """Stage S2 ships without the canonical SKILL.md → the resolver
    falls back to the placeholder stub and flags it as not-real.
    """
    canonical = canonical_source_path()
    assert canonical is None, (
        "Stage S2 must NOT bundle a wheel SKILL.md (S3 owns content); "
        f"unexpected canonical path: {canonical}"
    )

    content, is_real = resolve_skill_source()
    assert is_real is False
    assert is_real_skill(content) is False
    assert content == render_stub()
    assert "Stage S2 placeholder" in content
