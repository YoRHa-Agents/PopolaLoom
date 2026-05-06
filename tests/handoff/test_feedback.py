"""Tests for :mod:`popolaloom.handoff.feedback` — HITL feedback envelope (v0.7.3+).

Companion module to :class:`HandoffEnvelope`; this verifies the FeedbackEnvelope
schema, slug-hash addressing (``<task_id>-fb-<8hex>``), Markdown front-matter
ser/deser round-trip, and the atomic ``write_feedback`` writer.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from popolaloom.handoff import (
    FEEDBACK_SCHEMA_VERSION,
    FeedbackEnvelope,
    feedback_path,
    generate_feedback_id,
    write_feedback,
)


def _build_feedback(
    task_id: str = "cursor-23e74ec18917",
    hitl_id: str = "hitl-abc-001",
    answer: str = "approve",
    *,
    reason: str | None = None,
    tags: list[str] | None = None,
    responder: str | None = None,
    channel: str | None = None,
) -> FeedbackEnvelope:
    return FeedbackEnvelope(
        feedback_id=generate_feedback_id(task_id, hitl_id, answer, responder=responder),
        created_at=datetime.now(UTC),
        task_id=task_id,
        hitl_id=hitl_id,
        answer=answer,
        reason=reason,
        tags=tags or [],
        responder=responder,
        channel=channel,
    )


# ── schema ──────────────────────────────────────────────────────────────


def test_feedback_envelope_schema_version_constant() -> None:
    """``FEEDBACK_SCHEMA_VERSION`` is the canonical "1"."""
    assert FEEDBACK_SCHEMA_VERSION == "1"


def test_feedback_envelope_minimal_construction() -> None:
    """All required fields populated → valid model."""
    env = FeedbackEnvelope(
        feedback_id="cursor-abc-fb-12345678",
        created_at=datetime.now(UTC),
        task_id="cursor-abc",
        hitl_id="hitl-001",
        answer="approve",
    )
    assert env.schema_version == "1"
    assert env.tags == []
    assert env.reason is None
    assert env.responder is None
    assert env.channel is None


def test_feedback_envelope_rejects_extra_keys() -> None:
    """``extra="forbid"`` rejects unknown front-matter keys."""
    with pytest.raises(ValueError, match="extra_forbidden|Extra inputs"):
        FeedbackEnvelope(
            feedback_id="x-fb-12345678",
            created_at=datetime.now(UTC),
            task_id="x",
            hitl_id="h-1",
            answer="ok",
            unknown_key="should fail",  # type: ignore[call-arg]
        )


def test_feedback_envelope_rejects_empty_required_strings() -> None:
    """All ``min_length=1`` strings reject the empty string."""
    base = {
        "feedback_id": "x-fb-12345678",
        "created_at": datetime.now(UTC),
        "task_id": "x",
        "hitl_id": "h-1",
        "answer": "a",
    }
    for field in ("feedback_id", "task_id", "hitl_id", "answer"):
        bad = {**base, field: ""}
        with pytest.raises(ValueError):
            FeedbackEnvelope(**bad)  # type: ignore[arg-type]


# ── generate_feedback_id ────────────────────────────────────────────────


def test_generate_feedback_id_format() -> None:
    """ID matches ``<task_id>-fb-<8hex>``."""
    fid = generate_feedback_id("cursor-23e74ec18917", "hitl-abc-1", "approve")
    assert re.fullmatch(r"^cursor-23e74ec18917-fb-[0-9a-f]{8}$", fid), fid


def test_generate_feedback_id_deterministic() -> None:
    """Same inputs → same id (100 calls)."""
    fid1 = generate_feedback_id("t-1", "h-1", "approve", responder="alice")
    for _ in range(100):
        assert generate_feedback_id("t-1", "h-1", "approve", responder="alice") == fid1


def test_generate_feedback_id_sensitive_to_inputs() -> None:
    """Different inputs → different ids."""
    base = generate_feedback_id("t-1", "h-1", "approve")
    assert generate_feedback_id("t-2", "h-1", "approve") != base
    assert generate_feedback_id("t-1", "h-2", "approve") != base
    assert generate_feedback_id("t-1", "h-1", "reject") != base
    assert generate_feedback_id("t-1", "h-1", "approve", responder="bob") != base


def test_generate_feedback_id_rejects_empty_inputs() -> None:
    """Empty task_id / hitl_id / answer → ValueError."""
    with pytest.raises(ValueError, match="task_id"):
        generate_feedback_id("", "h-1", "approve")
    with pytest.raises(ValueError, match="hitl_id"):
        generate_feedback_id("t-1", "", "approve")
    with pytest.raises(ValueError, match="answer"):
        generate_feedback_id("t-1", "h-1", "")


# ── to_markdown / from_markdown round-trip ──────────────────────────────


def test_feedback_to_markdown_starts_with_fence() -> None:
    """Serialised form starts with ``---\\n`` and contains the answer body."""
    env = _build_feedback(answer="approve\nreason: looks good")
    text = env.to_markdown()
    assert text.startswith("---\n")
    assert "approve" in text
    assert env.feedback_id in text


def test_feedback_roundtrip_minimal() -> None:
    """Minimal feedback survives a write→parse round-trip."""
    env = _build_feedback()
    text = env.to_markdown()
    parsed = FeedbackEnvelope.from_markdown(text)
    assert parsed == env


def test_feedback_roundtrip_full_fields() -> None:
    """All-fields-populated feedback round-trips identically."""
    env = _build_feedback(
        answer="reject\nThe diff has unhandled None case.",
        reason="found NoneType bug at line 42",
        tags=["v0.7.3", "blocker"],
        responder="alice@neolix.ai",
        channel="lark",
    )
    text = env.to_markdown()
    parsed = FeedbackEnvelope.from_markdown(text)
    assert parsed == env


def test_feedback_from_markdown_rejects_no_fence() -> None:
    """Missing front-matter fence → ValueError."""
    with pytest.raises(ValueError, match="front-matter"):
        FeedbackEnvelope.from_markdown("no fence here\nanswer body")


def test_feedback_from_markdown_rejects_unclosed_fence() -> None:
    """Open fence without close → ValueError."""
    with pytest.raises(ValueError, match="closing front-matter"):
        FeedbackEnvelope.from_markdown("---\nfoo: bar\nanswer body")


def test_feedback_from_markdown_rejects_empty_body() -> None:
    """Front-matter present but empty body → ValueError."""
    with pytest.raises(ValueError, match="answer.*empty"):
        FeedbackEnvelope.from_markdown(
            "---\n"
            "schema_version: '1'\n"
            "feedback_id: x-fb-12345678\n"
            "created_at: '2026-05-06T14:00:00+00:00'\n"
            "task_id: x\n"
            "hitl_id: h-1\n"
            "reason: null\n"
            "tags: []\n"
            "responder: null\n"
            "channel: null\n"
            "---\n"
        )


# ── feedback_path ───────────────────────────────────────────────────────


def test_feedback_path_canonical(tmp_path: Path) -> None:
    """feedback_path returns ``<base_dir>/<feedback_id>.md``."""
    p = feedback_path("cursor-abc-fb-12345678", base_dir=tmp_path)
    assert p == tmp_path / "cursor-abc-fb-12345678.md"


def test_feedback_path_rejects_traversal(tmp_path: Path) -> None:
    """Traversal in feedback_id → ValueError."""
    for bad in ("../escape", "cursor/abc-fb-12345678", "cursor\\abc"):
        with pytest.raises(ValueError, match="traversal"):
            feedback_path(bad, base_dir=tmp_path)


def test_feedback_path_rejects_empty(tmp_path: Path) -> None:
    """Empty feedback_id → ValueError."""
    with pytest.raises(ValueError, match="non-empty"):
        feedback_path("", base_dir=tmp_path)


def test_feedback_path_uses_env_var_when_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``base_dir=None`` falls back to ``$POPOLA_HANDOFF_DIR``."""
    monkeypatch.setenv("POPOLA_HANDOFF_DIR", str(tmp_path))
    p = feedback_path("x-fb-12345678")
    assert p == tmp_path / "x-fb-12345678.md"


