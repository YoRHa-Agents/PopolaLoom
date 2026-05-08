"""M7 / SECURITY R4 — template_version dispatch isolation tests.

Per ``.local/.agent/active/v0.8.7-cloud-hitl-prod/SECURITY_CHECKLIST.md`` §5
**R4**: replay across templates is impossible because
``card_metadata.template_version`` is part of the dispatch state machine.
A v0.8.8+ v2 card MUST NOT satisfy v1 listener dedup keys (and vice
versa).

Pre-fix the listener ignored ``template_version`` entirely, so the
defense was conceptual. The Stage 3 fix adds a dispatch step in both
:meth:`LarkListener._handle_card_action` (the production runtime path)
and :func:`parse_card_action` (the renderer-side adapter) that rejects
unknown versions with ``unauthorized=True``.

The tests below cover the v1-OK / v2-rejected matrix for both code paths.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from popolaloom.lark.listener import (
    SUPPORTED_TEMPLATE_VERSIONS,
    LarkEventCallbacks,
    LarkListener,
    parse_card_action,
)


def _build_event(
    *,
    template_version: str,
    sender_open_id: str = "ou_legit",
) -> dict[str, Any]:
    """Construct a synthetic ``card.action.trigger_v1`` event."""
    return {
        "header": {
            "event_type": "card.action.trigger_v1",
            "event_id": f"evt-{template_version}",
        },
        "event": {
            "operator": {"open_id": sender_open_id},
            "action": {
                "value": {
                    "hitl_id": "h-tv",
                    "option_id": "approve",
                    "template_version": template_version,
                }
            },
        },
    }


# ── parse_card_action (renderer adapter) — v1 OK / v2 rejected ───────────


def test_parse_card_action_accepts_v1_template() -> None:
    """A v1 card click parses cleanly when sender is in the allowlist."""
    event = _build_event(template_version="v1")
    result = parse_card_action(
        event,
        allowed_responders=["ou_legit"],
    )
    assert result.ok is True, f"v1 rejected: {result.reason}"
    assert result.unauthorized is False
    assert result.reply is not None
    assert result.reply.hitl_id == "h-tv"


def test_parse_card_action_rejects_unknown_v2_template() -> None:
    """A v2 card click is rejected with ``unauthorized=True`` and a
    descriptor citing the unsupported version (No Silent Failures —
    the operator can grep daemon logs for the exact rejection reason).
    """
    event = _build_event(template_version="v2")
    result = parse_card_action(
        event,
        allowed_responders=["ou_legit"],
    )
    assert result.ok is False, (
        "M7 regression: v2 card click was accepted by parse_card_action — "
        "an attacker could downgrade-replay v0.8.8+ cards against v1 listeners."
    )
    assert result.unauthorized is True
    assert "v2" in result.reason
    assert result.reply is None


def test_parse_card_action_missing_template_version_defaults_to_v1() -> None:
    """A v0.8.5 card (rendered before v0.8.7 added the version stamp)
    has no ``template_version`` field; the listener defaults to ``"v1"``
    so legacy cards still parse cleanly."""
    event = _build_event(template_version="v1")
    # Strip the version field
    del event["event"]["action"]["value"]["template_version"]
    result = parse_card_action(
        event,
        allowed_responders=["ou_legit"],
    )
    assert result.ok is True, (
        f"backward-compat regression: legacy card without template_version "
        f"rejected: {result.reason}"
    )


def test_supported_template_versions_contains_v1_only_for_v0_8_7() -> None:
    """Sanity: the v0.8.7 dispatch table contains ONLY ``"v1"`` —
    forward-compat assertion that catches an accidental "permit all"
    (e.g., setting it to a wildcard or adding "v2" prematurely)."""
    assert frozenset({"v1"}) == SUPPORTED_TEMPLATE_VERSIONS, (
        f"v0.8.7 ships v1 only; got {SUPPORTED_TEMPLATE_VERSIONS}. Bumping "
        f"this set requires a corresponding receiver-code-path change."
    )


# ── LarkListener._handle_card_action — runtime path ─────────────────────


@pytest.mark.asyncio
async def test_listener_handle_card_action_accepts_v1() -> None:
    """The production listener path dispatches a v1 click into
    ``on_card_action`` (no ``unauthorized`` increment)."""
    received: list[tuple[str, str]] = []

    async def on_card_action(
        event: dict[str, Any], parsed: tuple[str, str]
    ) -> None:
        received.append(parsed)

    listener = LarkListener(
        callbacks=LarkEventCallbacks(on_card_action=on_card_action),
        allowed_responders=["ou_legit"],
    )
    event = _build_event(template_version="v1")
    await listener._dispatch_event(event)  # type: ignore[attr-defined]

    assert received == [("h-tv", "approve")]
    assert listener._state.unauthorized == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_listener_handle_card_action_rejects_v2() -> None:
    """A v2 click is rejected: ``on_card_action`` is NOT called and the
    ``on_unauthorized`` callback fires so the operator's daemon logs
    record the rejection (per spec §4.3 dispatch pseudocode)."""
    received: list[tuple[str, str]] = []
    unauthorized_events: list[dict[str, Any]] = []

    async def on_card_action(
        event: dict[str, Any], parsed: tuple[str, str]
    ) -> None:
        received.append(parsed)

    async def on_unauthorized(event: dict[str, Any], sender: str) -> None:
        unauthorized_events.append(event)

    listener = LarkListener(
        callbacks=LarkEventCallbacks(
            on_card_action=on_card_action,
            on_unauthorized=on_unauthorized,
        ),
        allowed_responders=["ou_legit"],
    )
    event = _build_event(template_version="v2")
    await listener._dispatch_event(event)  # type: ignore[attr-defined]
    await asyncio.sleep(0)

    assert received == [], (
        "M7 regression: v2 card was dispatched into on_card_action; "
        "the version dispatch is missing from the runtime listener path."
    )
    assert len(unauthorized_events) == 1, (
        "expected on_unauthorized to fire on rejected template_version"
    )
    assert listener._state.unauthorized >= 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_listener_handle_card_action_rejects_garbage_version() -> None:
    """Empty / non-string / random-junk versions are also rejected."""
    received: list[tuple[str, str]] = []

    async def on_card_action(
        event: dict[str, Any], parsed: tuple[str, str]
    ) -> None:
        received.append(parsed)

    listener = LarkListener(
        callbacks=LarkEventCallbacks(on_card_action=on_card_action),
        allowed_responders=["ou_legit"],
    )
    event = _build_event(template_version="garbage")
    await listener._dispatch_event(event)  # type: ignore[attr-defined]

    assert received == []
    assert listener._state.unauthorized >= 1  # type: ignore[attr-defined]
