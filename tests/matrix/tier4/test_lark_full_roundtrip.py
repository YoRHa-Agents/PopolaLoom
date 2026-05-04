"""Tier 4 — Lark out + in roundtrip via mocked subprocess.

Per testing-matrix.md §1.4 + roadmap §12.8 + v0.3.0-plan §4 Stage F4.

Drives the full Lark cycle:

1. Render card → build_card_send_argv contains expected pieces.
2. Mocked subprocess.run returns success → message_id captured.
3. Simulated card.action.trigger_v1 event → parse_reply → HITLReply.
4. mark_answered atomically transitions; second event for same hitl_id rejected.

Real lark-cli / real bot path is exercised by
``tests/matrix/tier5/test_lark_real_e2e.py`` (default-skipped).

≥ 4 cases as required by AC #3 of the v0.3.0 task spec.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

from popolaloom.hitl import HITLOption, HITLPrompt
from popolaloom.hitl.renderers.lark import (
    parse_reply,
    render_lark_card,
    send_lark_card,
)
from popolaloom.hitl.sync import HITLStore
from popolaloom.lark.card_templates import build_card_send_argv

## v0.3.0 F4.D: subprocess mocked + SQLite only; default-lane safe.


def _new_prompt() -> HITLPrompt:
    return HITLPrompt(
        trigger="approval",
        why="ok",
        what="pick",
        options=[
            HITLOption(id="yes", label="Yes"),
            HITLOption(id="no", label="No", default=True),
        ],
        default_option_id="no",
        channels=["lark", "ide"],
        deadline_seconds=3600,
        prompt_id="hitl-rt-1",
    )


def _bootstrap_store(tmp_path) -> HITLStore:
    db = tmp_path / "lark_rt.db"
    conn = sqlite3.connect(str(db), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS popola_hitl (
            hitl_id TEXT PRIMARY KEY,
            trigger TEXT NOT NULL,
            status TEXT NOT NULL,
            prompt_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            deadline_at TEXT,
            answered_at TEXT,
            answered_via TEXT,
            answer_option_id TEXT,
            answer_reason TEXT,
            answer_responder_id TEXT,
            lark_message_id TEXT,
            lark_event_ids TEXT,
            lark_send_attempts INTEGER NOT NULL DEFAULT 0,
            lark_last_send_error TEXT,
            task_id TEXT
        )
        """
    )
    return HITLStore(conn)


def test_render_lark_card_includes_action_buttons() -> None:
    prompt = _new_prompt()
    card = render_lark_card(prompt)
    actions = next(
        el for el in card["body"]["elements"] if el["tag"] == "action"
    )
    assert len(actions["actions"]) == len(prompt.options)


def test_send_lark_card_mocked_success_extracts_message_id() -> None:
    prompt = _new_prompt()
    fake_proc = MagicMock(returncode=0, stdout='{"message_id": "om_RT"}', stderr="")
    runner = MagicMock(return_value=fake_proc)
    result = send_lark_card(
        prompt,
        target_open_id="ou_test",
        runner=runner,
        backoff_s=(0.0,),
    )
    assert result.ok is True
    assert result.message_id == "om_RT"
    assert result.attempts == 1


def test_simulated_card_action_event_parsed_into_reply() -> None:
    event = {
        "header": {"event_type": "card.action.trigger_v1", "event_id": "e-1"},
        "event": {
            "operator": {"open_id": "ou_test"},
            "action": {"value": {"hitl_id": "hitl-rt-1", "option_id": "yes"}},
        },
    }
    reply = parse_reply(event)
    assert reply is not None
    assert reply.hitl_id == "hitl-rt-1"
    assert reply.option_id == "yes"
    assert reply.via == "lark"


def test_full_send_then_event_then_mark_answered_roundtrip(tmp_path, monkeypatch) -> None:
    """End-to-end: render → send (mock) → simulated event → mark_answered."""
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_test")
    monkeypatch.setenv("LARK_HITL_ALLOWED_RESPONDERS", "ou_test")

    store = _bootstrap_store(tmp_path)
    prompt = _new_prompt()
    hitl_id = store.create(prompt, hitl_id=prompt.prompt_id)

    ## Mock the subprocess send.
    fake_proc = MagicMock(returncode=0, stdout='{"message_id": "om_R"}', stderr="")
    runner = MagicMock(return_value=fake_proc)
    send_result = send_lark_card(
        prompt,
        target_open_id="ou_test",
        runner=runner,
        backoff_s=(0.0,),
    )
    assert send_result.ok is True

    ## Simulate the inbound event.
    event = {
        "header": {"event_type": "card.action.trigger_v1", "event_id": "evt-1"},
        "event": {
            "operator": {"open_id": "ou_test"},
            "action": {"value": {"hitl_id": hitl_id, "option_id": "yes"}},
        },
    }
    reply = parse_reply(event)
    assert reply is not None
    fold = store.fold_reply(reply)
    assert fold.ok is True

    ## Second event for same hitl_id must be rejected.
    fold2 = store.fold_reply(reply)
    assert fold2.ok is False
    assert fold2.already_status == "answered"


def test_argv_built_for_lark_cli_im_send() -> None:
    prompt = _new_prompt()
    argv = build_card_send_argv(prompt, target_open_id="ou_test")
    assert "lark-cli" in argv[0]
    assert "+send" in argv
    assert "--card" in argv
