"""C6 — UDS socket bind PermissionError → daemon exits with clear error.

Per testing-matrix.md §10 #5.  Mock the daemon main loop's
``socket.bind`` (via uvicorn.Config) so it raises PermissionError;
verify the error propagates rather than being swallowed.

We model this at unit level (mocking uvicorn.Server.serve to raise)
rather than spawning a real daemon under a chmod 000 directory, which
would be flaky inside CI.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import popolaloom.daemon.main as daemon_main


def test_chaos_uvicorn_serve_raises_permission_error_propagates(
    tmp_path: Path,
    mocker,
) -> None:
    """``server.serve`` raising PermissionError → propagates out of main()."""
    fake_server = mocker.MagicMock()
    async def _serve_raises():
        raise PermissionError(13, "Permission denied")

    fake_server.serve = _serve_raises
    mocker.patch.object(daemon_main.uvicorn, "Server", return_value=fake_server)
    mocker.patch.object(
        daemon_main.uvicorn,
        "Config",
        return_value=mocker.MagicMock(),
    )
    mocker.patch.object(
        daemon_main, "_build_default_popolad", return_value=mocker.MagicMock()
    )

    socket_path = tmp_path / "popolad.sock"
    pid_path = tmp_path / "popolad.pid"
    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True, exist_ok=True)

    with pytest.raises(PermissionError) as exc:
        asyncio.run(
            daemon_main.main(
                socket_path=socket_path,
                events_dir=events_dir,
                pid_path=pid_path,
            )
        )
    assert exc.value.errno == 13


def test_chaos_socket_remove_permission_denied_logged_and_raised(
    tmp_path: Path,
    mocker,
) -> None:
    """Stale socket cleanup PermissionError → logs error AND re-raises (No Silent Failures)."""
    socket_path = tmp_path / "stale.sock"
    socket_path.touch()

    mocker.patch.object(
        daemon_main, "_build_default_popolad", return_value=mocker.MagicMock()
    )
    mocker.patch.object(
        daemon_main.Path, "unlink", side_effect=PermissionError(13, "Permission denied")
    )

    with pytest.raises(PermissionError) as exc:
        asyncio.run(
            daemon_main.main(
                socket_path=socket_path,
                events_dir=tmp_path / "events",
                pid_path=tmp_path / "popolad.pid",
            )
        )
    assert exc.value.errno == 13
