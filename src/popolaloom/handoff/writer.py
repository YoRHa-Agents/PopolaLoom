"""Atomic file writer for handoff envelopes (v0.7.1, design Q4 = D4 active root).

Per the v0.8.0 plan (user decision 2026-05-06):

- Q4 = D4 — handoff envelopes live in a **two-tier** filesystem layout:

  * **active root** (this module): ``.local/.agent/handoff/<handoff_id>.md``
    is the in-flight payload that ``dispatch`` (v0.7.2) drops on disk for
    the receiving CLI to read.  ``.local/`` is gitignored as of v0.7.0
    so envelopes never escape into commits.
  * **archive root** (sister :mod:`popolaloom.handoff.archive` module):
    ``.local/.agent/archive/<task_id>/<handoff_id>.md`` is the audit
    snapshot copied (not moved, not symlinked) when a task reaches a
    terminal state.

This module is deliberately **just** the active-root writer — terminal-state
auto-archiving rides in v0.7.2.  The split keeps the writer trivially safe to
call repeatedly (idempotent) and the archive function trivially safe to call
exactly once (terminal-state hook).

Atomicity contract
==================

The writer follows the standard POSIX rename pattern:

1. Serialize the envelope into a sibling temp file
   (``<target>.tmp`` — *same directory* so :func:`os.replace` is a same-FS
   inode swap, not a cross-device copy that degrades to non-atomic).
2. :func:`os.replace` the temp file onto the final path.  Per CPython
   docs (3.3+) this resolves to ``rename(2)`` on POSIX (atomic
   replace-if-exists) and ``MoveFileExW(MOVEFILE_REPLACE_EXISTING)`` on
   Windows (also atomic-on-NTFS).

Workspace rule "No Silent Failures": every error path either
re-raises with full context (``OSError`` from the OS layer is surfaced
verbatim) or raises an explicit ``TypeError`` with a diagnostic
message — nothing is swallowed.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Final

from popolaloom.handoff.envelope import HandoffEnvelope

DEFAULT_HANDOFF_ROOT: Final[Path] = Path(".local/.agent/handoff")
"""Default active root, relative to repo root.

Gitignored as of v0.7.0 (see ``.gitignore``'s
``=== PopolaLoom local agent workspace (v0.7.0+) ===`` block).
Resolved relative to the *current working directory* when a relative
path is passed to :func:`write_envelope` / :func:`envelope_path`; the
caller is responsible for ``chdir``-ing to the repo root if they care
about absolute placement (the dispatch path in v0.7.2 will do this
explicitly).
"""


def _resolve_base_dir(base_dir: Path | str | None) -> Path:
    """Return ``base_dir`` as a :class:`Path`, defaulting to :data:`DEFAULT_HANDOFF_ROOT`.

    Pure helper — no IO, no validation beyond the type coercion.
    """
    if base_dir is None:
        return DEFAULT_HANDOFF_ROOT
    return Path(base_dir)


def envelope_path(handoff_id: str, *, base_dir: Path | str | None = None) -> Path:
    """Return the canonical path ``<base_dir>/<handoff_id>.md`` *without writing*.

    Side-effect-free helper for callers (e.g. dispatch in v0.7.2) that
    need to compute the eventual landing path before — or independently
    of — actually serializing the envelope.

    Args:
        handoff_id: The envelope's ``handoff_id`` (typically produced by
            :func:`popolaloom.handoff.hash.generate_handoff_id`).  Must be
            non-empty; an empty id would land at ``<base_dir>/.md`` which
            is almost certainly a caller bug.
        base_dir:   Override for the active root.  Defaults to
            :data:`DEFAULT_HANDOFF_ROOT`.  May be relative (resolved
            against CWD by the eventual filesystem call) or absolute.

    Returns:
        ``Path(base_dir) / f"{handoff_id}.md"``.  No directory is created
        and no file is touched.

    Raises:
        ValueError: ``handoff_id`` is empty or contains a path separator
            (``/``, ``\\``) or path-traversal sequence (``..``) — these
            would let a malicious id escape the active root.  The
            sister :mod:`archive` module enforces the same invariant on
            ``task_id``; we keep the rule local here so callers can rely
            on the writer alone for the active-root contract.
    """
    if not handoff_id:
        raise ValueError("envelope_path: handoff_id must be non-empty")
    if "/" in handoff_id or "\\" in handoff_id:
        raise ValueError(
            f"envelope_path: handoff_id must not contain path separators, got {handoff_id!r}"
        )
    if ".." in handoff_id.split("-"):
        raise ValueError(
            f"envelope_path: handoff_id must not contain '..' segments, got {handoff_id!r}"
        )

    return _resolve_base_dir(base_dir) / f"{handoff_id}.md"


def write_envelope(
    envelope: HandoffEnvelope,
    *,
    base_dir: Path | str | None = None,
) -> Path:
    """Atomically write ``envelope`` to ``<base_dir>/<handoff_id>.md``.

    The serialized form is :meth:`HandoffEnvelope.to_markdown` (Markdown
    front-matter + body) encoded as UTF-8.  The write is staged through
    a sibling ``.tmp`` file and finalized via :func:`os.replace` so a
    crash mid-write leaves the *previous* contents (or no file at all)
    intact — readers never observe a half-written envelope.

    Args:
        envelope: The :class:`HandoffEnvelope` to land on disk.  Its
            ``handoff_id`` determines the filename.
        base_dir: Override for the active root.  Defaults to
            :data:`DEFAULT_HANDOFF_ROOT`.  Created via ``mkdir -p`` if
            it (or any parent) does not yet exist.

    Returns:
        Absolute or relative :class:`Path` of the written file (whichever
        ``base_dir`` resolves to).  Idempotent: re-calling with the same
        envelope hits the same path with byte-identical contents (same
        ``handoff_id`` ↔ same content by construction; see
        :func:`popolaloom.handoff.hash.generate_handoff_id`).

    Raises:
        TypeError: ``envelope`` is not a :class:`HandoffEnvelope`.
            (Without this guard a duck-typed input would land at the
            wrong path or produce garbled markdown — explicit failure
            per workspace rule "No Silent Failures".)
        OSError:   Disk full / permission denied / read-only filesystem.
            Re-raised unchanged so the caller (dispatch in v0.7.2) can
            map to its own error envelope rather than guessing why the
            write failed.
    """
    if not isinstance(envelope, HandoffEnvelope):
        raise TypeError(
            "write_envelope: envelope must be a HandoffEnvelope instance, "
            f"got {type(envelope).__name__}"
        )

    target = envelope_path(envelope.handoff_id, base_dir=base_dir)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)

    # Use a sibling tmp file (same dir) so os.replace is a same-FS rename.
    # Putting tmp in /tmp would risk EXDEV (cross-device link) on systems
    # where the active root is on a separate mount (containers, NFS, etc.).
    tmp = parent / f"{target.name}.tmp"
    payload = envelope.to_markdown()

    try:
        # Path.write_text(encoding="utf-8") opens with "w" (truncate) which
        # is fine for the tmp file — a leftover .tmp from a previous crashed
        # run is just overwritten before the atomic os.replace step.
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        # No Silent Failures: the primary OSError (ENOSPC / EACCES / EROFS / …)
        # is re-raised below so the caller can act on it.  Cleaning the tmp
        # debris is a best-effort secondary task — if even the unlink fails
        # we still want the original error to reach the caller, so the
        # cleanup itself is wrapped in ``contextlib.suppress(OSError)``.
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise

    return target
