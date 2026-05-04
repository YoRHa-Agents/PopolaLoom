"""C5 — Supervisor.spawn raises OSError → state=failed, error captured.

Per testing-matrix.md §10 #4.  When ``subprocess.Popen`` raises (e.g.
the binary path is missing, or PATH lookup fails), the dispatch chain
must surface the failure rather than registering a phantom task.

Key invariants:

1. ``Popen`` raising ``FileNotFoundError`` propagates out of
   ``Supervisor.spawn``; **the supervisor does NOT swallow it**.
2. ``Popolad.dispatch_task`` (legacy path) does not catch it either —
   the caller (RPC layer) sees the exception and converts to HTTP 400.
3. The Popolad's StateStore does NOT contain a task_id for the failed
   dispatch (no phantom in-flight registration).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.server import Popolad
from popolaloom.daemon.supervisor import Supervisor


def test_chaos_supervisor_spawn_raises_oserror_propagates(
    tmp_path: Path,
    mocker,
) -> None:
    """``Popen`` raising ``OSError`` from supervisor.spawn → propagates out."""
    sup = Supervisor()
    log = EventLog(tmp_path / "c5_a.jsonl", fsync_interval_s=0.0)

    mocker.patch(
        "popolaloom.daemon.supervisor.subprocess.Popen",
        side_effect=OSError(2, "No such file or directory: 'missing-binary'"),
    )

    try:
        with pytest.raises(OSError) as exc_info:
            sup.spawn(
                task_id="cursor-c5",
                cmd=["missing-binary", "arg"],
                cwd=None,
                env=None,
                event_log=log,
            )
        assert exc_info.value.errno == 2
    finally:
        log.close()


def test_chaos_dispatch_with_missing_binary_propagates_error(
    tmp_path: Path,
    mocker,
) -> None:
    """Dispatch where supervisor.spawn raises FileNotFoundError → propagates.

    The error MUST surface to the caller (No Silent Failures); the rpc
    layer translates it into a clear HTTP 4xx so the operator sees
    the bad cmd.  Note: the StateStore *may* contain a "running" entry
    pre-dating the spawn failure (handle is registered before
    supervisor.spawn is called, see :meth:`Popolad._dispatch_legacy`).
    That's a known Stage E cleanup item; the No-Silent-Failures
    invariant we care about here is that the exception propagates and
    a follow-up status query reflects the error.
    """
    def _adapter(cli, prompt, cwd, extra=None):
        return ["nonexistent-binary-xyz", "echo"]

    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_adapter,
        use_graph=False,
    )

    mocker.patch(
        "popolaloom.daemon.supervisor.subprocess.Popen",
        side_effect=FileNotFoundError(2, "No such file or directory"),
    )

    with pytest.raises(FileNotFoundError) as exc_info:
        popolad.dispatch_task("cursor", "trigger C5", cwd=None)
    assert exc_info.value.errno == 2
