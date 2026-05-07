"""HITL feedback envelope — companion of :class:`HandoffEnvelope` for user answers (v0.7.3).

Per design D-080 Q7=yes (user-decided 2026-05-06): HITL feedback (the user's
typed answer to a ``LangGraph.interrupt()`` prompt) is also persisted as a
file-based envelope so the entire dispatch ↔ feedback round-trip is
auditable / replayable / addressable by id.

Schema mirrors :class:`HandoffEnvelope` design choices (Pydantic v2,
``extra="forbid"``, schema_version="1") but the body is the user's free-form
``answer`` text rather than a dispatch ``prompt``. Front-matter carries the
linkage (``task_id`` / ``hitl_id``) so a tooling pipeline can join feedback
back to the originating dispatch.

This module ships in v0.7.3 as the **foundation** — wiring it into the live
HITL feedback flow (``popola feedback ...`` CLI / daemon ``mark_answered``)
is opt-in via a future ``--persist`` flag and will land in v0.7.4. Until
then callers can manually write feedback envelopes from their own scripts
(e.g. for after-the-fact audit imports).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

FEEDBACK_SCHEMA_VERSION: Final[str] = "1"

DEFAULT_FEEDBACK_FILE_PREFIX: Final[str] = "feedback-"
"""Active feedback envelope filename prefix (e.g. ``feedback-<id>.md``)."""


class FeedbackEnvelope(BaseModel):
    """File-based HITL feedback envelope (companion to HandoffEnvelope).

    Attributes:
        schema_version: anchor for forward-compat schema evolution.
        feedback_id: slug-hash address (e.g. ``cursor-23e74ec18917-fb-3a7f9c1d``).
        created_at: UTC tz-aware datetime of the answer.
        task_id: the popola task id this feedback is for (back-reference).
        hitl_id: the LangGraph interrupt prompt id this answers.
        answer: the user's free-form answer text (the body of the file).
        reason: optional human-readable rationale (typically why
            "approve" / "reject" was chosen).
        tags: free-form labels (e.g. ``["approval", "v0.7.x"]``).
        responder: optional user identifier ("alice", "bob@example.com",
            ``None`` when unknown). Recorded for audit trail; popolad
            does NOT validate against a user directory.
        channel: which HITL channel (cli / lark / ide / mcp / web) the
            answer arrived from. Useful for post-hoc routing analysis.
            ``None`` when unknown.

    "No Silent Failures": ``extra="forbid"`` so unknown front-matter keys
    raise :class:`pydantic.ValidationError` instead of silently dropping
    audit data.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = FEEDBACK_SCHEMA_VERSION
    feedback_id: str = Field(..., min_length=1)
    created_at: datetime
    task_id: str = Field(..., min_length=1)
    hitl_id: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1)
    reason: str | None = None
    tags: list[str] = Field(default_factory=list)
    responder: str | None = None
    channel: str | None = None

    def to_markdown(self) -> str:
        """Serialise as Markdown front-matter (YAML metadata + body=answer).

        Same shape as :meth:`HandoffEnvelope.to_markdown`: front-matter holds
        every field except ``answer``; body holds the answer text. The
        ``created_at`` field is stringified to ISO-8601 to avoid pyyaml
        timestamp-style drift across builds.
        """
        meta = self.model_dump(mode="python", exclude={"answer"})
        meta["created_at"] = self.created_at.isoformat()
        front = yaml.safe_dump(
            meta,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )
        body = self.answer.rstrip() + "\n"
        return f"---\n{front}---\n{body}"

    @classmethod
    def from_markdown(cls, text: str) -> FeedbackEnvelope:
        """Parse a feedback envelope file back into a model.

        Mirrors :meth:`HandoffEnvelope.from_markdown` — wraps Pydantic
        ``ValidationError`` into ``ValueError`` so callers handle a
        single error type per the No-Silent-Failures contract.
        """
        if not text.startswith("---\n"):
            raise ValueError(
                "feedback envelope must start with YAML front-matter fence '---\\n'"
            )
        fence_end = text.find("\n---\n", 4)
        if fence_end == -1:
            raise ValueError("feedback envelope missing closing front-matter fence")
        front = text[4:fence_end]
        body = text[fence_end + 5 :].rstrip("\n")
        if not body:
            raise ValueError("feedback envelope body (answer) is empty")
        try:
            meta = yaml.safe_load(front) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"feedback envelope YAML front-matter parse error: {exc}") from exc
        if not isinstance(meta, dict):
            raise ValueError(
                f"feedback envelope front-matter must be a mapping, got {type(meta).__name__}"
            )
        meta["answer"] = body
        try:
            return cls.model_validate(meta)
        except ValidationError as exc:
            raise ValueError(f"feedback envelope schema validation failed: {exc}") from exc


