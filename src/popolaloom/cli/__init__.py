"""popolaloom-cli — Typer ``popola`` console_script entry.

The ``popolad``, ``eval``, ``init``, and ``skill`` subcommand groups are
now registered inside :mod:`popolaloom.cli.main` itself (so that both
``popola`` and ``python -m popolaloom.cli.main`` show the full surface);
this package re-exports :data:`popolaloom.cli.main.app` for tests and
tooling that need the Typer app object directly (CliRunner, shell
completion, etc.).

The public surface is now ``popola dispatch / status / list / attach /
cancel / probe / popolad / eval / init / skill / doctor / version /
list-cli``.

v0.5.0 Stage S2: ``init_app`` is the ``popola init`` subcommand group
(skill installer + ``.local/`` scaffolder).
v0.5.0 Stage S4: ``skill_app`` is the ``popola skill`` subcommand
group (``install`` / ``doctor`` / ``upgrade``).  ``doctor_command`` is
the standalone ``popola doctor`` aggregate-health verb (registered as
a single command on the root app rather than a subcommand group, per
plan §S4.E).  Both surfaces are exposed here for direct CliRunner
invocation in tests.
"""

from __future__ import annotations

from popolaloom.cli.doctor_cmd import doctor_command
from popolaloom.cli.eval import app as eval_app
from popolaloom.cli.init_cmd import app as init_app
from popolaloom.cli.main import app
from popolaloom.cli.popolad import app as popolad_app
from popolaloom.cli.skill_cmd import app as skill_app

__all__ = [
    "app",
    "doctor_command",
    "eval_app",
    "init_app",
    "popolad_app",
    "skill_app",
]
