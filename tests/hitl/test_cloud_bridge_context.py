"""v0.8.7 T2.1.3 — context validation + persistence + mis-route defense.

Covers AC (a) "persists ``idempotency_key`` into ``popola_hitl.metadata``",
(c) "``cursor_agent_id`` / ``cursor_run_id`` / ``task_id`` validated non-null
on the request side", and (d) "``submit_answer`` rejects with HTTP 400 when
the inbound ``hitl_id`` does NOT match the row's stored
``(cursor_agent_id, cursor_run_id)`` tuple".

The replay-window subset (AC (b) + AC (e) replay tests) lives in the sibling
:mod:`tests.hitl.test_cloud_bridge_replay` module.
"""

from __future__ import annotations

from importlib import resources
from importlib import resources
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.rpc import create_app
from popolaloom.daemon.server import Popolad
from popolaloom.hitl.cloud_bridge import (
    CloudHITLBridge,
    compute_idempotency_key,
)
from popolaloom.hitl.sync import HITLStore

_MIGRATIONS = ("006_popola_hitl.sql", "007_popola_hitl_metadata.sql")


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply both 006 + 007 so ``popola_hitl.metadata`` is available."""
    repo_root = Path(__file__).resolve().parents[2]
    for name in _MIGRATIONS:
        sql = (Path(resources.files("popolaloom.migrations")) / name).read_text(encoding="utf-8")
        conn.executescript(sql)
    conn.commit()


@pytest.fixture()
def hitl_store(tmp_path: Path) -> HITLStore:
    """v0.8.7-aware fixture that applies migrations 006 + 007 (metadata col)."""
    db_path = tmp_path / "ctx.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)
    return HITLStore(conn)


@pytest.fixture()
def tmp_events_dir(tmp_path: Path) -> Path:
    root = tmp_path / "events"
    root.mkdir()
    return root


# ── AC (a) — persist idempotency_key + cursor tuple in metadata ─────────


def test_submit_request_persists_idempotency_key_in_metadata(
    hitl_store: HITLStore,
) -> None:
    """v0.8.7 AC (a): ``CloudHITLBridge.submit_request`` writes the
    ``idempotency_key`` into the ``popola_hitl.metadata`` JSON column under
    ``$.idempotency_key``. The persisted JSON also surfaces the structured
    ``(cursor_agent_id, cursor_run_id, task_id)`` tuple per
    ``mcp-tool-contract.md`` §5 storage row.
    """
    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id="ctx-task",
        cursor_agent_id="agent-A",
        cursor_run_id="run-A",
        prompt_title="Title",
        prompt_body="Body?",
        options=[{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
        idempotency_key="explicit-key-32hex",
    )
    cur = hitl_store.conn.execute(
        "SELECT metadata FROM popola_hitl WHERE hitl_id = ?",
        (req.hitl_id,),
    )
    row = cur.fetchone()
    assert row is not None
    parsed = json.loads(row["metadata"])
    assert parsed["idempotency_key"] == "explicit-key-32hex"
    assert parsed["cursor_agent_id"] == "agent-A"
    assert parsed["cursor_run_id"] == "run-A"
    assert parsed["task_id"] == "ctx-task"
    assert req.metadata["idempotency_key"] == "explicit-key-32hex"
    assert req.deduped is False


def test_submit_request_auto_derives_idempotency_key_when_omitted(
    hitl_store: HITLStore,
) -> None:
    """When the caller omits ``idempotency_key``, the bridge derives
    ``sha256(task_id|agent_id|run_id|question_text)[:32]`` per
    ``mcp-tool-contract.md`` §5 default rule. The same tuple → same key."""
    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id="autoder",
        cursor_agent_id="ag",
        cursor_run_id="rn",
        prompt_title="t",
        prompt_body="Question text",
        options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
    )
    expected = compute_idempotency_key(
        task_id="autoder",
        cursor_agent_id="ag",
        cursor_run_id="rn",
        prompt_body="Question text",
    )
    assert len(expected) == 32
    assert req.metadata["idempotency_key"] == expected


def test_submit_request_lookup_by_idempotency_key_round_trips(
    hitl_store: HITLStore,
) -> None:
    """``lookup_by_idempotency_key`` returns the same row created by
    ``submit_request`` (verifies the JSON1 SELECT path used by the daemon)."""
    bridge = CloudHITLBridge(hitl_store, None)
    key = "round-trip-key-aaaa"
    req = bridge.submit_request(
        task_id="rt",
        cursor_agent_id="ag",
        cursor_run_id="rn",
        prompt_title="t",
        prompt_body="b",
        options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        idempotency_key=key,
    )
    found = bridge.lookup_by_idempotency_key(key)
    assert found is not None
    assert found.hitl_id == req.hitl_id
    assert found.cursor_agent_id == "ag"
    assert found.cursor_run_id == "rn"
    assert found.deduped is True


# ── AC (c) — invalid_context for missing cursor_agent_id / run_id ───────


@pytest.mark.asyncio
async def test_rpc_request_rejects_missing_cursor_agent_id(
    tmp_events_dir: Path, tmp_path: Path
) -> None:
    """v0.8.7 AC (c): ``POST /hitl/cloud/request`` returns 400 with
    ``invalid_context`` when ``cursor_agent_id`` is omitted/empty."""
    db_path = tmp_path / "rpc.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)
    store = HITLStore(conn)
    popolad = Popolad(
        events_dir=tmp_events_dir,
        adapter=lambda *args, **kwargs: ["echo"],  # type: ignore[arg-type,misc]
    )
    popolad.hitl_store = store
    popolad._event_logs["t"] = EventLog(  # type: ignore[attr-defined]
        path=tmp_events_dir / "t.jsonl"
    )

    body = {
        "task_id": "t",
        "cursor_run_id": "run-x",
        "prompt_title": "Title",
        "prompt_body": "Body",
        "options": [
            {"id": "y", "label": "Yes"},
            {"id": "n", "label": "No"},
        ],
    }
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/hitl/cloud/request", json=body)
    assert resp.status_code == 400
    assert "invalid_context" in resp.json()["detail"]
    assert "cursor_agent_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_rpc_request_rejects_missing_cursor_run_id(
    tmp_events_dir: Path, tmp_path: Path
) -> None:
    """v0.8.7 AC (c): ``POST /hitl/cloud/request`` returns 400 with
    ``invalid_context`` when ``cursor_run_id`` is omitted/empty."""
    db_path = tmp_path / "rpc.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)
    store = HITLStore(conn)
    popolad = Popolad(
        events_dir=tmp_events_dir,
        adapter=lambda *args, **kwargs: ["echo"],  # type: ignore[arg-type,misc]
    )
    popolad.hitl_store = store
    popolad._event_logs["t"] = EventLog(  # type: ignore[attr-defined]
        path=tmp_events_dir / "t.jsonl"
    )

    body = {
        "task_id": "t",
        "cursor_agent_id": "ag-x",
        "cursor_run_id": "   ",
        "prompt_title": "Title",
        "prompt_body": "Body",
        "options": [
            {"id": "y", "label": "Yes"},
            {"id": "n", "label": "No"},
        ],
    }
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/hitl/cloud/request", json=body)
    assert resp.status_code == 400
    assert "invalid_context" in resp.json()["detail"]
    assert "cursor_run_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_rpc_request_accepts_full_context_and_returns_deduped_false(
    tmp_events_dir: Path, tmp_path: Path
) -> None:
    """The happy path: a fully-populated body returns ``deduped=False`` on
    first call; the response also surfaces the cursor tuple for clients."""
    db_path = tmp_path / "happy.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)
    store = HITLStore(conn)
    popolad = Popolad(
        events_dir=tmp_events_dir,
        adapter=lambda *args, **kwargs: ["echo"],  # type: ignore[arg-type,misc]
    )
    popolad.hitl_store = store
    popolad._event_logs["t-happy"] = EventLog(  # type: ignore[attr-defined]
        path=tmp_events_dir / "t-happy.jsonl"
    )

    body = {
        "task_id": "t-happy",
        "cursor_agent_id": "ag-happy",
        "cursor_run_id": "run-happy",
        "prompt_title": "Title",
        "prompt_body": "Body",
        "options": [
            {"id": "y", "label": "Yes"},
            {"id": "n", "label": "No"},
        ],
    }
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/hitl/cloud/request", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["deduped"] is False
    assert data["status"] == "pending"
    assert data["cursor_agent_id"] == "ag-happy"
    assert data["cursor_run_id"] == "run-happy"


# ── AC (d) — mis-route defense in submit_answer ─────────────────────────


def test_submit_answer_accepts_matching_cursor_tuple(
    hitl_store: HITLStore,
) -> None:
    """When ``expected_cursor_*`` match the row's stored tuple, the call
    proceeds normally and ``mark_answered`` records the win (sole-writer
    invariant I-4 preserved)."""
    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id="t-ok",
        cursor_agent_id="ag-ok",
        cursor_run_id="run-ok",
        prompt_title="t",
        prompt_body="b",
        options=[{"id": "yes", "label": "Y"}, {"id": "no", "label": "N"}],
    )
    ok, descriptor = bridge.submit_answer(
        req.hitl_id,
        "yes",
        responder_id="approver",
        channel="lark",
        expected_cursor_agent_id="ag-ok",
        expected_cursor_run_id="run-ok",
    )
    assert ok is True
    assert descriptor == "lark"
    row = hitl_store.get(req.hitl_id)
    assert row is not None
    assert row["status"] == "answered"
    assert row["answered_via"] == "lark"


def test_submit_answer_rejects_misrouted_cursor_run_id(
    hitl_store: HITLStore,
) -> None:
    """v0.8.7 AC (d) (P2-adjacent): a Lark webhook posting an answer for
    ``hitl_id`` belonging to a different ``cursor_run_id`` MUST be
    rejected — the bridge returns ``(False, "mis-route:...")`` and
    ``mark_answered`` is never invoked, so the row stays ``pending``."""
    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id="t-mis",
        cursor_agent_id="legit-agent",
        cursor_run_id="legit-run",
        prompt_title="t",
        prompt_body="b",
        options=[{"id": "yes", "label": "Y"}, {"id": "no", "label": "N"}],
    )
    ok, descriptor = bridge.submit_answer(
        req.hitl_id,
        "yes",
        responder_id="forged-clicker",
        channel="lark",
        expected_cursor_agent_id="legit-agent",
        expected_cursor_run_id="OTHER-run",
    )
    assert ok is False
    assert descriptor is not None
    assert descriptor.startswith("mis-route:")
    assert "OTHER-run" in descriptor
    row = hitl_store.get(req.hitl_id)
    assert row is not None
    assert row["status"] == "pending"
    assert row["answered_at"] is None


def test_submit_answer_rejects_misrouted_cursor_agent_id(
    hitl_store: HITLStore,
) -> None:
    """The agent id is also part of the mis-route key (defense-in-depth):
    a mismatch on either tuple component triggers the rejection."""
    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id="t-misag",
        cursor_agent_id="legit-agent",
        cursor_run_id="legit-run",
        prompt_title="t",
        prompt_body="b",
        options=[{"id": "yes", "label": "Y"}, {"id": "no", "label": "N"}],
    )
    ok, descriptor = bridge.submit_answer(
        req.hitl_id,
        "yes",
        responder_id="forged-clicker",
        channel="lark",
        expected_cursor_agent_id="OTHER-agent",
        expected_cursor_run_id="legit-run",
    )
    assert ok is False
    assert descriptor is not None
    assert descriptor.startswith("mis-route:")
    row = hitl_store.get(req.hitl_id)
    assert row is not None
    assert row["status"] == "pending"


def test_submit_answer_without_expected_cursor_kwargs_keeps_legacy_behavior(
    hitl_store: HITLStore,
) -> None:
    """Backward-compat: callers that omit the new ``expected_cursor_*``
    kwargs (v0.8.5 listener path) MUST still answer — the mis-route check
    only triggers when the inbound tuple is supplied."""
    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id="t-legacy",
        cursor_agent_id="legit-agent",
        cursor_run_id="legit-run",
        prompt_title="t",
        prompt_body="b",
        options=[{"id": "yes", "label": "Y"}, {"id": "no", "label": "N"}],
    )
    ok, descriptor = bridge.submit_answer(
        req.hitl_id,
        "yes",
        responder_id="legacy-clicker",
        channel="cloud",
    )
    assert ok is True
    assert descriptor == "cloud"
