"""v1.5.0 — dispatch CLI extras propagation contract tests.

Covers feedback_for_v1.4.0 §7 issue #2: ``[user_preferences.cursor].cli_args``
must propagate to ``extra["cli_args"]`` for ``--cli=cursor`` dispatches.
The v1.3.0 regression silently dropped this; v1.5.0 honors it.

See PLAN.md Phase E.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from popolaloom.cli.init_cmd import write_user_preferences_for_cli
from popolaloom.cli.main import app as main_app
from popolaloom.daemon.main import (
    UserPreferencesConfig,
    UserPrefsCursor,
)


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


def _patch_availability(
    monkeypatch: pytest.MonkeyPatch,
    available: dict[str, bool],
) -> None:
    def fake_get_adapter(name: str) -> _FakeAdapter:
        if name not in available:
            raise KeyError(name)
        return _FakeAdapter(available[name])

    monkeypatch.setattr("popolaloom.cli.main.get_adapter", fake_get_adapter)


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


def test_cli_args_propagated_from_prefs_for_cursor_dispatch(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1.5.0 — ``cursor.cli_args`` pref → ``extra['cli_args']`` for cli=cursor."""
    write_user_preferences_for_cli(
        UserPreferencesConfig(
            default_local_cli="cursor",
            cursor=UserPrefsCursor(cli_args=("--trust", "--no-color")),
        )
    )
    _patch_availability(monkeypatch, {"cursor": True})
    mock_client = _mock_dispatch_client(monkeypatch, "cursor-cli-args-1234")

    result = runner.invoke(
        main_app,
        ["dispatch", "test cli_args", "--cli=cursor", "--no-wizard"],
    )

    assert result.exit_code == 0, _combined_output(result)
    body = _posted_body(mock_client)
    assert body["cli"] == "cursor"
    assert body["extra"]["cli_args"] == ["--trust", "--no-color"]


def test_cli_args_not_overwritten_when_explicit_cli_flag_present(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1.5.0 — explicit ``--cli-flag cli_args=...`` wins over prefs."""
    write_user_preferences_for_cli(
        UserPreferencesConfig(
            default_local_cli="cursor",
            cursor=UserPrefsCursor(cli_args=("--trust",)),
        )
    )
    _patch_availability(monkeypatch, {"cursor": True})
    mock_client = _mock_dispatch_client(monkeypatch, "cursor-cli-args-explicit-1234")

    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "explicit override",
            "--cli=cursor",
            "--cli-flag",
            "cli_args=--explicit-flag",
            "--no-wizard",
        ],
    )

    assert result.exit_code == 0, _combined_output(result)
    body = _posted_body(mock_client)
    assert body["extra"]["cli_args"] == "--explicit-flag"


def test_cli_args_empty_prefs_no_extras_key(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1.5.0 — when prefs ``cli_args`` is empty, no ``extra['cli_args']`` written."""
    write_user_preferences_for_cli(
        UserPreferencesConfig(
            default_local_cli="cursor",
            cursor=UserPrefsCursor(cli_args=()),
        )
    )
    _patch_availability(monkeypatch, {"cursor": True})
    mock_client = _mock_dispatch_client(monkeypatch, "cursor-no-cli-args-1234")

    result = runner.invoke(
        main_app,
        ["dispatch", "no cli_args", "--cli=cursor", "--no-wizard"],
    )

    assert result.exit_code == 0, _combined_output(result)
    body = _posted_body(mock_client)
    assert "cli_args" not in body.get("extra", {})


def test_cli_args_not_propagated_for_non_cursor_adapter(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1.5.0 — ``cursor.cli_args`` is cursor-specific; not propagated for cli=claude."""
    write_user_preferences_for_cli(
        UserPreferencesConfig(
            default_local_cli="cursor",
            cursor=UserPrefsCursor(cli_args=("--trust",)),
        )
    )
    _patch_availability(monkeypatch, {"claude": True})
    mock_client = _mock_dispatch_client(monkeypatch, "claude-no-cli-args-1234")

    result = runner.invoke(
        main_app,
        ["dispatch", "claude dispatch", "--cli=claude", "--no-wizard"],
    )

    assert result.exit_code == 0, _combined_output(result)
    body = _posted_body(mock_client)
    # extra may be missing entirely or present without cli_args; either is OK.
    assert "cli_args" not in body.get("extra", {})
