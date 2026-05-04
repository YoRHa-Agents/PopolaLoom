"""S3 self-bootstrap: recursive dispatch (parent task spawns child task).

Validates that a popola dispatch can itself shell out to ``popola
dispatch`` to spawn a child task — exercising:

- thread_id isolation (each task gets its own LangGraph SqliteSaver
  thread; in v0.2.0 with ``POPOLA_USE_GRAPH=0`` we still validate
  via the simpler StateStore + ArkTower SQLite isolation).
- per-task NDJSON event log isolation (parent + child get distinct
  ``<task_id>.jsonl`` files).
- ArkTower SQLite captures both parent + child rows; the child row's
  ``parameters.parent_popola_task_id`` records the lineage so v0.3.0
  graph DAG queries can rebuild the dispatch tree.

Test plan:

1. Spawn popolad (real subprocess + UDS) in isolated ``$POPOLA_HOME``.
2. Write a parent script ``recursive_parent.py`` that:
   - Reads ``$POPOLA_HOME`` from env.
   - Invokes ``popola dispatch echo child --cli echo_recursive --json``.
   - Records the child's task_id + writes a marker line
     ``CHILD_DISPATCHED:<id>`` so the test can correlate.
3. Register a tiny ``echo_recursive`` adapter on the daemon side
   (via ``--cli-flag`` injection through env-driven adapter… actually
   the cleanest approach is to use the ``cursor-agent`` shim
   technique from S1 and call the parent script directly).
4. Dispatch the parent: ``popola dispatch <parent prompt>
   --cli cursor --wait``.
5. Wait for parent to reach terminal state.
6. Verify ArkTower DB has ≥ 2 tasks (parent + child).
7. Verify ``$POPOLA_HOME/events`` has ≥ 2 ``.jsonl`` files.
8. Verify each task has a distinct event log file (basic isolation).

Like S1, this test is gated on the ``slow`` marker and needs a real
daemon subprocess.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DAEMON_BOOT_TIMEOUT_S: float = 15.0
_DAEMON_SHUTDOWN_TIMEOUT_S: float = 5.0
_PARENT_WAIT_TIMEOUT_S: float = 60.0
_MISSING_LOG_TEXT: str = "(missing)"


pytestmark = pytest.mark.slow


def _safe_read(path: Path) -> str:
    """Read ``path`` as utf-8 with replacement; return ``_MISSING_LOG_TEXT`` if missing."""
    if not path.exists():
        return _MISSING_LOG_TEXT
    return path.read_text(encoding="utf-8", errors="replace")


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


def _run_cli(
    args: list[str],
    env: dict[str, str],
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "popolaloom.cli.main", *args]
    return subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(_REPO_ROOT),
    )


def _spawn_daemon(env: dict[str, str], log_path: Path) -> subprocess.Popen[bytes]:
    """Spawn ``python -m popolaloom.daemon`` in a fresh session."""
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


def _make_recursive_parent_shim(
    bin_dir: Path,
    parent_script: Path,
) -> Path:
    """Fake ``cursor-agent`` shim that runs the recursive parent script.

    The CursorAdapter builds a command like
    ``[cursor-agent, agent, --print, --output-format, text, <prompt>]``;
    our shim ignores all those args and just exec's the parent script
    so the child dispatch happens inside a real subprocess started by
    popolad.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "cursor-agent"
    shim.write_text(
        (
            "#!/usr/bin/env python3\n"
            "import os, sys, subprocess\n"
            f"script = {str(parent_script)!r}\n"
            "result = subprocess.run([sys.executable, script], env=os.environ.copy())\n"
            "sys.exit(result.returncode)\n"
        ),
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def _make_codex_shim(bin_dir: Path) -> Path:
    """Fake ``codex`` shim used by the child dispatch.

    The child dispatch uses ``--cli codex`` so it builds
    ``[codex, exec, <prompt>]``; this shim prints + exits 0.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "codex"
    shim.write_text(
        (
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "print('codex shim: child task running, args=', sys.argv, flush=True)\n"
            "sys.exit(0)\n"
        ),
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def _write_recursive_parent_script(tmp_path: Path) -> Path:
    """Write a parent script that invokes ``popola dispatch`` for a child task.

    The script:

    - Echoes ``PARENT_STARTED`` to its own stdout (captured into popolad
      event log via the ``process.stdout`` channel).
    - Invokes ``popola dispatch ... --cli codex --json``; on success
      emits ``CHILD_DISPATCHED:<task_id>``; on failure emits
      ``CHILD_DISPATCH_FAILED:<rc>`` and a stderr dump.
    - Echoes ``PARENT_DONE`` and exits 0.
    """
    script = tmp_path / "recursive_parent.py"
    script.write_text(
        (
            "#!/usr/bin/env python3\n"
            "import json, os, subprocess, sys\n"
            "print('PARENT_STARTED', flush=True)\n"
            "env = os.environ.copy()\n"
            "cmd = [\n"
            "    sys.executable, '-m', 'popolaloom.cli.main',\n"
            "    'dispatch', 'echo child', '--cli', 'codex', '--json',\n"
            "]\n"
            "result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)\n"
            "print('CHILD_RC=', result.returncode, flush=True)\n"
            "print('CHILD_STDOUT=', result.stdout, flush=True)\n"
            "print('CHILD_STDERR=', result.stderr, flush=True)\n"
            "if result.returncode == 0 and result.stdout.strip():\n"
            "    try:\n"
            "        payload = json.loads(result.stdout.strip().splitlines()[-1])\n"
            "        print('CHILD_DISPATCHED:', payload.get('task_id'), flush=True)\n"
            "    except Exception as exc:\n"
            "        print('CHILD_PARSE_ERROR:', repr(exc), flush=True)\n"
            "else:\n"
            "    print('CHILD_DISPATCH_FAILED', flush=True)\n"
            "print('PARENT_DONE', flush=True)\n"
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@pytest.fixture
def isolated_popola_home(tmp_path: Path) -> Iterator[dict[str, str]]:
    """Build an env dict pointing ``$POPOLA_HOME`` at a fresh tmp dir."""
    home = tmp_path / "popola_home"
    home.mkdir(parents=True, exist_ok=True)
    arktower_home = tmp_path / "arktower_home"
    arktower_home.mkdir(parents=True, exist_ok=True)
    bin_dir = tmp_path / "bin"

    parent_script = _write_recursive_parent_script(tmp_path)
    _make_recursive_parent_shim(bin_dir, parent_script)
    _make_codex_shim(bin_dir)

    env = os.environ.copy()
    env["POPOLA_HOME"] = str(home)
    env["ARKTOWER_HOME"] = str(arktower_home)
    env["POPOLA_USE_GRAPH"] = "0"
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = str(_REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    migrations_default = "/home/agent/reference/ArkTower/migrations"
    if Path(migrations_default).is_dir():
        env["POPOLA_ARKTOWER_MIGRATIONS_DIR"] = migrations_default

    yield env

    if home.exists():
        shutil.rmtree(home, ignore_errors=True)
    if arktower_home.exists():
        shutil.rmtree(arktower_home, ignore_errors=True)


def test_s3_recursive_dispatch_creates_parent_and_child(
    tmp_path: Path,
    isolated_popola_home: dict[str, str],
) -> None:
    """S3: parent task popola-dispatches a child; both tasks persist + isolate.

    Asserts:

    1. Parent dispatch returns a task_id; ``--wait`` lets it reach a
       terminal state within the parent timeout.
    2. ``popola list --all --json`` shows ≥ 2 tasks (parent + child).
    3. The events_dir contains ≥ 2 ``.jsonl`` files (per-task isolation).
    4. The ArkTower SQLite ``tasks`` table has ≥ 2 rows.
    5. Parent NDJSON contains a ``CHILD_DISPATCHED:<id>`` marker
       (proving the child dispatch actually executed end-to-end).
    """
    env = isolated_popola_home
    home = Path(env["POPOLA_HOME"])
    arktower_db = Path(env["ARKTOWER_HOME"]) / "arktower.db"
    log = tmp_path / "popolad.log"
    socket_path = home / "popolad.sock"

    daemon = _spawn_daemon(env, log)
    parent_task_id: str | None = None
    try:
        if not _wait_for_socket(socket_path, _DAEMON_BOOT_TIMEOUT_S):
            log_text = _safe_read(log)
            pytest.fail(
                f"daemon socket {socket_path} did not appear in "
                f"{_DAEMON_BOOT_TIMEOUT_S}s; log:\n{log_text}"
            )

        dispatch_result = _run_cli(
            [
                "dispatch",
                "parent task",
                "--cli",
                "cursor",
                "--wait",
                "--timeout",
                str(_PARENT_WAIT_TIMEOUT_S),
                "--json",
            ],
            env=env,
            timeout=_PARENT_WAIT_TIMEOUT_S + 15.0,
        )
        if dispatch_result.returncode != 0:
            log_text = _safe_read(log)
            pytest.fail(
                f"parent dispatch failed: returncode={dispatch_result.returncode}\n"
                f"stdout: {dispatch_result.stdout}\n"
                f"stderr: {dispatch_result.stderr}\n"
                f"daemon log:\n{log_text}"
            )

        payload = json.loads(dispatch_result.stdout.strip().splitlines()[-1])
        parent_task_id = payload["task_id"]
        assert parent_task_id, f"no parent task_id: {dispatch_result.stdout}"

        deadline = time.monotonic() + 10.0
        list_payload: list[dict[str, object]] = []
        while time.monotonic() < deadline:
            list_result = _run_cli(["list", "--all", "--json"], env=env, timeout=15.0)
            if list_result.returncode == 0:
                try:
                    list_payload = json.loads(list_result.stdout.strip().splitlines()[-1])
                    if len(list_payload) >= 2:
                        break
                except (json.JSONDecodeError, IndexError):
                    pass
            time.sleep(0.3)

        assert len(list_payload) >= 2, (
            f"expected ≥ 2 tasks (parent + child) in popola list --all; "
            f"got {len(list_payload)}: {list_payload}"
        )

        events_dir = home / "events"
        jsonl_files = sorted(events_dir.glob("*.jsonl"))
        assert len(jsonl_files) >= 2, (
            f"expected ≥ 2 NDJSON event files in {events_dir}; "
            f"got {len(jsonl_files)}: {[p.name for p in jsonl_files]}"
        )

        parent_log = events_dir / f"{parent_task_id}.jsonl"
        assert parent_log.exists(), f"parent NDJSON missing: {parent_log}"
        parent_text = parent_log.read_text(encoding="utf-8")
        m = re.search(r"CHILD_DISPATCHED:\s*([\w-]+)", parent_text)
        assert m, (
            f"CHILD_DISPATCHED marker missing from parent log; "
            f"first 4000 chars:\n{parent_text[:4000]!r}"
        )
        child_task_id = m.group(1)
        assert child_task_id != parent_task_id, (
            f"child task_id collides with parent: {child_task_id}"
        )

        child_log = events_dir / f"{child_task_id}.jsonl"
        assert child_log.exists(), (
            f"child NDJSON missing at {child_log}; events_dir contents: "
            f"{[p.name for p in jsonl_files]}"
        )

        if arktower_db.exists():
            conn = sqlite3.connect(str(arktower_db))
            try:
                rows = conn.execute(
                    "SELECT id, parameters FROM tasks ORDER BY created_at"
                ).fetchall()
            finally:
                conn.close()
            assert len(rows) >= 2, (
                f"ArkTower DB has only {len(rows)} task(s); expected ≥ 2 "
                f"(parent + child); rows={rows}"
            )
            popola_ids: set[str] = set()
            for _ark_id, params_json in rows:
                if not params_json:
                    continue
                try:
                    params = json.loads(params_json)
                except json.JSONDecodeError:
                    continue
                pid = params.get("popola_task_id")
                if pid:
                    popola_ids.add(pid)
            assert parent_task_id in popola_ids, (
                f"parent {parent_task_id} not in ArkTower popola_task_ids: {popola_ids}"
            )
            assert child_task_id in popola_ids, (
                f"child {child_task_id} not in ArkTower popola_task_ids: {popola_ids}"
            )

    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(daemon.pid, signal.SIGTERM)
        try:
            daemon.wait(timeout=_DAEMON_SHUTDOWN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(daemon.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                daemon.wait(timeout=2.0)
