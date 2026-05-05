"""skill_doctor — read-only audit of installed PopolaLoom SKILL.md files (v0.5.0 Stage S4).

Per [v0.5.0-plan.md §4 Stage S4.B](../../../.local/memory/specs/popolaloom/v0.5.0-plan.md):

This module hosts the LIBRARY API consumed by the
:mod:`popolaloom.cli.skill_cmd` ``doctor`` verb and the aggregate
:mod:`popolaloom.cli.doctor_cmd` ``popola doctor`` command.  The
contract is a single pure-read function — :func:`check_skill_health`
— that walks every IDE target in :data:`SKILL_TARGETS` (or a caller-
supplied subset), parses the YAML frontmatter, and returns one
:class:`DoctorReport` per (target, scope) pair.

Per workspace rule "No Silent Failures": every error path is captured
in :attr:`DoctorReport.notes` so the operator sees what went wrong
(e.g. "frontmatter missing", "unreadable: PermissionError"); the
function itself never raises.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from popolaloom import __version__
from popolaloom.evolution.skill_inject import SKILL_TARGETS

__all__ = [
    "DoctorReport",
    "check_skill_health",
    "format_target_label",
]


@dataclass(frozen=True)
class DoctorReport:
    """Per-(target, scope) health report returned by :func:`check_skill_health`.

    Immutable + serialisable so the CLI verb's ``--json`` flag can dump
    a list of reports via :func:`dataclasses.asdict` directly.

    Attributes:
        target:        IDE target name (``cursor`` / ``claude`` / ...).
        scope:         Resolved scope (``global`` / ``project``).
        expected_path: Where ``check_skill_health`` looked for the
                       SKILL.md (or ``copilot-instructions.md``).
        exists:        ``True`` iff ``expected_path.is_file()``.
        bytes:         File size in bytes when it exists; ``None`` otherwise.
        version:       Frontmatter ``version:`` value when it exists +
                       parses; ``None`` when the file is absent or has
                       no parseable frontmatter.
        drift:         ``True`` iff ``version`` is non-``None`` and not
                       equal to :data:`popolaloom.__version__`.
                       ``False`` when the file is missing (the doctor
                       output flags missing files separately so drift
                       only flags an *installed-but-stale* skill).
        notes:         Free-form list of human-readable diagnostics
                       (workspace rule "No Silent Failures": every
                       OSError encountered while reading the file is
                       appended here as ``"unreadable: <repr>"``).
    """

    target: str
    scope: str
    expected_path: Path
    exists: bool
    bytes: int | None
    version: str | None
    drift: bool
    notes: list[str] = field(default_factory=list)


_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _parse_skill_md_version(text: str) -> tuple[str | None, list[str]]:
    """Extract the frontmatter ``version:`` value + any parser warnings.

    Returns:
        tuple[str | None, list[str]]: ``(version, warnings)`` where
        ``version`` is ``None`` when the file lacks a parseable
        frontmatter block or no ``version:`` line is found.
    """
    notes: list[str] = []
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        notes.append("frontmatter missing or malformed")
        return None, notes
    block = match.group(1)
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if line.startswith("version:"):
            value = line.split(":", 1)[1].strip().strip("'\"")
            if not value:
                notes.append("frontmatter has empty version")
                return None, notes
            return value, notes
    notes.append("frontmatter missing version key")
    return None, notes


def _build_report(target: str, scope: str, path: Path) -> DoctorReport:
    """Assemble a :class:`DoctorReport` for one ``(target, scope)`` slot.

    All read errors are caught and surfaced via the ``notes`` list so
    the caller never has to wrap the call in ``try``.
    """
    notes: list[str] = []
    if not path.is_file():
        return DoctorReport(
            target=target,
            scope=scope,
            expected_path=path,
            exists=False,
            bytes=None,
            version=None,
            drift=False,
            notes=[f"missing: expected SKILL.md at {path}"],
        )

    try:
        raw = path.read_bytes()
    except OSError as exc:
        notes.append(f"unreadable: {exc!r}")
        return DoctorReport(
            target=target,
            scope=scope,
            expected_path=path,
            exists=True,
            bytes=None,
            version=None,
            drift=False,
            notes=notes,
        )

    text: str
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        notes.append(f"non-utf8 content: {exc!r}")
        return DoctorReport(
            target=target,
            scope=scope,
            expected_path=path,
            exists=True,
            bytes=len(raw),
            version=None,
            drift=False,
            notes=notes,
        )

    version, parse_notes = _parse_skill_md_version(text)
    notes.extend(parse_notes)
    drift = version is not None and version != __version__
    if drift:
        notes.append(
            f"version drift: installed={version!r} expected={__version__!r}"
        )

    return DoctorReport(
        target=target,
        scope=scope,
        expected_path=path,
        exists=True,
        bytes=len(raw),
        version=version,
        drift=drift,
        notes=notes,
    )


def check_skill_health(
    targets: list[str] | None = None,
) -> list[DoctorReport]:
    """Walk every (target, scope) pair in :data:`SKILL_TARGETS` and audit each.

    Args:
        targets: optional whitelist of target names; ``None`` (default)
            walks every key in :data:`SKILL_TARGETS`.  Unknown targets
            in the whitelist are silently skipped (the upstream CLI
            verb validates the input list before calling this so
            invalid input is reported with an explicit error there).

    Returns:
        list[DoctorReport]: one report per (target, scope) pair.  The
        order is stable: targets follow :data:`SKILL_TARGETS` insertion
        order, scopes within a target are sorted alphabetically.
    """
    reports: list[DoctorReport] = []
    target_iter = list(SKILL_TARGETS) if targets is None else targets
    for target in target_iter:
        if target not in SKILL_TARGETS:
            continue
        for scope in sorted(SKILL_TARGETS[target]):
            resolver = SKILL_TARGETS[target][scope]
            path = resolver()
            reports.append(_build_report(target, scope, path))
    return reports


def format_target_label(report: DoctorReport) -> str:
    """Render a one-line ``{target} {scope}`` label for table output.

    Used by both ``popola skill doctor`` and ``popola doctor``; lifted
    here so the two verbs stay byte-identical in their column
    formatting (the doctor output spec in plan §S4.E is a fixed
    layout).
    """
    return f"{report.target:<8} {report.scope:<7}"
