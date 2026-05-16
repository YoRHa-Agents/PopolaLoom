"""``popola update`` verb — Python equivalent of ``install.sh update`` (v1.4.0).

Single-verb Typer module that wraps
:func:`popolaloom.evolution.self_update.update_all` for the operator-facing
CLI.  Mirrors the structure of :mod:`popolaloom.cli.skill_cmd` (validation
helpers + ``--json`` switch + Rich table rendering) so the two surfaces feel
the same on the command line.

Verb shape (matches [install.sh](../../../install.sh) ``verb_update`` flag
matrix at lines 502-525)::

    popola update [--target=cursor|claude|codex|copilot|all]
                  [--scope=global|project|both]
                  [--from=git|pypi|<PATH>]
                  [--ref=<tag|branch|sha>]
                  [--version=X.Y.Z]
                  [--python=<bin>]
                  [--no-skills]
                  [--no-doctor]
                  [--with-credentials]
                  [--force]
                  [--dry-run]
                  [--quiet]
                  [--json]

Defaults:

* ``--target=all`` — every IDE skill target.
* ``--scope=both`` — global + project (mirrors install.sh's two-pass pattern
  but does it in one invocation; rationale in the v1.4.0 ``popola update``
  feature plan).
* ``--from=git`` — tracks ``main`` (matches install.sh v0.9.6+ default).
* The remaining flags default to ``False`` / ``None`` so a no-flag
  invocation reproduces ``install.sh update`` exactly.

Workspace rules honoured:

* "No Silent Failures": every refusal path
  (:class:`popolaloom.evolution.self_update.UnsafeInstallError`,
  :class:`popolaloom.evolution.self_update.PipUpgradeError`, ``ValueError``
  from spec / target / scope validation) renders a one-line ``error: ...``
  message and exits non-zero (exit ``1`` for spec / IO failures, ``2`` for
  unsafe install kinds — matches Typer's ``BadParameter`` exit code).
* "Always use braces for if": N/A (Python).
* "Mandatory Verification": companion test file is
  ``tests/cli/test_update_cmd.py`` — the verb is exercised end-to-end via
  Typer's :class:`typer.testing.CliRunner` (no real pip subprocess; the
  helper :func:`run_pip_upgrade` is monkey-patched in tests).
"""

from __future__ import annotations

import json
import sys

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from popolaloom import __version__
from popolaloom.cli.skill_cmd import VALID_TARGET_FLAGS
from popolaloom.evolution.self_update import (
    PipUpgradeError,
    UnsafeInstallError,
    UpdateConfig,
    UpdateOutcome,
    outcome_to_json,
    update_all,
)

__all__ = ["app", "VALID_SCOPE_FLAGS", "VALID_FROM_PROTOCOLS"]


app = typer.Typer(
    name="update",
    help=(
        "Upgrade popolaloom + refresh installed Skills (Python equivalent of "
        "`install.sh update`).\n\n"
        "Default: pip-upgrades the wheel from the GitHub `main` branch, then "
        "force-reinstalls the SKILL.md into every IDE target (both global + "
        "project scopes), then runs `popola doctor` to surface any residual "
        "drift. Pass `--dry-run` to plan the steps without spawning pip or "
        "writing any skill files."
    ),
    no_args_is_help=False,
    add_completion=False,
    invoke_without_command=True,
)


_console_out = Console()
_console_err = Console(stderr=True)


VALID_SCOPE_FLAGS: frozenset[str] = frozenset({"global", "project", "both"})
"""Accepted ``--scope`` values; ``both`` is the v1.4.0 default."""

VALID_FROM_PROTOCOLS: frozenset[str] = frozenset({"git", "pypi"})
"""Recognised ``--from`` shorthand keywords. Anything else is treated as a
local filesystem path / non-git URL (matches install.sh:resolve_install_spec
fall-through at lines 422-435)."""


def _validate_target_flag(target: str) -> None:
    if target not in VALID_TARGET_FLAGS:
        valid = ", ".join(sorted(VALID_TARGET_FLAGS))
        raise typer.BadParameter(
            f"--target must be one of {valid}; got {target!r}"
        )


def _validate_scope_flag(scope: str) -> None:
    if scope not in VALID_SCOPE_FLAGS:
        valid = ", ".join(sorted(VALID_SCOPE_FLAGS))
        raise typer.BadParameter(
            f"--scope must be one of {valid}; got {scope!r}"
        )


