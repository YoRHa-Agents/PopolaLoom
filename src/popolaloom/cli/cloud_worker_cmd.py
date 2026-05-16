"""``popola cloud worker`` subcommand group — v0.9.1 self-hosted worker UX.

Self-hosted Cursor worker handoff helpers — wraps the upstream ``agent
worker`` CLI verbs (``debug`` / ``start``) and adds two PopolaLoom-only
verbs (``status`` / ``handoff``) plus a workspace-aware dispatch helper so
operators on this machine can:

1. ``popola cloud worker debug`` — preflight diagnostics that pass
   through to ``agent worker debug``.
2. ``popola cloud worker start`` — start a Cursor self-hosted worker
   process (My Machines mode by default; ``--pool`` opts into
   Self-Hosted Pool which requires a service-account API key).
3. ``popola cloud worker status`` — poll the worker's optional
   management server (``/healthz``, ``/readyz``, ``/metrics``) without
   needing ``CURSOR_API_KEY``.
4. ``popola cloud worker handoff`` — emit a copy-paste-ready Cloud
   Agents handoff (URL + prompt) for the My Machines / dashboard flow,
   explicitly noting that no PopolaLoom task id is created until a real
   REST dispatch happens.
5. ``popola cloud worker dispatch`` — directly POST to ``popolad`` with
   ``cli=cursor-cloud`` extras that route to the current workspace worker
   by ``worker_name`` (``--print-only`` / ``--dry-run`` keeps the preview
   behavior).

Design boundary (per v0.9.1 plan §"Design constraints"):

- ``agent worker start`` registers this machine for browser-driven (or
  trigger-surface-driven) Cloud Agent runs. ``worker dispatch`` is the
  PopolaLoom-tracked REST path: it contacts ``popolad`` and asks the
  ``cursor-cloud`` adapter to route to the detected workspace worker.
- Pool mode requires a **service-account API key** (Enterprise);
  PopolaLoom refuses to launch a pool worker when ``CURSOR_API_KEY`` is
  unset (No Silent Failures).  Shared / "My Machines" workers happily
  inherit the user's browser-based ``agent login`` session.
- All failure paths exit non-zero with a stderr message naming the
  exact missing prerequisite — never fall back to a different mode.
- ``start`` is workspace-reuse-first: the default worker name includes
  the repo/workspace name + stable path hash, and a running worker with
  the same resolved ``--worker-dir`` is reused unless
  ``--allow-duplicate`` is passed.

v0.10.0 (DECISIONS Q-3 + Q-4 + Q-7 in
``.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md``)
replaces the v0.9.9 ``_enforce_account_class_pre_flight_gate`` (which was
based on the now-disconfirmed Spike-0 BRANCH_B verdict) with
``_enforce_self_hosted_worker_exists``:

- **Q-3 (worker discovery)** — the new gate calls
  :func:`popolaloom.cloud.preflight.check_self_hosted_worker_exists`
  which probes ``GET /v0/private-workers`` (works under personal API
  keys per research/01 PROBE_07/44) to verify the named worker is
  registered before the dispatch attempt.
- **Q-4 (pre-flight gate semantics)** — the v0.9.9 ``account_class``
  hard-fail gate is DELETED; the worker-existence pre-check (Q-3)
  takes its slot in :func:`worker_dispatch_cmd`. Exit code 78 is
  preserved (renamed constant ``_EXIT_PRE_FLIGHT_GATE``) because the
  operator-meaning ("operator must change account/config to proceed")
  is unchanged across the gate semantics swap.
- **Q-7 (no-fallback contract)** — when the named worker is missing
  the gate hard-exits with a bilingual hint pointing at
  ``popola cloud worker start --name <X> --worker-dir <repo-root>``
  (the actual fix). It NEVER suggests ``--cli=cursor`` or any other
  local-CLI fallback path; cloud dispatch and local execution are
  semantically distinct per ``feedbacks/feedback_for_v0.10.0.md``
  L5+L11.

The module's three indirection points (``_resolve_agent_binary``,
``_run_subprocess``, ``_fetch_management_endpoint``) are factored so
:file:`tests/cli/test_cloud_worker_cmd.py` can monkeypatch them without
touching real network or real subprocesses.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import FrameType
from typing import Any, NoReturn, cast

import httpx
import typer
from rich.console import Console
from rich.table import Table

logger = logging.getLogger(__name__)

__all__ = ["app"]


# ── exit code constants (mirrored from cloud_cmd.py for consistency) ──────


_EXIT_OK: int = 0
_EXIT_UNREACHABLE: int = 1
_EXIT_INVALID_ARGS: int = 2
_EXIT_MISSING_AGENT_BINARY: int = 4
_EXIT_POOL_REQUIRES_API_KEY: int = 77
"""Pool worker requested but ``CURSOR_API_KEY`` unset; mirrors
``_EXIT_CLOUD_AUTH_ERROR`` in ``cloud_cmd.py`` so scripts can branch on
the same code regardless of which sub-verb hit the auth gap."""

_EXIT_PRE_FLIGHT_GATE: int = 78
"""Cloud-dispatch pre-flight gate refused — the operator must change
account / config / worker registration to proceed.

