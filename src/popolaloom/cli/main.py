"""popola CLI — Typer entry point for the popolaloom meta-orchestrator.

v0.2.0 Stage A: switched from in-process ``Popolad`` calls to a real RPC
client over Unix Domain Socket (closes R-001 + R-005). Each subcommand
opens an :class:`httpx.AsyncClient` with
``transport=httpx.AsyncHTTPTransport(uds=str(socket_path))`` and talks
to the popolad daemon process at ``http://popolad/<endpoint>``.

User-facing subcommands:

- ``popola version`` — print package version + exit (no daemon needed).
- ``popola list-cli`` — show registered CLI adapter names + availability
  (no daemon needed; only the local registry).
- ``popola dispatch`` — POST /dispatch (daemon required).
- ``popola status``  — GET  /status/{id}.
- ``popola list``    — GET  /list (default non-terminal only).
- ``popola attach``  — GET  /attach_stream/{id} (default ``--follow=True``,
  R-005 fix; ``--no-follow`` reverts to one-shot dump).
- ``popola cancel``  — POST /cancel/{id}.
- ``popola probe``   — GET  /probe (lightweight daemon health).

All daemon-bound commands handle ``httpx.ConnectError`` with a friendly
"popolad not running" message + ``exit 1`` (No Silent Failures).

New v0.2.0 options:

- ``--cli-flag KEY=VAL`` repeated → ``extra`` dict passed to adapter
  (R-012 fix — enables ``cursor`` ``--yolo`` etc.).
- ``--events-dir PATH`` on dispatch (R-014 part) — currently advisory only;
  daemon process owns the actual events directory; this passes the option
  to the daemon for future per-task override (Stage E).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from popolaloom import __version__
from popolaloom.adapters import get_adapter, list_registered
from popolaloom.adapters.cursor_cloud import (
    CloudCursorClient,
    CursorCloudError,
    CursorCloudStreamInvalidLastEventIdError,
    SSEReader,
)

if TYPE_CHECKING:
    from popolaloom.daemon.event_log import EventLog

__all__ = ["app", "make_async_client", "make_sync_client"]

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="popola",
    help="PopolaLoom meta-orchestrator CLI — dispatch tasks across local agent CLIs.",
    no_args_is_help=True,
    add_completion=False,
)


def _register_subcommand_groups() -> None:
    """Attach the popolad + eval + init + skill subcommand groups + doctor verb.

    Registered here (not in :mod:`popolaloom.cli.__init__`) so that
    ``python -m popolaloom.cli.main`` invocations get the same surface
    as ``popola`` (the console_script entry).  Splitting the imports
    into a helper avoids module-level circular imports during the
    Typer app construction.

    v0.5.0 Stage S4: registers the new ``popola skill`` subcommand
    group (``install`` / ``doctor`` / ``upgrade``) and the standalone
    ``popola doctor`` aggregate-health verb (single command, not a
    subcommand group, per plan §S4.E).

    v0.8.8 T2.4.1 (Q-C-1 偏离默认): registers the new ``popola cloud``
    subcommand group whose first verb is ``runs`` — list cloud-agent
    run history per ``runs-subcommand-spec.md`` §2.2.
    """
    from popolaloom.cli.auth_cmd import app as auth_app
    from popolaloom.cli.cloud_cmd import app as cloud_app
    from popolaloom.cli.doctor_cmd import doctor_command
    from popolaloom.cli.eval import app as eval_app
    from popolaloom.cli.handoff_cmd import app as handoff_app
    from popolaloom.cli.init_cmd import app as init_app
    from popolaloom.cli.popolad import app as popolad_app
    from popolaloom.cli.relay_cmd import relay_command
    from popolaloom.cli.skill_cmd import app as skill_app

    app.add_typer(popolad_app, name="popolad", help="Manage popolad daemon process")
    app.add_typer(
        eval_app,
        name="eval",
        help="PopolaLoom self-evaluation (8-dim nines runner)",
    )
    app.add_typer(init_app, name="init")
    app.add_typer(
        skill_app,
        name="skill",
        help="Install / audit / upgrade the PopolaLoom Skill",
    )
    app.add_typer(
        handoff_app,
        name="handoff",
        help="Inspect / archive on-disk handoff envelopes (v0.7.2+)",
    )
    app.add_typer(
        cloud_app,
        name="cloud",
        help="Cloud-agent (cursor-cloud runtime) introspection verbs.",
    )
    app.add_typer(
        auth_app,
        name="auth",
        help="Manage credentials (Cursor API key keyring storage, v0.9.2+).",
    )
    app.command(name="relay")(relay_command)
    app.command(name="doctor")(doctor_command)


_register_subcommand_groups()

_console_out = Console()

_TERMINAL_STATES: frozenset[str] = frozenset({"completed", "failed", "canceled"})

_POLL_INTERVAL_S: float = 0.5
_DEFAULT_WAIT_TIMEOUT_S: float = 60.0


# ── transport helpers ────────────────────────────────────────────────────


def _socket_path() -> Path:
    """Resolve the popolad UDS path: ``$POPOLA_HOME/popolad.sock`` or ``~/.popola/popolad.sock``.

    Tests can override by setting ``$POPOLA_HOME`` to a tmp_path.
    """
    home = os.environ.get("POPOLA_HOME")
    base = Path(home).expanduser().resolve() if home else Path.home() / ".popola"
    return base / "popolad.sock"


def make_async_client(socket_path: Path | None = None) -> httpx.AsyncClient:
    """Construct an :class:`httpx.AsyncClient` bound to the popolad UDS.

    Args:
        socket_path: override (tests use this); default resolves via
            :func:`_socket_path`.

    Returns:
        httpx.AsyncClient: caller is responsible for ``async with`` /
        ``aclose()``.

    Raises:
        Nothing here — connection failures surface on first request as
        :class:`httpx.ConnectError`, handled by :func:`_render_connect_error`.
    """
    sock = socket_path or _socket_path()
    transport = httpx.AsyncHTTPTransport(uds=str(sock))
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://popolad",
        timeout=httpx.Timeout(connect=5.0, read=None, write=10.0, pool=10.0),
    )


def make_sync_client(socket_path: Path | None = None) -> httpx.Client:
    """Construct a synchronous :class:`httpx.Client` bound to the popolad UDS.

    Used for fast non-streaming subcommands (``status``, ``list``) where
    asyncio overhead is not warranted; ``attach`` uses the async variant
    for proper SSE streaming.
    """
    sock = socket_path or _socket_path()
    transport = httpx.HTTPTransport(uds=str(sock))
    return httpx.Client(
        transport=transport,
        base_url="http://popolad",
        timeout=httpx.Timeout(connect=5.0, read=None, write=10.0, pool=10.0),
    )


def _render_connect_error(exc: httpx.HTTPError) -> None:
    """Print friendly daemon-down message + exit 1 (No Silent Failures)."""
    typer.echo(
        "error: popolad not running, run `popola popolad start` to start it",
        err=True,
    )
    logger.debug("daemon connect error: %r", exc)
    raise typer.Exit(code=1)


# ── version (no daemon) ───────────────────────────────────────────────────


@app.command()
def version() -> None:
    """Print package version and exit (e.g. ``popolaloom 0.0.1``)."""
    typer.echo(f"popolaloom {__version__}")


# ── list-cli (no daemon) ──────────────────────────────────────────────────


@app.command(name="list-cli")
def list_cli() -> None:
    """Show all registered CLI adapter names + availability on ``$PATH``.

    R-014 fix: status column is rendered via :class:`rich.text.Text` so
    Rich markup like ``[available]`` doesn't get interpreted (previously
    rendered as empty in some terminals).
    """
    names = list_registered()
    if not names:
        typer.echo(
            "error: no adapters registered (Phase 1 expects cursor/claude/codex)",
            err=True,
        )
        raise typer.Exit(code=1)

    table = Table(title="Registered CLI adapters", show_header=True, header_style="bold")
    table.add_column("name", style="bold")
    table.add_column("binary")
    table.add_column("status")

    for name in names:
        adapter = get_adapter(name)
        if adapter.is_available():
            status_text = Text("available", style="green")
        else:
            status_text = Text("missing", style="yellow")
        table.add_row(name, adapter.binary, status_text)

    _console_out.print(table)


# ── dispatch ──────────────────────────────────────────────────────────────


@app.command()
def dispatch(
    prompt: str = typer.Argument(
        "",
        help="Prompt string forwarded to the chosen CLI. May be empty when --replay is set.",
    ),
    cli: str = typer.Option(
        "",
        "--cli",
        help="CLI adapter name (cursor/claude/codex/...). May be empty when --replay is set.",
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Working directory for the spawned subprocess (defaults to popolad's CWD).",
    ),
    cli_flag: list[str] = typer.Option(  # noqa: B008
        [],
        "--cli-flag",
        help=(
            "Repeatable adapter extras KEY=VAL (e.g. repo_url=...; "
            "cursor-cloud routing: worker_name/pool_name/labels). R-012."
        ),
    ),
    events_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--events-dir",
        help="Override events_dir (advisory; daemon owns actual write path). R-014 part.",
    ),
    replay: str = typer.Option(
        "",
        "--replay",
        help=(
            "Replay a previously written handoff envelope by id "
            "(e.g. cursor-fix-bug-foo-py-3a7f9c1d). When set, overrides "
            "prompt / --cli / --cwd / --cli-flag from the envelope's "
            "stored values (v0.7.3+)."
        ),
    ),
    wait: bool = typer.Option(
        False,
        "--wait",
        help="Block until the task reaches a terminal state (completed/failed/canceled).",
    ),
    timeout_s: float = typer.Option(
        _DEFAULT_WAIT_TIMEOUT_S,
        "--timeout",
        help="--wait timeout in seconds (default 60).",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON instead of a plain task_id line.",
    ),
) -> None:
    """Dispatch a new task to popolad and (optionally) wait for completion.

    v0.7.3+ ``--replay HANDOFF_ID`` reads a previously written envelope from
    ``$POPOLA_HANDOFF_DIR`` (or ``.local/.agent/handoff/``) and uses its
    ``target_cli`` / ``prompt`` / ``cwd`` / ``adapter_extra`` as the
    dispatch payload — exact replay of a prior dispatch (or a relay'd /
    HITL'd one) without re-typing the prompt or its flags.
    """
    if replay:
        _resolved = _resolve_replay(replay, prompt, cli, cwd, cli_flag)
        prompt = _resolved.prompt
        cli = _resolved.cli
        cwd = _resolved.cwd
        extra = _resolved.adapter_extra
    else:
        if not prompt:
            typer.echo("error: missing prompt (or use --replay HANDOFF_ID)", err=True)
            raise typer.Exit(code=2)
        if not cli:
            typer.echo("error: --cli is required (or use --replay HANDOFF_ID)", err=True)
            raise typer.Exit(code=2)
        extra = _parse_cli_flags(cli_flag)

    if events_dir is not None:
        extra.setdefault("__events_dir", str(events_dir))

    body: dict[str, Any] = {"cli": cli, "prompt": prompt}
    if cwd is not None:
        body["cwd"] = str(cwd)
    if extra:
        body["extra"] = extra

    try:
        with make_sync_client() as client:
            r = client.post("/dispatch", json=body)
    except httpx.ConnectError as exc:
        _render_connect_error(exc)
        return

    if r.status_code == 404:
        typer.echo(f"error: unknown cli={cli!r}: {r.json().get('detail', '')}", err=True)
        raise typer.Exit(code=1)
    if r.status_code == 400:
        typer.echo(f"error: dispatch failed: {r.json().get('detail', '')}", err=True)
        raise typer.Exit(code=1)
    if r.status_code != 200:
        typer.echo(f"error: dispatch unexpected status {r.status_code}: {r.text}", err=True)
        raise typer.Exit(code=1)

    payload = r.json()
    task_id = payload["task_id"]

    if wait:
        _wait_for_terminal(task_id, timeout_s=timeout_s)

    if json_out:
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        typer.echo(task_id)


# ── status ────────────────────────────────────────────────────────────────


@app.command()
def status(
    task_id: str = typer.Argument(..., help="Task identifier returned by `popola dispatch`."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help=(
            "v0.8.8 (Q-C-2): show the cost / model / wall-clock surface "
            "per cost-fields.md §3.1. Default off — the surface is opt-in "
            "to avoid promoting fabricated cost numbers (cost is always "
            "rendered as 'cost: n/a' since Cursor's public REST/SSE API "
            "does not expose per-run cost in v0.8.8). With --json, adds "
            "a 'verbose' block per spec §3.2 (10 keys)."
        ),
    ),
) -> None:
    """Query a task's runtime status (state / pid / exit_code / timestamps).

    v0.8.8 T2.1.2 (Q-C-2 ``cost-fields.md``):

    - Without ``--verbose`` the response shape is unchanged from v0.8.7
      — no cost block in the table, no ``verbose`` key in the JSON.
    - With ``--verbose`` the table appends a one-liner
      ``cost: n/a  model: <id|->  [mode: max]  wall: NN.Ns  link: <url>``
      and the JSON gains a ``verbose`` object per spec §3.2.

    The ``cost: n/a`` literal is locked: PopolaLoom does NOT fabricate
    per-run cost numbers (no authoritative source in the public Cloud
    Agents v1 API). Future Admin-API correlation is opt-in and will
    surface as ``cost: ~$X.XX (admin-est)`` behind a separate config
    knob.
    """
    try:
        with make_sync_client() as client:
            params: dict[str, Any] = {"verbose": "true"} if verbose else {}
            r = client.get(f"/status/{task_id}", params=params)
    except httpx.ConnectError as exc:
        _render_connect_error(exc)
        return

    if r.status_code == 404:
        typer.echo(f"error: task not found: {task_id}", err=True)
        raise typer.Exit(code=1)
    if r.status_code != 200:
        typer.echo(f"error: status unexpected {r.status_code}: {r.text}", err=True)
        raise typer.Exit(code=1)

    info = r.json()

    busy_line = _build_status_busy_line(task_id)

    if json_out:
        typer.echo(json.dumps(info, ensure_ascii=False))
        if busy_line is not None:
            # Q-C-7 default-visibility: surface the WAITING marker even
            # in --json mode so machine consumers (status pollers) see
            # the same signal humans do; written to stderr so JSON
            # parsers reading stdout are unaffected.
            typer.echo(busy_line, err=True)
        return

    table = Table(title=f"Task {task_id}", show_header=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    fields = (
        "task_id",
        "cli",
        "state",
        "pid",
        "exit_code",
        "started_at",
        "completed_at",
        "latest_event_index",
        "arktower_task_id",
        "persisted",
    )
    for key in fields:
        value = info.get(key)
        table.add_row(key, "" if value is None else str(value))
    _console_out.print(table)

    # v0.8.8 T2.2.2 (Q-C-7): default-visible WAITING line surfaced
    # below the table whenever the latest unmatched ``cloud.queued_quota_exceeded``
    # / ``cloud.busy_queued`` event indicates the daemon is parked on a
    # 429 backoff or 409 agent_busy queue. This is disjoint from
    # T2.1.2's --verbose path (the cost line); both can render in the
    # same invocation.
    if busy_line is not None:
        typer.echo(busy_line)

    if verbose:
        cost_line = _format_verbose_cost_line(info.get("verbose"))
        typer.echo(cost_line)


def _build_status_busy_line(task_id: str) -> str | None:
    """Compose the ``WAITING:`` line for ``popola status`` (Q-C-7).

    Per ``quota-config.md`` §5.2, ``popola status`` at default verbosity
    must surface a single line summarising the latest
    ``cloud.queued_quota_exceeded`` (or ``cloud.busy_queued``) event
    until a matching exit event arrives. Returns ``None`` when the task
    is not in any waiting state — the caller suppresses the line entirely
    in that case so non-throttled tasks render as before.

    The events log lives at ``$POPOLA_HOME/events/<task_id>.jsonl``;
    we read it directly rather than adding a new RPC method (the daemon
    already owns the file's authoritative writes; the CLI is purely a
    reader here, so concurrent writes are safe via ``EventLog``'s
    ``O_APPEND`` semantics).
    """
    events = _read_events_for_task(task_id)
    if not events:
        return None
    return _busy_line_from_events(events)


def _events_path_for_task(task_id: str) -> Path:
    """Resolve ``$POPOLA_HOME/events/<task_id>.jsonl`` (mirrors daemon).

    Centralised here so the WAITING surface and any future per-task
    event peek share one path resolver — keeps the CLI's view of
    ``$POPOLA_HOME`` consistent with :func:`_socket_path`.
    """
    home = os.environ.get("POPOLA_HOME")
    base = Path(home).expanduser().resolve() if home else Path.home() / ".popola"
    return base / "events" / f"{task_id}.jsonl"


def _read_events_for_task(task_id: str) -> list[dict[str, Any]]:
    """Tolerantly read every NDJSON event for ``task_id``.

    Non-existent file → ``[]`` (the task may be local, or events may not
    have been written yet). Per workspace rule "No Silent Failures" we
    log decode errors at DEBUG so operators grepping the daemon log can
    still see them — but the CLI's status surface is non-fatal, so a
    bad line skips rather than aborts.
    """
    path = _events_path_for_task(task_id)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("status busy-line: cannot read %s: %s", path, exc)
        return []

    events: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            logger.debug(
                "status busy-line: skipping corrupt line %d in %s: %s",
                line_no,
                path,
                exc,
            )
    return events


def _busy_line_from_events(events: list[dict[str, Any]]) -> str | None:
    """Pick a WAITING summary from a per-task event stream (or ``None``).

    Decision tree (per ``quota-config.md`` §5.2 + §4.3):

    1. Walk events in chronological order tracking the most recent
       ``cloud.queued_quota_exceeded`` (rate-limit backoff) and
       ``cloud.busy_queued`` (409 agent_busy queue).
    2. Each is "cleared" by a matching exit event:
       - ``cloud.queued_quota_exceeded`` cleared by ``cloud.queue_exit``
         with ``outcome="success"`` (other outcomes leave the line up
         briefly so the eventual ``task.failed`` rendering takes over).
       - ``cloud.busy_queued`` cleared by ``cloud.busy_dispatched`` or
         ``cloud.busy_timeout``.
    3. The most-recent uncleared event of either kind wins; if both are
       uncleared we prefer the agent_busy line because it carries the
       longer wait window (per spec §5.2 priority).
    4. ``task.completed`` / ``task.failed`` / ``task.canceled`` clears
       both kinds (the task is over).
    """
    pending_quota: dict[str, Any] | None = None
    pending_busy: dict[str, Any] | None = None
    quota_ts: str | None = None
    busy_ts: str | None = None

    for ev in events:
        ev_type = ev.get("type") if isinstance(ev, dict) else None
        if not isinstance(ev_type, str):
            continue
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        if ev_type == "cloud.queued_quota_exceeded":
            pending_quota = data
            quota_ts = ev.get("time") if isinstance(ev.get("time"), str) else None
        elif ev_type == "cloud.queue_exit":
            outcome = data.get("outcome") if isinstance(data, dict) else None
            if outcome == "success" or outcome is None:
                pending_quota = None
                quota_ts = None
        elif ev_type == "cloud.busy_queued":
            pending_busy = data
            busy_ts = ev.get("time") if isinstance(ev.get("time"), str) else None
        elif ev_type in {"cloud.busy_dispatched", "cloud.busy_timeout"}:
            pending_busy = None
            busy_ts = None
        elif ev_type in {"task.completed", "task.failed", "task.canceled"}:
            pending_quota = None
            pending_busy = None
            quota_ts = None
            busy_ts = None

    if pending_busy is None and pending_quota is None:
        return None

    # Priority: busy (longer wait window) > quota (short backoff) when
    # both are uncleared simultaneously — see spec §5.2.
    if pending_busy is not None:
        return _format_busy_queue_line(pending_busy)
    assert pending_quota is not None
    _ = quota_ts  # ts retained for future enrichment (latency display)
    _ = busy_ts
    return _format_quota_waiting_line(pending_quota)


def _format_quota_waiting_line(payload: dict[str, Any]) -> str:
    """Format the rate-limit waiting line (``WAITING: rate_limit ...``).

    Spec example (§5.2): ``WAITING: rate_limit retry 2/5 next=~2.5s``.

    The current ``_retrying_request`` helper emits
    ``cloud.queued_quota_exceeded`` once per backoff sequence (NOT per
    attempt), so we have ``max_retries`` and (optionally)
    ``retry_after_ms`` but not the exact attempt counter. We render
    ``retry 1/<max>`` to match the spec example shape — operators
    reading the line know the daemon is parked on the *first* observed
    backoff in the current sequence; subsequent retries are silent
    until exit (the once-per-sequence emit policy is the design intent
    per spec §3.3).
    """
    max_retries = payload.get("max_retries")
    retry_after_ms = payload.get("retry_after_ms")
    max_str = (
        str(max_retries)
        if isinstance(max_retries, int) and max_retries > 0
        else "?"
    )
    if isinstance(retry_after_ms, (int, float)) and retry_after_ms >= 0:
        next_segment = f" next=~{float(retry_after_ms) / 1000.0:.1f}s"
    else:
        next_segment = ""
    return f"WAITING: rate_limit retry 1/{max_str}{next_segment}"


def _format_busy_queue_line(payload: dict[str, Any]) -> str:
    """Format the 409 agent_busy waiting line (``WAITING: agent_busy ...``).

    Per spec §5.2 the queue path is symmetric with the rate-limit path
    (both default-visible). We surface the queue position and the
    deadline so operators can decide whether to ``popola cancel <task>``
    or wait it out — the deadline is the most actionable bit.
    """
    agent_id = payload.get("agent_id")
    position = payload.get("queue_position")
    deadline_ts = payload.get("deadline_ts")

    parts: list[str] = ["WAITING: agent_busy"]
    if isinstance(agent_id, str) and agent_id:
        parts.append(f"agent={agent_id}")
    if isinstance(position, int) and position > 0:
        parts.append(f"position={position}")
    if isinstance(deadline_ts, str) and deadline_ts:
        parts.append(f"deadline={deadline_ts}")
    elif deadline_ts is None:
        # ``None`` is the "wait forever" sentinel (queue_max_wait_s=0).
        parts.append("deadline=never")
    return " ".join(parts)


def _format_verbose_cost_line(verbose_block: Any) -> str:
    """Render the §3.1 one-liner from the daemon's verbose block.

    Format (per ``cost-fields.md`` §3.1):

    ``cost: n/a  model: <id|->  [mode: max]  wall: NN.Ns  link: <url|->``

    Rules:

    - ``cost: n/a`` is always literal (Q-C-2: no fabricated numbers).
    - ``model: -`` when ``model_id`` is ``None`` (default substituted).
    - ``mode:`` segment omitted entirely when mode is ``"std"`` (keeps
      the line short in the common case per §3.1 example).
    - ``wall:`` shows 1-decimal seconds; ``-`` when missing.
    - ``link:`` from ``agent_url``; ``-`` when missing (local task).
    """
    if not isinstance(verbose_block, dict):
        return "cost: n/a  model: -  wall: -  link: -"

    model_id = verbose_block.get("model_id")
    model_str = model_id if isinstance(model_id, str) and model_id else "-"

    model_mode = verbose_block.get("model_mode")
    mode_segment = ""
    if isinstance(model_mode, str) and model_mode and model_mode != "std":
        mode_segment = f"mode: {model_mode}  "

    wall = verbose_block.get("wall_clock_s")
    wall_str = (
        f"{float(wall):.1f}s"
        if isinstance(wall, (int, float)) and wall >= 0
        else "-"
    )

    link = verbose_block.get("agent_url")
    link_str = link if isinstance(link, str) and link else "-"

    return (
        f"cost: n/a  model: {model_str}  {mode_segment}wall: {wall_str}  link: {link_str}"
    )


# ── attach ────────────────────────────────────────────────────────────────


@app.command()
def attach(
    task_id: str = typer.Argument(..., help="Task identifier whose events to tail."),
    from_index: int = typer.Option(
        0,
        "--from",
        help="Skip first N events (set to previous len() for incremental polling).",
    ),
    follow: bool = typer.Option(
        True,
        "--follow/--no-follow",
        help=(
            "Stream new events until terminal (default). "
            "Use --no-follow for a one-shot dump (R-005 fix in v0.2.0)."
        ),
    ),
    no_stream: bool = typer.Option(
        False,
        "--no-stream",
        help=(
            "v0.8.6 (T2.2.1): force the legacy poll-only path even for "
            "runtime=cloud tasks (escape hatch). By default --follow on a "
            "cloud task additionally pumps Cursor's SSE events into the "
            "renderer alongside the daemon's /attach_stream feed."
        ),
    ),
) -> None:
    """Print task events as ``<time>  <type>  <data_summary>`` lines.

    R-005 fix: ``--follow`` defaults to **True** so cross-process attach
    sees new events live. Use ``--no-follow`` for the legacy one-shot dump.

    v0.8.6 (T2.2.1): for ``runtime=cloud`` tasks the default ``--follow``
    additionally launches a Cursor SSE pump on a background thread that
    feeds ``cloud.sse.*`` events into the same renderer as the daemon's
    ``/attach_stream`` feed. On disconnect / ``410 stream_expired`` /
    network error the SSE thread bows out cleanly with a
    ``cloud.sse.fallback_to_poll`` boundary marker and a stderr notice;
    the existing poll-driven view continues. Use ``--no-stream`` to force
    the legacy poll-only path (escape hatch).
    """
    if follow:
        _attach_streaming(task_id, from_index=from_index, no_stream=no_stream)
    else:
        _attach_one_shot(task_id, from_index=from_index)


def _attach_one_shot(task_id: str, *, from_index: int) -> None:
    """Issue a one-shot tail request via ``GET /status`` + log file read.

    For non-follow mode we just hit ``/status`` (to verify the task exists)
    then ``/attach_stream`` with a short timeout: producer terminates as
    soon as the event log is drained when ``handle.is_terminal()``. For
    in-flight tasks we still produce the events available so far.
    """
    try:
        with make_sync_client() as client:
            r_status = client.get(f"/status/{task_id}")
            if r_status.status_code == 404:
                typer.echo(f"error: task not found: {task_id}", err=True)
                raise typer.Exit(code=1)
            if r_status.status_code != 200:
                typer.echo(f"error: status {r_status.status_code}: {r_status.text}", err=True)
                raise typer.Exit(code=1)

            with client.stream(
                "GET",
                f"/attach_stream/{task_id}",
                params={"since": from_index},
                timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=10.0),
            ) as stream:
                if stream.status_code != 200:
                    typer.echo(
                        f"error: attach_stream {stream.status_code}",
                        err=True,
                    )
                    raise typer.Exit(code=1)
                _consume_sse(stream, terminate_on_terminal=True)
    except httpx.ConnectError as exc:
        _render_connect_error(exc)


def _attach_streaming(
    task_id: str,
    *,
    from_index: int,
    no_stream: bool = False,
) -> None:
    """Long-poll the SSE attach_stream endpoint until terminal or Ctrl-C.

    v0.8.6 (T2.2.1): when the status snapshot reports ``runtime=cloud``
    and ``--no-stream`` was *not* passed, additionally spawn a daemon
    thread that pumps Cursor's SSE events into the local renderer. The
    thread exits cleanly on ``CursorCloudStreamExpiredError``,
    ``httpx.ReadError`` / ``httpx.ConnectError`` /
    ``httpx.TimeoutException``, missing API key, or main-thread teardown
    (``stop_event.set()``). On any error path it surfaces a
    ``cloud.sse.fallback_to_poll`` boundary event via the sink + a
    ``[cloud sse] ...`` one-liner on stderr (No-Silent-Failures).
    """
    sse_thread: threading.Thread | None = None
    sse_stop_event: threading.Event | None = None
    try:
        with make_sync_client() as client:
            r_status = client.get(f"/status/{task_id}")
            if r_status.status_code == 404:
                typer.echo(f"error: task not found: {task_id}", err=True)
                raise typer.Exit(code=1)
            if r_status.status_code != 200:
                typer.echo(f"error: status {r_status.status_code}: {r_status.text}", err=True)
                raise typer.Exit(code=1)

            status_info = _safe_status_payload(r_status)

            if not no_stream and status_info.get("runtime") == "cloud":
                sse_stop_event = threading.Event()
                sse_thread = _maybe_spawn_cloud_sse_thread(
                    task_id=task_id,
                    status_info=status_info,
                    stop_event=sse_stop_event,
                )

            try:
                with client.stream(
                    "GET",
                    f"/attach_stream/{task_id}",
                    params={"since": from_index},
                    timeout=httpx.Timeout(connect=5.0, read=None, write=10.0, pool=10.0),
                ) as stream:
                    if stream.status_code != 200:
                        typer.echo(
                            f"error: attach_stream {stream.status_code}",
                            err=True,
                        )
                        raise typer.Exit(code=1)
                    _consume_sse(stream, terminate_on_terminal=True)
            finally:
                if sse_stop_event is not None:
                    sse_stop_event.set()
                if sse_thread is not None:
                    sse_thread.join(timeout=2.0)
    except httpx.ConnectError as exc:
        _render_connect_error(exc)
    except KeyboardInterrupt:
        return


def _consume_sse(response: httpx.Response, *, terminate_on_terminal: bool) -> bool:
    """Iterate raw SSE response, parse each ``data:`` frame, render one line.

    Args:
        response: an open streaming :class:`httpx.Response`.
        terminate_on_terminal: when True, stop iterating once a terminal
            event is seen (we still let the server close naturally too).

    Returns:
        bool: ``True`` if a terminal event (``task.completed`` /
        ``task.failed`` / ``task.canceled``) or a server-side
        ``event: end-of-stream`` marker was observed. Callers can use
        this to distinguish "stream ended cleanly" from "stream broke
        mid-flight" (BUG-C in v0.7.0 feedback).

    BUG-C (v0.7.1): when the server's ``StreamingResponse`` returns
    after writing many SSE frames, ``httpx`` occasionally misclassifies
    the resulting EOF as a :class:`httpx.ReadTimeout` instead of a
    clean stream close. We catch ReadTimeout here and, when at least
    one terminal event has already been rendered, treat it as a normal
    stream-end (the producer is done; nothing more is coming). When the
    timeout fires *before* any terminal event we re-raise so the caller
    can decide (server hung mid-stream is still an error).
    """
    terminal_types = {"task.completed", "task.failed", "task.canceled"}
    saw_terminal = False
    try:
        for raw_line in response.iter_lines():
            if not raw_line:
                continue
            if raw_line.startswith("data: "):
                payload = raw_line[len("data: "):]
            elif raw_line.startswith("event:"):
                event_kind = raw_line[len("event:"):].strip()
                if event_kind == "end-of-stream":
                    saw_terminal = True
                    break
                continue
            elif raw_line.startswith(":"):
                continue
            else:
                continue
            try:
                envelope = json.loads(payload)
            except json.JSONDecodeError:
                logger.warning("Skipping un-parsable SSE frame: %r", payload[:200])
                continue
            typer.echo(_format_event(envelope))
            if envelope.get("type") in terminal_types:
                saw_terminal = True
                if terminate_on_terminal:
                    break
    except httpx.ReadTimeout:
        if not saw_terminal:
            logger.warning(
                "_consume_sse: ReadTimeout before any terminal event was seen; "
                "re-raising to caller"
            )
            raise
        logger.debug(
            "_consume_sse: ReadTimeout after terminal event observed — "
            "treating as clean stream-end (BUG-C)"
        )
    return saw_terminal


# ── cloud SSE pump (T2.2.1) ──────────────────────────────────────────────


def _utc_now_iso_ms() -> str:
    """ISO-8601 UTC timestamp with millisecond precision and ``Z`` suffix.

    Mirrors the format produced by :func:`popolaloom.daemon.event_log._utc_now_iso`
    so cloud SSE envelopes rendered by :class:`_CloudSSEEventSink` look the
    same as those streamed from the daemon's ``/attach_stream`` feed.
    """
    return (
        datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class _CloudSSEEventSink:
    """Duck-typed :class:`EventLog` for the CLI cloud SSE worker (T2.2.1).

    Implements the same ``append(event_type, data) -> dict`` contract as
    :class:`popolaloom.daemon.event_log.EventLog`, but instead of writing to
    disk it renders each envelope with :func:`_format_event` and emits it via
    :func:`typer.echo` so ``cloud.sse.*`` events appear inline in
    ``popola attach`` output alongside the daemon's ``/attach_stream`` feed.

    Per the v0.8.6 writer contract (``state-source-of-truth.md`` §1.2 rule 1),
    the SSE reader must NOT receive a :class:`StateStore` reference — it is
    structurally barred from ``cloud_phase`` mutation. This sink mirrors that
    property: it has no ``state_store`` attribute, never imports the daemon
    state module, and never makes a daemon RPC call.

    The collected ``events`` list is exposed for tests so they can introspect
    every envelope appended without having to capture stdout.
    """

    closed: bool = False

    def __init__(self, *, source: str = "popola/cli-cloud-sse") -> None:
        self._source = source
        self.events: list[dict[str, Any]] = []

    def append(self, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        envelope: dict[str, Any] = {
            "specversion": "1.0",
            "id": f"evt-cli-{uuid.uuid4().hex}",
            "source": self._source,
            "type": event_type,
            "time": _utc_now_iso_ms(),
            "data": data,
        }
        self.events.append(envelope)
        try:
            typer.echo(_format_event(envelope))
        except Exception as exc:  # noqa: BLE001 — render failures must not crash worker
            logger.warning(
                "cloud SSE sink failed to render event %s: %s", event_type, exc
            )
        return envelope


def _safe_status_payload(response: httpx.Response) -> dict[str, Any]:
    """Tolerantly parse a ``GET /status/{task_id}`` JSON body.

    Returns an empty dict on any decode failure or non-dict shape so the
    caller's ``runtime``/``cursor_agent_id`` lookups simply fall through to
    the legacy poll-only path (No-Silent-Failures: a warning is logged at
    DEBUG level).
    """
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        logger.debug("attach: status payload not JSON-decodable: %s", exc)
        return {}
    if not isinstance(body, dict):
        return {}
    return cast(dict[str, Any], body)


def _maybe_spawn_cloud_sse_thread(
    *,
    task_id: str,
    status_info: dict[str, Any],
    stop_event: threading.Event,
) -> threading.Thread | None:
    """Construct and start a daemon thread pumping Cursor SSE.

    Returns ``None`` (no thread spawned) when the prerequisites for the
    cloud SSE channel are not met:

    - ``cursor_agent_id`` or ``cursor_run_id`` missing on the status
      snapshot (the dispatch may have failed before the cloud
      ``POST /v1/agents`` returned, or runtime hydration is incomplete);
    - ``CURSOR_API_KEY`` env var is unset/empty (no cloud auth possible).

    Each fall-back path emits a ``[cloud sse] ...`` notice on stderr per
    the workspace **No-Silent-Failures** rule so the user understands why
    the SSE channel is silent and why we're using the existing poll-driven
    view alone.
    """
    agent_id = status_info.get("cursor_agent_id")
    run_id = status_info.get("cursor_run_id")
    if not agent_id or not isinstance(agent_id, str):
        logger.warning(
            "cloud SSE skipped for task_id=%s: cursor_agent_id missing/invalid (%r)",
            task_id,
            agent_id,
        )
        typer.echo(
            f"[cloud sse] cursor_agent_id missing for {task_id}; using poll-only view",
            err=True,
        )
        return None
    if not run_id or not isinstance(run_id, str):
        logger.warning(
            "cloud SSE skipped for task_id=%s: cursor_run_id missing/invalid (%r)",
            task_id,
            run_id,
        )
        typer.echo(
            f"[cloud sse] cursor_run_id missing for {task_id}; using poll-only view",
            err=True,
        )
        return None

    # v0.9.2: route through the resolver so SSE attach honours OS
    # keyring storage in addition to the historical env-var path. The
    # downgrade to poll-only when no slot answers preserves backward
    # compatibility with the v0.8.6+ stderr one-liner contract.
    from popolaloom.credentials import resolve_cursor_api_key

    api_key = resolve_cursor_api_key()
    if not api_key:
        logger.warning(
            "cloud SSE skipped for task_id=%s: no Cursor API key configured",
            task_id,
        )
        typer.echo(
            "[cloud sse] no Cursor API key configured "
            "(set CURSOR_API_KEY env or run `popola auth cursor set`); "
            "using poll-only view",
            err=True,
        )
        return None

    sink = _CloudSSEEventSink()
    thread = threading.Thread(
        target=_run_cloud_sse_pump,
        kwargs={
            "api_key": api_key,
            "task_id": task_id,
            "agent_id": agent_id,
            "run_id": run_id,
            "sink": sink,
            "stop_event": stop_event,
        },
        name=f"popola-cloud-sse-{task_id}",
        daemon=True,
    )
    thread.start()
    return thread


def _run_cloud_sse_pump(
    *,
    api_key: str,
    task_id: str,
    agent_id: str,
    run_id: str,
    sink: _CloudSSEEventSink,
    stop_event: threading.Event,
) -> None:
    """Pump Cursor SSE into ``sink`` until done, error, or ``stop_event``.

    Constructs a fresh :class:`CloudCursorClient` (one per attach session,
    closed in the ``finally`` block), wires it to a :class:`SSEReader`
    pointed at our duck-typed sink, and drives ``pump()``. ``pump()``
    already absorbs :class:`CursorCloudStreamExpiredError` and
    ``httpx.RemoteProtocolError`` / ``httpx.ReadError`` internally —
    emitting ``cloud.sse.stream_expired`` / ``cloud.sse.parse_error``
    envelopes — so the worker only has to catch the broader
    ``httpx.ConnectError`` / ``httpx.TimeoutException`` /
    :class:`CursorCloudStreamInvalidLastEventIdError` / generic
    :class:`CursorCloudError` cases.

    On any non-graceful exit (``stop_event`` not set), emits a single
    ``cloud.sse.fallback_to_poll`` envelope via the sink + a stderr
    one-liner so users see the transition to poll-only mode (AC e + g).
    """
    fallback_reason: str | None = None
    fallback_error: str | None = None
    client: CloudCursorClient | None = None
    try:
        try:
            client = CloudCursorClient(api_key)
        except (ValueError, CursorCloudError) as exc:
            logger.warning("cloud SSE client init failed: %s", exc)
            fallback_reason = "client_init_failed"
            fallback_error = f"{type(exc).__name__}: {exc}"
            return

        try:
            reader = SSEReader(
                client,
                cast("EventLog", sink),
                task_id,
                run_id,
                agent_id=agent_id,
            )
        except (TypeError, AssertionError) as exc:
            logger.warning("cloud SSE reader init rejected: %s", exc)
            fallback_reason = "reader_init_failed"
            fallback_error = f"{type(exc).__name__}: {exc}"
            return

        try:
            reader.pump(stop_event=stop_event)
        except CursorCloudStreamInvalidLastEventIdError as exc:
            logger.warning(
                "cloud SSE invalid Last-Event-ID for task_id=%s; aborting: %s",
                task_id,
                exc,
            )
            fallback_reason = "invalid_last_event_id"
            fallback_error = f"{type(exc).__name__}: {exc}"
        except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError) as exc:
            logger.warning(
                "cloud SSE network error for task_id=%s: %s", task_id, exc
            )
            fallback_reason = "network_error"
            fallback_error = f"{type(exc).__name__}: {exc}"
        except CursorCloudError as exc:
            logger.warning(
                "cloud SSE Cursor API error for task_id=%s: %s", task_id, exc
            )
            fallback_reason = "cursor_error"
            fallback_error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 — surface unexpected, then fallback
            logger.exception("cloud SSE unexpected error for task_id=%s", task_id)
            fallback_reason = "unexpected_error"
            fallback_error = f"{type(exc).__name__}: {exc}"
        else:
            fallback_reason = "stream_ended"
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001 — close failure is non-fatal
                logger.debug("cloud SSE client.close() failed: %s", exc)
        if not stop_event.is_set() and fallback_reason is not None:
            data: dict[str, Any] = {
                "reason": fallback_reason,
                "task_id": task_id,
            }
            if fallback_error is not None:
                data["error"] = fallback_error
            try:
                sink.append("cloud.sse.fallback_to_poll", data)
            except Exception:  # noqa: BLE001 — sink errors must never crash worker
                logger.debug("cloud SSE failed to append fallback envelope", exc_info=True)
            try:
                typer.echo(
                    f"[cloud sse] stream ended ({fallback_reason}); switching to poll",
                    err=True,
                )
            except Exception:  # noqa: BLE001 — stderr write must never crash worker
                logger.debug("cloud SSE failed to write stderr notice", exc_info=True)


# ── list ──────────────────────────────────────────────────────────────────


@app.command(name="list")
def list_active(
    state: str | None = typer.Option(
        None,
        "--state",
        help="Filter by state (running/pending). Default: all non-terminal.",
    ),
    include_terminal: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Include terminal tasks (completed/failed/canceled).",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
    no_runtime: bool = typer.Option(
        False,
        "--no-runtime",
        help=(
            "Hide the runtime column (escape hatch). v0.8.6: column is shown by "
            "default; --json output always includes the field."
        ),
    ),
) -> None:
    """List currently-running (non-terminal) tasks, optionally filtered by state.

    v0.8.6 (T2.1.2): the table gains a default-on ``runtime`` column showing
    ``local`` (subprocess) vs ``cloud`` (Cursor Cloud Agent) per row, sourced
    from ``TaskHandle.runtime`` via the daemon ``/list`` summary builder.
    The ``--no-runtime`` flag hides the column without affecting JSON output
    (``--json`` always emits the field, no schema change).
    Legacy rows missing the field render ``"-"`` (No Silent Failures).
    Column order: ``task_id, runtime, cli, state, pid, started_at``.
    """
    try:
        with make_sync_client() as client:
            r = client.get("/list", params={"include_terminal": include_terminal})
    except httpx.ConnectError as exc:
        _render_connect_error(exc)
        return

    if r.status_code != 200:
        typer.echo(f"error: list unexpected {r.status_code}: {r.text}", err=True)
        raise typer.Exit(code=1)

    items = r.json()
    if state is not None:
        items = [item for item in items if item.get("state") == state]

    if json_out:
        typer.echo(json.dumps(items, ensure_ascii=False))
        return

    if not items:
        _console_out.print(Text("No active tasks.", style="dim"))
        return

    table = Table(
        title="Active tasks" if not include_terminal else "All tasks",
        show_header=True,
        header_style="bold",
    )
    columns: tuple[str, ...] = (
        ("task_id", "cli", "state", "pid", "started_at")
        if no_runtime
        else ("task_id", "runtime", "cli", "state", "pid", "started_at")
    )
    for col in columns:
        table.add_column(col)
    for item in items:
        row: list[str] = [item.get("task_id", "")]
        if not no_runtime:
            row.append(item.get("runtime") or "-")
        row.extend(
            [
                item.get("cli", ""),
                item.get("state", ""),
                "" if item.get("pid") is None else str(item["pid"]),
                item.get("started_at", ""),
            ]
        )
        table.add_row(*row)
    _console_out.print(table)


# ── cancel ────────────────────────────────────────────────────────────────


@app.command()
def cancel(
    task_id: str = typer.Argument(
        ..., help="Task identifier to cancel (SIGTERM, SIGKILL after 5s)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a plain line."),
) -> None:
    """Send SIGTERM to a task subprocess; SIGKILL after 5s grace."""
    try:
        with make_sync_client() as client:
            r = client.post(f"/cancel/{task_id}")
    except httpx.ConnectError as exc:
        _render_connect_error(exc)
        return

    if r.status_code == 404:
        typer.echo(f"error: task not found: {task_id}", err=True)
        raise typer.Exit(code=1)
    if r.status_code == 409:
        typer.echo(f"error: cannot cancel: {r.json().get('detail', '')}", err=True)
        raise typer.Exit(code=1)
    if r.status_code != 200:
        typer.echo(f"error: cancel unexpected {r.status_code}: {r.text}", err=True)
        raise typer.Exit(code=1)

    info = r.json()
    if json_out:
        typer.echo(json.dumps(info, ensure_ascii=False))
    else:
        sig = info["requested_signal"]
        esc = " (escalated to SIGKILL)" if info["escalated_to_sigkill"] else ""
        typer.echo(f"cancel requested for {task_id}: {sig}{esc}")


# ── probe ─────────────────────────────────────────────────────────────────


@app.command()
def probe(
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Lightweight health probe — ``daemon_pid``, ``uptime``, ``active_tasks``."""
    try:
        with make_sync_client() as client:
            r = client.get("/probe")
    except httpx.ConnectError as exc:
        _render_connect_error(exc)
        return

    if r.status_code != 200:
        typer.echo(f"error: probe unexpected {r.status_code}: {r.text}", err=True)
        raise typer.Exit(code=1)

    info = r.json()
    if json_out:
        typer.echo(json.dumps(info, ensure_ascii=False))
        return

    table = Table(title="popolad probe", show_header=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    for key in ("daemon_pid", "started_at", "uptime_seconds", "active_tasks", "version"):
        table.add_row(key, str(info.get(key, "")))
    _console_out.print(table)


# ── helpers ───────────────────────────────────────────────────────────────


def _wait_for_terminal(task_id: str, *, timeout_s: float) -> None:
    """Poll ``/status/{task_id}`` until terminal or timeout."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    last_state = "<unknown>"
    try:
        with make_sync_client() as client:
            while True:
                r = client.get(f"/status/{task_id}")
                if r.status_code != 200:
                    typer.echo(
                        f"warning: --wait status {r.status_code}: {r.text}",
                        err=True,
                    )
                    return
                info = r.json()
                last_state = info["state"]
                if last_state in _TERMINAL_STATES:
                    return
                if time.monotonic() >= deadline:
                    typer.echo(
                        f"warning: --wait timed out after {timeout_s}s; "
                        f"task {task_id} still in state={last_state}",
                        err=True,
                    )
                    return
                time.sleep(_POLL_INTERVAL_S)
    except httpx.ConnectError as exc:
        _render_connect_error(exc)


def _parse_cli_flags(flags: list[str]) -> dict[str, Any]:
    """Parse repeatable ``--cli-flag KEY=VAL`` into a dict (R-012).

    Values are parsed as JSON when possible (e.g. ``yolo=true`` →
    ``{"yolo": True}``); falls back to string when JSON decoding fails
    (e.g. ``output_format=text`` → ``{"output_format": "text"}``).

    Raises:
        typer.BadParameter: when a flag is missing ``=``.
    """
    result: dict[str, Any] = {}
    for raw in flags:
        if "=" not in raw:
            raise typer.BadParameter(
                f"--cli-flag must be KEY=VAL form, got: {raw!r}"
            )
        key, _, value = raw.partition("=")
        key = key.strip()
        if not key:
            raise typer.BadParameter(f"--cli-flag missing key: {raw!r}")
        try:
            parsed: Any = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        result[key] = parsed
    return result


@dataclass(frozen=True, slots=True)
class _ReplayPayload:
    """Resolved dispatch parameters from a `--replay HANDOFF_ID` lookup."""

    cli: str
    prompt: str
    cwd: Path | None
    adapter_extra: dict[str, Any]


def _resolve_replay(
    handoff_id: str,
    user_prompt: str,
    user_cli: str,
    user_cwd: Path | None,
    user_cli_flag: list[str],
) -> _ReplayPayload:
    """Load envelope by id and produce a dispatch payload.

    v0.7.3+ ``popola dispatch --replay <handoff_id>`` reads the local
    envelope file and uses its stored fields (``target_cli`` / ``prompt`` /
    ``cwd`` / ``adapter_extra``) as the dispatch payload — an exact replay
    of a prior dispatch.

    If the user also passed ``prompt`` / ``--cli`` / ``--cwd`` /
    ``--cli-flag`` on the same invocation we WARN to stderr that those are
    overridden by the envelope (No Silent Failures — the user gets
    explicit feedback that their inline args were ignored).
    """
    from popolaloom.handoff import load_envelope

    try:
        env = load_envelope(handoff_id)
    except FileNotFoundError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        typer.echo(f"error: invalid handoff_id: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    overrides: list[str] = []
    if user_prompt:
        overrides.append(f"prompt={user_prompt!r}")
    if user_cli:
        overrides.append(f"--cli={user_cli!r}")
    if user_cwd is not None:
        overrides.append(f"--cwd={user_cwd!s}")
    if user_cli_flag:
        overrides.append(f"--cli-flag (×{len(user_cli_flag)})")

    if overrides:
        typer.echo(
            f"warning: --replay overrides inline {', '.join(overrides)} with envelope values",
            err=True,
        )

    return _ReplayPayload(
        cli=env.target_cli,
        prompt=env.prompt,
        cwd=Path(env.cwd) if env.cwd else None,
        adapter_extra=dict(env.adapter_extra) if env.adapter_extra else {},
    )


def _format_event(ev: dict[str, Any]) -> str:
    """Render a CloudEvents envelope as ``<time>  <type>  <data_summary>``."""
    time_s = ev.get("time", "")
    type_s = ev.get("type", "")
    data = ev.get("data", {})
    summary = _summarize_data(type_s, data)
    return f"{time_s}  {type_s}  {summary}"


def _summarize_data(event_type: str, data: Any) -> str:
    """Pick a short, human-friendly summary based on event type."""
    if not isinstance(data, dict):
        return repr(data)

    if event_type in {"process.stdout", "process.stderr"}:
        return str(data.get("line", ""))
    if event_type == "task.dispatched":
        return f"cli={data.get('cli')!r} prompt={data.get('prompt')!r}"
    if event_type in {"task.completed", "task.failed"}:
        parts: list[str] = [f"exit_code={data.get('exit_code')}"]
        runtime = data.get("runtime")
        if runtime and runtime != "local":
            parts.append(f"runtime={runtime}")
        error_kind = data.get("error_kind")
        if error_kind:
            parts.append(f"error_kind={error_kind}")
        err = data.get("error")
        if isinstance(err, dict):
            et = err.get("error_type")
            if et:
                parts.append(f"error_type={et}")
        return " ".join(parts)
    if event_type == "cloud.queued" and data:
        return (
            f"agent_id={data.get('agent_id')!r} "
            f"run_id={data.get('run_id')!r} "
            f"initial_phase={data.get('initial_phase')!r}"
        )
    # v0.8.8 T2.2.2 (Q-C-7 default-visibility): 429 / 409 queue events
    # render with a stable, grep-friendly prefix so attach UIs and tests
    # can match on the literal "WAITING:" / "DISPATCHED:" / "TIMEOUT:"
    # tokens. None of these events are debug-filtered: they ship inline
    # alongside the existing cloud.run_status / cloud.sse.* feed.
    if event_type == "cloud.queued_quota_exceeded":
        return _format_quota_waiting_line(data)
    if event_type == "cloud.queue_exit":
        outcome = data.get("outcome") if isinstance(data, dict) else None
        attempts = data.get("attempts") if isinstance(data, dict) else None
        wait_ms = data.get("total_wait_ms") if isinstance(data, dict) else None
        exit_parts: list[str] = ["QUEUE_EXIT"]
        if isinstance(outcome, str) and outcome:
            exit_parts.append(f"outcome={outcome}")
        if isinstance(attempts, int):
            exit_parts.append(f"attempts={attempts}")
        if isinstance(wait_ms, (int, float)) and wait_ms >= 0:
            exit_parts.append(f"total_wait={float(wait_ms) / 1000.0:.1f}s")
        return " ".join(exit_parts)
    if event_type == "cloud.busy_queued":
        return _format_busy_queue_line(data)
    if event_type == "cloud.busy_dispatched":
        agent = data.get("agent_id") if isinstance(data, dict) else None
        prev_run = data.get("prev_run_id") if isinstance(data, dict) else None
        new_run = data.get("new_run_id") if isinstance(data, dict) else None
        waited = data.get("waited_ms") if isinstance(data, dict) else None
        bits: list[str] = ["DISPATCHED:"]
        if isinstance(agent, str) and agent:
            bits.append(f"agent={agent}")
        if isinstance(prev_run, str) and prev_run:
            bits.append(f"prev_run={prev_run}")
        if isinstance(new_run, str) and new_run:
            bits.append(f"new_run={new_run}")
        if isinstance(waited, (int, float)) and waited >= 0:
            bits.append(f"waited={float(waited) / 1000.0:.1f}s")
        return " ".join(bits)
    if event_type == "cloud.busy_timeout":
        agent = data.get("agent_id") if isinstance(data, dict) else None
        run_id = (
            data.get("current_run_id_at_timeout")
            if isinstance(data, dict)
            else None
        )
        waited = data.get("waited_ms") if isinstance(data, dict) else None
        bits = ["TIMEOUT: agent_busy"]
        if isinstance(agent, str) and agent:
            bits.append(f"agent={agent}")
        if isinstance(run_id, str) and run_id:
            bits.append(f"current_run={run_id}")
        if isinstance(waited, (int, float)) and waited >= 0:
            bits.append(f"waited={float(waited) / 1000.0:.1f}s")
        return " ".join(bits)
    if event_type == "process.started":
        return f"pid={data.get('pid')} session_id={data.get('session_id')}"
    if event_type == "stream.truncated":
        return (
            f"stream={data.get('stream')} actual_lines={data.get('actual_lines')} "
            f"reason={data.get('reason')}"
        )
    if event_type == "state.ghost_exit":
        return f"reason={data.get('reason')!r} exit_code={data.get('exit_code')}"

    serialized = json.dumps(data, ensure_ascii=False, default=str)
    if len(serialized) > 120:
        serialized = serialized[:117] + "..."
    return serialized


def main() -> None:
    """Entry point for ``python -m popolaloom.cli.main`` debug invocations."""
    app()


if __name__ == "__main__":
    main()
