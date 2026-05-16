"""v1.3.0 P6 — UserPrefsCursorCloud.default_* + UserPrefsCursor.default_model persistence.

Mirrors the test pattern in :mod:`tests.cli.test_init_prefs_v111`:

- Override ``$POPOLA_HOME`` so writes go to a tmp dir.
- Use :class:`typer.testing.CliRunner` to invoke ``popola init prefs --set``.
- Read the resulting ``popolad.toml`` via :mod:`tomllib` to assert
  the new ``cursor.default_model`` + ``cursor-cloud.default_*`` keys
  round-trip via the writer/loader pair extended in Step 1e.

Acceptance criteria from PLAN.md §Patch P6 are exercised by:

1. :func:`test_set_default_preset_grind_roundtrips`
2. :func:`test_set_default_thinking_level_roundtrips`
3. :func:`test_set_default_model_cursor_local_roundtrips`
4. :func:`test_set_invalid_preset_rejected`
5. :func:`test_set_boolean_defaults_roundtrip`
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from popolaloom.cli.init_cmd import app as init_app
from popolaloom.daemon.main import (
    USER_PREF_VALID_AGENT_MODES,
    USER_PREF_VALID_PRESETS,
)


@pytest.fixture
def popola_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "popola"
    home.mkdir()
    monkeypatch.setenv("POPOLA_HOME", str(home))
    yield home


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


def test_set_default_preset_grind_roundtrips(popola_home: Path) -> None:
    """Persist ``cursor-cloud.default_preset=grind`` to popolad.toml."""
    runner = CliRunner()
    result = runner.invoke(
        init_app,
        ["prefs", "--set", "cursor-cloud.default_preset=grind"],
    )
    assert result.exit_code == 0, _combined_output(result)
    toml_path = popola_home / "popolad.toml"
    assert toml_path.exists()
    data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    assert data["user_preferences"]["cursor-cloud"]["default_preset"] == "grind"


def test_set_default_thinking_level_roundtrips(popola_home: Path) -> None:
    """Persist ``cursor-cloud.default_thinking_level=high`` to popolad.toml."""
    runner = CliRunner()
    result = runner.invoke(
        init_app,
        ["prefs", "--set", "cursor-cloud.default_thinking_level=high"],
    )
    assert result.exit_code == 0, _combined_output(result)
    data = tomllib.loads((popola_home / "popolad.toml").read_text(encoding="utf-8"))
    assert data["user_preferences"]["cursor-cloud"]["default_thinking_level"] == "high"


def test_set_default_model_cursor_local_roundtrips(popola_home: Path) -> None:
    """Persist ``cursor.default_model=gpt-5.5`` to popolad.toml.

    v1.3.0 P6 — the local cursor adapter forwards the persisted default
    via ``--model`` whenever the per-task ``--model`` flag is empty
    (see :func:`popolaloom.cli.main.dispatch` cursor branch).
    """
    runner = CliRunner()
    result = runner.invoke(
        init_app,
        ["prefs", "--set", "cursor.default_model=gpt-5.5"],
    )
    assert result.exit_code == 0, _combined_output(result)
    data = tomllib.loads((popola_home / "popolad.toml").read_text(encoding="utf-8"))
    assert data["user_preferences"]["cursor"]["default_model"] == "gpt-5.5"


def test_set_invalid_preset_rejected(popola_home: Path) -> None:
    """``--set cursor-cloud.default_preset=banana`` exits non-zero."""
    runner = CliRunner()
    result = runner.invoke(
        init_app,
        ["prefs", "--set", "cursor-cloud.default_preset=banana"],
    )
    assert result.exit_code != 0
    output = _combined_output(result)
    if result.exception is not None:
        output += "\n" + str(result.exception)
    lowered = output.lower()
    assert "banana" in output or "preset" in lowered
    assert "grind" in lowered or "must be one of" in lowered


def test_set_boolean_defaults_roundtrip(popola_home: Path) -> None:
    """Persist three boolean Path-B defaults in one invocation."""
    runner = CliRunner()
    result = runner.invoke(
        init_app,
        [
            "prefs",
            "--set", "cursor-cloud.default_max_mode=true",
            "--set", "cursor-cloud.default_long_running=true",
            "--set", "cursor-cloud.default_auto_proceed_after_plan=false",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    data = tomllib.loads((popola_home / "popolad.toml").read_text(encoding="utf-8"))
    cc = data["user_preferences"]["cursor-cloud"]
    assert cc["default_max_mode"] is True
    assert cc["default_long_running"] is True
    assert cc["default_auto_proceed_after_plan"] is False


def test_user_pref_constants_have_expected_members() -> None:
    """v1.3.0 P6 — schema constants list ``grind`` + ``plan``."""
    assert "grind" in USER_PREF_VALID_PRESETS
    assert "plan" in USER_PREF_VALID_AGENT_MODES
