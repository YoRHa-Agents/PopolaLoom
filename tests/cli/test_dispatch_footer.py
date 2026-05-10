"""v0.9.9 F3 — dispatch-time footer + worker-status idle hint tests.

Covers the three CLI surfaces the v0.9.9 plan binds for the F3 patch:

(f) ``popola dispatch --cli=cursor`` stdout includes the verbatim
    ``view: popola attach <task_id> --follow ...`` footer line per
    ``feedback_for_v0.9.7.md:51``;
(g) ``popola dispatch --cli=cursor-cloud`` does NOT include the
    footer (cloud-runtime tasks ARE visible on the Cursor dashboard,
    so the footer would mislead operators);
(h) ``popola cloud worker status`` includes the verbatim
    ``note: 0 sessions claimed since worker started ...`` idle hint
    when the rendered status carries no session-claim signal.

Hermetic — every test mocks the popolad RPC transport (``make_sync_client``
for dispatch; ``_fetch_management_endpoint`` for worker status) so no
real daemon process is required.  The CliRunner is instantiated with
``mix_stderr=False`` semantics (Typer ≥ 0.9 default) so the footer's
stdout-vs-stderr placement is asserted unambiguously.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from rich.console import Console
from typer.testing import CliRunner

from popolaloom.cli import cloud_worker_cmd
from popolaloom.cli.main import app as main_app


@pytest.fixture
def runner() -> CliRunner:
    """Default :class:`CliRunner` (Typer ≥ 0.9 drops ``mix_stderr``)."""
    return CliRunner()


@pytest.fixture
def isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Hermetic ``$POPOLA_HOME`` + ``$HOME`` so credential reads cannot bleed."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    yield tmp_path


@pytest.fixture(autouse=True)
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the worker-cmd Rich console wide so substring asserts hold."""
    monkeypatch.setattr(
        cloud_worker_cmd, "_console_out", Console(width=200, height=50)
    )


def _make_dispatch_mock(task_id: str) -> MagicMock:
    """Build a ``make_sync_client`` mock whose POST /dispatch returns ``task_id``."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"task_id": task_id}
    mock_client.__enter__.return_value.post.return_value = mock_response
    return mock_client


def _combined_output(result: Any) -> str:
    """Return ``stdout + stderr`` as a single string (Typer / Click compat)."""
    parts: list[str] = []
    for attr in ("stdout", "stderr", "output"):
        try:
            value = getattr(result, attr, "") or ""
        except (ValueError, AttributeError):
            value = ""
        if value and value not in parts:
            parts.append(value)
    return "".join(parts)


# ── (f) cursor → footer present ──────────────────────────────────────────


def test_dispatch_cursor_footer_present(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--cli=cursor`` dispatch stdout includes the verbatim footer."""
    task_id = "cursor-fakefoot1234"
    mock_client = _make_dispatch_mock(task_id)
    monkeypatch.setattr(
        "popolaloom.cli.main.make_sync_client", lambda: mock_client
    )

    result = runner.invoke(
        main_app,
        ["dispatch", "echo hi", "--cli", "cursor"],
    )
    assert result.exit_code == 0, _combined_output(result)
    out = result.stdout
    assert task_id in out, f"task_id missing from stdout: {out!r}"

    expected = (
        f"view: popola attach {task_id} --follow "
        "(note: Cursor dashboard does not show local subprocess tasks)"
    )
    assert expected in out, (
        f"footer line not found in stdout.\n"
        f"  expected: {expected!r}\n"
        f"  actual:   {out!r}"
    )


# ── (g) cursor-cloud → footer absent ─────────────────────────────────────


