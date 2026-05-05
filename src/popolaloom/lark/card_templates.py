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
    "HEADER_COLOR_BY_TERMINAL_TRIGGER",
    "HEADER_COLOR_BY_TRIGGER",
    "LARK_FOOTER",
    "LARK_NOTIFY_PROMPT_TRUNCATE",
    "build_canceled_card",
    "build_cancel_escalated_card",
    "build_card_payload",
    "build_card_send_argv",
    "build_completion_card",
    "build_failure_card",
    "build_skill_missing_card",
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
``violet``, ``purple``, ``indigo``, ``grey``.  v0.3.0 used 5 of the 12
(``blue`` / ``yellow`` / ``red`` / ``purple``); v0.4.1 Stage L1.B
extends the **palette** with ``green`` (task.completed) and
``orange`` (task.cancel_escalated) for the new terminal-event card
builders below — see :data:`HEADER_COLOR_BY_TERMINAL_TRIGGER`."""

_HEADER_TITLE_BY_TRIGGER: dict[HITLTrigger, str] = {
    "info_request": "PopolaLoom · 需要回答",
    "round_floor": "PopolaLoom · 需要确认 (round floor)",
    "approval": "PopolaLoom · 需要审批",
    "destructive_op": "PopolaLoom · 危险操作确认",
    "ambiguous_input": "PopolaLoom · 多候选选择",
}


HEADER_COLOR_BY_TERMINAL_TRIGGER: dict[str, str] = {
    "task.completed": "green",
    "task.failed": "red",
    "task.canceled": "yellow",
    "task.cancel_escalated": "orange",
    "skill.missing": "yellow",
}
"""Per-terminal-trigger header color (v0.4.1 Stage L1.B).

Mirrors :data:`HEADER_COLOR_BY_TRIGGER` shape but keyed by the
NDJSON event type that produced the card (consumed by
:mod:`popolaloom.lark.notifier` in Stage L2).  The 5 keys cover the
proactive-notification taxonomy from research §E.2.3:

- ``task.completed`` → green (success)
- ``task.failed`` → red (failure)
- ``task.canceled`` → yellow (user-initiated cancel, no SIGKILL)
- ``task.cancel_escalated`` → orange (cancel needed SIGKILL escalation)
- ``skill.missing`` → yellow (Skill detection found a gap)
"""


_HEADER_TITLE_BY_TERMINAL_TRIGGER: dict[str, str] = {
    "task.completed": "PopolaLoom · 任务完成",
    "task.failed": "PopolaLoom · 任务失败",
    "task.canceled": "PopolaLoom · 任务已取消",
    "task.cancel_escalated": "PopolaLoom · 取消升级 SIGKILL",
    "skill.missing": "PopolaLoom · Skill 检测缺失",
}


LARK_NOTIFY_PROMPT_TRUNCATE: int = 200
"""Maximum :attr:`prompt_summary` length displayed in terminal cards.

Per research §E.2.4 ``LARK_NOTIFY_PROMPT_TRUNCATE``; over-long prompts
are truncated to this many characters and suffixed with ``…`` so the
card body stays scrollable on mobile and the JSON envelope stays
under Lark's per-message size cap."""


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


def _truncate_prompt(prompt_summary: str, limit: int = LARK_NOTIFY_PROMPT_TRUNCATE) -> str:
    """Truncate ``prompt_summary`` to ``limit`` chars + ``…`` if longer.

    Pure function — does not mutate state, raises only on type errors
    that the caller should already have prevented (No Silent Failures:
    a non-string ``prompt_summary`` is the caller's bug, not ours).
    """
    if not isinstance(prompt_summary, str):
        raise TypeError(
            f"_truncate_prompt: expected str, got {type(prompt_summary).__name__}"
        )
    if len(prompt_summary) <= limit:
        return prompt_summary
    return prompt_summary[:limit] + "…"


