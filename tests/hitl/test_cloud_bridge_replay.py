"""v0.8.7 T2.1.3 — replay / dedup-window / restart-still-dedups.

Covers AC (b) "1-hour window short-circuits", (e) replay-related cases:

- Replay inside the window short-circuits to the existing ``hitl_id`` with
  ``deduped=True`` and does NOT call the Lark notifier a second time.
- Replay after the window creates a new ``hitl_id`` (same key, new row).
- Restart-then-replay still short-circuits — SECURITY R3: the dedup state
  lives in SQLite (``popola_hitl.metadata``) so it survives a daemon
  restart; no in-memory cache is involved.
- Terminal-state rows (``cancelled`` / ``timeout``) are NOT dedup-eligible
  per ``mcp-tool-contract.md`` §5 terminal-state row.
- The daemon RPC handler observes ``deduped=true`` on the second call.
"""

from __future__ import annotations

from importlib import resources
from importlib import resources
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.rpc import create_app
from popolaloom.daemon.server import Popolad
from popolaloom.hitl.cloud_bridge import (
    CLOUD_HITL_IDEMPOTENCY_WINDOW_S,
    CloudHITLBridge,
    compute_idempotency_key,
)
from popolaloom.hitl.sync import HITLStore

_MIGRATIONS = ("006_popola_hitl.sql", "007_popola_hitl_metadata.sql")


def _apply_migrations(conn: sqlite3.Connection) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    for name in _MIGRATIONS:
        sql = (Path(resources.files("popolaloom.migrations")) / name).read_text(encoding="utf-8")
        conn.executescript(sql)
    conn.commit()


@pytest.fixture()
def hitl_store(tmp_path: Path) -> HITLStore:
    db_path = tmp_path / "replay.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)
    return HITLStore(conn)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Reusable on-disk SQLite path for the restart-then-replay test."""
    p = tmp_path / "restart.db"
    conn = sqlite3.connect(p, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)
    conn.close()
    return p


# ── AC (b) + (e) replay-within-window short-circuits ───────────────────


def test_replay_within_window_returns_same_hitl_id(
    hitl_store: HITLStore,
) -> None:
    """v0.8.7 AC (b): two ``submit_request`` calls with the same key inside
    the 1-hour window return the same ``hitl_id`` and the second result is
    flagged ``deduped=True``."""
    notifier = MagicMock()
    bridge = CloudHITLBridge(hitl_store, notifier)
    options = [{"id": "yes", "label": "Y"}, {"id": "no", "label": "N"}]
    first = bridge.submit_request(
        task_id="t-replay",
        cursor_agent_id="ag",
        cursor_run_id="run",
        prompt_title="t",
        prompt_body="Same question?",
        options=options,
        idempotency_key="replay-key-aaaa",
    )
    assert first.deduped is False
    second = bridge.submit_request(
        task_id="t-replay",
        cursor_agent_id="ag",
        cursor_run_id="run",
        prompt_title="t",
        prompt_body="Same question?",
        options=options,
        idempotency_key="replay-key-aaaa",
    )
    assert second.deduped is True
    assert second.hitl_id == first.hitl_id
    # Lark notifier MUST be called exactly once — dedup hits do not re-fan-out.
    assert notifier.send_hitl_card.call_count == 1


def test_replay_via_auto_derived_key_short_circuits(
    hitl_store: HITLStore,
) -> None:
    """When neither call passes ``idempotency_key=``, the bridge derives the
    same sha256-truncated key from ``(task_id, agent_id, run_id, body)`` so
    even MCP tool callers that don't know about the key benefit from dedup."""
    notifier = MagicMock()
    bridge = CloudHITLBridge(hitl_store, notifier)
    options = [{"id": "yes", "label": "Y"}, {"id": "no", "label": "N"}]
    first = bridge.submit_request(
        task_id="auto-replay",
        cursor_agent_id="ag",
        cursor_run_id="run",
        prompt_title="t",
        prompt_body="Identical question",
        options=options,
    )
    second = bridge.submit_request(
        task_id="auto-replay",
        cursor_agent_id="ag",
        cursor_run_id="run",
        prompt_title="t",
        prompt_body="Identical question",
        options=options,
    )
    assert first.hitl_id == second.hitl_id
    assert second.deduped is True
    assert notifier.send_hitl_card.call_count == 1
    derived = compute_idempotency_key(
        task_id="auto-replay",
        cursor_agent_id="ag",
        cursor_run_id="run",
        prompt_body="Identical question",
    )
    assert first.metadata["idempotency_key"] == derived


