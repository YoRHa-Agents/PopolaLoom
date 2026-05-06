"""``popola handoff`` subcommand group — v0.7.2 patch 2.

Surface:

- ``popola handoff list``   — enumerate active envelopes in
  ``$POPOLA_HANDOFF_DIR`` (or ``.local/.agent/handoff/``) sorted by mtime
  desc; prints a Rich table.
- ``popola handoff show <id>`` — print the raw Markdown envelope (cat-friendly
  by design — Q1=A4); ``--json`` re-serialises the parsed model as JSON.
- ``popola handoff archive <id> <task_id>`` — copy the active envelope to
  ``<archive_root>/<task_id>/<id>.md`` (D4 audit snapshot).  No-op if the
  destination already has the same content.

All three operate purely on the local filesystem (no daemon required) —
they are read-side cousins of the ``write_envelope`` / ``archive_envelope``
helpers under :mod:`popolaloom.handoff`.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from popolaloom.handoff import (
    HandoffSummary,
    archive_envelope,
    list_active_envelopes,
    load_envelope,
    resolve_envelope_path,
)

app = typer.Typer(
    name="handoff",
    help="Inspect or archive on-disk handoff envelopes (no daemon required).",
    no_args_is_help=True,
    add_completion=False,
)

_console = Console()


def _format_size(n: int) -> str:
    """Render a byte count as e.g. ``1.2 KB`` / ``45 B``."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


@app.command(name="list")
def list_cmd(
    handoff_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--handoff-dir",
        help="Override active root (else $POPOLA_HANDOFF_DIR or .local/.agent/handoff/).",
    ),
    *,
    json_output: bool = typer.Option(  # noqa: B008
        False, "--json", help="Emit JSON instead of a Rich table."
    ),
) -> None:
    """List active envelopes in the handoff dir, newest first."""
    summaries: list[HandoffSummary] = list_active_envelopes(base_dir=handoff_dir)

    if json_output:
        payload = [
            {
                "handoff_id": s.handoff_id,
                "path": str(s.path),
                "size_bytes": s.size_bytes,
                "mtime": s.mtime.isoformat(),
            }
            for s in summaries
        ]
        typer.echo(json.dumps(payload, indent=2))
        return

    if not summaries:
        _console.print("[yellow]No active envelopes.[/yellow]")
        return

    table = Table(title="Active handoff envelopes", show_lines=False)
    table.add_column("handoff_id", style="cyan", no_wrap=True)
    table.add_column("size", justify="right")
    table.add_column("mtime", style="dim")
    table.add_column("path", style="dim")
    for s in summaries:
        table.add_row(
            s.handoff_id,
            _format_size(s.size_bytes),
            s.mtime.strftime("%Y-%m-%d %H:%M:%S"),
            str(s.path),
        )
    _console.print(table)


@app.command(name="show")
def show_cmd(
    handoff_id: str = typer.Argument(..., help="Slug-hash id (no .md suffix)."),
    handoff_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--handoff-dir",
        help="Override active root.",
    ),
    *,
    json_output: bool = typer.Option(  # noqa: B008
        False, "--json", help="Re-serialise the parsed envelope as JSON."
    ),
) -> None:
    """Print the active envelope for ``handoff_id``.

    Default mode prints the raw Markdown front-matter file (Q1=A4 — the
    cat-friendly form).  ``--json`` parses the file via
    :class:`HandoffEnvelope` and prints the validated model as JSON
    (useful for piping into ``jq`` etc.).
    """
    try:
        path = resolve_envelope_path(handoff_id, base_dir=handoff_dir)
    except ValueError as exc:
        typer.echo(f"error: invalid handoff_id: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if not path.is_file():
        typer.echo(f"error: handoff envelope not found: {path}", err=True)
        raise typer.Exit(code=1)

    if json_output:
        env = load_envelope(handoff_id, base_dir=handoff_dir)
        # Pydantic v2 model_dump_json gives clean JSON; mode=json normalises
        # datetime → ISO 8601 string in the same shape as front-matter.
        typer.echo(env.model_dump_json(indent=2))
        return

    typer.echo(path.read_text(encoding="utf-8"), nl=False)


@app.command(name="archive")
def archive_cmd(
    handoff_id: str = typer.Argument(..., help="Slug-hash id (no .md suffix)."),
    task_id: str = typer.Argument(
        ..., help="Popola task id — destination grouping under the archive root."
    ),
    handoff_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--handoff-dir",
        help="Override active root (source).",
    ),
    archive_root: Path | None = typer.Option(  # noqa: B008
        None,
        "--archive-root",
        help="Override archive root (else .local/.agent/archive/).",
    ),
) -> None:
    """Copy the active envelope to ``<archive_root>/<task_id>/<id>.md`` (D4).

    Idempotent: re-archiving the same envelope to the same destination is
    a safe overwrite.  The active source file is NOT deleted (audit
    snapshot semantics — see :func:`popolaloom.handoff.archive_envelope`).
    """
    try:
        src = resolve_envelope_path(handoff_id, base_dir=handoff_dir)
    except ValueError as exc:
        typer.echo(f"error: invalid handoff_id: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if not src.is_file():
        typer.echo(f"error: source envelope missing: {src}", err=True)
        raise typer.Exit(code=1)

    try:
        dest = archive_envelope(src, task_id, archive_root=archive_root)
    except ValueError as exc:
        typer.echo(f"error: archive rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(str(dest))
