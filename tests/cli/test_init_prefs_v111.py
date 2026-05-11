"""v1.1.1 preferences metadata, Rich rendering, and doctor detail tests."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from popolaloom.cli.init_cmd import app as init_app
from popolaloom.cli.main import app as main_app


@pytest.fixture
def isolated_popola_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    popola_home = tmp_path / "popola"
    popola_home.mkdir()
    monkeypatch.setenv("POPOLA_HOME", str(popola_home))
    yield popola_home


def _combined_output(result: object) -> str:
    stdout = getattr(result, "stdout", "") or ""
    try:
        stderr = getattr(result, "stderr", "") or ""
    except (ValueError, AttributeError):
        stderr = ""
    output = getattr(result, "output", "") or ""
    return stdout + stderr + output


def test_prefs_set_auto_stamps_last_set_metadata(isolated_popola_home: Path) -> None:
    result = CliRunner().invoke(
        init_app,
        ["prefs", "--set", "routing.default_runtime=cloud"],
    )

    out = _combined_output(result)
    assert result.exit_code == 0, out
    raw = tomllib.loads((isolated_popola_home / "popolad.toml").read_text(encoding="utf-8"))
    prefs = raw["user_preferences"]

    datetime.fromisoformat(prefs["last_set_at"])
    assert re.match(r"^.+@.+ via popola \d+\.\d+\.\d+$", prefs["last_set_by"])


def test_prefs_show_renders_user_preferences_sections_literally(
    isolated_popola_home: Path,
) -> None:
    runner = CliRunner()
    set_result = runner.invoke(init_app, ["prefs", "--set", "routing.default_runtime=cloud"])
    assert set_result.exit_code == 0, _combined_output(set_result)

    result = runner.invoke(init_app, ["prefs", "show"])

    out = _combined_output(result)
    assert result.exit_code == 0, out
    assert "[user_preferences]" in out
    assert "[user_preferences.routing]" in out


def test_doctor_user_preferences_row_includes_last_set_at(
    isolated_popola_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    set_result = runner.invoke(init_app, ["prefs", "--set", "routing.default_runtime=cloud"])
    assert set_result.exit_code == 0, _combined_output(set_result)
    raw = tomllib.loads((isolated_popola_home / "popolad.toml").read_text(encoding="utf-8"))
    last_set_at = raw["user_preferences"]["last_set_at"]
    monkeypatch.setattr("popolaloom.cli.doctor_cmd.shutil.which", lambda _: None)

    result = runner.invoke(main_app, ["doctor", "--json"])

    out = _combined_output(result)
    assert result.exit_code == 0, out
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    row = next(item for item in payload["preferences"] if item["name"] == "schema")
    assert last_set_at in row["detail"]