# ── AC (e) replay-after-window creates fresh row ───────────────────────


def test_replay_after_window_creates_new_row(
    hitl_store: HITLStore,
) -> None:
    """Per ``mcp-tool-contract.md`` §5: a replay older than 1 hour is NOT a
    dedup hit; the bridge creates a fresh row. We backdate the first row's
    ``created_at`` directly in SQLite to simulate the elapsed window."""
    notifier = MagicMock()
    bridge = CloudHITLBridge(hitl_store, notifier)
    first = bridge.submit_request(
        task_id="window",
        cursor_agent_id="ag",
        cursor_run_id="run",
        prompt_title="t",
        prompt_body="Body",
        options=[{"id": "yes", "label": "Y"}, {"id": "no", "label": "N"}],
        idempotency_key="window-key-1234",
    )
    backdated = (
        datetime.now(UTC)
        - timedelta(seconds=CLOUD_HITL_IDEMPOTENCY_WINDOW_S + 60)
    ).isoformat()
    hitl_store.conn.execute(
        "UPDATE popola_hitl SET created_at = ? WHERE hitl_id = ?",
        (backdated, first.hitl_id),
    )
    hitl_store.conn.commit()

    second = bridge.submit_request(
        task_id="window",
        cursor_agent_id="ag",
        cursor_run_id="run",
        prompt_title="t",
        prompt_body="Body",
        options=[{"id": "yes", "label": "Y"}, {"id": "no", "label": "N"}],
        idempotency_key="window-key-1234",
    )
    assert second.deduped is False
    assert second.hitl_id != first.hitl_id
    # Lark notifier called once per fresh row → 2 total (no replay short-circuit).
    assert notifier.send_hitl_card.call_count == 2


def test_lookup_excludes_terminal_rows(
    hitl_store: HITLStore,
) -> None:
    """Per ``mcp-tool-contract.md`` §5 terminal-state row: cancelled/timeout
    rows are dedup-invalidated. ``lookup_by_idempotency_key`` returns ``None``
    so a retry rebuilds — the test mirrors that contract."""
    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id="term",
        cursor_agent_id="ag",
        cursor_run_id="run",
        prompt_title="t",
        prompt_body="b",
        options=[{"id": "yes", "label": "Y"}, {"id": "no", "label": "N"}],
        idempotency_key="terminal-key",
    )
    ok = hitl_store.mark_status(req.hitl_id, "cancelled")
    assert ok is True

    found = bridge.lookup_by_idempotency_key("terminal-key")
    assert found is None


# ── AC (e) restart-then-replay still short-circuits (SECURITY R3) ──────


