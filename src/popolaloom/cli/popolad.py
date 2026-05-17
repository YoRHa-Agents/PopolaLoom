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

v1.5.0 (feedback_for_v1.4.0 G8 + G9) — ``start`` now resolves the
daemon child env by walking a 4-tier injection chain (see
:func:`_resolve_child_env`):

1. explicit ``--env-file <path>`` argument (when supplied)
2. ``~/.popola/cursor_api_key.env`` (existing boot-time fallback,
   honored by the daemon side at :func:`popolaloom.credentials.load_env_fallback_into_environ`;
   this CLI side injects it into the child env so the wheel-installed
   ``popola popolad start`` works the same regardless of how the shell
   is configured)
3. ``<cwd>/.local/.secrets/cursor_user_api_key.secret`` (single-line
   bare key, by-convention path documented in
   ``feedback_for_v1.4.0.md`` G8). **CLI-side env injection only;
   does NOT participate in
   :func:`popolaloom.credentials.resolve_cursor_api_key` precedence**)
4. ``<cwd>/.env`` (when ``mode == 0o600``; legacy dotenv fallback)

All paths require ``mode == 0o600``; non-secure modes log a warning
and are skipped (No Silent Failures).

``--reload-env`` is an opt-in alias for ``popola popolad stop &&
popola popolad start <same-args>`` — single-command convenience for
"my shell now has new ``CURSOR_API_KEY``; please re-inject without me
typing two commands"; we accept the cost of brief downtime over the
risk of a multi-process race condition.
"""

from __future__ import annotations

import contextlib
import json
import logging
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

logger = logging.getLogger(__name__)

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
_REQUIRED_ENV_FILE_MODE: int = 0o600
_REQUIRED_ENV_FILE_MODE_MASK: int = 0o777
_CURSOR_API_KEY_ENV: str = "CURSOR_API_KEY"


def _check_secure_mode(path: Path) -> bool:
    """Return True iff ``path`` exists with mode ``0o600``.

    Permissive failure mode: any other mode → False + typer WARN, so we
    skip the file without aborting the chain (No Silent Failures: the
    skip is visible; the operator can fix the mode and retry).
    """
    try:
        actual_mode = path.stat().st_mode & _REQUIRED_ENV_FILE_MODE_MASK
    except OSError:
        return False
    if actual_mode != _REQUIRED_ENV_FILE_MODE:
        typer.echo(
            f"warn: env file {path} has mode {oct(actual_mode)} "
            f"(expected {oct(_REQUIRED_ENV_FILE_MODE)}); skipping for "
            f"safety. (env 文件权限不安全,已跳过)",
            err=True,
        )
        return False
    return True


def _parse_env_file_contents(text: str, source: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines from ``text``; tolerant of ``#`` comments + blanks.

    Returns an empty dict on any parse issue; emits a typer warn for
    each malformed row (No Silent Failures). Stripping respects shell
    quoting only for the simplest cases (matching values in single or
    double quotes).
    """
    out: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            typer.echo(
                f"warn: malformed env line in {source} L{lineno}: {raw!r}",
                err=True,
            )
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ('"', "'")
        ):
            value = value[1:-1]
        if not key:
            typer.echo(
                f"warn: missing key in {source} L{lineno}: {raw!r}",
                err=True,
            )
            continue
        out[key] = value
    return out


def _load_kv_env_file(path: Path) -> dict[str, str]:
    """Read a ``KEY=VALUE`` env file at ``path``; returns ``{}`` on any failure.

    Enforces 0o600 mode (skip with WARN otherwise — see
    :func:`_check_secure_mode`). Returns empty when the file is absent
    OR every row was malformed.
    """
    if not path.is_file() or not _check_secure_mode(path):
        return {}
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(
            f"warn: could not read env file {path}: {exc} (skipping)",
            err=True,
        )
        return {}
    return _parse_env_file_contents(contents, path)


def _load_bare_secret_file(path: Path) -> dict[str, str]:
    """Read a single-line bare-secret file → ``{"CURSOR_API_KEY": <contents>}``.

    Matches the by-convention shape of the path G8 of
    ``feedback_for_v1.4.0.md`` references
    (``.local/.secrets/cursor_user_api_key.secret``): a single line of
    raw key material, NO ``KEY=`` prefix. Enforces 0o600 mode and skips
    silently when the file is absent.
    """
    if not path.is_file() or not _check_secure_mode(path):
        return {}
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        typer.echo(
            f"warn: could not read bare-secret file {path}: {exc} (skipping)",
            err=True,
        )
        return {}
    if not raw:
        return {}
    # Defensive: if the operator wrote KEY=VALUE format anyway, treat
    # it as a KV file (single line). Otherwise treat the whole content
    # as the key value.
    if "=" in raw and raw.partition("=")[0].strip().isidentifier():
        return _parse_env_file_contents(raw, path)
    return {_CURSOR_API_KEY_ENV: raw}


