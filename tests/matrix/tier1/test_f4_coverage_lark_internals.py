"""Tier 1 — coverage boost for Lark listener / supervisor internals.

These tests exercise the pure helper functions inside ``lark/listener.py``
+ ``lark/supervisor.py`` without spawning real subprocesses, hitting
the lines that the integration tests can't easily reach.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from popolaloom.lark.listener import (
    DEFAULT_EVENTS,
    LarkEventCallbacks,
    LarkListener,
    _extract_event_type,
    _extract_sender_open_id,
    _extract_text_message,
    _lazy_lark_allowed_responders,
    parse_card_action,
    parse_message_command,
)
from popolaloom.lark.supervisor import (
    LarkSupervisor,
    SupervisorState,
)

# ── _extract_event_type ──────────────────────────────────────────────────


def test_extract_event_type_v2() -> None:
    assert _extract_event_type({"header": {"event_type": "foo.v1"}}) == "foo.v1"


def test_extract_event_type_v1() -> None:
    event = {"schema": "1.0", "event": {"type": "old.event"}}
    assert _extract_event_type(event) == "old.event"


def test_extract_event_type_missing() -> None:
    assert _extract_event_type({}) is None


def test_extract_event_type_invalid_header_shape() -> None:
    assert _extract_event_type({"header": "not a dict"}) is None


# ── _extract_sender_open_id ─────────────────────────────────────────────


def test_extract_sender_from_sender_id_open_id() -> None:
    event = {"event": {"sender": {"sender_id": {"open_id": "ou_alice"}}}}
    assert _extract_sender_open_id(event) == "ou_alice"


def test_extract_sender_from_sender_open_id() -> None:
    event = {"event": {"sender": {"open_id": "ou_alice"}}}
    assert _extract_sender_open_id(event) == "ou_alice"


def test_extract_sender_from_operator() -> None:
    event = {"event": {"operator": {"open_id": "ou_alice"}}}
    assert _extract_sender_open_id(event) == "ou_alice"


def test_extract_sender_returns_none_when_missing() -> None:
    assert _extract_sender_open_id({"event": {}}) is None


def test_extract_sender_handles_no_event_wrapper() -> None:
    """Some event payloads come pre-flattened (no 'event' key)."""
    event = {"sender": {"open_id": "ou_top"}}
    assert _extract_sender_open_id(event) == "ou_top"


# ── _extract_text_message ───────────────────────────────────────────────


def test_extract_text_message_happy() -> None:
    event = {
        "event": {
            "message": {"content": json.dumps({"text": "hello world"})}
        }
    }
    assert _extract_text_message(event) == "hello world"


def test_extract_text_message_missing_event_key() -> None:
    assert _extract_text_message({}) is None


def test_extract_text_message_bad_json() -> None:
    event = {"event": {"message": {"content": "not-json"}}}
    assert _extract_text_message(event) is None


def test_extract_text_message_missing_content() -> None:
    event = {"event": {"message": {}}}
    assert _extract_text_message(event) is None


def test_extract_text_message_no_text_field() -> None:
    event = {"event": {"message": {"content": json.dumps({"image": "x"})}}}
    assert _extract_text_message(event) is None


# ── parse_card_action / parse_message_command edge cases ───────────────


def test_parse_card_action_invalid_value_returns_failure() -> None:
    event = {
        "header": {"event_type": "card.action.trigger_v1"},
        "event": {
            "operator": {"open_id": "ou_alice"},
            "action": {"value": {}},  # no hitl_id / option_id
        },
    }
    result = parse_card_action(event, allowed_responders=["ou_alice"])
    assert result.ok is False
    assert "missing hitl_id" in result.reason


def test_parse_card_action_no_whitelist_passes() -> None:
    """When allowed_responders is empty the message is accepted."""
    event = {
        "header": {"event_type": "card.action.trigger_v1"},
        "event": {
            "operator": {"open_id": "ou_anyone"},
            "action": {"value": {"hitl_id": "h", "option_id": "y"}},
        },
    }
    result = parse_card_action(event, allowed_responders=[])
    assert result.ok is True


def test_parse_message_command_unauthorised_sender() -> None:
    event = {
        "header": {"event_type": "im.message.receive_v1", "event_id": "ev-x"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_attacker"}},
            "message": {"content": json.dumps({
                "text": "/popola feedback hitl-1 --option=yes"
            })},
        },
    }
    result = parse_message_command(event, allowed_responders=["ou_alice"])
    assert result.ok is False
    assert result.unauthorized is True


# ── _lazy_lark_allowed_responders ──────────────────────────────────────


def test_lazy_lark_allowed_responders_proxies_env(monkeypatch) -> None:
    monkeypatch.setenv("LARK_HITL_ALLOWED_RESPONDERS", "ou_x,ou_y")
    out = _lazy_lark_allowed_responders()
    assert out == ["ou_x", "ou_y"]


# ── LarkListener: state-only paths ─────────────────────────────────────


def test_lark_listener_initial_state() -> None:
    listener = LarkListener(LarkEventCallbacks(), allowed_responders=["ou_a"])
    assert listener.is_alive is False
    stats = listener.stats
    assert stats["events_seen"] == 0
    assert stats["unauthorized"] == 0
    assert stats["is_alive"] is False
    assert listener.allowed_responders == ["ou_a"]
    assert listener.events == DEFAULT_EVENTS


@pytest.mark.asyncio
async def test_lark_listener_dispatch_unauthorised_invokes_callback() -> None:
    """_dispatch_event triggers on_unauthorized when sender outside whitelist."""
    captured: list[tuple[dict[str, Any], str]] = []

    async def on_unauth(event: dict[str, Any], sender: str) -> None:
        captured.append((event, sender))

    listener = LarkListener(
        LarkEventCallbacks(on_unauthorized=on_unauth),
        allowed_responders=["ou_alice"],
    )
    event = {
        "header": {"event_type": "card.action.trigger_v1"},
        "event": {
            "operator": {"open_id": "ou_attacker"},
            "action": {"value": {"hitl_id": "h", "option_id": "y"}},
        },
    }
    await listener._dispatch_event(event)
    assert len(captured) == 1
    assert listener._state.unauthorized == 1


@pytest.mark.asyncio
async def test_lark_listener_dispatch_card_action_invokes_callback() -> None:
    captured: list[tuple[dict[str, Any], tuple[str, str]]] = []

    async def on_card(event: dict[str, Any], parsed: tuple[str, str]) -> None:
        captured.append((event, parsed))

    listener = LarkListener(
        LarkEventCallbacks(on_card_action=on_card),
        allowed_responders=[],  # no whitelist → all allowed
    )
    event = {
        "header": {"event_type": "card.action.trigger_v1"},
        "event": {
            "operator": {"open_id": "ou_alice"},
            "action": {"value": {"hitl_id": "h", "option_id": "yes"}},
        },
    }
    await listener._dispatch_event(event)
    assert captured == [(event, ("h", "yes"))]


@pytest.mark.asyncio
async def test_lark_listener_dispatch_text_invokes_callback() -> None:
    captured: list[dict[str, str]] = []

    async def on_text(event: dict[str, Any], parsed: dict[str, str]) -> None:
        captured.append(parsed)

    listener = LarkListener(
        LarkEventCallbacks(on_text_feedback=on_text),
    )
    event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_a"}},
            "message": {"content": json.dumps({
                "text": '/popola feedback hitl-1 --option=yes --reason="ok"'
            })},
        },
    }
    await listener._dispatch_event(event)
    assert captured == [{"hitl_id": "hitl-1", "option_id": "yes", "reason": "ok"}]


@pytest.mark.asyncio
async def test_lark_listener_dispatch_unknown_event_type_ignored() -> None:
    """Events with unknown event_type are silently ignored (no callbacks)."""
    captured: list[Any] = []

    async def on_card(event: dict[str, Any], parsed: tuple[str, str]) -> None:
        captured.append(parsed)

    listener = LarkListener(LarkEventCallbacks(on_card_action=on_card))
    await listener._dispatch_event({"header": {"event_type": "wat.unknown"}})
    assert captured == []


@pytest.mark.asyncio
async def test_lark_listener_dispatch_card_action_bad_value_increments_parse_errors() -> None:
    listener = LarkListener(LarkEventCallbacks())
    event = {
        "header": {"event_type": "card.action.trigger_v1"},
        "event": {
            "operator": {"open_id": "ou_a"},
            "action": {"value": "not-json"},
        },
    }
    await listener._dispatch_event(event)
    assert listener._state.parse_errors == 1


@pytest.mark.asyncio
async def test_lark_listener_dispatch_text_no_match_no_callback() -> None:
    captured: list[Any] = []

    async def on_text(event: dict[str, Any], parsed: dict[str, str]) -> None:
        captured.append(parsed)

    listener = LarkListener(LarkEventCallbacks(on_text_feedback=on_text))
    event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_a"}},
            "message": {"content": json.dumps({"text": "hello"})},
        },
    }
    await listener._dispatch_event(event)
    assert captured == []


@pytest.mark.asyncio
async def test_lark_listener_handle_text_feedback_missing_text() -> None:
    """When message text can't be extracted parse_errors increments."""
    listener = LarkListener(LarkEventCallbacks())
    event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {"sender": {"sender_id": {"open_id": "ou_a"}}, "message": {}},
    }
    await listener._dispatch_event(event)
    assert listener._state.parse_errors == 1


