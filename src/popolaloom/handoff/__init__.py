"""PopolaLoom v0.8.0 hands-off envelope — file-based dispatch payload.

Per design doc D-080 (v0.8.0 plan, user-decided 2026-05-06):
- Q1=A4 Markdown front-matter, Q2=B4 slug-hash, Q4=D4 active+archive 双层
- v0.7.1 (foundation slice): schema + hash + writer + archive.
- v0.7.2 (THIS slice): adds ``loader`` (read-side helpers — list active /
  resolve path / load + parse) so the new ``popola handoff`` CLI subcommand
  group + ``Popolad.dispatch_with_envelope`` E3-internal-unification can
  consume envelopes back from disk.  Dispatch glue + adapter env+flag
  injection also lands here (see :class:`popolaloom.daemon.server.Popolad`).
"""

from __future__ import annotations

from popolaloom.handoff.archive import (
    DEFAULT_ARCHIVE_ROOT,
    archive_dir_for,
    archive_envelope,
)
from popolaloom.handoff.envelope import HANDOFF_SCHEMA_VERSION, HandoffEnvelope
from popolaloom.handoff.feedback import (
    DEFAULT_FEEDBACK_FILE_PREFIX,
    FEEDBACK_SCHEMA_VERSION,
    FeedbackEnvelope,
    feedback_path,
    generate_feedback_id,
    write_feedback,
)
from popolaloom.handoff.hash import content_hash, generate_handoff_id, slugify_prompt
from popolaloom.handoff.loader import (
    HandoffSummary,
    list_active_envelopes,
    load_envelope,
    resolve_envelope_path,
)
from popolaloom.handoff.writer import (
    DEFAULT_HANDOFF_ROOT,
    envelope_path,
    write_envelope,
)

__all__ = [
    "DEFAULT_ARCHIVE_ROOT",
    "DEFAULT_FEEDBACK_FILE_PREFIX",
    "DEFAULT_HANDOFF_ROOT",
    "FEEDBACK_SCHEMA_VERSION",
    "FeedbackEnvelope",
    "HANDOFF_SCHEMA_VERSION",
    "HandoffEnvelope",
    "HandoffSummary",
    "archive_dir_for",
    "archive_envelope",
    "content_hash",
    "envelope_path",
    "feedback_path",
    "generate_feedback_id",
    "generate_handoff_id",
    "list_active_envelopes",
    "load_envelope",
    "resolve_envelope_path",
    "slugify_prompt",
    "write_envelope",
    "write_feedback",
]
