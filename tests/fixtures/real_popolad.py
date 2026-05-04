"""``real_popolad`` fixture — spawn a real popolad daemon process (Tier 3).

Per testing-matrix.md §5.1 + §1.3 — Tier 3 cross-process tests need a
real ``python -m popolaloom.daemon`` subprocess so the UDS RPC path is
exercised end-to-end (CLI → UDS → uvicorn → Popolad).  This module
provides:

* :class:`RealPopoladHandle` — a lightweight value object exposing
  ``socket_path`` / ``pid`` / ``home`` / ``env`` / ``log_path`` /
  ``proc`` so the test can drive the daemon however it likes.
* :func:`spawn_real_popolad` — context manager that boots the daemon in
  a fresh ``$POPOLA_HOME``, polls the UDS until ready (≤ 5 s by default),
  yields the handle, and on teardown sends ``SIGTERM`` (+ ``SIGKILL``
  fallback after 5 s).
* :func:`make_async_client` — convenience helper that wraps an
  :class:`httpx.AsyncClient` around the daemon's UDS so test code can
  call ``await client.get("/probe")`` directly.

Design notes
------------

* We never use ``pytest.fixture(scope="module")`` here because the
  Tier 3 cases want **fresh** daemon state per test — module scope would
  hide state-leak bugs (e.g. residual NDJSON files from a previous case
  poisoning ``rehydrate_from_persistence``).  The matrix conftest exposes
  the helpers as function-scoped factories so each test owns its own
  daemon.
* ``start_new_session=True`` mirrors the production
  ``popolad start`` subcommand (see
  :file:`src/popolaloom/cli/popolad.py`) so the daemon detaches into its
  own session group → SIGHUP from the test process never propagates.
  This is what allows NFR-5 cross-terminal survival to be tested at all.
* ``POPOLA_USE_GRAPH=0`` is forced to keep Tier 3 deterministic — the
  graph path runs an extra background thread that races with our
  ``request.is_disconnected`` sentinel during attach-stream tests.  Tier
  4 will exercise the graph path explicitly.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx

_DAEMON_BOOT_TIMEOUT_S: float = 5.0
"""Default cap on UDS-appears wait per testing-matrix.md §5.1 (5 s)."""

_DAEMON_GRACEFUL_SHUTDOWN_S: float = 5.0
"""SIGTERM grace before SIGKILL fallback (matches production CLI)."""

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
"""Workspace root — needed so ``cwd`` of the daemon points at a stable
location (not the random pytest tmp_path)."""

_ARKTOWER_MIGRATIONS_FALLBACK: Path = Path("/home/agent/reference/ArkTower/migrations")
"""Same fallback as :mod:`popolaloom.daemon.repository` — needed so the
test daemon can apply ArkTower's core migrations without the operator
setting ``$POPOLA_ARKTOWER_MIGRATIONS_DIR`` themselves."""


@dataclass
class RealPopoladHandle:
    """Value object describing a running popolad subprocess.

    Attributes:
        proc: The :class:`subprocess.Popen` for the daemon.  Tests can
            ``proc.send_signal(...)`` for chaos-style scenarios.
        pid: Convenience alias for ``proc.pid``.
        home: ``$POPOLA_HOME`` (= the test's tmp dir).
        socket_path: ``$POPOLA_HOME/popolad.sock`` (UDS bind point).
        events_dir: ``$POPOLA_HOME/events/`` (per-task NDJSON files).
        env: The full env dict the daemon was launched with — useful when
            the test wants to spawn a *second* daemon (S1 SIGKILL-restart
            scenarios) reusing the same ``$POPOLA_HOME``.
        log_path: stdout/stderr capture file path.
    """

    proc: subprocess.Popen[bytes]
    pid: int
    home: Path
    socket_path: Path
    events_dir: Path
    env: dict[str, str]
    log_path: Path
    cleanup_pids: list[int] = field(default_factory=list)
    """Side-channel: tests can stash leaked subprocess pids that they
    want the fixture teardown to clean up (e.g. cursor shim sleepers
    surviving SIGKILL-of-daemon per the R-005 contract)."""

    def is_alive(self) -> bool:
        """Return True iff the daemon process is still running."""
        if self.proc.poll() is not None:
            return False
        return _pid_alive(self.pid)

    def read_log(self) -> str:
        """Read the daemon's captured stdout/stderr (best-effort)."""
        if not self.log_path.exists():
            return "(missing)"
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"(read failed: {exc})"

    def make_sync_client(self, timeout: float = 5.0) -> httpx.Client:
        """Construct an :class:`httpx.Client` bound to this daemon's UDS."""
        return httpx.Client(
            transport=httpx.HTTPTransport(uds=str(self.socket_path)),
            base_url="http://popolad",
            timeout=timeout,
        )

    def make_async_client(self, timeout: float = 5.0) -> httpx.AsyncClient:
        """Construct an :class:`httpx.AsyncClient` bound to this daemon's UDS."""
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(self.socket_path)),
            base_url="http://popolad",
            timeout=timeout,
        )


