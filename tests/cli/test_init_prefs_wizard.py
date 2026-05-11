"""v1.1.0 expanded ``popola init prefs`` tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from popolaloom.cli.init_cmd import app as init_app
from popolaloom.cli.init_cmd import load_user_preferences_for_cli


@pytest.fixture
def isolated_popola_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    popola_home = tmp_path / "popola"
    popola_home.mkdir()
    monkeypatch.setenv("POPOLA_HOME", str(popola_home))
    yield popola_home


def test_prefs_set_accepts_dotted_paths(isolated_popola_home: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        init_app,
        [
            "prefs",
            "--set",
            "cursor-cloud.model=composer-2",
            "--set",
            "lark.notify_on_completed=false",
            "--set",
            "codex.sandbox=read-only",
        ],
    )

    assert result.exit_code == 0, result.output
    prefs = load_user_preferences_for_cli()
    assert prefs is not None
    assert prefs.cursor_cloud.model == "composer-2"
    assert prefs.lark.notify_on_completed is False
    assert prefs.codex.sandbox == "read-only"


def test_prefs_show_json_outputs_nested_structure(isolated_popola_home: Path) -> None:
    runner = CliRunner()
    set_result = runner.invoke(init_app, ["prefs", "--set", "cursor.output_format=stream-json"])
    assert set_result.exit_code == 0, set_result.output

    result = runner.invoke(init_app, ["prefs", "show", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 2
    assert payload["cursor"]["output_format"] == "stream-json"


def test_prefs_wizard_standalone_writes_nested_defaults(isolated_popola_home: Path) -> None:
    runner = CliRunner()
    # Accept every default in the expanded wizard and confirm submission.
    answers = "\n".join([""] * 28) + "\n"

    result = runner.invoke(init_app, ["prefs", "--wizard"], input=answers)

    assert result.exit_code == 0, result.output
    prefs = load_user_preferences_for_cli()
    assert prefs is not None
    assert prefs.schema_version == 2
    assert prefs.routing.default_runtime == "local"
    assert prefs.cursor.output_format == "text"
    assert prefs.dispatch.ambiguity_resolution == "prompt"
