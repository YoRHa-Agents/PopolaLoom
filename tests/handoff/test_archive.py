"""Unit tests for :mod:`popolaloom.handoff.archive`.

Coverage targets (per T1.2 task spec):

- Basic copy: active envelope at ``tmp/handoff/`` is reproduced bit-for-bit
  under ``<archive_root>/<task_id>/<filename>``.
- Auto ``mkdir -p``: nested missing archive_root tree is created.
- Source preservation: ``archive_envelope`` does not delete or move the
  active copy (it's a *copy*, not a *move*; the active log stays
  authoritative).
- Audit invariant: the destination's ``mtime`` matches the source's
  ``mtime`` (``shutil.copy2`` preserves it).
- Idempotency: re-archiving the same source to the same dest yields the
  same content + the same path (overwrite is the no-op of the
  ``copy2`` contract).
- :func:`archive_dir_for` is side-effect-free and returns the same parent
  directory as ``archive_envelope``'s destination.
- Path-traversal defense: malformed ``task_id`` (``..``, ``/``, ``\\``,
  empty) raises :exc:`ValueError` with no IO performed.
- Missing source: :exc:`FileNotFoundError` for non-existent ``handoff_path``.
- Per-task isolation: two different ``task_id`` values archive into two
  distinct subdirectories without interfering.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from popolaloom.handoff import (
    DEFAULT_ARCHIVE_ROOT,
    HandoffEnvelope,
    archive_dir_for,
    archive_envelope,
    write_envelope,
)

NOW = datetime(2026, 5, 6, 22, 0, tzinfo=UTC)


def _envelope(
    *,
    handoff_id: str = "cursor-fix-bug-3a7f9c1d",
    target_cli: str = "cursor",
    prompt: str = "fix the bug in foo.py",
) -> HandoffEnvelope:
    return HandoffEnvelope(
        handoff_id=handoff_id,
        created_at=NOW,
        target_cli=target_cli,
        prompt=prompt,
    )


@pytest.fixture
def active_envelope(tmp_path: Path) -> tuple[Path, HandoffEnvelope]:
    """Land an active envelope under ``tmp/handoff/`` and return ``(path, envelope)``.

    Mirrors what dispatch (v0.7.2) will do before invoking the archive
    hook: the envelope is *already on disk* in the active root.
    """
    env = _envelope()
    handoff_root = tmp_path / "handoff"
    path = write_envelope(env, base_dir=handoff_root)
    return path, env


# ─────────────────── basic copy ───────────────────


def test_archive_envelope_copies_to_task_subdir(
    tmp_path: Path, active_envelope: tuple[Path, HandoffEnvelope]
) -> None:
    src, env = active_envelope
    archive_root = tmp_path / "archive"
    dest = archive_envelope(src, "task-001", archive_root=archive_root)

    assert dest == archive_root / "task-001" / src.name
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == env.to_markdown()


def test_archive_envelope_content_matches_source_byte_for_byte(
    tmp_path: Path, active_envelope: tuple[Path, HandoffEnvelope]
) -> None:
    src, _ = active_envelope
    archive_root = tmp_path / "archive"
    dest = archive_envelope(src, "task-001", archive_root=archive_root)
    assert dest.read_bytes() == src.read_bytes()


def test_archive_envelope_default_archive_root_is_under_local_agent() -> None:
    """Sanity: default root is ``.local/.agent/archive``."""
    assert DEFAULT_ARCHIVE_ROOT.parts == (".local", ".agent", "archive")


def test_archive_envelope_accepts_string_paths(
    tmp_path: Path, active_envelope: tuple[Path, HandoffEnvelope]
) -> None:
    """Both ``handoff_path`` and ``archive_root`` accept str."""
    src, env = active_envelope
    archive_root = tmp_path / "archive"
    dest = archive_envelope(str(src), "task-001", archive_root=str(archive_root))
    assert dest.read_text(encoding="utf-8") == env.to_markdown()


# ─────────────────── auto-mkdir ───────────────────


def test_archive_envelope_creates_nested_archive_root(
    tmp_path: Path, active_envelope: tuple[Path, HandoffEnvelope]
) -> None:
    src, _ = active_envelope
    archive_root = tmp_path / "deep" / "nested" / "missing" / "archive"
    assert not archive_root.exists()
    dest = archive_envelope(src, "task-001", archive_root=archive_root)
    assert archive_root.is_dir()
    assert dest.parent == archive_root / "task-001"


# ─────────────────── source preservation (copy, not move) ───────────────────


def test_archive_envelope_does_not_delete_source(
    tmp_path: Path, active_envelope: tuple[Path, HandoffEnvelope]
) -> None:
    src, env = active_envelope
    src_content_before = src.read_text(encoding="utf-8")
    archive_root = tmp_path / "archive"
    archive_envelope(src, "task-001", archive_root=archive_root)
    assert src.exists(), "source must NOT be deleted (copy, not move)"
    assert src.read_text(encoding="utf-8") == src_content_before
    assert src_content_before == env.to_markdown()


def test_archive_envelope_does_not_move_source(
    tmp_path: Path, active_envelope: tuple[Path, HandoffEnvelope]
) -> None:
    """Same invariant as above, framed as ``stat.st_ino`` identity:
    the source's inode must be unchanged after archiving."""
    src, _ = active_envelope
    src_inode_before = src.stat().st_ino
    archive_root = tmp_path / "archive"
    archive_envelope(src, "task-001", archive_root=archive_root)
    assert src.stat().st_ino == src_inode_before


