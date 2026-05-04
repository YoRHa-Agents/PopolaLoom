r"""Tests for popola CLI v0.2.0 httpx UDS client (Stage A A4 + A6 patches).

Coverage targets (≥ 4 cases per the v0.2.0 plan):

1. CLI ``dispatch`` → daemon UDS RPC end-to-end (real httpx + uvicorn).
2. CLI ``--cli-flag KEY=VAL`` parsing and pass-through to adapter (R-012).
3. CLI ``--events-dir`` flag accepted by dispatch (R-014 part).
4. Daemon-down handling: ``popola dispatch`` with no socket → friendly error
   "popolad not running, run \`popola popolad start\` to start it" + exit 1.

Plus bonus:

5. ``popola popolad --help`` lists ``start / stop / status`` subcommands.
6. ``popola attach`` defaults to ``--follow=True`` (R-005 fix).
7. ``popola list-cli`` renders without Rich markup leak (R-014 part).

Implementation note: tests #1, #2, #3 use the same uvicorn-in-thread fixture
pattern as test_e2e.py (in-process daemon for fast iteration). Test #4 uses
a non-existent socket path to trigger ``httpx.ConnectError``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from typer.testing import CliRunner

from popolaloom.adapters import base as adapter_base
from popolaloom.adapters import build_command, register_adapter
from popolaloom.cli import main as cli_main
from popolaloom.cli.main import _parse_cli_flags
from popolaloom.daemon import Popolad
from popolaloom.daemon.rpc import create_app

# ── shared fixtures ──────────────────────────────────────────────────────


class _CliFlagsAdapter:
    """Echo adapter that captures the ``extra`` kwarg into a sidecar file.

    Lets test_dispatch_cli_flag_passes_extra verify the entire pipeline:
    CLI parses ``--cli-flag yolo=true`` → daemon receives ``{yolo: True}`` →
    adapter sees it.
    """

    name = "echo_extras"
    binary = sys.executable
    captured_extra: list[dict[str, Any]] = []

    def build_command(
        self,
        prompt: str,
        cwd: Path | None = None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        type(self).captured_extra.append(dict(extra) if extra else {})
        return [
            sys.executable,
            "-c",
            f"print('echo:', {prompt!r}); import sys; sys.exit(0)",
        ]

    def is_available(self) -> bool:
        return True


@pytest.fixture
def isolated_registry() -> Iterator[None]:
    saved = dict(adapter_base._REGISTRY)
    _CliFlagsAdapter.captured_extra.clear()
    try:
        yield
    finally:
        adapter_base._REGISTRY.clear()
        adapter_base._REGISTRY.update(saved)


@pytest.fixture
def daemon_uds(
    tmp_path: Path,
    isolated_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, _CliFlagsAdapter]]:
    """Start an in-thread popolad UDS server with the echo_extras adapter registered."""
    adapter = _CliFlagsAdapter()
    register_adapter(adapter)

    sock = tmp_path / "popolad.sock"
    events = tmp_path / "events"
    popolad = Popolad(events_dir=events, adapter=build_command)
    app = create_app(popolad=popolad)

    config = uvicorn.Config(
        app=app,
        uds=str(sock),
        log_level="error",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)

    crashed: list[BaseException] = []

    def _run() -> None:
        try:
            asyncio.run(server.serve())
        except BaseException as exc:
            crashed.append(exc)

    thread = threading.Thread(target=_run, daemon=True, name="popolad-uvicorn-cli-test")
    thread.start()

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if sock.exists():
            try:
                with httpx.Client(
                    transport=httpx.HTTPTransport(uds=str(sock)),
                    base_url="http://popolad",
                    timeout=1.0,
                ) as c:
                    if c.get("/health").status_code == 200:
                        break
            except (httpx.HTTPError, OSError):
                pass
        if crashed:
            pytest.fail(f"uvicorn thread crashed: {crashed[0]!r}")
        time.sleep(0.05)
    else:
        pytest.fail("uvicorn UDS server did not become healthy within 5s")

    def _make_test_sync_client(_socket_path: Path | None = None) -> httpx.Client:
        return httpx.Client(
            transport=httpx.HTTPTransport(uds=str(sock)),
            base_url="http://popolad",
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=10.0),
        )

    monkeypatch.setattr(cli_main, "make_sync_client", _make_test_sync_client)

    try:
        yield sock, adapter
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        try:
            if sock.exists():
                sock.unlink()
        except OSError:
            pass


# ── 1. CLI dispatch → daemon UDS end-to-end ─────────────────────────────


def test_cli_dispatch_via_httpx_uds(
    daemon_uds: tuple[Path, _CliFlagsAdapter],
) -> None:
    """``popola dispatch`` posts to ``POST /dispatch`` over httpx UDS, returns task_id."""
    runner = CliRunner()
    r = runner.invoke(
        cli_main.app,
        ["dispatch", "hi httpx", "--cli", "echo_extras", "--wait", "--timeout", "5", "--json"],
    )
    assert r.exit_code == 0, f"exit={r.exit_code} stdout={r.stdout!r} exc={r.exception!r}"
    payload = json.loads(r.stdout.strip().splitlines()[-1])
    assert payload["task_id"].startswith("echo_extras-")
    assert payload["cli"] == "echo_extras"


# ── 2. --cli-flag KEY=VAL parsing + pass-through (R-012) ────────────────


def test_cli_flag_parsing_unit() -> None:
    """``_parse_cli_flags`` parses KEY=VAL with JSON value coercion + raises on bad input."""
    parsed = _parse_cli_flags(["yolo=true", "session_id=abc-123", "max_turns=5"])
    assert parsed == {"yolo": True, "session_id": "abc-123", "max_turns": 5}

    parsed_str = _parse_cli_flags(["output_format=text"])
    assert parsed_str == {"output_format": "text"}

    parsed_empty = _parse_cli_flags([])
    assert parsed_empty == {}

    import typer

    with pytest.raises(typer.BadParameter, match="KEY=VAL"):
        _parse_cli_flags(["nokey"])

    with pytest.raises(typer.BadParameter, match="missing key"):
        _parse_cli_flags(["=value"])


def test_cli_flag_passes_extra_to_adapter(
    daemon_uds: tuple[Path, _CliFlagsAdapter],
) -> None:
    """End-to-end: ``--cli-flag yolo=true`` → adapter sees ``extra={'yolo': True}``."""
    runner = CliRunner()
    _sock, adapter = daemon_uds

    r = runner.invoke(
        cli_main.app,
        [
            "dispatch",
            "with-flags",
            "--cli",
            "echo_extras",
            "--cli-flag",
            "yolo=true",
            "--cli-flag",
            "session_id=abc-123",
            "--wait",
            "--timeout",
            "5",
            "--json",
        ],
    )
    assert r.exit_code == 0, f"exit={r.exit_code} stdout={r.stdout!r} exc={r.exception!r}"
    captured = type(adapter).captured_extra
    assert captured, "adapter never saw any extra kwarg"
    last = captured[-1]
    assert last.get("yolo") is True, f"yolo not parsed correctly: {last}"
    assert last.get("session_id") == "abc-123", f"session_id missing: {last}"


# ── 3. --events-dir advisory (R-014 part) ───────────────────────────────


def test_cli_events_dir_advisory_passthrough(
    daemon_uds: tuple[Path, _CliFlagsAdapter],
    tmp_path: Path,
) -> None:
    """``--events-dir`` is accepted (R-014); CLI passes it as ``__events_dir`` extra hint.

    Stage A only wires the option through to the daemon as an advisory hint
    in ``extra``; Stage E will let dispatch_task actually honor it.
    """
    runner = CliRunner()
    advisory = tmp_path / "user_supplied_events"

    r = runner.invoke(
        cli_main.app,
        [
            "dispatch",
            "events-dir-test",
            "--cli",
            "echo_extras",
            "--events-dir",
            str(advisory),
            "--wait",
            "--timeout",
            "5",
            "--json",
        ],
    )
    assert r.exit_code == 0, f"exit={r.exit_code} stdout={r.stdout!r} exc={r.exception!r}"
    _sock, adapter = daemon_uds
    captured = type(adapter).captured_extra
    assert captured, "adapter never saw extra kwarg"
    last = captured[-1]
    assert last.get("__events_dir") == str(advisory), (
        f"__events_dir advisory missing or wrong: {last}"
    )


# ── 4. Daemon-down handling ─────────────────────────────────────────────


def test_dispatch_when_daemon_down_renders_friendly_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_registry: None,
) -> None:
    """``popola dispatch`` with no daemon → friendly error msg + exit 1 (No Silent Failures)."""
    nonexistent = tmp_path / "no_socket_here.sock"
    monkeypatch.setattr(cli_main, "_socket_path", lambda: nonexistent)

    runner = CliRunner()
    r = runner.invoke(
        cli_main.app,
        ["dispatch", "doomed", "--cli", "cursor", "--json"],
    )
    assert r.exit_code == 1, f"expected exit 1, got {r.exit_code}; stdout={r.stdout!r}"
    output = r.stdout + (r.stderr if hasattr(r, "stderr") else "")
    assert "popolad not running" in output, (
        f"missing 'popolad not running' message in: {output!r}"
    )


# ── 5. Bonus: popola popolad --help lists subcommands ────────────────────


def test_popolad_subcommand_group_registered() -> None:
    """``popola popolad --help`` lists start / stop / status."""
    from popolaloom.cli import app as popola_root_app

    runner = CliRunner()
    r = runner.invoke(popola_root_app, ["popolad", "--help"])
    assert r.exit_code == 0, f"exit={r.exit_code} stdout={r.stdout!r}"
    assert "start" in r.stdout
    assert "stop" in r.stdout
    assert "status" in r.stdout


# ── 6. Bonus: attach defaults to --follow=True (R-005 fix) ──────────────


def test_attach_default_follow_true(
    daemon_uds: tuple[Path, _CliFlagsAdapter],
) -> None:
    """``popola attach <id>`` (no --no-follow) streams events; we just verify exit 0.

    The default ``--follow`` for an already-terminal task should still run
    cleanly (terminate when stream closes).
    """
    runner = CliRunner()

    r_disp = runner.invoke(
        cli_main.app,
        [
            "dispatch",
            "for-attach",
            "--cli",
            "echo_extras",
            "--wait",
            "--timeout",
            "5",
            "--json",
        ],
    )
    assert r_disp.exit_code == 0
    tid = json.loads(r_disp.stdout.strip().splitlines()[-1])["task_id"]

    r_att = runner.invoke(cli_main.app, ["attach", tid])
    assert r_att.exit_code == 0, (
        f"attach (default --follow) failed: exit={r_att.exit_code} "
        f"stdout={r_att.stdout!r} exc={r_att.exception!r}"
    )
    assert "task.dispatched" in r_att.stdout
    assert "task.completed" in r_att.stdout


# ── 7. Bonus: list-cli renders without Rich markup leak (R-014) ─────────


def test_list_cli_no_markup_leak() -> None:
    """``popola list-cli`` renders status as ``available`` / ``missing`` (R-014)."""
    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["list-cli"])
    assert r.exit_code == 0
    out = r.stdout
    assert "[available]" not in out, f"R-014: literal '[available]' leaked: {out}"
    assert "[missing]" not in out, f"R-014: literal '[missing]' leaked: {out}"


# ── 8. Bonus: cancel command end-to-end ─────────────────────────────────


def test_cli_cancel_command(
    daemon_uds: tuple[Path, _CliFlagsAdapter],
    isolated_registry: None,
) -> None:
    """``popola cancel <id>`` returns success on a long-running task."""

    class _SleepyAdapter:
        name = "sleepy_cli"
        binary = sys.executable

        def build_command(
            self,
            prompt: str,
            cwd: Path | None = None,
            extra: dict[str, Any] | None = None,
        ) -> list[str]:
            return [sys.executable, "-c", "import time; time.sleep(30)"]

        def is_available(self) -> bool:
            return True

    register_adapter(_SleepyAdapter())

    runner = CliRunner()
    r_disp = runner.invoke(
        cli_main.app,
        ["dispatch", "long", "--cli", "sleepy_cli", "--json"],
    )
    assert r_disp.exit_code == 0
    tid = json.loads(r_disp.stdout.strip().splitlines()[-1])["task_id"]

    time.sleep(0.2)

    r_cancel = runner.invoke(cli_main.app, ["cancel", tid, "--json"])
    assert r_cancel.exit_code == 0, f"cancel failed: {r_cancel.stdout!r}"
    body = json.loads(r_cancel.stdout.strip().splitlines()[-1])
    assert body["task_id"] == tid
    assert body["requested_signal"] == "SIGTERM"
