"""PopolaLoom v0.8.0 hands-off envelope — file-based dispatch payload.

Per design doc D-080 (v0.8.0 plan, user-decided 2026-05-06):
- Q1=A4 Markdown front-matter, Q2=B4 slug-hash, Q4=D4 active+archive 双层
- v0.7.1 (this slice): schema + hash only; writer/archive in v0.7.1 next slice;
  dispatch_with_envelope unification in v0.7.2.
- v0.7.1 (patch 2 — this commit): adds the ``writer`` (atomic active-root
  landing) and ``archive`` (terminal-state copy to ``<task_id>/`` audit
  tree) layers.  Dispatch glue still rides v0.7.2.
"""

from __future__ import annotations

from popolaloom.handoff.archive import (
    DEFAULT_ARCHIVE_ROOT,
    archive_dir_for,
    archive_envelope,
)
from popolaloom.handoff.envelope import HANDOFF_SCHEMA_VERSION, HandoffEnvelope
from popolaloom.handoff.hash import content_hash, generate_handoff_id, slugify_prompt
from popolaloom.handoff.writer import (
    DEFAULT_HANDOFF_ROOT,
    envelope_path,
    write_envelope,
)

__all__ = [
    "HANDOFF_SCHEMA_VERSION",
    "DEFAULT_ARCHIVE_ROOT",
    "DEFAULT_HANDOFF_ROOT",
    "HandoffEnvelope",
    "archive_dir_for",
    "archive_envelope",
    "content_hash",
    "envelope_path",
    "generate_handoff_id",
    "slugify_prompt",
    "write_envelope",
]
