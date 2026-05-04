"""Tier 4 — HITL graph interrupt → render → reply → resume roundtrip.

Per testing-matrix.md §1.4 + roadmap §12 + v0.3.0-plan §4 Stage F4 + AC #2.

≥ 6 cases:

1. interrupt → render to lark + ide → mark_answered via lark wins
2. mock reply through CLI renderer
3. mock reply through MCP renderer
4. cross-channel sync — first wins, second is rejected
5. timeout fires default option
6. cancel + re-create new prompt for same task

These tests exercise the full sync.HITLStore flow without needing a
real LangGraph runtime (LangGraph integration is exercised separately
by tier4 test_hitl_interrupt_resume_extended.py).
"""

from __future__ import annotations

import sqlite3

from popolaloom.hitl import HITLOption, HITLPrompt
from popolaloom.hitl.renderers import cli, lark, mcp
from popolaloom.hitl.sync import HITLStore

## v0.3.0 F4: SQLite-only roundtrip; no subprocess; default-lane safe.


def _bootstrap_store(tmp_path) -> HITLStore:
    db_path = tmp_path / "hitl_full.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS popola_hitl (
            hitl_id             TEXT PRIMARY KEY,
            trigger             TEXT NOT NULL,
            status              TEXT NOT NULL,
            prompt_json         TEXT NOT NULL,
            created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            deadline_at         TEXT,
            answered_at         TEXT,
            answered_via        TEXT,
            answer_option_id    TEXT,
            answer_reason       TEXT,
            answer_responder_id TEXT,
            lark_message_id     TEXT,
            lark_event_ids      TEXT,
            lark_send_attempts  INTEGER NOT NULL DEFAULT 0,
            lark_last_send_error TEXT,
            task_id             TEXT
        )
        """
    )
    return HITLStore(conn)


def _new_prompt() -> HITLPrompt:
    return HITLPrompt(
        trigger="approval",
        why="confirm",
        what="yes/no?",
        options=[
            HITLOption(id="yes", label="Yes"),
            HITLOption(id="no", label="No", default=True),
        ],
        default_option_id="no",
        channels=["lark", "ide", "cli"],
        deadline_seconds=3600,
    )


def test_render_to_lark_then_mark_answered(tmp_path) -> None:
    store = _bootstrap_store(tmp_path)
    prompt = _new_prompt()
    hitl_id = store.create(prompt)
    card = lark.render_lark_card(prompt)
    assert "本消息由飞书工具" in str(card)
    result = store.mark_answered(hitl_id, option_id="yes", via="lark")
    assert result.ok is True


def test_render_to_cli_then_mark_answered(tmp_path) -> None:
    store = _bootstrap_store(tmp_path)
    prompt = _new_prompt()
    hitl_id = store.create(prompt)
    text = cli.render_pending_text([prompt])
    assert hitl_id in text or prompt.prompt_id in text
    reply = cli.parse_reply(hitl_id, "yes")
    if hasattr(store, "mark_answered_from_reply"):
        result = store.mark_answered_from_reply(reply)
    else:
        result = store.fold_reply(reply)
    assert result.ok is True


def test_mcp_renderer_then_mark_answered(tmp_path) -> None:
    store = _bootstrap_store(tmp_path)
    prompt = _new_prompt()
    hitl_id = store.create(prompt)
    payload = mcp.render_mcp_elicitation(prompt)
    assert payload.get("params", {}).get("mode") == "form"
    reply = mcp.parse_reply({"hitl_id": hitl_id, "choice": "yes"})
    assert reply is not None
    result = store.fold_reply(reply)
    assert result.ok is True


def test_cross_channel_first_wins_second_rejected(tmp_path) -> None:
    store = _bootstrap_store(tmp_path)
    prompt = _new_prompt()
    hitl_id = store.create(prompt)
    r1 = store.mark_answered(hitl_id, option_id="yes", via="lark")
    r2 = store.mark_answered(hitl_id, option_id="no", via="ide")
    assert r1.ok is True
    assert r2.ok is False
    assert r2.already_via == "lark"


def test_timeout_applies_default_option(tmp_path) -> None:
    store = _bootstrap_store(tmp_path)
    prompt = _new_prompt()
    hitl_id = store.create(prompt)
    fired = store.process_timeout(hitl_id)
    assert fired is True
    row = store.get(hitl_id)
    assert row is not None
    assert row["status"] == "timeout"
    assert row["answer_option_id"] == prompt.default_option_id


def test_cancel_and_recreate_for_same_task(tmp_path) -> None:
    store = _bootstrap_store(tmp_path)
    prompt = _new_prompt()
    hitl_id_1 = store.create(prompt)
    cancelled = store.mark_status(hitl_id_1, "cancelled")
    assert cancelled is True
    ## Now create a fresh prompt for the same task; new hitl_id.
    prompt2 = _new_prompt()
    prompt2.prompt_id = "hitl-new-1"
    hitl_id_2 = store.create(prompt2, hitl_id="hitl-new-1")
    assert hitl_id_1 != hitl_id_2
    row = store.get(hitl_id_2)
    assert row is not None
    assert row["status"] == "pending"
