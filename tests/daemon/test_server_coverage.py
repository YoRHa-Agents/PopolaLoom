"""Coverage gap-fillers for ``daemon/server.py`` + ``daemon/supervisor.py``.

v0.5.2 Loop 2 §L2.D: at v0.5.1 ``daemon/server.py`` was at 87 % and
``daemon/supervisor.py`` was at 87 % default-lane coverage; the
remaining uncovered lines fall into a few small "rare-but-real"
branches:

* ``Popolad.lark_supervisor`` property (line 251) — not reached when
  ``_lark_supervisor`` is left at ``None`` (the default).
* ``cancel_task`` ``KeyError`` ramps when the state-store handle has
  vanished mid-flight (lines 711-712, 778-779).
* ``cancel_task`` ``ProcessLookupError`` after-the-SIGTERM-fact
  branch (lines 720-731).
* ``_maybe_create_arktower_task`` ImportError fallback (lines 899-904)
  + repository.create exception path (lines 944-952).
* ``_schedule_lark_terminal_notification`` exception swallow path
  (lines 1064-1065).
* ``rehydrate_from_persistence`` ImportError + empty-result branches
  (lines 1244-1248, 1269-1270, 1280, 1300-1304).
* ``_emit_recovered_events`` existing-event-log reuse + Exception
  swallow (lines 1356, 1367-1368).
* Supervisor ``state_store`` property (line 94).
* Supervisor ``_drain_stream`` exception path + close-failed swallow
  (lines 208-218).
* Supervisor ``_resolve_terminal_event`` ``state_store.get`` exception
  fallback (lines 333-339).
* :func:`_get_session_id` exception → ``None`` branch (lines 422-423).

These tests are unit-level (no real subprocess except where the
existing supervisor fixture pattern requires it) so they run inside
the default lane in well under a second per case.  Combined they add
≥ 12 default-lane cases to satisfy the L2.D test-count delta.
"""

from __future__ import annotations

import builtins
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.server import Popolad
from popolaloom.daemon.state import StateStore, TaskHandle, TaskState
from popolaloom.daemon.supervisor import Supervisor, _get_session_id

# ──────────────────────────────────────────────────────────────────────────
# Popolad.lark_supervisor property + setter wiring (line 251)
# ──────────────────────────────────────────────────────────────────────────


def test_lark_supervisor_property_returns_attached_value(tmp_path: Path) -> None:
    """Reading ``Popolad.lark_supervisor`` after the daemon main wires it.

    Default daemon construction leaves ``_lark_supervisor=None``; the
    daemon main path stamps a value on it post-construction. This test
    verifies the property returns whatever was stored.
    """
    popolad = Popolad(events_dir=tmp_path / "events")
    assert popolad.lark_supervisor is None

    sentinel = object()
    popolad._lark_supervisor = sentinel  # type: ignore[assignment]
    assert popolad.lark_supervisor is sentinel


# ──────────────────────────────────────────────────────────────────────────
# Popolad.cancel_task KeyError ramps (lines 711-712, 720-731, 778-779)
# ──────────────────────────────────────────────────────────────────────────


