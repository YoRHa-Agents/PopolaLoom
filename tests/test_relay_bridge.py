"""Tests for v0.7.3 relay→handoff envelope bridge (``to_handoff_envelope``).

The bridge converts the v0.3.0 :class:`RelayHandoffEnvelope` into the
v0.8.0 :class:`HandoffEnvelope` so callers can write file-based audit
records for relay'd dispatches without changing the relay primitive
itself.
"""

from __future__ import annotations

import pytest

from popolaloom.daemon.primitives import RelayHandoffEnvelope, to_handoff_envelope
from popolaloom.handoff import HandoffEnvelope

_SENTINEL: dict[str, object] = {"__not_set__": True}


def _build_relay(
    *,
    payload: dict[str, object] | None = None,
    constraints: dict[str, object] | None = None,
) -> RelayHandoffEnvelope:
    # Honor explicit empty dict — caller distinguishes "no override" (None)
    # from "explicitly empty" ({}). The "or" idiom would silently coerce
    # both, so we use an explicit None-check.
    actual_payload = (
        payload
        if payload is not None
        else {"file": "src/foo.py", "kind": "review"}
    )
    actual_constraints = (
        constraints
        if constraints is not None
        else {"timeout": 1800}
    )
    return RelayHandoffEnvelope(
        source_cli="cursor",
        target_cli="claude",
        source_task_id="cursor-23e74ec18917",
        payload=actual_payload,
        reason="cross-CLI code review",
        constraints=actual_constraints,
    )


# ── happy path ──────────────────────────────────────────────────────────


def test_bridge_returns_handoff_envelope() -> None:
    """Conversion produces a valid HandoffEnvelope instance."""
    relay_env = _build_relay()
    result = to_handoff_envelope(relay_env)

    assert isinstance(result, HandoffEnvelope)


def test_bridge_maps_core_fields() -> None:
    """source_cli / target_cli / parent_task_id / reason / constraints carry over."""
    relay_env = _build_relay()
    result = to_handoff_envelope(relay_env)

    assert result.source_cli == "cursor"
    assert result.target_cli == "claude"
    assert result.parent_task_id == "cursor-23e74ec18917"
    assert result.reason == "cross-CLI code review"
    assert result.constraints == {"timeout": 1800}


def test_bridge_default_prompt_synthesised() -> None:
    """No explicit prompt → ``[relay from <id>] <reason>`` synthesised."""
    relay_env = _build_relay()
    result = to_handoff_envelope(relay_env)

    assert result.prompt == (
        "[relay from cursor-23e74ec18917] cross-CLI code review"
    )


def test_bridge_explicit_prompt_used() -> None:
    """``prompt=`` arg overrides the synthesised default."""
    relay_env = _build_relay()
    result = to_handoff_envelope(relay_env, prompt="explicit reviewer prompt")

    assert result.prompt == "explicit reviewer prompt"


def test_bridge_explicit_cwd_used() -> None:
    """``cwd=`` arg sets the new envelope's cwd."""
    relay_env = _build_relay()
    result = to_handoff_envelope(relay_env, cwd="/work/repo")

    assert result.cwd == "/work/repo"


def test_bridge_default_cwd_none() -> None:
    """Default cwd is None when not supplied."""
    relay_env = _build_relay()
    result = to_handoff_envelope(relay_env)

    assert result.cwd is None


def test_bridge_payload_folded_into_adapter_extra() -> None:
    """``payload`` lands under ``adapter_extra["_relay_payload"]``."""
    payload = {"file": "src/foo.py", "kind": "review"}
    relay_env = _build_relay(payload=payload)
    result = to_handoff_envelope(relay_env)

    assert result.adapter_extra == {"_relay_payload": payload}


def test_bridge_empty_payload_yields_empty_adapter_extra() -> None:
    """Empty payload → adapter_extra is empty (no ``_relay_payload`` key)."""
    relay_env = _build_relay(payload={})
    result = to_handoff_envelope(relay_env)

    assert result.adapter_extra == {}


def test_bridge_tags_marked_relay_bridged() -> None:
    """Bridged envelopes are tagged ``relay-bridged`` for downstream filtering."""
    relay_env = _build_relay()
    result = to_handoff_envelope(relay_env)

    assert "relay-bridged" in result.tags


def test_bridge_handoff_id_format() -> None:
    """Synthesised handoff_id matches ``<cli>-<slug>-<8hex>`` pattern."""
    import re

    relay_env = _build_relay()
    result = to_handoff_envelope(relay_env)

    assert re.fullmatch(r"^claude-[a-z0-9-]+-[0-9a-f]{8}$", result.handoff_id), (
        result.handoff_id
    )


def test_bridge_schema_version_is_one() -> None:
    """Bridged envelope carries the v0.8.0 schema_version="1"."""
    relay_env = _build_relay()
    result = to_handoff_envelope(relay_env)

    assert result.schema_version == "1"


# ── invalid inputs ──────────────────────────────────────────────────────


def test_bridge_rejects_non_relay_envelope() -> None:
    """Passing non-RelayHandoffEnvelope → TypeError (No Silent Failures)."""
    with pytest.raises(TypeError, match="RelayHandoffEnvelope"):
        to_handoff_envelope("not a relay envelope")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="RelayHandoffEnvelope"):
        to_handoff_envelope({"source_cli": "cursor"})  # type: ignore[arg-type]


# ── round-trip via write/load ───────────────────────────────────────────


def test_bridge_envelope_can_be_written_and_loaded(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Bridged envelope can be written + loaded like any other HandoffEnvelope."""
    from popolaloom.handoff import load_envelope, write_envelope

    relay_env = _build_relay()
    new_env = to_handoff_envelope(relay_env)

    write_envelope(new_env, base_dir=tmp_path)
    loaded = load_envelope(new_env.handoff_id, base_dir=tmp_path)

    assert loaded == new_env
    assert "relay-bridged" in loaded.tags
    assert loaded.parent_task_id == relay_env.source_task_id