v0.10.0 (DECISIONS Q-4) re-uses the v0.9.9 ``_EXIT_ACCOUNT_CLASS_GATE``
slot for the new worker-existence pre-check. The numeric value (78) is
unchanged so any script branching on ``78 == "operator must change
something about their account / config to proceed"`` keeps a coherent
meaning across cloud sub-verbs. Mirrors ``forbidden_plan_required`` /
GitHub-App integration error ``cli_exit=78`` in
``adapters/cursor_cloud._ERROR_CATALOG``."""


# ── default constants ────────────────────────────────────────────────────


_DEFAULT_MANAGEMENT_ADDR: str = "127.0.0.1:39231"
"""Default ``--management-addr`` for ``status`` lookups; matches the
quickstart docs so a status-only flow doesn't need to hunt for the port.
Operators who pass a different addr to ``start`` MUST pass the same
addr to ``status`` (the worker only listens on its configured port)."""

_DEFAULT_AGENT_BINARIES: tuple[str, ...] = ("agent", "cursor-agent")
"""Resolution order for the upstream worker CLI binary.  The 2026.05.07
release of cursor-agent installs both names symlinked at the same path;
older installs may only ship one or the other.  We accept either."""

_WORKER_CMD_BASENAMES: frozenset[str] = frozenset({"agent", "cursor-agent"})
"""Upstream worker binary basenames recognised by the /proc cmdline scanner."""

_DEFAULT_PROC_ROOT: Path = Path("/proc")
"""Linux procfs root used for duplicate-worker detection; injectable in tests."""

_DEFAULT_HEALTH_TIMEOUT_S: float = 3.0
"""Per-request timeout when reading ``/healthz`` / ``/readyz`` /
``/metrics``.  The endpoints are loopback so latency is trivial; the
short timeout keeps a misconfigured ``--management-addr`` from blocking
the CLI for the httpx default 5 s."""


_console_out = Console()


app = typer.Typer(
    name="worker",
    help=(
        "Self-hosted Cursor worker helpers (v0.9.1+). Wraps `agent worker "
        "debug` / `agent worker start` and adds status / handoff verbs."
    ),
    no_args_is_help=True,
    add_completion=False,
)


# ── helpers (pure / monkey-patchable) ─────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LocalWorkerProcess:
    """Metadata extracted from a running local Cursor worker process."""

    pid: int
    worker_dir: Path
    name: str | None
    management_addr: str | None
    argv: tuple[str, ...]


def _resolve_agent_binary() -> str:
    """Return the absolute path to the ``agent`` (or ``cursor-agent``) CLI.

    Raises :class:`typer.Exit` with code :data:`_EXIT_MISSING_AGENT_BINARY`
    when neither name resolves on ``$PATH`` — failing here lets the user
    install ``agent`` (``curl https://cursor.com/install -fsS | bash``)
    without seeing a confusing ``FileNotFoundError`` from ``subprocess``.
    """
    for candidate in _DEFAULT_AGENT_BINARIES:
        resolved = shutil.which(candidate)
        if resolved is not None:
            return resolved
    typer.echo(
        "error: cursor `agent` CLI not found on PATH; install it via "
        "`curl https://cursor.com/install -fsS | bash` and retry "
        "(see https://cursor.com/docs/cloud-agent/my-machines for the "
        "full quickstart).",
        err=True,
    )
    raise typer.Exit(code=_EXIT_MISSING_AGENT_BINARY)


def _resolve_worker_dir(worker_dir: Path) -> Path:
    """Return the normalized absolute worker directory path."""
    return worker_dir.expanduser().resolve(strict=False)


def _sanitize_worker_name_component(value: str) -> str:
    """Return an ASCII-safe worker-name component."""
    ascii_value = value.encode("ascii", errors="ignore").decode("ascii")
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "-", ascii_value).strip("-._")
    return sanitized or "workspace"


def _default_worker_name(worker_dir: Path) -> str:
    """Return a deterministic workspace-aware worker name for ``worker_dir``."""
    resolved = _resolve_worker_dir(worker_dir)
    repo_name = _sanitize_worker_name_component(resolved.name)
    digest = sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"popolaloom-{repo_name}-{digest}"


def _validate_management_addr(addr: str) -> tuple[str, int]:
    """Parse a ``host:port`` (or ``:port``) management-server address.

    The upstream ``agent worker start`` flag accepts both forms (e.g.
    ``"127.0.0.1:8080"`` and ``":8080"``).  Returns ``(host, port)``
    where ``host`` defaults to ``"127.0.0.1"`` for the bare-port form;
    raises :class:`typer.Exit` with :data:`_EXIT_INVALID_ARGS` when the
    string is malformed (No Silent Failures).
    """
    raw = addr.strip()
    if not raw:
        typer.echo("error: --management-addr must be non-empty", err=True)
        raise typer.Exit(code=_EXIT_INVALID_ARGS)
    if raw.startswith(":"):
        host = "127.0.0.1"
        port_str = raw[1:]
    elif ":" in raw:
        host, port_str = raw.rsplit(":", 1)
    else:
        typer.echo(
            f"error: --management-addr must be 'host:port' or ':port', got {raw!r}",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)
    if not port_str.isdigit():
        typer.echo(
            f"error: --management-addr port must be an integer, got {port_str!r}",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)
    port = int(port_str)
    if not 1 <= port <= 65535:
        typer.echo(
            f"error: --management-addr port must be in [1, 65535], got {port}",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)
    return (host or "127.0.0.1", port)


def _validate_label(label: str) -> tuple[str, str]:
    """Split a ``key=value`` label string.

    Mirrors the upstream ``agent worker start --label k=v`` shape with
    one extra rule: both halves must be non-empty.  Raises
    :class:`typer.Exit` with :data:`_EXIT_INVALID_ARGS` when malformed
    so a typo in a long label list surfaces at the popola boundary
    instead of mid-worker-startup.
    """
    if "=" not in label:
        typer.echo(
            f"error: --label must be 'key=value', got {label!r}",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)
    key, value = label.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key or not value:
        typer.echo(
            f"error: --label key and value must both be non-empty (got {label!r})",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)
    return (key, value)


def _extract_flag_value(argv: list[str], flag: str) -> str | None:
    """Extract ``--flag value`` or ``--flag=value`` from ``argv``."""
    for idx, token in enumerate(argv):
        if token == flag:
            if idx + 1 < len(argv):
                return argv[idx + 1]
            return None
        prefix = f"{flag}="
        if token.startswith(prefix):
            return token[len(prefix) :]
    return None


def _parse_worker_start_cmdline(
    pid: int,
    argv: list[str],
) -> LocalWorkerProcess | None:
    """Parse a procfs cmdline into worker metadata when it is a worker start.

    v1.3.0 P3 (``feedback_for_v1.2.0.md`` §7 "popola cloud worker stop
    当前定位 bug"): the matcher now handles Node-wrapped invocations
    like ``["node", "/usr/lib/cursor-agent/agent.js", "worker", "start",
    "--worker-dir", "/x"]`` in addition to the direct
    ``["agent", "worker", "start", "--worker-dir", "/x"]`` form.

    The modern cursor-agent install ships ``agent`` as a small shell shim
    that ``exec``s ``node /path/to/agent.js worker start ...``; the
    running process therefore has ``argv[0] == "node"`` and the
    pre-v1.3.0 basename-only check failed to identify it as a worker.
    The rule is now:

    1. argv must contain the contiguous subsequence ``["worker",
       "start"]`` at some position ``>= 1`` (a flag named ``--worker``
       followed by something else stays a non-match), AND
    2. argv must include a "worker binary indicator" token anywhere —
       basename in :data:`_WORKER_CMD_BASENAMES` (``agent`` /
       ``cursor-agent``) OR a token ending in ``/agent.js`` /
       ``/cursor-agent.js`` (case-insensitive — POSIX paths are case-
       sensitive but defending here is cheap).

    The change is strictly more permissive than the v1.1.1 matcher: every
    argv the old matcher accepted (``argv[0]`` basename in the allow-list,
    ``argv[1:3] == ["worker","start"]``) still satisfies the new rule.
    """
    if len(argv) < 3:
        return None
    worker_idx = -1
    for i in range(1, len(argv) - 1):
        if argv[i] == "worker" and argv[i + 1] == "start":
            worker_idx = i
            break
    if worker_idx < 0:
        return None
    has_indicator = False
    for token in argv:
        if not token:
            continue
        base = Path(token).name.lower()
        if base in _WORKER_CMD_BASENAMES:
            has_indicator = True
            break
        lower = token.lower()
        if lower.endswith("/agent.js") or lower.endswith("/cursor-agent.js"):
            has_indicator = True
            break
    if not has_indicator:
        return None
    worker_dir_raw = _extract_flag_value(argv, "--worker-dir")
    if worker_dir_raw is None:
        return None
    return LocalWorkerProcess(
        pid=pid,
        worker_dir=_resolve_worker_dir(Path(worker_dir_raw)),
        name=_extract_flag_value(argv, "--name"),
        management_addr=_extract_flag_value(argv, "--management-addr"),
        argv=tuple(argv),
    )


def _iter_proc_cmdlines(
    proc_root: Path = _DEFAULT_PROC_ROOT,
) -> Iterator[tuple[int, list[str]]]:
    """Yield ``(pid, argv)`` from Linux procfs; fail open when unavailable."""
    try:
        entries = list(proc_root.iterdir())
    except OSError as exc:
        logger.debug("worker detection: cannot read %s: %s", proc_root, exc)
        return

    for entry in entries:
        if not entry.name.isdigit():
            continue
        cmdline_path = entry / "cmdline"
        try:
            raw = cmdline_path.read_bytes()
        except OSError as exc:
            logger.debug(
                "worker detection: cannot read %s: %s",
                cmdline_path,
                exc,
            )
            continue
        if not raw:
            continue
        argv = [
            chunk.decode("utf-8", errors="replace")
            for chunk in raw.split(b"\0")
            if chunk
        ]
        if argv:
            yield int(entry.name), argv


def _detect_running_workers_for_dir(
    worker_dir: Path,
    *,
    proc_root: Path = _DEFAULT_PROC_ROOT,
) -> list[LocalWorkerProcess]:
    """Return running local worker processes whose ``--worker-dir`` matches."""
    target = _resolve_worker_dir(worker_dir)
    matches: list[LocalWorkerProcess] = []
    for pid, argv in _iter_proc_cmdlines(proc_root):
        parsed = _parse_worker_start_cmdline(pid, argv)
        if parsed is None:
            continue
        if parsed.worker_dir == target:
            matches.append(parsed)
    return matches


def _build_debug_argv(
    *,
    binary: str,
    worker_dir: Path,
    name: str | None,
    pool: bool,
    pool_name: str | None,
    labels: list[tuple[str, str]],
) -> list[str]:
    """Construct the ``agent worker debug`` argv list (pure)."""
    cmd: list[str] = [binary, "worker", "debug", "--worker-dir", str(worker_dir)]
    if name is not None:
        cmd.extend(["--name", name])
    if pool:
        cmd.append("--pool")
        if pool_name is not None:
            cmd.extend(["--pool-name", pool_name])
    for key, value in labels:
        cmd.extend(["--label", f"{key}={value}"])
    return cmd


def _build_start_argv(
    *,
    binary: str,
    worker_dir: Path,
    name: str | None,
    pool: bool,
    pool_name: str | None,
    idle_release_timeout: int | None,
    labels: list[tuple[str, str]],
    management_addr: str | None,
) -> list[str]:
    """Construct the ``agent worker start`` argv list (pure)."""
    cmd: list[str] = [binary, "worker", "start", "--worker-dir", str(worker_dir)]
    if name is not None:
        cmd.extend(["--name", name])
    if pool:
        cmd.append("--pool")
        if pool_name is not None:
            cmd.extend(["--pool-name", pool_name])
    if idle_release_timeout is not None:
        cmd.extend(["--idle-release-timeout", str(idle_release_timeout)])
    for key, value in labels:
        cmd.extend(["--label", f"{key}={value}"])
    if management_addr is not None:
        cmd.extend(["--management-addr", management_addr])
    return cmd


def _run_subprocess(argv: list[str]) -> int:
    """Run ``argv`` synchronously; return its exit code.

    Factored out so :file:`tests/cli/test_cloud_worker_cmd.py` can
    monkeypatch a no-op recorder without spawning a real subprocess.
    Streams stdout / stderr to the parent terminal so operators see the
    upstream ``agent worker`` output verbatim (No Silent Failures).

    v0.9.9 (F6 — closes ``feedback_for_v0.9.7.md:88-96``): the child is
    spawned via :class:`subprocess.Popen` with ``start_new_session=True``
    (calls :func:`os.setsid` between fork and exec) so the Node
    ``agent worker start`` process becomes the leader of its own
    process group. SIGTERM and SIGINT delivered to *this* Python
    wrapper are then cascaded to the entire child group via
    :func:`os.killpg`, which fixes the orphaning problem where
    ``kill <wrapper-pid>`` against
    ``nohup popola cloud worker start ... &`` left the Node child
    re-parented to PID 1 and still connected to Cursor's cloud.

    The 1-arg ``argv: list[str] -> int`` signature is **preserved**
    (Q-V099-15) so the existing
    :file:`tests/cli/test_cloud_worker_cmd.py` monkeypatches like
    ``lambda argv: 0`` keep working unchanged. Signal handlers are
    installed inside a ``try/finally`` so they are always restored
    when this function returns — signal-handling code is
    security-critical and we never want to leak a forwarder into
    surrounding test runs or unrelated CLI flows.
    """
    child = subprocess.Popen(argv, start_new_session=True)  # noqa: S603

    def _forward(sig: int, _frame: FrameType | None) -> None:
        """Re-deliver ``sig`` to the child's whole process group."""
        try:
            pgid = os.getpgid(child.pid)
        except ProcessLookupError:
            logger.debug(
                "_run_subprocess: child pid=%d already gone before forwarding %s",
                child.pid,
                signal.Signals(sig).name,
            )
            return
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            logger.debug(
                "_run_subprocess: pgid=%d already gone before forwarding %s",
                pgid,
                signal.Signals(sig).name,
            )

    previous_term = signal.signal(signal.SIGTERM, _forward)
    previous_int = signal.signal(signal.SIGINT, _forward)
    try:
        return child.wait()
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def _resolve_pool_env(env: dict[str, str]) -> dict[str, str]:
    """Inject ``CURSOR_API_KEY`` into ``env`` from the credential resolver.

    v0.9.2: ``agent worker start --pool`` reads ``CURSOR_API_KEY`` from
    the spawned subprocess environment. When the operator stored their
    service-account key via ``popola auth cursor set`` (precedence #3)
    instead of ``export CURSOR_API_KEY=...``, we need to surface the
    resolved value into the subprocess env so the upstream CLI sees it.

    Returns a fresh dict (does not mutate the caller's). Returns the
    input unchanged when no API key is configured (caller has already
    failed via :func:`_fail_pool_requires_api_key` in that case).
    """
    from popolaloom.credentials import resolve_cursor_api_key

    if env.get("CURSOR_API_KEY", "").strip():
        return env
    resolved = resolve_cursor_api_key()
    if not resolved:
        return env
    out = dict(env)
    out["CURSOR_API_KEY"] = resolved
    return out


def _spawn_worker_subprocess(argv: list[str], *, pool: bool) -> int:
    """Spawn the ``agent worker`` subprocess; inject keyring-resolved key when ``pool`` is True.

    v0.9.2: when ``pool`` is True we may need to inject ``CURSOR_API_KEY``
    into the subprocess env (the upstream CLI reads from env) so a
    keyring-stored service-account key reaches the pool worker without
    a manual ``export`` step. We do this by mutating the parent
    ``os.environ`` only when the resolver-side value is missing —
    short-circuiting both branches of :func:`_resolve_pool_env` when
    no injection is needed.

    The injection mutates ``os.environ`` directly (not a private dict)
    so existing test fixtures that monkey-patch :func:`_run_subprocess`
    with a 1-arg lambda continue to work; v0.9.2 callers gain the
    keyring-aware behaviour transparently.

    Returns the subprocess exit code (whatever :func:`_run_subprocess`
    returns).
    """
    if pool:
        merged = _resolve_pool_env(dict(os.environ))
        injected_key = merged.get("CURSOR_API_KEY")
        original_value = os.environ.get("CURSOR_API_KEY")
        try:
            if injected_key and not (original_value and original_value.strip()):
                os.environ["CURSOR_API_KEY"] = injected_key
                return _run_subprocess(argv)
            return _run_subprocess(argv)
        finally:
            if injected_key and not (original_value and original_value.strip()):
                if original_value is None:
                    os.environ.pop("CURSOR_API_KEY", None)
                else:
                    os.environ["CURSOR_API_KEY"] = original_value
    return _run_subprocess(argv)


def _spawn_detached_worker(
    argv: list[str],
    *,
    name: str,
    log_dir: Path,
    pid_dir: Path,
) -> dict[str, Any]:
    """Double-fork ``argv`` so the grandchild has PPID=1 and is session-leader.

    v1.3.0 P1 (``feedback_for_v1.2.0.md`` §7) — closes the
    IDE-window-vs-worker SIGHUP cascade. The pre-v1.3.0 foreground
    default left the worker re-parented to the IDE's node process, so
    closing the IDE window cascaded SIGHUP to the worker and the
    Cursor cloud connection died with it. The fix is the standard
    POSIX detach idiom:

    1. ``os.fork()`` → first child;
    2. first child calls ``os.setsid()`` so it leaves the parent's
       controlling-terminal session and becomes a process-group leader;
    3. first child ``os.fork()`` → grandchild, then ``os._exit(0)`` so
       the grandchild gets re-parented to PID 1 (init / systemd) and
       cannot accidentally re-acquire a controlling tty;
    4. grandchild redirects stdin to ``/dev/null``, stdout+stderr to
       ``log_dir/worker-<name>.log`` (mode 0o644, append), then
       ``os.execvp(argv[0], argv)``.

    The parent reads the grandchild pid back from the first child via
    a pipe (``os.pipe()``) — the pid file written under ``pid_dir``
    captures it for ``popola cloud worker stop`` to consume.

    Args:
        argv: The ``agent worker start ...`` argv list (built by
            :func:`_build_start_argv`); ``argv[0]`` is the resolved
            agent-binary path.
        name: Sanitised worker name; used as the suffix for the
            per-worker log + pid files. The caller has already
            resolved the default via :func:`_default_worker_name`
            when ``--name`` was omitted.
        log_dir: Directory where the per-worker log lives; created if
            missing (mode 0o755 via :meth:`pathlib.Path.mkdir`).
        pid_dir: Directory where the per-worker pid file lives;
            created if missing.

    Returns:
        A dict with keys ``pid`` (int), ``name`` (str), ``worker_dir``
        (str, possibly ``""`` when no ``--worker-dir`` was passed),
        ``log_file`` (str), ``pid_file`` (str), ``management_addr``
        (str | None), ``detached`` (always ``True``). Suitable for
        printing as one JSON line so callers can grep the grandchild
        pid out of the stdout of ``popola cloud worker start --detach``.

    Raises:
        OSError: when ``fork``, ``pipe``, or the pid-read fails;
            surfaced to the caller (typer translates to a non-zero
            exit per the No-Silent-Failures workspace rule).
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    pid_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"worker-{name}.log"
    pid_path = pid_dir / f"worker-{name}.pid"

    worker_dir = _extract_flag_value(argv, "--worker-dir") or ""
    management_addr = _extract_flag_value(argv, "--management-addr")

    read_fd, write_fd = os.pipe()
    first_pid = os.fork()
    if first_pid == 0:
        os.close(read_fd)
        with contextlib.suppress(OSError):
            os.setsid()
        try:
            second_pid = os.fork()
        except OSError as exc:
            os.write(write_fd, f"ERR:{exc}".encode())
            os.close(write_fd)
            os._exit(1)
        if second_pid == 0:
            os.close(write_fd)
            try:
                with open("/dev/null", "rb", buffering=0) as dn:
                    os.dup2(dn.fileno(), 0)
                with open(log_path, "ab", buffering=0) as logf:
                    os.dup2(logf.fileno(), 1)
                    os.dup2(logf.fileno(), 2)
            except OSError:
                os._exit(2)
            try:
                os.execvp(argv[0], argv)
            except OSError:
                os._exit(3)
        else:
            os.write(write_fd, str(second_pid).encode())
            os.close(write_fd)
            os._exit(0)
    os.close(write_fd)
    os.waitpid(first_pid, 0)
    pid_bytes = b""
    while True:
        chunk = os.read(read_fd, 4096)
        if not chunk:
            break
        pid_bytes += chunk
    os.close(read_fd)
    pid_text = pid_bytes.decode("utf-8", errors="replace").strip()
    if pid_text.startswith("ERR:"):
        raise OSError(
            f"_spawn_detached_worker fork failed: {pid_text[4:]}"
        )
    try:
        grand_pid = int(pid_text)
    except ValueError as exc:
        raise OSError(
            f"_spawn_detached_worker received invalid pid {pid_text!r}"
        ) from exc
    pid_path.write_text(f"{grand_pid}\n", encoding="utf-8")
    try:
        os.chmod(pid_path, 0o644)
    except OSError as exc:
        logger.warning(
            "_spawn_detached_worker: chmod 0o644 on %s failed: %s "
            "(pid file written but permissions left at umask default)",
            pid_path,
            exc,
        )
    return {
        "pid": grand_pid,
        "name": name,
        "worker_dir": worker_dir,
        "log_file": str(log_path),
        "pid_file": str(pid_path),
        "management_addr": management_addr,
        "detached": True,
    }


def _fetch_management_endpoint(
    host: str,
    port: int,
    path: str,
    *,
    timeout_s: float = _DEFAULT_HEALTH_TIMEOUT_S,
) -> tuple[int, str]:
    """GET ``http://<host>:<port>/<path>`` and return ``(status, body)``.

    Factored out so tests can swap in an :class:`httpx.MockTransport`
    backed double.  All error paths raise :class:`httpx.HTTPError`
    subclasses; callers translate those into typer exits.
    """
    url = f"http://{host}:{port}/{path.lstrip('/')}"
    with httpx.Client(timeout=timeout_s) as client:
        response = client.get(url)
    return response.status_code, response.text


def _parse_worker_metrics(text: str) -> dict[str, float]:
    """Parse Prometheus text into a flat ``{metric_name: value}`` dict.

    Only extracts the ``cursor_self_hosted_worker_*`` gauges + the
    ``connect_attempts_total`` / ``connect_retry_total`` counters that
    the worker management server exposes; ignores everything else for
    forward compat (a future Cursor release adding new metrics MUST NOT
    crash this parser).  Values that fail to parse as floats are
    silently dropped per Prometheus best practice (the comment-only
    ``# HELP`` / ``# TYPE`` lines are ignored).
    """
    out: dict[str, float] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip optional labels: ``foo{label="x"} 1`` → ``foo`` + ``1``.
        if "{" in line:
            name_part, _, rest = line.partition("{")
            _, _, value_part = rest.partition("}")
            value_part = value_part.strip()
            name = name_part.strip()
        else:
            name, _, value_part = line.partition(" ")
            name = name.strip()
            value_part = value_part.strip()
        if not name or not value_part:
            continue
        if not name.startswith("cursor_self_hosted_worker_"):
            continue
        try:
            out[name] = float(value_part)
        except ValueError:
            logger.debug("worker metric parse: skipping %r=%r", name, value_part)
            continue
    return out


def _format_quoted_argv(argv: list[str]) -> str:
    """Return a shell-quoted single-line representation of ``argv``."""
    return " ".join(shlex.quote(token) for token in argv)


def _format_worker_reuse_message(worker: LocalWorkerProcess) -> str:
    """Human-readable duplicate-worker reuse message."""
    parts = [f"pid={worker.pid}"]
    if worker.name:
        parts.append(f"name={worker.name}")
    if worker.management_addr:
        parts.append(f"management_addr={worker.management_addr}")
    parts.append(f"worker_dir={worker.worker_dir}")
    return (
        "Reusing existing Cursor self-hosted worker for this workspace; "
        + ", ".join(parts)
    )


def _popolad_socket_path() -> Path:
    """Resolve the local ``popolad`` UDS path without importing ``cli.main``."""
    home = os.environ.get("POPOLA_HOME")
    base = Path(home).expanduser().resolve() if home else Path.home() / ".popola"
    return base / "popolad.sock"


def _make_popolad_sync_client(socket_path: Path | None = None) -> httpx.Client:
    """Construct a synchronous client for ``popolad``'s Unix socket."""
    sock = socket_path or _popolad_socket_path()
    transport = httpx.HTTPTransport(uds=str(sock))
    return httpx.Client(
        transport=transport,
        base_url="http://popolad",
        timeout=httpx.Timeout(connect=5.0, read=None, write=10.0, pool=10.0),
    )


def _post_popolad_dispatch_request(body: dict[str, Any]) -> httpx.Response:
    """POST a dispatch body to ``popolad``; separated for hermetic tests."""
    with _make_popolad_sync_client() as client:
        return client.post("/dispatch", json=body)


def _render_popolad_connect_error(exc: httpx.HTTPError) -> NoReturn:
    """Print the friendly daemon-down message and exit non-zero."""
    typer.echo(
        "error: popolad not running, run `popola popolad start` to start it",
        err=True,
    )
    logger.debug("popolad dispatch connect error: %r", exc)
    raise typer.Exit(code=_EXIT_UNREACHABLE)


def _dispatch_to_popolad(body: dict[str, Any]) -> dict[str, Any]:
    """Send a dispatch request to ``popolad`` and return its JSON payload."""
    try:
        response = _post_popolad_dispatch_request(body)
    except httpx.ConnectError as exc:
        _render_popolad_connect_error(exc)

    if response.status_code == 404:
        typer.echo(
            f"error: unknown cli={body.get('cli')!r}: {response.json().get('detail', '')}",
            err=True,
        )
        raise typer.Exit(code=_EXIT_UNREACHABLE)
    if response.status_code == 400:
        typer.echo(
            f"error: dispatch failed: {response.json().get('detail', '')}",
            err=True,
        )
        raise typer.Exit(code=_EXIT_UNREACHABLE)
    if response.status_code != 200:
        typer.echo(
            f"error: dispatch unexpected status {response.status_code}: {response.text}",
            err=True,
        )
        raise typer.Exit(code=_EXIT_UNREACHABLE)

    payload_raw = response.json()
    if not isinstance(payload_raw, dict):
        typer.echo("error: dispatch response must be a JSON object", err=True)
        raise typer.Exit(code=_EXIT_UNREACHABLE)
    payload = cast(dict[str, Any], payload_raw)
    if "task_id" not in payload:
        typer.echo("error: dispatch response missing task_id", err=True)
        raise typer.Exit(code=_EXIT_UNREACHABLE)
    return payload


def _build_workspace_worker_dispatch_body(
    *,
    prompt: str,
    worker_dir: Path,
    worker_name: str,
    repo_url: str | None,
    pr_url: str | None,
    starting_ref: str,
    model: str,
) -> dict[str, Any]:
    """Build the direct ``popolad`` dispatch body for ``cursor-cloud``."""
    extra: dict[str, Any] = {
        "worker_name": worker_name,
        "starting_ref": starting_ref,
        "model": model,
    }
    if repo_url is not None:
        extra["repo_url"] = repo_url
    if pr_url is not None:
        extra["pr_url"] = pr_url
    return {
        "cli": "cursor-cloud",
        "prompt": prompt,
        "cwd": str(_resolve_worker_dir(worker_dir)),
        "extra": extra,
    }


def _build_workspace_worker_dispatch_argv(
    *,
    prompt: str,
    worker_dir: Path,
    worker_name: str,
    repo_url: str | None,
    pr_url: str | None,
    starting_ref: str,
    model: str,
) -> list[str]:
    """Build the suggested ``popola dispatch --cli=cursor-cloud`` argv."""
    argv = [
        "popola",
        "dispatch",
        prompt,
        "--cli=cursor-cloud",
        "--cwd",
        str(_resolve_worker_dir(worker_dir)),
        "--cli-flag",
        f"worker_name={worker_name}",
    ]
    if repo_url is not None:
        argv.extend(["--cli-flag", f"repo_url={repo_url}"])
    if pr_url is not None:
        argv.extend(["--cli-flag", f"pr_url={pr_url}"])
    argv.extend(["--cli-flag", f"starting_ref={starting_ref}"])
    argv.extend(["--cli-flag", f"model={model}"])
    return argv


# ── debug verb ───────────────────────────────────────────────────────────


@app.command(name="debug")
def worker_debug_cmd(
    worker_dir: Path = typer.Option(  # noqa: B008
        Path.cwd,
        "--worker-dir",
        "-w",
        help="Directory of the repo this worker should serve. Defaults to the current dir.",
    ),
    name: str | None = typer.Option(  # noqa: B008
        None,
        "--name",
        help="Custom display name for the debug probe (defaults to upstream behaviour).",
    ),
    pool: bool = typer.Option(  # noqa: B008
        False,
        "--pool",
        help="Probe the worker as a Self-Hosted Pool member (requires service-account API key).",
    ),
    pool_name: str | None = typer.Option(  # noqa: B008
        None,
        "--pool-name",
        help="Pool label when --pool is set; defaults to 'default'.",
    ),
    label: list[str] = typer.Option(  # noqa: B008
        [],
        "--label",
        help="Repeatable key=value worker label (forwarded to `agent worker debug`).",
    ),
) -> None:
    """Run the upstream ``agent worker debug`` preflight report.

    Forwards stdout / stderr verbatim so the operator sees the same
    auth method / visibility-probe report the upstream CLI emits. Pool
    workers require a service-account API key — when ``--pool`` is set
    without one configured (env var OR keyring) we fail fast with the
    canonical hint. v0.9.2: the keyring-stored value is injected into
    the subprocess env so the upstream CLI sees ``CURSOR_API_KEY``.
    """
    if pool and not _has_resolvable_api_key():
        _fail_pool_requires_api_key()

    binary = _resolve_agent_binary()
    labels_kv = [_validate_label(item) for item in label]
    argv = _build_debug_argv(
        binary=binary,
        worker_dir=worker_dir,
        name=name,
        pool=pool,
        pool_name=pool_name,
        labels=labels_kv,
    )
    rc = _spawn_worker_subprocess(argv, pool=pool)
    raise typer.Exit(code=rc)


# ── start verb ───────────────────────────────────────────────────────────


@app.command(name="start")
def worker_start_cmd(
    worker_dir: Path = typer.Option(  # noqa: B008
        Path.cwd,
        "--worker-dir",
        "-w",
        help="Directory of the repo this worker should serve. Defaults to the current dir.",
    ),
    name: str | None = typer.Option(  # noqa: B008
        None,
        "--name",
        help=(
            "Custom display name for the worker. Defaults to a deterministic "
            "workspace-aware name like popolaloom-<repo>-<hash>."
        ),
    ),
    pool: bool = typer.Option(  # noqa: B008
        False,
        "--pool",
        help=(
            "Register as a Self-Hosted Pool worker. REQUIRES a service-account "
            "API key; user / browser-login auth is rejected by Cursor."
        ),
    ),
    pool_name: str | None = typer.Option(  # noqa: B008
        None,
        "--pool-name",
        help="Pool label when --pool is set; defaults to 'default'.",
    ),
    idle_release_timeout: int | None = typer.Option(  # noqa: B008
        None,
        "--idle-release-timeout",
        help=(
            "Seconds the worker stays connected after going idle before exiting "
            "cleanly. Default: no timeout."
        ),
    ),
    label: list[str] = typer.Option(  # noqa: B008
        [],
        "--label",
        help="Repeatable key=value worker label.",
    ),
    management_addr: str | None = typer.Option(  # noqa: B008
        None,
        "--management-addr",
        help=(
            "Bind a `/healthz` + `/readyz` + `/metrics` HTTP server at this "
            "address (e.g. ':8080' or '127.0.0.1:39231'). Recommended so "
            "`popola cloud worker status` can poll the worker."
        ),
    ),
    dry_run: bool = typer.Option(  # noqa: B008
        False,
        "--dry-run",
        help="Print the exact `agent worker start` argv that would run, then exit.",
    ),
    allow_duplicate: bool = typer.Option(  # noqa: B008
        False,
        "--allow-duplicate",
        help=(
            "Start another worker even when one already serves the same "
            "--worker-dir. Default: reuse the existing workspace worker."
        ),
    ),
    detach: bool = typer.Option(  # noqa: B008
        False,
        "--detach",
        help=(
            "v1.3.0+ (feedback §7): start the worker as a detached "
            "background process (double-fork + setsid). The grandchild's "
            "PPID becomes 1 and stdout/stderr go to "
            "~/.popola/log/worker-<name>.log. Returns one-line JSON "
            "with the grandchild pid + paths and exits 0 immediately. "
            "Default: False (foreground)."
        ),
    ),
) -> None:
    """Start a Cursor self-hosted worker process.

    Defaults to **My Machines** mode (shared assignment; works with the
    user's browser ``agent login``).  Pass ``--pool`` to register as a
    Self-Hosted Pool worker — that mode is Enterprise-only and requires
    a service-account ``CURSOR_API_KEY``.

    Default execution mode is **foreground** (mirrors the upstream CLI
    semantics); leave the terminal open or wrap it with ``systemd-run``
    / ``tmux`` for production. Pass ``--detach`` (v1.3.0+; closes
    ``feedback_for_v1.2.0.md`` §7) to spawn the worker as a detached
    background process via the standard double-fork + setsid idiom —
    the grandchild reparents to PID 1, stdin is ``/dev/null``,
    stdout+stderr go to ``~/.popola/log/worker-<name>.log``, and this
    command returns a one-line JSON envelope with the grandchild pid
    + log path and exits 0 immediately. Closing the launching shell
    (or the IDE terminal that spawned it) no longer kills the worker.

    Once running, point ``popola cloud worker status`` at the same
    ``--management-addr`` to confirm the outbound connection to Cursor's
    cloud is live.
    """
    if management_addr is not None:
        # Validate early (fail fast before subprocess spawn) but pass
        # the original string through to ``agent worker start`` so the
        # upstream CLI sees the verbatim form the user typed.
        _validate_management_addr(management_addr)

    resolved_worker_dir = _resolve_worker_dir(worker_dir)
    effective_name = name or _default_worker_name(resolved_worker_dir)
    labels_kv = [_validate_label(item) for item in label]

    if not dry_run and not allow_duplicate:
        running = _detect_running_workers_for_dir(resolved_worker_dir)
        if running:
            typer.echo(_format_worker_reuse_message(running[0]))
            raise typer.Exit(code=_EXIT_OK)

    if pool and not _has_resolvable_api_key():
        _fail_pool_requires_api_key()

    binary = _resolve_agent_binary()
    argv = _build_start_argv(
        binary=binary,
        worker_dir=resolved_worker_dir,
        name=effective_name,
        pool=pool,
        pool_name=pool_name,
        idle_release_timeout=idle_release_timeout,
        labels=labels_kv,
        management_addr=management_addr,
    )

    if detach:
        if dry_run:
            typer.echo("# popola cloud worker start --detach (dry run)")
            typer.echo(_format_quoted_argv(argv))
            raise typer.Exit(code=_EXIT_OK)
        log_dir = Path.home() / ".popola" / "log"
        pid_dir = Path.home() / ".popola"
        if pool:
            merged_env = _resolve_pool_env(dict(os.environ))
            injected = merged_env.get("CURSOR_API_KEY")
            original = os.environ.get("CURSOR_API_KEY")
            try:
                if injected and not (original and original.strip()):
                    os.environ["CURSOR_API_KEY"] = injected
                result = _spawn_detached_worker(
                    argv,
                    name=effective_name,
                    log_dir=log_dir,
                    pid_dir=pid_dir,
                )
            finally:
                if injected and not (original and original.strip()):
                    if original is None:
                        os.environ.pop("CURSOR_API_KEY", None)
                    else:
                        os.environ["CURSOR_API_KEY"] = original
        else:
            result = _spawn_detached_worker(
                argv,
                name=effective_name,
                log_dir=log_dir,
                pid_dir=pid_dir,
            )
        typer.echo(json.dumps(result, ensure_ascii=False))
        raise typer.Exit(code=_EXIT_OK)

    if dry_run:
        typer.echo("# popola cloud worker start (dry run)")
        typer.echo(_format_quoted_argv(argv))
        raise typer.Exit(code=_EXIT_OK)

    rc = _spawn_worker_subprocess(argv, pool=pool)
    raise typer.Exit(code=rc)


# ── stop verb ────────────────────────────────────────────────────────────


_STOP_POLL_INTERVAL_S: float = 0.1
"""Liveness-poll cadence (seconds) during the ``--grace`` SIGTERM
window in :func:`worker_stop_cmd`. 100 ms keeps the perceived stop
latency tight while bounding wakeups during long ``--grace`` waits;
mirrors the ``_POLL_INTERVAL_S`` constant in :mod:`popolaloom.cli.popolad`.
"""


def _pid_alive(pid: int) -> bool:
    """Return True iff signal 0 reaches ``pid`` (process exists + we have permission).

    Mirrors :func:`popolaloom.cli.popolad._pid_alive` so the worker
    stop path can poll for graceful exit without reaching across
    modules. ``PermissionError`` means the process exists but is owned
    by another uid (treat as alive); ``ProcessLookupError`` means the
    pid is gone (treat as dead).
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


def _find_worker_for_stop(
    *, name: str | None, worker_dir: Path | None
) -> LocalWorkerProcess | None:
    """Locate a single running worker by ``--name`` OR ``--worker-dir``.

    ``--worker-dir`` reuses :func:`_detect_running_workers_for_dir`
    (which already normalises the path + filters by procfs cmdline).
    ``--name`` scans every ``agent worker start`` cmdline via
    :func:`_iter_proc_cmdlines` + :func:`_parse_worker_start_cmdline`
    and returns the first parsed worker whose ``--name`` value matches.
    Returns ``None`` when no match — caller emits the canonical
    "no matching worker found" error and exits :data:`_EXIT_UNREACHABLE`.
    The XOR rule (exactly one of the two flags) is enforced by the
    caller before invoking this helper.
    """
    if worker_dir is not None:
        matches = _detect_running_workers_for_dir(_resolve_worker_dir(worker_dir))
        return matches[0] if matches else None
    if name is not None:
        for pid, argv in _iter_proc_cmdlines():
            parsed = _parse_worker_start_cmdline(pid, argv)
            if parsed is None:
                continue
            if parsed.name == name:
                return parsed
    return None


@app.command(name="stop")
def worker_stop_cmd(
    name: str | None = typer.Option(  # noqa: B008
        None,
        "--name",
        help=(
            "Worker --name value to match against running "
            "`agent worker start` cmdlines."
        ),
    ),
    worker_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--worker-dir",
        help="Resolve the running worker via its --worker-dir.",
    ),
    grace: float = typer.Option(  # noqa: B008
        5.0,
        "--grace",
        help=(
            "Seconds to wait for graceful SIGTERM exit before escalating "
            "to SIGKILL (default 5.0)."
        ),
    ),
) -> None:
    """Stop a running cloud worker (SIGTERM-then-SIGKILL after --grace seconds).

    Stops the worker even if a Cloud Agent session is currently claimed; """ \
        """compose with `popola cloud worker status --busy` to gate.
    """
    if name is None and worker_dir is None:
        typer.echo(
            "error: pass --name OR --worker-dir to identify the worker",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)

    worker = _find_worker_for_stop(name=name, worker_dir=worker_dir)
    if worker is None:
        typer.echo("error: no matching worker found", err=True)
        raise typer.Exit(code=_EXIT_UNREACHABLE)

    pid = worker.pid
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError as exc:
        typer.echo(
            f"error: worker pid={pid} disappeared before signal could be sent: {exc}",
            err=True,
        )
        raise typer.Exit(code=_EXIT_UNREACHABLE) from exc

    logger.info(
        "worker_stop: sending SIGTERM to worker pid=%d pgid=%d (grace=%.1fs)",
        pid,
        pgid,
        grace,
    )
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError as exc:
        typer.echo(
            f"error: worker pid={pid} pgid={pgid} disappeared before SIGTERM "
            f"could be delivered: {exc}",
            err=True,
        )
        raise typer.Exit(code=_EXIT_UNREACHABLE) from exc

    start = time.monotonic()
    deadline = start + max(0.0, grace)
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            elapsed = time.monotonic() - start
            typer.echo(
                f"Stopped worker pid={pid} (signal=SIGTERM, "
                f"exited within {elapsed:.1f}s)"
            )
            return
        time.sleep(_STOP_POLL_INTERVAL_S)

    logger.warning(
        "worker_stop: pid=%d did not exit within %.1fs grace; sending SIGKILL",
        pid,
        grace,
    )
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        logger.info(
            "worker_stop: pgid=%d already gone before SIGKILL fired",
            pgid,
        )
    typer.echo(
        f"Killed worker pid={pid} (signal=SIGKILL after {grace:.1f}s grace expiry)"
    )


# ── dispatch verb ────────────────────────────────────────────────────────


@app.command(name="dispatch")
def worker_dispatch_cmd(
    prompt: str = typer.Argument(
        ...,
        help="Prompt string to dispatch through `popolad`.",
    ),
    worker_dir: Path = typer.Option(  # noqa: B008
        Path.cwd,
        "--worker-dir",
        "-w",
        help="Directory/workspace whose existing worker should be targeted.",
    ),
    repo_url: str | None = typer.Option(  # noqa: B008
        None,
        "--repo-url",
        help="GitHub repository URL for `cursor-cloud` dispatch.",
    ),
    pr_url: str | None = typer.Option(  # noqa: B008
        None,
        "--pr-url",
        help="GitHub PR URL for `cursor-cloud` dispatch (alternative to --repo-url).",
    ),
    starting_ref: str = typer.Option(  # noqa: B008
        "main",
        "--starting-ref",
        help="Starting ref forwarded to `cursor-cloud`.",
    ),
    model: str = typer.Option(  # noqa: B008
        "composer-2",
        "--model",
        help="Cursor cloud model id forwarded via --cli-flag model=...",
    ),
    print_only: bool = typer.Option(  # noqa: B008
        False,
        "--print-only",
        "--dry-run",
        help=(
            "Preview the equivalent `popola dispatch --cli=cursor-cloud` command "
            "without contacting popolad."
        ),
    ),
    json_out: bool = typer.Option(  # noqa: B008
        False,
        "--json",
        help="Emit machine-readable JSON instead of the plain task_id / preview text.",
    ),
) -> None:
    """Dispatch to the workspace worker through ``popolad``.

    By default this detects the already-running worker for ``--worker-dir``
    when present, then POSTs a ``cli=cursor-cloud`` dispatch to ``popolad``
    with ``worker_name`` and repo/PR routing extras.  ``--print-only`` (or
    ``--dry-run``) preserves the old side-effect-free command preview.
    """
    if repo_url is not None and pr_url is not None:
        typer.echo("error: pass --repo-url OR --pr-url, not both", err=True)
        raise typer.Exit(code=_EXIT_INVALID_ARGS)
    if repo_url is None and pr_url is None:
        typer.echo("error: pass either --repo-url or --pr-url", err=True)
        raise typer.Exit(code=_EXIT_INVALID_ARGS)
    if not starting_ref.strip():
        typer.echo("error: --starting-ref must be non-empty", err=True)
        raise typer.Exit(code=_EXIT_INVALID_ARGS)
    if not model.strip():
        typer.echo("error: --model must be non-empty", err=True)
        raise typer.Exit(code=_EXIT_INVALID_ARGS)

    resolved_worker_dir = _resolve_worker_dir(worker_dir)
    running = _detect_running_workers_for_dir(resolved_worker_dir)
    worker = running[0] if running else None
    worker_name = (
        worker.name
        if worker is not None and worker.name
        else _default_worker_name(resolved_worker_dir)
    )

    # v0.10.0 (DECISIONS Q-3 + Q-4 + Q-7): self-hosted worker existence
    # pre-flight. The ``popola cloud worker dispatch`` verb is, by
    # construction, the self-hosted-target dispatch path (see module
    # docstring), so ``target="self-hosted"`` is hard-coded here. When
    # B3's ``popola dispatch --cloud-target=...`` lands at a sibling
    # call site, that one will pass the resolved ``extra["cloud_target"]``
    # value — same gate function, different caller. The gate gracefully
    # degrades (loud WARN, then proceed) when no API key is resolvable —
    # see ``_enforce_self_hosted_worker_exists`` docstring for rationale.
    from popolaloom.credentials import resolve_cursor_api_key

    api_key = resolve_cursor_api_key() or ""
    _enforce_self_hosted_worker_exists(
        api_key=api_key,
        worker_name=worker_name,
        target="self-hosted",
    )
    dispatch_body = _build_workspace_worker_dispatch_body(
        prompt=prompt,
        worker_dir=resolved_worker_dir,
        worker_name=worker_name,
        repo_url=repo_url,
        pr_url=pr_url,
        starting_ref=starting_ref.strip(),
        model=model.strip(),
    )
    argv = _build_workspace_worker_dispatch_argv(
        prompt=prompt,
        worker_dir=resolved_worker_dir,
        worker_name=worker_name,
        repo_url=repo_url,
        pr_url=pr_url,
        starting_ref=starting_ref.strip(),
        model=model.strip(),
    )

    payload = {
        "command": _format_quoted_argv(argv),
        "worker": {
            "found": worker is not None,
            "pid": worker.pid if worker is not None else None,
            "name": worker_name,
            "management_addr": worker.management_addr if worker is not None else None,
            "worker_dir": str(resolved_worker_dir),
        },
    }
    if print_only and json_out:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    if print_only:
        if worker is not None:
            typer.echo(_format_worker_reuse_message(worker))
        else:
            typer.echo(
                "No running worker found for this workspace; routing will target "
                f"deterministic worker_name={worker_name}. Start a matching worker "
                "with `popola cloud worker start` if it is not already running."
            )
        typer.echo("")
        typer.echo("# Run this to route the task to the workspace worker:")
        typer.echo(payload["command"])
        return

    if worker is None and not json_out:
        typer.echo(
            "No running worker found for this workspace; routing will target "
            f"deterministic worker_name={worker_name}. Start a matching worker "
            "with `popola cloud worker start` if it is not already running.",
            err=True,
        )

    response_payload = _dispatch_to_popolad(dispatch_body)
    if json_out:
        typer.echo(json.dumps(response_payload, ensure_ascii=False))
    else:
        typer.echo(response_payload["task_id"])


def _has_resolvable_api_key() -> bool:
    """True iff the credential resolver returns a non-empty key.

    v0.9.2: the pool worker's API key lookup honours both the env var
    and the OS keyring (precedence #2 + #3 from the resolver). Returning
    True here means :func:`_resolve_pool_env` will subsequently inject
    the resolved value into the spawned subprocess env.
    """
    from popolaloom.credentials import resolve_cursor_api_key

    return resolve_cursor_api_key() is not None


def _build_self_hosted_worker_missing_hint(worker_name: str) -> str:
    """Return the bilingual hint emitted when the named self-hosted worker is missing.

    v0.10.0 (DECISIONS Q-3 + Q-7) — replaces the v0.9.9 account-class
    bilingual hint.  Required substrings (PLAN.md C1 AC 7, pinned by
    Wave D3's regex tests):

    * English fragment ``popola cloud worker start --name <X>
      --worker-dir <repo-root>`` (with the actual ``worker_name``
      substituted for ``<X>``) — points operators at the actual fix.
    * English fragment ``popola cloud worker dispatch`` — names the
      verb the operator just ran so the retry instruction is concrete.
    * Chinese fragment ``Worker '<name>' 不存在`` — name surrounded by
      single quotes so D3's regex pin (``Worker '\\S+' 不存在``)
      matches the rendered text exactly.

    The hint NEVER suggests ``--cli=cursor`` or any other local-CLI
    fallback path per ``feedbacks/feedback_for_v0.10.0.md`` L5+L11
    (cloud dispatch and local execution are semantically distinct;
    silently re-routing one to the other is a correctness bug).
    """
    return (
        f"error: self-hosted worker '{worker_name}' is not registered with Cursor.\n"
        "\n"
        "Reason: popola cloud worker dispatch with --cloud-target=self-hosted "
        "requires a registered self-hosted worker (verified via "
        "GET /v0/private-workers per DECISIONS Q-3). The named worker "
        f"'{worker_name}' was NOT found in the inventory returned by Cursor.\n"
        "\n"
        "Fix — start a worker for this workspace, then retry:\n"
        f"  popola cloud worker start --name {worker_name} --worker-dir <repo-root>\n"
        "  # ...wait for the worker to register, then re-run:\n"
        f"  popola cloud worker dispatch \"<prompt>\" --worker-dir <repo-root> "
        "--repo-url <repo-url>\n"
        "\n"
        "Per the v0.10.0 no-fallback contract (DECISIONS Q-7), popola will NOT "
        "silently re-route this dispatch to a local cursor-agent subprocess — "
        "cloud dispatch and local execution are semantically distinct.\n"
        "\n"
        f"错误：Worker '{worker_name}' 不存在 — 该 self-hosted worker 未在 Cursor 注册。\n"
        "原因：popola cloud worker dispatch 在 --cloud-target=self-hosted 模式下需要"
        "已注册的 self-hosted worker（通过 GET /v0/private-workers 校验）。\n"
        "\n"
        "解决方案：先在仓库根目录启动同名 worker，再重试派发：\n"
        f"  popola cloud worker start --name {worker_name} --worker-dir <repo-root>\n"
        "  # 等 worker 注册成功后：\n"
        f"  popola cloud worker dispatch \"<prompt>\" --worker-dir <repo-root> "
        "--repo-url <repo-url>\n"
        "\n"
        "根据 v0.10.0 no-fallback 契约（DECISIONS Q-7），popola 不会静默回退到"
        "本地 cursor-agent 子进程 —— 云端派发与本地执行是语义不同的两件事。\n"
    )


def _enforce_self_hosted_worker_exists(
    api_key: str,
    worker_name: str,
    target: str,
) -> None:
    """Refuse ``popola cloud worker dispatch`` when the named self-hosted worker is missing.

    v0.10.0 (DECISIONS Q-3 + Q-4 + Q-7) replaces the v0.9.9
    ``_enforce_account_class_pre_flight_gate``.  The v0.9.9 gate was
    based on the Spike-0 BRANCH_B verdict that personal API keys
    cannot drive self-hosted-worker dispatch — research/01's 22
    successful 2xx probes (verdict ``BRANCH_A_FEASIBLE``) DISCONFIRMED
    that verdict, so the relevant failure mode in v0.10.0 is
    "operator typed --worker-name=<X> but no such worker is registered
    with Cursor".  The new gate refuses early so the operator sees an
    actionable hint pointing at ``popola cloud worker start --name
    <X> --worker-dir <repo-root>`` instead of waiting for the gateway
    to surface a generic 4xx.

    Behaviour matrix (PLAN.md C1 AC 5):

    * ``target != "self-hosted"`` → return immediately (no-op for
      ``cursor-managed`` / ``ask-each-time`` / empty target).
    * ``target == "self-hosted"`` AND ``api_key`` is empty → emit a
      loud WARN explaining the pre-flight is skipped (cannot reach
      ``GET /v0/private-workers`` without auth) and return; the
      downstream popolad / gateway still surfaces any auth error so
      this is NOT a silent failure (we're skipping an additive
      pre-flight check, not suppressing an error).
    * ``target == "self-hosted"`` AND ``found=False`` → emit the
      bilingual hint to stderr, log a WARN per No-Silent-Failures, and
      ``raise typer.Exit(_EXIT_PRE_FLIGHT_GATE)`` (78). NEVER suggests
      a local-CLI fallback per Q-7.
    * ``target == "self-hosted"`` AND ``found=True`` AND
      ``is_in_use=True`` → log a WARN ("the run will queue until the
      worker is free") and return; the dispatch is allowed because
      Cursor's gateway accepts the POST and queues the run (Q-3 soft-
      warn semantics).
    * ``target == "self-hosted"`` AND ``found=True`` AND
      ``is_in_use=False`` → return silently (gate passed).
    * Any :class:`CursorCloudError` from the underlying
      ``client.list_workers()`` propagates UNCHANGED so the caller's
      error catalog can format the bilingual hint (No-Silent-Failures
      rule). The gate does NOT swallow transient HTTP errors.

    Args:
        api_key: Resolved Cursor API key. When empty, the gate
            gracefully degrades (loud WARN, then proceed) — this is
            an explicit choice so callers that mock the downstream
            popolad RPC don't need to also mock the Cursor REST
            ``list_workers`` call when no API key is configured.
        worker_name: The display name of the self-hosted worker the
            dispatch will route to. Compared against ``worker["name"]``
            returned by ``GET /v0/private-workers``.
        target: The resolved cloud target. Only ``"self-hosted"`` triggers
            the gate; any other value (including ``""``,
            ``"cursor-managed"``, ``"ask-each-time"``) is a no-op.

    Raises:
        typer.Exit: with code :data:`_EXIT_PRE_FLIGHT_GATE` (78) when
            the named worker is not registered.
        popolaloom.adapters.cursor_cloud.CursorCloudError: any HTTP /
            JSON / auth error from the underlying ``list_workers()``
            call propagates unchanged so the popolad / dispatch path's
            error-catalog handling kicks in.
    """
    if target != "self-hosted":
        return

    if not api_key:
        logger.warning(
            "self-hosted worker existence pre-flight SKIPPED for worker_name=%r: "
            "no Cursor API key resolvable via env or keyring (cannot call "
            "GET /v0/private-workers). The dispatch will continue but if the "
            "worker is not registered, the failure will surface as a generic "
            "downstream gateway error rather than a friendly pre-flight hint. "
            "Set CURSOR_API_KEY or run `popola auth cursor set` to enable the "
            "pre-flight check.",
            worker_name,
        )
        return

    from popolaloom.adapters.cursor_cloud import CloudCursorClient
    from popolaloom.cloud.preflight import check_self_hosted_worker_exists

    with CloudCursorClient(api_key) as client:
        result = check_self_hosted_worker_exists(client, worker_name)

    if not result.found:
        typer.echo(
            _build_self_hosted_worker_missing_hint(worker_name),
            err=True,
        )
        logger.warning(
            "worker_dispatch refused: worker %r not registered (%s)",
            worker_name,
            result.message,
        )
        raise typer.Exit(code=_EXIT_PRE_FLIGHT_GATE)

    if result.is_in_use:
        logger.warning(
            "worker %r is currently busy (is_in_use=True); your run will queue "
            "until the worker is free",
            worker_name,
        )


def _fail_pool_requires_api_key() -> NoReturn:
    """Print the canonical pool-without-key hint and exit ``77``."""
    typer.echo(
        "error: --pool requires a Cursor service-account API key (Enterprise). "
        "Configure one via: export CURSOR_API_KEY=<service-account-key>, OR "
        "`popola auth cursor set` (stores in OS keyring), OR drop --pool to "
        "launch a shared 'My Machines' worker (works with `agent login`).",
        err=True,
    )
    typer.echo(
        "  see: https://cursor.com/docs/cloud-agent/self-hosted-pool#authenticate-workers",
        err=True,
    )
    raise typer.Exit(code=_EXIT_POOL_REQUIRES_API_KEY)


# ── status verb ──────────────────────────────────────────────────────────


@app.command(name="status")
def worker_status_cmd(
    management_addr: str = typer.Option(  # noqa: B008
        _DEFAULT_MANAGEMENT_ADDR,
        "--management-addr",
        help=(
            "Worker management-server address. Must match the "
            "`agent worker start --management-addr ...` value."
        ),
    ),
    timeout_s: float = typer.Option(  # noqa: B008
        _DEFAULT_HEALTH_TIMEOUT_S,
        "--timeout",
        help="Per-request timeout in seconds (loopback only; default 3 s).",
    ),
    json_out: bool = typer.Option(  # noqa: B008
        False,
        "--json",
        help="Emit machine-readable JSON instead of a Rich table.",
    ),
) -> None:
    """Probe the worker's `/healthz` + `/readyz` + `/metrics` endpoints.

    Does NOT require ``CURSOR_API_KEY`` — the worker management server
    is a loopback-only diagnostic surface.  Reports:

    - ``healthz`` JSON status (``ok`` when the worker process is alive).
    - ``readyz`` ``connected`` / ``claimed`` flags (the outbound
      connection to Cursor's cloud + whether a Cloud Agent session is
      currently using this worker).
    - ``metrics`` summary: connected gauge, session-active gauge,
      connect-attempts counter.

    Exit ``1`` when the management server is unreachable (worker not
    running, wrong address, or `--management-addr` was not configured
    on the worker process); exit ``2`` for invalid CLI flags.
    """
    if timeout_s <= 0:
        typer.echo(
            f"error: --timeout must be > 0 (got {timeout_s})",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)
    host, port = _validate_management_addr(management_addr)

    payload: dict[str, Any] = {
        "management_addr": f"{host}:{port}",
        "healthz": None,
        "readyz": None,
        "metrics": None,
    }
    try:
        for path, key in (("healthz", "healthz"), ("readyz", "readyz")):
            status, body = _fetch_management_endpoint(
                host, port, path, timeout_s=timeout_s
            )
            payload[key] = _parse_health_body(status, body)
        metrics_status, metrics_body = _fetch_management_endpoint(
            host, port, "metrics", timeout_s=timeout_s
        )
        payload["metrics"] = {
            "status": metrics_status,
            "values": _parse_worker_metrics(metrics_body) if metrics_status == 200 else {},
        }
    except httpx.HTTPError as exc:
        typer.echo(
            f"error: worker management server unreachable at {host}:{port}: "
            f"{type(exc).__name__}: {exc}",
            err=True,
        )
        if management_addr == _DEFAULT_MANAGEMENT_ADDR:
            typer.echo(
                f"  hint: --management-addr defaults to {_DEFAULT_MANAGEMENT_ADDR}; "
                "pass --management-addr <host:port> matching your "
                "`agent worker start --management-addr ...` invocation, "
                "OR add --management-addr to that invocation if you "
                "haven't yet (the management server is opt-in).",
                err=True,
            )
        else:
            typer.echo(
                "  hint: did you start the worker with "
                f"`agent worker start ... --management-addr {management_addr}`?",
                err=True,
            )
        raise typer.Exit(code=_EXIT_UNREACHABLE) from exc

    if json_out:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    _render_status_table(payload)

    # v0.9.9 F3 (feedback_for_v0.9.7.md:52): when the worker process is
    # alive (healthz responsive, /readyz returned, metrics scraped) but
    # has never been routed a Cloud Agent session, surface a single
    # idle-hint line so operators can distinguish "worker dead" (the
    # _EXIT_UNREACHABLE path above) from "worker alive but no traffic
    # yet". This is additive only — the rendered table is unchanged
    # and the JSON path still returns early before this hint.
    if _is_worker_idle(payload):
        typer.echo(
            "note: 0 sessions claimed since worker started "
            "(the worker is healthy but has not been routed any task yet)"
        )


def _is_worker_idle(payload: dict[str, Any]) -> bool:
    """Decide whether to print the v0.9.9 F3 worker idle-hint line.

    Returns ``True`` iff the management server reported back AND
    none of the available signals indicate the worker has ever
    claimed a Cloud Agent session:

    - ``readyz.claimed`` is not currently ``True`` (no live session
      holding the worker right now);
    - ``metrics.cursor_self_hosted_worker_session_active`` is 0 / None
      (Prometheus gauge agrees: no live session);
    - ``metrics.cursor_self_hosted_worker_last_activity_unix_seconds``
      is ``0`` / missing (the activity heartbeat the worker emits on
      every claim has never fired).

    The idle-hint complements (does NOT replace) the existing
    unreachable-path stderr hint at :meth:`worker_status_cmd`'s
    ``httpx.HTTPError`` branch — that path already covers "worker
    dead", this path covers "worker alive but never busy".
    """
    healthz = payload.get("healthz") or {}
    readyz = payload.get("readyz") or {}
    metrics_block = payload.get("metrics") or {}
    metrics_values = metrics_block.get("values") or {}

    healthz_status = healthz.get("status")
    if healthz_status not in {"ok", "healthy"} and healthz_status is not None:
        return False
    if not isinstance(readyz, dict):
        return False

    if readyz.get("claimed") is True:
        return False

    session_active = metrics_values.get(
        "cursor_self_hosted_worker_session_active"
    )
    if isinstance(session_active, (int, float)) and session_active > 0:
        return False

    last_activity = metrics_values.get(
        "cursor_self_hosted_worker_last_activity_unix_seconds"
    )
    return not (
        isinstance(last_activity, (int, float)) and last_activity > 0
    )


def _parse_health_body(status: int, body: str) -> dict[str, Any]:
    """Parse a ``healthz`` / ``readyz`` body into a structured dict.

    The worker emits compact JSON like
    ``{"status": "ok", "connected": true, "claimed": false, ...}``.  On
    parse failure we surface the raw body under ``raw`` (No Silent
    Failures — operators can still see what came back).
    """
    out: dict[str, Any] = {"status_code": status}
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        out["raw"] = body.strip()
        return out
    if isinstance(parsed, dict):
        out.update(parsed)
    else:
        out["raw"] = body.strip()
    return out


def _render_status_table(payload: dict[str, Any]) -> None:
    """Render the ``status`` payload as a 2-column Rich table."""
    table = Table(
        title=f"Cursor self-hosted worker @ {payload.get('management_addr')}",
        show_header=True,
        header_style="bold",
    )
    table.add_column("field")
    table.add_column("value")

    healthz = payload.get("healthz") or {}
    readyz = payload.get("readyz") or {}
    metrics = (payload.get("metrics") or {}).get("values") or {}

    table.add_row("healthz.status", str(healthz.get("status", "-")))
    table.add_row(
        "readyz.connected",
        _format_bool(readyz.get("connected")),
    )
    table.add_row(
        "readyz.claimed",
        _format_bool(readyz.get("claimed")),
    )
    table.add_row(
        "metrics.connected",
        _format_metric(metrics.get("cursor_self_hosted_worker_connected")),
    )
    table.add_row(
        "metrics.session_active",
        _format_metric(metrics.get("cursor_self_hosted_worker_session_active")),
    )
    table.add_row(
        "metrics.connect_attempts_total",
        _format_metric(
            metrics.get("cursor_self_hosted_worker_connect_attempts_total")
        ),
    )
    table.add_row(
        "metrics.last_activity",
        _format_unix_timestamp(
            metrics.get("cursor_self_hosted_worker_last_activity_unix_seconds")
        ),
    )
    _console_out.print(table)


def _format_bool(value: Any) -> str:
    """Render a ``readyz`` boolean (or sentinel) for the status table."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "-"


def _format_metric(value: Any) -> str:
    """Render a Prometheus metric value, collapsing ``None`` to ``-``."""
    if value is None:
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _format_unix_timestamp(value: Any) -> str:
    """Render ``last_activity_unix_seconds`` as an ISO-8601 UTC string.

    The worker's metrics surface a Unix epoch float; rendering it as an
    ISO timestamp makes a stale worker (last_activity drifting into the
    past) immediately visible in the status table.  Falls back to ``-``
    when the value is missing / zero (the worker emits ``0`` before any
    heartbeat lands) or unparseable.
    """
    if value is None:
        return "-"
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "-"
    if seconds <= 0:
        return "never"
    from datetime import UTC, datetime

    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")


# ── handoff verb ─────────────────────────────────────────────────────────


@app.command(name="handoff")
def worker_handoff_cmd(
    worker_id: str | None = typer.Option(  # noqa: B008
        None,
        "--worker-id",
        help=(
            "Cursor worker UUID printed by `agent worker start` (e.g. "
            "'c60a7ec7-a15c-4aff-a9d8-0b550c9893dc'). Used to compose the "
            "Cloud Agents URL."
        ),
    ),
    worker_url: str | None = typer.Option(  # noqa: B008
        None,
        "--worker-url",
        help=(
            "Pre-built Cloud Agents URL (e.g. "
            "'https://cursor.com/agents#workerId=...'). Mutually exclusive "
            "with --worker-id."
        ),
    ),
    prompt: str | None = typer.Option(  # noqa: B008
        None,
        "--prompt",
        help="Inline task prompt. Mutually exclusive with --prompt-file.",
    ),
    prompt_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--prompt-file",
        help="Read the task prompt from a UTF-8 text file.",
    ),
    title: str | None = typer.Option(  # noqa: B008
        None,
        "--title",
        help="Optional one-line title shown above the prompt in the handoff envelope.",
    ),
    json_out: bool = typer.Option(  # noqa: B008
        False,
        "--json",
        help="Emit machine-readable JSON instead of human-readable Markdown.",
    ),
) -> None:
    """Emit a copy-paste-ready Cloud Agents handoff envelope.

    PopolaLoom does **not** create a Cloud Agent task here — the
    upstream Cursor REST surface (``POST /v1/agents``) requires
    ``CURSOR_API_KEY`` and is exposed via ``popola dispatch
    --cli=cursor-cloud``.  This verb is for the **My Machines / web UI**
    flow: paste the URL in a browser, paste the prompt, click Run, and
    the worker started by ``popola cloud worker start`` executes the
    tool calls in this environment.

    The output makes the contract explicit (no popola task id is
    created) so operators don't conflate the two dispatch paths.
    """
    if worker_id is not None and worker_url is not None:
        typer.echo(
            "error: pass --worker-id OR --worker-url, not both", err=True
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)
    if prompt is not None and prompt_file is not None:
        typer.echo(
            "error: pass --prompt OR --prompt-file, not both", err=True
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)

    resolved_url = _resolve_worker_url(worker_id=worker_id, worker_url=worker_url)
    resolved_prompt = _resolve_prompt(prompt=prompt, prompt_file=prompt_file)

    envelope: dict[str, Any] = {
        "kind": "popola.cloud.worker.handoff",
        "version": "v0.9.1",
        "title": title,
        # ``worker_id`` is surfaced separately from ``worker_url`` so
        # automating callers (CI / Slack bots) don't have to re-parse
        # ``#workerId=<uuid>`` out of the URL fragment.  Stays ``None``
        # when the operator passed ``--worker-url`` directly without a
        # discoverable id.
        "worker_id": _extract_worker_id_from_url(resolved_url),
        "worker_url": resolved_url,
        "prompt": resolved_prompt,
        "popola_task_id": None,
        "note": (
            "PopolaLoom did NOT create a Cloud Agent run. Open the "
            "worker_url in a browser and paste the prompt to launch a "
            "Cloud Agent on this self-hosted worker, OR use `popola "
            "dispatch --cli=cursor-cloud` (requires CURSOR_API_KEY) to "
            "create a run via REST."
        ),
    }

    if json_out:
        typer.echo(json.dumps(envelope, indent=2, sort_keys=True))
        return

    _render_handoff_markdown(envelope)


def _extract_worker_id_from_url(url: str) -> str | None:
    """Pull the ``workerId=<uuid>`` value out of a Cloud Agents URL.

    Recognises both fragment (``#workerId=...``) and query
    (``?workerId=...``) forms; returns ``None`` when no id is found
    so the caller surfaces ``"worker_id": null`` in the JSON envelope.
    """
    for sep in ("#workerId=", "?workerId=", "&workerId="):
        marker = url.find(sep)
        if marker != -1:
            tail = url[marker + len(sep) :]
            for terminator in ("&", "#", "?"):
                cut = tail.find(terminator)
                if cut != -1:
                    tail = tail[:cut]
            tail = tail.strip()
            return tail or None
    return None


def _resolve_worker_url(
    *, worker_id: str | None, worker_url: str | None
) -> str:
    """Build the Cloud Agents URL from ``--worker-id`` or pass through.

    Either of the two flags must be set — without a target URL the
    envelope is unactionable, so we fail fast.  When ``worker_id`` is
    used we emit the canonical fragment form
    (``https://cursor.com/agents#workerId=<id>``) the upstream CLI
    prints from ``agent worker start``.
    """
    if worker_id is not None:
        wid = worker_id.strip()
        if not wid:
            typer.echo("error: --worker-id must be non-empty", err=True)
            raise typer.Exit(code=_EXIT_INVALID_ARGS)
        return f"https://cursor.com/agents#workerId={wid}"
    if worker_url is not None:
        url = worker_url.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            typer.echo(
                f"error: --worker-url must start with http(s)://, got {url!r}",
                err=True,
            )
            raise typer.Exit(code=_EXIT_INVALID_ARGS)
        return url
    typer.echo(
        "error: pass either --worker-id <uuid> or --worker-url <url>",
        err=True,
    )
    raise typer.Exit(code=_EXIT_INVALID_ARGS)


def _resolve_prompt(
    *, prompt: str | None, prompt_file: Path | None
) -> str:
    """Read the prompt text from inline ``--prompt`` or ``--prompt-file``.

    Falls back to a sentinel placeholder when both are unset so the
    operator gets a usable envelope template instead of a hard error
    (the placeholder makes it obvious the prompt still needs editing).
    """
    if prompt is not None:
        text = prompt
    elif prompt_file is not None:
        try:
            text = prompt_file.read_text(encoding="utf-8")
        except OSError as exc:
            typer.echo(
                f"error: cannot read --prompt-file {prompt_file}: {exc}",
                err=True,
            )
            raise typer.Exit(code=_EXIT_INVALID_ARGS) from exc
    else:
        text = "<paste your task prompt here>"
    text = text.strip("\n")
    if not text.strip():
        typer.echo(
            "error: prompt must be non-empty", err=True
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)
    return text


def _render_handoff_markdown(envelope: dict[str, Any]) -> None:
    """Render the handoff envelope as Markdown (default human view)."""
    typer.echo("# Cursor Cloud Agents — handoff envelope")
    title = envelope.get("title")
    if isinstance(title, str) and title.strip():
        typer.echo(f"\n## {title.strip()}")
    typer.echo("\n## How to dispatch")
    typer.echo("1. Open the URL below in your browser (Cloud Agents UI).")
    typer.echo("2. Paste the prompt into the chat box.")
    typer.echo(
        "3. Confirm the worker that this machine is registered against and click Run."
    )
    typer.echo("\n## URL")
    typer.echo(f"\n    {envelope['worker_url']}\n")
    typer.echo("## Prompt")
    typer.echo("")
    typer.echo("```text")
    typer.echo(envelope["prompt"])
    typer.echo("```")
    typer.echo("\n## Notes")
    typer.echo(f"- {envelope['note']}")
    typer.echo("- popola_task_id: none (no popolad row written for browser handoff).")
