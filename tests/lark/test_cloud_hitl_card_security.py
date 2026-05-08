"""Security tests for v0.8.7 cloud HITL Lark card v1 (T2.1.2 AC g).

Per ``SECURITY_CHECKLIST.md`` §4 S1 (no ``CURSOR_API_KEY`` /
``LARK_APP_SECRET`` / ``POPOLAD_API_KEY`` in the card payload) and
``lark-card-spec.md`` §6.1 (allowlist input pattern) + §6.2
(idempotency key opacity).

Each test injects a representative env value via ``monkeypatch`` and
asserts the literal value does **not** appear in ``json.dumps(card)``.
The tests are deliberately independent of the card builder's internals
(they only inspect the JSON-serialised output) so they keep working if
the card builder is later refactored.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import pytest

from popolaloom.lark.cloud_hitl_card import (
    CARD_METADATA_KEYS,
    CARD_TEMPLATE_ID,
    CARD_TEMPLATE_VERSION,
    CloudHITLCardInput,
    build_cloud_hitl_card,
    compute_idempotency_key,
    mutate_card_for_answered,
    mutate_card_for_pending_second_approver,
    mutate_card_for_timeout,
)

# ── Test fixtures ──────────────────────────────────────────────────────


_SAMPLE_DEADLINE = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
_SAMPLE_NOW = datetime(2026, 5, 8, 11, 45, 0, tzinfo=UTC)
_SAMPLE_HITL_ID = "abcdef0123456789abcdef0123456789"
_SAMPLE_TASK_ID = "popola-task-secret-test"
_SAMPLE_QUESTION = "Approve secret rotation?"
_SAMPLE_PROMPT_BODY = (
    "Plan: rotate AWS access keys for prod-us-east-1. Existing keys will be "
    "invalidated within 60 seconds of approval."
)


def _make_input(**overrides: Any) -> CloudHITLCardInput:
    base: dict[str, Any] = {
        "hitl_id": _SAMPLE_HITL_ID,
        "task_id": _SAMPLE_TASK_ID,
        "question_text": _SAMPLE_QUESTION,
        "prompt_body": _SAMPLE_PROMPT_BODY,
        "cursor_agent_id": "bc-1234567890ab",
        "cursor_run_id": "run-abcd1234",
        "idempotency_key": compute_idempotency_key(
            task_id=_SAMPLE_TASK_ID,
            cursor_run_id="run-abcd1234",
            question_text=_SAMPLE_QUESTION,
        ),
        "expiration_at": _SAMPLE_DEADLINE,
    }
    base.update(overrides)
    return CloudHITLCardInput(**base)


# ── (S-1) CURSOR_API_KEY does NOT appear in the rendered card ──────────


def test_cursor_api_key_not_in_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per SECURITY S1 + spec §6.1: ``CURSOR_API_KEY`` env value MUST NOT
    leak into the card payload (header, body, action.value, or
    ``card_metadata``)."""
    sentinel = "key_AKIAIOSFODNN7EXAMPLE_CURSOR_DO_NOT_LEAK"
    monkeypatch.setenv("CURSOR_API_KEY", sentinel)

    card = build_cloud_hitl_card(_make_input(), now=_SAMPLE_NOW)
    serialised = json.dumps(card, ensure_ascii=False)

    assert sentinel not in serialised, (
        "CURSOR_API_KEY env value MUST NOT appear anywhere in the card payload"
    )
    assert os.environ.get("CURSOR_API_KEY", "fallback-default") not in serialised


# ── (S-2) LARK_APP_SECRET does NOT appear in the rendered card ─────────