def generate_feedback_id(
    task_id: str,
    hitl_id: str,
    answer: str,
    *,
    responder: str | None = None,
) -> str:
    """Slug-hash address for a feedback envelope.

    Format: ``<task_id>-fb-<8hex>`` where the hex is sha256 of
    canonical-JSON over (task_id, hitl_id, answer, responder).

    The ``-fb-`` infix marks the file as a feedback envelope (as opposed
    to a dispatch envelope from :func:`generate_handoff_id`) so the two
    coexist in the same active dir without colliding.

    Examples:
        >>> generate_feedback_id("cursor-23e74ec18917", "hitl-abc-1", "approve")
        # 'cursor-23e74ec18917-fb-3a7f9c1d' (actual hash is content-determined)
    """
    if not task_id:
        raise ValueError("task_id must be non-empty")
    if not hitl_id:
        raise ValueError("hitl_id must be non-empty")
    if not answer:
        raise ValueError("answer must be non-empty")
    payload: dict[str, Any] = {
        "task_id": task_id,
        "hitl_id": hitl_id,
        "answer": answer,
        "responder": responder,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
    return f"{task_id}-fb-{digest}"


def feedback_path(
    feedback_id: str,
    *,
    base_dir: Path | str | None = None,
) -> Path:
    """Return the canonical active path for a feedback envelope.

    Resolves ``base_dir`` with the same precedence as the dispatch writer:
    explicit arg → ``$POPOLA_HANDOFF_DIR`` env → ``DEFAULT_HANDOFF_ROOT``.
    Both feedback and dispatch envelopes share the active dir; the
    ``-fb-`` infix in ``feedback_id`` is the only physical distinction.

    Args:
        feedback_id: slug-hash from :func:`generate_feedback_id`. Must
            be non-empty + free of path-traversal segments.

    Raises:
        ValueError: invalid feedback_id (empty / traversal).
    """
    from popolaloom.handoff.writer import DEFAULT_HANDOFF_ROOT

    if not feedback_id:
        raise ValueError("feedback_id must be non-empty")
    if "/" in feedback_id or "\\" in feedback_id or ".." in feedback_id.split("/"):
        raise ValueError(
            f"feedback_id must not contain path traversal sequences: {feedback_id!r}"
        )

    if base_dir is None:
        env_dir = os.environ.get("POPOLA_HANDOFF_DIR")
        base_dir = Path(env_dir) if env_dir else DEFAULT_HANDOFF_ROOT

    return Path(base_dir) / f"{feedback_id}.md"


def write_feedback(
    envelope: FeedbackEnvelope,
    *,
    base_dir: Path | str | None = None,
) -> Path:
    """Atomically write the feedback envelope to ``<base_dir>/<feedback_id>.md``.

    Mirrors :func:`popolaloom.handoff.write_envelope` — same atomic
    rename strategy (``.tmp`` sibling + ``os.replace``), same auto
    ``mkdir -p``, same idempotent-overwrite semantics.

    Raises:
        TypeError: if ``envelope`` is not a :class:`FeedbackEnvelope`.
        OSError: on disk-full / permission failure (re-raised, No
            Silent Failures).
    """
    if not isinstance(envelope, FeedbackEnvelope):
        raise TypeError(
            f"write_feedback expects FeedbackEnvelope, got {type(envelope).__name__}"
        )

    target = feedback_path(envelope.feedback_id, base_dir=base_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(envelope.to_markdown(), encoding="utf-8")
        os.replace(tmp, target)
    except Exception:
        # Best-effort cleanup of partial tmp file on failure; the original
        # error is always re-raised (No Silent Failures — see calling
        # context above the suppress block).
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
    return target