def test_restart_then_replay_still_short_circuits(db_path: Path) -> None:
    """SECURITY R3 + AC (e): the dedup record is SQL-only — it survives a
    daemon restart. We simulate by:

    1. Creating ``bridge_a`` on a fresh connection, submitting a row.
    2. Closing the connection (simulating the daemon crashing) and opening
       a new connection on the same file (simulating restart).
    3. Building ``bridge_b`` on the new connection and replaying.
    4. Asserting the same ``hitl_id`` is returned with ``deduped=True``.

    This proves no in-memory cache is involved.
    """
    options = [{"id": "yes", "label": "Y"}, {"id": "no", "label": "N"}]
    conn_a = sqlite3.connect(db_path, check_same_thread=False)
    conn_a.row_factory = sqlite3.Row
    bridge_a = CloudHITLBridge(HITLStore(conn_a), None)
    first = bridge_a.submit_request(
        task_id="restart",
        cursor_agent_id="ag",
        cursor_run_id="run",
        prompt_title="t",
        prompt_body="Body across restart",
        options=options,
        idempotency_key="restart-survive-key",
    )
    conn_a.close()

    conn_b = sqlite3.connect(db_path, check_same_thread=False)
    conn_b.row_factory = sqlite3.Row
    bridge_b = CloudHITLBridge(HITLStore(conn_b), None)
    second = bridge_b.submit_request(
        task_id="restart",
        cursor_agent_id="ag",
        cursor_run_id="run",
        prompt_title="t",
        prompt_body="Body across restart",
        options=options,
        idempotency_key="restart-survive-key",
    )
    conn_b.close()

    assert second.hitl_id == first.hitl_id
    assert second.deduped is True


# ── AC (b) end-to-end via daemon RPC (deduped: true wire flag) ──────────


@pytest.mark.asyncio
async def test_rpc_replay_returns_deduped_true(
    tmp_path: Path,
) -> None:
    """End-to-end through the FastAPI handler: a second
    ``POST /hitl/cloud/request`` with the same context returns
    ``deduped: true`` and the same ``hitl_id``.
    """
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    db = tmp_path / "rpc-replay.db"
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)
    store = HITLStore(conn)
    popolad = Popolad(
        events_dir=events_dir,
        adapter=lambda *args, **kwargs: ["echo"],  # type: ignore[arg-type,misc]
    )
    popolad.hitl_store = store
    popolad._event_logs["t-rpc"] = EventLog(  # type: ignore[attr-defined]
        path=events_dir / "t-rpc.jsonl"
    )

    body: dict[str, Any] = {
        "task_id": "t-rpc",
        "cursor_agent_id": "ag-rpc",
        "cursor_run_id": "run-rpc",
        "prompt_title": "Title",
        "prompt_body": "Identical question",
        "options": [
            {"id": "yes", "label": "Yes"},
            {"id": "no", "label": "No"},
        ],
    }
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/hitl/cloud/request", json=body)
        second = await client.post("/hitl/cloud/request", json=body)
    assert first.status_code == 200
    assert second.status_code == 200
    payload_a = first.json()
    payload_b = second.json()
    assert payload_a["deduped"] is False
    assert payload_b["deduped"] is True
    assert payload_a["hitl_id"] == payload_b["hitl_id"]


@pytest.mark.asyncio
async def test_rpc_replay_distinct_question_does_not_dedup(
    tmp_path: Path,
) -> None:
    """A different ``prompt_body`` produces a distinct auto-derived key, so
    no dedup — proves the lookup is keyed on the structured tuple, not on
    e.g. ``task_id`` alone."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    db = tmp_path / "rpc-distinct.db"
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)
    store = HITLStore(conn)
    popolad = Popolad(
        events_dir=events_dir,
        adapter=lambda *args, **kwargs: ["echo"],  # type: ignore[arg-type,misc]
    )
    popolad.hitl_store = store
    popolad._event_logs["t-distinct"] = EventLog(  # type: ignore[attr-defined]
        path=events_dir / "t-distinct.jsonl"
    )

    base: dict[str, Any] = {
        "task_id": "t-distinct",
        "cursor_agent_id": "ag-d",
        "cursor_run_id": "run-d",
        "prompt_title": "Title",
        "options": [
            {"id": "yes", "label": "Yes"},
            {"id": "no", "label": "No"},
        ],
    }
    body_one = {**base, "prompt_body": "Question A"}
    body_two = {**base, "prompt_body": "Question B"}
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/hitl/cloud/request", json=body_one)
        second = await client.post("/hitl/cloud/request", json=body_two)
    payload_a = first.json()
    payload_b = second.json()
    assert payload_a["deduped"] is False
    assert payload_b["deduped"] is False
    assert payload_a["hitl_id"] != payload_b["hitl_id"]
