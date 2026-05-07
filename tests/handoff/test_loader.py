"""Tests for :mod:`popolaloom.handoff.loader` — read-side helpers (v0.7.2 patch 2).

Covers :func:`list_active_envelopes` / :func:`resolve_envelope_path` /
:func:`load_envelope` invariants:

- empty / missing dir → empty list (vs raising)
- mtime-desc sort, ``.md`` filter (non-md files ignored)
- handoff_id path-traversal defense
- $POPOLA_HANDOFF_DIR env-var precedence vs explicit ``base_dir`` arg
- round-trip with writer (write_envelope → load_envelope → equal model)
- FileNotFoundError on missing id (No Silent Failures)
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from popolaloom.handoff import (
    HandoffEnvelope,
    HandoffSummary,
    generate_handoff_id,
    list_active_envelopes,
    load_envelope,
    resolve_envelope_path,
    write_envelope,
)


def _build_envelope(prompt: str = "demo prompt", target_cli: str = "cursor") -> HandoffEnvelope:
    return HandoffEnvelope(
        handoff_id=generate_handoff_id(target_cli, prompt),
        created_at=datetime.now(UTC),
        target_cli=target_cli,
        prompt=prompt,
    )


# ── list_active_envelopes ────────────────────────────────────────────────


def test_list_active_envelopes_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    """Missing handoff dir → empty list (NOT a FileNotFoundError)."""
    missing = tmp_path / "no_such_dir"
    assert list_active_envelopes(base_dir=missing) == []


def test_list_active_envelopes_returns_empty_when_dir_empty(tmp_path: Path) -> None:
    """Empty handoff dir → empty list."""
    (tmp_path / "empty_handoff").mkdir()
    assert list_active_envelopes(base_dir=tmp_path / "empty_handoff") == []


def test_list_active_envelopes_returns_summaries(tmp_path: Path) -> None:
    """Multiple envelopes → list of HandoffSummary objects."""
    env1 = _build_envelope("first")
    env2 = _build_envelope("second")
    write_envelope(env1, base_dir=tmp_path)
    write_envelope(env2, base_dir=tmp_path)

    summaries = list_active_envelopes(base_dir=tmp_path)

    assert len(summaries) == 2
    ids = {s.handoff_id for s in summaries}
    assert env1.handoff_id in ids
    assert env2.handoff_id in ids
    for s in summaries:
        assert isinstance(s, HandoffSummary)
        assert s.path.is_absolute()
        assert s.size_bytes > 0
        assert s.mtime.tzinfo is not None


def test_list_active_envelopes_sort_mtime_desc(tmp_path: Path) -> None:
    """Newest envelope appears first (mtime descending)."""
    env_old = _build_envelope("old")
    env_new = _build_envelope("new")
    write_envelope(env_old, base_dir=tmp_path)
    # Force mtime ordering: a small sleep ensures the newer file's mtime > older.
    # On filesystems with sub-second resolution, even tiny delays suffice.
    time.sleep(0.01)
    write_envelope(env_new, base_dir=tmp_path)

    summaries = list_active_envelopes(base_dir=tmp_path)

    assert summaries[0].handoff_id == env_new.handoff_id
    assert summaries[1].handoff_id == env_old.handoff_id


def test_list_active_envelopes_ignores_non_md_files(tmp_path: Path) -> None:
    """Non-``.md`` siblings (e.g. ``.tmp`` leftovers) are excluded."""
    env = _build_envelope("only md")
    write_envelope(env, base_dir=tmp_path)
    (tmp_path / "stray.txt").write_text("noise")
    (tmp_path / "leftover.md.tmp").write_text("partial write")
    (tmp_path / "subdir").mkdir()

    summaries = list_active_envelopes(base_dir=tmp_path)

    assert len(summaries) == 1
    assert summaries[0].handoff_id == env.handoff_id


def test_list_active_envelopes_resolves_env_var_when_base_dir_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``base_dir=None`` falls back to ``$POPOLA_HANDOFF_DIR``."""
    handoff_dir = tmp_path / "via_env"
    monkeypatch.setenv("POPOLA_HANDOFF_DIR", str(handoff_dir))
    env = _build_envelope("env-resolved")
    write_envelope(env, base_dir=handoff_dir)

    summaries = list_active_envelopes()

    assert len(summaries) == 1
    assert summaries[0].handoff_id == env.handoff_id


