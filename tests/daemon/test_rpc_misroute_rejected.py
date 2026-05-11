"""C1 wiring regression — production callers wire ``expected_cursor_*``.

Per ``.local/.agent/active/v0.8.7-cloud-hitl-prod/REVIEW.md`` finding **C1**
(the ``expected_cursor_agent_id`` / ``expected_cursor_run_id`` mis-route
defense added to :meth:`CloudHITLBridge.submit_answer` was dead code in
production: ``daemon/rpc.py::hitl_cloud_answer`` did not accept the
fields, and ``daemon/main.py::on_card_action`` bypassed the bridge
entirely by calling ``store.fold_reply``). The Stage 3 fix:

(a) extends :class:`CloudHITLAnswerBody` with optional
    ``cursor_agent_id`` / ``cursor_run_id`` fields,
(b) threads them through :meth:`CloudHITLBridge.submit_answer` as
    ``expected_cursor_*=`` kwargs,
(c) translates a mis-route descriptor → HTTP 400, and
(d) replaces ``store.fold_reply`` in the Lark listener path with a
    bridge ``submit_answer`` call that derives the expected tuple from
    the row's metadata JSON.

The cases below cover both production callers (the HTTP path through
``daemon/rpc.py`` directly and the Lark listener path through
``daemon/main.py::on_card_action``).
"""

from __future__ import annotations

from importlib import resources
from importlib import resources
import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.main import _build_lark_callbacks
from popolaloom.daemon.rpc import create_app
from popolaloom.daemon.server import Popolad
from popolaloom.hitl import HITLStore
from popolaloom.hitl.cloud_bridge import CloudHITLBridge

_MIGRATIONS = ("006_popola_hitl.sql", "007_popola_hitl_metadata.sql")


def _apply_migrations(conn: sqlite3.Connection) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    for name in _MIGRATIONS:
        sql = (Path(resources.files("popolaloom.migrations")) / name).read_text(encoding="utf-8")
        conn.executescript(sql)
    conn.commit()


@pytest.fixture()
def tmp_events_dir(tmp_path: Path) -> Path:
    root = tmp_path / "events"
    root.mkdir()
    return root


@pytest.fixture()
def hitl_store(tmp_path: Path) -> HITLStore:
    db = tmp_path / "misroute.db"
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)
    return HITLStore(conn)


def _popolad_with_log(
    events_dir: Path, store: HITLStore, task_id: str
) -> Popolad:
    popolad = Popolad(
        events_dir=events_dir,
        adapter=lambda *args, **kwargs: ["echo"],  # type: ignore[arg-type,misc]
    )
    popolad.hitl_store = store
    popolad._event_logs[task_id] = EventLog(  # type: ignore[attr-defined]
        path=events_dir / f"{task_id}.jsonl"
    )
    return popolad


# ── C1 (a) + (c): HTTP /hitl/cloud/answer rejects mis-route ──────────────


@pytest.mark.asyncio
async def test_http_answer_with_mismatched_cursor_run_id_returns_400(
    tmp_events_dir: Path, hitl_store: HITLStore
) -> None:
    """A HTTP caller posting ``cursor_run_id="OTHER"`` for a row owned by a
    different run id MUST receive ``HTTP 400`` with a ``mis-route``
    descriptor; the row stays ``pending`` (no answer columns written)."""
    task_id = "misroute-http-run"
    popolad = _popolad_with_log(tmp_events_dir, hitl_store, task_id)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)

    body = {
        "task_id": task_id,
        "cursor_agent_id": "legit-agent",
        "cursor_run_id": "legit-run",
        "prompt_title": "Title",
        "prompt_body": "Approve?",
        "options": [{"id": "y", "label": "Y"}, {"id": "n", "label": "N"}],
    }
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        create_resp = await client.post("/hitl/cloud/request", json=body)
        assert create_resp.status_code == 200
        hitl_id = create_resp.json()["hitl_id"]

        # Forged answer carrying a wrong cursor_run_id.
        answer_resp = await client.post(
            f"/hitl/cloud/answer/{hitl_id}",
            json={
                "option_id": "y",
                "responder_id": "forged-clicker",
                "channel": "lark",
                "cursor_agent_id": "legit-agent",
                "cursor_run_id": "OTHER-run",
            },
        )

    assert answer_resp.status_code == 400, (
        f"expected HTTP 400 mis-route reject; got {answer_resp.status_code} "
        f"{answer_resp.text!r}"
    )
    detail = answer_resp.json()["detail"]
    assert detail.startswith("mis-route:"), f"got detail={detail!r}"
    assert "OTHER-run" in detail

    row = hitl_store.get(hitl_id)
    assert row is not None
    assert row["status"] == "pending"
    assert row["answered_at"] is None


