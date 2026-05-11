"""Tier 1 — coverage boost for v0.3.0 F4 internals.

These tests exercise the smaller helper paths in F4 modules that the
behaviour-first tests don't otherwise cover (per-function rich helpers,
``_row_to_dict`` tuple branch, deadline formatting, etc.).
"""

from __future__ import annotations

from importlib import resources
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from popolaloom.hitl import HITLOption, HITLPrompt, HITLStore
from popolaloom.hitl.renderers import ide
from popolaloom.hitl.renderers.cli import (
    deadline_remaining_human,
    render_pending_table,
    render_pending_text,
)
from popolaloom.hitl.renderers.lark import (
    LarkSendResult,
    _extract_message_id,
)
from popolaloom.hitl.sync import HITLRow, _row_to_dict, _str_or_none
from popolaloom.lark import is_lark_runtime_available, lark_allowed_responders, lark_target_open_id
from popolaloom.lark.card_templates import (
    extract_button_value,
    footer_with_origin_note,
)
from popolaloom.lark.supervisor import SupervisorState

_counter = 0


def _make_prompt(prompt_id: str | None = None) -> HITLPrompt:
    global _counter
    _counter += 1
    return HITLPrompt(
        trigger="approval",
        why="why",
        what="what",
        options=[HITLOption(id="a", label="A"), HITLOption(id="b", label="B")],
        default_option_id="a",
        channels=["lark", "ide"],
        deadline_seconds=3600,
        prompt_id=prompt_id or f"hitl-{_counter}",
    )


# ── HITLStore exercise ──────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path: Path) -> HITLStore:
    db_path = tmp_path / "hitl.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    migration_sql = (Path(resources.files("popolaloom.migrations")) / "006_popola_hitl.sql").read_text(encoding="utf-8")
    conn.executescript(migration_sql)
    conn.commit()
    return HITLStore(conn)


def test_list_pending_with_task_filter(store: HITLStore) -> None:
    """list_pending(task_id=...) filters correctly."""
    a = store.create(_make_prompt(), task_id="task-alpha")
    b = store.create(_make_prompt(), task_id="task-beta")
    pending_a = store.list_pending(task_id="task-alpha")
    assert {p["hitl_id"] for p in pending_a} == {a}
    pending_b = store.list_pending(task_id="task-beta")
    assert {p["hitl_id"] for p in pending_b} == {b}


def test_list_overdue_returns_only_past_deadlines(store: HITLStore) -> None:
    """list_overdue filters by deadline."""
    past = datetime.now(UTC) - timedelta(hours=1)
    future = datetime.now(UTC) + timedelta(hours=1)
    overdue_id = store.create(_make_prompt(), deadline_at=past)
    store.create(_make_prompt(), deadline_at=future)
    overdue = store.list_overdue()
    assert {r["hitl_id"] for r in overdue} == {overdue_id}


def test_update_lark_send_records_message_id(store: HITLStore) -> None:
    """update_lark_send records message_id + last error + attempts."""
    prompt = _make_prompt()
    hitl_id = store.create(prompt)
    store.update_lark_send(
        hitl_id, message_id="om_abc", last_send_error=None, attempts_increment=1
    )
    row = store.get(hitl_id)
    assert row is not None
    assert row["lark_message_id"] == "om_abc"
    assert row["lark_send_attempts"] == 1


def test_append_lark_event_id_dedup(store: HITLStore) -> None:
    """append_lark_event_id refuses duplicates."""
    prompt = _make_prompt()
    hitl_id = store.create(prompt)
    assert store.append_lark_event_id(hitl_id, "ev-1") is True
    assert store.append_lark_event_id(hitl_id, "ev-1") is False
    assert store.append_lark_event_id(hitl_id, "ev-2") is True


def test_append_lark_event_id_nonexistent(store: HITLStore) -> None:
    """append_lark_event_id returns False on unknown hitl_id."""
    assert store.append_lark_event_id("hitl-nope", "ev-1") is False


def test_mark_status_invalid_status_raises(store: HITLStore) -> None:
    with pytest.raises(ValueError, match=r"only handles timeout/cancelled"):
        store.mark_status("hitl-x", "answered")


def test_fold_reply_raises_on_unknown_via(store: HITLStore) -> None:
    from popolaloom.hitl import HITLReply
    # email is in the allowed list but we should still ensure it dispatches.
    store.create(_make_prompt())
    # Construct a reply with an invalid via via raw model bypass.
    bad = HITLReply.model_construct(
        hitl_id="hitl-x", option_id="a", via="invalid"  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match=r"unsupported reply channel"):
        store.fold_reply(bad)