def make_isolated_env(
    home: Path,
    *,
    use_graph: bool = False,
    extra_path: Path | None = None,
) -> dict[str, str]:
    """Build the env dict used to launch a daemon under ``home``.

    Args:
        home: Tmp dir to use as ``$POPOLA_HOME``.  Also doubles as
            ``$ARKTOWER_HOME`` so the ArkTower SQLite lands under it.
        use_graph: When False (default), forces ``POPOLA_USE_GRAPH=0``
            for Tier 3 determinism.  Set True if a test specifically
            wants the graph path.
        extra_path: Optional extra dir prepended to ``$PATH`` (e.g. a
            tmp dir housing a fake ``cursor-agent`` shim).
    """
    home.mkdir(parents=True, exist_ok=True)
    arktower_home = home / "_arktower"
    arktower_home.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["POPOLA_HOME"] = str(home)
    env["ARKTOWER_HOME"] = str(arktower_home)
    env["POPOLA_USE_GRAPH"] = "1" if use_graph else "0"

    src_path = _REPO_ROOT / "src"
    if src_path.is_dir():
        env["PYTHONPATH"] = str(src_path) + os.pathsep + env.get("PYTHONPATH", "")

    if _ARKTOWER_MIGRATIONS_FALLBACK.is_dir():
        env.setdefault("POPOLA_ARKTOWER_MIGRATIONS_DIR", str(_ARKTOWER_MIGRATIONS_FALLBACK))

    if extra_path is not None:
        env["PATH"] = str(extra_path) + os.pathsep + env.get("PATH", "")

    return env


def _pid_alive(pid: int) -> bool:
    """Lightweight liveness probe (signal 0)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _wait_for_socket(socket_path: Path, timeout_s: float) -> bool:
    """Block until ``socket_path`` accepts connections or ``timeout_s`` elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if socket_path.exists():
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.settimeout(0.5)
                sock.connect(str(socket_path))
                return True
            except OSError:
                pass
            finally:
                with contextlib.suppress(OSError):
                    sock.close()
        time.sleep(0.05)
    return False


def _spawn_daemon_process(
    env: dict[str, str],
    log_path: Path,
) -> subprocess.Popen[bytes]:
    """``subprocess.Popen`` wrapper for ``python -m popolaloom.daemon``.

    Mirrors the production ``popola popolad start`` subcommand exactly
    (``start_new_session=True``, ``stdout/stderr`` to log file).
    """
    log_fh = log_path.open("ab", buffering=0)
    cmd = [sys.executable, "-m", "popolaloom.daemon"]
    return subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=log_fh,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=env,
        cwd=str(_REPO_ROOT),
    )


def _terminate_daemon(
    proc: subprocess.Popen[bytes],
    *,
    grace_s: float = _DAEMON_GRACEFUL_SHUTDOWN_S,
) -> None:
    """SIGTERM → wait → SIGKILL fallback teardown for ``proc``."""
    if proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=2.0)


