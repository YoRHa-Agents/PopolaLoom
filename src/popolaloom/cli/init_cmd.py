"""``popola init`` subcommand group — install PopolaLoom skill into IDEs.

Stage S2 of v0.5.0 (closes the v0.4.1 → v0.5.0 plan §4 Stage S2).
Mirrors DevolaFlow's ``devola-init`` 14-row verb + 8-modifier flag
matrix (per Q5-2 lock); the reference implementation lives at
``/root/miniforge/lib/python3.12/site-packages/devolaflow/init_project.py``
(read-only).

Verb matrix (8 rows, mirrored from the locked plan §4):

+----+--------------------------------+------------------------------------+
| #  | Invocation                     | Behaviour                          |
+====+================================+====================================+
| 1  | ``popola init`` (no args)      | ``_auto_detect`` + dispatch each   |
+----+--------------------------------+------------------------------------+
| 2  | ``popola init cursor``         | install Cursor target              |
+----+--------------------------------+------------------------------------+
| 3  | ``popola init claude``         | install Claude Code target         |
+----+--------------------------------+------------------------------------+
| 4  | ``popola init copilot``        | install Copilot single-file target |
+----+--------------------------------+------------------------------------+
| 5  | ``popola init codex``          | install Codex target               |
+----+--------------------------------+------------------------------------+
| 6  | ``popola init local``          | scaffold .local/ workspace         |
+----+--------------------------------+------------------------------------+
| 7  | ``popola init all``            | every entry except ``local``       |
+----+--------------------------------+------------------------------------+
| 8  | ``popola init --list``         | print detected tools + exit        |
+----+--------------------------------+------------------------------------+

Modifier matrix (8 flags):

* ``--global`` / ``--project`` — install scope (default project except
  copilot which is project-only).
* ``--no-compile`` — skip rule compile after ``local`` scaffold (Stage
  S2 has no rule compile yet so the flag is accepted + recorded but
  is a no-op; the .rules/ compile chain is deferred to v0.6.0 per the
  plan §S2 risk note).
* ``--with-examples`` / ``--no-with-examples`` — seed example tasks
  during ``local`` scaffold.
* ``--mode={core,standard,full}`` — shorthand: ``core`` ⇒
  ``--no-compile --no-with-examples``; ``full`` ⇒ ``--with-examples``
  + compile-on; ``standard`` ⇒ defaults.  Individual flags override
  ``--mode`` (explicit-beats-implicit, mirrors DevolaFlow §165-191).
* ``--dry-run`` — print every path that *would* be written without
  touching the filesystem.

Idempotency contract: every install verb gates writes by
``Path.exists()``; second runs print ``SKIP <path> (already
installed)`` instead of overwriting.  Tests ``test_init_cmd.py``
verify the contract end-to-end with ``tmp_path``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from popolaloom.cli._skill_source import resolve_skill_source

__all__ = ["app"]


app = typer.Typer(
    name="init",
    help=(
        "Install + register the PopolaLoom skill into IDEs and scaffold the "
        ".local/ workspace.\n\n"
        "8 verb forms (mirrors DevolaFlow per Q5-2 lock):\n"
        "  popola init                — auto-detect targets from cwd\n"
        "  popola init cursor         — install Cursor target\n"
        "  popola init claude         — install Claude Code target\n"
        "  popola init copilot        — install Copilot target (project only)\n"
        "  popola init codex          — install Codex target ($CODEX_HOME)\n"
        "  popola init local          — scaffold .local/ workspace\n"
        "  popola init all            — every target except local\n"
        "  popola init --list         — print detected tools + exit\n"
    ),
    no_args_is_help=False,
    add_completion=False,
    invoke_without_command=True,
)


_console_out = Console()


VALID_MODES: frozenset[str] = frozenset({"core", "standard", "full"})

VALID_TARGETS: frozenset[str] = frozenset(
    {"cursor", "claude", "copilot", "codex", "local"}
)


# ── path resolvers ────────────────────────────────────────────────────────


def cursor_target_path(scope: str, cwd: Path | None = None) -> Path:
    """Resolve the Cursor SKILL.md install path for ``scope``.

    Args:
        scope: ``"global"`` → ``~/.cursor/skills/popolaloom/SKILL.md``;
            ``"project"`` (default) → ``<cwd>/.cursor/skills/popolaloom/SKILL.md``.
        cwd: project root (defaults to :func:`Path.cwd`).
    """
    base_dir = Path.home() / ".cursor" if scope == "global" else (cwd or Path.cwd()) / ".cursor"
    return base_dir / "skills" / "popolaloom" / "SKILL.md"


def claude_target_path(scope: str, cwd: Path | None = None) -> Path:
    """Resolve the Claude Code SKILL.md install path for ``scope``."""
    base_dir = Path.home() / ".claude" if scope == "global" else (cwd or Path.cwd()) / ".claude"
    return base_dir / "skills" / "popolaloom" / "SKILL.md"


def copilot_target_path(cwd: Path | None = None) -> Path:
    """Resolve the GitHub Copilot single-file install path.

    Copilot is *always* project-local: ``<cwd>/.github/copilot-instructions.md``.
    The ``--global`` modifier prints a warning + falls back to project
    (mirrors DevolaFlow `init_project.py:222-226`).
    """
    return (cwd or Path.cwd()) / ".github" / "copilot-instructions.md"


def codex_target_path() -> Path:
    """Resolve the Codex SKILL.md install path.

    Honours ``$CODEX_HOME`` per the Codex CLI convention; falls back to
    ``~/.codex/`` when unset.
    """
    import os

    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    return codex_home / "skills" / "popolaloom" / "SKILL.md"


# ── auto-detect ──────────────────────────────────────────────────────────


def _auto_detect(cwd: Path) -> list[str]:
    """Return the list of targets to install based on filesystem clues.

    Mirrors DevolaFlow `init_project.py:707-727`:

    * ``.cursor/`` exists ⇒ ``cursor``
    * ``.claude/`` exists ⇒ ``claude``
    * ``.github/`` exists ⇒ ``copilot``
    * ``~/.codex/`` exists ⇒ ``codex``
    * ``.local/`` is absent ⇒ ``local`` (so a fresh repo gets scaffolded
      on the first ``popola init`` invocation; idempotent thereafter)
    """
    found: list[str] = []
    if (cwd / ".cursor").is_dir():
        found.append("cursor")
    if (cwd / ".claude").is_dir():
        found.append("claude")
    if (cwd / ".github").is_dir():
        found.append("copilot")
    if Path.home().joinpath(".codex").is_dir():
        found.append("codex")
    if not (cwd / ".local").is_dir():
        found.append("local")
    return found


# ── mode resolver ────────────────────────────────────────────────────────


def _resolve_mode(
    mode: str | None,
    *,
    no_compile_flag: bool,
    with_examples_flag: bool | None,
) -> tuple[bool, bool]:
    """Return ``(no_compile, with_examples)`` after applying ``--mode``.

    Individual flags OVERRIDE the mode-derived default
    (explicit-beats-implicit; mirrors DevolaFlow `init_project.py:165-191`).

    * ``--mode=core``     → defaults ``(no_compile=True,  with_examples=False)``
    * ``--mode=standard`` → defaults ``(no_compile=False, with_examples=False)``
    * ``--mode=full``     → defaults ``(no_compile=False, with_examples=True)``
    * ``mode=None``       → defaults ``(no_compile=False, with_examples=False)``

    Args:
        mode: the parsed ``--mode=`` value (``None`` when absent).
        no_compile_flag: ``True`` iff ``--no-compile`` was explicitly
            passed (overrides the mode default).
        with_examples_flag: ``True`` iff ``--with-examples`` was passed,
            ``False`` iff ``--no-with-examples`` was passed, ``None``
            iff neither (use the mode default).

    Raises:
        typer.BadParameter: when ``mode`` is non-``None`` and not in
            :data:`VALID_MODES` (S-5: explicit error state, never a
            silent fallback to ``standard``).
    """
    if mode is not None and mode not in VALID_MODES:
        valid = ", ".join(sorted(VALID_MODES))
        raise typer.BadParameter(f"--mode must be one of {valid} (got {mode!r})")

    if mode == "core":
        default_no_compile, default_with_examples = True, False
    elif mode == "full":
        default_no_compile, default_with_examples = False, True
    elif mode == "standard":
        default_no_compile, default_with_examples = False, False
    else:
        default_no_compile, default_with_examples = False, False

    no_compile = no_compile_flag or default_no_compile
    with_examples = (
        default_with_examples if with_examples_flag is None else with_examples_flag
    )
    return no_compile, with_examples


# ── core install helpers ─────────────────────────────────────────────────


def _write_skill(target: Path, content: str, *, dry_run: bool) -> str:
    """Write ``content`` to ``target`` and return the action taken.

    Returns one of the literal strings ``"OK"`` (wrote new file),
    ``"SKIP"`` (target already exists, no overwrite — idempotent), or
    ``"DRY"`` (``dry_run=True``, no write occurred).  Tests assert on
    the literal so behavioural changes surface as test diffs.
    """
    if dry_run:
        typer.echo(f"  DRY  {target}")
        return "DRY"
    if target.exists():
        typer.echo(f"  SKIP {target} (already installed)")
        return "SKIP"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")
    typer.echo(f"  OK   {target}")
    return "OK"


def _write_marker(install_dir: Path, *, dry_run: bool) -> None:
    """Write a ``.popolaloom-version`` marker beside the SKILL.md.

    The marker stores the running wheel version so a future ``popola
    doctor`` (Stage S4) can detect drift between the installed skill
    and the live install.
    """
    from popolaloom import __version__

    marker = install_dir / ".popolaloom-version"
    if dry_run:
        typer.echo(f"  DRY  {marker}")
        return
    if marker.exists():
        return
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{__version__}\n", encoding="utf-8")


def _install_target(
    target: str,
    *,
    scope: str,
    cwd: Path,
    dry_run: bool,
) -> str:
    """Install one IDE target; return the literal action (OK/SKIP/DRY)."""
    content, _is_real = resolve_skill_source()

    if target == "cursor":
        path = cursor_target_path(scope, cwd=cwd)
    elif target == "claude":
        path = claude_target_path(scope, cwd=cwd)
    elif target == "copilot":
        if scope == "global":
            typer.echo(
                "  warning: Copilot does not support --global; falling back to --project."
            )
        path = copilot_target_path(cwd=cwd)
    elif target == "codex":
        path = codex_target_path()
    else:
        raise typer.BadParameter(f"unknown install target: {target!r}")

    typer.echo(f"\n  {target.capitalize()} ({scope}) -> {path}")
    action = _write_skill(path, content, dry_run=dry_run)
    if action == "OK":
        _write_marker(path.parent, dry_run=dry_run)
    return action


# ── local scaffold ───────────────────────────────────────────────────────


_LOCAL_README: str = (
    "# .local/ workspace\n\n"
    "Scaffolded by `popola init local` (PopolaLoom v0.5.0+).  Mirrors the\n"
    "DevolaFlow .local/ contract; see the project root README + the\n"
    "v0.5.0 plan for the canonical surface.\n"
)

_TRACKER_MD: str = (
    "# Feedback Tracker\n\n"
    "| ID | Created | Topic | State |\n"
    "|---|---|---|---|\n"
    "<!-- append a new row when you file feedback under .local/feedbacks/ -->\n"
)

_MEMORY_MD: str = (
    "# Memory Index\n\n"
    "Long-lived knowledge captured across PopolaLoom sessions.  Use\n"
    "`.local/memory/specs/` for source-of-truth specs and\n"
    "`.local/memory/research/` for research dossiers.\n"
)

_INDEX_MD: str = (
    "# .local/ workspace index\n\n"
    "Auto-generated directory listing.\n\n"
    "- `.agent/`\n"
    "- `feedbacks/`\n"
    "- `memory/`\n"
    "- `tasks/`\n"
)


def _scaffold_path(
    target: Path,
    *,
    is_dir: bool,
    content: str | None = None,
    dry_run: bool,
) -> str:
    """Create ``target`` (file or dir) idempotently.

    Returns ``"OK"``, ``"SKIP"``, or ``"DRY"`` (matches :func:`_write_skill`).
    """
    if dry_run:
        kind = "dir " if is_dir else "file"
        typer.echo(f"  DRY  {kind} {target}")
        return "DRY"
    if is_dir:
        if target.exists():
            typer.echo(f"  SKIP {target} (already exists)")
            return "SKIP"
        target.mkdir(parents=True, exist_ok=True)
        typer.echo(f"  OK   {target}/")
        return "OK"
    if target.exists():
        typer.echo(f"  SKIP {target} (already exists)")
        return "SKIP"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content or "", encoding="utf-8", newline="\n")
    typer.echo(f"  OK   {target}")
    return "OK"


def _install_local(
    cwd: Path,
    *,
    no_compile: bool,
    with_examples: bool,
    dry_run: bool,
) -> None:
    """Scaffold the canonical ``.local/`` workspace surface.

    Creates the 8 canonical paths from the v0.5.0 plan §S2.B; every
    existing path is left untouched (idempotent).  Honours ``--dry-run``
    by printing every path that would be touched without writing.

    The ``no_compile`` flag is recorded but is a no-op for Stage S2: the
    ``.rules/`` compile chain landed in DevolaFlow but is deferred to
    PopolaLoom v0.6.0 per the plan §S2 risk note.  The flag is still
    accepted so CI scripts authored against the DevolaFlow surface keep
    working.
    """
    typer.echo(f"\n  Local workspace -> {cwd / '.local/'}")

    _scaffold_path(cwd / ".local" / "feedbacks", is_dir=True, dry_run=dry_run)
    _scaffold_path(
        cwd / ".local" / "feedbacks" / "TRACKER.md",
        is_dir=False,
        content=_TRACKER_MD,
        dry_run=dry_run,
    )
    _scaffold_path(
        cwd / ".local" / "feedbacks" / "README.md",
        is_dir=False,
        content="# .local/feedbacks/\n\nFile feedback drops here.\n",
        dry_run=dry_run,
    )
    _scaffold_path(cwd / ".local" / "tasks", is_dir=True, dry_run=dry_run)
    _scaffold_path(
        cwd / ".local" / "tasks" / "README.md",
        is_dir=False,
        content="# .local/tasks/\n\nTask briefs land here.\n",
        dry_run=dry_run,
    )
    _scaffold_path(cwd / ".local" / "memory", is_dir=True, dry_run=dry_run)
    _scaffold_path(
        cwd / ".local" / "memory" / "MEMORY.md",
        is_dir=False,
        content=_MEMORY_MD,
        dry_run=dry_run,
    )
    _scaffold_path(
        cwd / ".local" / "memory" / "README.md",
        is_dir=False,
        content="# .local/memory/\n\nLong-lived memory store.\n",
        dry_run=dry_run,
    )
    _scaffold_path(
        cwd / ".local" / "index.md",
        is_dir=False,
        content=_INDEX_MD,
        dry_run=dry_run,
    )
    _scaffold_path(cwd / ".local" / ".agent" / "active", is_dir=True, dry_run=dry_run)
    _scaffold_path(
        cwd / ".local" / ".agent" / "active" / "README.md",
        is_dir=False,
        content="# .local/.agent/active/\n\nIn-flight change folders.\n",
        dry_run=dry_run,
    )
    _scaffold_path(cwd / ".local" / ".agent" / "handoff", is_dir=True, dry_run=dry_run)
    _scaffold_path(
        cwd / ".local" / ".agent" / "handoff" / "README.md",
        is_dir=False,
        content="# .local/.agent/handoff/\n\nAppend-only handoff envelopes.\n",
        dry_run=dry_run,
    )
    _scaffold_path(cwd / ".local" / ".agent" / "archive", is_dir=True, dry_run=dry_run)
    _scaffold_path(
        cwd / ".local" / ".agent" / "archive" / "README.md",
        is_dir=False,
        content="# .local/.agent/archive/\n\nClosed change folders.\n",
        dry_run=dry_run,
    )

    if no_compile:
        typer.echo("  SKIP compile (--no-compile flag set; .rules/ compile is v0.6.0)")
    else:
        typer.echo("  NOTE compile chain is deferred to v0.6.0 (plan §S2 risk note)")

    if with_examples:
        _seed_local_examples(cwd, dry_run=dry_run)


_EXAMPLE_TASK_MD: str = (
    "# Example Task: dispatch-cursor-task\n\n"
    "Worked-trace fixture seeded by `popola init local --with-examples`\n"
    "(v0.5.0 PV-S2).  Demonstrates how to delegate a long-running\n"
    "task to a local agent CLI via PopolaLoom.\n\n"
    "## Steps\n\n"
    "1. `popola popolad start` — start the daemon.\n"
    "2. `popola dispatch \"refactor module X\" --cli cursor --cwd .`\n"
    "3. `popola attach <task_id> --follow` to watch live events.\n\n"
    "Delete this file once you author your first real task.\n"
)


def _seed_local_examples(cwd: Path, *, dry_run: bool) -> None:
    """Seed example task files under ``.local/`` (mode=full)."""
    typer.echo(f"\n  Example seed -> {cwd / '.local' / 'tasks' / 'example-dispatch.md'}")
    _scaffold_path(
        cwd / ".local" / "tasks" / "example-dispatch.md",
        is_dir=False,
        content=_EXAMPLE_TASK_MD,
        dry_run=dry_run,
    )


# ── --list ───────────────────────────────────────────────────────────────


def _print_list(cwd: Path) -> None:
    """Print the detected tools / scope / agent_dir / SKILL.md status."""
    detected = _auto_detect(cwd)
    content, is_real = resolve_skill_source()

    table = Table(title="popola init — detected targets", show_header=True, header_style="bold")
    table.add_column("target")
    table.add_column("scope")
    table.add_column("install path")
    table.add_column("present?")

    for target in ("cursor", "claude", "copilot", "codex"):
        if target == "cursor":
            path = cursor_target_path("project", cwd=cwd)
        elif target == "claude":
            path = claude_target_path("project", cwd=cwd)
        elif target == "copilot":
            path = copilot_target_path(cwd=cwd)
        else:
            path = codex_target_path()
        scope = "project" if target != "codex" else "global"
        table.add_row(
            target,
            scope,
            str(path),
            "yes" if path.exists() else "no",
        )
    table.add_row(
        "local",
        "project",
        str(cwd / ".local"),
        "yes" if (cwd / ".local").is_dir() else "no",
    )
    _console_out.print(table)

    typer.echo(f"\nDetected by auto-detect: {', '.join(detected) if detected else '(none)'}")
    typer.echo(f"Skill source: {'wheel-bundled (S3+)' if is_real else 'placeholder stub (S2)'}")
    typer.echo(f"Skill bytes:  {len(content)}")


# ── Typer wiring ─────────────────────────────────────────────────────────


@app.callback()
def init_callback(
    ctx: typer.Context,
    list_only: bool = typer.Option(
        False,
        "--list",
        help="Print detected tools + install paths + skill source state and exit.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print every path that would be written without touching disk.",
    ),
) -> None:
    """Top-level callback: handle ``--list`` and the no-subcommand auto-detect path."""
    cwd = Path.cwd()
    if list_only:
        if ctx.invoked_subcommand is not None:
            raise typer.BadParameter(
                "--list cannot be combined with a verb subcommand"
            )
        _print_list(cwd)
        raise typer.Exit(code=0)

    if ctx.invoked_subcommand is not None:
        return

    detected = _auto_detect(cwd)
    if not detected:
        typer.echo("  No AI tools detected. Installing for Cursor (most common).")
        detected = ["cursor"]

    typer.echo(f"  popola init — auto-detected targets: {', '.join(detected)}")
    for target in detected:
        if target == "local":
            _install_local(cwd, no_compile=False, with_examples=False, dry_run=dry_run)
        elif target in {"cursor", "claude", "copilot", "codex"}:
            _install_target(target, scope="project", cwd=cwd, dry_run=dry_run)


def _resolve_scope(global_: bool, project: bool, *, default: str = "project") -> str:
    """Resolve ``--global`` / ``--project`` to a single ``scope`` string.

    Raises:
        typer.BadParameter: when both flags are passed simultaneously
            (S-5: explicit conflict error rather than a silent
            last-flag-wins fallback).
    """
    if global_ and project:
        raise typer.BadParameter("--global and --project are mutually exclusive")
    if global_:
        return "global"
    if project:
        return "project"
    return default


@app.command("cursor")
def cmd_cursor(
    global_: bool = typer.Option(False, "--global", help="Install to ~/.cursor/."),
    project: bool = typer.Option(False, "--project", help="Install to <cwd>/.cursor/."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print without writing."),
) -> None:
    """Install the PopolaLoom skill for Cursor (project-local default)."""
    scope = _resolve_scope(global_, project)
    _install_target("cursor", scope=scope, cwd=Path.cwd(), dry_run=dry_run)


@app.command("claude")
def cmd_claude(
    global_: bool = typer.Option(False, "--global", help="Install to ~/.claude/."),
    project: bool = typer.Option(False, "--project", help="Install to <cwd>/.claude/."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print without writing."),
) -> None:
    """Install the PopolaLoom skill for Claude Code."""
    scope = _resolve_scope(global_, project)
    _install_target("claude", scope=scope, cwd=Path.cwd(), dry_run=dry_run)


@app.command("copilot")
def cmd_copilot(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print without writing."),
) -> None:
    """Install the PopolaLoom skill for GitHub Copilot (single-file, project-local)."""
    _install_target("copilot", scope="project", cwd=Path.cwd(), dry_run=dry_run)


@app.command("codex")
def cmd_codex(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print without writing."),
) -> None:
    """Install the PopolaLoom skill for Codex (``$CODEX_HOME`` or ~/.codex/)."""
    _install_target("codex", scope="global", cwd=Path.cwd(), dry_run=dry_run)


@app.command("local")
def cmd_local(
    no_compile: bool = typer.Option(
        False,
        "--no-compile",
        help="Skip the .rules/ compile chain (deferred to v0.6.0 — flag is a no-op for now).",
    ),
    with_examples_flag: bool | None = typer.Option(
        None,
        "--with-examples/--no-with-examples",
        help="Seed example tasks under .local/tasks/ (default: off; --mode=full overrides).",
    ),
    mode: str | None = typer.Option(
        None,
        "--mode",
        help="Shorthand: core | standard | full (overridden by individual flags).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print without writing."),
) -> None:
    """Scaffold the .local/ workspace surface for PopolaLoom."""
    no_compile_resolved, with_examples_resolved = _resolve_mode(
        mode,
        no_compile_flag=no_compile,
        with_examples_flag=with_examples_flag,
    )
    _install_local(
        Path.cwd(),
        no_compile=no_compile_resolved,
        with_examples=with_examples_resolved,
        dry_run=dry_run,
    )


@app.command("all")
def cmd_all(
    global_: bool = typer.Option(False, "--global", help="Use global scope where supported."),
    project: bool = typer.Option(False, "--project", help="Force project scope."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print without writing."),
) -> None:
    """Install every IDE target (excluding ``local`` — opt-in via ``init local``)."""
    scope = _resolve_scope(global_, project)
    _install_all(scope=scope, cwd=Path.cwd(), dry_run=dry_run)


def _install_all(*, scope: str, cwd: Path, dry_run: bool) -> None:
    """Install every IDE target except ``local`` (mirrors DevolaFlow `all`)."""
    targets: Sequence[str] = ("cursor", "claude", "copilot", "codex")
    typer.echo(f"  popola init all — installing: {', '.join(targets)} (scope={scope})")
    for target in targets:
        _install_target(target, scope=scope, cwd=cwd, dry_run=dry_run)
