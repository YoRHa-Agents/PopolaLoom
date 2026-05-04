"""Tier 1 — Lark card template tests (v0.3.0 F4.D §12.8.1).

Per v0.3.0-plan §4 Stage F4.6 + AC #3 of the v0.3.0 task spec
+ DoD 6 (footer 100% coverage).

≥ 15 cases asserting:

- footer ``\\n---\\n本消息由飞书工具 Lark-Cli 发送`` is present in every
  rendered card body (workspace rule "lark-cli 写入操作须追加来源标注")
- header colour map matches trigger severity table
- button.value JSON contains hitl_id + option_id
- argv builder emits ``lark-cli im +send --as bot --target-id ...
  --card '<json>' --metadata-key hitl_id=...``
"""

from __future__ import annotations

import json

import pytest

from popolaloom.hitl import HITLOption, HITLPrompt
from popolaloom.lark.card_templates import (
    HEADER_COLOR_BY_TRIGGER,
    LARK_FOOTER,
    build_card_payload,
    build_card_send_argv,
    extract_action_value,
    extract_button_value,
    footer_with_origin_note,
)


def _approval_prompt(prompt_id: str = "hitl-aaaa") -> HITLPrompt:
    p = HITLPrompt(
        trigger="approval",
        why="Auto-merge will rewrite history",
        what="Confirm rebase + force-push",
        options=[
            HITLOption(id="yes", label="Approve"),
            HITLOption(id="no", label="Block", default=True),
        ],
        default_option_id="no",
        channels=["lark", "ide", "cli"],
        deadline_seconds=3600,
        prompt_id=prompt_id,
    )
    return p


def _round_floor_prompt(prompt_id: str = "hitl-bbbb") -> HITLPrompt:
    return HITLPrompt(
        trigger="round_floor",
        why="Round 2 missed gate floor",
        what="Pick override / rollback / defer",
        options=[
            HITLOption(id="override", label="Override"),
            HITLOption(id="rollback", label="Rollback"),
            HITLOption(id="defer", label="Defer", default=True),
        ],
        default_option_id="defer",
        channels=["lark", "ide", "cli"],
        deadline_seconds=86400,
        prompt_id=prompt_id,
    )


# ── Footer presence (workspace rule) ────────────────────────────────────


def test_footer_value_contains_chinese_origin() -> None:
    assert "本消息由飞书工具 Lark-Cli 发送" in LARK_FOOTER
    assert "---" in LARK_FOOTER


def test_footer_with_origin_note_appends_when_missing() -> None:
    out = footer_with_origin_note("Hello")
    assert out.endswith(LARK_FOOTER)


def test_footer_with_origin_note_idempotent() -> None:
    once = footer_with_origin_note("Hello")
    twice = footer_with_origin_note(once)
    assert once == twice


def test_card_body_contains_footer_for_approval() -> None:
    card = build_card_payload(_approval_prompt())
    serialised = json.dumps(card, ensure_ascii=False)
    assert "本消息由飞书工具 Lark-Cli 发送" in serialised


def test_card_body_contains_footer_for_round_floor() -> None:
    card = build_card_payload(_round_floor_prompt())
    serialised = json.dumps(card, ensure_ascii=False)
    assert "本消息由飞书工具 Lark-Cli 发送" in serialised


# ── Header colour map ───────────────────────────────────────────────────


def test_header_colour_map_covers_all_triggers() -> None:
    assert set(HEADER_COLOR_BY_TRIGGER.keys()) >= {
        "info_request", "round_floor", "approval",
        "destructive_op", "ambiguous_input",
    }


@pytest.mark.parametrize(
    "trigger, expected_color",
    [
        ("info_request", "blue"),
        ("round_floor", "yellow"),
        ("approval", "yellow"),
        ("destructive_op", "red"),
        ("ambiguous_input", "purple"),
    ],
)
def test_card_uses_correct_header_colour(trigger, expected_color) -> None:
    p = HITLPrompt(
        trigger=trigger,
        why="why",
        what="what",
        options=[
            HITLOption(id="a", label="A"),
            HITLOption(id="b", label="B"),
        ],
        default_option_id="a",
        channels=["lark", "ide"],
        deadline_seconds=3600,
        prompt_id="hitl-xxx",
    )
    card = build_card_payload(p)
    assert card["header"]["template"] == expected_color


