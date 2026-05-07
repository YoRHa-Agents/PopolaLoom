"""Terminal-state archive copier for handoff envelopes (v0.7.1, design Q4 = D4 archive root).

Per the v0.8.0 plan (user decision 2026-05-06), Q4 = D4 splits the
handoff filesystem into two layers:

- **active root** (sister :mod:`popolaloom.handoff.writer`):
  ``.local/.agent/handoff/<handoff_id>.md`` is the in-flight payload.
- **archive root** (this module):
  ``.local/.agent/archive/<task_id>/<handoff_id>.md`` is the terminal-
  state audit snapshot.

Why **copy** instead of move / symlink?
=======================================

* **Cross-FS robustness** — a move (``rename(2)``) degrades to a
  copy-then-unlink across mountpoints; an explicit copy is the same cost
  and works regardless.
* **Cross-OS compatibility** — Windows symlinks require either admin
  privileges or developer-mode + a security-policy tweak.  Hard links
  fail across volumes.  Copies always work.
* **Audit invariant** — the archive entry is meant to be an immutable
  snapshot.  Copying decouples it from any later GC / mutation of the
  active copy (planned for v0.7.2's terminal-state hook); a symlink
  would silently rot when the active copy is deleted.

The only state this module touches is the destination tree under
:data:`DEFAULT_ARCHIVE_ROOT` (or the caller's override).  The source
file at ``handoff_path`` is **never** removed or modified — the active
writer owns that lifecycle.

Workspace rule "No Silent Failures": all error paths re-raise
(``FileNotFoundError`` / ``OSError``) or raise an explicit
``ValueError`` with diagnostics; nothing is swallowed.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Final

DEFAULT_ARCHIVE_ROOT: Final[Path] = Path(".local/.agent/archive")
"""Default archive root, relative to repo root.

