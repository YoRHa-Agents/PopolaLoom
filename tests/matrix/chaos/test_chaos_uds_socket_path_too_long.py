"""C7 — UDS socket path length > sun_path limit → clear error.

AF_UNIX socket paths cap at ``sizeof(sun_path)`` bytes, which is 108
on Linux (kernel constant ``UNIX_PATH_MAX``).  When a user passes a
``$POPOLA_HOME`` path long enough that ``$POPOLA_HOME/popolad.sock``
exceeds 108 chars, the bind fails with ``OSError(36, "File name too
long")`` or similar.

This unit test verifies the error surfaces (No Silent Failures) by
mocking the uvicorn Server.serve to raise the equivalent OSError.

We could alternatively spawn a real daemon under a deeply-nested
tmp_path, but that requires nesting > 100 chars of directory names
which would clutter the test container's filesystem and is flaky
across kernel implementations of UNIX_PATH_MAX.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import popolaloom.daemon.main as daemon_main


def test_chaos_socket_path_oserror_propagates_long_path(
    tmp_path: Path,
    mocker,
) -> None:
    """``serve`` raising OSError(36) ``File name too long`` propagates."""
    fake_server = mocker.MagicMock()

    async def _serve_raises():
        raise OSError(36, "File name too long")

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

    long_dir = tmp_path / ("X" * 60) / ("Y" * 60)
    long_dir.mkdir(parents=True, exist_ok=True)
    socket_path = long_dir / "popolad.sock"
    pid_path = long_dir / "popolad.pid"
    events_dir = long_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)

    with pytest.raises(OSError) as exc_info:
        asyncio.run(
            daemon_main.main(
                socket_path=socket_path,
                events_dir=events_dir,
                pid_path=pid_path,
            )
        )
    assert exc_info.value.errno == 36
    assert "too long" in str(exc_info.value).lower()


def test_chaos_long_path_string_length_check(tmp_path: Path) -> None:
    """Sanity: sufficiently long paths exceed AF_UNIX `sun_path` limit (~108)."""
    long_dir = tmp_path / ("X" * 60) / ("Y" * 60)
    socket_path = long_dir / "popolad.sock"
    assert len(str(socket_path)) > 100, (
        "test setup error: synthetic path should exceed 100 chars to model the "
        f"AF_UNIX limit edge case; got len={len(str(socket_path))}"
    )
