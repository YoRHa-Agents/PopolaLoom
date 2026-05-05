"""v0.4.1 Stage L2.B — end-to-end Lark terminal notification tests.

Per the L2 task spec (~ 2 cases):

(a) Dispatch a fake echo task via :meth:`Popolad.dispatch_task`, let it
    complete, assert :func:`send_terminal_notification` was scheduled
    on the loop and produced a ``lark.send.ok`` NDJSON envelope.

(b) Cancel a long-running dispatch before exit, assert the canceled
    card path fired (NOT the failure card path) — protects the L2.B
    state-clobber fix in :meth:`Popolad._on_subprocess_exit`.

These tests construct a real :class:`Popolad` (no full daemon main, no
uvicorn) inside an asyncio loop so the wait-thread → loop scheduling
path mirrors the production daemon. ``lark-cli`` is replaced with the
:func:`subprocess.run` test seam; ``is_lark_runtime_available`` is
forced ``True`` via :func:`unittest.mock.patch`.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from popolaloom.daemon.server import Popolad
from popolaloom.daemon.state import TaskState


def _echo_adapter(
    cli: str, prompt: str, cwd: Any, extra: dict[str, Any] | None
) -> list[str]:
    """4-arg adapter that runs the prompt through ``python -c print(...)``."""
    return [sys.executable, "-c", f"print({prompt!r})"]


def _sleep_adapter(
    cli: str, prompt: str, cwd: Any, extra: dict[str, Any] | None
) -> list[str]:
    """4-arg adapter that runs ``python -c "import time; time.sleep(60)"``."""
    return [sys.executable, "-c", "import time; time.sleep(60)"]


class _StubCompletedProcess:
    def __init__(
        self, returncode: int = 0, stdout: str = "", stderr: str = ""
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _wait_until(predicate: Any, *, timeout_s: float = 5.0) -> bool:
    """Poll ``predicate()`` until True or timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _read_events(popolad: Popolad, task_id: str) -> list[dict[str, Any]]:
    """Flush + read the NDJSON envelopes for ``task_id`` (in-process safe).

    The popolad daemon owns a buffered :class:`EventLog` per task; the
    background fsync worker only flushes every ``fsync_interval_s`` so
    tests that read immediately after a write must force a flush first.
    """
    log = popolad.event_log(task_id)
    if log is None:
        return []
    log.fsync()
    return log.tail()