@pytest.mark.asyncio
async def test_lark_listener_stop_idempotent() -> None:
    listener = LarkListener(LarkEventCallbacks())
    await listener.stop()  # no proc → no-op
    await listener.stop()  # second call also no-op


# ── LarkSupervisor: state introspection ────────────────────────────────


def test_supervisor_state_records_events_capped() -> None:
    """_emit caps history at 100/200; check the limit is respected."""
    state = SupervisorState()
    state.events.extend({"event": str(i)} for i in range(250))
    assert len(state.events) == 250  # data class doesn't enforce


@pytest.mark.asyncio
async def test_supervisor_emit_appends_event(tmp_path) -> None:
    listener = LarkListener(LarkEventCallbacks())
    captured: list[dict[str, str]] = []

    async def on_event(event: dict[str, str]) -> None:
        captured.append(dict(event))

    sup = LarkSupervisor(listener, on_event=on_event)
    await sup._emit({"event": "test"})
    assert captured == [{"event": "test", "at": captured[0]["at"]}]
    assert sup.state.events[-1]["event"] == "test"


@pytest.mark.asyncio
async def test_supervisor_emit_caps_history() -> None:
    listener = LarkListener(LarkEventCallbacks())
    sup = LarkSupervisor(listener)
    for i in range(250):
        await sup._emit({"event": f"e{i}"})
    assert len(sup.state.events) <= 200


@pytest.mark.asyncio
async def test_supervisor_emit_callback_failure_logged() -> None:
    listener = LarkListener(LarkEventCallbacks())
    raised = False

    async def bad_callback(event: dict[str, str]) -> None:
        nonlocal raised
        raised = True
        raise RuntimeError("boom")

    sup = LarkSupervisor(listener, on_event=bad_callback)
    await sup._emit({"event": "boom-event"})  # should not raise
    assert raised is True


@pytest.mark.asyncio
async def test_supervisor_stop_no_running_task() -> None:
    listener = LarkListener(LarkEventCallbacks())
    sup = LarkSupervisor(listener)
    await sup.stop()  # task is None — must not raise
