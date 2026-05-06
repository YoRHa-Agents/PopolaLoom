"""Coverage gap-closer tests (v0.5.5 L5.E final coverage push).

v0.5.5 (Loop 5 of v0.5.x → v0.6.0) targets the LAST missing branches
across the codebase to clear the ``[tool.coverage.report] fail_under``
floor at 94%. Each test in this file targets a specific line range
flagged by ``pytest --cov-report=term-missing`` at the v0.5.4
baseline (93.94%); the goal is a precise, high-leverage push rather
than a shotgun survey.

Modules + lines targeted (per the term-missing report on
``feature/v0.5.0-skill-install`` HEAD ``740d011``):

1. ``cli/_skill_source.py`` — line 159 (placeholder stub fallback path
   when canonical source is missing) is exercised in this file under
   a hermetic monkeypatch.
2. ``evaluation/dimensions/dispatch_isolation.py`` — line 45 (``None``
   pid → ``None`` pgid) + lines 51-53 (``TypeError`` / ``ValueError``
   on non-int pid).
3. ``evaluation/dimensions/single_threaded_writes.py`` — lines 52-56
   (``OSError`` reading file) + lines 99-104 (``ImportError`` of
   popolaloom).
4. ``evolution/skill_inject.py`` — lines 194-195 (unknown target
   ``KeyError``), 200-201 (unsupported scope ``KeyError``), 245
   (``$HOME`` env override fallback), 380 + 404-405
   (``emit_skill_check_event`` no-op + log-and-continue when append
   fails).
5. ``evolution/skill_upgrade.py`` — lines 97-98 (``UnicodeDecodeError``
   reading existing file), 100 (no ``---`` frontmatter), 103 (no
   closing ``---``), 110 (frontmatter with no ``version:``), 168-169
   (``OSError`` re-reading existing for content compare).
6. ``cli/skill_cmd.py`` — lines 113-115 (``SKIP`` + ``?`` paths in
   ``_outcome_status_text``), 125 (``UP-TO-DATE``), 128 (``?``),
   137-142 (``DRIFT`` paths in ``_doctor_status_text``).

Each test is hermetic (tmp_path + monkeypatch) and runs in < 50 ms.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

# ── 1. cli/_skill_source.py — fallback stub when canonical missing ────────


@pytest.fixture
def hermetic_skill_source(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force ``canonical_source_path`` to return ``None`` so the stub fires."""
    from popolaloom.cli import _skill_source as mod

    monkeypatch.setattr(mod, "canonical_source_path", lambda: None)
    yield


def test_resolve_skill_source_falls_back_to_stub_when_canonical_missing(
    hermetic_skill_source: None,
) -> None:
    """Line 159 — when ``canonical_source_path()`` returns None, render the stub."""
    from popolaloom.cli._skill_source import (
        STUB_BODY,
        is_real_skill,
        resolve_skill_source,
    )

    content, is_real = resolve_skill_source()
    assert content == STUB_BODY
    assert is_real is False
    assert not is_real_skill(content)


