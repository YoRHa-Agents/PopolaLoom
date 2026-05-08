"""Append-only NDJSON audit log writer for ``popola relay`` (v0.8.8 T2.3.3).

Implements **mitigation M2** of ``relay-auto-safety.md`` §4 — every
``popola relay`` invocation MUST land at least one row in
``<audit_root>/<source_task_id>.jsonl`` before the cloud API call so a
mid-flight crash leaves a forensic trail. The row schema (≥14 keys) is
owned by the CLI in T2.2.1; this writer is intentionally **schema-
agnostic** and treats the row as an opaque ``dict[str, Any]`` so future
schema bumps land without modifying the writer.

Contract (per task brief AC (a) + (b)):

1. The audit file path is ``<root>/<source_task_id>.jsonl`` — one file
   per source task. The ``source_task_id`` is read from the row itself
   (key ``"source_task_id"``); attempting to append a row missing this
   key raises :class:`ValueError` (No-Silent-Failures).
2. The serialized line is
   ``json.dumps(row, sort_keys=False, separators=(",", ":"),
   ensure_ascii=False) + "\n"`` — compact (no whitespace) with key
   order preserved (so the row schema's documented key ordering carries
   into the file unchanged).
3. The file is opened with mode ``"a"`` which on POSIX maps to
   ``O_WRONLY | O_CREAT | O_APPEND``. The ``O_APPEND`` flag is the
   load-bearing piece for **multi-writer atomicity** — POSIX guarantees
   each ``write()`` of ≤ ``PIPE_BUF`` bytes is atomic when ``O_APPEND``
   is set, so two concurrent ``RelayAuditWriter.append`` calls on the
   same file land as two whole rows with no interleaving (no lock
   needed for the cross-process case; matches §8.2 of the safety spec).
4. After the write, ``fh.flush()`` followed by ``os.fsync(fh.fileno())``
   forces the data into the kernel buffer cache and onto the storage
   device. A kernel panic immediately after ``append()`` returns
   therefore loses ≤ 0 audit rows (modulo the storage layer's
   guarantees).
5. On first creation the audit file is ``chmod 0o600`` (owner-only
   read+write) and its parent dir is ``chmod 0o700`` (owner-only
   traverse+read+write). The parent ``os.makedirs(..., mode=0o700,
   exist_ok=True)`` only honors ``mode`` on creation, so we
   ``os.chmod(parent, 0o700)`` afterwards as a defensive double-tap
   (covers the case where the parent dir already exists at a looser
   mode, e.g. ``0o755``). The same defensive ``chmod(path, 0o600)``
   runs after every successful append so a manual ``chmod 644
   <file>.jsonl`` is reverted on the next relay attempt.

Workspace rule **No Silent Failures**: ``ValueError`` for malformed
``source_task_id`` (empty / contains path separator / contains ``..``)
and ``OSError`` for filesystem-level errors are surfaced unchanged.
``json.dumps`` ``TypeError`` (row contains a non-serialisable value) is
propagated — callers (T2.2.1) MUST sanitise the row before append.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)


DEFAULT_AUDIT_ROOT: Final[Path] = Path(".local/.agent/archive/relay")
"""Default audit log root, relative to repo root.

Sibling of the existing ``.local/.agent/archive/<task_id>/`` per-task
handoff envelope tree (see ``popolaloom.handoff.archive``); the two
layouts coexist because they answer different forensic queries:

- ``<archive_root>/<task_id>/<handoff_id>.md`` — "what envelopes did
  this task produce?" (per-task envelope grouping, owned by
  :mod:`popolaloom.handoff.archive`).
- ``<audit_root>/<source_task_id>.jsonl`` — "what relays did this
  source task trigger?" (per-source append-only log of relay
  decisions, owned by THIS module).