# ─────────────────── audit invariant: mtime preserved ───────────────────


def test_archive_envelope_preserves_mtime(
    tmp_path: Path, active_envelope: tuple[Path, HandoffEnvelope]
) -> None:
    """``shutil.copy2`` preserves mtime — the archive is meant to be an
    audit snapshot of *when the envelope was created*, not when the
    archive ran."""
    src, _ = active_envelope
    fixed_mtime = 1_700_000_000.0  # 2023-11-14T22:13:20+00:00
    os.utime(src, (fixed_mtime, fixed_mtime))

    archive_root = tmp_path / "archive"
    dest = archive_envelope(src, "task-001", archive_root=archive_root)

    assert dest.stat().st_mtime == fixed_mtime


# ─────────────────── idempotency / overwrite ───────────────────


def test_archive_envelope_idempotent_repeat_call(
    tmp_path: Path, active_envelope: tuple[Path, HandoffEnvelope]
) -> None:
    src, env = active_envelope
    archive_root = tmp_path / "archive"

    d1 = archive_envelope(src, "task-001", archive_root=archive_root)
    d2 = archive_envelope(src, "task-001", archive_root=archive_root)

    assert d1 == d2
    assert d1.read_text(encoding="utf-8") == env.to_markdown()
    assert d2.read_text(encoding="utf-8") == env.to_markdown()


def test_archive_envelope_overwrites_existing_dest(tmp_path: Path) -> None:
    """If the dest already exists with stale content, ``copy2`` truncates
    + replaces — the most recent active copy wins."""
    archive_root = tmp_path / "archive"
    handoff_root = tmp_path / "handoff"

    env_a = _envelope(prompt="alpha original")
    src = write_envelope(env_a, base_dir=handoff_root)
    dest = archive_envelope(src, "task-001", archive_root=archive_root)
    assert "alpha original" in dest.read_text(encoding="utf-8")

    # Mutate the active source (simulates a corrected envelope land).
    src.write_text(
        env_a.to_markdown().replace("alpha original", "alpha corrected"),
        encoding="utf-8",
    )
    dest = archive_envelope(src, "task-001", archive_root=archive_root)
    assert "alpha corrected" in dest.read_text(encoding="utf-8")
    assert "alpha original" not in dest.read_text(encoding="utf-8")


# ─────────────────── archive_dir_for ───────────────────


def test_archive_dir_for_matches_actual_dest_parent(
    tmp_path: Path, active_envelope: tuple[Path, HandoffEnvelope]
) -> None:
    src, _ = active_envelope
    archive_root = tmp_path / "archive"
    predicted_dir = archive_dir_for("task-001", archive_root=archive_root)
    actual_dest = archive_envelope(src, "task-001", archive_root=archive_root)
    assert predicted_dir == actual_dest.parent


