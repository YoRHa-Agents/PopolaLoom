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
import time
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from popolaloom import __version__
from popolaloom.adapters import get_adapter, list_registered

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
    """
    from popolaloom.cli.doctor_cmd import doctor_command
    from popolaloom.cli.eval import app as eval_app
    from popolaloom.cli.handoff_cmd import app as handoff_app
    from popolaloom.cli.init_cmd import app as init_app
    from popolaloom.cli.popolad import app as popolad_app
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
    prompt: str = typer.Argument(..., help="Prompt string forwarded to the chosen CLI."),
    cli: str = typer.Option(..., "--cli", help="CLI adapter name (cursor/claude/codex/...)."),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Working directory for the spawned subprocess (defaults to popolad's CWD).",
    ),
    cli_flag: list[str] = typer.Option(  # noqa: B008
        [],
        "--cli-flag",
        help="Repeatable adapter extras KEY=VAL (e.g. --cli-flag yolo=true). R-012.",
    ),
    events_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--events-dir",
        help="Override events_dir (advisory; daemon owns actual write path). R-014 part.",
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
    """Dispatch a new task to popolad and (optionally) wait for completion."""
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
) -> None:
    """Query a task's runtime status (state / pid / exit_code / timestamps)."""
    try:
        with make_sync_client() as client:
            r = client.get(f"/status/{task_id}")
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

    if json_out:
        typer.echo(json.dumps(info, ensure_ascii=False))
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
) -> None:
    """Print task events as ``<time>  <type>  <data_summary>`` lines.

    R-005 fix: ``--follow`` defaults to **True** so cross-process attach
    sees new events live. Use ``--no-follow`` for the legacy one-shot dump.
    """
    if follow:
        _attach_streaming(task_id, from_index=from_index)
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


def _attach_streaming(task_id: str, *, from_index: int) -> None:
    """Long-poll the SSE attach_stream endpoint until terminal or Ctrl-C."""
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
                timeout=httpx.Timeout(connect=5.0, read=None, write=10.0, pool=10.0),
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
) -> None:
    """List currently-running (non-terminal) tasks, optionally filtered by state."""
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
    for col in ("task_id", "cli", "state", "pid", "started_at"):
        table.add_column(col)
    for item in items:
        table.add_row(
            item.get("task_id", ""),
            item.get("cli", ""),
            item.get("state", ""),
            "" if item.get("pid") is None else str(item["pid"]),
            item.get("started_at", ""),
        )
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
        return f"exit_code={data.get('exit_code')}"
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
