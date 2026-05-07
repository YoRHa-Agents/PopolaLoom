"""skill_uninstall — runtime API for ``popola skill uninstall`` (v0.8.4).

Per [feedback_for_v0.8.3.md](../../../.local/feedbacks/feedback_for_v0.8.3.md):

The v0.8.4 release closes a long-standing gap in the Skill management
surface: ``popola skill install`` and ``popola skill upgrade`` have
shipped since v0.5.0 (Stage S4) but there has been no first-party way
to *remove* an installed Skill short of ``rm -rf``.  This module hosts
the LIBRARY API that backs the new ``popola skill uninstall`` Typer
verb (see :mod:`popolaloom.cli.skill_cmd`); the unified bash installer
``install.sh`` at the repo root composes ``popola skill uninstall``
into its ``uninstall`` verb so end-to-end teardown is a single shell
command.

Mirrors :mod:`popolaloom.evolution.skill_install`:

* Resolves the install path via :data:`SKILL_TARGETS` (shared registry
  in :mod:`popolaloom.evolution.skill_inject`) so this module never
  duplicates path-resolution logic.
* Honours the same scope-fallback behaviour
  (:func:`_resolve_scope_with_fallback` from
  :mod:`popolaloom.evolution.skill_install`) so a request like
  ``uninstall_skill('copilot', scope='global')`` gracefully resolves
  to ``project`` and records the fallback in the outcome's
  ``reason`` field — matching the install / upgrade surfaces.
* Honours ``dry_run=True`` by returning an outcome whose
  ``would_remove`` field reports the path the writer would have
  unlinked, without touching the filesystem.
* When the SKILL.md is absent on disk, returns an outcome with
  ``skipped=True`` (idempotent re-uninstall — re-running ``popola
  skill uninstall`` after the file is gone is a no-op).

Per workspace rule "No Silent Failures": every :class:`OSError`
encountered while unlinking the SKILL.md or its sibling
``.popola-loom-version`` marker bubbles up to the caller.  The one
explicit best-effort step is the post-unlink ``rmdir`` of the parent
``popola-loom/`` directory: the directory may legitimately contain
non-Skill files (e.g. operator notes, unrelated tooling), and treating
"directory not empty" as a hard failure would force the operator to
manually `rm -rf` instead — an actively worse UX than the current
"leave the dir alone if other files are present" behaviour.  That
single best-effort path is documented at the call site below.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path

from popolaloom.evolution.skill_inject import SKILL_TARGETS, resolve_target_path
from popolaloom.evolution.skill_install import (
    VERSION_MARKER_FILENAME,
    _resolve_scope_with_fallback,
)

__all__ = [
    "UninstallOutcome",
    "uninstall_all_skills",
    "uninstall_skill",
]


@dataclass(frozen=True)
class UninstallOutcome:
    """Immutable result of a single :func:`uninstall_skill` call.

    Either ``uninstalled=True`` OR ``skipped=True`` OR ``would_remove``
    is set; the three states are mutually exclusive.

    Attributes:
        target:         IDE target name (``cursor`` / ``claude`` /
                        ``codex`` / ``copilot``).
        scope:          Resolved scope (after fallback — may differ from
                        the user-requested scope when the target only
                        supports one, e.g. copilot → project).
        target_path:    Absolute path the SKILL.md lives at (or *would*
                        have lived at for a missing target).
        uninstalled:    ``True`` when the SKILL.md was removed from disk.
        skipped:        ``True`` when the SKILL.md was already absent
                        (idempotent re-uninstall).
        would_remove:   When ``dry_run=True``, set to ``target_path``;
                        ``None`` otherwise.
        bytes_removed:  Size of the unlinked SKILL.md in bytes (``None``
                        for ``skipped``/``dry_run`` outcomes).
        removed_marker: ``True`` when the sibling
                        ``.popola-loom-version`` marker was also
                        removed.  Always ``False`` for copilot since
                        copilot installs as a single
                        ``copilot-instructions.md`` file at
                        ``.github/`` with no companion marker.
        reason:         Optional human-readable note (e.g. "already
                        absent", "scope downgraded: copilot does not
                        support --global").
    """

    target: str
    scope: str
    target_path: Path
    uninstalled: bool = False
    skipped: bool = False
    would_remove: Path | None = None
    bytes_removed: int | None = None
    removed_marker: bool = False
    reason: str | None = None


def _has_marker(target: str) -> bool:
    """Return ``True`` iff ``target`` installs a sibling marker file.

    Cursor / Claude / Codex install ``SKILL.md`` plus a sibling
    ``.popola-loom-version`` marker file (per
    :func:`popolaloom.evolution.skill_install._write_marker`); copilot
    installs as a single flat-file ``copilot-instructions.md`` at
    ``.github/`` with no companion marker.  The uninstall surface
    needs to know which targets to look for the marker beside.
    """
    return target != "copilot"


def _is_empty_dir(path: Path) -> bool:
    """Return ``True`` iff ``path`` is a directory and contains no entries."""
    if not path.is_dir():
        return False
    try:
        next(path.iterdir())
    except StopIteration:
        return True
    return False


def uninstall_skill(
    target: str,
    scope: str = "global",
    *,
    dry_run: bool = False,
) -> UninstallOutcome:
    """Remove the PopolaLoom SKILL.md for one IDE target.

    Per the v0.8.4 acceptance contract:

    1. Resolve the install path via :func:`resolve_target_path` (using
       the same scope-fallback behaviour as
       :func:`popolaloom.evolution.skill_install.install_skill`).
    2. When ``dry_run=True``, return an outcome with ``would_remove``
       set to the resolved path (no filesystem write).
    3. When the SKILL.md does not exist on disk, return
       ``skipped=True`` with ``reason="already absent"``
       (idempotent re-uninstall — never raises on a clean home).
    4. When the SKILL.md exists, capture its size, ``unlink()`` it,
       then for non-copilot targets attempt to unlink the sibling
       ``.popola-loom-version`` marker.  Marker removal failures
       bubble up as :class:`OSError` (No Silent Failures).
    5. Best-effort ``rmdir`` of the parent ``popola-loom/`` directory
       *iff* it is empty — this is the one explicit best-effort step
       (documented in the module docstring).  Non-popola files in the
       parent are left untouched and we return success.

    Args:
        target:  one of ``cursor`` / ``claude`` / ``codex`` / ``copilot``.
        scope:   ``global`` or ``project`` (default ``global``); when
                 the target does not support the requested scope, the
                 outcome's ``scope`` field reflects the resolved
                 fallback (matches install_skill).
        dry_run: when ``True``, no files are touched.

    Returns:
        UninstallOutcome: see class docstring for the field contract.

    Raises:
        KeyError: when ``target`` is not in :data:`SKILL_TARGETS`
            (re-raised from :func:`_resolve_scope_with_fallback`).
        OSError:  bubbles up from :meth:`Path.unlink` when the
            operator lacks write permission on the install directory
            (workspace rule "No Silent Failures").
    """
    resolved_scope, fallback_reason = _resolve_scope_with_fallback(target, scope)
    target_path = resolve_target_path(target, resolved_scope)

    if dry_run:
        return UninstallOutcome(
            target=target,
            scope=resolved_scope,
            target_path=target_path,
            would_remove=target_path,
            reason=fallback_reason,
        )

    if not target_path.is_file():
        return UninstallOutcome(
            target=target,
            scope=resolved_scope,
            target_path=target_path,
            skipped=True,
            reason=fallback_reason or "already absent",
        )

    size = target_path.stat().st_size
    target_path.unlink()

    removed_marker = False
    parent_dir = target_path.parent
    if _has_marker(target):
        marker_path = parent_dir / VERSION_MARKER_FILENAME
        if marker_path.is_file():
            marker_path.unlink()
            removed_marker = True

    # Best-effort prune of the now-empty popola-loom/ leaf directory only.
    # Per the module docstring this is the single explicit best-effort
    # step in the uninstall surface: we never traverse upwards (e.g.
    # ~/.cursor/skills/ is left alone even when popola-loom/ was its
    # only child) and we never attempt to remove a non-empty dir.
    # contextlib.suppress(OSError) covers the rare race where the dir
    # becomes non-empty between our check and the rmdir call (concurrent
    # IDE writes); we leave it alone rather than escalating.
    if parent_dir.name == "popola-loom" and _is_empty_dir(parent_dir):
        with contextlib.suppress(OSError):
            parent_dir.rmdir()

    return UninstallOutcome(
        target=target,
        scope=resolved_scope,
        target_path=target_path,
        uninstalled=True,
        bytes_removed=size,
        removed_marker=removed_marker,
        reason=fallback_reason,
    )


def uninstall_all_skills(
    scope: str = "global",
    *,
    dry_run: bool = False,
) -> list[UninstallOutcome]:
    """Uninstall every target in :data:`SKILL_TARGETS` (mirrors install_all_skills).

    Iterates ``cursor`` / ``claude`` / ``codex`` / ``copilot`` in the
    registry's insertion order; each call delegates to
    :func:`uninstall_skill` so per-target scope fallback (e.g. copilot
    → project even when ``--global`` is requested) is uniform.

    Args:
        scope:    ``global`` or ``project``; per-target downgrades are
                  recorded in each outcome's ``reason`` field.
        dry_run:  when ``True``, no files are touched.

    Returns:
        list[UninstallOutcome]: one outcome per registry target, in
        registry-insertion order.
    """
    outcomes: list[UninstallOutcome] = []
    for target in SKILL_TARGETS:
        outcomes.append(uninstall_skill(target, scope=scope, dry_run=dry_run))
    return outcomes
