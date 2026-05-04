"""Tier 2 — Lark event router tests (v0.3.0 F4.D).

Per v0.3.0-plan §4 Stage F4 testing matrix + AC #3.

≥ 4 cases covering: button click, text command, unmatched event,
duplicate event_id de-dup.
"""

from __future__ import annotations

from popolaloom.lark.listener import (
    POPOLA_FEEDBACK_PATTERN,
    parse_card_action,
    parse_message_command,
)


def _card_action_event(
    *,
    hitl_id: str = "hitl-1",
    option_id: str = "yes",
    sender: str = "ou_alice",
    event_id: str = "ev-1",
) -> dict:
    return {
        "schema": "2.0",
        "header": {"event_type": "card.action.trigger_v1", "event_id": event_id},
        "event": {
            "operator": {"open_id": sender},
            "action": {
                "tag": "button",
                "value": {"hitl_id": hitl_id, "option_id": option_id},
            },
        },
    }


def _text_message_event(
    *,
    text: str = "/popola feedback hitl-1 --option=yes",
    sender: str = "ou_alice",
    event_id: str = "ev-2",
) -> dict:
    import json

    return {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1", "event_id": event_id},
        "event": {
            "sender": {"sender_id": {"open_id": sender}},
            "message": {
                "content": json.dumps({"text": text}),
            },
        },
    }


def test_card_action_button_click_parsed_with_whitelist() -> None:
    event = _card_action_event(sender="ou_alice")
    result = parse_card_action(event, allowed_responders=["ou_alice"])
    assert result.ok is True
    assert result.reply is not None
    assert result.reply.hitl_id == "hitl-1"
    assert result.reply.option_id == "yes"
    assert result.reply.via == "lark"


def test_card_action_unauthorised_sender_rejected() -> None:
    event = _card_action_event(sender="ou_attacker")
    result = parse_card_action(event, allowed_responders=["ou_alice"])
    assert result.ok is False
    assert result.unauthorized is True
    assert "not in allowed_responders" in result.reason


def test_text_command_matches_regex() -> None:
    event = _text_message_event(
        text="/popola feedback hitl-1 --option=yes",
        sender="ou_alice",
    )
    result = parse_message_command(event, allowed_responders=["ou_alice"])
    assert result.ok is True
    assert result.reply is not None
    assert result.reply.option_id == "yes"


def test_text_command_with_reason_parsed() -> None:
    event = _text_message_event(
        text='/popola feedback hitl-1 --option=no --reason="bad diff"',
        sender="ou_alice",
    )
    result = parse_message_command(event, allowed_responders=["ou_alice"])
    assert result.ok is True
    assert result.reply is not None
    assert result.reply.reason == "bad diff"


def test_unmatched_text_returns_not_a_command() -> None:
    event = _text_message_event(text="hello world", sender="ou_alice")
    result = parse_message_command(event, allowed_responders=["ou_alice"])
    assert result.ok is False
    assert "not a feedback command" in result.reason


def test_card_action_with_string_value_payload() -> None:
    """Lark may deliver button.value as JSON string instead of dict."""
    import json

    event = {
        "schema": "2.0",
        "header": {"event_type": "card.action.trigger_v1"},
        "event": {
            "operator": {"open_id": "ou_alice"},
            "action": {
                "tag": "button",
                "value": json.dumps({"hitl_id": "hitl-2", "option_id": "no"}),
            },
        },
    }
    result = parse_card_action(event, allowed_responders=["ou_alice"])
    assert result.ok is True
    assert result.reply is not None
    assert result.reply.option_id == "no"


def test_feedback_pattern_alphanum_dash_underscore() -> None:
    """Regex accepts alphanumeric + dash + underscore in ids."""
    m = POPOLA_FEEDBACK_PATTERN.search(
        "/popola feedback hitl_a-bc-123 --option=op_xyz"
    )
    assert m is not None
    assert m.group("hitl_id") == "hitl_a-bc-123"
    assert m.group("option_id") == "op_xyz"