def test_canonical_source_path_returns_none_when_not_a_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Line 68 — when joinpath resolves but is not a file, return None.

    Patch :func:`importlib.resources.files` to return a non-file path
    so ``path.is_file()`` returns False.
    """
    import importlib.resources as resmod

    fake_root = tmp_path / "popolaloom"
    fake_root.mkdir()

    class _FakeRoot:
        def joinpath(self, *parts: str) -> Path:
            return fake_root / "/".join(parts)

    monkeypatch.setattr(resmod, "files", lambda _: _FakeRoot())

    from popolaloom.cli._skill_source import canonical_source_path

    assert canonical_source_path() is None


# ── 2. evaluation/dimensions/dispatch_isolation.py edge cases ─────────────


def test_safe_getpgid_handles_none_input() -> None:
    """Line 45 — ``_safe_getpgid(None)`` returns None (early-out branch)."""
    from popolaloom.evaluation.dimensions.dispatch_isolation import _safe_getpgid

    assert _safe_getpgid(None) is None


def test_safe_getpgid_handles_non_integer_input() -> None:
    """Lines 51-53 — TypeError / ValueError on non-int input → None."""
    from popolaloom.evaluation.dimensions.dispatch_isolation import _safe_getpgid

    assert _safe_getpgid("not-a-pid") is None  # type: ignore[arg-type]
    assert _safe_getpgid(object()) is None  # type: ignore[arg-type]


def test_dispatch_isolation_pid_only_branch_returns_score() -> None:
    """Line 92-93 — when getpgid lookups fail, fall through to PID inequality."""
    from popolaloom.evaluation.dimensions.dispatch_isolation import DispatchIsolation

    scorer = DispatchIsolation()
    assert scorer.score({"daemon_pid": 1, "cli_pid": 2}) in {1.0, 0.0}


# ── 3. single_threaded_writes — IO + ImportError edges ────────────────────


def test_count_locks_in_file_returns_zero_on_oserror(tmp_path: Path) -> None:
    """Lines 52-56 — ``_count_locks_in_file`` returns 0 when read fails."""
    from popolaloom.evaluation.dimensions.single_threaded_writes import (
        _count_locks_in_file,
    )

    # A directory pretending to be a file: read_text raises IsADirectoryError
    # which is an OSError subclass, exercising the 52-56 branch.
    bogus = tmp_path / "is-a-dir"
    bogus.mkdir()
    assert _count_locks_in_file(bogus) == 0


def test_single_threaded_writes_handles_import_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines 99-104 — when popolaloom can't be located, return 0.0."""
    import builtins

    from popolaloom.evaluation.dimensions.single_threaded_writes import (
        SingleThreadedWrites,
    )

    real_import = builtins.__import__

    def _fake_import(name: str, *args, **kwargs):
        if name == "popolaloom":
            raise ImportError("intentional failure for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    scorer = SingleThreadedWrites()
    score = scorer.score({})
    assert score == 0.0


def test_single_threaded_writes_locks_evidence_returns_one() -> None:
    """The fast-path evidence form returns 1.0 when all 3 locks are present."""
    from popolaloom.evaluation.dimensions.single_threaded_writes import (
        SingleThreadedWrites,
    )

    scorer = SingleThreadedWrites()
    full = {"locks_present": {"_event_logs_lock", "state_store_lock", "event_log_lock"}}
    assert scorer.score(full) == 1.0


# ── 4. skill_inject.py KeyError + HOME env + event-log error paths ────────


def test_resolve_target_path_unknown_target_raises() -> None:
    """Lines 194-195 — unknown target raises KeyError with helpful message."""
    from popolaloom.evolution.skill_inject import resolve_target_path

    with pytest.raises(KeyError, match="unknown skill target"):
        resolve_target_path("not-a-real-target", "global")


def test_resolve_target_path_unsupported_scope_raises() -> None:
    """Lines 200-201 — copilot doesn't support 'global', raises KeyError."""
    from popolaloom.evolution.skill_inject import resolve_target_path

    with pytest.raises(KeyError, match="does not support scope"):
        resolve_target_path("copilot", "global")


def test_supported_scopes_returns_empty_for_unknown_target() -> None:
    from popolaloom.evolution.skill_inject import supported_scopes

    assert supported_scopes("ghost") == []


def test_home_path_uses_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Line 245 — ``$HOME`` env var override is honoured."""
    from popolaloom.evolution.skill_inject import _home_path

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    assert _home_path() == fake_home


def test_home_path_fallback_to_pathhome_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Line 245-246 fallback — no HOME → Path.home()."""
    from popolaloom.evolution.skill_inject import _home_path

    monkeypatch.delenv("HOME", raising=False)
    assert _home_path() == Path.home()


def test_emit_skill_check_event_noop_when_event_log_none() -> None:
    """Line 380 — None event_log is silently a no-op."""
    from popolaloom.evolution.skill_inject import (
        SkillCheckResult,
        emit_skill_check_event,
    )

    result = SkillCheckResult(
        present=True,
        cursor_skill_path=Path("/nope/cursor"),
        claude_skill_path=Path("/nope/claude"),
        found_paths=[],
    )
    emit_skill_check_event(event_log=None, round_num=1, result=result)


def test_emit_skill_check_event_swallows_append_exception() -> None:
    """Lines 404-405 — when ``append()`` raises, log + continue (no propagation)."""
    from popolaloom.evolution.skill_inject import (
        SkillCheckResult,
        emit_skill_check_event,
    )

    class _Bomb:
        def append(self, *args, **kwargs):
            raise RuntimeError("intentional boom")

    result = SkillCheckResult(
        present=False,
        cursor_skill_path=Path("/nope/cursor"),
        claude_skill_path=Path("/nope/claude"),
        found_paths=[],
    )
    emit_skill_check_event(event_log=_Bomb(), round_num=1, result=result)


# ── 5. skill_upgrade.py read-existing edge cases ──────────────────────────


def test_read_existing_version_returns_none_when_file_missing(tmp_path: Path) -> None:
    """The first guard — file doesn't exist → None."""
    from popolaloom.evolution.skill_upgrade import _read_existing_version

    assert _read_existing_version(tmp_path / "missing.md") is None


def test_read_existing_version_returns_none_on_unicode_error(tmp_path: Path) -> None:
    """Lines 97-98 — UnicodeDecodeError on read returns None."""
    from popolaloom.evolution.skill_upgrade import _read_existing_version

    bad = tmp_path / "binary.md"
    bad.write_bytes(b"\xff\xfe\xfd not utf8")
    assert _read_existing_version(bad) is None


def test_read_existing_version_no_frontmatter_returns_none(tmp_path: Path) -> None:
    """Line 100 — file without ``---\\n`` start sentinel returns None."""
    from popolaloom.evolution.skill_upgrade import _read_existing_version

    plain = tmp_path / "plain.md"
    plain.write_text("# Hello world\n", encoding="utf-8")
    assert _read_existing_version(plain) is None


def test_read_existing_version_unclosed_frontmatter_returns_none(
    tmp_path: Path,
) -> None:
    """Line 103 — file with opening ``---`` but no closing returns None."""
    from popolaloom.evolution.skill_upgrade import _read_existing_version

    open_only = tmp_path / "open.md"
    open_only.write_text("---\nname: x\nversion: 1\n", encoding="utf-8")
    assert _read_existing_version(open_only) is None


def test_read_existing_version_no_version_field_returns_none(tmp_path: Path) -> None:
    """Line 110 — frontmatter without ``version:`` line returns None."""
    from popolaloom.evolution.skill_upgrade import _read_existing_version

    versionless = tmp_path / "no-version.md"
    versionless.write_text("---\nname: popola-loom\n---\nbody\n", encoding="utf-8")
    assert _read_existing_version(versionless) is None


def test_read_existing_version_parses_quoted_version(tmp_path: Path) -> None:
    """The happy path — single-quoted + double-quoted versions both parse."""
    from popolaloom.evolution.skill_upgrade import _read_existing_version

    quoted = tmp_path / "quoted.md"
    quoted.write_text(
        "---\nname: popola-loom\nversion: '0.5.5'\n---\nbody\n",
        encoding="utf-8",
    )
    assert _read_existing_version(quoted) == "0.5.5"

    dquoted = tmp_path / "dquoted.md"
    dquoted.write_text(
        '---\nname: popola-loom\nversion: "0.5.5"\n---\nbody\n',
        encoding="utf-8",
    )
    assert _read_existing_version(dquoted) == "0.5.5"


# ── 6. skill_cmd.py status renderers (table action column) ────────────────


def test_outcome_status_text_renders_skip_branch() -> None:
    """Line 113-114 — InstallOutcome with skipped=True renders 'SKIP'."""
    from popolaloom.cli.skill_cmd import _outcome_status_text
    from popolaloom.evolution.skill_install import InstallOutcome

    outcome = InstallOutcome(
        target="cursor",
        scope="global",
        target_path=Path("/x/SKILL.md"),
        installed=False,
        skipped=True,
        bytes=42,
    )
    text = _outcome_status_text(outcome)
    assert "SKIP" in str(text)


def test_outcome_status_text_renders_unknown_branch() -> None:
    """Line 115 — InstallOutcome with no flags set falls through to '?'."""
    from popolaloom.cli.skill_cmd import _outcome_status_text
    from popolaloom.evolution.skill_install import InstallOutcome

    outcome = InstallOutcome(
        target="cursor",
        scope="global",
        target_path=Path("/x/SKILL.md"),
        installed=False,
        skipped=False,
    )
    text = _outcome_status_text(outcome)
    assert "?" in str(text)


def test_upgrade_status_text_renders_up_to_date() -> None:
    """Line 124-125 — UpgradeOutcome with up_to_date=True renders 'UP-TO-DATE'."""
    from popolaloom.cli.skill_cmd import _upgrade_status_text
    from popolaloom.evolution.skill_upgrade import UpgradeOutcome

    outcome = UpgradeOutcome(
        target="cursor",
        scope="global",
        target_path=Path("/x/SKILL.md"),
        up_to_date=True,
    )
    text = _upgrade_status_text(outcome)
    assert "UP-TO-DATE" in str(text)


def test_upgrade_status_text_renders_unknown_branch() -> None:
    """Line 128 — UpgradeOutcome with no flags set renders '?'."""
    from popolaloom.cli.skill_cmd import _upgrade_status_text
    from popolaloom.evolution.skill_upgrade import UpgradeOutcome

    outcome = UpgradeOutcome(
        target="cursor",
        scope="global",
        target_path=Path("/x/SKILL.md"),
    )
    text = _upgrade_status_text(outcome)
    assert "?" in str(text)


def test_doctor_status_text_renders_drift() -> None:
    """Lines 137-141 — DoctorReport with drift=True renders 'DRIFT'."""
    from popolaloom.cli.skill_cmd import _doctor_status_text
    from popolaloom.evolution.skill_doctor import DoctorReport

    report = DoctorReport(
        target="cursor",
        scope="global",
        expected_path=Path("/x/SKILL.md"),
        exists=True,
        bytes=42,
        version="0.4.0",
        drift=True,
    )
    text, suffix = _doctor_status_text(report)
    assert "DRIFT" in str(text)
    assert "0.4.0" in suffix


def test_doctor_status_text_renders_ok() -> None:
    """Line 142 — DoctorReport with no drift + exists renders 'OK'."""
    from popolaloom import __version__
    from popolaloom.cli.skill_cmd import _doctor_status_text
    from popolaloom.evolution.skill_doctor import DoctorReport

    report = DoctorReport(
        target="cursor",
        scope="global",
        expected_path=Path("/x/SKILL.md"),
        exists=True,
        bytes=42,
        version=__version__,
        drift=False,
    )
    text, suffix = _doctor_status_text(report)
    assert "OK" in str(text)
    assert __version__ in suffix


def test_doctor_status_text_renders_miss() -> None:
    """Line 135-136 — DoctorReport with exists=False renders 'MISS'."""
    from popolaloom.cli.skill_cmd import _doctor_status_text
    from popolaloom.evolution.skill_doctor import DoctorReport

    report = DoctorReport(
        target="cursor",
        scope="global",
        expected_path=Path("/x/SKILL.md"),
        exists=False,
        bytes=None,
        version=None,
        drift=False,
    )
    text, suffix = _doctor_status_text(report)
    assert "MISS" in str(text)
    assert "expected" in suffix