# ── Button value (round-trip) ───────────────────────────────────────────


def test_button_value_round_trip_dict() -> None:
    card = build_card_payload(_approval_prompt(prompt_id="hitl-cccc"))
    actions = card["body"]["elements"][1]["actions"]
    assert len(actions) == 2
    for btn in actions:
        hitl_id, option_id = extract_button_value(btn["value"])
        assert hitl_id == "hitl-cccc"
        assert option_id in {"yes", "no"}


def test_extract_action_value_strict_raises_on_bad_input() -> None:
    with pytest.raises(ValueError):
        extract_action_value("not-json")


def test_extract_button_value_returns_none_on_bad_input() -> None:
    a, b = extract_button_value(123)  # type: ignore[arg-type]
    assert (a, b) == (None, None)


def test_extract_button_value_handles_string_json() -> None:
    raw = json.dumps({"hitl_id": "h", "option_id": "y"})
    a, b = extract_button_value(raw)
    assert a == "h" and b == "y"


# ── Argv builder ────────────────────────────────────────────────────────


def test_build_card_send_argv_basic_shape() -> None:
    argv = build_card_send_argv(_approval_prompt(prompt_id="hitl-dddd"), "ou_target_xxx")
    assert argv[0] == "lark-cli"
    assert argv[1:3] == ["im", "+send"]
    assert "--as" in argv and argv[argv.index("--as") + 1] == "bot"
    assert "--target-id" in argv
    assert argv[argv.index("--target-id") + 1] == "ou_target_xxx"
    assert "--card" in argv
    assert "--metadata-key" in argv


def test_build_card_send_argv_metadata_includes_hitl_id() -> None:
    argv = build_card_send_argv(_round_floor_prompt(prompt_id="hitl-eeee"), "ou_x")
    metadata_idx = argv.index("--metadata-key")
    metadata_value = argv[metadata_idx + 1]
    assert metadata_value == "hitl_id=hitl-eeee"


def test_card_payload_contains_hitl_id_in_button_values() -> None:
    p = _round_floor_prompt(prompt_id="hitl-ffff")
    card = build_card_payload(p)
    actions = card["body"]["elements"][1]["actions"]
    for btn in actions:
        assert btn["value"]["hitl_id"] == "hitl-ffff"


# ── Schema fields ───────────────────────────────────────────────────────


def test_card_has_v2_schema_marker() -> None:
    card = build_card_payload(_approval_prompt(prompt_id="hitl-gggg"))
    assert card.get("schema") == "2.0"


def test_card_body_has_action_block_with_buttons() -> None:
    card = build_card_payload(_approval_prompt(prompt_id="hitl-hhhh"))
    elements = card["body"]["elements"]
    action_blocks = [e for e in elements if e.get("tag") == "action"]
    assert len(action_blocks) == 1
    assert all(a["tag"] == "button" for a in action_blocks[0]["actions"])


def test_card_body_displays_why_and_what() -> None:
    p = _approval_prompt()
    card = build_card_payload(p)
    body_text = json.dumps(card, ensure_ascii=False)
    assert p.why in body_text
    assert p.what in body_text


def test_card_artifacts_appear_in_body() -> None:
    from popolaloom.hitl import ArtifactRef
    p = HITLPrompt(
        trigger="approval",
        why="why",
        what="what",
        options=[HITLOption(id="a", label="A"), HITLOption(id="b", label="B")],
        default_option_id="a",
        channels=["lark", "ide"],
        deadline_seconds=3600,
        prompt_id="hitl-iiii",
        artifacts=[ArtifactRef(type="diff", uri="patch://abc", label="The diff")],
    )
    card = build_card_payload(p)
    body_text = json.dumps(card, ensure_ascii=False)
    assert "patch://abc" in body_text
