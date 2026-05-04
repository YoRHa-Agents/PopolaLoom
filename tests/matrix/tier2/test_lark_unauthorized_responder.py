"""Tier 2 — Lark unauthorized responder rejection (v0.3.0 F4.D, R-LARK-3).

Per v0.3.0-plan D3.7 (allowed_responders default to target_open_id) +
RV3-9 mitigation: events from senders outside the whitelist must be
rejected with ``unauthorized=True`` (not silently dropped).
"""

from __future__ import annotations

from popolaloom.lark.listener import parse_card_action


def test_default_whitelist_blocks_unknown_open_id() -> None:
    event = {
        "header": {"event_type": "card.action.trigger_v1", "event_id": "ev-1"},
        "event": {
            "operator": {"open_id": "ou_attacker"},
            "action": {
                "tag": "button",
                "value": {"hitl_id": "hitl-x", "option_id": "yes"},
            },
        },
    }
    result = parse_card_action(event, allowed_responders=["ou_target"])
    assert result.ok is False
    assert result.unauthorized is True
    assert result.sender_open_id == "ou_attacker"
    # The reason explicitly mentions the bad sender for audit logs (No Silent Failures)
    assert "ou_attacker" in result.reason