def _validate_from_flag(from_: str) -> None:
    if not from_.strip():
        raise typer.BadParameter("--from must be a non-empty string")
    # ``git`` / ``pypi`` are well-known shortcuts; any other value is treated
    # as a local filesystem path (mirrors install.sh and pip's own behaviour).


def _emit_outcome_table(outcome: UpdateOutcome) -> None:
    """Render the human-friendly table form."""
    cfg = outcome.config

    header = Table.grid(padding=(0, 1))
    header.add_column(style="bold")
    header.add_column()
    header.add_row("popola update", f"v{__version__}")
    header.add_row("install kind", outcome.install_kind.value)
    header.add_row("pip spec", outcome.spec)
    if cfg.dry_run:
        header.add_row("mode", Text("dry-run", style="cyan"))
    _console_out.print(header)

    if outcome.pip is not None:
        pip_table = Table(
            title="step 1 — pip install --upgrade",
            show_header=True,
            header_style="bold",
        )
        pip_table.add_column("step")
        pip_table.add_column("status")
        pip_table.add_column("detail")
        if outcome.pip.dry_run:
            pip_table.add_row(
                "pip",
                Text("DRY", style="cyan"),
                " ".join(outcome.pip.argv),
            )
        else:
            pip_table.add_row(
                "pip",
                Text("OK", style="green"),
                f"exit={outcome.pip.returncode}",
            )
        _console_out.print(pip_table)

    if outcome.skills:
        skill_table = Table(
            title=(
                f"step 2 — popola skill upgrade — target={cfg.target} "
                f"scope={cfg.scope}{' (dry-run)' if cfg.dry_run else ''}"
            ),
            show_header=True,
            header_style="bold",
        )
        skill_table.add_column("target", style="bold")
        skill_table.add_column("scope")
        skill_table.add_column("path")
        skill_table.add_column("action")
        skill_table.add_column("from -> to")
        for s in outcome.skills:
            if s.would_write is not None:
                action = Text("DRY", style="cyan")
            elif s.replaced:
                action = Text("REPLACED", style="green")
            elif s.up_to_date:
                action = Text("UP-TO-DATE", style="dim")
            elif s.installed:
                action = Text("INSTALLED", style="green")
            else:
                action = Text("?", style="red")
            prev = f"v{s.previous_version}" if s.previous_version else "(none)"
            new = f"v{s.new_version}" if s.new_version else "(unknown)"
            skill_table.add_row(
                s.target,
                s.scope,
                str(s.target_path),
                action,
                f"{prev} -> {new}",
            )
        _console_out.print(skill_table)

    if outcome.doctor:
        doc_table = Table(
            title="step 3 — popola doctor",
            show_header=True,
            header_style="bold",
        )
        doc_table.add_column("target", style="bold")
        doc_table.add_column("scope")
        doc_table.add_column("path")
        doc_table.add_column("status")
        doc_table.add_column("version")
        for r in outcome.doctor:
            if not r.exists:
                status = Text("MISS", style="red")
                ver = f"expected v{__version__}"
            elif r.drift:
                status = Text("DRIFT", style="yellow")
                ver = f"v{r.version} (expected v{__version__})"
            else:
                status = Text("OK", style="green")
                ver = f"v{r.version or __version__}"
            doc_table.add_row(
                r.target,
                r.scope,
                str(r.expected_path),
                status,
                ver,
            )
        _console_out.print(doc_table)

    for w in outcome.warnings:
        _console_err.print(f"[yellow]warn:[/yellow] {w}")


