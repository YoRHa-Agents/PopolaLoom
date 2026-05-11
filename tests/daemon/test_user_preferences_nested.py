"""v1.1.0 nested ``[user_preferences]`` schema tests."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from popolaloom.daemon.main import (
    UserPreferencesConfig,
    load_popolad_config,
    user_preferences_to_toml_dict,
)


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_nested_preferences_loads(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "popolad.toml",
        """
[user_preferences]
schema_version = 2
last_set_by = "ci"

[user_preferences.routing]
default_runtime = "cloud"
default_local_cli = "claude"
fallback_chain = ["claude", "codex"]
cloud_target_priority = ["cursor-managed", "self-hosted"]

[user_preferences.defaults]
wait_timeout_s = 120
hitl_enabled = false
follow_devola_flow = true
prompt_each_dispatch = true

[user_preferences.cursor]
output_format = "stream-json"
cli_args = ["--foo"]

[user_preferences.cursor-cloud]
model = "gpt-5.5"
starting_ref = "release"
auto_create_pr = true
work_on_current_branch = true
skip_reviewer_request = true
default_cloud_target = "cursor-managed"
worker_name = "worker-a"

[user_preferences.claude]
max_turns = 5

[user_preferences.codex]
sandbox = "read-only"

[user_preferences.lark]
notify_on_completed = false
notify_on_failed = true
notify_on_canceled = false
notify_on_cancel_escalated = true
prompt_truncate = 42

[user_preferences.dispatch]
ambiguity_resolution = "use-defaults"
ask_dimensions = ["target", "model"]
""",
    )

    prefs = load_popolad_config(path).user_preferences

    assert prefs is not None
    assert prefs.schema_version == 2
    assert prefs.default_runtime == "cloud"
    assert prefs.cursor.output_format == "stream-json"
    assert prefs.cursor_cloud.model == "gpt-5.5"
    assert prefs.cursor_cloud.default_cloud_target == "cursor-managed"
    assert prefs.claude.max_turns == 5
    assert prefs.codex.sandbox == "read-only"
    assert prefs.lark.prompt_truncate == 42
    assert prefs.dispatch.ambiguity_resolution == "use-defaults"


def test_legacy_flat_preferences_auto_migrate_with_backup(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "popolad.toml",
        """
[user_preferences]
default_runtime = "cloud"
default_cloud_target = "self-hosted"
default_local_cli = "codex"
fallback_chain = ["cursor", "claude"]
hitl_enabled = false
""",
    )

    prefs = load_popolad_config(path).user_preferences

    assert prefs is not None
    assert prefs.schema_version == 2
    assert prefs.default_runtime == "cloud"
    assert prefs.default_cloud_target == "self-hosted"
    assert prefs.default_local_cli == "codex"
    assert prefs.fallback_chain == ("cursor", "claude")
    assert prefs.hitl_enabled is False
    assert path.with_name("popolad.toml.v1.bak").exists()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    assert raw["user_preferences"]["schema_version"] == 2
    assert raw["user_preferences"]["routing"]["default_runtime"] == "cloud"
    assert raw["user_preferences"]["cursor-cloud"]["default_cloud_target"] == "self-hosted"


def test_mixed_preferences_nested_wins_over_flat(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "popolad.toml",
        """
[user_preferences]
schema_version = 2
default_runtime = "cloud"

[user_preferences.routing]
default_runtime = "local"
""",
    )

    prefs = load_popolad_config(path).user_preferences

    assert prefs is not None
    assert prefs.default_runtime == "local"


def test_unknown_nested_key_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "popolad.toml",
        """
[user_preferences]
schema_version = 2

[user_preferences.cursor-cloud]
modle = "typo"
""",
    )

    with pytest.raises(ValueError, match="modle"):
        load_popolad_config(path)


def test_nested_type_validation(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "popolad.toml",
        """
[user_preferences]
schema_version = 2

[user_preferences.claude]
max_turns = "many"
""",
    )

    with pytest.raises(ValueError, match="max_turns"):
        load_popolad_config(path)


def test_serializer_emits_nested_v2() -> None:
    payload = user_preferences_to_toml_dict(
        UserPreferencesConfig(default_cloud_target="cursor-managed")
    )

    assert payload["schema_version"] == 2
    assert payload["cursor-cloud"]["default_cloud_target"] == "cursor-managed"
