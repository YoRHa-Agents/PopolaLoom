"""v0.4.1 Stage L1.B — terminal-event card builder tests.

Per the v0.4.1 task spec L1.D #1 (~ 10 cases): one happy-path test
per new builder + edge cases (very long ``prompt_summary``, empty
``last_stderr_lines``, ``escalated_to_sigkill=True``, empty path
lists for ``build_skill_missing_card``).

Each case asserts:

- ``header.template`` is the expected color from
  :data:`HEADER_COLOR_BY_TERMINAL_TRIGGER`.
- ``header.title.tag == "plain_text"``.
- ``body.elements`` has at least one ``div`` element.
- The last ``div`` element body text ends with :data:`LARK_FOOTER`
  (workspace rule "lark-cli 写入操作须追加来源标注").
- The button ``value`` JSON matches the expected
  ``{"task_id": ..., "action": ...}`` shape.

These tests live in the default lane (no ``slow`` / ``nightly`` /
``real_lark`` markers) per the v0.4.1 acceptance criteria — they are
pure-function tests with no IO and no subprocess.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from popolaloom.lark.card_templates import (
    HEADER_COLOR_BY_TERMINAL_TRIGGER,
    LARK_FOOTER,
    LARK_NOTIFY_PROMPT_TRUNCATE,
    build_cancel_escalated_card,
    build_canceled_card,
    build_completion_card,
    build_failure_card,
    build_skill_missing_card,
)


def _div_elements(card: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the list of ``tag == "div"`` elements in ``card.body.elements``."""
    elements = card["body"]["elements"]
    return [e for e in elements if e.get("tag") == "div"]


def _action_block(card: dict[str, Any]) -> dict[str, Any] | None:
    """Return the ``action`` block (or ``None`` if absent)."""
    elements = card["body"]["elements"]
    for e in elements:
        if e.get("tag") == "action":
            return e
    return None


def _last_div_text(card: dict[str, Any]) -> str:
    divs = _div_elements(card)
    assert divs, "card body must have at least one div element"
    return divs[-1]["text"]["content"]


def _assert_card_envelope_shape(card: dict[str, Any], expected_color: str) -> None:
    """Reusable shape assertions per the v0.4.1 L1.D acceptance contract."""
    assert card["schema"] == "2.0"
    assert card["header"]["template"] == expected_color
    assert card["header"]["title"]["tag"] == "plain_text"
    assert isinstance(card["header"]["title"]["content"], str)
    assert card["header"]["title"]["content"].startswith("PopolaLoom · ")
    divs = _div_elements(card)
    assert len(divs) >= 1, "card must have at least one div element"
    assert _last_div_text(card).rstrip().endswith(LARK_FOOTER.strip())


# ── 1: build_completion_card happy path ─────────────────────────────────


def test_build_completion_card_happy_path() -> None:
    card = build_completion_card(
        task_id="cursor-abc123",
        cli="cursor",
        prompt_summary="Refactor utils.py",
        exit_code=0,
        started_at="2026-05-05T07:00:00.000+00:00",
        completed_at="2026-05-05T07:01:23.456+00:00",
        latest_event_index=42,
    )
    _assert_card_envelope_shape(
        card, HEADER_COLOR_BY_TERMINAL_TRIGGER["task.completed"]
    )
    assert card["header"]["template"] == "green"
    assert "任务完成" in card["header"]["title"]["content"]

    body = _last_div_text(card)
    assert "cursor-abc123" in body
    assert "Refactor utils.py" in body
    assert "Exit code" in body
    assert "42" in body  # latest_event_index

    actions = _action_block(card)
    assert actions is not None
    assert len(actions["actions"]) == 2
    btn_actions = [a["value"]["action"] for a in actions["actions"]]
    assert btn_actions == ["view_log", "ack"]
    for btn in actions["actions"]:
        assert btn["value"]["task_id"] == "cursor-abc123"


# ── 2: build_completion_card truncates very-long prompt_summary ─────────


