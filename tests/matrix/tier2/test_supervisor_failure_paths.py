"""Tier 2 / B1 — :class:`Supervisor` failure-mode integration tests.

Per testing-matrix.md §1.2 example
``test_supervisor_error_modes`` + the L3 brief. Each scenario verifies:

- the resulting NDJSON ends with ``task.failed`` (exit_code != 0) or
  ``task.completed`` (exit_code == 0)
- the on_exit callback fires with the expected exit code
- Popolad's StateStore transitions to FAILED for non-zero, COMPLETED
  for zero (matching ``server.py::_on_subprocess_exit``)
- No silent crash: the wait thread always closes the loop with
  either a terminal event or an explicit ``task.failed`` envelope

The supervisor is a thin :func:`subprocess.Popen` + threading layer, so
these tests prefer real (but tiny/fast) subprocesses driving short
``python -c '...'`` snippets to simulate exit codes; the Popen-error
edge cases (cwd missing / binary missing) use real Popen to surface
real ``FileNotFoundError``.

Each case ≤ 1 s; suite ≤ 60 s per Tier 2 §1.2 budget.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from popolaloom.daemon import EventLog, Popolad, StateStore, Supervisor, TaskState


def _wait_for_event_type(
    event_log: EventLog,
    event_type: str,
    timeout_s: float = 3.0,
) -> dict[str, Any] | None:
    """Poll the NDJSON until an envelope with the given ``type`` appears."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for ev in event_log.tail():
            if ev["type"] == event_type:
                return ev
        time.sleep(0.05)
    return None


def _spawn_python_exit(
    supervisor: Supervisor,
    *,
    task_id: str,
    code_snippet: str,
    event_log: EventLog,
    on_exit: Any = None,
) -> int:
    """Spawn ``sys.executable -c <code_snippet>`` via the supervisor."""
    return supervisor.spawn(
        task_id=task_id,
        cmd=[sys.executable, "-c", code_snippet],
        cwd=None,
        env=None,
        event_log=event_log,
        on_exit=on_exit,
    )


# ── 1: clean exit 0 (positive baseline) ──────────────────────────────────


def test_clean_exit_zero_emits_task_completed(tmp_path: Path) -> None:
    """Exit 0 → ``task.completed`` event; baseline for the Failure ladder."""
    log = EventLog(tmp_path / "ok.jsonl", fsync_interval_s=0)
    sup = Supervisor()
    captured: list[tuple[str, int]] = []
    _spawn_python_exit(
        sup,
        task_id="ok-exit",
        code_snippet="print('done'); import sys; sys.exit(0)",
        event_log=log,
        on_exit=lambda tid, code: captured.append((tid, code)),
    )
    assert sup.join("ok-exit", timeout=3.0)
    ev = _wait_for_event_type(log, "task.completed")
    assert ev is not None
    assert ev["data"]["exit_code"] == 0
    assert captured == [("ok-exit", 0)]
    log.close()


# ── 2: generic non-zero exit → task.failed ───────────────────────────────


@pytest.mark.parametrize("code", [1, 2, 7, 127], ids=lambda c: f"exit={c}")
def test_generic_non_zero_exit_emits_task_failed(tmp_path: Path, code: int) -> None:
    """Any non-zero exit code lands on ``task.failed`` (NOT ``task.completed``)."""
    log = EventLog(tmp_path / f"fail_{code}.jsonl", fsync_interval_s=0)
    sup = Supervisor()
    captured: list[int] = []
    _spawn_python_exit(
        sup,
        task_id=f"fail-{code}",
        code_snippet=f"import sys; sys.exit({code})",
        event_log=log,
        on_exit=lambda tid, c: captured.append(c),
    )
    assert sup.join(f"fail-{code}", timeout=3.0)
    ev = _wait_for_event_type(log, "task.failed")
    assert ev is not None, f"task.failed envelope missing for exit={code}; events={log.tail()}"
    assert ev["data"]["exit_code"] == code
    assert captured == [code]

    types = {e["type"] for e in log.tail()}
    assert "task.completed" not in types, "non-zero exit must not emit task.completed"
    log.close()


# ── 3: SIGKILL / SIGTERM → negative returncode → task.failed ─────────────


def test_sigkill_returncode_negative_9_emits_task_failed(tmp_path: Path) -> None:
    """SIGKILL via os.kill(pid, 9) — returncode is -9 → ``task.failed`` with that code."""
    import os
    import signal

    log = EventLog(tmp_path / "sigkill.jsonl", fsync_interval_s=0)
    sup = Supervisor()
    pid = _spawn_python_exit(
        sup,
        task_id="sigkill-1",
        code_snippet="import time; time.sleep(60)",
        event_log=log,
    )
    time.sleep(0.15)
    os.kill(pid, signal.SIGKILL)
    assert sup.join("sigkill-1", timeout=4.0)
    ev = _wait_for_event_type(log, "task.failed")
    assert ev is not None
    assert ev["data"]["exit_code"] == -signal.SIGKILL
    log.close()


