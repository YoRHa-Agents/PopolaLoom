"""Tier 1 — HITL renderer branch coverage gap-fillers (v0.3.0 F4 polish).

Targeted unit tests for code paths in
:mod:`popolaloom.hitl.renderers.cli` and
:mod:`popolaloom.hitl.renderers.lark` not yet exercised by the
default lane.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from popolaloom.hitl import HITLOption, HITLPrompt
from popolaloom.hitl.renderers import cli, lark
from popolaloom.hitl.renderers.cli import (
    _coerce_to_prompt,
    deadline_remaining_human,
    render_pending_table,
)
from popolaloom.hitl.renderers.lark import (
    LarkSendResult,
    _extract_message_id,
)


def _new_prompt() -> HITLPrompt:
    return HITLPrompt(
        trigger="approval",
        why="x",
        what="y",
        options=[
            HITLOption(id="yes", label="Yes"),
            HITLOption(id="no", label="No", default=True),
        ],
        default_option_id="no",
        channels=["lark", "ide"],
        deadline_seconds=3600,
    )


def test_coerce_to_prompt_rejects_non_dict_non_prompt() -> None:
    with pytest.raises(ValueError, match="HITLPrompt or dict"):
        _coerce_to_prompt(42)  # type: ignore[arg-type]


def test_coerce_to_prompt_rejects_dict_missing_prompt_json() -> None:
    with pytest.raises(ValueError, match="prompt_json"):
        _coerce_to_prompt({"hitl_id": "abc"})


def test_render_pending_table_empty_input() -> None:
    table = render_pending_table([])
    assert table is not None


def test_deadline_remaining_human_invalid_iso() -> None:
    """Invalid ISO returns the input string unchanged (no raise)."""
    assert deadline_remaining_human("not an iso date") == "not an iso date"


def test_cli_parse_reply_blank_hitl_id_raises() -> None:
    with pytest.raises(ValueError, match="hitl_id"):
        cli.parse_reply("   ", "yes")


def test_cli_parse_reply_blank_option_id_raises() -> None:
    with pytest.raises(ValueError, match="option_id"):
        cli.parse_reply("hitl-x", "")


def test_cli_parse_reply_default_via_is_cli() -> None:
    reply = cli.parse_reply("hitl-x", "yes")
    assert reply.via == "cli"


def test_cli_parse_reply_via_can_be_overridden_to_ide() -> None:
    """The CLI renderer is also used by IDE notify reply path."""
    reply = cli.parse_reply("hitl-x", "yes", via="ide")
    assert reply.via == "ide"


def test_lark_send_result_truncates_long_stdout() -> None:
    result = LarkSendResult(
        ok=True,
        message_id="om_1",
        attempts=1,
        stdout="x" * 5000,
        stderr="",
    )
    assert len(result.stdout) <= 2048


def test_lark_extract_message_id_with_data_envelope() -> None:
    output = '{"data": {"message_id": "om_inside_data"}}'
    assert _extract_message_id(output) == "om_inside_data"


def test_lark_extract_message_id_with_id_field() -> None:
    output = '{"id": "om_id_field"}'
    assert _extract_message_id(output) == "om_id_field"


def test_lark_extract_message_id_falls_back_to_stdout_prefix() -> None:
    output = "no json here just text"
    assert _extract_message_id(output)[:32] == "no json here just text"[:32]


def test_lark_render_card_with_unset_prompt_id_auto_generates() -> None:
    prompt = _new_prompt()
    prompt.prompt_id = None
    card = lark.render_lark_card(prompt)
    assert prompt.prompt_id is not None  # ensure_prompt_id triggered
    assert "本消息由飞书工具" in str(card)


def test_lark_send_card_skips_when_no_target() -> None:
    prompt = _new_prompt()
    runner = MagicMock(side_effect=AssertionError("should not be called"))
    result = lark.send_lark_card(prompt, target_open_id=None, runner=runner)
    assert result.ok is False
    assert "LARK_HITL_TARGET_OPEN_ID" in (result.error or "")
