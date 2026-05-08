"""Functional tests for v0.8.7 cloud HITL Lark card v1 (T2.1.2 AC f).

Covers (per ``PLAN.md`` §4.1 T2.1.2 AC f / spec §2.3 + §3 + §4):

- Happy build: 4-block envelope + 12-key ``card_metadata``.
- All 3 P0 state transitions (S1 single-approver, S2 serial-two,
  S3 timeout) — one mutator per scenario.
- Truncation boundaries on B2 at 199 / 200 / 201 chars.
- B1 over-2000-char rejection at the builder boundary.
- Metadata version stamp (``template_version`` + ``template_id``).
- Header color invariants per spec §2.1.
- Action-button payload shape per spec §2.3 A1.

These tests live in the default lane (no ``slow`` / ``real_lark`` / etc.
markers) — pure-function asserts, no IO.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from popolaloom.lark.card_templates import LARK_FOOTER, LARK_NOTIFY_PROMPT_TRUNCATE
from popolaloom.lark.cloud_hitl_card import (
    CARD_METADATA_KEYS,
    CARD_TEMPLATE_ID,
    CARD_TEMPLATE_VERSION,
    DEFAULT_POPOLAD_HOST,
    DEFAULT_POPOLAD_PORT,
    DEFAULT_TIMEOUT_S,
    MAX_QUESTION_TEXT_LEN,
    CloudHITLCardInput,
    build_cloud_hitl_card,
    compute_idempotency_key,
    mutate_card_for_answered,
    mutate_card_for_pending_second_approver,
    mutate_card_for_timeout,
)

# ── Fixtures / factories ────────────────────────────────────────────────


_SAMPLE_DEADLINE = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
_SAMPLE_NOW = datetime(2026, 5, 8, 11, 45, 0, tzinfo=UTC)
_SAMPLE_HITL_ID = "abcdef0123456789abcdef0123456789"
_SAMPLE_TASK_ID = "popola-task-42"
_SAMPLE_QUESTION = (
    "May the cloud agent run `terraform apply` against the prod-us-east-1 "
    "workspace?"
)
_SAMPLE_PROMPT_BODY = (
    "Plan diff: 14 add, 2 modify, 0 destroy. Estimated cost delta: $250/month."
)


def _make_input(**overrides: Any) -> CloudHITLCardInput:
    """Build a representative :class:`CloudHITLCardInput` for tests."""
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
        "timeout_seconds": DEFAULT_TIMEOUT_S,
        "responder_policy": "single",
    }
    base.update(overrides)
    return CloudHITLCardInput(**base)


def _div_elements(card: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in card["body"]["elements"] if e.get("tag") == "div"]


def _action_block(card: dict[str, Any]) -> dict[str, Any] | None:
    for e in card["body"]["elements"]:
        if e.get("tag") == "action":
            return e
    return None


def _div_text(card: dict[str, Any], idx: int) -> str:
    text = _div_elements(card)[idx]["text"]["content"]
    assert isinstance(text, str)
    return text


# ── (1) Happy build — 4-block envelope ─────────────────────────────────


def test_build_cloud_hitl_card_happy_path_envelope_shape() -> None:
    card = build_cloud_hitl_card(_make_input(), now=_SAMPLE_NOW)

    assert card["schema"] == "2.0"
    assert card["config"]["wide_screen_mode"] is True
    header = card["header"]
    assert header["title"]["tag"] == "plain_text"
    assert header["title"]["content"] == f"PopolaLoom HITL — {_SAMPLE_TASK_ID}"
    assert header["subtitle"]["content"] == "Cloud agent approval"
    assert header["template"] == "blue"  # initial Pending color
    assert header["ud_icon"] == {"token": "approval_outlined"}

    elements = card["body"]["elements"]
    divs = _div_elements(card)
    actions = _action_block(card)
    assert len(divs) == 3, "must have B1+B2+B3 div blocks"
    assert actions is not None, "must have A1 action block"
    assert len(elements) == 4

    b1, b2, b3 = divs
    assert b1["text"]["tag"] == "lark_md"
    assert "Question" in b1["text"]["content"]
    assert _SAMPLE_QUESTION in b1["text"]["content"]
    assert "Context" in b2["text"]["content"]
    assert "[Expand →]" in b2["text"]["content"]
    assert _SAMPLE_HITL_ID in b3["text"]["content"]
    assert _SAMPLE_TASK_ID in b3["text"]["content"]
    assert b3["text"]["content"].rstrip().endswith(LARK_FOOTER.strip()), (
        "B3 must end with the workspace-rule footer"
    )

    buttons = actions["actions"]
    assert [b["value"]["action"] for b in buttons] == ["approve", "reject", "custom"]
    for b in buttons:
        assert b["value"]["hitl_id"] == _SAMPLE_HITL_ID
        assert b["value"]["template_version"] == CARD_TEMPLATE_VERSION


# ── (2) Metadata version stamp + 12 keys ────────────────────────────────


def test_card_metadata_version_stamp_and_full_key_set() -> None:
    card = build_cloud_hitl_card(_make_input(), now=_SAMPLE_NOW)
    metadata = card["card_metadata"]

    assert metadata["template_version"] == "v1"
    assert metadata["template_id"] == CARD_TEMPLATE_ID
    assert metadata["template_id"] == "cloud_hitl_request_card_v1"
    assert metadata["idempotency_key"].startswith("sha256:")
    assert metadata["responder_policy"] == "single"
    assert metadata["first_approver_open_id"] is None
    assert metadata["first_approver_at"] is None
    assert set(metadata.keys()) == set(CARD_METADATA_KEYS)
    assert len(CARD_METADATA_KEYS) == 12


# ── (3) B2 truncation boundary (199 / 200 / 201) ───────────────────────


@pytest.mark.parametrize(
    "length, expect_ellipsis",
    [
        (199, False),
        (200, False),
        (201, True),
    ],
    ids=["199-no-ellipsis", "200-boundary-no-ellipsis", "201-truncated"],
)
def test_b2_truncation_boundary(length: int, expect_ellipsis: bool) -> None:
    body = "x" * length
    card = build_cloud_hitl_card(_make_input(prompt_body=body), now=_SAMPLE_NOW)
    b2_text = _div_text(card, 1)
    if expect_ellipsis:
        assert "…" in b2_text, (
            f"prompt_body of length {length} should be truncated + …"
        )
        rendered_x_run = "x" * LARK_NOTIFY_PROMPT_TRUNCATE
        assert rendered_x_run in b2_text
        assert "x" * (LARK_NOTIFY_PROMPT_TRUNCATE + 1) not in b2_text
    else:
        assert "…" not in b2_text.replace("Expand →", ""), (
            f"prompt_body of length {length} should NOT be truncated"
        )
        assert body in b2_text, "full prompt_body must appear when ≤200"


# ── (4) B1 question-too-long rejection at the builder boundary ─────────


def test_question_text_at_max_len_rejected() -> None:
    too_long = "y" * MAX_QUESTION_TEXT_LEN
    with pytest.raises(ValueError, match="question_text length"):
        _make_input(question_text=too_long)


def test_question_text_just_under_max_accepted() -> None:
    just_under = "y" * (MAX_QUESTION_TEXT_LEN - 1)
    card = build_cloud_hitl_card(_make_input(question_text=just_under), now=_SAMPLE_NOW)
    assert just_under in _div_text(card, 0)


# ── (5) S1 — mutate_card_for_answered ──────────────────────────────────


@pytest.mark.parametrize(
    "option_id, expected_color, expected_emoji",
    [
        ("approve", "green", "✅"),
        ("reject", "red", "❌"),
        ("custom", "yellow", "📝"),
    ],
)
def test_mutate_for_answered_s1(
    option_id: str,
    expected_color: str,
    expected_emoji: str,
) -> None:
    card = build_cloud_hitl_card(_make_input(), now=_SAMPLE_NOW)
    answered_at = datetime(2026, 5, 8, 11, 50, 0, tzinfo=UTC)

    mutated = mutate_card_for_answered(
        card,
        "ou_responder_alice",
        answered_at,
        option_id=option_id,
        channel="lark",
    )

    assert mutated is not card, "must return a new card (no in-place mutation)"
    assert card["header"]["template"] == "blue", "input card must not be mutated"
    assert mutated["header"]["template"] == expected_color
    assert _action_block(mutated) is None, "action block must be removed"
    b3_text = _div_text(mutated, 2)
    assert expected_emoji in b3_text
    assert "ou_responder_alice" in b3_text
    assert option_id in b3_text


def test_mutate_for_answered_with_custom_reason_truncates() -> None:
    card = build_cloud_hitl_card(_make_input(), now=_SAMPLE_NOW)
    long_reason = "z" * 400
    mutated = mutate_card_for_answered(
        card,
        "ou_responder_bob",
        datetime(2026, 5, 8, 11, 50, 0, tzinfo=UTC),
        option_id="custom",
        reason=long_reason,
    )
    b3_text = _div_text(mutated, 2)
    assert "z" * 200 in b3_text
    assert "z" * 201 not in b3_text
    assert "…" in b3_text


# ── (6) S2 — mutate_card_for_pending_second_approver ──────────────────


def test_mutate_for_pending_second_approver_s2() -> None:
    card = build_cloud_hitl_card(
        _make_input(responder_policy="serial_two"), now=_SAMPLE_NOW
    )
    when = datetime(2026, 5, 8, 11, 48, 0, tzinfo=UTC)
    mutated = mutate_card_for_pending_second_approver(
        card,
        "ou_first_approver",
        first_approver_at=when,
    )

    assert mutated is not card
    assert card["header"]["template"] == "blue", "input must not be mutated"
    assert mutated["header"]["template"] == "wathet"
    metadata = mutated["card_metadata"]
    assert metadata["first_approver_open_id"] == "ou_first_approver"
    assert metadata["first_approver_at"] == when.isoformat(timespec="milliseconds")
    assert _action_block(mutated) is not None, (
        "action block must remain so a 2nd approver can finalise"
    )
    b3_text = _div_text(mutated, 2)
    assert "1/2 approved" in b3_text
    assert "ou_first_approver" in b3_text


# ── (7) S3 — mutate_card_for_timeout ───────────────────────────────────


def test_mutate_for_timeout_s3() -> None:
    card = build_cloud_hitl_card(_make_input(), now=_SAMPLE_NOW)
    when = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    mutated = mutate_card_for_timeout(card, timed_out_at=when)

    assert mutated is not card
    assert card["header"]["template"] == "blue", "input must not be mutated"
    assert mutated["header"]["template"] == "grey"
    assert _action_block(mutated) is None, "action block must be removed on timeout"
    b3_text = _div_text(mutated, 2)
    assert "Timed out" in b3_text
    assert "1800" in b3_text  # default timeout seconds rendered in body
    assert b3_text.rstrip().endswith(LARK_FOOTER.strip()), (
        "S3 footer must still carry the workspace-rule footer"
    )


# ── (8) Optional cursor_agent / cursor_run fields render as null ───────


def test_optional_cursor_fields_are_null_in_metadata() -> None:
    card = build_cloud_hitl_card(
        _make_input(cursor_agent_id=None, cursor_run_id=None),
        now=_SAMPLE_NOW,
    )
    metadata = card["card_metadata"]
    assert metadata["cursor_agent_id"] is None
    assert metadata["cursor_run_id"] is None
    serialised = json.dumps(card)
    assert "null" in serialised  # JSON serialisation roundtrips


# ── (9) Expand link uses defaults / overrides correctly ────────────────


def test_expand_link_uses_loopback_default() -> None:
    card = build_cloud_hitl_card(_make_input(), now=_SAMPLE_NOW)
    b2_text = _div_text(card, 1)
    expected = (
        f"http://{DEFAULT_POPOLAD_HOST}:{DEFAULT_POPOLAD_PORT}"
        f"/hitl/cloud/context/{_SAMPLE_HITL_ID}"
    )
    assert expected in b2_text


def test_expand_link_respects_host_override() -> None:
    card = build_cloud_hitl_card(
        _make_input(popolad_host="popolad.internal", popolad_port=12345),
        now=_SAMPLE_NOW,
    )
    b2_text = _div_text(card, 1)
    assert (
        f"http://popolad.internal:12345/hitl/cloud/context/{_SAMPLE_HITL_ID}" in b2_text
    )


# ── (10) Responder policy validation + invalid input rejection ────────


def test_invalid_responder_policy_rejected() -> None:
    with pytest.raises(ValueError, match="responder_policy"):
        _make_input(responder_policy="quorum")


def test_empty_hitl_id_rejected() -> None:
    with pytest.raises(ValueError, match="hitl_id"):
        _make_input(hitl_id="")


def test_negative_timeout_rejected() -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        _make_input(timeout_seconds=0)


def test_naive_datetime_normalised_to_utc_in_metadata() -> None:
    naive = datetime(2026, 5, 8, 12, 0, 0)
    card = build_cloud_hitl_card(_make_input(expiration_at=naive), now=_SAMPLE_NOW)
    iso = card["card_metadata"]["expiration_at"]
    assert isinstance(iso, str)
    assert iso.endswith("+00:00") or iso.endswith("Z")


# ── (11) S2 mutator preserves S1 buttons for the 2nd approver ─────────


def test_s2_then_answered_full_state_chain() -> None:
    card = build_cloud_hitl_card(
        _make_input(responder_policy="serial_two"), now=_SAMPLE_NOW
    )
    pending_second = mutate_card_for_pending_second_approver(
        card,
        "ou_first",
        first_approver_at=datetime(2026, 5, 8, 11, 48, 0, tzinfo=UTC),
    )
    final = mutate_card_for_answered(
        pending_second,
        "ou_second",
        datetime(2026, 5, 8, 11, 55, 0, tzinfo=UTC),
        option_id="approve",
        channel="lark",
    )
    assert final["header"]["template"] == "green"
    assert _action_block(final) is None
    metadata = final["card_metadata"]
    assert metadata["first_approver_open_id"] == "ou_first"
    assert metadata["first_approver_at"] is not None
    b3_text = _div_text(final, 2)
    assert "ou_second" in b3_text


# ── (12) compute_idempotency_key opacity / determinism ─────────────────


def test_compute_idempotency_key_is_deterministic_and_distinct() -> None:
    key_a = compute_idempotency_key(
        task_id="t1", cursor_run_id="r1", question_text="q1"
    )
    key_a_again = compute_idempotency_key(
        task_id="t1", cursor_run_id="r1", question_text="q1"
    )
    key_b = compute_idempotency_key(
        task_id="t1", cursor_run_id="r1", question_text="q2"
    )
    assert key_a == key_a_again, "deterministic given equal inputs"
    assert key_a != key_b, "different question_text → different key"
    assert key_a.startswith("sha256:")
    assert len(key_a) == len("sha256:") + 16


def test_compute_idempotency_key_handles_none_run_id() -> None:
    key = compute_idempotency_key(
        task_id="t1", cursor_run_id=None, question_text="q1"
    )
    key_via_empty = compute_idempotency_key(
        task_id="t1", cursor_run_id="", question_text="q1"
    )
    assert key == key_via_empty


def test_idempotency_key_normalised_in_metadata() -> None:
    raw_64hex = "a" * 64
    card = build_cloud_hitl_card(
        _make_input(idempotency_key=raw_64hex),
        now=_SAMPLE_NOW,
    )
    assert card["card_metadata"]["idempotency_key"] == "sha256:" + ("a" * 16)


def test_idempotency_key_already_prefixed_renormalised() -> None:
    raw = "sha256:" + ("b" * 32)
    card = build_cloud_hitl_card(
        _make_input(idempotency_key=raw),
        now=_SAMPLE_NOW,
    )
    assert card["card_metadata"]["idempotency_key"] == "sha256:" + ("b" * 16)


# ── (13) Title override + remaining-time rendering ─────────────────────


def test_title_override_takes_effect() -> None:
    card = build_cloud_hitl_card(
        _make_input(title_task_id_override="my-custom-display-id"),
        now=_SAMPLE_NOW,
    )
    title = card["header"]["title"]["content"]
    assert title == "PopolaLoom HITL — my-custom-display-id"


def test_b3_rendered_remaining_time_is_present() -> None:
    deadline = _SAMPLE_NOW + timedelta(minutes=15)
    card = build_cloud_hitl_card(
        _make_input(expiration_at=deadline),
        now=_SAMPLE_NOW,
    )
    b3_text = _div_text(card, 2)
    assert "remaining" in b3_text


def test_b3_renders_expired_for_past_deadline() -> None:
    past = _SAMPLE_NOW - timedelta(minutes=5)
    card = build_cloud_hitl_card(
        _make_input(expiration_at=past),
        now=_SAMPLE_NOW,
    )
    b3_text = _div_text(card, 2)
    assert "expired" in b3_text