def test_archive_dir_for_does_not_create_directory(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    predicted = archive_dir_for("task-001", archive_root=archive_root)
    assert not predicted.exists()
    assert not archive_root.exists()


def test_archive_dir_for_default_root() -> None:
    p = archive_dir_for("task-xyz")
    assert p == DEFAULT_ARCHIVE_ROOT / "task-xyz"


# ─────────────────── path-traversal defense (No Silent Failures) ───────────────────


@pytest.mark.parametrize(
    "bad_task_id",
    [
        "../../etc/passwd",
        "cursor/abc",
        "cursor\\abc",
        "..",
        "../foo",
        "a/b",
        "a\\b",
    ],
)
def test_archive_envelope_rejects_traversal_task_id(
    tmp_path: Path, active_envelope: tuple[Path, HandoffEnvelope], bad_task_id: str
) -> None:
    src, _ = active_envelope
    archive_root = tmp_path / "archive"
    with pytest.raises(ValueError):
        archive_envelope(src, bad_task_id, archive_root=archive_root)
    # No partial state: the archive root must not have materialized.
    assert not archive_root.exists()


def test_archive_envelope_rejects_empty_task_id(
    tmp_path: Path, active_envelope: tuple[Path, HandoffEnvelope]
) -> None:
    src, _ = active_envelope
    archive_root = tmp_path / "archive"
    with pytest.raises(ValueError, match=r"non-empty"):
        archive_envelope(src, "", archive_root=archive_root)
    assert not archive_root.exists()


@pytest.mark.parametrize(
    "bad_task_id",
    [
        "../../etc/passwd",
        "cursor/abc",
        "cursor\\abc",
        "..",
        "",
    ],
)
def test_archive_dir_for_rejects_traversal_task_id(bad_task_id: str) -> None:
    with pytest.raises(ValueError):
        archive_dir_for(bad_task_id)


# ─────────────────── error: missing source ───────────────────


def test_archive_envelope_missing_source_raises_filenotfound(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    bogus = tmp_path / "does-not-exist.md"
    assert not bogus.exists()
    with pytest.raises(FileNotFoundError, match=r"does not exist"):
        archive_envelope(bogus, "task-001", archive_root=archive_root)


def test_archive_envelope_source_is_directory_raises_isadirectory(tmp_path: Path) -> None:
    """If the caller passes a directory path (operator error), surface
    it explicitly instead of silently triggering ``copytree`` semantics."""
    src_dir = tmp_path / "iam_a_dir"
    src_dir.mkdir()
    archive_root = tmp_path / "archive"
    with pytest.raises(IsADirectoryError, match=r"is a directory"):
        archive_envelope(src_dir, "task-001", archive_root=archive_root)


# ─────────────────── per-task isolation ───────────────────


def test_archive_envelope_two_task_ids_land_in_distinct_dirs(
    tmp_path: Path, active_envelope: tuple[Path, HandoffEnvelope]
) -> None:
    src, env = active_envelope
    archive_root = tmp_path / "archive"

    dest_a = archive_envelope(src, "task-A", archive_root=archive_root)
    dest_b = archive_envelope(src, "task-B", archive_root=archive_root)

    assert dest_a.parent == archive_root / "task-A"
    assert dest_b.parent == archive_root / "task-B"
    assert dest_a != dest_b
    assert dest_a.exists() and dest_b.exists()
    assert dest_a.read_text(encoding="utf-8") == env.to_markdown()
    assert dest_b.read_text(encoding="utf-8") == env.to_markdown()


def test_archive_envelope_default_archive_root_under_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active_envelope: tuple[Path, HandoffEnvelope]
) -> None:
    """When ``archive_root`` is omitted the writer uses
    :data:`DEFAULT_ARCHIVE_ROOT` relative to CWD."""
    monkeypatch.chdir(tmp_path)
    src, env = active_envelope
    dest = archive_envelope(src, "task-001")
    expected = tmp_path / DEFAULT_ARCHIVE_ROOT / "task-001" / src.name
    assert dest.resolve() == expected.resolve()
    assert dest.read_text(encoding="utf-8") == env.to_markdown()