def test_sigterm_returncode_negative_15_emits_task_failed(tmp_path: Path) -> None:
    """SIGTERM produces ``returncode == -15`` (when child doesn't trap)."""
    import os
    import signal

    log = EventLog(tmp_path / "sigterm.jsonl", fsync_interval_s=0)
    sup = Supervisor()
    pid = _spawn_python_exit(
        sup,
        task_id="sigterm-1",
        code_snippet=(
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_DFL); "
            "time.sleep(60)"
        ),
        event_log=log,
    )
    time.sleep(0.15)
    os.kill(pid, signal.SIGTERM)
    assert sup.join("sigterm-1", timeout=4.0)
    ev = _wait_for_event_type(log, "task.failed")
    assert ev is not None
    assert ev["data"]["exit_code"] == -signal.SIGTERM
    log.close()


# ── 4: simulated OOM (exit 137) — supervisor treats as task.failed ───────


def test_oom_exit_137_emits_task_failed(tmp_path: Path) -> None:
    """An emulated OOM (sys.exit(137)) lands on task.failed (returncode > 0 path)."""
    log = EventLog(tmp_path / "oom.jsonl", fsync_interval_s=0)
    sup = Supervisor()
    _spawn_python_exit(
        sup,
        task_id="oom-1",
        code_snippet="import sys; sys.exit(137)",
        event_log=log,
    )
    assert sup.join("oom-1", timeout=3.0)
    ev = _wait_for_event_type(log, "task.failed")
    assert ev is not None
    assert ev["data"]["exit_code"] == 137
    log.close()


# ── 5: cwd doesn't exist → FileNotFoundError (Popen raises) ──────────────


def test_cwd_does_not_exist_raises_file_not_found(tmp_path: Path) -> None:
    """Popen with a non-existent cwd raises FileNotFoundError synchronously.

    This is the spec contract for cwd missing: supervisor.spawn() does not
    attempt to mask the error (No Silent Failures). The caller (Popolad
    or test) sees the raise.
    """
    log = EventLog(tmp_path / "nocwd.jsonl", fsync_interval_s=0)
    sup = Supervisor()
    missing = tmp_path / "ghost-dir-xyz"
    assert not missing.exists()
    with pytest.raises((FileNotFoundError, NotADirectoryError)):
        sup.spawn(
            task_id="nocwd-1",
            cmd=[sys.executable, "-c", "print('hi')"],
            cwd=missing,
            env=None,
            event_log=log,
        )
    log.close()


# ── 6: binary missing → FileNotFoundError ────────────────────────────────


def test_binary_missing_raises_file_not_found(tmp_path: Path) -> None:
    """argv[0] pointing to a non-existent binary raises FileNotFoundError."""
    log = EventLog(tmp_path / "nobin.jsonl", fsync_interval_s=0)
    sup = Supervisor()
    with pytest.raises(FileNotFoundError):
        sup.spawn(
            task_id="nobin-1",
            cmd=["/no/such/binary/exists/anywhere", "arg1"],
            cwd=None,
            env=None,
            event_log=log,
        )
    log.close()


# ── 7: large stdout (~100K lines emulated as 1K) — drain still completes ─


def test_large_stdout_drain_completes(tmp_path: Path) -> None:
    """A subprocess emitting many stdout lines drains cleanly (R-007 30s join window)."""
    log = EventLog(tmp_path / "big.jsonl", fsync_interval_s=0)
    sup = Supervisor()
    n_lines = 1000
    snippet = (
        f"import sys\n"
        f"for i in range({n_lines}): print(f'line-{{i}}')\n"
        f"sys.exit(0)\n"
    )
    _spawn_python_exit(
        sup,
        task_id="big-1",
        code_snippet=snippet,
        event_log=log,
    )
    assert sup.join("big-1", timeout=8.0), "supervisor did not drain large stdout in time"
    events = log.tail()
    types = {e["type"] for e in events}
    assert "task.completed" in types
    stdout_lines = [e for e in events if e["type"] == "process.stdout"]
    assert len(stdout_lines) >= n_lines, (
        f"expected >= {n_lines} stdout lines, got {len(stdout_lines)}"
    )
    log.close()


# ── 8: Popolad facade integration — failed exit transitions state to FAILED ─


