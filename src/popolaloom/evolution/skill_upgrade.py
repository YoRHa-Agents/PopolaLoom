"""skill_upgrade — re-install the canonical SKILL.md (v0.5.0 Stage S4).

Per [v0.5.0-plan.md §4 Stage S4.C](../../../.local/memory/specs/popolaloom/v0.5.0-plan.md):

The ``upgrade`` verb is intentionally a "force re-install" rather than
a smart 3-way merge: it overwrites whatever SKILL.md currently lives
at the target with the wheel-bundled canonical content.  Same contract
as ``devolaflow plugins refresh`` (research §A.5) — operators who
hand-edit their skill must commit those edits to a fork before
running ``popola skill upgrade``.

Implementation reuses :func:`popolaloom.evolution.skill_install.install_skill`
so the two verbs share path resolution, version-marker handling, and
scope fallback behaviour.  The only difference is that ``upgrade``
writes even when the on-disk content matches the canonical source
(the install verb skips that case to keep mtimes stable).

Workspace rule "No Silent Failures": filesystem errors (e.g. permission
denied on the parent directory) bubble up unchanged; the caller is
expected to catch them and surface a one-line error to the operator.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from popolaloom.evolution.skill_inject import resolve_target_path
from popolaloom.evolution.skill_install import (
    _load_canonical_source,
    _parse_frontmatter_version,
    _resolve_scope_with_fallback,
    _write_marker,
)

__all__ = [
    "UpgradeOutcome",
    "upgrade_skill",
]


@dataclass(frozen=True)
class UpgradeOutcome:
    """Immutable result of a single :func:`upgrade_skill` call.

    Distinct from :class:`InstallOutcome` because upgrade has different
    success semantics: ``replaced=True`` means the previous content
    differed from the canonical source (the operator's edits were
    overwritten — the CLI verb prints a warning), while
    ``up_to_date=True`` means the on-disk content matched and the
    re-write was a no-op (still useful info for ``--dry-run`` output).

    Attributes:
        target:        IDE target name.
        scope:         Resolved scope (after fallback).
        target_path:   Absolute path written.
        replaced:      ``True`` when the writer overwrote a file whose
                       previous content differed from the canonical
                       source.
        up_to_date:    ``True`` when the on-disk content already matched
                       the canonical source (re-write was a no-op).
        installed:     ``True`` when the target file did not exist
                       previously (fresh install via the upgrade path —
                       happens when the operator runs ``popola skill
                       upgrade`` without ever running ``popola skill
                       install``).
        would_write:   When ``dry_run=True``, set to ``target_path``.
        bytes:         New file size in bytes (or planned size for dry-run).
        previous_version: Frontmatter version of the file before the
                       upgrade (``None`` when the file didn't exist or
                       had no parseable frontmatter).
        new_version:   Frontmatter version of the canonical source
                       (the version the file holds after the upgrade).
        reason:        Optional human-readable note (scope downgrade,
                       etc.).
    """

    target: str
    scope: str
    target_path: Path
    replaced: bool = False
    up_to_date: bool = False
    installed: bool = False
    would_write: Path | None = None
    bytes: int | None = None
    previous_version: str | None = None
    new_version: str | None = None
    reason: str | None = None


def _read_existing_version(path: Path) -> str | None:
    """Return the frontmatter version of ``path`` or ``None`` if unreadable."""
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    block = text[4:end]
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if line.startswith("version:"):
            value = line.split(":", 1)[1].strip().strip("'\"")
            return value or None
    return None


def upgrade_skill(
    target: str,
    scope: str = "global",
    *,
    dry_run: bool = False,
) -> UpgradeOutcome:
    """Force re-install the canonical SKILL.md for ``target`` × ``scope``.

    Unlike :func:`popolaloom.evolution.skill_install.install_skill`,
    this function ALWAYS writes (when not in dry-run) even if the
    on-disk content byte-matches the canonical source.  This matches
    the ``devolaflow plugins refresh`` semantics referenced in the
    plan §S4.C: an operator who runs ``upgrade`` is asking for a
    forced re-sync, not an idempotent install.

    Args:
        target:  one of ``cursor`` / ``claude`` / ``codex`` / ``copilot``.
        scope:   ``global`` or ``project`` (default ``global``).
        dry_run: when ``True``, no files are touched.

    Returns:
        UpgradeOutcome: see class docstring for the field contract.

    Raises:
        KeyError: when ``target`` is unknown.
        OSError:  bubbles up from
                  :func:`popolaloom.cli._skill_source.resolve_skill_source`
                  when the canonical SKILL.md is unreadable, OR from the
                  filesystem write when the operator lacks write
                  permission on the install directory.
    """
    resolved_scope, fallback_reason = _resolve_scope_with_fallback(target, scope)
    target_path = resolve_target_path(target, resolved_scope)

    content, _is_real = _load_canonical_source()
    encoded = content.encode("utf-8")
    new_version = _parse_frontmatter_version(content)
    previous_version = _read_existing_version(target_path)
    target_existed = target_path.is_file()

    if dry_run:
        return UpgradeOutcome(
            target=target,
            scope=resolved_scope,
            target_path=target_path,
            would_write=target_path,
            previous_version=previous_version,
            new_version=new_version,
            reason=fallback_reason,
        )

    same_content = False
    if target_existed:
        try:
            same_content = target_path.read_bytes() == encoded
        except OSError:
            same_content = False

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(encoded)
    _write_marker(target_path.parent, new_version)

    if not target_existed:
        return UpgradeOutcome(
            target=target,
            scope=resolved_scope,
            target_path=target_path,
            installed=True,
            bytes=len(encoded),
            previous_version=None,
            new_version=new_version,
            reason=fallback_reason,
        )

    if same_content:
        return UpgradeOutcome(
            target=target,
            scope=resolved_scope,
            target_path=target_path,
            up_to_date=True,
            bytes=len(encoded),
            previous_version=previous_version,
            new_version=new_version,
            reason=fallback_reason,
        )

    return UpgradeOutcome(
        target=target,
        scope=resolved_scope,
        target_path=target_path,
        replaced=True,
        bytes=len(encoded),
        previous_version=previous_version,
        new_version=new_version,
        reason=fallback_reason,
    )