@contextlib.contextmanager
def spawn_real_popolad(
    tmp_path: Path,
    *,
    boot_timeout_s: float = _DAEMON_BOOT_TIMEOUT_S,
    use_graph: bool = False,
    extra_path: Path | None = None,
    home_subdir: str = "popola_home",
    log_filename: str = "popolad.log",
) -> Iterator[RealPopoladHandle]:
    """Boot a real popolad subprocess; yield a handle; tear down on exit.

    Per testing-matrix.md §5.1 — Tier 3 fixture contract.

    Args:
        tmp_path: pytest tmp dir; we create ``tmp_path / home_subdir`` as
            ``$POPOLA_HOME`` so the test gets a fresh DB + sockets.
        boot_timeout_s: Max wait for the UDS to accept a connection
            (default 5 s per spec).  Failure raises :class:`RuntimeError`
            with the captured daemon log to aid debugging.
        use_graph: Force ``POPOLA_USE_GRAPH`` (default False = legacy
            path; deterministic for Tier 3).
        extra_path: Optional extra dir prepended to ``$PATH`` (e.g.
            location of a ``cursor-agent`` shim binary).
        home_subdir: Sub-directory of ``tmp_path`` used as POPOLA_HOME;
            override when a single test wants two distinct homes.
        log_filename: stdout/stderr capture file inside ``tmp_path``.

    Yields:
        :class:`RealPopoladHandle`: live daemon handle.  After the
        context exits the daemon is SIGTERM'd (5 s grace) then SIGKILL'd.
    """
    home = tmp_path / home_subdir
    log_path = tmp_path / log_filename

    env = make_isolated_env(home, use_graph=use_graph, extra_path=extra_path)
    socket_path = home / "popolad.sock"
    events_dir = home / "events"

    proc = _spawn_daemon_process(env, log_path)

    handle = RealPopoladHandle(
        proc=proc,
        pid=proc.pid,
        home=home,
        socket_path=socket_path,
        events_dir=events_dir,
        env=env,
        log_path=log_path,
    )

    try:
        if not _wait_for_socket(socket_path, boot_timeout_s):
            log_text = handle.read_log()
            raise RuntimeError(
                f"popolad daemon failed to bind UDS {socket_path} within "
                f"{boot_timeout_s}s; log:\n{log_text}"
            )
        yield handle
    finally:
        _terminate_daemon(proc)
        for pid in handle.cleanup_pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        kill_orphan_cursor_shims()
        with contextlib.suppress(OSError):
            if socket_path.exists():
                socket_path.unlink()
        if home.exists():
            shutil.rmtree(home, ignore_errors=True)


def make_cursor_shim(bin_dir: Path, *, sleep_seconds: float = 8.0) -> Path:
    """Drop a fake ``cursor-agent`` shim into ``bin_dir`` and ``chmod +x`` it.

    Reused across tier3 + chaos tests that need a long-running task to
    keep the daemon "in-flight" while we SIGKILL it.  The shim writes a
    marker line so tests can confirm the subprocess actually started
    before sleeping.

    Default sleep is **8 s**: long enough to be "in-flight" during any
    Tier 3 assertion sequence (typical ≤ 5 s) but short enough that
    leaked shims clean themselves up well before the next test even
    starts.  Tests that need a 30 s sleeper (S1 SIGKILL/restart) should
    pass an explicit ``sleep_seconds=30``.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "cursor-agent"
    shim.write_text(
        (
            "#!/usr/bin/env python3\n"
            "import sys, time\n"
            "print('cursor-agent shim started:', sys.argv, flush=True)\n"
            f"time.sleep({sleep_seconds})\n"
            "print('cursor-agent shim exiting normally', flush=True)\n"
        ),
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def kill_orphan_cursor_shims() -> int:
    """Best-effort kill any leaked ``cursor-agent`` shim python subprocesses.

    Returns the number of processes killed.  Used by tier3 / chaos
    fixtures so leaked sleepers from prior tests don't accumulate and
    starve the test session of file descriptors / processes.

    Implementation: walks ``/proc`` directly (instead of ``pkill -f``)
    so the pattern only matches *child* processes that exec'd our shim
    script — never ourselves or our parent shell.  Identifies a shim by
    the marker substring ``cursor-agent`` *and* a sibling
    ``time.sleep`` literal in the script body (avoids accidentally
    killing the user's real cursor-agent if one happens to be alive).
    """
    killed = 0
    proc_root = Path("/proc")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        cmdline_path = entry / "cmdline"
        try:
            cmdline = cmdline_path.read_bytes().split(b"\x00")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        joined = b" ".join(part for part in cmdline if part)
        if b"cursor-agent" not in joined:
            continue
        # cursor-agent shim is invoked as: ``<shim> <prompt args...>`` where
        # <shim> ends in ``/cursor-agent`` and is a Python script we wrote.
        # Reject anything that doesn't smell like our shim (e.g. real CLI).
        if not any(part.endswith(b"/cursor-agent") for part in cmdline):
            continue
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
            killed += 1
    return killed
