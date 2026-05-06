"""skill_install — runtime API for ``popola skill install`` (v0.5.0 Stage S4).

Per [v0.5.0-plan.md §4 Stage S4.A](../../../.local/memory/specs/popolaloom/v0.5.0-plan.md):

This module hosts the LIBRARY API consumed by the
:mod:`popolaloom.cli.skill_cmd` Typer verb.  Splitting the install
mechanics out of the CLI module keeps the verb thin (parse args →
dispatch → render) and allows the upgrade verb in
:mod:`popolaloom.evolution.skill_upgrade` to reuse the same write
primitives without dragging Typer in.

The single public function is :func:`install_skill`:

* Resolves the canonical SKILL.md from the wheel via
  :func:`popolaloom.cli._skill_source.canonical_source_path` (Stage S2
  resolver — falls back to the byte-stable stub when the wheel is
  pre-S3 corrupt, surfaced via :func:`resolve_skill_source`).
* Computes the install path via :data:`SKILL_TARGETS` (Stage S4 shared
  registry living in :mod:`popolaloom.evolution.skill_inject`).
* Honours ``dry_run=True`` by returning an outcome whose
  ``would_write`` field reports the path the writer would have
  touched, without creating any files.
* Honours idempotency by hashing the existing on-disk content when
  present and skipping the write when it byte-matches the canonical
  source.

The companion :func:`install_all_skills` helper iterates every target
in :data:`SKILL_TARGETS` whose registry entry includes the requested
scope; targets that don't support that scope (e.g. ``copilot`` with
``global``) automatically fall back to their only supported scope and
the resulting outcome records the resolved scope.

Workspace rule "No Silent Failures": every error path is either
re-raised (e.g. unreadable canonical source) or recorded explicitly in
the returned :class:`InstallOutcome` so the caller can surface the
failure to the operator (the CLI verb prints a one-line ``error: ...``
message + exits non-zero).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from popolaloom import __version__
from popolaloom.evolution.skill_inject import (
    SKILL_TARGETS,
    resolve_target_path,
    supported_scopes,
)


def _load_canonical_source() -> tuple[str, bool]:
    """Resolve the canonical SKILL.md content (lazy to avoid an evolution → cli cycle).

    Imported lazily because :mod:`popolaloom.cli._skill_source` lives
    inside the :mod:`popolaloom.cli` package, whose ``__init__.py`` —
    via :mod:`popolaloom.cli.main` — registers the
    :mod:`popolaloom.cli.skill_cmd` subcommand which in turn imports
    *this* module.  Lazy import breaks the would-be cycle.
    """
    from popolaloom.cli._skill_source import resolve_skill_source

    return resolve_skill_source()

__all__ = [
    "InstallOutcome",
    "install_all_skills",
    "install_skill",
    "VERSION_MARKER_FILENAME",
]


VERSION_MARKER_FILENAME: str = ".popola-loom-version"
"""Filename of the version marker written beside each SKILL.md.

Mirrors DevolaFlow's ``.devola-flow-version`` (research §A.5); used by
:mod:`popolaloom.evolution.skill_doctor` to detect drift between the
installed skill and the running wheel.

