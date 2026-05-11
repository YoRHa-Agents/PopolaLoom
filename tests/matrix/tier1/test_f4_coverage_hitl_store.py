"""Tier 1 — fast-lane HITLStore coverage tests (v0.3.0 F4.C).

The full race tests live at
``tests/matrix/tier3/test_hitl_cross_channel_sync.py`` (slow lane).
This file replicates the per-method assertions in the default lane so
the daemon HITL store coverage stays ≥ 90 %.
"""

from __future__ import annotations

from importlib import resources
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from popolaloom.hitl import HITLOption, HITLPrompt, HITLReply, HITLStore

_counter = 0


def _make_prompt(prompt_id: str | None = None) -> HITLPrompt:
    global _counter
    _counter += 1
    return HITLPrompt(
        trigger="approval",
        why="why",
        what="what",
        options=[HITLOption(id="a", label="A"), HITLOption(id="b", label="B")],
        default_option_id="a",
        channels=["lark", "ide"],
        deadline_seconds=3600,
        prompt_id=prompt_id or f"hitl-store-{_counter}",
    )


@pytest.fixture()
def store(tmp_path: Path) -> HITLStore:
    db_path = tmp_path / "hitl.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    migration_sql = (Path(resources.files("popolaloom.migrations")) / "006_popola_hitl.sql").read_text(encoding="utf-8")
    conn.executescript(migration_sql)
    conn.commit()
    return HITLStore(conn)


# ── mark_answered (winner + loser paths) ─────────────────────────────────


def test_mark_answered_winner(store: HITLStore) -> None:
    hitl_id = store.create(_make_prompt())
    result = store.mark_answered(
        hitl_id, option_id="a", via="lark", reason="r1", responder_id="ou_a"
    )
    assert result.ok is True
    row = store.get(hitl_id)
    assert row is not None
    assert row["status"] == "answered"
    assert row["answered_via"] == "lark"


def test_mark_answered_loses_race_with_same_via(store: HITLStore) -> None:
    """Second mark_answered returns ok=False with already_status='answered'."""
    hitl_id = store.create(_make_prompt())
    store.mark_answered(hitl_id, option_id="a", via="lark", responder_id="ou_a")
    second = store.mark_answered(hitl_id, option_id="b", via="ide")
    assert second.ok is False
    assert second.already_status == "answered"
    assert second.already_via == "lark"


def test_mark_answered_no_existing_row(store: HITLStore) -> None:
    """Marking an unknown hitl_id returns ok=False, already_status=None."""
    result = store.mark_answered("hitl-nope", option_id="a", via="cli")
    assert result.ok is False
    assert result.already_status is None


# ── mark_status ─────────────────────────────────────────────────────────


def test_mark_status_to_cancelled(store: HITLStore) -> None:
    hitl_id = store.create(_make_prompt())
    assert store.mark_status(hitl_id, "cancelled") is True
    row = store.get(hitl_id)
    assert row["status"] == "cancelled"  # type: ignore[index]


def test_mark_status_to_timeout(store: HITLStore) -> None:
    hitl_id = store.create(_make_prompt())
    assert store.mark_status(hitl_id, "timeout") is True


def test_mark_status_already_answered_returns_false(store: HITLStore) -> None:
    hitl_id = store.create(_make_prompt())
    store.mark_answered(hitl_id, option_id="a", via="cli")
    # mark_status to cancelled fails because row is already 'answered'.
    assert store.mark_status(hitl_id, "cancelled") is False


def test_mark_status_invalid_status_raises(store: HITLStore) -> None:
    with pytest.raises(ValueError):
        store.mark_status("hitl-x", "answered")


# ── update_lark_send ────────────────────────────────────────────────────


def test_update_lark_send_records_and_increments(store: HITLStore) -> None:
    hitl_id = store.create(_make_prompt())
    store.update_lark_send(
        hitl_id, message_id="om_a", last_send_error=None, attempts_increment=1
    )
    store.update_lark_send(
        hitl_id, message_id=None, last_send_error="x", attempts_increment=2
    )
    row = store.get(hitl_id)
    assert row is not None
    # message_id retained because second update used None (COALESCE)
    assert row["lark_message_id"] == "om_a"
    assert row["lark_send_attempts"] == 3
    assert row["lark_last_send_error"] == "x"


# ── append_lark_event_id ────────────────────────────────────────────────


def test_append_lark_event_id_dedup(store: HITLStore) -> None:
    hitl_id = store.create(_make_prompt())
    assert store.append_lark_event_id(hitl_id, "ev-1") is True
    assert store.append_lark_event_id(hitl_id, "ev-1") is False
    assert store.append_lark_event_id(hitl_id, "ev-2") is True


def test_append_lark_event_id_unknown_id_returns_false(store: HITLStore) -> None:
    assert store.append_lark_event_id("hitl-nope", "ev-1") is False