def test_dispatch_cursor_cloud_footer_absent(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--cli=cursor-cloud`` dispatch must NOT print the local-only footer."""
    task_id = "cursor-cloud-fake5678"
    mock_client = _make_dispatch_mock(task_id)
    monkeypatch.setattr(
        "popolaloom.cli.main.make_sync_client", lambda: mock_client
    )

    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "build something",
            "--cli",
            "cursor-cloud",
            "--cli-flag",
            "repo_url=https://example.invalid/repo",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    out = result.stdout
    assert task_id in out
    assert "popola attach" not in out, (
        f"local-only footer leaked into cursor-cloud dispatch stdout: {out!r}"
    )
    assert "Cursor dashboard does not show" not in out


def test_dispatch_other_cli_footer_absent(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--cli=claude`` dispatch must NOT print the cursor-only footer."""
    task_id = "claude-fakeabcd"
    mock_client = _make_dispatch_mock(task_id)
    monkeypatch.setattr(
        "popolaloom.cli.main.make_sync_client", lambda: mock_client
    )

    result = runner.invoke(
        main_app,
        ["dispatch", "do work", "--cli", "claude"],
    )
    assert result.exit_code == 0, _combined_output(result)
    out = result.stdout
    assert task_id in out
    assert "popola attach" not in out
    assert "Cursor dashboard" not in out


# ── (h) worker status idle hint ──────────────────────────────────────────


def _fake_management_endpoint_factory(
    responses: dict[str, tuple[int, str]],
) -> Any:
    """Build a fake ``_fetch_management_endpoint`` from a ``{path: (status, body)}`` map."""

    def fake_fetch(
        host: str, port: int, path: str, *, timeout_s: float = 3.0
    ) -> tuple[int, str]:
        normalized = path.lstrip("/")
        if normalized not in responses:
            raise httpx.ConnectError(f"path {normalized!r} not stubbed")
        return responses[normalized]

    return fake_fetch


def test_worker_status_idle_hint_appears_when_no_sessions_claimed(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cloud worker status`` adds the idle hint when no claim signal yet.

    Health endpoints respond OK and the worker is connected, but
    ``readyz.claimed`` is false, ``session_active`` is 0, and the
    ``last_activity`` heartbeat has never fired (zero-valued).
    """
    fake = _fake_management_endpoint_factory(
        {
            "healthz": (200, json.dumps({"status": "ok"})),
            "readyz": (
                200,
                json.dumps(
                    {"status": "ok", "connected": True, "claimed": False}
                ),
            ),
            "metrics": (
                200,
                "cursor_self_hosted_worker_connected 1\n"
                "cursor_self_hosted_worker_session_active 0\n"
                "cursor_self_hosted_worker_connect_attempts_total 1\n"
                "cursor_self_hosted_worker_last_activity_unix_seconds 0\n",
            ),
        }
    )
    monkeypatch.setattr(cloud_worker_cmd, "_fetch_management_endpoint", fake)

    result = runner.invoke(
        main_app,
        ["cloud", "worker", "status", "--management-addr", "127.0.0.1:39231"],
    )
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "0 sessions claimed since worker started" in out, (
        f"idle hint missing from worker-status output: {out!r}"
    )
    assert (
        "the worker is healthy but has not been routed any task yet" in out
    ), f"idle-hint trailing context missing: {out!r}"


def test_worker_status_idle_hint_absent_when_claim_seen(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cloud worker status`` suppresses the hint when the worker has been claimed."""
    fake = _fake_management_endpoint_factory(
        {
            "healthz": (200, json.dumps({"status": "ok"})),
            "readyz": (
                200,
                json.dumps(
                    {"status": "ok", "connected": True, "claimed": False}
                ),
            ),
            "metrics": (
                200,
                "cursor_self_hosted_worker_connected 1\n"
                "cursor_self_hosted_worker_session_active 0\n"
                "cursor_self_hosted_worker_connect_attempts_total 1\n"
                "cursor_self_hosted_worker_last_activity_unix_seconds "
                "1778335163\n",
            ),
        }
    )
    monkeypatch.setattr(cloud_worker_cmd, "_fetch_management_endpoint", fake)

    result = runner.invoke(
        main_app,
        ["cloud", "worker", "status", "--management-addr", "127.0.0.1:39231"],
    )
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "0 sessions claimed since worker started" not in out, (
        f"idle hint must NOT appear when last_activity > 0; got: {out!r}"
    )


def test_worker_status_idle_hint_absent_when_currently_claimed(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``readyz.claimed`` is true the idle hint is suppressed."""
    fake = _fake_management_endpoint_factory(
        {
            "healthz": (200, json.dumps({"status": "ok"})),
            "readyz": (
                200,
                json.dumps(
                    {"status": "ok", "connected": True, "claimed": True}
                ),
            ),
            "metrics": (
                200,
                "cursor_self_hosted_worker_connected 1\n"
                "cursor_self_hosted_worker_session_active 1\n"
                "cursor_self_hosted_worker_last_activity_unix_seconds 0\n",
            ),
        }
    )
    monkeypatch.setattr(cloud_worker_cmd, "_fetch_management_endpoint", fake)

    result = runner.invoke(
        main_app,
        ["cloud", "worker", "status", "--management-addr", "127.0.0.1:39231"],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert "0 sessions claimed" not in _combined_output(result)


def test_worker_status_idle_hint_skipped_in_json_mode(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json`` keeps the JSON parseable: no hint line is appended."""
    fake = _fake_management_endpoint_factory(
        {
            "healthz": (200, json.dumps({"status": "ok"})),
            "readyz": (
                200,
                json.dumps(
                    {"status": "ok", "connected": True, "claimed": False}
                ),
            ),
            "metrics": (
                200,
                "cursor_self_hosted_worker_session_active 0\n"
                "cursor_self_hosted_worker_last_activity_unix_seconds 0\n",
            ),
        }
    )
    monkeypatch.setattr(cloud_worker_cmd, "_fetch_management_endpoint", fake)

    result = runner.invoke(
        main_app,
        [
            "cloud",
            "worker",
            "status",
            "--management-addr",
            "127.0.0.1:39231",
            "--json",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "0 sessions claimed since worker started" not in out
    parsed = json.loads(out)
    assert parsed["healthz"]["status"] == "ok"
