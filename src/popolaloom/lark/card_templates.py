"""Lark interactive card v2 JSON builders (v0.3.0 F4.D §12.8.1).

Per roadmap §12.8.1 + v0.3.0-plan §4 Stage F4.6 + workspace rule
"lark-cli 写入操作须追加来源标注".

Each ``HITLPrompt`` is rendered as an interactive card v2:

- header: title = trigger label; color depends on trigger severity
  (T1=blue, T2=yellow, T3=red, T4=purple, T5=orange).
- div: rendered why (markdown).
- div: rendered what (markdown) with options summary.
- action: ``button`` for each option, ``button.value`` carries the
  JSON payload ``{"hitl_id": "...", "option_id": "..."}`` (parsed
  back by :func:`popolaloom.lark.listener.parse_card_action`).
- footer note: workspace rule footer ``---\\n本消息由飞书工具 Lark-Cli 发送``.

The output is plain Python dict; serialise with
:func:`json.dumps` before passing to ``lark-cli im +send --card``.

Note: lark-cli accepts cards via ``--card '<json>'`` flag.  We also
provide :func:`build_card_send_argv` as the canonical argv builder
so the daemon never has to hand-craft the command line.
"""

from __future__ import annotations

import json
from typing import Any

from popolaloom.hitl import HITLPrompt, HITLTrigger

__all__ = [
    "HEADER_COLOR_BY_TRIGGER",
    "LARK_FOOTER",
    "build_card_payload",
    "build_card_send_argv",
    "extract_action_value",
    "extract_button_value",
    "footer_with_origin_note",
]

LARK_FOOTER: str = "\n---\n本消息由飞书工具 Lark-Cli 发送"
"""Workspace rule footer — appended to every outbound card body.

The text is a 标注 行 ("source annotation line") required by the
project workspace rule "lark-cli 写入操作须追加来源标注".  Every
``lark-cli im +send`` produced by this package MUST include this
footer; tests assert it on every generated card."""

HEADER_COLOR_BY_TRIGGER: dict[HITLTrigger, str] = {
    "info_request": "blue",
    "round_floor": "yellow",
    "approval": "yellow",
    "destructive_op": "red",
    "ambiguous_input": "purple",
}
"""Per-trigger header color (per roadmap §12.8.1).

The 5 base trigger types map to severity colors so the human can
prioritise at a glance.  Lark v2 cards support: ``blue``, ``wathet``,
``turquoise``, ``green``, ``yellow``, ``orange``, ``red``, ``carmine``,
``violet``, ``purple``, ``indigo``, ``grey`` (we use 5 of the 12)."""

_HEADER_TITLE_BY_TRIGGER: dict[HITLTrigger, str] = {
    "info_request": "PopolaLoom · 需要回答",
    "round_floor": "PopolaLoom · 需要确认 (round floor)",
    "approval": "PopolaLoom · 需要审批",
    "destructive_op": "PopolaLoom · 危险操作确认",
    "ambiguous_input": "PopolaLoom · 多候选选择",
}


def footer_with_origin_note(body: str) -> str:
    """Append the workspace-rule footer to ``body`` (idempotent).

    If ``body`` already ends with the footer, return as-is — keeps
    the function safe to call multiple times in a render pipeline.
    """
    if body.rstrip().endswith(LARK_FOOTER.strip()):
        return body
    return body + LARK_FOOTER


def _format_options_summary(prompt: HITLPrompt) -> str:
    """Render a markdown bullet list of option labels."""
    lines = []
    for opt in prompt.options:
        marker = "→" if opt.id == prompt.default_option_id else "•"
        lines.append(f"{marker} **{opt.label}** (`{opt.id}`)")
    return "\n".join(lines)


def _format_artifacts_block(prompt: HITLPrompt) -> str:
    """Render a markdown list of attached artifacts (or empty string)."""
    if not prompt.artifacts:
        return ""
    rows = ["", "**附件**:"]
    for art in prompt.artifacts:
        label = art.label or art.type
        rows.append(f"- {label}: `{art.uri}`")
    return "\n".join(rows)