def test_lark_app_secret_not_in_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per SECURITY S1: ``LARK_APP_SECRET`` (Lark webhook signing secret)
    MUST NOT leak into the card payload."""
    sentinel = "lark_app_secret_DO_NOT_LEAK_42"
    monkeypatch.setenv("LARK_APP_SECRET", sentinel)

    card = build_cloud_hitl_card(_make_input(), now=_SAMPLE_NOW)
    serialised = json.dumps(card, ensure_ascii=False)

    assert sentinel not in serialised


def test_lark_verify_token_not_in_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per SECURITY S1 + S3: the HMAC verification token also MUST NOT
    appear in the card body."""
    sentinel = "lark_verify_token_DO_NOT_LEAK_99"
    monkeypatch.setenv("LARK_VERIFY_TOKEN", sentinel)

    card = build_cloud_hitl_card(_make_input(), now=_SAMPLE_NOW)
    serialised = json.dumps(card, ensure_ascii=False)

    assert sentinel not in serialised


def test_popolad_api_key_not_in_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per SECURITY S4: ``POPOLAD_API_KEY`` (per-tenant token) MUST NOT
    appear in the rendered card."""
    sentinel = "popolad_api_key_DO_NOT_LEAK_tenant_a"
    monkeypatch.setenv("POPOLAD_API_KEY", sentinel)

    card = build_cloud_hitl_card(_make_input(), now=_SAMPLE_NOW)
    serialised = json.dumps(card, ensure_ascii=False)

    assert sentinel not in serialised


# ── (S-3) Idempotency key is opaque (sha256-prefixed, non-reversible) ──


def test_idempotency_key_is_opaque_in_card_metadata() -> None:
    """Per spec §6.2 + SECURITY R1: the rendered ``idempotency_key`` is
    sha256-prefixed, length-bounded, and one-way (cannot recover inputs)."""
    sensitive_question = "leaking_marker_in_question_text_should_not_appear"
    sensitive_task = "leaking_marker_task_id"
    card = build_cloud_hitl_card(
        _make_input(
            task_id=sensitive_task,
            question_text=sensitive_question,
            idempotency_key=compute_idempotency_key(
                task_id=sensitive_task,
                cursor_run_id="run-abcd",
                question_text=sensitive_question,
            ),
        ),
        now=_SAMPLE_NOW,
    )
    metadata = card["card_metadata"]
    key = metadata["idempotency_key"]

    assert isinstance(key, str)
    assert key.startswith("sha256:")
    assert len(key) == len("sha256:") + 16

    hex_part = key[len("sha256:"):]
    assert all(c in "0123456789abcdef" for c in hex_part)

    assert sensitive_question not in key
    assert sensitive_task not in key


def test_idempotency_key_distinct_inputs_give_distinct_keys() -> None:
    """Two independent prompts must NOT collide on the truncated digest
    (sanity bound — collision probability ~1/2^64, but the test uses
    deterministic distinct inputs)."""
    k_a = compute_idempotency_key(
        task_id="task_a", cursor_run_id="run_a", question_text="q_a"
    )
    k_b = compute_idempotency_key(
        task_id="task_b", cursor_run_id="run_a", question_text="q_a"
    )
    k_c = compute_idempotency_key(
        task_id="task_a", cursor_run_id="run_b", question_text="q_a"
    )
    k_d = compute_idempotency_key(
        task_id="task_a", cursor_run_id="run_a", question_text="q_b"
    )
    assert len({k_a, k_b, k_c, k_d}) == 4, "no collisions among deterministic samples"


# ── (S-4) card_metadata MUST NOT contain full prompt_body or PII ───────


def test_card_metadata_does_not_contain_full_prompt_body() -> None:
    """Per spec §2.4: ``card_metadata`` MUST NOT contain the full
    ``prompt_body`` or any PII; it carries only opaque identifiers and
    timing fields. The prompt body lives only in B2 (truncated) and on
    the popolad-served context page."""
    distinctive = "MARKER_PROMPT_BODY_CONTENT_NEVER_IN_METADATA"
    long_body = distinctive + ("z" * 250)
    card = build_cloud_hitl_card(
        _make_input(prompt_body=long_body),
        now=_SAMPLE_NOW,
    )
    metadata_serialised = json.dumps(card["card_metadata"])
    assert distinctive not in metadata_serialised, (
        "prompt_body marker leaked into card_metadata — would expose the "
        "full body to log scrapers / dispatch routers"
    )


def test_card_metadata_keys_are_exactly_the_allowlist() -> None:
    """Per spec §6.1 (allowlist pattern): ``card_metadata`` exposes
    exactly the documented 12 keys — no extras, no omissions."""
    card = build_cloud_hitl_card(_make_input(), now=_SAMPLE_NOW)
    metadata = card["card_metadata"]
    actual = set(metadata.keys())
    expected = set(CARD_METADATA_KEYS)
    assert actual == expected, (
        f"card_metadata key drift detected: missing={expected - actual} "
        f"unexpected={actual - expected}"
    )


def test_card_metadata_template_stamp_is_pinned() -> None:
    """Per spec §4.3 + SECURITY I-3: ``template_version`` and
    ``template_id`` are the dispatch keys; an unstamped card would let
    a future v2 receiver mistakenly handle a v1 callback as v2."""
    card = build_cloud_hitl_card(_make_input(), now=_SAMPLE_NOW)
    metadata = card["card_metadata"]
    assert metadata["template_version"] == CARD_TEMPLATE_VERSION == "v1"
    assert metadata["template_id"] == CARD_TEMPLATE_ID == "cloud_hitl_request_card_v1"


# ── (S-5) Mutators preserve the no-leak invariant after state changes ──


def test_mutators_preserve_no_secret_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """The S1 / S2 / S3 mutators MUST NOT introduce env-secret values into
    the card; iterate through the full state chain and re-assert the
    sentinel never appears."""
    sentinel_a = "CURSOR_API_KEY_LEAK_SENTINEL_42"
    sentinel_b = "LARK_APP_SECRET_LEAK_SENTINEL_99"
    monkeypatch.setenv("CURSOR_API_KEY", sentinel_a)
    monkeypatch.setenv("LARK_APP_SECRET", sentinel_b)

    base = build_cloud_hitl_card(
        _make_input(responder_policy="serial_two"), now=_SAMPLE_NOW
    )
    pending_second = mutate_card_for_pending_second_approver(
        base,
        "ou_first_approver",
        first_approver_at=datetime(2026, 5, 8, 11, 48, 0, tzinfo=UTC),
    )
    answered = mutate_card_for_answered(
        pending_second,
        "ou_second_approver",
        datetime(2026, 5, 8, 11, 55, 0, tzinfo=UTC),
        option_id="approve",
        channel="lark",
    )
    timed_out = mutate_card_for_timeout(
        base,
        timed_out_at=datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC),
    )

    for variant in (base, pending_second, answered, timed_out):
        rendered = json.dumps(variant, ensure_ascii=False)
        assert sentinel_a not in rendered
        assert sentinel_b not in rendered


def test_first_approver_open_id_only_appears_after_s2_click() -> None:
    """Per SECURITY P2 + spec §3.2: ``first_approver_open_id`` is null on
    a freshly-built card; only the S2 mutator may populate it."""
    fresh = build_cloud_hitl_card(
        _make_input(responder_policy="serial_two"), now=_SAMPLE_NOW
    )
    fresh_serialised = json.dumps(fresh)
    assert "ou_first_approver_x" not in fresh_serialised
    assert fresh["card_metadata"]["first_approver_open_id"] is None
    assert fresh["card_metadata"]["first_approver_at"] is None

    after_first_click = mutate_card_for_pending_second_approver(
        fresh,
        "ou_first_approver_x",
        first_approver_at=datetime(2026, 5, 8, 11, 48, 0, tzinfo=UTC),
    )
    metadata = after_first_click["card_metadata"]
    assert metadata["first_approver_open_id"] == "ou_first_approver_x"
    assert metadata["first_approver_at"] is not None
