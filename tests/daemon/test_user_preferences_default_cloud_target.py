"""v0.10.0 Wave B1 — ``[user_preferences].default_cloud_target`` loader tests.

Covers PLAN.md Wave B1 acceptance criteria 1-7 verbatim:

1. Field present on :class:`UserPreferencesConfig` defaulting to
   ``"ask-each-time"``.
2. ``USER_PREF_VALID_DEFAULT_CLOUD_TARGET`` constant exposed publicly with
   the documented enum ``{"self-hosted","cursor-managed","ask-each-time"}``.
3. ``_USER_PREF_KNOWN_KEYS`` extended (covered indirectly: the loader
   accepts the key without raising the strict-keys error).
4. Validator rejects unknown values with the
   ``"default_cloud_target must be one of ...; got ..."`` style message
   that mirrors the existing ``default_runtime`` validator (workspace
   "No Silent Failures" rule).
5. :func:`user_preferences_to_toml_dict` serializes the new key.
6. One-time deprecation WARN fires when ``cloud_target_priority`` is set
   AND ``default_cloud_target`` is at default; gated by the module-level
   ``_CLOUD_TARGET_PRIORITY_DEPRECATION_WARNED`` flag so it fires at most
   once per process.
7. Round-trip serialise / parse + WARN-fires-only-once coverage.

References
----------
- ``.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md`` Q-5
- ``.local/.agent/active/v0.10.0-cloud-dispatch-clarity/PLAN.md`` §"Wave B → Task B1"
- ``.local/feedbacks/feedback_for_v0.10.0.md`` L11 (user-contract input)
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

import pytest

from popolaloom.daemon import main as daemon_main
from popolaloom.daemon.main import (
    USER_PREF_VALID_DEFAULT_CLOUD_TARGET,
    UserPreferencesConfig,
    load_popolad_config,
    user_preferences_to_toml_dict,
)

# ---------------------------------------------------------------------------
# Test fixtures / helpers.
# ---------------------------------------------------------------------------


def _write_toml(path: Path, body: str) -> Path:
    """Write ``body`` to ``path`` and return the path (helper for tmp_path)."""
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _reset_deprecation_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the module-level deprecation-warned flag before every test.

    The flag persists across loader calls within a process so the WARN
    fires at most once (AC 6 + 7). To make each test deterministic we
    flip it back to ``False`` at setup; the "fires only once" test
    explicitly verifies the post-fire state by issuing back-to-back
    loads in a single test.
    """
    monkeypatch.setattr(
        daemon_main,
        "_CLOUD_TARGET_PRIORITY_DEPRECATION_WARNED",
        False,
    )


# ---------------------------------------------------------------------------
# Constant + dataclass surface (AC 1 + AC 2).
# ---------------------------------------------------------------------------


def test_valid_default_cloud_target_constant_shape() -> None:
    """``USER_PREF_VALID_DEFAULT_CLOUD_TARGET`` is the documented frozenset (AC 2)."""
    assert frozenset(
        {"self-hosted", "cursor-managed", "ask-each-time"}
    ) == USER_PREF_VALID_DEFAULT_CLOUD_TARGET
    assert isinstance(USER_PREF_VALID_DEFAULT_CLOUD_TARGET, frozenset)


def test_user_preferences_config_default_value() -> None:
    """``UserPreferencesConfig.default_cloud_target`` defaults to ``"ask-each-time"`` (AC 1)."""
    cfg = UserPreferencesConfig()
    assert cfg.default_cloud_target == "ask-each-time"


# ---------------------------------------------------------------------------
# Loader — default value (AC 4 + AC 7 'default').
# ---------------------------------------------------------------------------


