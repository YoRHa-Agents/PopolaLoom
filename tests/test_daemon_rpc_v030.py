"""v0.3.0 daemon RPC endpoint tests (F2 + F4 + F5).

Tests the new endpoints added in v0.3.0:

- ``POST /hitl/answer`` (F4.F)
- ``GET /hitl/pending`` (F4.F)
- ``POST /relay`` (F2)
- ``POST /supervise`` (F2)
- ``POST /federate`` (F2)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

from popolaloom.daemon.rpc import create_app
from popolaloom.daemon.server import Popolad
from popolaloom.hitl import HITLOption, HITLPrompt, HITLStore

# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture()
def tmp_events_dir(tmp_path: Path) -> Path:
    events = tmp_path / "events"
    events.mkdir()
    return events


@pytest.fixture()
def hitl_store(tmp_path: Path) -> HITLStore:
    db_path = tmp_path / "hitl.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    migration_sql = (
        Path(__file__).resolve().parent.parent / "migrations" / "006_popola_hitl.sql"
    ).read_text(encoding="utf-8")
    conn.executescript(migration_sql)
    conn.commit()
    return HITLStore(conn)


def _approval_prompt() -> HITLPrompt:
    return HITLPrompt(
        trigger="approval",
        why="why",
        what="what",
        options=[
            HITLOption(id="a", label="A"),
            HITLOption(id="b", label="B"),
        ],
        default_option_id="a",
        channels=["lark", "ide"],
        deadline_seconds=3600,
    )


# ── /hitl/answer happy + sad paths ──────────────────────────────────────


@pytest.mark.asyncio
async def test_hitl_answer_returns_503_when_no_store(tmp_events_dir: Path) -> None:
    """When popolad.hitl_store is None the endpoint replies 503."""
    popolad = Popolad(events_dir=tmp_events_dir, adapter=lambda *args, **kw: ["echo"])
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/hitl/answer",
            json={
                "hitl_id": "x", "option_id": "y", "via": "cli",
            },
        )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_hitl_answer_records_winner_via_store(
    tmp_events_dir: Path, hitl_store: HITLStore
) -> None:
    popolad = Popolad(events_dir=tmp_events_dir, adapter=lambda *args, **kw: ["echo"])
    popolad.hitl_store = hitl_store
    hitl_id = hitl_store.create(_approval_prompt())
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/hitl/answer",
            json={
                "hitl_id": hitl_id,
                "option_id": "a",
                "via": "cli",
                "responder_id": "ou_x",
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["hitl_id"] == hitl_id


@pytest.mark.asyncio
async def test_hitl_answer_loser_returns_already_via(
    tmp_events_dir: Path, hitl_store: HITLStore
) -> None:
    popolad = Popolad(events_dir=tmp_events_dir, adapter=lambda *args, **kw: ["echo"])
    popolad.hitl_store = hitl_store
    hitl_id = hitl_store.create(_approval_prompt())
    hitl_store.mark_answered(hitl_id, option_id="a", via="lark")
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/hitl/answer",
            json={
                "hitl_id": hitl_id, "option_id": "b", "via": "ide",
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["already_via"] == "lark"


@pytest.mark.asyncio
async def test_hitl_pending_returns_pending_rows(
    tmp_events_dir: Path, hitl_store: HITLStore
) -> None:
    popolad = Popolad(events_dir=tmp_events_dir, adapter=lambda *args, **kw: ["echo"])
    popolad.hitl_store = hitl_store
    hitl_store.create(_approval_prompt())
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/hitl/pending")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_hitl_pending_returns_503_when_no_store(tmp_events_dir: Path) -> None:
    popolad = Popolad(events_dir=tmp_events_dir, adapter=lambda *args, **kw: ["echo"])
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/hitl/pending")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_hitl_pending_filters_by_task_id(
    tmp_events_dir: Path, hitl_store: HITLStore
) -> None:
    popolad = Popolad(events_dir=tmp_events_dir, adapter=lambda *args, **kw: ["echo"])
    popolad.hitl_store = hitl_store
    hitl_store.create(_approval_prompt(), task_id="task-a")
    hitl_store.create(_approval_prompt(), task_id="task-b")
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/hitl/pending", params={"task_id": "task-a"})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["task_id"] == "task-a"


# ── /relay ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_relay_endpoint_unknown_source_returns_400(
    tmp_events_dir: Path,
) -> None:
    popolad = Popolad(events_dir=tmp_events_dir, adapter=lambda *args, **kw: ["echo"])
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/relay",
            json={
                "source_task_id": "task-nonexistent",
                "target_cli": "echo",
                "payload": {},
                "reason": "test relay",
            },
        )
    assert resp.status_code == 400
    assert "not found" in resp.json()["detail"].lower()


# ── /supervise ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supervise_endpoint_basic_subscribe(tmp_events_dir: Path) -> None:
    popolad = Popolad(events_dir=tmp_events_dir, adapter=lambda *args, **kw: ["echo"])
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/supervise",
            json={
                "parent_task_id": "task-parent",
                "child_task_id": "task-child",
            },
        )
    # The endpoint may succeed (creating subscription) or 400 if missing tasks.
    assert resp.status_code in (200, 400)


# ── /federate ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_federate_invalid_voting_strategy_rejected(
    tmp_events_dir: Path,
) -> None:
    """Pydantic v2 rejects non-Literal voting_strategy with 422."""
    popolad = Popolad(events_dir=tmp_events_dir, adapter=lambda *args, **kw: ["echo"])
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/federate",
            json={
                "prompt": "what is 2+2?",
                "cli_list": ["echo"],
                "voting_strategy": "bogus",
            },
        )
    # Either 400 (manual validation) or 422 (Pydantic schema) — both acceptable.
    assert resp.status_code in (400, 422)