The marker filename was renamed from ``.popolaloom-version`` to
``.popola-loom-version`` in v0.7.1+ alongside the Skill directory
rename ``popolaloom`` → ``popola-loom``; the symbol name
``VERSION_MARKER_FILENAME`` (and the underlying drift-detection
contract) is unchanged.
"""


@dataclass(frozen=True)
class InstallOutcome:
    """Immutable result of a single :func:`install_skill` call.

    Either ``installed=True`` OR ``skipped=True`` OR ``would_write`` is
    set; the three fields are mutually exclusive (verified by
    :meth:`__post_init__` via dataclass invariants).

    Attributes:
        target:        The IDE target name (``cursor`` / ``claude`` / ``codex`` / ``copilot``).
        scope:         The resolved scope (``global`` / ``project``).  May
                       differ from the user-requested scope when the
                       target only supports one (e.g. copilot →
                       project).
        target_path:   Absolute path the writer wrote to (or *would* have
                       written to in a dry-run).
        installed:     ``True`` when a fresh write happened.
        skipped:       ``True`` when the on-disk content was byte-identical
                       to the canonical source (idempotent re-install).
        would_write:   When ``dry_run=True``, set to ``target_path``;
                       ``None`` otherwise.
        bytes:         File size in bytes after the write (or the
                       existing skipped file's size).  ``None`` for
                       dry-runs.
        reason:        Optional human-readable note (e.g. "already
                       installed (byte-identical)", "scope downgraded:
                       copilot does not support --global").
        version:       Frontmatter ``version:`` value parsed from the
                       skill source (informational; doctor verb does
                       the actual drift check).
    """

    target: str
    scope: str
    target_path: Path
    installed: bool = False
    skipped: bool = False
    would_write: Path | None = None
    bytes: int | None = None
    reason: str | None = None
    version: str = field(default_factory=lambda: __version__)


def _resolve_scope_with_fallback(target: str, requested_scope: str) -> tuple[str, str | None]:
    """Resolve a user-requested scope against :data:`SKILL_TARGETS`.

    Returns:
        tuple[str, str | None]: ``(resolved_scope, reason)``;
        ``reason`` is ``None`` when no fallback was needed,
        otherwise a single-line note explaining the downgrade.

    Raises:
        KeyError: when ``target`` is not in :data:`SKILL_TARGETS`.
    """
    scopes = supported_scopes(target)
    if not scopes:
        valid = ", ".join(sorted(SKILL_TARGETS))
        raise KeyError(
            f"unknown skill target {target!r}; valid targets: {valid}"
        )
    if requested_scope in scopes:
        return requested_scope, None
    fallback = scopes[0]
    return (
        fallback,
        f"target {target!r} does not support scope {requested_scope!r}; "
        f"falling back to {fallback!r}",
    )


def _parse_frontmatter_version(content: str) -> str:
    """Best-effort extract the ``version:`` field from a SKILL.md frontmatter.

    Returns the string after ``version:`` on the first matching line in
    the leading ``---`` ... ``---`` block; falls back to
    :data:`popolaloom.__version__` when the file has no frontmatter or
    no ``version:`` line so callers always have a non-empty value to
    compare against (the doctor verb still flags drift correctly).
    """
    if not content.startswith("---\n"):
        return __version__
    end = content.find("\n---\n", 4)
    if end == -1:
        return __version__
    block = content[4:end]
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if line.startswith("version:"):
            value = line.split(":", 1)[1].strip()
            return value.strip("'\"") or __version__
    return __version__


def _content_byte_match(target_path: Path, expected: bytes) -> bool:
    """Return ``True`` iff ``target_path`` exists and matches ``expected`` byte-for-byte."""
    if not target_path.is_file():
        return False
    try:
        existing = target_path.read_bytes()
    except OSError:
        return False
    return hashlib.sha256(existing).digest() == hashlib.sha256(expected).digest()


def _write_marker(install_dir: Path, version: str) -> None:
    """Write the ``.popola-loom-version`` marker beside the SKILL.md.

    Idempotent: re-writes the marker only when the on-disk content
    differs from ``version`` (so re-installing the same wheel doesn't
    bump the marker mtime needlessly).
    """
    marker_path = install_dir / VERSION_MARKER_FILENAME
    expected = f"{version}\n".encode()
    if marker_path.is_file():
        try:
            if marker_path.read_bytes() == expected:
                return
        except OSError:
            pass
    install_dir.mkdir(parents=True, exist_ok=True)
    marker_path.write_bytes(expected)


def install_skill(
    target: str,
    scope: str = "global",
    *,
    dry_run: bool = False,
) -> InstallOutcome:
    """Install (or re-check) the canonical PopolaLoom SKILL.md for one target.

    Per v0.5.0-plan §4 Stage S4.A:

    1. Resolve the canonical SKILL.md content via
       :func:`resolve_skill_source` (wheel-bundled when present —
       Stage S3+ — falls back to the S2 stub otherwise).
    2. Compute the install path via
       :func:`popolaloom.evolution.skill_inject.resolve_target_path`.
       When ``scope`` is unsupported for ``target`` (e.g. copilot +
       global) the function downgrades to the supported scope and
       records the downgrade in :attr:`InstallOutcome.reason`.
    3. When ``dry_run=True``, return an outcome with ``would_write``
       set to the resolved path (no filesystem write).
    4. When the existing on-disk SKILL.md is byte-identical to the
       canonical source, return ``skipped=True`` (idempotent
       re-install).
    5. Otherwise write the SKILL.md (creating parent directories) and
       a sibling ``.popola-loom-version`` marker.

    Args:
        target:  one of ``cursor`` / ``claude`` / ``codex`` / ``copilot``.
        scope:   ``global`` or ``project`` (default ``global``).
        dry_run: when ``True``, no files are touched.

    Returns:
        InstallOutcome: see class docstring for the field contract.

    Raises:
        KeyError: when ``target`` is unknown.
        OSError:  bubbles up from :func:`resolve_skill_source` when the
                  canonical SKILL.md exists but is unreadable
                  (workspace rule "No Silent Failures").
    """
    resolved_scope, fallback_reason = _resolve_scope_with_fallback(target, scope)
    target_path = resolve_target_path(target, resolved_scope)

    content, _is_real = _load_canonical_source()
    encoded = content.encode("utf-8")
    version = _parse_frontmatter_version(content)

    if dry_run:
        return InstallOutcome(
            target=target,
            scope=resolved_scope,
            target_path=target_path,
            would_write=target_path,
            reason=fallback_reason,
            version=version,
        )

    if _content_byte_match(target_path, encoded):
        return InstallOutcome(
            target=target,
            scope=resolved_scope,
            target_path=target_path,
            skipped=True,
            bytes=len(encoded),
            reason=fallback_reason or "already installed (byte-identical)",
            version=version,
        )

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(encoded)
    _write_marker(target_path.parent, version)

    return InstallOutcome(
        target=target,
        scope=resolved_scope,
        target_path=target_path,
        installed=True,
        bytes=len(encoded),
        reason=fallback_reason,
        version=version,
    )


def install_all_skills(
    scope: str = "global",
    *,
    dry_run: bool = False,
) -> list[InstallOutcome]:
    """Install every target in :data:`SKILL_TARGETS` (mirrors ``popola init all``).

    Iterates ``cursor`` / ``claude`` / ``codex`` / ``copilot`` in the
    registry's insertion order; each call delegates to
    :func:`install_skill` so per-target scope fallback (e.g. copilot →
    project even when ``--global`` is requested) is uniform.

    Args:
        scope:    ``global`` or ``project``; per-target downgrades are
                  recorded in each outcome's ``reason`` field.
        dry_run:  when ``True``, no files are touched.

    Returns:
        list[InstallOutcome]: one outcome per registry target, in
        registry-insertion order.
    """
    outcomes: list[InstallOutcome] = []
    for target in SKILL_TARGETS:
        outcomes.append(install_skill(target, scope=scope, dry_run=dry_run))
    return outcomes
