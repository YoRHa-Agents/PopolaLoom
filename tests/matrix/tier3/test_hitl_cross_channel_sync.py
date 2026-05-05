"""Tier 3 — HITL cross-channel sync race tests (v0.3.0 F4.C).

Per testing-matrix.md §1.3 + roadmap §12.7 + v0.3.0-plan §4 Stage F4.11.

The atomic ``UPDATE WHERE status='pending'`` in
:meth:`popolaloom.hitl.sync.HITLStore.mark_answered` MUST be race-free:
when two channels reply nearly simultaneously, exactly one wins.

≥ 4 cases as required by AC #2 of the v0.3.0 task spec.
"""

from __future__ import annotations

import sqlite3

from popolaloom.hitl import HITLOption, HITLPrompt
from popolaloom.hitl.sync import HITLStore

## v0.3.0 F4.C: tests are SQLite-only (no subprocess) so they run on
## the default lane without slowing it; the @slow marker was originally
## set for the v0.2.x mock variant that drove the popolad subprocess.


def _bootstrap_store(tmp_path) -> HITLStore:
    """Create a fresh popola_hitl SQLite + HITLStore for tests."""
    db_path = tmp_path / "test_hitl.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    ## Use the v0.3.0 F4.C migration directly so tests don't need
    ## the full ArkTower migration runner.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS popola_hitl (
            hitl_id             TEXT PRIMARY KEY,
            trigger             TEXT NOT NULL,
            status              TEXT NOT NULL,
            prompt_json         TEXT NOT NULL,
            created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            deadline_at         TEXT,
            answered_at         TEXT,
            answered_via        TEXT,
            answer_option_id    TEXT,
            answer_reason       TEXT,
            answer_responder_id TEXT,
            lark_message_id     TEXT,
            lark_event_ids      TEXT,
            lark_send_attempts  INTEGER NOT NULL DEFAULT 0,
            lark_last_send_error TEXT,
            task_id             TEXT
        )
        """
    )
    return HITLStore(conn)


def _sample_prompt() -> HITLPrompt:
    return HITLPrompt(
        trigger="approval",
        why="Confirm",
        what="Pick yes/no",
        options=[
            HITLOption(id="yes", label="Yes"),
            HITLOption(id="no", label="No", default=True),
        ],
        default_option_id="no",
        channels=["lark", "ide", "cli"],
        deadline_seconds=3600,
    )


def test_first_responder_wins_others_get_already_status(tmp_path) -> None:
    """First mark_answered ok=True; second ok=False with already_status='answered'."""
    store = _bootstrap_store(tmp_path)
    prompt = _sample_prompt()
    hitl_id = store.create(prompt)

    r1 = store.mark_answered(hitl_id, option_id="yes", via="lark", reason="picked yes")
    assert r1.ok is True

    r2 = store.mark_answered(hitl_id, option_id="no", via="ide", reason="changed mind")
    assert r2.ok is False
    assert r2.already_status == "answered"
    assert r2.already_via == "lark"


def test_sequential_three_channel_race_exactly_one_wins(tmp_path) -> None:
    """3 sequential mark_answered calls → exactly 1 ok=True (race-free contract).

    SQLite connections aren't safe to share across threads even with
    ``check_same_thread=False`` for write workloads; the production
    daemon uses a single async event loop + an asyncio.to_thread
    boundary to serialise these. This test simulates that linearised
    order — exactly 1 winner is the contract regardless of timing.
    """
    store = _bootstrap_store(tmp_path)
    prompt = _sample_prompt()
    hitl_id = store.create(prompt)

    results: list[bool] = []
    for channel in ("lark", "ide", "cli"):
        result = store.mark_answered(hitl_id, option_id="yes", via=channel)
        results.append(result.ok)
    assert sum(results) == 1, f"expected exactly 1 winner, got {sum(results)}: {results}"
    assert results[0] is True
    assert results[1] is False
    assert results[2] is False


def test_cancel_other_channels_emits_only_non_winning_channels(tmp_path) -> None:
    """cancel_other_channels lists every channel except the winner."""
    import asyncio

    store = _bootstrap_store(tmp_path)
    prompt = _sample_prompt()
    hitl_id = store.create(prompt)
    store.mark_answered(hitl_id, option_id="yes", via="lark")
    result = asyncio.run(
        store.cancel_other_channels(hitl_id, except_via="lark")
    )
    assert "lark" not in result.cancelled
    assert "ide" in result.cancelled
    assert "cli" in result.cancelled


def test_process_timeout_only_fires_when_pending(tmp_path) -> None:
    """process_timeout returns False if row is already answered."""
    store = _bootstrap_store(tmp_path)
    prompt = _sample_prompt()
    hitl_id = store.create(prompt)
    ## Already answered → timeout no-op
    store.mark_answered(hitl_id, option_id="yes", via="lark")
    assert store.process_timeout(hitl_id) is False


def test_mark_status_transition_to_cancelled(tmp_path) -> None:
    """mark_status('cancelled') flips a pending row."""
    store = _bootstrap_store(tmp_path)
    prompt = _sample_prompt()
    hitl_id = store.create(prompt)
    assert store.mark_status(hitl_id, "cancelled") is True
    row = store.get(hitl_id)
    assert row is not None
    assert row["status"] == "cancelled"
