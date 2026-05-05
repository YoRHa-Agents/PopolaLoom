"""Tier 2 — HITL 5-renderer roundtrip tests (v0.3.0 F4.B).

Per AC #2 of the v0.3.0 task spec (≥10 cases combined): each of 5
renderers (lark / ide / cli / mcp / web) gets a happy-path render +
a sad-path / parse-failure case.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

from popolaloom.hitl import HITLOption, HITLPrompt, HITLReply
from popolaloom.hitl.renderers import cli, ide, lark, mcp, web


def _make_prompt(prompt_id: str = "hitl-test-1") -> HITLPrompt:
    return HITLPrompt(
        trigger="approval",
        why="Auto-merge will rewrite history",
        what="Approve the merge?",
        options=[
            HITLOption(id="yes", label="Approve"),
            HITLOption(id="no", label="Block"),
        ],
        default_option_id="no",
        channels=["lark", "ide", "cli", "mcp", "web"],
        deadline_seconds=3600,
        prompt_id=prompt_id,
    )


# ── lark renderer ───────────────────────────────────────────────────────


def test_lark_renderer_render_card_happy() -> None:
    prompt = _make_prompt("hitl-lark-1")
    card = lark.render_lark_card(prompt)
    assert card["schema"] == "2.0"
    body = json.dumps(card, ensure_ascii=False)
    assert "本消息由飞书工具 Lark-Cli 发送" in body


def test_lark_renderer_parse_card_action_event() -> None:
    event = {
        "header": {"event_type": "card.action.trigger_v1", "event_id": "ev-1"},
        "event": {
            "operator": {"open_id": "ou_responder"},
            "action": {
                "tag": "button",
                "value": {"hitl_id": "hitl-lark-1", "option_id": "yes"},
            },
        },
    }
    reply = lark.parse_reply(event)
    assert reply is not None
    assert reply.hitl_id == "hitl-lark-1"
    assert reply.option_id == "yes"
    assert reply.via == "lark"


def test_lark_renderer_returns_none_on_unknown_event_type() -> None:
    event = {"header": {"event_type": "unrelated.event_v1"}, "event": {}}
    assert lark.parse_reply(event) is None


def test_lark_renderer_parse_drops_event_with_no_value() -> None:
    event = {
        "header": {"event_type": "card.action.trigger_v1"},
        "event": {"operator": {"open_id": "ou_x"}, "action": {"tag": "button"}},
    }
    assert lark.parse_reply(event) is None


# ── ide renderer ───────────────────────────────────────────────────────


def test_ide_renderer_render_message_happy() -> None:
    prompt = _make_prompt("hitl-ide-1")
    msg = ide.render_ide_notify(prompt)
    assert msg.title.startswith("PopolaLoom")
    assert "popola feedback hitl-ide-1" in msg.cli_command
    assert msg.urgency in {"low", "normal", "critical"}


def test_ide_renderer_parse_reply_happy() -> None:
    payload = {
        "hitl_id": "hitl-ide-1",
        "option_id": "yes",
        "reason": "looks good",
        "responder": "alice",
    }
    reply = ide.parse_reply(payload)
    assert reply is not None
    assert reply.via == "ide"
    assert reply.responder == "alice"


def test_ide_renderer_parse_reply_returns_none_on_missing() -> None:
    assert ide.parse_reply({"hitl_id": "h"}) is None


# ── cli renderer ───────────────────────────────────────────────────────


def test_cli_renderer_render_pending_text_empty() -> None:
    output = cli.render_pending_text([])
    assert "no pending" in output


def test_cli_renderer_render_pending_text_has_rows() -> None:
    p = _make_prompt("hitl-cli-1")
    output = cli.render_pending_text([p])
    assert "hitl-cli-1" in output
    assert "approval" in output


def test_cli_renderer_parse_reply_happy() -> None:
    reply = cli.parse_reply("hitl-cli-1", "yes", reason="lgtm")
    assert isinstance(reply, HITLReply)
    assert reply.via == "cli"
    assert reply.reason == "lgtm"


def test_cli_renderer_parse_reply_blank_id_raises() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        cli.parse_reply("", "yes")


# ── mcp renderer ───────────────────────────────────────────────────────


def test_mcp_renderer_render_elicitation_happy() -> None:
    prompt = _make_prompt("hitl-mcp-1")
    payload = mcp.render_mcp_elicitation(prompt)
    assert isinstance(payload, dict)
    serialised = json.dumps(payload, default=str)
    assert "hitl-mcp-1" in serialised


def test_mcp_renderer_parse_reply_happy() -> None:
    response = {"hitl_id": "hitl-mcp-1", "choice": "yes", "reason": "ok"}
    reply = mcp.parse_reply(response)
    assert reply is not None
    assert reply.via == "mcp"
    assert reply.option_id == "yes"


def test_mcp_renderer_parse_reply_returns_none_on_missing() -> None:
    assert mcp.parse_reply({"hitl_id": "h"}) is None


def test_mcp_renderer_parse_reply_raises_on_non_dict() -> None:
    with pytest.raises(TypeError):
        mcp.parse_reply([])  # type: ignore[arg-type]


# ── web renderer ───────────────────────────────────────────────────────


def test_web_renderer_render_form_contains_inputs() -> None:
    prompt = _make_prompt("hitl-web-1")
    html_str = web.render_web_form(prompt)
    assert 'name="hitl_id"' in html_str
    assert 'value="hitl-web-1"' in html_str
    # XSS hardened — does not contain raw `<` from why/what
    assert "<script" not in html_str.lower()


def test_web_renderer_parse_reply_happy() -> None:
    body_str = "hitl_id=hitl-web-1&option_id=yes&reason=lgtm"
    parsed = urllib.parse.parse_qs(body_str)
    reply = web.parse_reply(parsed)
    assert reply is not None
    assert reply.via == "web"
    assert reply.option_id == "yes"


def test_web_renderer_parse_reply_returns_none_on_missing() -> None:
    assert web.parse_reply({"hitl_id": ["h"]}) is None
