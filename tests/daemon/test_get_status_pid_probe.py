"""F2 ``kill -0`` probe in :meth:`Popolad.get_status` (v0.9.9 / Q-V099-4).

Pins the WARN-only status-vs-pid drift detector that was introduced as a
direct response to ``feedback_for_v0.9.7.md`` lines 37-41 (operators saw
``state=running`` for tasks whose subprocess had already exited but whose
supervisor wait-thread had not yet flipped the in-memory state).

Acceptance contract (Q-V099-4):

(a) When ``handle.runtime == "local"`` AND ``handle.state == RUNNING``
    AND ``handle.pid is not None``, :meth:`Popolad.get_status` calls
    ``os.kill(handle.pid, 0)``. On :class:`ProcessLookupError`, the
    returned summary gains ``pid_alive=False`` AND a daemon-log
    ``WARNING`` is emitted (caplog-captured here).

(b) ``--json`` consumers receive the new ``pid_alive`` field naturally
    because :meth:`Popolad.get_status` returns a plain dict that the RPC
    layer JSON-serializes end-to-end (verified via :func:`json.dumps`).

(c) The test simulates a true "pid reap mid-status-call" race by
    spawning a real :class:`subprocess.Popen`, capturing the pid, then
    ``os.kill(pid, signal.SIGKILL)`` + ``proc.wait()`` to reap it
    *before* invoking :meth:`Popolad.get_status` with a stale
    ``handle.pid``. Both the WARN log and ``pid_alive=False`` must
    surface.

(d) ``pid_alive`` is **absent** from the summary dict when
    ``handle.state != RUNNING`` (e.g. ``COMPLETED``) — additive-only
    contract per AC #4 so old consumers keep working.

(e) ``pid_alive`` is **absent** when ``handle.runtime != "local"`` (e.g.
    ``cloud``) — same additive-only contract.

(f) :class:`PermissionError` from ``os.kill`` (process exists but the
    daemon lacks signal permission) → ``pid_alive=True`` (the process
    *does* exist; we treat the EPERM signal as positive evidence).

Force-finalize is intentionally OUT OF SCOPE per Q-V099-4 (deferred to
``BL-v0.10.0-supervisor-force-finalize``); these tests never call
``Supervisor.force_finalize`` and assert the patch only emits a WARN.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from popolaloom.daemon.server import Popolad
from popolaloom.daemon.state import TaskHandle, TaskState


def _register(
    tmp_path: Path,
    *,
    task_id: str,
    pid: int | None,
    state: TaskState,
    runtime: str = "local",
) -> Popolad:
    """Build a fresh :class:`Popolad` and register one synthetic handle."""
    popolad = Popolad(events_dir=tmp_path / "events")
    handle = TaskHandle(
        task_id=task_id,
        cli="cursor-cloud" if runtime == "cloud" else "cursor",
        pid=pid,
        state=state,
        started_at=datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC),
        event_log_path=tmp_path / "events" / f"{task_id}.jsonl",
        runtime=runtime,
        cursor_agent_id="bc-x" if runtime == "cloud" else None,
        cursor_run_id="run-x" if runtime == "cloud" else None,
    )
    popolad.state_store.register(handle)
    return popolad


# ── (a) + (c): real subprocess, reap pid, assert WARN + pid_alive=False ────


def test_local_running_with_reaped_pid_reports_pid_alive_false_and_warns(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Real-pid race: spawn a subprocess, reap it, then call get_status.

    The supervisor wait-thread is not running here, so ``handle.state``
    stays ``RUNNING`` even though the OS-level process is gone — exactly
    the v0.9.7 drift symptom feedback line 37-41 documents. The probe
    must catch it via :class:`ProcessLookupError` and surface
    ``pid_alive=False`` plus a daemon-log ``WARNING``.
    """
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os, time; print(os.getpid()); time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    pid = proc.pid
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        proc.wait(timeout=5)
        pytest.fail(
            "subprocess died before SIGKILL could fire; rerun the test"
        )
    proc.wait(timeout=5)
    # Sanity: the kernel really has reaped the pid (a follow-up signal
    # MUST raise ProcessLookupError); otherwise the probe assertion
    # below would be a tautology.
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

    popolad = _register(
        tmp_path,
        task_id="t-reaped",
        pid=pid,
        state=TaskState.RUNNING,
        runtime="local",
    )

    with caplog.at_level(logging.WARNING, logger="popolaloom.daemon.server"):
        summary = popolad.get_status("t-reaped")

    assert "pid_alive" in summary, (
        "F2 probe must add 'pid_alive' for local+RUNNING+pid handles"
    )
    assert summary["pid_alive"] is False, (
        f"reaped pid must surface pid_alive=False; got {summary['pid_alive']!r}"
    )

    drift_records = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "status drift" in record.getMessage()
    ]
    assert drift_records, (
        "expected a WARNING log mentioning 'status drift'; "
        f"got: {[r.getMessage() for r in caplog.records]!r}"
    )
    msg = drift_records[0].getMessage()
    assert "t-reaped" in msg, f"WARN log must include task_id; got: {msg!r}"
    assert str(pid) in msg, f"WARN log must include pid; got: {msg!r}"