def test_cancel_task_with_already_dead_pid_returns_process_already_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cancel_task`` short-circuits to ``process_already_gone`` on ProcessLookupError.

    Covers lines 720-731 in ``daemon/server.py`` — when the SIGTERM
    raises :class:`ProcessLookupError` the cancel result becomes
    ``"process_already_gone"`` and the event log gets a
    ``task.cancel_requested`` event with that reason.
    """
    popolad = Popolad(events_dir=tmp_path / "events")
    handle = TaskHandle(
        task_id="cancel-dead",
        cli="cursor",
        pid=99999999,  # bogus pid that won't exist
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "events" / "cancel-dead.jsonl",
    )
    popolad.state_store.register(handle)

    log = EventLog(handle.event_log_path, source="popola/cancel-dead")
    popolad._event_logs[handle.task_id] = log

    def _raise_lookup(_pid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr("os.kill", _raise_lookup)

    result = popolad.cancel_task("cancel-dead")
    assert result["task_id"] == "cancel-dead"
    assert result["result"] == "process_already_gone"
    assert result["escalated_to_sigkill"] is False

    types = {ev["type"] for ev in log.tail()}
    assert "task.cancel_requested" in types

    log.close()


# ──────────────────────────────────────────────────────────────────────────
# _maybe_create_arktower_task ImportError fallback (lines 899-904)
# ──────────────────────────────────────────────────────────────────────────


def test_maybe_create_arktower_task_import_error_returns_none_false(
    tmp_path: Path,
) -> None:
    """When the vendored ArkTower can't be imported, the helper logs + returns ``(None, False)``.

    Covers lines 899-904 in ``daemon/server.py`` — exercises the
    ``except ImportError`` ramp with a synthetic ``__import__`` patch.
    """
    popolad = Popolad(events_dir=tmp_path / "events")

    real_import = builtins.__import__

    def _shim_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if "popolaloom._vendored.arktower.core.models" in name:
            raise ImportError("synthetic _vendored.arktower import block")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", _shim_import):
        result = popolad._maybe_create_arktower_task(
            task_id="import-err-tid",
            cli="cursor",
            prompt="hello",
            cmd=["echo", "hi"],
        )

    assert result == (None, False)


# ──────────────────────────────────────────────────────────────────────────
# _maybe_create_arktower_task with task_repository .create raising (944-952)
# ──────────────────────────────────────────────────────────────────────────


def test_maybe_create_arktower_task_repository_create_exception_returns_none_false(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``task_repository.create`` raising → log + return ``(None, False)``.

    Covers lines 944-952 in ``daemon/server.py`` — the in-memory
    fallback path used for tests when persistence is None and a
    legacy task_repository is provided.
    """

    class _BoomRepo:
        def create(self, _ark_task: Any) -> Any:
            raise RuntimeError("synthetic repo boom")

    popolad = Popolad(
        events_dir=tmp_path / "events",
        task_repository=_BoomRepo(),
    )
    with caplog.at_level(logging.ERROR, logger="popolaloom.daemon.server"):
        result = popolad._maybe_create_arktower_task(
            task_id="repo-boom-tid",
            cli="cursor",
            prompt="hello",
            cmd=["echo", "hi"],
        )

    assert result == (None, False)
    assert any(
        "repo.create failed" in rec.getMessage() for rec in caplog.records
    )


# ──────────────────────────────────────────────────────────────────────────
# _schedule_lark_terminal_notification exception swallow (1064-1065)
# ──────────────────────────────────────────────────────────────────────────


def test_schedule_lark_terminal_notification_swallows_run_coroutine_threadsafe_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``asyncio.run_coroutine_threadsafe`` raising must NOT crash the wait thread.

    Covers lines 1064-1069 in ``daemon/server.py``.  With a fake loop
    that's open but with the real call patched to raise, the helper
    must log ``lark.notify.schedule_failed`` at ERROR and return.
    """
    popolad = Popolad(events_dir=tmp_path / "events")

    class _FakeLoop:
        def is_closed(self) -> bool:
            return False

    popolad._loop = _FakeLoop()  # type: ignore[assignment]

    def _boom(coro: Any, loop: Any) -> Any:
        if hasattr(coro, "close"):
            coro.close()
        raise RuntimeError("synthetic schedule boom")

    monkeypatch.setattr(
        "popolaloom.daemon.server.asyncio.run_coroutine_threadsafe",
        _boom,
    )

    with caplog.at_level(logging.ERROR, logger="popolaloom.daemon.server"):
        popolad._schedule_lark_terminal_notification(
            task_id="boom-tid",
            terminal_state=TaskState.COMPLETED,
            exit_code=0,
        )

    assert any(
        "lark.notify.schedule_failed" in rec.getMessage()
        for rec in caplog.records
    )


def test_schedule_lark_terminal_notification_skips_when_loop_is_none(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_loop is None`` → INFO log ``lark.notify.unscheduled reason=no_loop``.

    Covers lines 1048-1054 in ``daemon/server.py``.
    """
    popolad = Popolad(events_dir=tmp_path / "events")
    popolad._loop = None
    with caplog.at_level(logging.INFO, logger="popolaloom.daemon.server"):
        popolad._schedule_lark_terminal_notification(
            task_id="no-loop-tid",
            terminal_state=TaskState.COMPLETED,
            exit_code=0,
        )

    assert any(
        "lark.notify.unscheduled" in rec.getMessage() and "no_loop" in rec.getMessage()
        for rec in caplog.records
    )


