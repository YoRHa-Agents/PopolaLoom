"""Gap-filler coverage for :mod:`popolaloom.hitl.cloud_bridge` branches (v0.8.5)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from popolaloom.hitl.cloud_bridge import (
    CloudHITLBridge,
    bridge_for_daemon,
    build_default_bridge,
)
from popolaloom.hitl.sync import HITLStore, MarkAnsweredResult


@pytest.fixture()
def hitl_store(tmp_path: Path) -> HITLStore:
    db_path = tmp_path / "hitl.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    mig = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "006_popola_hitl.sql"
    ).read_text(encoding="utf-8")
    conn.executescript(mig)
    conn.commit()
    return HITLStore(conn)


@pytest.fixture()
def sqlite_conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "hitl_cov.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    mig = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "006_popola_hitl.sql"
    ).read_text(encoding="utf-8")
    conn.executescript(mig)
    conn.commit()
    return conn


def test_build_default_bridge_uses_noop_notifier_and_cover_send(
    sqlite_conn: sqlite3.Connection,
) -> None:
    bridge = build_default_bridge(sqlite_conn)
    req = bridge.submit_request(
        task_id="noop",
        cursor_agent_id=None,
        cursor_run_id=None,
        prompt_title="T",
        prompt_body="B",
        options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
    )
    row = bridge.store.get(req.hitl_id)
    assert row is not None
    assert row["status"] == "pending"


def test_bridge_for_daemon_none_store_returns_none() -> None:
    assert bridge_for_daemon(None) is None


def test_await_answer_missing_hitl_id_returns_none(hitl_store: HITLStore) -> None:
    bridge = CloudHITLBridge(hitl_store, None)
    got = bridge.await_answer(
        "nonexistent-hitl-id-hex",
        timeout_s=0.1,
        poll_interval_s=0.01,
    )
    assert got is None


def test_await_answer_timeout_row_status_returns_none(hitl_store: HITLStore) -> None:
    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id="to",
        cursor_agent_id=None,
        cursor_run_id=None,
        prompt_title="T",
        prompt_body="B",
        options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
    )
    ok = hitl_store.mark_status(req.hitl_id, "timeout")
    assert ok is True
    got = bridge.await_answer(req.hitl_id, timeout_s=1.0, poll_interval_s=0.01)
    assert got is None


def test_await_answer_cancelled_row_returns_none(hitl_store: HITLStore) -> None:
    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id="cx",
        cursor_agent_id=None,
        cursor_run_id=None,
        prompt_title="T",
        prompt_body="B",
        options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
    )
    ok = hitl_store.mark_status(req.hitl_id, "cancelled")
    assert ok is True
    assert bridge.await_answer(req.hitl_id, timeout_s=1.0, poll_interval_s=0.01) is None


def test_submit_answer_returns_channel_on_success(hitl_store: HITLStore) -> None:
    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id="win",
        cursor_agent_id=None,
        cursor_run_id=None,
        prompt_title="T",
        prompt_body="B",
        options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
    )
    ok, via = bridge.submit_answer(
        req.hitl_id, "b", responder_id="r99", channel="mcp",
    )
    assert ok is True
    assert via == "mcp"


def test_submit_answer_lost_race_missing_row_returns_none_pair() -> None:
    inner = MagicMock()
    inner.mark_answered.return_value = MarkAnsweredResult(ok=False)
    inner.get.return_value = None
    bridge = CloudHITLBridge(inner, MagicMock())
    ok, descriptor = bridge.submit_answer("hid", "a", responder_id="r1")
    assert ok is False
    assert descriptor is None
