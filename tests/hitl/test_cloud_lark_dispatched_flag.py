"""M3 — ``CloudHITLRequestResponse.lark_dispatched`` field is wired.

Per ``.local/.agent/active/v0.8.7-cloud-hitl-prod/REVIEW.md`` finding **M3**
(``cloud_hitl_tool.popolaloom_cloud_hitl_request`` reads
``request_payload.get("lark_dispatched", True)`` to flip
``timeout`` → ``lark_unreachable`` per contract §7 row 4, but the
daemon's :class:`CloudHITLRequestResponse` did not include the field —
production traffic always defaulted to ``True`` and the Lark-unreachable
code path was dead). The Stage 3 fix:

(a) adds ``lark_dispatched: bool = True`` to
    :class:`CloudHITLRequestResponse`,
(b) plumbs the bridge's ``lark_dispatched`` flag through the daemon
    response so the MCP tool sees the real Lark fan-out outcome.

The cases below verify the flag is set correctly on:

1. ``test_lark_dispatched_true_on_successful_lark_send`` — the bridge
   reports ``lark_dispatched=True`` when the notifier succeeds.
2. ``test_lark_dispatched_false_on_lark_send_failure`` — the bridge
   reports ``lark_dispatched=False`` when the notifier raises (Lark
   webhook unreachable, lark-cli missing, etc.); the audit chain
   still emits a ``cloud_hitl.failed`` row with
   ``error_kind="lark_unreachable"``.
3. ``test_default_response_has_lark_dispatched_true`` — the
   ``CloudHITLRequestResponse`` Pydantic model defaults to ``True``
   to preserve v0.8.5 wire compatibility.
"""

from __future__ import annotations

import asyncio
import sqlite3
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
import pytest

from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.rpc import CloudHITLRequestResponse, create_app
from popolaloom.daemon.server import Popolad
from popolaloom.hitl import HITLPrompt
from popolaloom.hitl.cloud_bridge import (
    CLOUD_HITL_FAILED_EVENT,
    CloudHITLBridge,
)
from popolaloom.hitl.sync import HITLStore

_MIGRATIONS = ("006_popola_hitl.sql", "007_popola_hitl_metadata.sql")


def _apply_migrations(conn: sqlite3.Connection) -> None:
    migrations_pkg = Path(resources.files("popolaloom.migrations"))
    for name in _MIGRATIONS:
        sql = (migrations_pkg / name).read_text(encoding="utf-8")
        conn.executescript(sql)
    conn.commit()


@pytest.fixture()
def hitl_store(tmp_path: Path) -> HITLStore:
    db = tmp_path / "m3.db"
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)
    return HITLStore(conn)


# ── M3 (a): default response carries lark_dispatched=True ────────────────


def test_default_response_has_lark_dispatched_true() -> None:
    """The Pydantic schema defaults ``lark_dispatched=True`` so the
    legacy v0.8.5 wire shape (no field) deserialises with the historical
    "delivery succeeded" semantics.
    """
    resp = CloudHITLRequestResponse(
        hitl_id="h-default",
        deadline_at="2026-05-08T12:00:00+00:00",
    )
    assert resp.lark_dispatched is True


# ── M3 (b): bridge → daemon response surfaces lark_dispatched correctly ─


