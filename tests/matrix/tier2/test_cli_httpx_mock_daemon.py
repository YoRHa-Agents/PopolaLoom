"""Tier 2 / B3 — popola CLI tests against a mocked httpx daemon transport.

The L3 brief mentions ``responses`` for HTTP mocking, but
``responses`` only mocks the ``requests`` library; for ``httpx``
(the actual transport our CLI uses) the canonical approach is
:class:`httpx.MockTransport`. We monkeypatch :func:`cli_main.make_sync_client`
to return a client backed by a MockTransport configured with canned
responses for each daemon endpoint.

5 scenarios per the brief:

1. dispatch returns task_id (200 OK).
2. status returns 404 → CLI exits 1 with "task not found" message.
3. list returns empty array → CLI prints "No active tasks." (zero items).
4. cancel returns success → CLI exits 0 with the SIGTERM verbiage.
5. daemon-down (ConnectError) → CLI prints "popolad not running" + exit 1.

Plus 2 bonus cases (probe success + dispatch with body validation) to
exercise more CLI branches and lift coverage.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from popolaloom.cli import main as cli_main


def _mock_client_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[Path | None], httpx.Client]:
    """Return a function suitable for monkey-patching ``cli_main.make_sync_client``."""

    def _factory(_socket_path: Path | None = None) -> httpx.Client:
        transport = httpx.MockTransport(handler)
        return httpx.Client(transport=transport, base_url="http://popolad", timeout=5.0)

    return _factory


# ── 1: dispatch returns task_id ──────────────────────────────────────────


def test_dispatch_returns_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 OK on POST /dispatch yields exit 0 + JSON containing task_id."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST"
        assert req.url.path == "/dispatch"
        body = json.loads(req.content)
        assert body["cli"] == "cursor"
        assert body["prompt"] == "hello"
        return httpx.Response(
            200,
            json={"task_id": "cursor-abc12345", "events_log": "/tmp/x.jsonl", "cli": "cursor"},
        )

    monkeypatch.setattr(cli_main, "make_sync_client", _mock_client_factory(handler))

    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["dispatch", "hello", "--cli", "cursor", "--json"])
    assert r.exit_code == 0, f"stdout={r.stdout!r} exc={r.exception!r}"
    payload = json.loads(r.stdout.strip().splitlines()[-1])
    assert payload["task_id"] == "cursor-abc12345"


# ── 2: status 404 → friendly CLI error ───────────────────────────────────


def test_status_404_renders_task_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GET /status/<id>`` 404 → CLI prints task-not-found + exits 1."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/status/missing-id"
        return httpx.Response(404, json={"detail": "task_id not found: missing-id"})

    monkeypatch.setattr(cli_main, "make_sync_client", _mock_client_factory(handler))

    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["status", "missing-id"])
    assert r.exit_code == 1, f"expected exit 1, got {r.exit_code}; stdout={r.stdout!r}"
    output = r.stdout + (r.stderr if hasattr(r, "stderr") else "")
    assert "task not found" in output


# ── 3: list returns empty array → "No active tasks." ─────────────────────


def test_list_returns_empty_array(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GET /list`` returning ``[]`` → CLI prints empty-state message."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/list"
        return httpx.Response(200, json=[])

    monkeypatch.setattr(cli_main, "make_sync_client", _mock_client_factory(handler))

    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["list"])
    assert r.exit_code == 0
    assert "No active tasks." in r.stdout


# ── 4: cancel success → exit 0, SIGTERM mentioned ────────────────────────


def test_cancel_success_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 OK on /cancel exits 0; non-JSON output mentions SIGTERM."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST"
        assert req.url.path == "/cancel/cursor-tid"
        return httpx.Response(
            200,
            json={
                "task_id": "cursor-tid",
                "requested_signal": "SIGTERM",
                "escalated_to_sigkill": False,
                "pid": 12345,
            },
        )

    monkeypatch.setattr(cli_main, "make_sync_client", _mock_client_factory(handler))

    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["cancel", "cursor-tid"])
    assert r.exit_code == 0, f"stdout={r.stdout!r} exc={r.exception!r}"
    assert "SIGTERM" in r.stdout


# ── 5: daemon down (ConnectError) → friendly "popolad not running" ───────


def test_daemon_down_renders_friendly_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the transport raises ConnectError, CLI exits 1 with the right msg."""

    def factory(_path: Path | None = None) -> httpx.Client:
        def _raise(_req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated daemon down")

        return httpx.Client(
            transport=httpx.MockTransport(_raise),
            base_url="http://popolad",
            timeout=1.0,
        )

    monkeypatch.setattr(cli_main, "make_sync_client", factory)

    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["dispatch", "x", "--cli", "cursor"])
    assert r.exit_code == 1, f"expected exit 1, got {r.exit_code}; stdout={r.stdout!r}"
    output = r.stdout + (r.stderr if hasattr(r, "stderr") else "")
    assert "popolad not running" in output


# ── 6: probe success → daemon health ─────────────────────────────────────


def test_probe_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """``popola probe`` against a 200 /probe shows daemon_pid in non-JSON mode."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/probe"
        return httpx.Response(
            200,
            json={
                "daemon_pid": 99999,
                "started_at": "2026-05-04T12:00:00Z",
                "uptime_seconds": 42.5,
                "active_tasks": 3,
                "version": "0.2.1",
            },
        )

    monkeypatch.setattr(cli_main, "make_sync_client", _mock_client_factory(handler))

    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["probe"])
    assert r.exit_code == 0
    assert "99999" in r.stdout


# ── 7: dispatch unknown CLI → 404 path ───────────────────────────────────


def test_dispatch_unknown_cli_404_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 on /dispatch (unknown adapter) exits 1 with "unknown cli" message."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "no adapter registered for cli='vim'"})

    monkeypatch.setattr(cli_main, "make_sync_client", _mock_client_factory(handler))

    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["dispatch", "x", "--cli", "vim"])
    assert r.exit_code == 1
    output = r.stdout + (r.stderr if hasattr(r, "stderr") else "")
    assert "unknown cli" in output


# ── 8: status JSON output ────────────────────────────────────────────────


def test_status_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--json`` on status emits a JSON dict on stdout."""

    payload = {
        "task_id": "cursor-tid",
        "cli": "cursor",
        "state": "completed",
        "pid": 1234,
        "exit_code": 0,
        "started_at": "2026-05-04T11:00:00.000Z",
        "completed_at": "2026-05-04T11:01:00.000Z",
        "latest_event_index": 7,
        "arktower_task_id": None,
        "persisted": False,
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(cli_main, "make_sync_client", _mock_client_factory(handler))

    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["status", "cursor-tid", "--json"])
    assert r.exit_code == 0
    parsed: dict[str, Any] = json.loads(r.stdout.strip().splitlines()[-1])
    assert parsed["task_id"] == "cursor-tid"
    assert parsed["state"] == "completed"