def build_card_payload(prompt: HITLPrompt) -> dict[str, Any]:
    """Build a Lark interactive card v2 JSON dict for ``prompt``.

    The button.value JSON for every option contains the prompt id +
    option id so the listener can route the click to the right
    HITL row::

        {"hitl_id": "<uuid>", "option_id": "<id>"}

    Args:
        prompt: a fully-validated :class:`HITLPrompt`. Must have
            :attr:`HITLPrompt.prompt_id` set (use
            :meth:`HITLPrompt.ensure_prompt_id`).

    Returns:
        dict[str, Any]: card v2 JSON ready to ``json.dumps`` and pass
        to ``lark-cli im +send --card``.
    """
    hitl_id = prompt.ensure_prompt_id()
    color = HEADER_COLOR_BY_TRIGGER.get(prompt.trigger, "blue")
    title = _HEADER_TITLE_BY_TRIGGER.get(prompt.trigger, "PopolaLoom · HITL")

    options_md = _format_options_summary(prompt)
    artifacts_md = _format_artifacts_block(prompt)

    body_text = (
        f"**Why**: {prompt.why}\n\n"
        f"**What**: {prompt.what}\n\n"
        f"**选项**:\n{options_md}"
        f"{artifacts_md}\n\n"
        f"⏰ 截止: {prompt.deadline_seconds // 3600}h "
        f"{(prompt.deadline_seconds % 3600) // 60}m"
    )
    body_text = footer_with_origin_note(body_text)

    actions_block = []
    for opt in prompt.options:
        button_type = "primary" if opt.id == prompt.default_option_id else "default"
        actions_block.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": opt.label},
                "type": button_type,
                "value": {
                    "hitl_id": hitl_id,
                    "option_id": opt.id,
                },
            }
        )

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
        },
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": body_text},
                },
                {
                    "tag": "action",
                    "actions": actions_block,
                },
            ]
        },
    }


def extract_button_value(value: dict[str, Any] | str) -> tuple[str | None, str | None]:
    """Parse a Lark card.action button value back into ``(hitl_id, option_id)``.

    Lark may deliver ``value`` as a dict (parsed) or a JSON string;
    handle both.  Returns ``(None, None)`` on parse failure (caller
    decides whether to log + ignore or escalate).
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None, None
    if not isinstance(value, dict):
        return None, None
    hitl_id = value.get("hitl_id")
    option_id = value.get("option_id")
    if not isinstance(hitl_id, str) or not isinstance(option_id, str):
        return None, None
    return hitl_id, option_id


def extract_action_value(value: dict[str, Any] | str) -> tuple[str, str]:
    """Strict variant of :func:`extract_button_value` (raises on bad input).

    Used by :class:`popolaloom.lark.listener.LarkListener._handle_card_action`
    where a malformed value should bubble up as a parse error (counted in
    ``_state.parse_errors``).

    Returns:
        tuple[str, str]: ``(hitl_id, option_id)``.

    Raises:
        ValueError: when the value cannot be decoded into the expected pair.
    """
    hitl_id, option_id = extract_button_value(value)
    if hitl_id is None or option_id is None:
        raise ValueError(f"extract_action_value: malformed button value: {value!r}")
    return hitl_id, option_id


def build_card_send_argv(
    prompt: HITLPrompt,
    target_open_id: str,
    *,
    card_payload: dict[str, Any] | None = None,
) -> list[str]:
    """Build the ``lark-cli im +send`` argv list for ``prompt``.

    Per workspace rule the argv embeds ``--metadata-key hitl_id=<uuid>``
    so the listener can look up the row even if the card_action event
    payload loses the id.

    Args:
        prompt: HITLPrompt envelope.
        target_open_id: Lark user open_id receiver.
        card_payload: optional pre-built payload (defaults to
            :func:`build_card_payload`).

    Returns:
        list[str]: argv ready for :func:`subprocess.run` / Popen.
    """
    hitl_id = prompt.ensure_prompt_id()
    payload = card_payload or build_card_payload(prompt)
    return [
        "lark-cli",
        "im",
        "+send",
        "--as", "bot",
        "--target-id", target_open_id,
        "--card", json.dumps(payload, ensure_ascii=False),
        "--metadata-key", f"hitl_id={hitl_id}",
    ]