@pytest.mark.asyncio
async def test_lark_dispatched_true_on_successful_lark_send(
    tmp_path: Path, hitl_store: HITLStore
) -> None:
    """When the bridge's Lark notifier returns successfully, the daemon's
    ``CloudHITLRequestResponse`` reports ``lark_dispatched=True``."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    popolad = Popolad(
        events_dir=events_dir,
        adapter=lambda *args, **kwargs: ["echo"],  # type: ignore[arg-type,misc]
    )
    popolad.hitl_store = hitl_store
    popolad._event_logs["t-ok"] = EventLog(  # type: ignore[attr-defined]
        path=events_dir / "t-ok.jsonl"
    )

    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)

    body = {
        "task_id": "t-ok",
        "cursor_agent_id": "ag-ok",
        "cursor_run_id": "run-ok",
        "prompt_title": "Title",
        "prompt_body": "Approve?",
        "options": [{"id": "y", "label": "Y"}, {"id": "n", "label": "N"}],
    }
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        # Patch send_lark_card to a no-op so the notifier "succeeds" —
        # the bridge keeps lark_dispatched=True per the production
        # path (this matches a healthy γ deployment where lark-cli
        # hit the Lark webhook and got 200 OK).
        from popolaloom.hitl import renderers

        original_send = renderers.lark.send_lark_card

        def _fake_send(*args: Any, **kwargs: Any) -> Any:
            from popolaloom.hitl.renderers.lark import LarkSendResult

            return LarkSendResult(ok=True, message_id="ok-msg", attempts=1)

        renderers.lark.send_lark_card = _fake_send  # type: ignore[assignment]
        try:
            resp = await client.post("/hitl/cloud/request", json=body)
        finally:
            renderers.lark.send_lark_card = original_send  # type: ignore[assignment]

    assert resp.status_code == 200
    data = resp.json()
    assert data["lark_dispatched"] is True, (
        f"M3 regression: lark_dispatched flag not surfaced or wrong value: {data}"
    )


@pytest.mark.asyncio
async def test_lark_dispatched_false_on_lark_send_failure(
    tmp_path: Path, hitl_store: HITLStore
) -> None:
    """When the notifier raises (Lark webhook unreachable, etc.), the
    bridge records the failure AND surfaces ``lark_dispatched=False`` so
    the MCP tool's poll-then-error path can flip the user-facing
    ``error.code`` from ``timeout`` to ``lark_unreachable``.
    """
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    popolad = Popolad(
        events_dir=events_dir,
        adapter=lambda *args, **kwargs: ["echo"],  # type: ignore[arg-type,misc]
    )
    popolad.hitl_store = hitl_store
    popolad._event_logs["t-fail"] = EventLog(  # type: ignore[attr-defined]
        path=events_dir / "t-fail.jsonl"
    )

    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)

    body = {
        "task_id": "t-fail",
        "cursor_agent_id": "ag-fail",
        "cursor_run_id": "run-fail",
        "prompt_title": "Title",
        "prompt_body": "Approve?",
        "options": [{"id": "y", "label": "Y"}, {"id": "n", "label": "N"}],
    }

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        from popolaloom.hitl import renderers

        original_send = renderers.lark.send_lark_card

        def _raising_send(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("simulated lark webhook unreachable")

        renderers.lark.send_lark_card = _raising_send  # type: ignore[assignment]
        try:
            resp = await client.post("/hitl/cloud/request", json=body)
        finally:
            renderers.lark.send_lark_card = original_send  # type: ignore[assignment]

    assert resp.status_code == 200
    data = resp.json()
    assert data["lark_dispatched"] is False, (
        f"M3 regression: bridge raised in send_hitl_card but the daemon "
        f"surfaced lark_dispatched=True; the MCP tool's "
        f"timeout → lark_unreachable flip is dead code. data={data}"
    )

    # The audit chain MUST still record the failure (No Silent Failures).
    # The EventLog uses a periodic fsync — explicitly flush the per-task
    # log so the test reads the freshly-written line synchronously.
    log = popolad.event_log("t-fail")
    assert log is not None
    log.fsync()
    log_path = events_dir / "t-fail.jsonl"
    log_text = log_path.read_text(encoding="utf-8")
    assert CLOUD_HITL_FAILED_EVENT in log_text
    assert "lark_unreachable" in log_text


# ── M3 cont.: bridge-level surface (deeper unit) ─────────────────────────


def test_bridge_submit_request_carries_lark_dispatched_field(
    hitl_store: HITLStore,
) -> None:
    """The bridge's :class:`CloudHITLRequest` carries ``lark_dispatched``
    as part of its return shape — proves the value is observable
    upstream of the Pydantic surface."""
    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id="t-bridge",
        cursor_agent_id="ag",
        cursor_run_id="rn",
        prompt_title="t",
        prompt_body="b?",
        options=[{"id": "y", "label": "Y"}, {"id": "n", "label": "N"}],
    )
    assert hasattr(req, "lark_dispatched"), (
        "M3 regression: CloudHITLRequest is missing the lark_dispatched "
        "attribute — the daemon cannot surface the flag without it."
    )
    # No notifier wired → no Lark fan-out attempted → flag stays True.
    assert req.lark_dispatched is True


def test_bridge_submit_request_lark_dispatched_false_on_notifier_raise(
    hitl_store: HITLStore,
) -> None:
    """When the injected notifier raises, the bridge sets
    ``lark_dispatched=False`` AND emits the
    :data:`CLOUD_HITL_FAILED_EVENT` audit row."""

    class _RaisingNotifier:
        def send_hitl_card(
            self,
            prompt: HITLPrompt,
            *,
            hitl_id: str,
            event_log: Any | None = None,
            task_id: str | None = None,
        ) -> Any:
            raise RuntimeError("simulated lark unreachable")

    class _CapturingLog:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []

        def append(self, event_type: str, data: dict[str, Any]) -> None:
            self.events.append((event_type, dict(data)))

    log = _CapturingLog()
    bridge = CloudHITLBridge(hitl_store, _RaisingNotifier())
    req = bridge.submit_request(
        task_id="t-fail",
        cursor_agent_id="ag",
        cursor_run_id="rn",
        prompt_title="t",
        prompt_body="b?",
        options=[{"id": "y", "label": "Y"}, {"id": "n", "label": "N"}],
        event_log=log,
    )
    assert req.lark_dispatched is False
    failed_events = [e for e in log.events if e[0] == CLOUD_HITL_FAILED_EVENT]
    assert len(failed_events) == 1
    assert failed_events[0][1]["error_kind"] == "lark_unreachable"


# Ensure asyncio import isn't flagged as unused (some runners gate on this).
_ = asyncio
