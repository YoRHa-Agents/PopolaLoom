"""Read-side helpers for the v0.8.0 hands-off envelope (v0.7.2 patch 2).

Companion to :mod:`popolaloom.handoff.writer` (atomic write) and
:mod:`popolaloom.handoff.archive` (active → archive copy):

- :func:`list_active_envelopes` — enumerate ``<base_dir>/*.md`` and return
  light-weight summaries (handoff_id + path + size + mtime). The full
  envelope body is NOT loaded — use :func:`load_envelope` for that.
- :func:`load_envelope` — read a specific envelope file and parse via
  :meth:`HandoffEnvelope.from_markdown`.
- :func:`resolve_envelope_path` — find the active envelope file for a
  given ``handoff_id`` (precedence mirrors the writer's: explicit
  ``base_dir`` arg > ``$POPOLA_HANDOFF_DIR`` env > default).

These are pure read functions: no mutation, no archive side-effects;
the archive workflow lives in :func:`popolaloom.handoff.archive_envelope`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from popolaloom.handoff.envelope import HandoffEnvelope
from popolaloom.handoff.writer import DEFAULT_HANDOFF_ROOT


@dataclass(frozen=True, slots=True)
class HandoffSummary:
    """Light-weight summary of an active envelope file (no body parse).

    Attributes:
        handoff_id: the slug-hash id (filename without ``.md`` suffix).
        path: absolute path to the envelope file on disk.
        size_bytes: file size in bytes (st_size).
        mtime: file modification time (UTC tz-aware).
    """

    handoff_id: str
    path: Path
    size_bytes: int
    mtime: datetime


def _resolve_base_dir(base_dir: Path | str | None) -> Path:
    """Mirror the writer's resolution: explicit > $POPOLA_HANDOFF_DIR > default.

    Centralised here so loader / writer / archive share the same precedence
    contract; future refactors only need to touch one place.
    """
    if base_dir is not None:
        return Path(base_dir)
    env_dir = os.environ.get("POPOLA_HANDOFF_DIR")
    if env_dir:
        return Path(env_dir)
    return DEFAULT_HANDOFF_ROOT


def list_active_envelopes(
    *,
    base_dir: Path | str | None = None,
) -> list[HandoffSummary]:
    """List all ``*.md`` files in the active handoff dir as summaries.

    Returns an empty list if the dir doesn't exist (vs. raising) — fresh
    workspaces with zero dispatches yet are a normal state. ``OSError``
    on a permission failure during listing IS re-raised (No Silent
    Failures).

    Results are sorted by ``mtime`` descending (newest first) so a
    ``popola handoff list`` user sees the latest dispatch at the top.

    Args:
        base_dir: optional override; ``None`` → ``$POPOLA_HANDOFF_DIR``
            env, fallback to ``DEFAULT_HANDOFF_ROOT``.

    Returns:
        list[HandoffSummary]: empty when dir missing; sorted by mtime
        desc when present.

    Raises:
        OSError: on permission errors during stat / iterdir.
    """
    root = _resolve_base_dir(base_dir)
    if not root.is_dir():
        return []
    summaries: list[HandoffSummary] = []
    for entry in root.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix != ".md":
            continue
        st = entry.stat()
        summaries.append(
            HandoffSummary(
                handoff_id=entry.stem,
                path=entry.resolve(),
                size_bytes=st.st_size,
                mtime=datetime.fromtimestamp(st.st_mtime, tz=UTC),
            )
        )
    summaries.sort(key=lambda s: s.mtime, reverse=True)
    return summaries


def resolve_envelope_path(
    handoff_id: str,
    *,
    base_dir: Path | str | None = None,
) -> Path:
    """Return the canonical active path for ``handoff_id``.

    Does NOT verify file existence — caller is responsible (see
    :func:`load_envelope` for the existence-checking variant).

    Args:
        handoff_id: slug-hash id (without ``.md``).
        base_dir: same resolution as :func:`list_active_envelopes`.

    Returns:
        Path: ``<base_dir>/<handoff_id>.md``.

    Raises:
        ValueError: if ``handoff_id`` is empty or contains path-traversal
            segments (``..`` / ``/`` / ``\\``); mirrors archive's
            ``task_id`` validation.
    """
    if not handoff_id:
        raise ValueError("handoff_id must be non-empty")
    if "/" in handoff_id or "\\" in handoff_id or ".." in handoff_id.split("/"):
        raise ValueError(
            f"handoff_id must not contain path traversal sequences: {handoff_id!r}"
        )
    return _resolve_base_dir(base_dir) / f"{handoff_id}.md"


def load_envelope(
    handoff_id: str,
    *,
    base_dir: Path | str | None = None,
) -> HandoffEnvelope:
    """Read + parse the envelope file for ``handoff_id``.

    Args:
        handoff_id: slug-hash id.
        base_dir: same resolution.

    Returns:
        HandoffEnvelope: validated model.

    Raises:
        ValueError: traversal in handoff_id, OR malformed file content
            (delegated to :meth:`HandoffEnvelope.from_markdown`).
        FileNotFoundError: when the file doesn't exist at the resolved
            path (No Silent Failures — caller decides whether absence is
            an error or "expected" via try/except).
    """
    path = resolve_envelope_path(handoff_id, base_dir=base_dir)
    if not path.is_file():
        raise FileNotFoundError(f"handoff envelope not found: {path}")
    text = path.read_text(encoding="utf-8")
    return HandoffEnvelope.from_markdown(text)