# ── (b) JSON-mode flow-through ─────────────────────────────────────────────


def test_pid_alive_field_is_json_serializable(
    tmp_path: Path,
) -> None:
    """The new field round-trips through :func:`json.dumps` cleanly.

    The CLI ``--json`` mode and the FastAPI ``/status/{id}`` endpoint
    both rely on the summary dict being natively JSON-serializable. We
    use a known-alive pid (the test process itself) to cover the
    ``pid_alive=True`` branch end-to-end through serialization.
    """
    popolad = _register(
        tmp_path,
        task_id="t-json",
        pid=os.getpid(),
        state=TaskState.RUNNING,
        runtime="local",
    )
    summary = popolad.get_status("t-json")
    assert summary["pid_alive"] is True

    encoded = json.dumps(summary)
    decoded: dict[str, Any] = json.loads(encoded)
    assert decoded["pid_alive"] is True
    # State stays RUNNING — the probe must NOT mutate handle.state
    # (force-finalize is deferred per Q-V099-4).
    handle = popolad.state_store.get("t-json")
    assert handle is not None
    assert handle.state == TaskState.RUNNING


# ── (d) additive-only: absent for terminal states ─────────────────────────


def test_pid_alive_absent_when_state_completed(tmp_path: Path) -> None:
    """``state=COMPLETED`` MUST omit the ``pid_alive`` key entirely.

    Per AC #4 (additive-only), the field appears only when the probe
    actually fires. Probing a completed task would be misleading (the
    pid may have been reused by the OS for an unrelated process).
    """
    popolad = _register(
        tmp_path,
        task_id="t-done",
        pid=os.getpid(),
        state=TaskState.COMPLETED,
        runtime="local",
    )
    summary = popolad.get_status("t-done")
    assert "pid_alive" not in summary, (
        f"terminal-state task must not surface pid_alive; got {summary!r}"
    )


# ── (e) additive-only: absent for cloud runtime ───────────────────────────


def test_pid_alive_absent_for_cloud_runtime(tmp_path: Path) -> None:
    """Cloud-runtime tasks have no local pid — the field MUST be absent.

    Cloud handles store ``pid=None`` by construction; the probe is a
    local-only diagnostic so we suppress the field entirely (rather
    than emitting a misleading ``pid_alive=False`` for cloud tasks).
    """
    popolad = _register(
        tmp_path,
        task_id="t-cloud",
        pid=None,
        state=TaskState.RUNNING,
        runtime="cloud",
    )
    summary = popolad.get_status("t-cloud")
    assert summary["runtime"] == "cloud"
    assert "pid_alive" not in summary


def test_pid_alive_absent_for_local_running_without_pid(tmp_path: Path) -> None:
    """Local + RUNNING but ``pid=None`` (transition window) → no field.

    There is a brief window between dispatch_task assigning state=RUNNING
    and the supervisor populating ``handle.pid``; during it we cannot
    probe and MUST omit the field rather than guess.
    """
    popolad = _register(
        tmp_path,
        task_id="t-nopid",
        pid=None,
        state=TaskState.RUNNING,
        runtime="local",
    )
    summary = popolad.get_status("t-nopid")
    assert "pid_alive" not in summary


# ── (f) PermissionError → pid_alive=True ──────────────────────────────────


def test_permission_error_treated_as_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``os.kill`` raising :class:`PermissionError` → ``pid_alive=True``.

    EPERM proves the process exists (the kernel checked process
    existence before refusing the signal), so per the spec we treat it
    as positive evidence that the pid is alive.
    """
    popolad = _register(
        tmp_path,
        task_id="t-eperm",
        pid=99999,
        state=TaskState.RUNNING,
        runtime="local",
    )

    captured: dict[str, Any] = {}

    def fake_kill(pid: int, sig: int) -> None:
        captured["pid"] = pid
        captured["sig"] = sig
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(
        "popolaloom.daemon.server.os.kill",
        fake_kill,
    )

    summary = popolad.get_status("t-eperm")
    assert captured["pid"] == 99999, "probe must call os.kill with the handle's pid"
    assert captured["sig"] == 0, "probe must send signal 0 (existence check only)"
    assert summary["pid_alive"] is True


# ── extra: alive pid (success path) → pid_alive=True ──────────────────────


def test_local_running_with_alive_pid_reports_pid_alive_true(
    tmp_path: Path,
) -> None:
    """Live pid (the test process) ⇒ ``pid_alive=True``, no WARN log.

    Documents the happy-path branch that produced no signal during the
    monkeypatched test above; here we cover the real ``os.kill(pid, 0)``
    success path with a guaranteed-alive pid.
    """
    popolad = _register(
        tmp_path,
        task_id="t-alive",
        pid=os.getpid(),
        state=TaskState.RUNNING,
        runtime="local",
    )
    summary = popolad.get_status("t-alive")
    assert summary["pid_alive"] is True