Like :data:`popolaloom.handoff.writer.DEFAULT_HANDOFF_ROOT`, this lives
under the gitignored ``.local/`` tree (per v0.7.0 ``.gitignore`` rules).
Resolved relative to CWD at call time when a relative path is passed —
the dispatch / terminal-state hook in v0.7.2 will ``chdir`` to the
repo root before calling :func:`archive_envelope`.
"""


def _validate_task_id(task_id: str) -> None:
    """Reject empty or path-traversal-shaped ``task_id`` values.

    Pure helper, no IO.  We forbid:

    - empty string (would land at ``<root>/``, polluting the root).
    - forward slash ``/`` (multi-segment path; would silently nest into
      attacker-controlled depth or escape via embedded ``..``).
    - backslash ``\\`` (Windows path separator; same risk as ``/``).
    - standalone ``..`` (the only remaining traversal vector once the
      separators above are gone — task ids legitimately contain dots
      for timestamps / semver suffixes, so we *only* reject the bare
      double-dot, not any string containing ``..`` as a substring).

    All four cases would normally be caught by the OS-level path
    resolution downstream, but per workspace rule "No Silent Failures"
    we surface the policy violation up-front with a clear message rather
    than letting the caller see a confusing OS error half-way through
    a copy.

    Raises:
        ValueError: any of the above invariants is violated.
    """
    if not task_id:
        raise ValueError("archive: task_id must be non-empty")
    if "/" in task_id:
        raise ValueError(
            f"archive: task_id must not contain '/' (path-traversal guard), got {task_id!r}"
        )
    if "\\" in task_id:
        raise ValueError(
            f"archive: task_id must not contain '\\\\' (path-traversal guard), got {task_id!r}"
        )
    # After the separators are gone, the only way ``Path(root) / task_id``
    # can escape ``root`` is if ``task_id`` *is* the literal ``..``.  A
    # leading dot (".env" / "..config") is a perfectly fine filename.
    if task_id == "..":
        raise ValueError(
            f"archive: task_id must not be '..' (path-traversal guard), got {task_id!r}"
        )


def _resolve_archive_root(archive_root: Path | str | None) -> Path:
    """Return ``archive_root`` as a :class:`Path`, defaulting to :data:`DEFAULT_ARCHIVE_ROOT`."""
    if archive_root is None:
        return DEFAULT_ARCHIVE_ROOT
    return Path(archive_root)


def archive_dir_for(task_id: str, *, archive_root: Path | str | None = None) -> Path:
    """Return ``<archive_root>/<task_id>`` *without creating* the directory.

    Side-effect-free helper for callers that want to know where a task's
    archive lives (e.g. cleanup / audit tooling) without triggering
    ``mkdir``.  The path-traversal guard still fires here — an attacker
    can't get a poisoned path by skipping :func:`archive_envelope`.

    Args:
        task_id:      Logical task identifier (e.g. ArkTower task id);
            validated against the path-traversal denylist above.
        archive_root: Override for the archive root.  Defaults to
            :data:`DEFAULT_ARCHIVE_ROOT`.

    Returns:
        ``Path(archive_root) / task_id``.

    Raises:
        ValueError: ``task_id`` fails :func:`_validate_task_id`'s checks.
    """
    _validate_task_id(task_id)
    return _resolve_archive_root(archive_root) / task_id


def archive_envelope(
    handoff_path: Path | str,
    task_id: str,
    *,
    archive_root: Path | str | None = None,
) -> Path:
    """Copy the active envelope at ``handoff_path`` into the per-task archive dir.

    Behavior:

    - Validates ``task_id`` (no traversal sequences) **before** any IO.
    - Verifies ``handoff_path`` exists and is a file (not a directory)
      before attempting the copy — :func:`shutil.copy2` would silently
      copy *into* a directory destination otherwise, hiding the bug.
    - Creates ``<archive_root>/<task_id>/`` via ``mkdir -p`` if missing.
    - Uses :func:`shutil.copy2` (preserves mtime + atime + permission
      bits — the audit trail needs the file's original timestamp, not
      "when the archive ran").
    - Idempotent: re-archiving the same source to the same destination
      is a no-op overwrite (``copy2`` truncates and rewrites).
    - **Does not delete or move** ``handoff_path`` — the active copy
      stays as the authoritative log; v0.7.2 may add a separate GC pass.

    Args:
        handoff_path: Path to the active envelope file (typically the
            return value of :func:`popolaloom.handoff.writer.write_envelope`).
        task_id:      Per-task subdirectory under ``archive_root``.
        archive_root: Override for the archive root.  Defaults to
            :data:`DEFAULT_ARCHIVE_ROOT`.

    Returns:
        Destination path: ``<archive_root>/<task_id>/<handoff_path.name>``.

    Raises:
        ValueError: ``task_id`` fails :func:`_validate_task_id`.
        FileNotFoundError: ``handoff_path`` does not exist.
        IsADirectoryError: ``handoff_path`` exists but points at a
            directory (operator error).
        OSError: Disk full / permission denied / etc. — re-raised
            verbatim from :func:`shutil.copy2`.
    """
    _validate_task_id(task_id)

    src = Path(handoff_path)
    if not src.exists():
        raise FileNotFoundError(
            f"archive_envelope: source handoff file does not exist: {src}"
        )
    if src.is_dir():
        # copy2 happily copies "into" a dir destination; it's silent on
        # "src is a dir" and would call shutil.copytree() semantics, which
        # is almost certainly not what the caller meant.
        raise IsADirectoryError(
            f"archive_envelope: source handoff path is a directory, not a file: {src}"
        )

    dest_dir = _resolve_archive_root(archive_root) / task_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name

    # copy2 preserves mtime/atime/perms; this is the audit-trail invariant.
    # On overwrite it truncates + rewrites the destination, so idempotency
    # is "last writer wins, content is identical when sources are identical".
    shutil.copy2(src, dest)
    return dest