def _terminal_card_envelope(
    *,
    trigger: str,
    body_text: str,
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the common card-v2 envelope shared by all 5 terminal builders.

    The body always contains a ``div`` rendered as ``lark_md`` with the
    workspace-rule footer appended (No Silent Failures: missing trigger
    falls back to ``grey`` + ``"PopolaLoom · 通知"``, with explicit log).
    Caller is responsible for pre-formatting ``body_text`` (newlines,
    bullet lists, etc.); this helper only concatenates the footer and
    optionally appends the action div.
    """
    color = HEADER_COLOR_BY_TERMINAL_TRIGGER.get(trigger, "grey")
    title = _HEADER_TITLE_BY_TERMINAL_TRIGGER.get(trigger, "PopolaLoom · 通知")
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": footer_with_origin_note(body_text),
            },
        },
    ]
    if actions:
        elements.append({"tag": "action", "actions": list(actions)})
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
        },
        "body": {"elements": elements},
    }


def _ack_button(task_id: str) -> dict[str, Any]:
    """Build the ``[确认]`` button — used by all 5 terminal cards."""
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "确认"},
        "type": "default",
        "value": {"task_id": task_id, "action": "ack"},
    }


def _view_log_button(task_id: str) -> dict[str, Any]:
    """Build the ``[查看日志]`` button — used by completion + failure cards."""
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "查看日志"},
        "type": "primary",
        "value": {"task_id": task_id, "action": "view_log"},
    }


def build_completion_card(
    task_id: str,
    cli: str,
    prompt_summary: str,
    exit_code: int,
    started_at: str,
    completed_at: str,
    latest_event_index: int,
) -> dict[str, Any]:
    """Build a green ``task.completed`` notification card (v0.4.1 Stage L1.B).

    Header: ``PopolaLoom · 任务完成`` (green).  Body lists task_id /
    cli / truncated prompt / exit_code / started_at / completed_at /
    latest_event_index.  Actions: ``[查看日志]`` + ``[确认]`` buttons
    whose ``value`` is ``{"task_id": ..., "action": "view_log" | "ack"}``.

    Args:
        task_id: popola internal task id (button value payload).
        cli: adapter name (``cursor`` / ``claude`` / ``codex`` / etc.).
        prompt_summary: prompt text — truncated to
            :data:`LARK_NOTIFY_PROMPT_TRUNCATE` chars + ``…`` if longer.
        exit_code: subprocess exit code (always ``0`` on the happy path
            but kept as int so the renderer is total — No Silent Failures
            if a caller passes a wrong value, the human sees it).
        started_at: ISO 8601 dispatch timestamp (callers usually pass
            ``handle.started_at.isoformat(timespec="milliseconds")``).
        completed_at: ISO 8601 terminal-event timestamp.
        latest_event_index: count of NDJSON events on the per-task log
            (so the operator can correlate ``popola tail`` output).

    Returns:
        dict[str, Any]: card v2 JSON ready to ``json.dumps``.
    """
    body_text = (
        f"**Task**: `{task_id}` · **CLI**: `{cli}`\n\n"
        f"**Prompt**: {_truncate_prompt(prompt_summary)}\n\n"
        f"**Exit code**: `{exit_code}`\n"
        f"**Started**: {started_at}\n"
        f"**Completed**: {completed_at}\n"
        f"**Events**: {latest_event_index}"
    )
    actions = [_view_log_button(task_id), _ack_button(task_id)]
    return _terminal_card_envelope(
        trigger="task.completed",
        body_text=body_text,
        actions=actions,
    )


def build_failure_card(
    task_id: str,
    cli: str,
    prompt_summary: str,
    exit_code: int,
    last_stderr_lines: list[str],
    started_at: str,
    failed_at: str,
) -> dict[str, Any]:
    """Build a red ``task.failed`` notification card (v0.4.1 Stage L1.B).

    Header: ``PopolaLoom · 任务失败`` (red).  Body lists task_id / cli /
    truncated prompt / exit_code / started_at / failed_at + a markdown
    code block of ``last_stderr_lines`` (empty list renders as
    ``"(no stderr captured)"``).  Actions: ``[查看日志]`` + ``[确认]``.

    Args:
        task_id: popola internal task id.
        cli: adapter name.
        prompt_summary: prompt text (truncated to 200 chars).
        exit_code: non-zero subprocess exit code.
        last_stderr_lines: tail of stderr (caller can clip to ~10 lines
            before calling; we serialise as a code block as-is).
        started_at: ISO 8601 dispatch timestamp.
        failed_at: ISO 8601 failure timestamp.

    Returns:
        dict[str, Any]: card v2 JSON ready to ``json.dumps``.
    """
    if not isinstance(last_stderr_lines, list):
        raise TypeError(
            f"build_failure_card: last_stderr_lines must be list, "
            f"got {type(last_stderr_lines).__name__}"
        )
    if last_stderr_lines:
        stderr_block = "```\n" + "\n".join(str(line) for line in last_stderr_lines) + "\n```"
    else:
        stderr_block = "_(no stderr captured)_"
    body_text = (
        f"**Task**: `{task_id}` · **CLI**: `{cli}`\n\n"
        f"**Prompt**: {_truncate_prompt(prompt_summary)}\n\n"
        f"**Exit code**: `{exit_code}`\n"
        f"**Started**: {started_at}\n"
        f"**Failed**: {failed_at}\n\n"
        f"**Last stderr**:\n{stderr_block}"
    )
    actions = [_view_log_button(task_id), _ack_button(task_id)]
    return _terminal_card_envelope(
        trigger="task.failed",
        body_text=body_text,
        actions=actions,
    )


def build_canceled_card(
    task_id: str,
    cli: str,
    prompt_summary: str,
    escalated_to_sigkill: bool,
    started_at: str,
    canceled_at: str,
) -> dict[str, Any]:
    """Build a yellow ``task.canceled`` notification card (v0.4.1 Stage L1.B).

    Header: ``PopolaLoom · 任务已取消`` (yellow).  Body lists task_id /
    cli / truncated prompt / escalated_to_sigkill flag / started_at /
    canceled_at.  Actions: 1 × ``[确认]`` button.

    Args:
        task_id: popola internal task id.
        cli: adapter name.
        prompt_summary: prompt text (truncated to 200 chars).
        escalated_to_sigkill: ``True`` iff cancel had to escalate SIGTERM
            → SIGKILL (taken from
            :attr:`TaskHandle.cancel_escalated_to_sigkill`).
        started_at: ISO 8601 dispatch timestamp.
        canceled_at: ISO 8601 cancel timestamp.

    Returns:
        dict[str, Any]: card v2 JSON ready to ``json.dumps``.
    """
    escalated_label = "是 (SIGTERM → SIGKILL)" if escalated_to_sigkill else "否 (SIGTERM only)"
    body_text = (
        f"**Task**: `{task_id}` · **CLI**: `{cli}`\n\n"
        f"**Prompt**: {_truncate_prompt(prompt_summary)}\n\n"
        f"**SIGKILL escalated**: {escalated_label}\n"
        f"**Started**: {started_at}\n"
        f"**Canceled**: {canceled_at}"
    )
    actions = [_ack_button(task_id)]
    return _terminal_card_envelope(
        trigger="task.canceled",
        body_text=body_text,
        actions=actions,
    )


def build_cancel_escalated_card(
    task_id: str,
    cli: str,
    prompt_summary: str,
    exit_code: int,
    sigterm_at: str,
    sigkill_at: str,
) -> dict[str, Any]:
    """Build an orange ``task.cancel_escalated`` notification (v0.4.1 Stage L1.B).

    Header: ``PopolaLoom · 取消升级 SIGKILL`` (orange).  Body lists
    task_id / cli / truncated prompt / exit_code / sigterm_at /
    sigkill_at.  Actions: 1 × ``[确认]`` button.

    Args:
        task_id: popola internal task id.
        cli: adapter name.
        prompt_summary: prompt text (truncated to 200 chars).
        exit_code: subprocess exit code (typically ``-9`` after SIGKILL).
        sigterm_at: ISO 8601 timestamp when SIGTERM was sent.
        sigkill_at: ISO 8601 timestamp when SIGKILL was sent.

    Returns:
        dict[str, Any]: card v2 JSON ready to ``json.dumps``.
    """
    body_text = (
        f"**Task**: `{task_id}` · **CLI**: `{cli}`\n\n"
        f"**Prompt**: {_truncate_prompt(prompt_summary)}\n\n"
        f"**Exit code**: `{exit_code}` (SIGKILL escalation path)\n"
        f"**SIGTERM at**: {sigterm_at}\n"
        f"**SIGKILL at**: {sigkill_at}"
    )
    actions = [_ack_button(task_id)]
    return _terminal_card_envelope(
        trigger="task.cancel_escalated",
        body_text=body_text,
        actions=actions,
    )


def build_skill_missing_card(
    skill_name: str,
    expected_paths: list[str],
    detected_paths: list[str],
) -> dict[str, Any]:
    """Build a yellow ``skill.missing`` notification card (v0.4.1 Stage L1.B).

    Header: ``PopolaLoom · Skill 检测缺失`` (yellow).  Body lists the
    skill name plus 2 markdown bullet lists: expected / detected
    install paths.  Action: 1 × ``[确认]`` button.

    Args:
        skill_name: the skill that's missing (used as the button
            ``value.task_id`` so the listener can route the ack —
            terminal card actions don't have a per-task id; we reuse
            the field for parity with the other 4 builders).
        expected_paths: list of paths the daemon expected to find.
        detected_paths: list of paths actually present on disk.

    Returns:
        dict[str, Any]: card v2 JSON ready to ``json.dumps``.
    """
    if not isinstance(expected_paths, list) or not isinstance(detected_paths, list):
        raise TypeError(
            "build_skill_missing_card: expected_paths and detected_paths "
            "must be list[str]"
        )

    def _format_paths(paths: list[str]) -> str:
        if not paths:
            return "_(none)_"
        return "\n".join(f"- `{p}`" for p in paths)

    body_text = (
        f"**Skill**: `{skill_name}`\n\n"
        f"**Expected paths** ({len(expected_paths)}):\n"
        f"{_format_paths(expected_paths)}\n\n"
        f"**Detected paths** ({len(detected_paths)}):\n"
        f"{_format_paths(detected_paths)}"
    )
    actions = [_ack_button(skill_name)]
    return _terminal_card_envelope(
        trigger="skill.missing",
        body_text=body_text,
        actions=actions,
    )


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