@pytest.mark.asyncio
async def test_http_answer_with_matching_cursor_tuple_succeeds(
    tmp_events_dir: Path, hitl_store: HITLStore
) -> None:
    """Sanity: a caller threading the correct ``cursor_*`` tuple still
    answers normally (HTTP 200, row marked answered)."""
    task_id = "misroute-http-ok"
    popolad = _popolad_with_log(tmp_events_dir, hitl_store, task_id)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)

    body = {
        "task_id": task_id,
        "cursor_agent_id": "legit-agent",
        "cursor_run_id": "legit-run",
        "prompt_title": "Title",
        "prompt_body": "Approve?",
        "options": [{"id": "y", "label": "Y"}, {"id": "n", "label": "N"}],
    }
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        create_resp = await client.post("/hitl/cloud/request", json=body)
        hitl_id = create_resp.json()["hitl_id"]

        answer_resp = await client.post(
            f"/hitl/cloud/answer/{hitl_id}",
            json={
                "option_id": "y",
                "responder_id": "legit-clicker",
                "channel": "lark",
                "cursor_agent_id": "legit-agent",
                "cursor_run_id": "legit-run",
            },
        )

    assert answer_resp.status_code == 200, (
        f"expected HTTP 200; got {answer_resp.status_code} {answer_resp.text!r}"
    )
    row = hitl_store.get(hitl_id)
    assert row is not None
    assert row["status"] == "answered"


@pytest.mark.asyncio
async def test_http_answer_without_cursor_kwargs_keeps_legacy_behavior(
    tmp_events_dir: Path, hitl_store: HITLStore
) -> None:
    """Backward-compat: callers that DON'T pass ``cursor_*`` (legacy v0.8.5
    HTTP clients) continue to work — the bridge skips the mis-route
    check when both kwargs are None and answers normally."""
    task_id = "misroute-http-legacy"
    popolad = _popolad_with_log(tmp_events_dir, hitl_store, task_id)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)

    body = {
        "task_id": task_id,
        "cursor_agent_id": "legit-agent",
        "cursor_run_id": "legit-run",
        "prompt_title": "Title",
        "prompt_body": "Approve?",
        "options": [{"id": "y", "label": "Y"}, {"id": "n", "label": "N"}],
    }
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        create_resp = await client.post("/hitl/cloud/request", json=body)
        hitl_id = create_resp.json()["hitl_id"]

        answer_resp = await client.post(
            f"/hitl/cloud/answer/{hitl_id}",
            json={
                "option_id": "y",
                "responder_id": "legacy-clicker",
                "channel": "cloud",
            },
        )
    assert answer_resp.status_code == 200
    row = hitl_store.get(hitl_id)
    assert row is not None
    assert row["status"] == "answered"


# ── C1 (d): on_card_action routes through bridge.submit_answer ──────────