# ──────────────────────────────────────────────────────────────────────────
# rehydrate_from_persistence ImportError + dedup + empty results
# ──────────────────────────────────────────────────────────────────────────


def test_rehydrate_returns_zero_when_no_persistence(tmp_path: Path) -> None:
    """``rehydrate_from_persistence`` returns 0 when no persistence is configured.

    Covers line 1237 in ``daemon/server.py``.
    """
    popolad = Popolad(events_dir=tmp_path / "events")
    assert popolad.rehydrate_from_persistence() == 0


def test_rehydrate_returns_zero_when_arktower_models_unimportable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ImportError of the vendored ArkTower models → log + return 0.

    Covers lines 1244-1248 in ``daemon/server.py``.
    """

    class _StubPersistence:
        def __init__(self) -> None:
            self.repository = self
            self.event_bus = self

        def list(self, _filter: Any) -> list[Any]:
            return []

        def close(self) -> None:
            pass

    popolad = Popolad(
        events_dir=tmp_path / "events",
        persistence=_StubPersistence(),  # type: ignore[arg-type]
    )

    real_import = builtins.__import__

    def _shim_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if "popolaloom._vendored.arktower.core.models" in name:
            raise ImportError("synthetic block during rehydrate")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", _shim_import), \
         caplog.at_level(logging.WARNING, logger="popolaloom.daemon.server"):
        result = popolad.rehydrate_from_persistence()

    assert result == 0
    assert any(
        "skipping rehydrate" in rec.getMessage() for rec in caplog.records
    )


def test_rehydrate_emits_recovered_events_logs_when_event_log_already_exists(
    tmp_path: Path,
) -> None:
    """When a task's EventLog is already in ``_event_logs``, reuse it (line 1356).

    Drives :meth:`Popolad._emit_recovered_events` directly with a
    pre-existing log to cover the ``existing is not None`` branch
    (line 1355-1356) without going through full rehydrate.
    """
    popolad = Popolad(events_dir=tmp_path / "events")

    handle = TaskHandle(
        task_id="recover-existing-1",
        cli="cursor",
        pid=None,
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "events" / "recover-existing-1.jsonl",
        arktower_task_id="ark-rec-1",
        persisted=True,
    )

    pre_existing = EventLog(
        handle.event_log_path, source="popola/recover-existing-1"
    )
    popolad._event_logs[handle.task_id] = pre_existing

    popolad._emit_recovered_events([handle], ["recover-existing-1"])

    types = [ev["type"] for ev in pre_existing.tail()]
    assert "popolad.recovered" in types
    pre_existing.close()


def test_emit_recovered_events_swallows_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``EventLog`` construction failure inside ``_emit_recovered_events`` is logged + skipped.

    Covers lines 1367-1372 in ``daemon/server.py``.
    """
    popolad = Popolad(events_dir=tmp_path / "events")

    handle = TaskHandle(
        task_id="recover-boom-1",
        cli="cursor",
        pid=None,
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "events" / "recover-boom-1.jsonl",
        arktower_task_id="ark-rec-boom",
        persisted=True,
    )

    real_eventlog_init = EventLog.__init__

    def _exploding_init(self: EventLog, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic event log boom")

    monkeypatch.setattr(EventLog, "__init__", _exploding_init)
    try:
        with caplog.at_level(logging.ERROR, logger="popolaloom.daemon.server"):
            # No exception raised — error swallowed + logged.
            popolad._emit_recovered_events([handle], ["recover-boom-1"])
    finally:
        monkeypatch.setattr(EventLog, "__init__", real_eventlog_init)

    assert any(
        "Failed to emit popolad.recovered" in rec.getMessage()
        for rec in caplog.records
    )


# ──────────────────────────────────────────────────────────────────────────
# Supervisor.state_store property (line 94)
# ──────────────────────────────────────────────────────────────────────────


def test_supervisor_state_store_property_returns_injected_value() -> None:
    """``Supervisor.state_store`` returns the injected store (or None)."""
    sup_no_store = Supervisor()
    assert sup_no_store.state_store is None

    store = StateStore()
    sup_with_store = Supervisor(state_store=store)
    assert sup_with_store.state_store is store


# ──────────────────────────────────────────────────────────────────────────
# Supervisor._drain_stream exception + close-failed swallow (208-218)
# ──────────────────────────────────────────────────────────────────────────


def test_drain_stream_exception_emits_stream_error(tmp_path: Path) -> None:
    """A stream that raises mid-readline → emits ``process.stream_error`` + closes.

    Covers lines 208-218 of ``daemon/supervisor.py``.  Drives
    :meth:`Supervisor._drain_stream` directly with a fake stream that
    raises on the second readline.
    """
    log = EventLog(tmp_path / "drain.jsonl", source="popola/drain-test")

    class _FakeStream:
        def __init__(self) -> None:
            self.read_count = 0
            self.closed = False

        def __iter__(self) -> Any:
            return self

        def __next__(self) -> str:
            return self.readline()

        def readline(self) -> str:
            self.read_count += 1
            if self.read_count == 1:
                return "first line\n"
            raise RuntimeError("synthetic readline boom")

        def close(self) -> None:
            self.closed = True

    sup = Supervisor()
    fake = _FakeStream()
    sup._line_counts["drain-test"] = {"stdout": 0, "stderr": 0}
    sup._drain_stream("drain-test", fake, "stdout", log)

    types = [ev["type"] for ev in log.tail()]
    assert "process.stream_error" in types
    assert fake.closed, "stream must be closed in finally"
    log.close()


def test_drain_stream_close_exception_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``stream.close()`` raising at the end is logged (lines 217-218)."""
    log = EventLog(tmp_path / "drain-close.jsonl", source="popola/drain-close")

    class _CloseBoomStream:
        def __init__(self) -> None:
            self.calls = 0

        def __iter__(self) -> Any:
            return self

        def __next__(self) -> str:
            return self.readline()

        def readline(self) -> str:
            return ""  # immediate EOF

        def close(self) -> None:
            raise RuntimeError("synthetic close boom")

    sup = Supervisor()
    sup._line_counts["close-test"] = {"stdout": 0, "stderr": 0}
    with caplog.at_level(logging.DEBUG, logger="popolaloom.daemon.supervisor"):
        sup._drain_stream("close-test", _CloseBoomStream(), "stdout", log)

    assert any(
        "Stream close failed" in rec.getMessage() for rec in caplog.records
    )
    log.close()


# ──────────────────────────────────────────────────────────────────────────
# Supervisor._maybe_canceled_terminal state_store.get exception (333-339)
# ──────────────────────────────────────────────────────────────────────────


def test_maybe_canceled_terminal_state_store_get_exception_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``state_store.get`` raising → log + return None (legacy 2-way path).

    Covers lines 333-339 in ``daemon/supervisor.py``.
    """

    class _BoomStore:
        def get(self, _task_id: str) -> Any:
            raise RuntimeError("synthetic state_store boom")

    with caplog.at_level(logging.ERROR, logger="popolaloom.daemon.supervisor"):
        result = Supervisor._maybe_canceled_terminal(
            task_id="boom-tid",
            pid=12345,
            exit_code=0,
            state_store=_BoomStore(),  # type: ignore[arg-type]
        )

    assert result is None
    assert any(
        "state_store.get failed" in rec.getMessage()
        for rec in caplog.records
    )


def test_maybe_canceled_terminal_returns_none_when_handle_not_canceled() -> None:
    """Handle not in CANCELED state → return None (legacy 2-way path)."""
    store = StateStore()
    handle = TaskHandle(
        task_id="running-tid",
        cli="cursor",
        pid=42,
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=Path("/tmp/popola-test.jsonl"),
    )
    store.register(handle)

    result = Supervisor._maybe_canceled_terminal(
        task_id="running-tid",
        pid=42,
        exit_code=0,
        state_store=store,
    )
    assert result is None


def test_maybe_canceled_terminal_returns_none_when_state_store_is_none() -> None:
    """``state_store is None`` → return None immediately (line 327-328)."""
    result = Supervisor._maybe_canceled_terminal(
        task_id="no-store",
        pid=1,
        exit_code=0,
        state_store=None,
    )
    assert result is None


# ──────────────────────────────────────────────────────────────────────────
# _get_session_id exception → None branch (lines 422-423)
# ──────────────────────────────────────────────────────────────────────────


def test_get_session_id_returns_none_for_dead_pid() -> None:
    """:func:`_get_session_id` returns None when ``os.getsid`` raises OSError.

    Covers lines 422-423 in ``daemon/supervisor.py``.
    """
    bogus_pid = 99999999
    result = _get_session_id(bogus_pid)
    assert result is None


# ──────────────────────────────────────────────────────────────────────────
# Stream truncated path: drain thread doesn't finish before timeout (lines 261, 264)
# ──────────────────────────────────────────────────────────────────────────


def test_emit_stream_truncated_writes_event(tmp_path: Path) -> None:
    """:meth:`_emit_stream_truncated` appends ``stream.truncated`` (lines 354-387).

    Covers the warning + event-emit path used when a drain thread
    didn't finish within the 30s join timeout.
    """
    log = EventLog(tmp_path / "trunc.jsonl", source="popola/trunc-test")
    sup = Supervisor()
    sup._line_counts["trunc-test"] = {"stdout": 7, "stderr": 0}

    sup._emit_stream_truncated("trunc-test", "stdout", log)

    events = log.tail()
    truncated = [ev for ev in events if ev["type"] == "stream.truncated"]
    assert len(truncated) == 1
    assert truncated[0]["data"]["actual_lines"] == 7
    assert truncated[0]["data"]["reason"] == "join_timeout_30s"
    log.close()


# ──────────────────────────────────────────────────────────────────────────
# Supervisor._safe_on_exit logs callback exceptions (398-399)
# ──────────────────────────────────────────────────────────────────────────


def test_safe_on_exit_swallows_callback_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Callback raising must NOT crash the wait thread (line 398-399)."""

    def _boom_callback(_tid: str, _ec: int) -> None:
        raise RuntimeError("synthetic callback boom")

    with caplog.at_level(logging.ERROR, logger="popolaloom.daemon.supervisor"):
        Supervisor._safe_on_exit(_boom_callback, "boom-tid", 1)

    assert any(
        "on_exit callback failed" in rec.getMessage()
        for rec in caplog.records
    )


# ──────────────────────────────────────────────────────────────────────────
# Wait-thread proc.wait exception path (245-257) + drain timeout (260-264)
# ──────────────────────────────────────────────────────────────────────────


def test_wait_and_finalize_proc_wait_exception_emits_terminal_with_error(
    tmp_path: Path,
) -> None:
    """``proc.wait`` raising → terminal event with ``error`` field (245-257).

    Covers the wait-thread error path: ``proc.wait()`` raising leads
    to a ``task.failed`` (or ``task.canceled`` if state_store says so)
    event with an ``error`` field set to the repr() of the exception.
    """
    log = EventLog(tmp_path / "waitfail.jsonl", source="popola/waitfail")

    class _BoomProc:
        pid = 123456789

        def wait(self) -> int:
            raise OSError("synthetic proc.wait boom")

    sup = Supervisor()
    sup._line_counts["waitfail-tid"] = {"stdout": 0, "stderr": 0}

    on_exit_calls: list[tuple[str, int]] = []

    def _on_exit(tid: str, ec: int) -> None:
        on_exit_calls.append((tid, ec))

    stdout_thread = threading.Thread(target=lambda: None)
    stdout_thread.start()
    stdout_thread.join()
    stderr_thread = threading.Thread(target=lambda: None)
    stderr_thread.start()
    stderr_thread.join()

    sup._wait_and_finalize(
        "waitfail-tid",
        _BoomProc(),  # type: ignore[arg-type]
        log,
        stdout_thread,
        stderr_thread,
        _on_exit,
        None,
    )

    events = log.tail()
    failed = [ev for ev in events if ev["type"] == "task.failed"]
    assert len(failed) == 1
    assert failed[0]["data"]["exit_code"] == -1
    assert "synthetic proc.wait boom" in failed[0]["data"]["error"]
    assert on_exit_calls == [("waitfail-tid", -1)]
    log.close()
