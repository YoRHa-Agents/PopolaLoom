"""Focused tests for v0.9.10 dispatch behavior driven by user_preferences."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from popolaloom.cli.init_cmd import write_user_preferences_for_cli
from popolaloom.cli.main import app as main_app
from popolaloom.daemon.main import UserPreferencesConfig


@pytest.fixture
def isolated_popola_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    popola_home = tmp_path / "popola"
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


class _FakeAdapter:
    def __init__(self, available: bool) -> None:
        self.available = available
        self.binary = "fake"

    def is_available(self) -> bool:
        return self.available


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


def _mock_dispatch_client(monkeypatch: pytest.MonkeyPatch, task_id: str) -> MagicMock:
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"task_id": task_id}
    mock_client.__enter__.return_value.post.return_value = mock_response
    monkeypatch.setattr("popolaloom.cli.main.make_sync_client", lambda: mock_client)
    return mock_client


def _posted_body(mock_client: MagicMock) -> dict[str, Any]:
    return mock_client.__enter__.return_value.post.call_args.kwargs["json"]


def _patch_availability(
    monkeypatch: pytest.MonkeyPatch,
    available: dict[str, bool],
) -> None:
    def fake_get_adapter(name: str) -> _FakeAdapter:
        if name not in available:
            raise KeyError(name)
        return _FakeAdapter(available[name])

    monkeypatch.setattr("popolaloom.cli.main.get_adapter", fake_get_adapter)


def test_dispatch_uses_local_default_when_cli_omitted(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_user_preferences_for_cli(
        UserPreferencesConfig(default_local_cli="claude", follow_devola_flow=True)
    )
    _patch_availability(monkeypatch, {"claude": True})
    mock_client = _mock_dispatch_client(monkeypatch, "claude-pref-1234")

    result = runner.invoke(main_app, ["dispatch", "do local work", "--no-wizard"])

    assert result.exit_code == 0, _combined_output(result)
    body = _posted_body(mock_client)
    assert body["cli"] == "claude"
    assert body["prompt"] == "do local work"
    assert body["extra"]["follow_devola_flow"] is True


def test_dispatch_uses_cloud_default_and_warns_when_no_self_hosted_worker(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_user_preferences_for_cli(UserPreferencesConfig(default_runtime="cloud"))
    mock_client = _mock_dispatch_client(monkeypatch, "cursor-cloud-pref-1234")

    result = runner.invoke(main_app, ["dispatch", "do cloud work", "--no-wizard"])

    assert result.exit_code == 0, _combined_output(result)
    body = _posted_body(mock_client)
    assert body["cli"] == "cursor-cloud"
    assert "worker_name" not in body.get("extra", {})
    assert "falling back to cursor-managed cloud" in _combined_output(result)


def test_dispatch_ask_each_time_prompts_once(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_user_preferences_for_cli(UserPreferencesConfig(default_runtime="ask-each-time"))
    _patch_availability(monkeypatch, {"claude": True})
    mock_client = _mock_dispatch_client(monkeypatch, "claude-ask-1234")

    result = runner.invoke(main_app, ["dispatch", "ask me", "--no-wizard"], input="claude\n")

    assert result.exit_code == 0, _combined_output(result)
    assert _posted_body(mock_client)["cli"] == "claude"


def test_dispatch_fallback_chain_uses_next_available_cli(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_user_preferences_for_cli(
        UserPreferencesConfig(
            default_local_cli="cursor",
            fallback_chain=("claude", "codex"),
        )
    )
    _patch_availability(monkeypatch, {"cursor": False, "claude": True, "codex": True})
    mock_client = _mock_dispatch_client(monkeypatch, "claude-fallback-1234")

    result = runner.invoke(main_app, ["dispatch", "fall back", "--no-wizard"])

    assert result.exit_code == 0, _combined_output(result)
    assert _posted_body(mock_client)["cli"] == "claude"
    assert "[prefs] cursor unavailable; falling back to claude" in _combined_output(result)


def test_dispatch_prompt_each_dispatch_overrides_local_default(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_user_preferences_for_cli(
        UserPreferencesConfig(default_local_cli="cursor", prompt_each_dispatch=True)
    )
    _patch_availability(monkeypatch, {"cursor": True, "codex": True})
    mock_client = _mock_dispatch_client(monkeypatch, "codex-prompt-1234")

    result = runner.invoke(
        main_app,
        ["dispatch", "prompt each", "--no-wizard"],
        input="codex\n",
    )

    assert result.exit_code == 0, _combined_output(result)
    assert _posted_body(mock_client)["cli"] == "codex"


def test_dispatch_without_preferences_preserves_old_cli_required_error(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    result = runner.invoke(main_app, ["dispatch", "some prompt"])

    assert result.exit_code == 2
    assert "error: --cli is required (or use --replay HANDOFF_ID)" in result.output