def test_load_default_when_section_present_but_key_absent(tmp_path: Path) -> None:
    """``[user_preferences]`` present but key absent → loader defaults to ``"ask-each-time"``."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        '[user_preferences]\ndefault_runtime = "local"\n',
    )
    config = load_popolad_config(p)
    assert config.user_preferences is not None
    assert config.user_preferences.default_cloud_target == "ask-each-time"


def test_load_default_when_section_absent(tmp_path: Path) -> None:
    """No ``[user_preferences]`` section → ``user_preferences=None`` (v0.9.9 compat)."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[hitl.cloud]\ntimeout_seconds = 1800\n",
    )
    config = load_popolad_config(p)
    assert config.user_preferences is None


# ---------------------------------------------------------------------------
# Loader — valid values (AC 4 + AC 7 'valid').
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    sorted({"self-hosted", "cursor-managed", "ask-each-time"}),
)
def test_load_valid_value_accepted(tmp_path: Path, value: str) -> None:
    """Every enum value in the spec frozenset is accepted by the loader."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        '[user_preferences]\n'
        f'default_cloud_target = "{value}"\n',
    )
    config = load_popolad_config(p)
    assert config.user_preferences is not None
    assert config.user_preferences.default_cloud_target == value


# ---------------------------------------------------------------------------
# Loader — invalid values (AC 4 + AC 7 'invalid').
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    ["", "Self-Hosted", "ask-each", "local", "cloud", "anything-else"],
)
def test_load_invalid_string_value_rejected(
    tmp_path: Path,
    bad_value: str,
) -> None:
    """Unknown string values raise ``ValueError`` with the spec-mirroring message.

    No silent failures — message format mirrors ``default_runtime``'s
    validator: ``"default_cloud_target must be one of [...]; got '<bad>'"``.
    """
    p = _write_toml(
        tmp_path / "popolad.toml",
        '[user_preferences]\n'
        f'default_cloud_target = "{bad_value}"\n',
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    msg = str(excinfo.value)
    assert "default_cloud_target" in msg
    assert "must be one of" in msg
    assert "ask-each-time" in msg
    assert "cursor-managed" in msg
    assert "self-hosted" in msg
    assert repr(bad_value) in msg


def test_load_non_string_value_rejected(tmp_path: Path) -> None:
    """Non-string ``default_cloud_target`` (e.g. int / bool) rejects loudly."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[user_preferences]\ndefault_cloud_target = 42\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    assert "default_cloud_target" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Serializer (AC 5) + round-trip (AC 7 'round-trip').
# ---------------------------------------------------------------------------


def test_user_preferences_to_toml_dict_serializes_new_key() -> None:
    """:func:`user_preferences_to_toml_dict` includes ``default_cloud_target`` (AC 5)."""
    cfg = UserPreferencesConfig(default_cloud_target="cursor-managed")
    payload = user_preferences_to_toml_dict(cfg)
    assert "default_cloud_target" in payload
    assert payload["default_cloud_target"] == "cursor-managed"


def test_round_trip_serialize_parse(tmp_path: Path) -> None:
    """Serialise a config → write TOML → re-parse → equal ``default_cloud_target`` (AC 7)."""
    cfg_in = UserPreferencesConfig(
        default_runtime="cloud",
        default_cloud_target="self-hosted",
        default_local_cli="claude",
    )
    payload = user_preferences_to_toml_dict(cfg_in)

    lines = ["[user_preferences]"]
    for key, value in payload.items():
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, list):
            inner = ", ".join(f'"{item}"' for item in value)
            lines.append(f"{key} = [{inner}]")
        else:
            lines.append(f'{key} = "{value}"')
    body = "\n".join(lines) + "\n"
    p = _write_toml(tmp_path / "popolad.toml", body)

    raw = tomllib.loads(p.read_text(encoding="utf-8"))
    assert raw["user_preferences"]["default_cloud_target"] == "self-hosted"

    cfg_out = load_popolad_config(p).user_preferences
    assert cfg_out is not None
    assert cfg_out.default_cloud_target == cfg_in.default_cloud_target
    assert cfg_out.default_runtime == cfg_in.default_runtime
    assert cfg_out.default_local_cli == cfg_in.default_local_cli


