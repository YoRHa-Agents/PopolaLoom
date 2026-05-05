"""popolaloom-cli — Typer ``popola`` console_script entry.

The ``popolad``, ``eval``, and ``init`` subcommand groups are now
registered inside :mod:`popolaloom.cli.main` itself (so that both
``popola`` and ``python -m popolaloom.cli.main`` show the full surface);
this package re-exports :data:`popolaloom.cli.main.app` for tests and
tooling that need the Typer app object directly (CliRunner, shell
completion, etc.).

The public surface is now ``popola dispatch / status / list / attach /
cancel / probe / popolad / eval / init / version / list-cli``.

v0.5.0 Stage S2: ``init_app`` is the new ``popola init`` subcommand
group (skill installer + ``.local/`` scaffolder), exposed here for
test ``CliRunner`` direct invocation.
"""

from __future__ import annotations

from popolaloom.cli.eval import app as eval_app
from popolaloom.cli.init_cmd import app as init_app
from popolaloom.cli.main import app
from popolaloom.cli.popolad import app as popolad_app

__all__ = ["app", "eval_app", "init_app", "popolad_app"]
