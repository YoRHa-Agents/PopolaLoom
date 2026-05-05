"""v0.4.1 Stage L2.C — :func:`_build_default_popolad` Lark wiring tests.

Per the L2 task spec (~ 1 case): with env vars set + mocked
:func:`is_lark_runtime_available` = True, verify
:func:`_build_default_popolad` constructs and stores a
:class:`LarkSupervisor` on ``popolad._lark_supervisor``. With env
vars unset, verify it skips with the explicit INFO log line
``lark.supervisor.skipped reason=...`` (No Silent Failures).

The test covers BOTH branches in one parameterised test so the L2.C
acceptance criterion is fully exercised.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import popolaloom.daemon.main as daemon_main
from popolaloom.lark.supervisor import LarkSupervisor


@pytest.mark.asyncio
async def test_build_default_popolad_wires_lark_supervisor_when_env_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two branches in one test: ON wiring vs OFF skip (per L2.C contract).

    Branch A — env+cli OK: ``popolad._lark_supervisor`` is a
    :class:`LarkSupervisor`; supervisor is started as a background
    task on the running loop; ``popolad._loop`` is set to the running
    loop via :meth:`Popolad.attach_loop`.

    Branch B — env unset: ``popolad._lark_supervisor`` stays ``None``;
    a single INFO log line ``lark.supervisor.skipped
    reason=lark_target_open_id_unset`` is emitted (No Silent Failures).
    """
    # ── Branch A: env present + lark-cli available → supervisor wired ──
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_wiring_branch_a")

    started_marker: dict[str, bool] = {"started": False}

    async def fake_start(self: LarkSupervisor) -> None:
        # Don't actually spawn lark-cli — record the start was attempted.
        started_marker["started"] = True

    with patch("popolaloom.lark.is_lark_runtime_available", return_value=True), \
         patch.object(LarkSupervisor, "start", fake_start):
        popolad_a = daemon_main._build_default_popolad(tmp_path / "a")
        # The supervisor.start coroutine was scheduled — let it run
        for _ in range(20):
            if started_marker["started"]:
                break
            await asyncio.sleep(0.01)

    assert popolad_a._lark_supervisor is not None, (
        "L2.C: env+cli ok must produce a non-None _lark_supervisor"
    )
    assert isinstance(popolad_a._lark_supervisor, LarkSupervisor)
    assert started_marker["started"] is True, (
        "supervisor.start coroutine must be scheduled on the running loop"
    )
    assert popolad_a._loop is not None, (
        "Popolad._loop must be wired to the running asyncio loop"
    )

    # ── Branch B: env unset → skip with explicit INFO log ──
    monkeypatch.delenv("LARK_HITL_TARGET_OPEN_ID", raising=False)
    monkeypatch.delenv("LARK_NOTIFY_TARGET_OPEN_ID", raising=False)
    caplog.clear()

    with patch("popolaloom.lark.is_lark_runtime_available", return_value=True), \
         caplog.at_level(logging.INFO, logger="popolaloom.daemon"):
        popolad_b = daemon_main._build_default_popolad(tmp_path / "b")

    assert popolad_b._lark_supervisor is None, (
        "L2.C: env unset must keep _lark_supervisor as None"
    )
    skip_logs = [
        rec.getMessage() for rec in caplog.records
        if "lark.supervisor.skipped" in rec.getMessage()
    ]
    assert any(
        "lark_target_open_id_unset" in msg for msg in skip_logs
    ), (
        f"expected explicit skip-reason INFO log; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )

    # ── Branch C: lark-cli unavailable → distinct skip reason ──
    caplog.clear()
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_anything")
    with patch("popolaloom.lark.is_lark_runtime_available", return_value=False), \
         caplog.at_level(logging.INFO, logger="popolaloom.daemon"):
        popolad_c = daemon_main._build_default_popolad(tmp_path / "c")
    assert popolad_c._lark_supervisor is None
    assert any(
        "lark_cli_unavailable" in rec.getMessage() for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_safe_supervisor_start_swallows_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception from ``supervisor.start`` is caught + logged (No Silent Failures).

    Daemon must keep serving even when the Lark listener cannot start
    (lark-cli vanishes between PATH check and exec, network down at
    websocket subscribe, etc.). Verifies the L2.C exception swallow.
    """

    class _BoomSupervisor:
        async def start(self) -> None:
            raise RuntimeError("synthetic supervisor boom")

    with caplog.at_level(logging.ERROR, logger="popolaloom.daemon"):
        await daemon_main._safe_supervisor_start(_BoomSupervisor())

    assert any(
        "lark.supervisor.start_failed" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_supervisor_event_logger_serializes_event_dict(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The supervisor on_event callback flattens the event dict into one INFO line."""
    cb = daemon_main._make_supervisor_event_logger()
    with caplog.at_level(logging.INFO, logger="popolaloom.daemon"):
        await cb({"event": "listener.died", "at": "2026-05-05T00:00:00Z"})
    msgs = [rec.getMessage() for rec in caplog.records]
    assert any("lark.supervisor.event" in m for m in msgs)
    assert any("listener.died" in m for m in msgs)


@pytest.mark.asyncio
async def test_lark_callbacks_drop_when_hitl_store_unwired(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When ``popolad.hitl_store is None`` the callbacks log + drop (no exception)."""
    from popolaloom.daemon.server import Popolad

    popolad = Popolad(events_dir=tmp_path)
    popolad.hitl_store = None

    cbs = daemon_main._build_lark_callbacks(popolad)
    with caplog.at_level(logging.DEBUG, logger="popolaloom.daemon"):
        await cbs.on_card_action({"event": {}}, ("h-1", "approve"))
        await cbs.on_text_feedback({"event": {}}, {"hitl_id": "h-2", "option_id": "approve"})
        await cbs.on_unauthorized({"header": {"event_id": "evt-99"}}, "ou_attacker")

    msgs = [rec.getMessage() for rec in caplog.records]
    assert any("lark.listener.card_action" in m and "unwired" in m for m in msgs)
    assert any("lark.listener.text_feedback" in m and "unwired" in m for m in msgs)
    assert any("lark.listener.unauthorized" in m for m in msgs)


@pytest.mark.asyncio
async def test_lark_callbacks_route_card_action_to_fold_reply(
    tmp_path: Path,
) -> None:
    """When ``hitl_store`` is wired, card_action invokes :meth:`HITLStore.fold_reply`."""
    from popolaloom.daemon.server import Popolad

    popolad = Popolad(events_dir=tmp_path)

    fold_calls: list[Any] = []

    class _StubStore:
        def fold_reply(self, reply: Any) -> Any:
            fold_calls.append(reply)
            return None

    popolad.hitl_store = _StubStore()

    cbs = daemon_main._build_lark_callbacks(popolad)
    sender_event: dict[str, Any] = {
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_responder_test"},
            },
        },
    }
    await cbs.on_card_action(sender_event, ("hitl-route-1", "yes"))

    assert len(fold_calls) == 1
    reply = fold_calls[0]
    assert reply.hitl_id == "hitl-route-1"
    assert reply.option_id == "yes"
    assert reply.via == "lark"
    assert reply.responder == "ou_responder_test"


@pytest.mark.asyncio
async def test_lark_callbacks_route_text_feedback_to_fold_reply(
    tmp_path: Path,
) -> None:
    """When ``hitl_store`` is wired, text_feedback invokes ``fold_reply`` with reason."""
    from popolaloom.daemon.server import Popolad

    popolad = Popolad(events_dir=tmp_path)

    fold_calls: list[Any] = []

    class _StubStore:
        def fold_reply(self, reply: Any) -> Any:
            fold_calls.append(reply)
            return None

    popolad.hitl_store = _StubStore()

    cbs = daemon_main._build_lark_callbacks(popolad)
    sender_event = {
        "event": {
            "operator": {"open_id": "ou_text_responder"},
        },
    }
    await cbs.on_text_feedback(
        sender_event,
        {"hitl_id": "hitl-text-1", "option_id": "no", "reason": "broken"},
    )

    assert len(fold_calls) == 1
    reply = fold_calls[0]
    assert reply.hitl_id == "hitl-text-1"
    assert reply.option_id == "no"
    assert reply.reason == "broken"
    assert reply.responder == "ou_text_responder"


@pytest.mark.asyncio
async def test_lark_callbacks_swallow_fold_reply_exception(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``fold_reply`` raising must NOT crash the listener event loop."""
    from popolaloom.daemon.server import Popolad

    popolad = Popolad(events_dir=tmp_path)

    class _BoomStore:
        def fold_reply(self, _reply: Any) -> Any:
            raise RuntimeError("synthetic store boom")

    popolad.hitl_store = _BoomStore()

    cbs = daemon_main._build_lark_callbacks(popolad)
    with caplog.at_level(logging.ERROR, logger="popolaloom.daemon"):
        await cbs.on_card_action({"event": {}}, ("h", "yes"))
        await cbs.on_text_feedback({"event": {}}, {"hitl_id": "h", "option_id": "no"})

    msgs = [rec.getMessage() for rec in caplog.records]
    assert any("fold_reply raised" in m and "card_action" in m for m in msgs)
    assert any("fold_reply raised" in m and "text_feedback" in m for m in msgs)


def test_extract_sender_open_id_handles_all_shapes() -> None:
    """Mini-coverage for :func:`_extract_sender_open_id` defensive branches."""
    fn = daemon_main._extract_sender_open_id
    # sender.sender_id.open_id
    assert fn({"event": {"sender": {"sender_id": {"open_id": "ou_a"}}}}) == "ou_a"
    # sender.open_id directly
    assert fn({"event": {"sender": {"open_id": "ou_b"}}}) == "ou_b"
    # operator.open_id fallback
    assert fn({"event": {"operator": {"open_id": "ou_c"}}}) == "ou_c"
    # No event wrapper → treat top-level as inner
    assert fn({"sender": {"open_id": "ou_d"}}) == "ou_d"
    # Nothing recognisable
    assert fn({"event": {"sender": "junk"}}) is None
    # Outer not a dict
    assert fn({"event": "not-a-dict"}) is None
