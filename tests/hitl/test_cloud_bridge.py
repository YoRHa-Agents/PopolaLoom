"""Tests for :mod:`popolaloom.hitl.cloud_bridge` (v0.8.5 cloud HITL bridge)."""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from importlib import resources
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from popolaloom.hitl import HITLPrompt
from popolaloom.hitl.cloud_bridge import CloudHITLBridge
from popolaloom.hitl.sync import HITLStore


@pytest.fixture()
def hitl_store(tmp_path: Path) -> HITLStore:
    db_path = tmp_path / "hitl.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    migrations_pkg = Path(resources.files("popolaloom.migrations"))
    mig = (migrations_pkg / "006_popola_hitl.sql").read_text(encoding="utf-8")
    conn.executescript(mig)
    conn.commit()
    return HITLStore(conn)


def test_submit_request_creates_hitl_row(hitl_store: HITLStore) -> None:
    mock_lark = MagicMock()
    bridge = CloudHITLBridge(hitl_store, mock_lark)
    req = bridge.submit_request(
        task_id="t1",
        cursor_agent_id=None,
        cursor_run_id=None,
        prompt_title="T",
        prompt_body="B",
        options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
    )
    row = hitl_store.get(req.hitl_id)
    assert row is not None
    assert row["status"] == "pending"
    assert row["task_id"] == "t1"
    p = HITLPrompt.model_validate_json(row["prompt_json"])
    assert p.what == "B"
    assert {o.id for o in p.options} == {"a", "b"}


def test_submit_request_calls_lark_notifier(hitl_store: HITLStore) -> None:
    mock_lark = MagicMock()
    bridge = CloudHITLBridge(hitl_store, mock_lark)
    req = bridge.submit_request(
        task_id="task-x",
        cursor_agent_id="ag1",
        cursor_run_id=None,
        prompt_title="title",
        prompt_body="body",
        options=[{"id": "x", "label": "X"}, {"id": "y", "label": "Y"}],
    )
    mock_lark.send_hitl_card.assert_called_once()
    call_args = mock_lark.send_hitl_card.call_args
    prompt_arg = call_args[0][0]
    assert isinstance(prompt_arg, HITLPrompt)
    assert call_args.kwargs.get("hitl_id") == req.hitl_id


def test_submit_request_lark_failure_does_not_raise(hitl_store: HITLStore) -> None:
    mock_lark = MagicMock()
    mock_lark.send_hitl_card.side_effect = RuntimeError("lark exploded")
    bridge = CloudHITLBridge(hitl_store, mock_lark)
    req = bridge.submit_request(
        task_id="t2",
        cursor_agent_id=None,
        cursor_run_id=None,
        prompt_title="T",
        prompt_body="B",
        options=[{"id": "p", "label": "P"}, {"id": "q", "label": "Q"}],
    )
    row = hitl_store.get(req.hitl_id)
    assert row is not None
    assert row["status"] == "pending"


def test_await_answer_returns_when_marked(hitl_store: HITLStore) -> None:
    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id="t-async",
        cursor_agent_id=None,
        cursor_run_id=None,
        prompt_title="T",
        prompt_body="B",
        options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        timeout_s=120.0,
    )
    ready = threading.Barrier(2)
    reply_holder: dict[str, object] = {}

    def _answerer() -> None:
        ready.wait()
        bridge.submit_answer(
            req.hitl_id,
            "a",
            responder_id="human-1",
            channel="cloud",
        )

    def _waiter() -> None:
        ready.wait()
        reply_holder["r"] = bridge.await_answer(
            req.hitl_id,
            timeout_s=15.0,
            poll_interval_s=0.05,
        )

    t1 = threading.Thread(target=_answerer)
    t2 = threading.Thread(target=_waiter)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    reply = reply_holder["r"]
    assert reply is not None
    assert reply.option_id == "a"


def test_await_answer_times_out(hitl_store: HITLStore) -> None:
    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id="slow",
        cursor_agent_id=None,
        cursor_run_id=None,
        prompt_title="T",
        prompt_body="B",
        options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
    )
    seen = bridge.await_answer(
        req.hitl_id,
        timeout_s=0.15,
        poll_interval_s=0.05,
    )
    assert seen is None


