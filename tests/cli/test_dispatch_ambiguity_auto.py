"""Implicit dispatch wizard trigger tests."""

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
def isolated_popola_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    popola_home = tmp_path / "popola"
    project = tmp_path / "project"
    popola_home.mkdir()
    project.mkdir()
    monkeypatch.setenv("POPOLA_HOME", str(popola_home))
    monkeypatch.chdir(project)
    yield popola_home


def _mock_dispatch_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"task_id": "implicit-wizard-123"}
    mock_client.__enter__.return_value.post.return_value = mock_response
    monkeypatch.setattr("popolaloom.cli.main.make_sync_client", lambda: mock_client)
    return mock_client


def _posted_body(mock_client: MagicMock) -> dict[str, Any]:
    return mock_client.__enter__.return_value.post.call_args.kwargs["json"]


def test_ambiguity_prompt_auto_enters_wizard(
    isolated_popola_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_user_preferences_for_cli(UserPreferencesConfig())
    mock_client = _mock_dispatch_client(monkeypatch)
    runner = CliRunner()

    result = runner.invoke(main_app, ["dispatch", "ambiguous task"], input="1\n1\ny\n")

    assert result.exit_code == 0, result.output
    assert "Dispatch target" in result.output
    assert _posted_body(mock_client)["cli"] == "cursor"


def test_no_wizard_uses_preferences_silently(
    isolated_popola_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_user_preferences_for_cli(UserPreferencesConfig(default_local_cli="claude"))
    mock_client = _mock_dispatch_client(monkeypatch)
    monkeypatch.setattr("popolaloom.cli.main._local_cli_available", lambda _name: True)
    runner = CliRunner()

    result = runner.invoke(main_app, ["dispatch", "precise task", "--no-wizard"])

    assert result.exit_code == 0, result.output
    assert _posted_body(mock_client)["cli"] == "claude"