def test_list_active_envelopes_explicit_arg_beats_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit ``base_dir`` arg overrides ``$POPOLA_HANDOFF_DIR``."""
    env_dir = tmp_path / "from_env"
    explicit_dir = tmp_path / "explicit"
    monkeypatch.setenv("POPOLA_HANDOFF_DIR", str(env_dir))

    env_in_env_dir = _build_envelope("env one")
    env_in_explicit = _build_envelope("explicit one")
    write_envelope(env_in_env_dir, base_dir=env_dir)
    write_envelope(env_in_explicit, base_dir=explicit_dir)

    summaries = list_active_envelopes(base_dir=explicit_dir)

    assert len(summaries) == 1
    assert summaries[0].handoff_id == env_in_explicit.handoff_id


# ── resolve_envelope_path ────────────────────────────────────────────────


def test_resolve_envelope_path_returns_canonical(tmp_path: Path) -> None:
    """Resolved path is ``<base_dir>/<handoff_id>.md`` even without writing."""
    path = resolve_envelope_path("cursor-demo-12345678", base_dir=tmp_path)
    assert path == tmp_path / "cursor-demo-12345678.md"
    assert not path.exists(), "resolve must not create the file"


def test_resolve_envelope_path_rejects_empty_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        resolve_envelope_path("", base_dir=tmp_path)


def test_resolve_envelope_path_rejects_traversal(tmp_path: Path) -> None:
    """Path-traversal attempts in handoff_id raise ValueError."""
    for bad_id in ("../etc/passwd", "cursor/relpath", "cursor\\winpath"):
        with pytest.raises(ValueError, match="traversal"):
            resolve_envelope_path(bad_id, base_dir=tmp_path)


# ── load_envelope ────────────────────────────────────────────────────────


def test_load_envelope_roundtrip(tmp_path: Path) -> None:
    """``write_envelope`` then ``load_envelope`` gives back an equal model."""
    env = _build_envelope("roundtrip me")
    write_envelope(env, base_dir=tmp_path)

    loaded = load_envelope(env.handoff_id, base_dir=tmp_path)

    assert loaded == env


def test_load_envelope_missing_raises_filenotfound(tmp_path: Path) -> None:
    """Missing id → FileNotFoundError (No Silent Failures)."""
    with pytest.raises(FileNotFoundError):
        load_envelope("nonexistent-id-xx-12345678", base_dir=tmp_path)


def test_load_envelope_traversal_blocked(tmp_path: Path) -> None:
    """Traversal in handoff_id is caught at the resolve step before any I/O."""
    with pytest.raises(ValueError, match="traversal"):
        load_envelope("../escape-12345678", base_dir=tmp_path)


def test_load_envelope_uses_env_var_when_base_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``base_dir=None`` falls back to ``$POPOLA_HANDOFF_DIR``."""
    handoff_dir = tmp_path / "via_env"
    monkeypatch.setenv("POPOLA_HANDOFF_DIR", str(handoff_dir))
    env = _build_envelope("env-loaded")
    write_envelope(env, base_dir=handoff_dir)

    loaded = load_envelope(env.handoff_id)

    assert loaded == env


def test_handoff_summary_is_immutable() -> None:
    """``HandoffSummary`` is frozen dataclass (no mutation)."""
    s = HandoffSummary(
        handoff_id="x-y-z-12345678",
        path=Path("/tmp/x.md"),
        size_bytes=42,
        mtime=datetime.now(UTC),
    )
    with pytest.raises((AttributeError, TypeError)):
        s.size_bytes = 99  # type: ignore[misc]