def test_submit_answer_first_wins(tmp_path: Path) -> None:
    db_path = tmp_path / "race.db"
    conn_a = sqlite3.connect(db_path, check_same_thread=False)
    conn_a.row_factory = sqlite3.Row
    conn_b = sqlite3.connect(db_path, check_same_thread=False)
    conn_b.row_factory = sqlite3.Row
    migrations_pkg = Path(resources.files("popolaloom.migrations"))
    mig = (migrations_pkg / "006_popola_hitl.sql").read_text(encoding="utf-8")
    conn_a.executescript(mig)
    conn_a.commit()
    store_main = HITLStore(conn_a)
    bridge = CloudHITLBridge(store_main, None)
    req = bridge.submit_request(
        task_id="race",
        cursor_agent_id=None,
        cursor_run_id=None,
        prompt_title="T",
        prompt_body="B",
        options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
    )
    barrier = threading.Barrier(2)
    outcomes: dict[int, tuple[bool, str | None]] = {}

    def _try(tid: int) -> None:
        store = HITLStore(conn_a if tid == 0 else conn_b)
        b = CloudHITLBridge(store, None)
        barrier.wait()
        outcomes[tid] = b.submit_answer(
            req.hitl_id,
            "a",
            responder_id=f"u{tid}",
            channel="cloud",
        )

    threads = [threading.Thread(target=_try, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    oks = [outcomes[i][0] for i in range(2)]
    assert sum(oks) == 1
    conn_a.close()
    conn_b.close()


def test_submit_answer_idempotent_after_first(hitl_store: HITLStore) -> None:
    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id="idem",
        cursor_agent_id=None,
        cursor_run_id=None,
        prompt_title="T",
        prompt_body="B",
        options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
    )
    ok1, _ = bridge.submit_answer(
        req.hitl_id, "b", responder_id="one", channel="cloud"
    )
    ok2, already2 = bridge.submit_answer(
        req.hitl_id, "b", responder_id="two", channel="cloud"
    )
    ok3, already3 = bridge.submit_answer(
        req.hitl_id, "b", responder_id="three", channel="cloud"
    )
    assert ok1 is True
    assert ok2 is False
    assert ok3 is False
    assert already2
    assert already3


def test_cloud_channel_recorded(hitl_store: HITLStore) -> None:
    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id="ch",
        cursor_agent_id=None,
        cursor_run_id=None,
        prompt_title="T",
        prompt_body="B",
        options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
    )
    bridge.submit_answer(
        req.hitl_id,
        "a",
        responder_id="r1",
        channel="cloud",
    )
    row = hitl_store.get(req.hitl_id)
    assert row is not None
    assert row["answered_via"] == "cloud"


def test_request_includes_cursor_ids(hitl_store: HITLStore) -> None:
    bridge = CloudHITLBridge(hitl_store, None)
    agent = uuid.uuid4().hex[:8]
    run = uuid.uuid4().hex[:8]
    req = bridge.submit_request(
        task_id="meta-task",
        cursor_agent_id=agent,
        cursor_run_id=run,
        prompt_title="T",
        prompt_body="B",
        options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        metadata={"env": "test"},
    )
    row = hitl_store.get(req.hitl_id)
    assert row is not None
    p = HITLPrompt.model_validate_json(row["prompt_json"])
    assert agent in p.why
    assert run in p.why
    assert "env" in p.why
    assert req.cursor_agent_id == agent
    assert req.cursor_run_id == run
    assert req.metadata.get("env") == "test"


def test_options_propagate(hitl_store: HITLStore) -> None:
    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id="opt",
        cursor_agent_id=None,
        cursor_run_id=None,
        prompt_title="Pick",
        prompt_body="?",
        options=[
            {"id": "blue", "label": "Blue"},
            {"id": "green", "label": "Green"},
            {"id": "red", "label": "Red"},
        ],
    )
    ids = [o.id for o in req.prompt.options]
    assert ids == ["blue", "green", "red"]
    row = hitl_store.get(req.hitl_id)
    loaded = HITLPrompt.model_validate_json(row["prompt_json"])  # type: ignore[index]
    assert len(loaded.options) == 3


def test_deadline_at_set(hitl_store: HITLStore) -> None:
    bridge = CloudHITLBridge(hitl_store, None, default_timeout_s=600.0)
    t0 = time.time()
    req = bridge.submit_request(
        task_id="dl",
        cursor_agent_id=None,
        cursor_run_id=None,
        prompt_title="T",
        prompt_body="B",
        options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        timeout_s=180.0,
    )
    row = hitl_store.get(req.hitl_id)
    assert row is not None
    dl = req.deadline_at.timestamp()
    assert dl >= req.created_at.timestamp() + 179.5
    assert dl <= time.time() + 400
    assert t0 < dl