@pytest.mark.asyncio
async def test_on_card_action_uses_bridge_with_expected_cursor_kwargs(
    tmp_events_dir: Path, hitl_store: HITLStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Lark listener's ``on_card_action`` MUST call
    :meth:`CloudHITLBridge.submit_answer` (not ``store.fold_reply``) and
    MUST thread the row's stored cursor tuple as
    ``expected_cursor_*`` kwargs.

    We monkey-patch ``bridge_for_daemon`` BEFORE building the callbacks so
    the listener captures our spy bridge in its closure. The spy records
    the kwargs passed to ``submit_answer`` for assertion.
    """
    task_id = "listener-kwargs"
    popolad = _popolad_with_log(tmp_events_dir, hitl_store, task_id)

    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id=task_id,
        cursor_agent_id="legit-agent",
        cursor_run_id="legit-run",
        prompt_title="t",
        prompt_body="Approve?",
        options=[
            {"id": "approve", "label": "Approve"},
            {"id": "reject", "label": "Reject"},
        ],
    )
    hitl_id = req.hitl_id

    captured_kwargs: list[dict[str, Any]] = []

    class _SpyBridge:
        """Recording wrapper around the real bridge — captures the
        ``submit_answer`` kwargs so the test can inspect what the
        listener fed into the mis-route defense."""

        def __init__(self, inner: CloudHITLBridge) -> None:
            self._inner = inner

        @property
        def store(self) -> HITLStore:
            return self._inner.store

        def get_request(self, hitl_id_inner: str) -> Any:
            return self._inner.get_request(hitl_id_inner)

        def submit_answer(self, *args: Any, **kwargs: Any) -> Any:
            captured_kwargs.append(dict(kwargs))
            return self._inner.submit_answer(*args, **kwargs)

    def _fake_factory(*args: Any, **kwargs: Any) -> Any:
        # Construct a fresh real bridge (no Lark fan-out) and wrap with spy.
        real = CloudHITLBridge(
            hitl_store, None, default_timeout_s=600.0
        )
        return _SpyBridge(real)

    # Patch BEFORE _build_lark_callbacks's local-scope import binds the
    # name in its enclosing scope.
    monkeypatch.setattr(
        "popolaloom.hitl.cloud_bridge.bridge_for_daemon",
        _fake_factory,
    )

    callbacks = _build_lark_callbacks(popolad)
    fake_event: dict[str, Any] = {
        "header": {"event_type": "card.action.trigger_v1"},
        "event": {
            "operator": {"open_id": "ou_legit_responder"},
            "action": {
                "value": {"hitl_id": hitl_id, "option_id": "approve"}
            },
        },
    }
    parsed = (hitl_id, "approve")

    await callbacks.on_card_action(fake_event, parsed)
    await asyncio.sleep(0)

    assert len(captured_kwargs) == 1, (
        f"C1 regression: bridge.submit_answer not called from listener; "
        f"captured_kwargs={captured_kwargs}. The listener may still be "
        f"using store.fold_reply — REVIEW.md C1 (b)."
    )
    kw = captured_kwargs[0]
    assert kw.get("channel") == "lark"
    assert kw.get("responder_id") == "ou_legit_responder"
    assert kw.get("expected_cursor_agent_id") == "legit-agent", (
        f"C1 regression: listener did NOT thread the row's "
        f"cursor_agent_id into submit_answer; kw={kw}"
    )
    assert kw.get("expected_cursor_run_id") == "legit-run", (
        f"C1 regression: listener did NOT thread the row's "
        f"cursor_run_id into submit_answer; kw={kw}"
    )

    row = hitl_store.get(hitl_id)
    assert row is not None
    assert row["status"] == "answered"


@pytest.mark.asyncio
async def test_on_card_action_legitimate_click_answers_normally(
    tmp_events_dir: Path, hitl_store: HITLStore
) -> None:
    """Sanity: a Lark card click whose operator + hitl_id match the
    row's stored cursor tuple proceeds through the bridge to
    ``mark_answered``. Catches regressions where the new wiring
    accidentally rejects every click."""
    task_id = "listener-ok"
    popolad = _popolad_with_log(tmp_events_dir, hitl_store, task_id)
    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id=task_id,
        cursor_agent_id="ag-ok",
        cursor_run_id="run-ok",
        prompt_title="t",
        prompt_body="Approve?",
        options=[
            {"id": "approve", "label": "Approve"},
            {"id": "reject", "label": "Reject"},
        ],
    )
    hitl_id = req.hitl_id

    callbacks = _build_lark_callbacks(popolad)
    fake_event: dict[str, Any] = {
        "header": {"event_type": "card.action.trigger_v1"},
        "event": {
            "operator": {"open_id": "ou_legit_responder"},
            "action": {
                "value": {"hitl_id": hitl_id, "option_id": "approve"}
            },
        },
    }
    parsed = (hitl_id, "approve")

    await callbacks.on_card_action(fake_event, parsed)
    await asyncio.sleep(0)

    row = hitl_store.get(hitl_id)
    assert row is not None
    assert row["status"] == "answered"
    assert row["answered_via"] == "lark"
    assert row["answer_responder_id"] == "ou_legit_responder"