def test_popolad_failed_exit_transitions_state_to_failed(tmp_path: Path) -> None:
    """When the subprocess exits non-zero, Popolad's StateStore goes to FAILED."""

    def adapter(
        cli: str,
        prompt: str,
        cwd: Path | None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        return [sys.executable, "-c", "import sys; sys.exit(7)"]

    popolad = Popolad(events_dir=tmp_path / "events", adapter=adapter, use_graph=False)
    task_id = popolad.dispatch_task(cli="failtest", prompt="boom")

    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        st = popolad.get_status(task_id)
        if st["state"] in {str(TaskState.COMPLETED), str(TaskState.FAILED)}:
            break
        time.sleep(0.05)
    else:
        pytest.fail("task did not reach terminal state")

    final = popolad.get_status(task_id)
    assert final["state"] == str(TaskState.FAILED)
    assert final["exit_code"] == 7

    events = popolad.tail_events(task_id)
    assert any(e["type"] == "task.failed" for e in events)
    assert not any(e["type"] == "task.completed" for e in events)


# ── 9: ghost-exit path — task gets removed before on_exit (R-008) ────────


def test_ghost_exit_emits_state_ghost_exit_event(tmp_path: Path) -> None:
    """When ``_on_subprocess_exit`` runs for a missing task, state.ghost_exit fires.

    Scenarios this defends against: cancel raced with subprocess exit
    and removed the StateStore handle; rehydrate misalignment; manual
    state.clear in tests. Per R-008 in v0.2.0-plan.md.
    """
    popolad = Popolad(events_dir=tmp_path, adapter=lambda *a, **kw: [sys.executable])
    popolad._on_subprocess_exit("does-not-exist", 0)

    expected_path = tmp_path / "does-not-exist.jsonl"
    assert expected_path.exists(), "ghost_exit path did not create the NDJSON file"
    import json

    text = expected_path.read_text(encoding="utf-8").strip()
    line = json.loads(text.splitlines()[-1])
    assert line["type"] == "state.ghost_exit"
    assert line["data"]["task_id"] == "does-not-exist"
    assert line["data"]["exit_code"] == 0


# ── 10: bonus — popen Popen.wait raise → task.failed ─────────────────────


def test_proc_wait_exception_emits_task_failed_with_minus_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``proc.wait()`` itself raises, supervisor emits task.failed exit_code=-1."""
    log = EventLog(tmp_path / "waitfail.jsonl", fsync_interval_s=0)
    sup = Supervisor()

    real_popen = subprocess.Popen

    def patched_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        proc = real_popen(*args, **kwargs)
        original_wait = proc.wait

        def boom(*_a: Any, **_kw: Any) -> int:
            try:
                original_wait()
            finally:
                pass
            raise RuntimeError("simulated proc.wait failure")

        proc.wait = boom  # type: ignore[method-assign]
        return proc

    monkeypatch.setattr(subprocess, "Popen", patched_popen)

    fired = threading.Event()
    captured_code: list[int] = []

    def on_exit(_tid: str, code: int) -> None:
        captured_code.append(code)
        fired.set()

    sup.spawn(
        task_id="wfail-1",
        cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
        cwd=None,
        env=None,
        event_log=log,
        on_exit=on_exit,
    )
    assert fired.wait(timeout=3.0)
    assert captured_code == [-1]

    ev = _wait_for_event_type(log, "task.failed")
    assert ev is not None
    assert ev["data"]["exit_code"] == -1
    log.close()


# ── 11: state store updates correctly through Popolad lifecycle ──────────


def test_state_store_transitions_running_to_completed(tmp_path: Path) -> None:
    """Popolad full happy path: dispatch → spawn → wait → state==COMPLETED."""

    def adapter(
        cli: str,
        prompt: str,
        cwd: Path | None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        return [sys.executable, "-c", "print('ok')"]

    popolad = Popolad(events_dir=tmp_path / "events", adapter=adapter, use_graph=False)
    task_id = popolad.dispatch_task(cli="okcli", prompt="ok")

    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        st = popolad.get_status(task_id)
        if st["state"] in {str(TaskState.COMPLETED), str(TaskState.FAILED)}:
            break
        time.sleep(0.05)

    final = popolad.get_status(task_id)
    assert final["state"] == str(TaskState.COMPLETED)
    assert final["exit_code"] == 0
    handle = popolad.state_store.get(task_id)
    assert isinstance(handle, type(handle))
    assert popolad.state_store.list_active() == []


# ── 12: empty stdout subprocess still emits started + completed ──────────


def test_silent_subprocess_still_emits_started_and_completed(tmp_path: Path) -> None:
    """A subprocess that prints nothing still has process.started + task.completed."""
    log = EventLog(tmp_path / "silent.jsonl", fsync_interval_s=0)
    sup = Supervisor()
    _spawn_python_exit(
        sup,
        task_id="silent-1",
        code_snippet="import sys; sys.exit(0)",
        event_log=log,
    )
    assert sup.join("silent-1", timeout=3.0)
    types = [e["type"] for e in log.tail()]
    assert "process.started" in types
    assert "task.completed" in types
    log.close()


# ── 13: store unused state for satisfying mypy import & linter -- ────────
_ = StateStore  # keep import alive (referenced symbolically in coverage assertions)