def test_row_to_dict_dict_factory_roundtrip(store: HITLStore) -> None:
    prompt = _make_prompt()
    hitl_id = store.create(prompt)
    row = store.get(hitl_id)
    assert isinstance(row, dict)
    assert row["hitl_id"] == hitl_id


def test_row_to_dict_tuple_fallback() -> None:
    """_row_to_dict accepts a plain tuple for a connection without row_factory."""
    columns = (
        "hitl-1", "approval", "pending", "{}",
        "2026-01-01T00:00:00Z", None, None,
        None, None, None, None,
        None, None, 0, None, None,
    )
    out = _row_to_dict(columns)
    assert out["hitl_id"] == "hitl-1"
    assert out["status"] == "pending"


def test_str_or_none_helper() -> None:
    assert _str_or_none(None) is None
    assert _str_or_none("") is None
    assert _str_or_none("ok") == "ok"
    assert _str_or_none(42) == "42"


def test_hitl_row_from_dict() -> None:
    data = {
        "hitl_id": "hitl-1",
        "trigger": "approval",
        "status": "pending",
        "prompt_json": json.dumps({
            "trigger": "approval",
            "why": "w",
            "what": "x",
            "options": [
                {"id": "a", "label": "A", "default": False},
                {"id": "b", "label": "B", "default": False},
            ],
            "default_option_id": "a",
            "channels": ["lark", "ide"],
            "deadline_seconds": 3600,
            "artifacts": [],
            "prompt_id": "hitl-1",
        }),
        "created_at": "2026-01-01T00:00:00Z",
    }
    row = HITLRow.from_dict(data)
    assert row.hitl_id == "hitl-1"
    assert row.prompt.trigger == "approval"
    assert row.prompt.default_option_id == "a"


# ── CLI renderer ──────────────────────────────────────────────────────


def test_render_pending_table_with_rows() -> None:
    table = render_pending_table([_make_prompt(), _make_prompt("hitl-y")])
    # Just ensure it returns something Rich-shaped (Table type or printable).
    assert table is not None


def test_render_pending_text_with_rows() -> None:
    out = render_pending_text([_make_prompt(), _make_prompt("hitl-y")])
    assert "hitl_id" in out
    assert "approval" in out


def test_deadline_remaining_human_overdue() -> None:
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    out = deadline_remaining_human(past)
    assert out == "overdue"


def test_deadline_remaining_human_minutes() -> None:
    future = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
    out = deadline_remaining_human(future)
    assert out.endswith("m")


def test_deadline_remaining_human_seconds() -> None:
    future = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
    out = deadline_remaining_human(future)
    assert out.endswith("s")


def test_deadline_remaining_human_invalid() -> None:
    """Non-ISO strings round-trip back unchanged (best-effort fallback)."""
    out = deadline_remaining_human("not-iso")
    assert out == "not-iso"


# ── IDE renderer ──────────────────────────────────────────────────────


def test_render_ide_notify_includes_default_option() -> None:
    msg = ide.render_ide_notify(_make_prompt())
    assert "popola feedback" in msg.cli_command


def test_dispatch_ide_notify_returns_false_when_no_notifier(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda *a, **kw: None)
    out = ide.dispatch_ide_notify(_make_prompt())
    assert out is False


# ── Lark renderer helpers ──────────────────────────────────────────────


def test_extract_message_id_from_json_line() -> None:
    stdout = '{"message_id": "om_xyz", "ts": 1}\nother line\n'
    assert _extract_message_id(stdout) == "om_xyz"


def test_extract_message_id_from_data_envelope() -> None:
    stdout = '{"data": {"message_id": "om_data", "k": "v"}}\n'
    assert _extract_message_id(stdout) == "om_data"


def test_extract_message_id_falls_back_to_stdout_prefix() -> None:
    stdout = "no json here at all"
    assert _extract_message_id(stdout) == "no json here at all"


def test_lark_send_result_truncates_logs() -> None:
    result = LarkSendResult(
        ok=False,
        attempts=3,
        error="x",
        argv=["lark-cli"],
        stdout="A" * 5000,
        stderr="B" * 5000,
    )
    assert len(result.stdout) == 2048
    assert len(result.stderr) == 2048


