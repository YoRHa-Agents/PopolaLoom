"""v1.6.0 constraint #4 — surface ``dashboard_url`` after dispatch.

The daemon emits ``cloud.queued`` carrying ``dashboard_url`` once the
supervisor's StartBackgroundComposerFromSnapshot RPC (Path-B) or
``POST /v1/agents`` (Path-A REST) returns. The CLI polls the events
log for up to ``_DASHBOARD_URL_POLL_TOTAL_S`` (~2 s default) and
prints ``view: <url>`` to stdout. Per the No-Silent-Failures rule the
CLI emits a stderr WARN when the poll times out.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from popolaloom.cli import cloud_worker_cmd
from popolaloom.cli.main import app as main_app


@pytest.fixture(autouse=True)
def _stub_jwt_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic JWT-loader stub: tests exercise dispatch wire shape only."""
    monkeypatch.setattr(
        "popolaloom.cloud.internal.jwt_auth.load_jwt_bundle",
        lambda: object(),
    )


@pytest.fixture
def isolated_popola_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    popola_home = tmp_path / "popola"
    popola_home.mkdir()
    (popola_home / "events").mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("POPOLA_HOME", str(popola_home))
    monkeypatch.delenv("POPOLA_WORKER_NAME", raising=False)
    monkeypatch.delenv("POPOLA_SELF_HOSTED_WORKER_NAME", raising=False)
    monkeypatch.chdir(project)
    yield popola_home


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _combined_output(result: object) -> str:
    parts: list[str] = []
    for attr in ("stdout", "stderr", "output"):
        try:
            value = getattr(result, attr, "") or ""
        except (ValueError, AttributeError):
            value = ""
        if value and value not in parts:
            parts.append(value)
    return "".join(parts)


def _seed_cloud_queued_event(
    popola_home: Path,
    task_id: str,
    dashboard_url: str,
) -> None:
    """Pretend the daemon already wrote the ``cloud.queued`` event."""
    events_dir = popola_home / "events"
    events_dir.mkdir(exist_ok=True)
    event_path = events_dir / f"{task_id}.jsonl"
    event = {
        "specversion": "1.0",
        "type": "cloud.queued",
        "time": "2026-05-18T20:00:00.000Z",
        "data": {
            "task_id": task_id,
            "agent_id": "bc-test",
            "run_id": "run-test",
            "runtime": "cloud",
            "initial_phase": "CREATING",
            "dashboard_url": dashboard_url,
        },
    }
    event_path.write_text(json.dumps(event) + "\n", encoding="utf-8")


def _mock_dispatch_client(monkeypatch: pytest.MonkeyPatch, task_id: str) -> MagicMock:
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"task_id": task_id}
    mock_client.__enter__.return_value.post.return_value = mock_response
    monkeypatch.setattr("popolaloom.cli.main.make_sync_client", lambda: mock_client)
    return mock_client


# ── popola dispatch surface ───────────────────────────────────────────────


def test_dispatch_prints_view_url_for_cursor_cloud(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola dispatch --cli=cursor-cloud`` prints ``view: <url>`` on success."""
    task_id = "cursor-cloud-view-1234"
    dashboard_url = "https://cursor.com/agents/bc-test"
    _seed_cloud_queued_event(isolated_popola_home, task_id, dashboard_url)
    _mock_dispatch_client(monkeypatch, task_id)

    # Speed up the poll loop so the test runs in ~0 s instead of 2 s.
    monkeypatch.setattr(
        "popolaloom.cli.main._DASHBOARD_URL_POLL_INTERVAL_S", 0.0
    )

    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "view-url smoke",
            "--cli=cursor-cloud",
        ],
    )

    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert task_id in out
    assert f"view: {dashboard_url}" in out


def test_dispatch_self_hosted_prints_view_url(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--cloud-target=self-hosted`` likewise surfaces the dashboard URL."""
    task_id = "self-hosted-view-1234"
    dashboard_url = "https://cursor.com/agents/bc-self-hosted"
    _seed_cloud_queued_event(isolated_popola_home, task_id, dashboard_url)
    _mock_dispatch_client(monkeypatch, task_id)
    monkeypatch.setattr(
        "popolaloom.cli.main._DASHBOARD_URL_POLL_INTERVAL_S", 0.0
    )

    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "self-hosted view-url",
            "--cloud-target=self-hosted",
            "--worker-name=probe-w1",
        ],
    )

    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert task_id in out
    assert f"view: {dashboard_url}" in out