def test_build_completion_card_truncates_long_prompt() -> None:
    long_prompt = "x" * (LARK_NOTIFY_PROMPT_TRUNCATE + 50)
    card = build_completion_card(
        task_id="t1",
        cli="claude",
        prompt_summary=long_prompt,
        exit_code=0,
        started_at="2026-05-05T07:00:00.000Z",
        completed_at="2026-05-05T07:00:01.000Z",
        latest_event_index=1,
    )
    body = _last_div_text(card)
    truncated_marker = "x" * LARK_NOTIFY_PROMPT_TRUNCATE + "…"
    assert truncated_marker in body
    assert long_prompt not in body
    assert body.rstrip().endswith(LARK_FOOTER.strip())


# ── 3: build_failure_card happy path with stderr lines ──────────────────


def test_build_failure_card_happy_path_with_stderr() -> None:
    card = build_failure_card(
        task_id="claude-fff",
        cli="claude",
        prompt_summary="Run pytest",
        exit_code=1,
        last_stderr_lines=[
            "TypeError: 'NoneType' object",
            "  at line 42",
            "  in module foo",
        ],
        started_at="2026-05-05T07:00:00.000Z",
        failed_at="2026-05-05T07:00:30.000Z",
    )
    _assert_card_envelope_shape(card, HEADER_COLOR_BY_TERMINAL_TRIGGER["task.failed"])
    assert card["header"]["template"] == "red"
    assert "任务失败" in card["header"]["title"]["content"]

    body = _last_div_text(card)
    assert "claude-fff" in body
    assert "TypeError" in body
    assert "```" in body  # markdown code block fence

    actions = _action_block(card)
    assert actions is not None
    assert len(actions["actions"]) == 2
    btn_actions = [a["value"]["action"] for a in actions["actions"]]
    assert btn_actions == ["view_log", "ack"]


# ── 4: build_failure_card edge case — empty stderr lines ────────────────


def test_build_failure_card_empty_stderr_renders_placeholder() -> None:
    card = build_failure_card(
        task_id="t-empty",
        cli="codex",
        prompt_summary="Some prompt",
        exit_code=137,
        last_stderr_lines=[],
        started_at="2026-05-05T07:00:00.000Z",
        failed_at="2026-05-05T07:00:01.000Z",
    )
    body = _last_div_text(card)
    assert "(no stderr captured)" in body
    assert "```" not in body  # no fence when empty


# ── 5: build_failure_card invalid stderr type → TypeError (No Silent Fail) ─


def test_build_failure_card_rejects_non_list_stderr() -> None:
    with pytest.raises(TypeError, match="last_stderr_lines"):
        build_failure_card(
            task_id="t",
            cli="cursor",
            prompt_summary="p",
            exit_code=1,
            last_stderr_lines="oops not a list",  # type: ignore[arg-type]
            started_at="t1",
            failed_at="t2",
        )


# ── 6: build_canceled_card happy path with sigkill_escalated=False ──────


def test_build_canceled_card_happy_path_no_escalation() -> None:
    card = build_canceled_card(
        task_id="cursor-cccc",
        cli="cursor",
        prompt_summary="Long-running task",
        escalated_to_sigkill=False,
        started_at="2026-05-05T07:00:00.000Z",
        canceled_at="2026-05-05T07:01:00.000Z",
    )
    _assert_card_envelope_shape(card, HEADER_COLOR_BY_TERMINAL_TRIGGER["task.canceled"])
    assert card["header"]["template"] == "yellow"
    assert "任务已取消" in card["header"]["title"]["content"]

    body = _last_div_text(card)
    assert "cursor-cccc" in body
    assert "SIGTERM only" in body  # escalated_to_sigkill=False label

    actions = _action_block(card)
    assert actions is not None
    assert len(actions["actions"]) == 1
    assert actions["actions"][0]["value"] == {
        "task_id": "cursor-cccc",
        "action": "ack",
    }


# ── 7: build_canceled_card edge case — escalated_to_sigkill=True ────────


def test_build_canceled_card_with_escalation_label() -> None:
    card = build_canceled_card(
        task_id="t-esc",
        cli="claude",
        prompt_summary="Stubborn process",
        escalated_to_sigkill=True,
        started_at="t1",
        canceled_at="t2",
    )
    body = _last_div_text(card)
    assert "SIGTERM → SIGKILL" in body
    actions = _action_block(card)
    assert actions is not None
    assert len(actions["actions"]) == 1


# ── 8: build_cancel_escalated_card happy path ───────────────────────────