# ── Card templates ────────────────────────────────────────────────────


def test_extract_button_value_with_dict() -> None:
    a, b = extract_button_value({"hitl_id": "h", "option_id": "y"})
    assert a == "h" and b == "y"


def test_extract_button_value_missing_keys() -> None:
    a, b = extract_button_value({})
    assert (a, b) == (None, None)


def test_footer_with_origin_note_skip_when_already_present() -> None:
    body = "x\n\n---\n本消息由飞书工具 Lark-Cli 发送"
    assert footer_with_origin_note(body) == body


# ── Lark env helpers ──────────────────────────────────────────────────


def test_is_lark_runtime_available(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda *a, **kw: "/usr/bin/lark-cli")
    assert is_lark_runtime_available() is True
    monkeypatch.setattr("shutil.which", lambda *a, **kw: None)
    assert is_lark_runtime_available() is False


def test_lark_target_open_id_env(monkeypatch) -> None:
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_test")
    assert lark_target_open_id() == "ou_test"
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "")
    assert lark_target_open_id() is None


def test_lark_allowed_responders_csv(monkeypatch) -> None:
    monkeypatch.setenv("LARK_HITL_ALLOWED_RESPONDERS", "ou_a, ou_b , ou_c")
    out = lark_allowed_responders()
    assert out == ["ou_a", "ou_b", "ou_c"]


def test_lark_allowed_responders_default_target(monkeypatch) -> None:
    monkeypatch.delenv("LARK_HITL_ALLOWED_RESPONDERS", raising=False)
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_target")
    assert lark_allowed_responders() == ["ou_target"]


def test_lark_allowed_responders_empty_when_no_target(monkeypatch) -> None:
    monkeypatch.delenv("LARK_HITL_ALLOWED_RESPONDERS", raising=False)
    monkeypatch.delenv("LARK_HITL_TARGET_OPEN_ID", raising=False)
    assert lark_allowed_responders() == []


# ── Supervisor state ──────────────────────────────────────────────────


def test_supervisor_state_defaults() -> None:
    state = SupervisorState()
    assert state.restart_count == 0
    assert state.escalated is False
    assert state.events == []


# ── HitlHandleability dim ──────────────────────────────────────────────


def test_hitl_handleability_full_evidence() -> None:
    """All 4 sub-components fully populated → 1.0."""
    from popolaloom.evaluation.dimensions.hitl_handleability import HitlHandleability

    dim = HitlHandleability()
    score = dim.score({
        # schema
        "hitl_prompts_emitted": 10,
        "hitl_schema_failures": 0,
        # reply parse
        "hitl_replies_received": 10,
        "hitl_replies_parsed": 10,
        # cross-channel sync
        "cross_channel_sync_total": 10,
        "cross_channel_sync_winners": 10,
        # lark health
        "lark_send_total": 10,
        "lark_send_ok": 10,
        "lark_listener_uptime_total_s": 100,
        "lark_listener_uptime_alive_s": 100,
        "lark_roundtrip_total": 10,
        "lark_roundtrip_under_10s": 10,
    })
    assert score == 1.0


def test_hitl_handleability_partial_evidence() -> None:
    """Only schema_completeness present → score equals that ratio."""
    from popolaloom.evaluation.dimensions.hitl_handleability import HitlHandleability

    dim = HitlHandleability()
    score = dim.score({
        "hitl_prompts_emitted": 10,
        "hitl_schema_failures": 2,  # 80% schema completeness
    })
    assert 0.79 <= score <= 0.81


def test_hitl_handleability_no_evidence() -> None:
    from popolaloom.evaluation.dimensions.hitl_handleability import HitlHandleability

    dim = HitlHandleability()
    assert dim.score({}) == 0.5


def test_hitl_handleability_zero_denominator_skipped() -> None:
    from popolaloom.evaluation.dimensions.hitl_handleability import HitlHandleability

    dim = HitlHandleability()
    score = dim.score({
        "hitl_replies_received": 0,
        "hitl_replies_parsed": 0,
    })
    assert score == 0.5


def test_hitl_handleability_lark_health_partial() -> None:
    from popolaloom.evaluation.dimensions.hitl_handleability import HitlHandleability

    dim = HitlHandleability()
    score = dim.score({
        "lark_send_total": 10,
        "lark_send_ok": 5,  # 50% send ratio
    })
    assert 0.4 <= score <= 0.6