# ── write_feedback ──────────────────────────────────────────────────────


def test_write_feedback_writes_atomically(tmp_path: Path) -> None:
    """write_feedback creates the file at the canonical path."""
    env = _build_feedback(answer="approve via test")

    p = write_feedback(env, base_dir=tmp_path)

    assert p == tmp_path / f"{env.feedback_id}.md"
    assert p.is_file()
    text = p.read_text(encoding="utf-8")
    assert text == env.to_markdown()


def test_write_feedback_rejects_non_envelope(tmp_path: Path) -> None:
    """Passing non-FeedbackEnvelope → TypeError (No Silent Failures)."""
    with pytest.raises(TypeError, match="FeedbackEnvelope"):
        write_feedback("not an envelope", base_dir=tmp_path)  # type: ignore[arg-type]


def test_write_feedback_idempotent_overwrite(tmp_path: Path) -> None:
    """Re-writing the same envelope produces the same file."""
    env = _build_feedback(answer="idempotent")

    p1 = write_feedback(env, base_dir=tmp_path)
    p2 = write_feedback(env, base_dir=tmp_path)

    assert p1 == p2
    assert p1.read_text(encoding="utf-8") == env.to_markdown()


def test_write_feedback_creates_parent_dir(tmp_path: Path) -> None:
    """write_feedback auto-creates missing parent dirs."""
    nested = tmp_path / "a" / "b" / "c"
    env = _build_feedback(answer="nested")

    p = write_feedback(env, base_dir=nested)

    assert p.is_file()
    assert p.parent == nested


def test_write_feedback_no_tmp_leftover(tmp_path: Path) -> None:
    """After successful write, no ``.tmp`` sibling remains."""
    env = _build_feedback(answer="clean")
    write_feedback(env, base_dir=tmp_path)

    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == [], f"unexpected tmp files: {leftovers}"


def test_write_feedback_uses_env_var_when_base_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``base_dir=None`` resolves through ``$POPOLA_HANDOFF_DIR``."""
    monkeypatch.setenv("POPOLA_HANDOFF_DIR", str(tmp_path))
    env = _build_feedback(answer="env-resolved")

    p = write_feedback(env)

    assert p.parent == tmp_path
    assert p.is_file()


# ── coexistence with HandoffEnvelope (same dir, no collision) ──────────


def test_feedback_and_dispatch_envelopes_coexist(tmp_path: Path) -> None:
    """Dispatch + feedback envelopes share the active dir without collision.

    Their filenames differ by the ``-fb-`` infix in feedback_id.
    """
    from popolaloom.handoff import (
        HandoffEnvelope,
        generate_handoff_id,
        write_envelope,
    )

    dispatch = HandoffEnvelope(
        handoff_id=generate_handoff_id("cursor", "do work"),
        created_at=datetime.now(UTC),
        target_cli="cursor",
        prompt="do work",
    )
    feedback = _build_feedback(
        task_id=dispatch.handoff_id,  # link feedback to a real dispatch id
        answer="approve",
    )

    write_envelope(dispatch, base_dir=tmp_path)
    write_feedback(feedback, base_dir=tmp_path)

    files = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
    assert len(files) == 2
    assert any("-fb-" in f for f in files)
    assert any("-fb-" not in f for f in files)