# ── (a) end-to-end COMPLETED happy path ─────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_completion_triggers_lark_terminal_notify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full dispatch → exit cycle schedules + sends a Lark completion card.

    This is the headline acceptance test for L2.B: the supervisor
    wait-thread fires :meth:`Popolad._on_subprocess_exit`, which in
    turn schedules :func:`send_terminal_notification` on the daemon
    loop; the notifier picks the ``build_completion_card`` builder
    and calls ``send_lark_card`` (stubbed to capture argv).
    """
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_e2e_completed")
    monkeypatch.setenv("LARK_NOTIFY_ON_COMPLETED", "1")
    monkeypatch.setenv("POPOLA_USE_GRAPH", "0")  # legacy path, simpler for e2e

    captured_argv: list[list[str]] = []

    def stub_run(argv: list[str], **_kw: Any) -> _StubCompletedProcess:
        captured_argv.append(list(argv))
        return _StubCompletedProcess(
            returncode=0, stdout='{"message_id": "om_e2e_done"}'
        )

    popolad = Popolad(events_dir=tmp_path, adapter=_echo_adapter)
    popolad.attach_loop(asyncio.get_running_loop())

    with patch("popolaloom.lark.notifier.is_lark_runtime_available", return_value=True), \
         patch("popolaloom.hitl.renderers.lark.subprocess.run", stub_run):
        task_id = popolad.dispatch_task(
            cli="cursor", prompt="hello e2e completion"
        )
        # Wait for the wait-thread to flip state + schedule the notifier
        assert _wait_until(
            lambda: (
                popolad._state.get(task_id) is not None
                and popolad._state.get(task_id).state == TaskState.COMPLETED
            ),
            timeout_s=5.0,
        ), "task did not transition to COMPLETED in time"
        # Yield so the run_coroutine_threadsafe future actually runs.
        for _ in range(40):
            await asyncio.sleep(0.05)
            if captured_argv:
                break

    assert captured_argv, (
        "expected lark-cli to be invoked once on COMPLETED; got 0 calls"
    )
    argv = captured_argv[0]
    assert argv[0:3] == ["lark-cli", "im", "+send"]
    assert "ou_e2e_completed" in argv
    metadata = argv[argv.index("--metadata-key") + 1]
    assert metadata == f"task_id={task_id}"

    card = json.loads(argv[argv.index("--card") + 1])
    assert card["header"]["template"] == "green"
    assert "任务完成" in card["header"]["title"]["content"]

    events = _read_events(popolad, task_id)
    types = [e["type"] for e in events]
    assert "task.completed" in types
    assert "lark.send.ok" in types, (
        f"expected lark.send.ok NDJSON envelope; got types={types}"
    )
    lark_event = next(e for e in events if e["type"] == "lark.send.ok")
    assert lark_event["data"]["kind"] == "terminal", (
        "kind=terminal must appear in the NDJSON envelope (L2.D)"
    )
    assert lark_event["data"]["target"] == "ou_e2e_completed"


# ── (b) end-to-end CANCEL path → canceled card (NOT failure card) ───────


@pytest.mark.asyncio
async def test_cancel_before_exit_renders_canceled_card_not_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel during run → wait-thread emits canceled, notifier renders canceled.

    Protects the L2.B state-clobber fix: ``_on_subprocess_exit`` must
    NOT overwrite ``state=CANCELED`` with ``state=FAILED`` when the
    SIGTERM-killed subprocess returns a non-zero exit code (-15).
    The notifier should pick the yellow ``build_canceled_card`` builder,
    NOT the red ``build_failure_card`` builder.
    """
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_e2e_canceled")
    monkeypatch.setenv("LARK_NOTIFY_ON_CANCELED", "1")
    monkeypatch.setenv("LARK_NOTIFY_ON_FAILED", "1")  # prove it doesn't fire
    monkeypatch.setenv("POPOLA_USE_GRAPH", "0")

    captured_argv: list[list[str]] = []

    def stub_run(argv: list[str], **_kw: Any) -> _StubCompletedProcess:
        captured_argv.append(list(argv))
        return _StubCompletedProcess(
            returncode=0, stdout='{"message_id": "om_e2e_cancel"}'
        )

    popolad = Popolad(events_dir=tmp_path, adapter=_sleep_adapter)
    popolad.attach_loop(asyncio.get_running_loop())

    with patch("popolaloom.lark.notifier.is_lark_runtime_available", return_value=True), \
         patch("popolaloom.hitl.renderers.lark.subprocess.run", stub_run):
        task_id = popolad.dispatch_task(
            cli="claude", prompt="long-running cancel target"
        )
        # Give the subprocess a beat to be alive + register pid
        assert _wait_until(
            lambda: (popolad._state.get(task_id) is not None
                     and popolad._state.get(task_id).pid is not None),
            timeout_s=2.0,
        ), "subprocess did not register pid in time"

        await asyncio.to_thread(popolad.cancel_task, task_id, sigterm_grace_s=2.0)

        assert _wait_until(
            lambda: (
                popolad._state.get(task_id) is not None
                and popolad._state.get(task_id).state == TaskState.CANCELED
            ),
            timeout_s=5.0,
        ), "task did not transition to CANCELED after cancel_task"

        for _ in range(40):
            await asyncio.sleep(0.05)
            if captured_argv:
                break

    handle = popolad._state.get(task_id)
    assert handle is not None
    assert handle.state == TaskState.CANCELED, (
        f"L2.B state-clobber fix regressed: state={handle.state} after cancel"
    )

    assert captured_argv, "expected lark-cli to be invoked on CANCELED"
    card = json.loads(captured_argv[0][captured_argv[0].index("--card") + 1])
    assert card["header"]["template"] == "yellow", (
        f"expected canceled card (yellow), got template={card['header']['template']}"
    )
    assert "任务已取消" in card["header"]["title"]["content"]
    assert "任务失败" not in card["header"]["title"]["content"]

    events = _read_events(popolad, task_id)
    types = [e["type"] for e in events]
    assert "task.canceled" in types, (
        f"supervisor must emit task.canceled (L1 fix); got: {types}"
    )
    assert "task.failed" not in types
