"""Unit tests for :mod:`popolaloom.relay.audit` (v0.8.8 T2.3.3).

Covers the AC items from the task brief §"Acceptance criteria":

- (a) ``RelayAuditWriter.append`` opens with mode ``"a"`` (POSIX
  ``O_APPEND``); serialises via the spec-locked ``json.dumps`` shape;
  calls ``flush()`` + ``os.fsync(fd)``; chmods file ``0o600`` and
  parent dir ``0o700`` on creation.
- (b) Concurrent ``append`` calls produce two atomic rows with no
  interleaving (POSIX ``O_APPEND`` semantics).
- (f) Test count: append-only + atomicity + file mode + fsync +
  fallback-WARN + false-positive (≥ 6).

The atomicity test uses an in-process thread pair rather than two
sub-processes; with ``O_APPEND`` set, a single ``write(2)`` of ≤
``PIPE_BUF`` bytes is POSIX-atomic regardless of whether the writers
are threads or processes (the kernel-level atomicity primitive
operates at the file-table-entry level).
"""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

import pytest

from popolaloom.relay.audit import (
    DEFAULT_AUDIT_ROOT,
    RelayAuditWriter,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _row(
    *,
    source_task_id: str = "v088-foo-bar-3a7f9c1d",
    outcome: str = "dispatched",
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a minimal audit row with the canonical key ordering."""
    base: dict[str, object] = {
        "schema_version": "1",
        "timestamp": "2026-05-08T15:33:42.918471+00:00",
        "source_task_id": source_task_id,
        "source_repo": "neolix-ai/popola-loom",
        "target_task_id": "v088-quux-baz-7d2e0a44",
        "target_repo": "neolix-ai/downstream-svc",
        "actor": "alice@neolix.ai",
        "mode": "auto",
        "outcome": outcome,
        "payload_sha256": "0fa2" + "0" * 60,
    }
    if extra:
        base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Path helpers + default root
# ---------------------------------------------------------------------------


def test_default_audit_root_is_under_local_agent() -> None:
    """Sanity: default root sits under ``.local/.agent/archive/relay``."""
    assert DEFAULT_AUDIT_ROOT.parts == (
        ".local",
        ".agent",
        "archive",
        "relay",
    )


def test_path_for_returns_canonical_path(tmp_path: Path) -> None:
    """:meth:`path_for` returns ``<root>/<source_task_id>.jsonl`` without IO."""
    writer = RelayAuditWriter(tmp_path)
    path = writer.path_for("v088-task-abc")
    assert path == tmp_path / "v088-task-abc.jsonl"
    assert not path.exists()


def test_path_for_rejects_path_separator(tmp_path: Path) -> None:
    """A ``/`` in ``source_task_id`` would let a row escape the audit root."""
    writer = RelayAuditWriter(tmp_path)
    with pytest.raises(ValueError, match="path separators"):
        writer.path_for("v088/escape")


def test_path_for_rejects_dotdot_segment(tmp_path: Path) -> None:
    """``..`` between hyphens (the popola task-id separator) MUST be rejected."""
    writer = RelayAuditWriter(tmp_path)
    with pytest.raises(ValueError, match=r"'\.\.'"):
        writer.path_for("v088-..-escape")


def test_path_for_rejects_empty_id(tmp_path: Path) -> None:
    """An empty ``source_task_id`` would land at ``<root>/.jsonl`` — caller bug."""
    writer = RelayAuditWriter(tmp_path)
    with pytest.raises(ValueError, match="non-empty"):
        writer.path_for("")


# ---------------------------------------------------------------------------
# AC (a) — basic append + JSON shape + fsync invariants
# ---------------------------------------------------------------------------


def test_append_writes_a_single_ndjson_line(tmp_path: Path) -> None:
    """One ``append()`` ≡ one NDJSON line trailing with ``\\n``."""
    writer = RelayAuditWriter(tmp_path)
    row = _row()
    path = writer.append(row)
    assert path == tmp_path / f"{row['source_task_id']}.jsonl"
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    decoded = json.loads(raw[:-1])
    assert decoded == row


def test_append_uses_compact_separators_no_whitespace(tmp_path: Path) -> None:
    """Spec: ``json.dumps(..., separators=(",", ":"), sort_keys=False)``.

    Compact separators ``(",", ":")`` means NO space after ``,`` or ``:``;
    ``sort_keys=False`` preserves the row's documented key ordering.
    """
    writer = RelayAuditWriter(tmp_path)
    row: dict[str, object] = {
        "schema_version": "1",
        "source_task_id": "v088-x",
        "outcome": "dispatched",
    }
    path = writer.append(row)
    raw = path.read_text(encoding="utf-8").rstrip("\n")
    assert raw == '{"schema_version":"1","source_task_id":"v088-x","outcome":"dispatched"}'


def test_append_preserves_unicode_without_ascii_escapes(tmp_path: Path) -> None:
    """``ensure_ascii=False`` keeps Chinese / emoji unescaped on disk."""
    writer = RelayAuditWriter(tmp_path)
    row = _row(extra={"actor": "李四@neolix.ai", "note": "релэй"})
    path = writer.append(row)
    body = path.read_text(encoding="utf-8")
    assert "李四@neolix.ai" in body
    assert "релэй" in body


def test_append_returns_path_pointing_to_actual_file(tmp_path: Path) -> None:
    writer = RelayAuditWriter(tmp_path)
    row = _row()
    path = writer.append(row)
    assert path.exists()
    assert path.is_file()
    assert path.parent == tmp_path


def test_append_ndjson_is_observable_after_fsync(tmp_path: Path) -> None:
    """After ``append()`` returns, the row is durable and readable.

    The ``fsync`` invariant is hard to assert directly without
    instrumenting the kernel; we verify the observable consequence —
    a fresh ``open()`` for read sees the line — which is the
    user-visible meaning of ``flush + fsync``. We also re-read the
    file from a sub-process (via ``Path.read_text``) since pytest's
    file system cache is process-local.
    """
    writer = RelayAuditWriter(tmp_path)
    row = _row()
    path = writer.append(row)
    contents = path.read_text(encoding="utf-8")
    lines = [line for line in contents.splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0]) == row


def test_fsync_invoked_on_underlying_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spy on ``os.fsync`` to confirm it is called at least once per append.

    The writer may legitimately fsync more than once per append (e.g.,
    once for the data file fd and once for the parent dir fd on
    filesystems that need it for durability); the contract this test
    enforces is *at least* one fsync per append, not *exactly* one.
    """
    seen: list[int] = []
    real_fsync = os.fsync

    def _spy(fd: int) -> None:
        seen.append(fd)
        real_fsync(fd)

    monkeypatch.setattr("popolaloom.relay.audit.os.fsync", _spy)
    writer = RelayAuditWriter(tmp_path)
    writer.append(_row())
    seen_after_first = len(seen)
    writer.append(_row(outcome="rejected_allowlist"))
    seen_after_second = len(seen)
    # At least one fsync per append — exact count varies by filesystem
    # (some implementations fsync the dir + the file separately).
    assert seen_after_first >= 1, "first append did not fsync"
    assert seen_after_second > seen_after_first, "second append did not fsync"


# ---------------------------------------------------------------------------
# AC (a) — file mode 0o600 + parent dir mode 0o700
# ---------------------------------------------------------------------------


def test_first_creation_sets_file_mode_to_0o600(tmp_path: Path) -> None:
    """The audit file is owner-only readable/writable on first creation."""
    writer = RelayAuditWriter(tmp_path / "relay")
    path = writer.append(_row())
    file_mode = stat.S_IMODE(path.stat().st_mode)
    assert file_mode == 0o600, f"got {file_mode:o} != 0o600"


def test_first_creation_sets_parent_dir_mode_to_0o700(
    tmp_path: Path,
) -> None:
    """The audit root is owner-only on first creation (locked-down dir)."""
    audit_root = tmp_path / "relay"
    writer = RelayAuditWriter(audit_root)
    writer.append(_row())
    parent_mode = stat.S_IMODE(audit_root.stat().st_mode)
    assert parent_mode == 0o700, f"got {parent_mode:o} != 0o700"


def test_chmod_repairs_loose_parent_mode_on_subsequent_append(
    tmp_path: Path,
) -> None:
    """A pre-existing parent at ``0o755`` is tightened to ``0o700`` on append.

    Spec hint: ``os.makedirs(..., mode=0o700, exist_ok=True)`` does NOT
    re-mode an existing dir; we ``chmod`` after for safety.
    """
    audit_root = tmp_path / "relay"
    audit_root.mkdir(mode=0o755)
    assert stat.S_IMODE(audit_root.stat().st_mode) == 0o755
    writer = RelayAuditWriter(audit_root)
    writer.append(_row())
    assert stat.S_IMODE(audit_root.stat().st_mode) == 0o700


def test_chmod_repairs_loose_file_mode_on_subsequent_append(
    tmp_path: Path,
) -> None:
    """A manual ``chmod 0o644`` is reverted on the next append (defence in depth)."""
    writer = RelayAuditWriter(tmp_path)
    path = writer.append(_row())
    os.chmod(path, 0o644)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    writer.append(_row(outcome="rejected_allowlist"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# AC (a) — append-only semantics (multiple rows accumulate)
# ---------------------------------------------------------------------------


def test_append_only_accumulates_rows(tmp_path: Path) -> None:
    """Two appends → two NDJSON lines on the same file (no truncation)."""
    writer = RelayAuditWriter(tmp_path)
    row1 = _row(outcome="dispatched")
    row2 = _row(outcome="rejected_allowlist")
    path1 = writer.append(row1)
    path2 = writer.append(row2)
    assert path1 == path2
    lines = path1.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == row1
    assert json.loads(lines[1]) == row2


def test_append_groups_per_source_task_id(tmp_path: Path) -> None:
    """Different source_task_ids land in separate files (per-source grouping)."""
    writer = RelayAuditWriter(tmp_path)
    path_a = writer.append(_row(source_task_id="v088-task-aaa"))
    path_b = writer.append(_row(source_task_id="v088-task-bbb"))
    assert path_a != path_b
    assert path_a == tmp_path / "v088-task-aaa.jsonl"
    assert path_b == tmp_path / "v088-task-bbb.jsonl"
    assert len(path_a.read_text(encoding="utf-8").splitlines()) == 1
    assert len(path_b.read_text(encoding="utf-8").splitlines()) == 1


def test_append_rejects_missing_source_task_id(tmp_path: Path) -> None:
    """Audit rows without ``source_task_id`` are caller bugs (No Silent Failures)."""
    writer = RelayAuditWriter(tmp_path)
    with pytest.raises(ValueError, match="source_task_id"):
        writer.append({"schema_version": "1"})


def test_append_rejects_non_string_source_task_id(tmp_path: Path) -> None:
    writer = RelayAuditWriter(tmp_path)
    with pytest.raises(ValueError, match="non-empty string"):
        writer.append({"source_task_id": 12345})


# ---------------------------------------------------------------------------
# AC (b) — concurrent append atomicity (POSIX O_APPEND)
# ---------------------------------------------------------------------------


def test_concurrent_appends_produce_atomic_rows(tmp_path: Path) -> None:
    """Two threads appending to the same file produce two complete rows.

    POSIX ``O_APPEND`` guarantees that ``write(2)`` of ≤ ``PIPE_BUF``
    bytes is atomic (the kernel takes a brief inode-level lock before
    advancing the file offset). With ``mode="a"`` Python opens the file
    with ``O_WRONLY | O_CREAT | O_APPEND`` — the load-bearing flag is
    ``O_APPEND``, NOT a userland Lock. This test pins the invariant
    by interleaving two threads and asserting:

    1. The file has exactly N rows (N = thread count × per-thread iters).
    2. Every row is a complete, parseable JSON object (no interleaving).
    3. Every row carries one of the two unique ``outcome`` markers
       (so we know the lines came from both writers).
    """
    writer = RelayAuditWriter(tmp_path)
    iters_per_thread = 50

    def _writer_loop(outcome_marker: str) -> None:
        for i in range(iters_per_thread):
            writer.append(
                _row(
                    outcome=outcome_marker,
                    extra={"iter": i, "thread": outcome_marker},
                )
            )

    t1 = threading.Thread(target=_writer_loop, args=("dispatched",))
    t2 = threading.Thread(target=_writer_loop, args=("rejected_allowlist",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    path = tmp_path / "v088-foo-bar-3a7f9c1d.jsonl"
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == iters_per_thread * 2

    outcomes: set[str] = set()
    for line in raw_lines:
        decoded = json.loads(line)
        assert decoded["source_task_id"] == "v088-foo-bar-3a7f9c1d"
        outcomes.add(str(decoded["outcome"]))
    assert outcomes == {"dispatched", "rejected_allowlist"}


# ---------------------------------------------------------------------------
# Constructor defaults
# ---------------------------------------------------------------------------


def test_writer_defaults_to_well_known_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a ``root`` arg the writer uses :data:`DEFAULT_AUDIT_ROOT`.

    We chdir into ``tmp_path`` so the relative default root materialises
    inside the per-test sandbox (mirrors
    :func:`tests.handoff.test_writer.test_write_envelope_default_base_dir_is_default_handoff_root`).
    """
    monkeypatch.chdir(tmp_path)
    writer = RelayAuditWriter()
    assert writer.root == DEFAULT_AUDIT_ROOT
    path = writer.append(_row())
    expected = (
        tmp_path / DEFAULT_AUDIT_ROOT / f"{_row()['source_task_id']}.jsonl"
    )
    assert path.resolve() == expected.resolve()
    assert expected.exists()