Both roots are gitignored under ``.local/`` per v0.7.0+ workspace
hygiene.
"""


def _validate_task_id(task_id: str) -> None:
    """Reject empty / path-separator / ``..`` task ids.

    Mirrors the path-traversal guard in
    :func:`popolaloom.handoff.writer.envelope_path`. Without this guard
    a row whose ``source_task_id`` contains ``..`` could escape the
    audit root and overwrite an arbitrary file the daemon has write
    access to. ``No-Silent-Failures`` rule: raise instead of sanitising.
    """
    if not task_id:
        raise ValueError(
            "RelayAuditWriter.append: row['source_task_id'] must be non-empty"
        )
    if "/" in task_id or "\\" in task_id:
        raise ValueError(
            "RelayAuditWriter.append: row['source_task_id'] must not contain "
            f"path separators, got {task_id!r}"
        )
    if ".." in task_id.split("-"):
        raise ValueError(
            "RelayAuditWriter.append: row['source_task_id'] must not contain "
            f"'..' segments, got {task_id!r}"
        )


class RelayAuditWriter:
    """Append-only audit log writer for relay decisions.

    The writer is **stateless across appends** (no held file descriptor,
    no in-memory buffer) so a long-lived CLI process can safely call
    ``append()`` and then exit without an explicit ``close()``: each call
    opens, writes, fsyncs, and closes the file. This matches the human-
    paced relay invocation cadence (≤ 1 call per CLI invocation typically;
    see ``relay-auto-safety.md`` §8.2 concurrency note) and avoids the
    file-descriptor leak class entirely.

    Args:
        root: Directory under which per-task audit files live. Created
            (with ``0o700`` mode) on first ``append()`` if missing.
            Defaults to :data:`DEFAULT_AUDIT_ROOT`. May be relative
            (resolved against CWD by the eventual filesystem call) or
            absolute.

    Example:
        >>> writer = RelayAuditWriter(Path("/tmp/audit"))  # doctest: +SKIP
        >>> writer.append({                                # doctest: +SKIP
        ...     "schema_version": "1",
        ...     "source_task_id": "v088-foo-3a7f",
        ...     "outcome": "dispatched",
        ... })
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self._root: Path = (
            Path(root) if root is not None else DEFAULT_AUDIT_ROOT
        )

    @property
    def root(self) -> Path:
        """Configured audit root directory (read-only view)."""
        return self._root

    def path_for(self, source_task_id: str) -> Path:
        """Return the canonical audit file path for ``source_task_id``.

        Side-effect-free helper; useful for callers that want to test
        path layout without writing a row. Raises :class:`ValueError`
        on the same path-traversal patterns as :meth:`append`.
        """
        _validate_task_id(source_task_id)
        return self._root / f"{source_task_id}.jsonl"

    def append(self, row: dict[str, Any]) -> Path:
        """Append ``row`` as one NDJSON line to the per-task audit file.

        Args:
            row: Audit row dict. MUST contain a non-empty
                ``"source_task_id"`` string key (used to derive the
                file path). All other keys are opaque to the writer —
                the row schema (≥14 keys per ``relay-auto-safety.md``
                §4.2) is owned by the caller (T2.2.1 ``cli/relay_cmd.py``).

        Returns:
            Path: Absolute or relative :class:`Path` of the audit file
            that was appended to (whichever ``root`` resolves to).
            Idempotent ONLY in the sense that re-calling with the same
            row appends a *second* row — auditing has append-only
            semantics, so de-duplication is the caller's job (via the
            ``idempotency_key`` field of the schema).

        Raises:
            ValueError: ``row`` is missing ``"source_task_id"`` or the
                value is empty / contains path separators / contains
                ``..`` segments.
            TypeError: ``row`` contains a value that ``json.dumps``
                cannot serialise.
            OSError: filesystem-level error (disk full, permission
                denied, read-only filesystem, …) — re-raised unchanged
                so the caller can map to its own error envelope.
        """
        task_id_obj = row.get("source_task_id")
        if not isinstance(task_id_obj, str):
            raise ValueError(
                "RelayAuditWriter.append: row['source_task_id'] must be a "
                f"non-empty string, got {type(task_id_obj).__name__}"
            )
        _validate_task_id(task_id_obj)

        path = self._root / f"{task_id_obj}.jsonl"
        parent = path.parent

        os.makedirs(parent, mode=0o700, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError as exc:
            logger.warning(
                "RelayAuditWriter: chmod(parent=%s, 0o700) failed: %s",
                parent,
                exc,
            )

        line = (
            json.dumps(
                row,
                sort_keys=False,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        )

        file_existed_before_open = path.exists()

        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

        if not file_existed_before_open:
            os.chmod(path, 0o600)
        else:
            try:
                os.chmod(path, 0o600)
            except OSError as exc:
                logger.warning(
                    "RelayAuditWriter: chmod(path=%s, 0o600) failed: %s",
                    path,
                    exc,
                )

        logger.debug(
            "RelayAuditWriter: appended row to %s (size=%d bytes)",
            path,
            len(line.encode("utf-8")),
        )
        return path
