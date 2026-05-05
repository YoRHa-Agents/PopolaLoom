"""``popola popolad`` subcommand group — daemon process lifecycle (v0.2.0 Stage A A5).

Three subcommands:

- ``popola popolad start``: spawn ``python -m popolaloom.daemon`` in a new
  session (``setsid``-style detached); wait up to 5s for the UDS socket to
  appear; print ``popolad started, PID=<pid>`` on success.
- ``popola popolad stop``: read PID file → SIGTERM → wait up to 5s for
  graceful exit → SIGKILL fallback; remove PID + socket files.
- ``popola popolad status``: check socket existence + ``GET /health``
  succeeds; print structured status with rich Console.

Note: v0.2.0 does **not** integrate ``systemd-run --user --scope`` (R-010
deferred to v0.3.0); ``Popen + start_new_session=True`` is sufficient for
NFR-5 (≥99% cross-terminal survival, parent SIGHUP doesn't propagate).

Logs go to ``$POPOLA_HOME/log/popolad.log`` (default ``~/.popola/log/popolad.log``).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

__all__ = ["app"]


app = typer.Typer(
    name="popolad",
    help="Manage the popolad daemon process (start / stop / status).",
    no_args_is_help=True,
    add_completion=False,
)


_console_out = Console()


def _popola_home() -> Path:
    """Resolve ``$POPOLA_HOME`` (default ``~/.popola``); ensure dir exists.

    Inlined here (vs. importing from daemon/main.py) to avoid forcing
    daemon/main.py imports — and thus uvicorn — onto the CLI startup path.
    """
    home = os.environ.get("POPOLA_HOME")
    path = Path(home).expanduser().resolve() if home else Path.home() / ".popola"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _socket_path() -> Path:
    return _popola_home() / "popolad.sock"


def _pid_path() -> Path:
    return _popola_home() / "popolad.pid"


def _log_path() -> Path:
    log_dir = _popola_home() / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "popolad.log"


_SOCKET_WAIT_TIMEOUT_S: float = 5.0
_STOP_GRACE_S: float = 5.0
_POLL_INTERVAL_S: float = 0.05


@app.command()
def start(
    foreground: bool = typer.Option(
        False,
        "--foreground",
        help="Run popolad in the foreground (current terminal); don't detach.",
    ),
    timeout_s: float = typer.Option(
        _SOCKET_WAIT_TIMEOUT_S,
        "--timeout",
        help="Wait this many seconds for the UDS socket to appear.",
    ),
) -> None:
    """Start the popolad daemon (spawns ``python -m popolaloom.daemon``).

    On success prints ``popolad started, PID=<pid>``; on failure prints a
    helpful error and exits 1 (No Silent Failures).
    """
    sock = _socket_path()
    pid_file = _pid_path()
    log_file = _log_path()

    if pid_file.exists():
        try:
            existing_pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            existing_pid = -1
        if existing_pid > 0 and _pid_alive(existing_pid):
            typer.echo(
                f"error: popolad already running (PID={existing_pid}); "
                f"use `popola popolad stop` first or remove {pid_file}",
                err=True,
            )
            raise typer.Exit(code=1)

    if sock.exists():
        try:
            sock.unlink()
        except OSError as exc:
            typer.echo(f"error: stale socket exists at {sock}, cannot remove: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    cmd = [sys.executable, "-m", "popolaloom.daemon"]

    if foreground:
        os.execvp(cmd[0], cmd)

    with open(log_file, "ab", buffering=0) as log_fh:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=os.environ.copy(),
        )

    deadline = time.monotonic() + max(0.5, timeout_s)
    while time.monotonic() < deadline:
        if sock.exists() and _can_connect(sock):
            typer.echo(f"popolad started, PID={proc.pid}")
            typer.echo(f"socket: {sock}")
            typer.echo(f"log:    {log_file}")
            return
        if proc.poll() is not None:
            typer.echo(
                f"error: popolad subprocess exited prematurely (code={proc.returncode}); "
                f"see log: {log_file}",
                err=True,
            )
            raise typer.Exit(code=1)
        time.sleep(_POLL_INTERVAL_S)

    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    typer.echo(
        f"error: popolad failed to bind socket {sock} within {timeout_s}s; "
        f"see log: {log_file}",
        err=True,
    )
    raise typer.Exit(code=1)


@app.command()
def stop(
    grace_s: float = typer.Option(
        _STOP_GRACE_S,
        "--grace",
        help="Wait this many seconds for graceful SIGTERM exit before SIGKILL.",
    ),
) -> None:
    """Stop the running popolad daemon (SIGTERM, SIGKILL after grace)."""
    pid_file = _pid_path()
    sock = _socket_path()

    if not pid_file.exists():
        typer.echo("popolad not running (no PID file)")
        if sock.exists():
            with contextlib.suppress(OSError):
                sock.unlink()
        return

    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        typer.echo(f"error: PID file unreadable at {pid_file}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if not _pid_alive(pid):
        typer.echo(f"popolad PID file exists but process {pid} is gone; cleaning up")
        _cleanup_files(pid_file, sock)
        return

    typer.echo(f"sending SIGTERM to popolad PID={pid}")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        typer.echo(f"PID {pid} already gone; cleaning up files")
        _cleanup_files(pid_file, sock)
        return

    deadline = time.monotonic() + max(0.0, grace_s)
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            typer.echo(f"popolad PID={pid} exited gracefully")
            _cleanup_files(pid_file, sock)
            return
        time.sleep(_POLL_INTERVAL_S)

    typer.echo(f"popolad PID={pid} did not exit in {grace_s}s; sending SIGKILL", err=True)
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    time.sleep(0.1)
    _cleanup_files(pid_file, sock)


@app.command()
def status(
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Print popolad daemon status (socket existence + /health probe)."""
    sock = _socket_path()
    pid_file = _pid_path()

    state: dict[str, object] = {
        "socket_path": str(sock),
        "socket_exists": sock.exists(),
        "pid_file": str(pid_file),
        "pid": None,
        "pid_alive": False,
        "health": None,
        "probe": None,
    }

    if pid_file.exists():
        try:
            pid_val = int(pid_file.read_text(encoding="utf-8").strip())
            state["pid"] = pid_val
            state["pid_alive"] = _pid_alive(pid_val)
        except (OSError, ValueError) as exc:
            state["pid_file_error"] = str(exc)

    if sock.exists():
        try:
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=str(sock)),
                base_url="http://popolad",
                timeout=2.0,
            ) as client:
                r_health = client.get("/health")
                if r_health.status_code == 200:
                    state["health"] = r_health.json()
                else:
                    state["health"] = {"status_code": r_health.status_code}
                r_probe = client.get("/probe")
                if r_probe.status_code == 200:
                    state["probe"] = r_probe.json()
        except (httpx.HTTPError, httpx.ConnectError, OSError) as exc:
            state["http_error"] = repr(exc)

    if json_out:
        typer.echo(json.dumps(state, ensure_ascii=False, default=str))
        return

    table = Table(title="popolad daemon status", show_header=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    for key in (
        "socket_path",
        "socket_exists",
        "pid_file",
        "pid",
        "pid_alive",
        "health",
        "probe",
    ):
        value = state.get(key)
        table.add_row(key, "" if value is None else str(value))
    if "http_error" in state:
        table.add_row("http_error", str(state["http_error"]))
    _console_out.print(table)

    is_up = bool(state["socket_exists"] and state["health"])
    if not is_up:
        raise typer.Exit(code=1)


def _pid_alive(pid: int) -> bool:
    """Return True iff signal 0 reaches ``pid`` (process exists + we have permission).

    Returns False on ProcessLookupError or PermissionError (don't raise on
    common races).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _can_connect(sock: Path) -> bool:
    """Return True iff a connection to ``sock`` succeeds (best-effort)."""
    try:
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(sock)),
            base_url="http://popolad",
            timeout=1.0,
        ) as client:
            r = client.get("/health")
            return r.status_code == 200
    except (httpx.HTTPError, OSError):
        return False


def _cleanup_files(pid_file: Path, sock: Path) -> None:
    """Best-effort cleanup of PID + socket files (logs but does not raise)."""
    for p in (pid_file, sock):
        try:
            if p.exists():
                p.unlink()
        except OSError as exc:
            typer.echo(f"warning: could not remove {p}: {exc}", err=True)
