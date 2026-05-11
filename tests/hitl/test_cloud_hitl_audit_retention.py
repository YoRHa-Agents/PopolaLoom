"""M9 / SECURITY L4 — audit-log retention guard.

Per ``.local/.agent/active/v0.8.7-cloud-hitl-prod/SECURITY_CHECKLIST.md`` §3
**L4**: ``popola_hitl`` keeps the four retention columns (``created_at``,
``answered_via``, ``answer_responder_id``, plus the
``cursor_run_id`` cross-reference under ``metadata``) for ≥ 90 days, and
**no v0.8.7 code path issues `DELETE FROM popola_hitl`**.

Pre-fix the gate criterion (§10 C4) cited this test file but it didn't
exist. Per REVIEW.md M9, this file is the missing tracer-bullet.

Two cases:

1. ``test_popola_hitl_schema_carries_retention_columns`` —
   ``PRAGMA table_info(popola_hitl)`` exposes the four documented
   retention columns.
2. ``test_no_delete_from_popola_hitl_in_v0_8_7_code_paths`` —
   ``rg "DELETE FROM popola_hitl"`` returns 0 hits across the entire
   ``src/popolaloom/`` tree (production code) and across migrations
   (so 90-day retention is structurally guaranteed by the absence of
   a row-deletion code path).
"""

from __future__ import annotations

from importlib import resources
import re
import sqlite3
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS_DIR = Path(resources.files("popolaloom.migrations"))
_DELETE_PATTERN = re.compile(r"DELETE\s+FROM\s+popola_hitl", re.IGNORECASE)


def _scan_for_delete(directory: Path, suffix_glob: str) -> list[tuple[Path, int, str]]:
    """Return list of (path, line_no, line_content) for every
    ``DELETE FROM popola_hitl`` hit under ``directory`` matching the
    suffix glob (e.g. ``*.py`` or ``*.sql``).

    Pure-Python (no ripgrep dependency) so the test runs unchanged on
    GitHub Actions runners that don't ship ``rg``.
    """
    hits: list[tuple[Path, int, str]] = []
    for path in directory.rglob(suffix_glob):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if _DELETE_PATTERN.search(line):
                hits.append((path, line_no, line.rstrip()))
    return hits


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for name in ("006_popola_hitl.sql", "007_popola_hitl_metadata.sql"):
        sql = (_MIGRATIONS_DIR / name).read_text(encoding="utf-8")
        conn.executescript(sql)
    conn.commit()


# ── L4 case 1: PRAGMA exposes the 4 retention columns ────────────────────


def test_popola_hitl_schema_carries_retention_columns(tmp_path: Path) -> None:
    """The ``popola_hitl`` schema MUST expose the four documented
    retention columns: ``created_at`` + ``answered_via`` +
    ``answer_responder_id`` + ``metadata`` (the JSON column added by
    migration 007 carries ``cursor_run_id`` under
    ``metadata->>'cursor_run_id'``)."""
    db = tmp_path / "retention.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)

    rows = list(conn.execute("PRAGMA table_info(popola_hitl)").fetchall())
    column_names = {row["name"] for row in rows}

    expected_retention = {
        "created_at",
        "answered_via",
        "answer_responder_id",
        "metadata",  # holds cursor_run_id JSON cross-ref per L4 spec
    }
    missing = expected_retention - column_names
    assert not missing, (
        f"L4 regression: popola_hitl missing retention columns {sorted(missing)}; "
        f"actual columns: {sorted(column_names)}. SECURITY §3 L4 requires "
        f"these for the ≥ 90-day retention contract."
    )


# ── L4 case 2: no DELETE FROM popola_hitl in v0.8.7 ──────────────────────


def test_no_delete_from_popola_hitl_in_v0_8_7_code_paths() -> None:
    """No production source line issues ``DELETE FROM popola_hitl``.

    The ≥ 90-day retention floor is structurally enforced by the absence
    of a row-deletion code path (per the SECURITY L4 contract). This
    guard fires if any future PR adds a ``DELETE FROM popola_hitl``
    statement; the only acceptable mutators are the migrations
    themselves (which create / extend the table, never delete) and the
    ``UPDATE`` writes via :meth:`HITLStore.mark_answered`.

    Implementation: pure-Python ``pathlib.rglob`` + ``re`` scan so the
    test runs unchanged on CI runners that don't ship ripgrep.
    """
    src_root = _REPO_ROOT / "src" / "popolaloom"
    assert src_root.is_dir(), f"missing source tree: {src_root}"

    src_hits = _scan_for_delete(src_root, "*.py")
    assert not src_hits, (
        "L4 regression: production code emits 'DELETE FROM popola_hitl' "
        "-- the ≥ 90-day audit retention contract is broken.\n"
        "Offending lines:\n"
        + "\n".join(f"  {p}:{ln}: {line}" for p, ln, line in src_hits)
    )

    # Also scan migrations: a migration that DROPs or DELETEs the table's
    # rows would be a red flag. CREATE / ALTER / INDEX are allowed —
    # we only reject DELETE.
    mig_hits = _scan_for_delete(_MIGRATIONS_DIR, "*.sql")
    assert not mig_hits, (
        "L4 regression: migration file deletes popola_hitl rows.\n"
        "Offending lines:\n"
        + "\n".join(f"  {p}:{ln}: {line}" for p, ln, line in mig_hits)
    )