@app.callback()
def cmd_update(
    ctx: typer.Context,
    target: str = typer.Option(
        "all",
        "--target",
        help="cursor / claude / codex / copilot / all (default).",
    ),
    scope: str = typer.Option(
        "both",
        "--scope",
        help="global / project / both (default).",
    ),
    from_: str = typer.Option(
        "git",
        "--from",
        help=(
            "git (default; tracks main) / pypi / <PATH>. "
            "Pass --ref=<tag|branch|sha> with --from=git or "
            "--version=X.Y.Z with --from=pypi."
        ),
    ),
    ref: str | None = typer.Option(
        None,
        "--ref",
        help="git tag / branch / sha pin (only valid with --from=git).",
    ),
    version: str | None = typer.Option(
        None,
        "--version",
        help="PyPI version pin X.Y.Z (only valid with --from=pypi).",
    ),
    python: str | None = typer.Option(
        None,
        "--python",
        help="Override Python interpreter for the pip subprocess (default: sys.executable).",
    ),
    no_skills: bool = typer.Option(
        False,
        "--no-skills",
        help="Skip the skill upgrade phase.",
    ),
    no_doctor: bool = typer.Option(
        False,
        "--no-doctor",
        help="Skip the post-upgrade `popola doctor` probe.",
    ),
    with_credentials: bool = typer.Option(
        False,
        "--with-credentials",
        help=(
            "Append the [credentials] extra (Python keyring>=25) so "
            "`popola auth cursor set` keeps working after the upgrade."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Override the editable / pipx refusal. Will leave a stale .pth "
            "entry (editable) or break pipx pin tracking (pipx) — use only "
            "when you understand the trade-off."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print every step without spawning pip or writing skill files.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress the human-readable table output (errors still go to stderr).",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit the update outcome as a JSON object (machine-readable).",
    ),
) -> None:
    """Upgrade popolaloom + refresh installed Skills.

    Equivalent to running, in order:

      1. ``python -m pip install --upgrade <spec>``  — wheel upgrade.
      2. ``popola skill upgrade --target=<X> --global``  — for every
         (target, global) pair the requested ``--target`` selects.
      3. ``popola skill upgrade --target=<X> --project``  — for every
         (target, project) pair, when ``--scope`` is ``both`` (default)
         or ``project``.
      4. ``popola doctor``  — surface any residual drift.

    Refuses to run on editable / pipx-managed installs unless
    ``--force`` is set; see :class:`popolaloom.evolution.self_update.UnsafeInstallError`
    for the rationale.
    """
    if ctx.invoked_subcommand is not None:
        # The verb has no subcommands; this branch is unreachable in practice
        # but kept defensive in case a future refactor adds one.
        return

    _validate_target_flag(target)
    _validate_scope_flag(scope)
    _validate_from_flag(from_)

    if ref is not None and from_ != "git":
        raise typer.BadParameter(
            f"--ref={ref!r} requires --from=git (got --from={from_!r})"
        )
    if version is not None and from_ != "pypi":
        raise typer.BadParameter(
            f"--version={version!r} requires --from=pypi (got --from={from_!r})"
        )

    config = UpdateConfig(
        target=target,
        scope=scope,
        from_=from_,
        ref=ref,
        version=version,
        python=python,
        no_skills=no_skills,
        no_doctor=no_doctor,
        with_credentials=with_credentials,
        dry_run=dry_run,
        force=force,
    )

    try:
        outcome = update_all(config)
    except UnsafeInstallError as exc:
        _console_err.print(
            f"[red]error:[/red] {exc.probe.kind.value} install detected; refusing to run."
        )
        _console_err.print(f"  hint: {exc.hint}")
        raise typer.Exit(code=2) from exc
    except PipUpgradeError as exc:
        _console_err.print(
            f"[red]error:[/red] pip install --upgrade failed (exit {exc.outcome.returncode})."
        )
        if exc.outcome.stderr.strip():
            tail = "\n".join(exc.outcome.stderr.splitlines()[-10:])
            _console_err.print("  pip stderr (last 10 lines):")
            for line in tail.splitlines():
                _console_err.print(f"    {line}")
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        # Spec / target / scope validation errors from the orchestrator.
        _console_err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if json_out:
        typer.echo(json.dumps(outcome_to_json(outcome), ensure_ascii=False))
        return

    if quiet:
        for w in outcome.warnings:
            _console_err.print(f"[yellow]warn:[/yellow] {w}")
        return

    _emit_outcome_table(outcome)

    # Final exit code: non-zero when skill doctor reports DRIFT or MISS even
    # after the upgrade (catches the edge case where a target's permission
    # error silently left it stale — surfaces it via exit code).
    drift_or_miss = any(
        r.drift or not r.exists for r in outcome.doctor if r.target != "copilot" or r.exists
    )
    if drift_or_miss and not config.dry_run:
        raise typer.Exit(code=3)


if __name__ == "__main__":  # pragma: no cover — Typer test entry point.
    sys.exit(app())
