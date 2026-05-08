"""M8 / SECURITY R3 — dedup state survives popolad restart.

Per ``.local/.agent/active/v0.8.7-cloud-hitl-prod/SECURITY_CHECKLIST.md`` §10
gate criterion **C4**:

    `tests/daemon/test_dedup_after_restart.py` is green AND
    `tests/hitl/test_cloud_hitl_audit_retention.py` is green
    (these are the two highest-leverage tests that simultaneously cover
    lateral-movement, audit retention, and replay-safety — the
    "tracer-bullet" subset).

The functional intent (R3 — dedup state survives ``popolad`` restart) is
covered by the canonical test at
``tests/hitl/test_cloud_bridge_replay.py:213``
(``test_restart_then_replay_still_short_circuits``), but the gate
criterion cites a different path. Per REVIEW.md M8, this file is the
lightweight forwarder so the gate path resolves.

We import the canonical test directly and re-run it under the cited
path. The test is small (40 lines of fixture + assertions), so the
double-run cost is trivial — and any future drift on the canonical
test surfaces here too.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# Re-export so pytest collects the canonical test under this path. The
# import is defensive: if the canonical test moves, this will fail loudly
# (No Silent Failures) instead of silently passing zero cases.
from tests.hitl.test_cloud_bridge_replay import (  # noqa: F401
    test_restart_then_replay_still_short_circuits as _canonical_test,
)

# Re-bind under the gate-cited name so pytest collects + runs the
# canonical forwarder via this file path.
test_restart_then_replay_still_short_circuits_forwarded = _canonical_test


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Forwarder fixture matching the canonical test's expected signature.

    The canonical ``test_restart_then_replay_still_short_circuits`` takes a
    ``db_path: Path`` fixture; we re-construct it here so the import-side
    re-export above can resolve a real on-disk SQLite file.
    """
    db = tmp_path / "dedup.db"
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    repo_root = Path(__file__).resolve().parents[2]
    for name in ("006_popola_hitl.sql", "007_popola_hitl_metadata.sql"):
        sql = (repo_root / "migrations" / name).read_text(encoding="utf-8")
        conn.executescript(sql)
    conn.commit()
    conn.close()
    return db


def test_dedup_after_restart_canonical_test_exists() -> None:
    """The canonical test for SECURITY R3 lives at the documented path —
    catches a file rename / removal so the gate reference doesn't drift
    silently.
    """
    canonical = (
        Path(__file__).resolve().parents[1]
        / "hitl"
        / "test_cloud_bridge_replay.py"
    )
    assert canonical.is_file(), (
        f"M8 regression: canonical R3 test missing at {canonical}; "
        f"SECURITY §10 C4 gate criterion cites this path."
    )
    text = canonical.read_text(encoding="utf-8")
    assert (
        "def test_restart_then_replay_still_short_circuits(" in text
    ), (
        "M8 regression: the function name "
        "test_restart_then_replay_still_short_circuits "
        "moved or was renamed in tests/hitl/test_cloud_bridge_replay.py"
    )