def test_dispatch_warn_when_dashboard_url_missing(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout path emits a bilingual stderr WARN (No-Silent-Failures)."""
    task_id = "cursor-cloud-no-url-1234"
    # Do NOT seed the cloud.queued event — the poller will time out.
    _mock_dispatch_client(monkeypatch, task_id)

    # Tighten the poll window so the test stays fast.
    monkeypatch.setattr(
        "popolaloom.cli.main._DASHBOARD_URL_POLL_TOTAL_S", 0.1
    )
    monkeypatch.setattr(
        "popolaloom.cli.main._DASHBOARD_URL_POLL_INTERVAL_S", 0.01
    )

    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "no url",
            "--cli=cursor-cloud",
        ],
    )

    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert task_id in out
    assert "dashboard_url not surfaced" in out
    # The WARN must NOT print a stray "view: " line on timeout.
    assert "view: " not in out


def test_dispatch_warn_when_event_payload_missing_url(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``cloud.queued`` event lacking ``dashboard_url`` emits a WARN, not a print."""
    task_id = "cursor-cloud-empty-url-1234"
    events_dir = isolated_popola_home / "events"
    events_dir.mkdir(exist_ok=True)
    event = {
        "specversion": "1.0",
        "type": "cloud.queued",
        "time": "2026-05-18T20:00:00.000Z",
        "data": {
            "task_id": task_id,
            "agent_id": "bc-empty",
            "run_id": "run-empty",
            "runtime": "cloud",
            "initial_phase": "CREATING",
            # NOTE: no dashboard_url field
        },
    }
    (events_dir / f"{task_id}.jsonl").write_text(
        json.dumps(event) + "\n",
        encoding="utf-8",
    )
    _mock_dispatch_client(monkeypatch, task_id)
    monkeypatch.setattr(
        "popolaloom.cli.main._DASHBOARD_URL_POLL_INTERVAL_S", 0.0
    )

    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "missing url field",
            "--cli=cursor-cloud",
        ],
    )

    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "did not carry a dashboard_url" in out
    assert "view: " not in out


def test_dispatch_non_cloud_cli_skips_dashboard_url_poll(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local CLI (cursor / claude / codex) does NOT trigger the cloud poll.

    The non-cloud surface keeps its v0.9.9 F3 line (``view: popola attach
    ... --follow``) but never blocks on a (never-emitted) ``cloud.queued``
    event.
    """
    task_id = "cursor-local-1234"
    _mock_dispatch_client(monkeypatch, task_id)

    # Stub the local adapter availability so the dispatch resolves the
    # ``cursor`` adapter without needing a real cursor-agent binary.
    class _FakeAdapter:
        binary = "fake-cursor"

        def is_available(self) -> bool:
            return True

    monkeypatch.setattr(
        "popolaloom.cli.main.get_adapter",
        lambda name: _FakeAdapter() if name == "cursor" else (_ for _ in ()).throw(KeyError(name)),
    )

    # Touch the poll constants to confirm the local path doesn't wait —
    # set the total to a large value so a regression would block.
    monkeypatch.setattr(
        "popolaloom.cli.main._DASHBOARD_URL_POLL_TOTAL_S", 30.0
    )

    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "local cursor",
            "--cli=cursor",
        ],
    )

    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert task_id in out
    # Local-cursor branch emits the legacy `view: popola attach ...` hint:
    assert "view: popola attach" in out
    # ...and NEVER prints the v1.6.0 dashboard URL form:
    assert "https://cursor.com/agents/" not in out


# ── popola cloud worker dispatch surface ──────────────────────────────────


def test_worker_dispatch_prints_view_url(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola cloud worker dispatch`` surfaces ``view: <url>`` too."""
    task_id = "worker-dispatch-view-1234"
    dashboard_url = "https://cursor.com/agents/bc-worker"
    _seed_cloud_queued_event(isolated_popola_home, task_id, dashboard_url)

    monkeypatch.setattr(
        cloud_worker_cmd, "_enforce_self_hosted_worker_exists", lambda **k: None
    )
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda worker_dir: [],
    )
    import httpx

    monkeypatch.setattr(
        cloud_worker_cmd,
        "_post_popolad_dispatch_request",
        lambda body: httpx.Response(200, json={"task_id": task_id}),
    )
    monkeypatch.setattr(
        "popolaloom.cli.main._DASHBOARD_URL_POLL_INTERVAL_S", 0.0
    )

    result = runner.invoke(
        main_app,
        [
            "cloud",
            "worker",
            "dispatch",
            "test worker dispatch view",
            "--worker-dir",
            str(isolated_popola_home),
            "--repo-url",
            "https://github.com/acme/repo",
        ],
    )

    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert task_id in out
    assert f"view: {dashboard_url}" in out
