"""FastAPI-level tests for v0.8.5 cloud HITL RPC routes."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest

from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.rpc import create_app
from popolaloom.daemon.server import Popolad
from popolaloom.hitl import HITLStore


@pytest.fixture()
def tmp_events_dir(tmp_path: Path) -> Path:
    root = tmp_path / "events"
    root.mkdir()
    return root


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


BODY_TEMPLATE = {
    "prompt_title": "Approve?",
    "prompt_body": "Continue with deploy.",
    "cursor_agent_id": "test-agent",
    "cursor_run_id": "test-run",
    "options": [
        {"id": "yes", "label": "Yes"},
        {"id": "no", "label": "No"},
    ],
}
"""v0.8.7 T2.1.3 made ``cursor_agent_id`` / ``cursor_run_id`` required on the
request side — the daemon RPC ``/hitl/cloud/request`` rejects with
``invalid_context`` (HTTP 400) when either is missing. The fixture now passes
synthetic values so the existing v0.8.5 round-trip / event / race tests
continue to exercise the post-validation happy path."""


def _popolad_with_log(
    tmp_events_dir: Path, hitl_store: HITLStore, task_id: str
) -> Popolad:
    """Wire event log manually (tests bypass normal dispatch_registration)."""
    popolad = Popolad(
        events_dir=tmp_events_dir,
        adapter=lambda *args, **kwargs: ["echo"],  # type: ignore[arg-type,misc]
    )
    popolad.hitl_store = hitl_store
    log = EventLog(path=tmp_events_dir / f"{task_id}.jsonl")
    popolad._event_logs[task_id] = log  # type: ignore[attr-defined]
    return popolad


@pytest.mark.asyncio
async def test_post_hitl_cloud_request_returns_hitl_id(
    tmp_events_dir: Path, hitl_store: HITLStore
) -> None:
    task_id = "cloud-hitl-task"
    popolad = _popolad_with_log(tmp_events_dir, hitl_store, task_id)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    payload = dict(BODY_TEMPLATE)
    payload["task_id"] = task_id
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/hitl/cloud/request", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["hitl_id"]
    assert data["status"] == "pending"
    assert data["deadline_at"]


@pytest.mark.asyncio
async def test_post_hitl_cloud_request_validates_body(
    tmp_events_dir: Path, hitl_store: HITLStore
) -> None:
    task_id = "validation-task"
    popolad = _popolad_with_log(tmp_events_dir, hitl_store, task_id)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        bad = dict(BODY_TEMPLATE)
        bad["task_id"] = task_id
        del bad["prompt_title"]
        resp = await client.post("/hitl/cloud/request", json=bad)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_hitl_cloud_wait_pending_returns_202(
    tmp_events_dir: Path, hitl_store: HITLStore
) -> None:
    task_id = "poll-task"
    popolad = _popolad_with_log(tmp_events_dir, hitl_store, task_id)
    app = create_app(popolad=popolad)
    req_body = dict(BODY_TEMPLATE)
    req_body["task_id"] = task_id
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/hitl/cloud/request", json=req_body)
        hid = created.json()["hitl_id"]
        resp = await client.get(
            f"/hitl/cloud/wait/{hid}", params={"timeout_s": 0.35}
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "pending"


@pytest.mark.asyncio
async def test_get_hitl_cloud_wait_returns_answer_when_marked(
    tmp_events_dir: Path, hitl_store: HITLStore
) -> None:
    task_id = "answer-wait-task"
    popolad = _popolad_with_log(tmp_events_dir, hitl_store, task_id)
    app = create_app(popolad=popolad)
    req_body = dict(BODY_TEMPLATE)
    req_body["task_id"] = task_id
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/hitl/cloud/request", json=req_body)
        hid = created.json()["hitl_id"]
        ans = await client.post(
            f"/hitl/cloud/answer/{hid}",
            json={"option_id": "yes", "responder_id": "bob"},
        )
        assert ans.status_code == 200
        resp = await client.get(
            f"/hitl/cloud/wait/{hid}", params={"timeout_s": 2}
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "answered"
    assert data["answer"]["option_id"] == "yes"
    assert data["answer"]["responder_id"] == "bob"
    assert data["answer"]["channel"] == "cloud"


@pytest.mark.asyncio
async def test_post_hitl_cloud_answer_first_wins(
    tmp_events_dir: Path, hitl_store: HITLStore
) -> None:
    task_id = "race-http"
    popolad = _popolad_with_log(tmp_events_dir, hitl_store, task_id)
    app = create_app(popolad=popolad)
    req_body = dict(BODY_TEMPLATE)
    req_body["task_id"] = task_id
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/hitl/cloud/request", json=req_body)
        hid = created.json()["hitl_id"]

        async def post_answer(u: str) -> httpx.Response:
            return await client.post(
                f"/hitl/cloud/answer/{hid}",
                json={"option_id": "yes", "responder_id": u},
            )

        results = await asyncio.gather(post_answer("alice"), post_answer("carol"))

    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409]
    oks = [r.json().get("ok") for r in results]
    assert sum(1 for x in oks if x is True) == 1


@pytest.mark.asyncio
async def test_post_hitl_cloud_answer_unknown_hitl_id(
    tmp_events_dir: Path, hitl_store: HITLStore
) -> None:
    popolad = Popolad(
        events_dir=tmp_events_dir,
        adapter=lambda *args, **kwargs: ["echo"],  # type: ignore[arg-type,misc]
    )
    popolad.hitl_store = hitl_store
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/hitl/cloud/answer/does-not-exist-ffffffff",
            json={"option_id": "yes", "responder_id": "x"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_emits_hitl_cloud_requested_event(
    tmp_events_dir: Path, hitl_store: HITLStore
) -> None:
    task_id = "evt-req"
    popolad = _popolad_with_log(tmp_events_dir, hitl_store, task_id)
    log = popolad._event_logs[task_id]  # type: ignore[attr-defined]
    app = create_app(popolad=popolad)
    body = dict(BODY_TEMPLATE)
    body["task_id"] = task_id
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/hitl/cloud/request", json=body)
        assert created.status_code == 200
        hid = created.json()["hitl_id"]

    entries = log.tail(since_index=0)
    types = {e["type"] for e in entries}
    assert "hitl.cloud_requested" in types
    match = next(e for e in entries if e["type"] == "hitl.cloud_requested")
    assert match["data"]["hitl_id"] == hid


@pytest.mark.asyncio
async def test_emits_hitl_cloud_answered_event(
    tmp_events_dir: Path, hitl_store: HITLStore
) -> None:
    task_id = "evt-ans"
    popolad = _popolad_with_log(tmp_events_dir, hitl_store, task_id)
    log = popolad._event_logs[task_id]  # type: ignore[attr-defined]
    app = create_app(popolad=popolad)
    body = dict(BODY_TEMPLATE)
    body["task_id"] = task_id
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/hitl/cloud/request", json=body)
        hid = created.json()["hitl_id"]
        ans = await client.post(
            f"/hitl/cloud/answer/{hid}",
            json={"option_id": "no", "responder_id": "dana"},
        )
        assert ans.status_code == 200

    entries = log.tail(since_index=0)
    answered = [e for e in entries if e["type"] == "hitl.cloud_answered"]
    assert len(answered) == 1
    assert answered[0]["data"]["hitl_id"] == hid
    assert answered[0]["data"]["option_id"] == "no"


@pytest.mark.asyncio
async def test_round_trip_request_then_answer_then_wait(
    tmp_events_dir: Path, hitl_store: HITLStore
) -> None:
    task_id = "e2e-cloud-hitl"
    popolad = _popolad_with_log(tmp_events_dir, hitl_store, task_id)
    app = create_app(popolad=popolad)
    body = dict(BODY_TEMPLATE)
    body["task_id"] = task_id
    body["cursor_agent_id"] = "agent-xyz"
    body["cursor_run_id"] = "run-abc"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        c1 = await client.post("/hitl/cloud/request", json=body)
        assert c1.status_code == 200
        hid = c1.json()["hitl_id"]
        w1 = await client.get(f"/hitl/cloud/wait/{hid}", params={"timeout_s": 0.2})
        assert w1.status_code == 202
        c2 = await client.post(
            f"/hitl/cloud/answer/{hid}",
            json={"option_id": "yes", "responder_id": "eve", "reason": "ok"},
        )
        assert c2.status_code == 200
        assert c2.json()["ok"] is True
        w2 = await client.get(f"/hitl/cloud/wait/{hid}", params={"timeout_s": 2})
    assert w2.status_code == 200
    assert w2.json()["status"] == "answered"
    assert w2.json()["answer"]["reason"] == "ok"