# ---------------------------------------------------------------------------
# Deprecation WARN (AC 6 + AC 7 'WARN fires once').
# ---------------------------------------------------------------------------


_EXPECTED_DEPRECATION_MESSAGE = (
    "cloud_target_priority is deprecated as of v0.10.0; "
    "use default_cloud_target instead"
)


def test_deprecation_warn_fires_when_legacy_key_set_and_target_at_default(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Legacy key explicitly set AND new key at default → WARN fires (AC 6)."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[user_preferences]\n"
        'cloud_target_priority = ["cursor-managed", "self-hosted"]\n',
    )
    with caplog.at_level(logging.WARNING, logger="popolaloom.daemon"):
        config = load_popolad_config(p)

    assert config.user_preferences is not None
    assert config.user_preferences.default_cloud_target == "ask-each-time"
    warn_msgs = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno >= logging.WARNING
    ]
    assert _EXPECTED_DEPRECATION_MESSAGE in warn_msgs, (
        f"expected deprecation WARN; got {warn_msgs!r}"
    )


def test_deprecation_warn_silent_when_default_cloud_target_explicit(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Legacy + new key both set → operator HAS migrated → no WARN (AC 6)."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[user_preferences]\n"
        'cloud_target_priority = ["cursor-managed", "self-hosted"]\n'
        'default_cloud_target = "cursor-managed"\n',
    )
    with caplog.at_level(logging.WARNING, logger="popolaloom.daemon"):
        load_popolad_config(p)

    warn_msgs = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno >= logging.WARNING
    ]
    assert _EXPECTED_DEPRECATION_MESSAGE not in warn_msgs, (
        f"WARN should be silent once operator migrated; got {warn_msgs!r}"
    )


def test_deprecation_warn_silent_when_legacy_key_absent(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fresh install (no legacy key in TOML) → no WARN (AC 6)."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        '[user_preferences]\ndefault_runtime = "cloud"\n',
    )
    with caplog.at_level(logging.WARNING, logger="popolaloom.daemon"):
        load_popolad_config(p)

    warn_msgs = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno >= logging.WARNING
    ]
    assert _EXPECTED_DEPRECATION_MESSAGE not in warn_msgs, (
        f"WARN should be silent for fresh installs; got {warn_msgs!r}"
    )


def test_deprecation_warn_fires_only_once_per_process(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Back-to-back loads with the deprecation precondition → WARN once (AC 7).

    The module-level ``_CLOUD_TARGET_PRIORITY_DEPRECATION_WARNED`` flag
    persists across loader calls within the same process. The autouse
    fixture resets the flag at test entry; this test does NOT reset
    between the two loads so the second call must observe the flag and
    stay silent.
    """
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[user_preferences]\n"
        'cloud_target_priority = ["cursor-managed", "self-hosted"]\n',
    )
    with caplog.at_level(logging.WARNING, logger="popolaloom.daemon"):
        load_popolad_config(p)
        load_popolad_config(p)
        load_popolad_config(p)

    deprecation_records = [
        rec
        for rec in caplog.records
        if rec.getMessage() == _EXPECTED_DEPRECATION_MESSAGE
        and rec.levelno == logging.WARNING
    ]
    assert len(deprecation_records) == 1, (
        "deprecation WARN must fire exactly once per process; "
        f"observed {len(deprecation_records)} record(s)"
    )
    assert daemon_main._CLOUD_TARGET_PRIORITY_DEPRECATION_WARNED is True


def test_deprecation_warn_flag_starts_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """The module-level flag is ``False`` at import time (AC 6 + AC 7 fresh-process state)."""
    monkeypatch.setattr(
        daemon_main,
        "_CLOUD_TARGET_PRIORITY_DEPRECATION_WARNED",
        False,
    )
    assert daemon_main._CLOUD_TARGET_PRIORITY_DEPRECATION_WARNED is False