def test_build_cancel_escalated_card_happy_path() -> None:
    card = build_cancel_escalated_card(
        task_id="codex-eee",
        cli="codex",
        prompt_summary="Tight loop",
        exit_code=-9,
        sigterm_at="2026-05-05T07:00:00.000Z",
        sigkill_at="2026-05-05T07:00:05.000Z",
    )
    _assert_card_envelope_shape(
        card, HEADER_COLOR_BY_TERMINAL_TRIGGER["task.cancel_escalated"]
    )
    assert card["header"]["template"] == "orange"
    assert "取消升级 SIGKILL" in card["header"]["title"]["content"]

    body = _last_div_text(card)
    assert "codex-eee" in body
    assert "-9" in body
    assert "SIGTERM at" in body
    assert "SIGKILL at" in body

    actions = _action_block(card)
    assert actions is not None
    assert len(actions["actions"]) == 1
    assert actions["actions"][0]["value"] == {
        "task_id": "codex-eee",
        "action": "ack",
    }


# ── 9: build_skill_missing_card happy path ──────────────────────────────


def test_build_skill_missing_card_happy_path() -> None:
    card = build_skill_missing_card(
        skill_name="popolaloom",
        expected_paths=["~/.cursor/skills/popolaloom/SKILL.md"],
        detected_paths=[],
    )
    _assert_card_envelope_shape(card, HEADER_COLOR_BY_TERMINAL_TRIGGER["skill.missing"])
    assert card["header"]["template"] == "yellow"
    assert "Skill 检测缺失" in card["header"]["title"]["content"]

    body = _last_div_text(card)
    assert "popolaloom" in body
    assert "Expected paths" in body and "(1)" in body
    assert "Detected paths" in body and "(0)" in body
    assert "(none)" in body  # detected paths empty placeholder

    actions = _action_block(card)
    assert actions is not None
    assert len(actions["actions"]) == 1
    assert actions["actions"][0]["value"] == {
        "task_id": "popolaloom",
        "action": "ack",
    }


# ── 10: build_skill_missing_card invalid arg → TypeError (No Silent Fail) ─


def test_build_skill_missing_card_rejects_non_list_paths() -> None:
    with pytest.raises(TypeError, match="expected_paths"):
        build_skill_missing_card(
            skill_name="x",
            expected_paths="not-a-list",  # type: ignore[arg-type]
            detected_paths=[],
        )


# ── 11: full-card serialisation round-trip — workspace footer present ───


def test_all_5_builders_serialize_with_footer() -> None:
    """Every new builder MUST embed the workspace-rule footer in its body.

    Per workspace rule "lark-cli 写入操作须追加来源标注": every outbound
    Lark write (excluding email) carries the
    ``\\n---\\n本消息由飞书工具 Lark-Cli 发送`` footer. We assert it on
    the JSON-serialised payload of all 5 builders.
    """
    builders_and_args = [
        (
            build_completion_card,
            {
                "task_id": "t1", "cli": "cursor", "prompt_summary": "p",
                "exit_code": 0, "started_at": "s", "completed_at": "c",
                "latest_event_index": 1,
            },
        ),
        (
            build_failure_card,
            {
                "task_id": "t2", "cli": "claude", "prompt_summary": "p",
                "exit_code": 1, "last_stderr_lines": ["e"],
                "started_at": "s", "failed_at": "f",
            },
        ),
        (
            build_canceled_card,
            {
                "task_id": "t3", "cli": "codex", "prompt_summary": "p",
                "escalated_to_sigkill": False,
                "started_at": "s", "canceled_at": "c",
            },
        ),
        (
            build_cancel_escalated_card,
            {
                "task_id": "t4", "cli": "cursor", "prompt_summary": "p",
                "exit_code": -9, "sigterm_at": "t", "sigkill_at": "k",
            },
        ),
        (
            build_skill_missing_card,
            {
                "skill_name": "popolaloom",
                "expected_paths": ["a"], "detected_paths": ["b"],
            },
        ),
    ]
    for builder, kwargs in builders_and_args:
        card = builder(**kwargs)
        serialized = json.dumps(card, ensure_ascii=False)
        assert "本消息由飞书工具 Lark-Cli 发送" in serialized, (
            f"footer missing from {builder.__name__} output"
        )
