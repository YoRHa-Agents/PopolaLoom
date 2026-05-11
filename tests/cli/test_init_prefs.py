"""Focused tests for v0.9.10 ``popola init prefs`` preferences support."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from popolaloom.cli.init_cmd import (
    app as init_app,
)
from popolaloom.cli.init_cmd import (
    load_user_preferences_for_cli,
    write_user_preferences_for_cli,
)
from popolaloom.daemon.main import UserPreferencesConfig, load_popolad_config


@pytest.fixture
def isolated_popola_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    home = tmp_path / "home"
    home.mkdir()
    popola_home = tmp_path / "popola"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("POPOLA_HOME", str(popola_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
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


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_missing_user_preferences_block_returns_none(isolated_popola_home: Path) -> None:
    """Absent file/block keeps v0.9.9 compatibility."""
    assert load_user_preferences_for_cli() is None
    config = load_popolad_config(isolated_popola_home / "popolad.toml")
    assert config.user_preferences is None


def test_present_user_preferences_block_loads(isolated_popola_home: Path) -> None:
    path = _write(
        isolated_popola_home / "popolad.toml",
        "[user_preferences]\n"
        'default_runtime = "cloud"\n'
        'cloud_target_priority = ["cursor-managed", "self-hosted"]\n'
        'default_local_cli = "claude"\n'
        'fallback_chain = ["claude", "codex"]\n'
        "hitl_enabled = false\n"
        "follow_devola_flow = true\n"
        "prompt_each_dispatch = true\n"
        'last_set_at = "2026-05-10T00:00:00Z"\n'
        'last_set_by = "ci"\n',
    )
    prefs = load_popolad_config(path).user_preferences
    assert prefs is not None
    assert prefs.default_runtime == "cloud"
    assert prefs.cloud_target_priority == ("cursor-managed", "self-hosted")
    assert prefs.default_local_cli == "claude"
    assert prefs.fallback_chain == ("claude", "codex")
    assert prefs.hitl_enabled is False
    assert prefs.follow_devola_flow is True
    assert prefs.prompt_each_dispatch is True
    assert prefs.last_set_by == "ci"


def test_invalid_present_user_preferences_field_raises(isolated_popola_home: Path) -> None:
    path = _write(
        isolated_popola_home / "popolad.toml",
        "[user_preferences]\n"
        'default_runtime = "sometimes"\n',
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(path)
    assert "default_runtime" in str(excinfo.value)


def test_prefs_set_writes_non_interactively(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    result = runner.invoke(
        init_app,
        [
            "prefs",
            "--set",
            "default_runtime=cloud",
            "--set",
            "cloud_target_priority=self-hosted,cursor-managed",
            "--set",
            "default_local_cli=claude",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    prefs = load_user_preferences_for_cli()
    assert prefs is not None
    assert prefs.default_runtime == "cloud"
    assert prefs.cloud_target_priority == ("self-hosted", "cursor-managed")
    assert prefs.default_local_cli == "claude"


def test_interactive_wizard_second_run_uses_current_preferences_as_defaults(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    write_user_preferences_for_cli(
        UserPreferencesConfig(
            default_runtime="cloud",
            cloud_target_priority=("cursor-managed", "self-hosted"),
            default_local_cli="codex",
            fallback_chain=("codex",),
            hitl_enabled=False,
            follow_devola_flow=True,
            prompt_each_dispatch=True,
            last_set_by="first-run",
        )
    )
    answers = [
        "y",  # Install cursor.
        "P",  # Project scope.
        "n",  # Claude.
        "n",  # Copilot.
        "n",  # Codex.
        "n",  # .local.
        "y",  # Proceed.
        "y",  # Configure preferences.
        "",  # default_runtime keeps cloud.
        "",  # priority keeps cursor-managed,self-hosted.
        "",  # default_local_cli keeps codex.
        "",  # fallback keeps codex.
        "",  # follow_devola_flow keeps true.
        "",  # hitl keeps false.
        "",  # prompt_each_dispatch keeps true.
    ]
    result = runner.invoke(init_app, ["--interactive"], input="\n".join(answers) + "\n")
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "Current [user_preferences]" in out
    prefs = load_user_preferences_for_cli()
    assert prefs is not None
    assert prefs.default_runtime == "cloud"
    assert prefs.cloud_target_priority == ("cursor-managed", "self-hosted")
    assert prefs.default_local_cli == "codex"
    assert prefs.fallback_chain == ("codex",)
    assert prefs.hitl_enabled is False
    assert prefs.follow_devola_flow is True
    assert prefs.prompt_each_dispatch is True


def test_prefs_show_json(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    write_user_preferences_for_cli(UserPreferencesConfig(default_runtime="cloud"))
    result = runner.invoke(init_app, ["prefs", "show", "--json"])
    assert result.exit_code == 0, _combined_output(result)
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 2
    assert payload["routing"]["default_runtime"] == "cloud"
    assert payload["routing"]["default_local_cli"] == "cursor"


def test_prefs_reset_deletes_block_and_preserves_other_sections(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    path = _write(
        isolated_popola_home / "popolad.toml",
        "[cloud.backoff]\nmax_retries = 3\n\n"
        "[user_preferences]\n"
        'default_runtime = "local"\n',
    )
    result = runner.invoke(init_app, ["prefs", "reset"])
    assert result.exit_code == 0, _combined_output(result)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    assert "user_preferences" not in raw
    assert raw["cloud"]["backoff"]["max_retries"] == 3


def test_prefs_set_preserves_old_toml_compatibility(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    path = _write(
        isolated_popola_home / "popolad.toml",
        "[hitl.cloud]\n"
        "timeout_seconds = 600\n"
        "idempotency_window_s = 7200\n",
    )
    result = runner.invoke(
        init_app,
        ["prefs", "--set", "default_local_cli=claude"],
    )
    assert result.exit_code == 0, _combined_output(result)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    assert raw["hitl"]["cloud"]["timeout_seconds"] == 600
    assert raw["hitl"]["cloud"]["idempotency_window_s"] == 7200
    assert raw["user_preferences"]["routing"]["default_local_cli"] == "claude"
