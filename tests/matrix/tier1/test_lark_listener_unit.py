"""Tier 1 — Lark listener inline-helper unit tests (v0.3.0 F4.D coverage).

Targeted unit tests for private helpers in
:mod:`popolaloom.lark.listener` that the integration tests don't
exercise via the listener subprocess.
"""

from __future__ import annotations

import json

from popolaloom.lark.listener import (
    POPOLA_FEEDBACK_PATTERN,
    _extract_event_type,
    _extract_sender_open_id,
    _extract_text_message,
    parse_card_action,
    parse_message_command,
)


def test_extract_event_type_v2_header() -> None:
    event = {"header": {"event_type": "card.action.trigger_v1"}}
    assert _extract_event_type(event) == "card.action.trigger_v1"


def test_extract_event_type_v1_inner_type() -> None:
    event = {"schema": "1.0", "event": {"type": "im.message.receive_v1"}}
    assert _extract_event_type(event) == "im.message.receive_v1"


def test_extract_event_type_returns_none_when_unset() -> None:
    assert _extract_event_type({}) is None


def test_extract_sender_open_id_from_sender_block() -> None:
    event = {"event": {"sender": {"sender_id": {"open_id": "ou_x"}}}}
    assert _extract_sender_open_id(event) == "ou_x"


def test_extract_sender_open_id_from_operator() -> None:
    event = {"event": {"operator": {"open_id": "ou_op"}}}
    assert _extract_sender_open_id(event) == "ou_op"


def test_extract_sender_open_id_returns_none_when_missing() -> None:
    assert _extract_sender_open_id({}) is None


def test_extract_text_message_returns_text_field() -> None:
    event = {
        "event": {
            "message": {"content": json.dumps({"text": "hello"})}
        }
    }
    assert _extract_text_message(event) == "hello"


def test_extract_text_message_returns_none_for_non_text() -> None:
    event = {"event": {"message": {"content": "not json"}}}
    assert _extract_text_message(event) is None


def test_popola_feedback_pattern_matches_minimal_command() -> None:
    text = "/popola feedback hitl-abc --option=yes"
    m = POPOLA_FEEDBACK_PATTERN.search(text)
    assert m is not None
    assert m.group("hitl_id") == "hitl-abc"
    assert m.group("option_id") == "yes"
    assert m.group("reason") is None


def test_popola_feedback_pattern_matches_with_reason() -> None:
    text = '/popola feedback hitl-abc --option=yes --reason="approved by admin"'
    m = POPOLA_FEEDBACK_PATTERN.search(text)
    assert m is not None
    assert m.group("reason") == "approved by admin"


def test_parse_card_action_no_whitelist_succeeds() -> None:
    """When allowed_responders is empty, sender check is skipped."""
    event = {
        "header": {"event_type": "card.action.trigger_v1", "event_id": "e-1"},
        "event": {
            "operator": {"open_id": "ou_anyone"},
            "action": {"value": {"hitl_id": "h", "option_id": "y"}},
        },
    }
    result = parse_card_action(event, allowed_responders=[])
    assert result.ok
    assert result.reply is not None


def test_parse_message_command_unrecognised_event_returns_skip() -> None:
    event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_x"}},
            "message": {"message_type": "image"},  # not text
        },
    }
    result = parse_message_command(event, allowed_responders=["ou_x"])
    assert not result.ok
    assert "feedback" in result.reason or "command" in result.reason