def _resolve_child_env(
    *,
    cwd: Path,
    env_file: Path | None,
) -> tuple[dict[str, str], list[str]]:
    """Build the child env dict by walking the 4-tier v1.5.0 injection chain.

    Returns a (env, sources) tuple. ``env`` is suitable for ``Popen(..., env=env)``;
    ``sources`` is a human-readable list of paths actually consulted (in
    order; only paths that contributed a value are recorded), used by
    the CLI surface to print a "loaded from: X, Y" diagnostic so the
    operator can see exactly what got injected.

    Precedence (highest → lowest; first non-empty value wins):

    1. existing ``os.environ`` (operator's shell)
    2. ``--env-file <path>``
    3. ``~/.popola/cursor_api_key.env`` (existing boot-time fallback)
    4. ``<cwd>/.local/.secrets/cursor_user_api_key.secret`` (G8;
       bare-key convention)
    5. ``<cwd>/.env`` (mode 0600; legacy dotenv fallback)

    NB: the resolution is "fill in the gaps" — keys already set in
    ``os.environ`` are NEVER overwritten by a lower-precedence source.
    This matches the v0.9.9 daemon-side
    :func:`popolaloom.credentials.load_env_fallback_into_environ`
    precedence rule (env-var wins).
    """
    env: dict[str, str] = os.environ.copy()
    sources: list[str] = []

    def _merge(src_path: Path, payload: dict[str, str]) -> None:
        contributed = False
        for k, v in payload.items():
            if k in env and env[k]:
                continue
            env[k] = v
            contributed = True
        if contributed:
            sources.append(str(src_path))

    if env_file is not None:
        _merge(env_file, _load_kv_env_file(env_file))

    _merge(
        _popola_home() / "cursor_api_key.env",
        _load_kv_env_file(_popola_home() / "cursor_api_key.env"),
    )

    workspace_secret = cwd / ".local" / ".secrets" / "cursor_user_api_key.secret"
    _merge(workspace_secret, _load_bare_secret_file(workspace_secret))

    workspace_env = cwd / ".env"
    _merge(workspace_env, _load_kv_env_file(workspace_env))

    return env, sources


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
    env_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--env-file",
        help=(
            "v1.5.0 — load KEY=VALUE pairs from <path> into the daemon "
            "subprocess env (highest-precedence file source; only fills "
            "keys not already set in os.environ). Mode must be 0o600. "
            "(v1.5.0 — 从 <path> 读取 KEY=VALUE 注入到 daemon 子进程 env。)"
        ),
    ),
    reload_env: bool = typer.Option(
        False,
        "--reload-env",
        help=(
            "v1.5.0 — convenience flag equivalent to "
            "`popola popolad stop && popola popolad start <same flags>` "
            "so the operator can re-inject env after editing "
            "~/.popola/cursor_api_key.env or .local/.secrets/cursor_user_api_key.secret "
            "without typing two commands. Accepts brief downtime to avoid "
            "multi-process state races. "
            "(v1.5.0 — 等价于 stop+start,用于刷新 env 注入。)"
        ),
    ),
) -> None:
    """Start the popolad daemon (spawns ``python -m popolaloom.daemon``).

    On success prints ``popolad started, PID=<pid>``; on failure prints a
    helpful error and exits 1 (No Silent Failures).

    v1.5.0 env injection (see module docstring): walks the 4-tier chain
    to build the child env so the daemon picks up CURSOR_API_KEY even
    when the operator's shell hasn't ``export``-ed it (feedback_for_v1.4.0
    G8 + G9).
    """
    sock = _socket_path()
    pid_file = _pid_path()
    log_file = _log_path()

    # v1.5.0 --reload-env: stop the existing daemon (if any) so the
    # subsequent start picks up the freshly-resolved env. We deliberately
    # serialise stop→start (vs. a SIGHUP / live-reload) because the
    # daemon's child-spawned subprocesses can NOT have their env updated
    # in-place; the only correct re-injection is a clean process boundary.
    if reload_env and pid_file.exists():
        try:
            existing_pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            existing_pid = -1
        if existing_pid > 0 and _pid_alive(existing_pid):
            typer.echo(
                "[--reload-env] stopping existing popolad to re-inject env...",
                err=True,
            )
            with contextlib.suppress(ProcessLookupError):
                os.kill(existing_pid, signal.SIGTERM)
            deadline = time.monotonic() + _STOP_GRACE_S
            while time.monotonic() < deadline:
                if not _pid_alive(existing_pid):
                    break
                time.sleep(_POLL_INTERVAL_S)
            if _pid_alive(existing_pid):
                typer.echo(
                    f"[--reload-env] PID {existing_pid} did not exit within "
                    f"{_STOP_GRACE_S}s; sending SIGKILL",
                    err=True,
                )
                with contextlib.suppress(ProcessLookupError):
                    os.kill(existing_pid, signal.SIGKILL)
                time.sleep(0.2)
            _cleanup_files(pid_file, sock)

    if pid_file.exists():
        try:
            existing_pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            existing_pid = -1
        if existing_pid > 0 and _pid_alive(existing_pid):
            typer.echo(
                f"error: popolad already running (PID={existing_pid}); "
                f"use `popola popolad stop` first (or pass --reload-env "
                f"to stop+start in one shot) or remove {pid_file}",
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

    child_env, env_sources = _resolve_child_env(
        cwd=Path.cwd(),
        env_file=env_file,
    )
    if env_sources:
        typer.echo(
            f"[env] injected from: {', '.join(env_sources)}",
            err=True,
        )
    if _CURSOR_API_KEY_ENV not in child_env:
        typer.echo(
            "warn: no CURSOR_API_KEY found in os.environ, --env-file, "
            "~/.popola/cursor_api_key.env, "
            "<cwd>/.local/.secrets/cursor_user_api_key.secret, or "
            "<cwd>/.env (0o600). Cloud dispatches will fail with "
            "missing_api_key until you set one. "
            "(未找到 CURSOR_API_KEY;云端派发将失败)",
            err=True,
        )

    if foreground:
        # NB: os.execvp does not pass env unless we use execvpe; keep
        # the historical behaviour of inheriting the current process's
        # env in foreground mode (the operator typically pre-exports
        # CURSOR_API_KEY before --foreground anyway). Honor --env-file
        # by exporting matching keys into os.environ first so the
        # foreground daemon sees them.
        for k, v in child_env.items():
            os.environ.setdefault(k, v)
        os.execvp(cmd[0], cmd)

    with open(log_file, "ab", buffering=0) as log_fh:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=child_env,
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
