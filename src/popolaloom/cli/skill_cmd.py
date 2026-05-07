"""``popola skill`` subcommand group — install / doctor / upgrade / uninstall.

Originally landed in v0.5.0 Stage S4 with three verbs (install / doctor /
upgrade); v0.8.4 closes the long-standing gap noted in
[feedback_for_v0.8.3.md](../../../.local/feedbacks/feedback_for_v0.8.3.md)
by adding a fourth verb ``uninstall`` so the unified bash installer
``install.sh`` can compose pip + skill teardown in a single shell
command.

This module is the Typer-side counterpart to the library APIs in
:mod:`popolaloom.evolution.skill_install`,
:mod:`popolaloom.evolution.skill_doctor`,
:mod:`popolaloom.evolution.skill_upgrade`, and
:mod:`popolaloom.evolution.skill_uninstall`.  Four verbs:

* ``popola skill install   [--target=...] [--global|--project] [--dry-run] [--json]``
* ``popola skill doctor    [--target=...] [--json]``
* ``popola skill upgrade   [--target=...] [--global|--project] [--dry-run] [--json]``
* ``popola skill uninstall [--target=...] [--global|--project] [--dry-run] [--json]``

The ``--target=all`` value is a CLI convenience that loops over every
key of :data:`popolaloom.evolution.skill_inject.SKILL_TARGETS`; the
underlying library functions only accept single targets, mirroring the
``popola init`` verb pattern.

Output rendering uses :class:`rich.table.Table` to match
``popola list-cli``; the ``--json`` flag emits the raw outcomes via
:func:`dataclasses.asdict` so machine-readable consumers (CI scripts,
test harnesses) get a stable structured view.

Workspace rule "No Silent Failures": every error path that could
silently produce a partial install (e.g. one of four ``--target=all``
targets failing mid-way) emits an explicit ``error: ...`` line and
exits non-zero so the operator sees the failure rather than getting a
"3-out-of-4 silently passed" surprise.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from popolaloom.evolution.skill_doctor import DoctorReport, check_skill_health
from popolaloom.evolution.skill_inject import SKILL_TARGETS
from popolaloom.evolution.skill_install import InstallOutcome, install_skill
from popolaloom.evolution.skill_uninstall import UninstallOutcome, uninstall_skill
from popolaloom.evolution.skill_upgrade import UpgradeOutcome, upgrade_skill

__all__ = [
    "VALID_TARGET_FLAGS",
    "app",
]


app = typer.Typer(
    name="skill",
    help=(
        "Install / audit / upgrade / uninstall the PopolaLoom Skill across IDEs.\n\n"
        "Four verbs:\n"
        "  popola skill install   — copy the wheel-bundled SKILL.md into IDE targets.\n"
        "  popola skill doctor    — read-only audit of every install target's state.\n"
        "  popola skill upgrade   — force re-install (overwrites operator edits).\n"
        "  popola skill uninstall — remove the SKILL.md (+ marker) from IDE targets.\n"
    ),
    no_args_is_help=True,
    add_completion=False,
)


_console_out = Console()


VALID_TARGET_FLAGS: frozenset[str] = frozenset({*SKILL_TARGETS.keys(), "all"})
"""Accepted ``--target`` values (registry keys + the ``all`` shorthand)."""


def _resolve_scope(global_: bool, project: bool, *, default: str = "global") -> str:
    """Resolve ``--global`` / ``--project`` to a single ``scope`` string.

    Raises:
        typer.BadParameter: when both flags are passed (S-5: explicit
            conflict error rather than silent last-flag-wins).
    """
    if global_ and project:
        raise typer.BadParameter("--global and --project are mutually exclusive")
    if global_:
        return "global"
    if project:
        return "project"
    return default


def _validate_target_flag(target: str) -> None:
    """Raise :class:`typer.BadParameter` when ``target`` is not in :data:`VALID_TARGET_FLAGS`."""
    if target not in VALID_TARGET_FLAGS:
        valid = ", ".join(sorted(VALID_TARGET_FLAGS))
        raise typer.BadParameter(
            f"--target must be one of {valid}; got {target!r}"
        )


def _expand_target_list(target: str) -> list[str]:
    """Expand the user-supplied ``--target`` flag into a concrete list of registry keys."""
    if target == "all":
        return list(SKILL_TARGETS.keys())
    return [target]


def _outcome_status_text(outcome: InstallOutcome) -> Text:
    """Render the action column for an :class:`InstallOutcome`."""
    if outcome.would_write is not None:
        return Text("DRY", style="cyan")
    if outcome.installed:
        return Text("OK", style="green")
    if outcome.skipped:
        return Text("SKIP", style="yellow")
    return Text("?", style="red")


def _upgrade_status_text(outcome: UpgradeOutcome) -> Text:
    """Render the action column for an :class:`UpgradeOutcome`."""
    if outcome.would_write is not None:
        return Text("DRY", style="cyan")
    if outcome.replaced:
        return Text("REPLACED", style="green")
    if outcome.up_to_date:
        return Text("UP-TO-DATE", style="dim")
    if outcome.installed:
        return Text("INSTALLED", style="green")
    return Text("?", style="red")


def _doctor_status_text(report: DoctorReport) -> tuple[Text, str]:
    """Render the action column + the (version) suffix for a :class:`DoctorReport`."""
    from popolaloom import __version__

    if not report.exists:
        return Text("MISS", style="red"), f"expected v{__version__}"
    if report.drift:
        return (
            Text("DRIFT", style="yellow"),
            f"v{report.version} (expected v{__version__})",
        )
    return Text("OK", style="green"), f"v{report.version or __version__}"


def _uninstall_status_text(outcome: UninstallOutcome) -> Text:
    """Render the action column for an :class:`UninstallOutcome`."""
    if outcome.would_remove is not None:
        return Text("DRY", style="cyan")
    if outcome.uninstalled:
        return Text("REMOVED", style="green")
    if outcome.skipped:
        return Text("ABSENT", style="yellow")
    return Text("?", style="red")


# ── install verb ─────────────────────────────────────────────────────────


@app.command("install")
def cmd_install(
    target: str = typer.Option(
        "all",
        "--target",
        help="cursor / claude / codex / copilot / all (default).",
    ),
    global_: bool = typer.Option(
        False,
        "--global",
        help="Install into the user's home directory (default).",
    ),
    project: bool = typer.Option(
        False,
        "--project",
        help="Install into the project's local <cwd>/.<ide>/ directory.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print every path that would be written without touching the filesystem.",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit the install outcomes as a JSON array (machine-readable).",
    ),
) -> None:
    """Install the PopolaLoom SKILL.md into one or more IDE targets."""
    _validate_target_flag(target)
    scope = _resolve_scope(global_, project, default="global")
    targets = _expand_target_list(target)

    outcomes = [install_skill(t, scope=scope, dry_run=dry_run) for t in targets]

    if json_out:
        typer.echo(
            json.dumps(
                [_outcome_to_jsonable(o) for o in outcomes],
                ensure_ascii=False,
            )
        )
        return

    table = Table(
        title=f"popola skill install — scope={scope}{' (dry-run)' if dry_run else ''}",
        show_header=True,
        header_style="bold",
    )
    table.add_column("target", style="bold")
    table.add_column("scope")
    table.add_column("path")
    table.add_column("action")
    table.add_column("note")

    for outcome in outcomes:
        table.add_row(
            outcome.target,
            outcome.scope,
            str(outcome.target_path),
            _outcome_status_text(outcome),
            outcome.reason or "",
        )
    _console_out.print(table)


# ── doctor verb ──────────────────────────────────────────────────────────


@app.command("doctor")
def cmd_doctor(
    target: str = typer.Option(
        "all",
        "--target",
        help="cursor / claude / codex / copilot / all (default).",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit the doctor reports as a JSON array (machine-readable).",
    ),
) -> None:
    """Audit every (target, scope) install and report drift / missing files."""
    _validate_target_flag(target)
    target_list = _expand_target_list(target)

    reports = check_skill_health(targets=target_list)

    if json_out:
        typer.echo(
            json.dumps(
                [_report_to_jsonable(r) for r in reports],
                ensure_ascii=False,
            )
        )
        return

    table = Table(
        title="popola skill doctor",
        show_header=True,
        header_style="bold",
    )
    table.add_column("target", style="bold")
    table.add_column("scope")
    table.add_column("path")
    table.add_column("status")
    table.add_column("version")

    for report in reports:
        action_text, version_text = _doctor_status_text(report)
        table.add_row(
            report.target,
            report.scope,
            str(report.expected_path),
            action_text,
            version_text,
        )
    _console_out.print(table)


# ── upgrade verb ─────────────────────────────────────────────────────────


@app.command("upgrade")
def cmd_upgrade(
    target: str = typer.Option(
        "all",
        "--target",
        help="cursor / claude / codex / copilot / all (default).",
    ),
    global_: bool = typer.Option(
        False,
        "--global",
        help="Upgrade the user-home install (default).",
    ),
    project: bool = typer.Option(
        False,
        "--project",
        help="Upgrade the project-local install.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print every path that would be written without touching the filesystem.",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit the upgrade outcomes as a JSON array (machine-readable).",
    ),
) -> None:
    """Force re-install the PopolaLoom SKILL.md (overwrites operator edits)."""
    _validate_target_flag(target)
    scope = _resolve_scope(global_, project, default="global")
    targets = _expand_target_list(target)

    outcomes = [upgrade_skill(t, scope=scope, dry_run=dry_run) for t in targets]

    if json_out:
        typer.echo(
            json.dumps(
                [_upgrade_to_jsonable(o) for o in outcomes],
                ensure_ascii=False,
            )
        )
        return

    table = Table(
        title=f"popola skill upgrade — scope={scope}{' (dry-run)' if dry_run else ''}",
        show_header=True,
        header_style="bold",
    )
    table.add_column("target", style="bold")
    table.add_column("scope")
    table.add_column("path")
    table.add_column("action")
    table.add_column("from → to")

    for outcome in outcomes:
        version_change = _format_version_change(
            outcome.previous_version, outcome.new_version
        )
        table.add_row(
            outcome.target,
            outcome.scope,
            str(outcome.target_path),
            _upgrade_status_text(outcome),
            version_change,
        )
    _console_out.print(table)


# ── uninstall verb ───────────────────────────────────────────────────────


@app.command("uninstall")
def cmd_uninstall(
    target: str = typer.Option(
        "all",
        "--target",
        help="cursor / claude / codex / copilot / all (default).",
    ),
    global_: bool = typer.Option(
        False,
        "--global",
        help="Remove the user-home install (default).",
    ),
    project: bool = typer.Option(
        False,
        "--project",
        help="Remove the project-local install.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print every path that would be removed without touching the filesystem.",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit the uninstall outcomes as a JSON array (machine-readable).",
    ),
) -> None:
    """Remove the PopolaLoom SKILL.md from one or more IDE targets.

    Idempotent: re-running ``uninstall`` on a target whose SKILL.md is
    already absent is a no-op (the outcome reports ``ABSENT`` rather
    than failing).  The companion ``.popola-loom-version`` marker is
    removed alongside the SKILL.md for cursor / claude / codex; copilot
    has no marker (single-file install).
    """
    _validate_target_flag(target)
    scope = _resolve_scope(global_, project, default="global")
    targets = _expand_target_list(target)

    outcomes = [uninstall_skill(t, scope=scope, dry_run=dry_run) for t in targets]

    if json_out:
        typer.echo(
            json.dumps(
                [_uninstall_to_jsonable(o) for o in outcomes],
                ensure_ascii=False,
            )
        )
        return

    table = Table(
        title=f"popola skill uninstall — scope={scope}{' (dry-run)' if dry_run else ''}",
        show_header=True,
        header_style="bold",
    )
    table.add_column("target", style="bold")
    table.add_column("scope")
    table.add_column("path")
    table.add_column("action")
    table.add_column("note")

    for outcome in outcomes:
        note = "marker removed" if outcome.removed_marker else (outcome.reason or "")
        table.add_row(
            outcome.target,
            outcome.scope,
            str(outcome.target_path),
            _uninstall_status_text(outcome),
            note,
        )
    _console_out.print(table)


# ── helpers ──────────────────────────────────────────────────────────────


def _format_version_change(prev: str | None, new: str | None) -> str:
    """Render ``previous → new`` for the upgrade table."""
    prev_str = f"v{prev}" if prev else "(none)"
    new_str = f"v{new}" if new else "(unknown)"
    return f"{prev_str} → {new_str}"


def _outcome_to_jsonable(outcome: InstallOutcome) -> dict[str, Any]:
    """Convert :class:`InstallOutcome` to a JSON-friendly dict (paths → strings)."""
    payload = asdict(outcome)
    return _stringify_paths(payload)


def _upgrade_to_jsonable(outcome: UpgradeOutcome) -> dict[str, Any]:
    """Convert :class:`UpgradeOutcome` to a JSON-friendly dict (paths → strings)."""
    payload = asdict(outcome)
    return _stringify_paths(payload)


def _uninstall_to_jsonable(outcome: UninstallOutcome) -> dict[str, Any]:
    """Convert :class:`UninstallOutcome` to a JSON-friendly dict (paths → strings)."""
    payload = asdict(outcome)
    return _stringify_paths(payload)


def _report_to_jsonable(report: DoctorReport) -> dict[str, Any]:
    """Convert :class:`DoctorReport` to a JSON-friendly dict (paths → strings)."""
    payload = asdict(report)
    return _stringify_paths(payload)


def _stringify_paths(payload: dict[str, Any]) -> dict[str, Any]:
    """Walk ``payload`` and convert any :class:`Path` values to ``str``."""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, Path):
            out[key] = str(value)
        else:
            out[key] = value
    return out