def test_append_lark_event_id_corrupt_existing_json(store: HITLStore) -> None:
    """Corrupt JSON in the column gets reset to a fresh array."""
    hitl_id = store.create(_make_prompt())
    store.conn.execute(
        "UPDATE popola_hitl SET lark_event_ids = ? WHERE hitl_id = ?",
        ("{not-valid-json", hitl_id),
    )
    store.conn.commit()
    assert store.append_lark_event_id(hitl_id, "ev-1") is True


# ── list_pending / list_overdue ─────────────────────────────────────────


def test_list_pending_empty(store: HITLStore) -> None:
    assert store.list_pending() == []


def test_list_overdue_empty(store: HITLStore) -> None:
    assert store.list_overdue() == []


def test_list_overdue_with_only_future_deadlines(store: HITLStore) -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    store.create(_make_prompt(), deadline_at=future)
    assert store.list_overdue() == []


def test_list_pending_excludes_answered(store: HITLStore) -> None:
    a = store.create(_make_prompt())
    b = store.create(_make_prompt())
    store.mark_answered(a, option_id="a", via="cli")
    pending = store.list_pending()
    assert {p["hitl_id"] for p in pending} == {b}


# ── process_timeout ────────────────────────────────────────────────────


def test_process_timeout_unknown_hitl_id(store: HITLStore) -> None:
    assert store.process_timeout("hitl-nope") is False


def test_process_timeout_already_answered(store: HITLStore) -> None:
    hitl_id = store.create(_make_prompt())
    store.mark_answered(hitl_id, option_id="a", via="cli")
    assert store.process_timeout(hitl_id) is False


def test_process_timeout_corrupt_prompt_json_returns_false(store: HITLStore) -> None:
    """When prompt_json is invalid JSON, process_timeout returns False."""
    hitl_id = store.create(_make_prompt())
    store.conn.execute(
        "UPDATE popola_hitl SET prompt_json = ? WHERE hitl_id = ?",
        ("not-json", hitl_id),
    )
    store.conn.commit()
    assert store.process_timeout(hitl_id) is False


def test_process_timeout_applies_default(store: HITLStore) -> None:
    past = datetime.now(UTC) - timedelta(hours=2)
    hitl_id = store.create(_make_prompt(), deadline_at=past)
    assert store.process_timeout(hitl_id) is True
    row = store.get(hitl_id)
    assert row is not None
    assert row["status"] == "timeout"


# ── fold_reply ─────────────────────────────────────────────────────────


def test_fold_reply_routes_to_mark_answered(store: HITLStore) -> None:
    hitl_id = store.create(_make_prompt())
    reply = HITLReply(
        hitl_id=hitl_id, option_id="a", via="lark", responder="ou_x"
    )
    result = store.fold_reply(reply)
    assert result.ok is True


def test_fold_reply_unsupported_via_raises(store: HITLStore) -> None:
    bad = HITLReply.model_construct(
        hitl_id="hitl-x", option_id="a", via="bogus"  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match=r"unsupported reply channel"):
        store.fold_reply(bad)


# ── cancel_other_channels ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_other_channels_no_emitter(store: HITLStore) -> None:
    """Without an emitter, cancel_other_channels still returns the channel list."""
    hitl_id = store.create(_make_prompt())
    store.mark_answered(hitl_id, option_id="a", via="lark")
    result = await store.cancel_other_channels(hitl_id, except_via="lark")
    assert "ide" in result.cancelled
    assert "lark" in result.skipped


@pytest.mark.asyncio
async def test_cancel_other_channels_unknown_hitl(store: HITLStore) -> None:
    """Unknown hitl_id → returns empty CancelOtherChannelsResult."""
    result = await store.cancel_other_channels("hitl-nope", except_via="lark")
    assert result.cancelled == []
    assert result.skipped == []


@pytest.mark.asyncio
async def test_cancel_other_channels_emitter_failure_logged(
    store: HITLStore,
) -> None:
    """When the emitter raises, cancel_other_channels logs + continues."""
    hitl_id = store.create(_make_prompt())
    store.mark_answered(hitl_id, option_id="a", via="lark")

    async def failing_emitter(hid: str, channel: str) -> None:
        raise RuntimeError("emitter blew up")

    result = await store.cancel_other_channels(
        hitl_id, except_via="lark", emitter=failing_emitter
    )
    # The cancelled list still contains channels (we don't fail because of emitter errors).
    assert "ide" in result.cancelled


@pytest.mark.asyncio
async def test_cancel_other_channels_corrupt_prompt_json(
    store: HITLStore,
) -> None:
    """Bad prompt_json yields an empty result (logged, not raised)."""
    hitl_id = store.create(_make_prompt())
    store.conn.execute(
        "UPDATE popola_hitl SET prompt_json = ? WHERE hitl_id = ?",
        ("not-json", hitl_id),
    )
    store.conn.commit()
    result = await store.cancel_other_channels(hitl_id, except_via="lark")
    assert result.cancelled == []
    assert result.skipped == []
