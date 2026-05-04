"""``popola eval`` subcommand group — PopolaLoom-nines runner (v0.2.0 Stage E E5).

Subcommands:

- ``popola eval run`` — Compute the 8-dimension PopolaLoom-nines and
  write the report to ``--output`` (default ``nines-iter2.toml`` in the
  current dir).  Reads event logs from ``--events-dir`` (default
  ``$POPOLA_HOME/events`` or ``~/.popola/events``).

The runner does NOT spawn the popolad daemon and does NOT need the
ArkTower DB to exist — it gracefully degrades to placeholder scores
(``0.5``) for dimensions whose evidence cannot be collected.

Why a separate Typer app and not a flat ``popola eval run`` command?
- Future ``popola eval diff`` (compare two reports) and ``popola eval
  history`` (list past reports) slot in cleanly.
- Group help text (``popola eval --help``) lists all eval verbs in one
  place so operators can discover the surface.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from popolaloom.evaluation import (
    DIMENSIONS,
    NinesReport,
    run_evaluation,
    toml_serialize,
)

__all__ = ["app"]


app = typer.Typer(
    name="eval",
    help="PopolaLoom self-evaluation (8-dim PopolaLoom-nines runner).",
    no_args_is_help=True,
    add_completion=False,
)


_console = Console()


def _default_output_path() -> Path:
    """Default output: ``nines-iter2.toml`` in the current working directory."""
    return Path.cwd() / "nines-iter2.toml"


def _default_events_dir() -> Path:
    """Default events dir: ``$POPOLA_HOME/events`` (or ``~/.popola/events``)."""
    import os

    home = os.environ.get("POPOLA_HOME")
    base = Path(home).expanduser().resolve() if home else Path.home() / ".popola"
    return base / "events"


@app.command()
def run(
    output: Path = typer.Option(  # noqa: B008
        None,
        "--output",
        "-o",
        help="Output TOML report path (default ./nines-iter2.toml).",
    ),
    events_dir: Path = typer.Option(  # noqa: B008
        None,
        "--events-dir",
        help="Directory of per-task NDJSON event logs (default $POPOLA_HOME/events).",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON to stdout in addition to writing TOML to --output.",
    ),
) -> None:
    """Run the 8-dim PopolaLoom-nines evaluation and write the report.

    Side effects:

    1. Walks ``--events-dir`` to count events per type.
    2. Loads ``nines.toml`` weights (fallback to baked-in defaults if missing).
    3. Scores each of the 8 dimensions in :data:`popolaloom.evaluation.DIMENSIONS`.
    4. Writes a TOML report to ``--output`` (overwrites if exists).
    5. Prints a one-line composite summary on stdout.

    No Silent Failures: if ``--output`` cannot be written, raises;
    if ``--events-dir`` does not exist, the runner falls back to
    placeholder evidence and the report still produces.
    """
    output_path = output or _default_output_path()
    events_path = events_dir or _default_events_dir()

    report = run_evaluation(events_dir=events_path)

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(toml_serialize(report), encoding="utf-8")
    except OSError as exc:
        typer.echo(f"error: could not write {output_path}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if json_out:
        typer.echo(json.dumps(report.to_dict(), ensure_ascii=False))

    typer.echo(
        f"composite={report.composite:.3f} → {output_path}",
        file=sys.stderr if json_out else None,
    )


@app.command(name="show")
def show_dimensions(
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Print the canonical list of 8 PopolaLoom-nines dimensions + their weights."""
    from popolaloom.evaluation.runner import _load_weights

    weights = _load_weights(None)

    if json_out:
        typer.echo(
            json.dumps(
                {dim.name: weights.get(dim.name, 0.0) for dim in DIMENSIONS},
                ensure_ascii=False,
            )
        )
        return

    table = Table(title="PopolaLoom-nines (8 dimensions)", show_header=True, header_style="bold")
    table.add_column("name", style="bold")
    table.add_column("weight", justify="right")

    for dim in DIMENSIONS:
        weight = weights.get(dim.name, 0.0)
        table.add_row(dim.name, f"{weight:.2f}")

    _console.print(table)


def _format_report_summary(report: NinesReport) -> str:
    """Compact one-line summary for stderr / log lines."""
    return f"composite={report.composite:.3f} dims={len(report.dimensions)}"
