"""C2 / SECURITY P1 — single-approver ACL tests.

Per ``.local/.agent/active/v0.8.7-cloud-hitl-prod/SECURITY_CHECKLIST.md`` §7
**P1**: every Lark card click MUST be evaluated against an allowlist of
``open_id`` values (operator-configured via ``LARK_HITL_ALLOWED_OPEN_IDS``
or per-card via ``card_metadata.allowed_responder_open_ids``). A click
from a non-allowlisted user is rejected with no row mutation; a click
from a member is accepted; a per-card override scopes that decision
even when the env-allowlist is unset.

The cases below exercise the listener-layer ACL boundary using
:func:`popolaloom.lark.listener.parse_card_action`, which reads the
inbound event and consults the active responder allowlist (the env-wide
list by default, the per-card override when supplied).

These tests cover SECURITY P1 (REVIEW.md C2) — the missing test files
cited in §7 P1 of the checklist. Three cases:

1. ``test_non_member_click_rejected`` — sender open_id NOT in the
   allowlist → ``ok=False`` + ``unauthorized=True`` + descriptor
   names the rejected sender.
2. ``test_member_click_accepted`` — sender open_id is in the
   allowlist → ``ok=True`` + a :class:`HITLReply` is returned.
3. ``test_per_card_acl_overrides_group_default`` — when the caller
   supplies an explicit ``allowed_responders`` argument it overrides
   the env-wide list (per spec §3.2 P1 "per-card override").
"""

from __future__ import annotations

from typing import Any

import pytest

from popolaloom.lark.listener import parse_card_action

# ── helpers ─────────────────────────────────────────────────────────────


def _build_card_action_event(
    *,
    sender_open_id: str | None,
    hitl_id: str = "h-test",
    option_id: str = "approve",
    template_version: str = "v1",
) -> dict[str, Any]:
    """Build a synthetic ``card.action.trigger_v1`` event.

    Mirrors the lark-cli NDJSON event shape so :func:`parse_card_action`
    sees an inbound that is structurally identical to a real Lark
    callback (without any HMAC since γ-mode authenticates via the
    websocket session — see SECURITY S3 doc-fix).
    """
    operator: dict[str, Any] = {}
    if sender_open_id is not None:
        operator["open_id"] = sender_open_id
    return {
        "header": {
            "event_type": "card.action.trigger_v1",
            "event_id": "evt-test",
        },
        "event": {
            "operator": operator,
            "action": {
                "value": {
                    "hitl_id": hitl_id,
                    "option_id": option_id,
                    "template_version": template_version,
                }
            },
        },
    }


# ── P1 case 1: non-member rejected ──────────────────────────────────────


def test_non_member_click_rejected() -> None:
    """A click whose ``operator.open_id`` is NOT in ``allowed_responders``
    MUST be rejected (``ok=False`` + ``unauthorized=True``).

    Mirrors the Lark group-reshare attack: a malicious user inside a
    group reshares the card into another group; their open_id is NOT
    on the operator's approver allowlist; the listener drops the click
    with the clear unauthorized descriptor.
    """
    event = _build_card_action_event(sender_open_id="ou_outsider")
    result = parse_card_action(
        event,
        allowed_responders=["ou_alice", "ou_bob"],
    )
    assert result.ok is False, (
        "P1 regression: non-member click was accepted; "
        "single-approver ACL is bypassed."
    )
    assert result.unauthorized is True
    assert "ou_outsider" in result.reason
    assert result.reply is None


def test_non_member_with_unset_open_id_is_rejected() -> None:
    """A click missing the ``open_id`` (corrupted Lark event) MUST also
    be rejected — never default to "permit when unknown" (per workspace
    rule "No Silent Failures")."""
    event = _build_card_action_event(sender_open_id=None)
    result = parse_card_action(
        event,
        allowed_responders=["ou_alice"],
    )
    assert result.ok is False
    assert result.unauthorized is True
    assert result.reply is None


# ── P1 case 2: member accepted ──────────────────────────────────────────


def test_member_click_accepted() -> None:
    """A click whose ``operator.open_id`` IS in ``allowed_responders`` is
    accepted: the parser returns a :class:`HITLReply` carrying the
    inbound ``hitl_id`` / ``option_id`` and the responder open_id."""
    event = _build_card_action_event(
        sender_open_id="ou_alice", option_id="approve"
    )
    result = parse_card_action(
        event,
        allowed_responders=["ou_alice", "ou_bob"],
    )
    assert result.ok is True, (
        f"P1 regression: legitimate member click rejected: {result.reason}"
    )
    assert result.unauthorized is False
    assert result.reply is not None
    assert result.reply.hitl_id == "h-test"
    assert result.reply.option_id == "approve"
    assert result.sender_open_id == "ou_alice"


# ── P1 case 3: per-card override ────────────────────────────────────────


def test_per_card_acl_overrides_group_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-supplied ``allowed_responders`` overrides the env-wide
    list (``LARK_HITL_ALLOWED_OPEN_IDS``).

    Threat model: the env-wide list is the default approver pool; some
    cards (e.g., a payments-team approval) need a stricter or different
    list. The per-card override lets the listener consult that scoped
    list instead of falling back to the env-wide list, which would
    otherwise authorise users outside the card's intended approvers.
    """
    # Configure a permissive env-wide list — under it, ou_carol would be
    # accepted. The per-card override restricts to ou_alice only.
    monkeypatch.setenv(
        "LARK_HITL_ALLOWED_OPEN_IDS",
        "ou_alice,ou_bob,ou_carol",
    )

    # Per-card override accepts only ou_alice — ou_carol is in the
    # env-wide list but NOT in the override → rejected.
    event_carol = _build_card_action_event(sender_open_id="ou_carol")
    result_carol = parse_card_action(
        event_carol,
        allowed_responders=["ou_alice"],  # per-card override
    )
    assert result_carol.ok is False
    assert result_carol.unauthorized is True

    # And the per-card override correctly accepts ou_alice.
    event_alice = _build_card_action_event(sender_open_id="ou_alice")
    result_alice = parse_card_action(
        event_alice,
        allowed_responders=["ou_alice"],
    )
    assert result_alice.ok is True
    assert result_alice.reply is not None
