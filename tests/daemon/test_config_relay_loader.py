"""v0.8.8 T2.2.1 — ``[cloud.relay]`` config loader tests.

Covers AC (h) per ``.local/.agent/active/v0.8.8-multi-run/PLAN.md`` §4.2
T2.2.1 + the spec ``relay-auto-safety.md`` §3.1 (locked-on bool keys) +
``relay-primitive.md`` §6 (schema) + PLAN.md §9 release-gate criterion C1.

Pinned invariants:

- Happy path — fully populated `[cloud.relay]` table loads into
  :class:`CloudRelayConfig` verbatim.
- Defaults when section / file absent — v0.8.5/v0.8.7 deployments
  without the new section keep working (mode="auto", repo_allowlist=()).
- Locked-on bool keys reject ``false`` with the **spec-locked error
  messages** (PLAN.md §9 box C1).
- Range strictness — ``prompt_size_cap_bytes`` and
  ``idempotency_window_s`` enforce documented bounds.
- Type strictness — ``bool`` rejected for int fields; non-string
  rejected for string fields; ``int`` rejected for bool fields.
- ``mode`` enum — only ``"auto"`` / ``"confirm"`` accepted.
- ``repo_allowlist`` entries — must match ``<org>/<repo>``; URL forms
  rejected; non-string entries rejected.
- Unknown keys — WARN log, NOT error (forward-compat).
- Section-shape strictness — ``[cloud.relay]`` must be a TOML table.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from popolaloom.daemon.main import (
    CLOUD_RELAY_IDEMPOTENCY_WINDOW_MAX_S,
    CLOUD_RELAY_IDEMPOTENCY_WINDOW_MIN_S,
    CLOUD_RELAY_PROMPT_SIZE_CAP_MAX,
    CLOUD_RELAY_PROMPT_SIZE_CAP_MIN,
    CloudRelayConfig,
    PopoladConfig,
    load_popolad_config,
)


def _write_toml(path: Path, body: str) -> Path:
    """Write ``body`` to ``path`` and return the path (helper for tmp_path)."""
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Happy path — every key parsed verbatim into CloudRelayConfig.
# ---------------------------------------------------------------------------


def test_cloud_relay_load_happy(tmp_path: Path) -> None:
    """Fully-populated ``[cloud.relay]`` table loads into a
    :class:`PopoladConfig` whose ``cloud.relay`` carries every field
    verbatim. Pins the public surface name + nesting.
    """
    p = _write_toml(
        tmp_path / "popolad.toml",
        '[cloud.relay]\n'
        'mode                              = "auto"\n'
        'repo_allowlist                    = ["neolix-ai/popola-loom", "neolix-ai/arktower"]\n'
        'prompt_size_cap_bytes             = 16384\n'
        'idempotency_window_s              = 3600\n'
        'audit_root                        = ""\n'
        'require_confirm_allowlist_flag    = true\n'
        'secret_scan_enabled               = true\n'
        'dry_run_emits_audit               = true\n',
    )
    config = load_popolad_config(p)
    assert isinstance(config, PopoladConfig)
    assert isinstance(config.cloud.relay, CloudRelayConfig)
    assert config.cloud.relay.mode == "auto"
    assert config.cloud.relay.repo_allowlist == (
        "neolix-ai/popola-loom",
        "neolix-ai/arktower",
    )
    assert config.cloud.relay.prompt_size_cap_bytes == 16_384
    assert config.cloud.relay.idempotency_window_s == 3600
    assert config.cloud.relay.audit_root == ""
    assert config.cloud.relay.require_confirm_allowlist_flag is True
    assert config.cloud.relay.secret_scan_enabled is True
    assert config.cloud.relay.dry_run_emits_audit is True


def test_cloud_relay_mode_confirm_accepted(tmp_path: Path) -> None:
    """``mode = "confirm"`` (operator opt-in to v0.8.5 behavior) loads."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        '[cloud.relay]\nmode = "confirm"\n',
    )
    config = load_popolad_config(p)
    assert config.cloud.relay.mode == "confirm"


def test_cloud_relay_audit_root_override(tmp_path: Path) -> None:
    """Non-default ``audit_root`` survives the loader (proves real reader)."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        '[cloud.relay]\naudit_root = "/var/log/popola/relay"\n',
    )
    config = load_popolad_config(p)
    assert config.cloud.relay.audit_root == "/var/log/popola/relay"


# ---------------------------------------------------------------------------
# Defaults when section / file absent.
# ---------------------------------------------------------------------------


def test_cloud_relay_defaults_when_section_absent(tmp_path: Path) -> None:
    """``popolad.toml`` without ``[cloud.relay]`` returns spec defaults."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[hitl.cloud]\ntimeout_seconds = 1800\n",
    )
    config = load_popolad_config(p)
    assert config.cloud.relay == CloudRelayConfig()
    assert config.cloud.relay.mode == "auto"
    assert config.cloud.relay.repo_allowlist == ()
    assert config.cloud.relay.prompt_size_cap_bytes == 16_384
    assert config.cloud.relay.idempotency_window_s == 3600


def test_cloud_relay_defaults_when_file_absent(tmp_path: Path) -> None:
    """No popolad.toml at all → relay defaults still apply."""
    p = tmp_path / "no-such-file.toml"
    assert not p.exists()
    config = load_popolad_config(p)
    assert config.cloud.relay == CloudRelayConfig()


def test_cloud_relay_defaults_when_section_empty(tmp_path: Path) -> None:
    """Empty ``[cloud.relay]`` table also returns defaults."""
    p = _write_toml(tmp_path / "popolad.toml", "[cloud.relay]\n")
    config = load_popolad_config(p)
    assert config.cloud.relay == CloudRelayConfig()


def test_cloud_relay_default_repo_allowlist_is_empty(tmp_path: Path) -> None:
    """**M1 default-deny invariant**: empty ``repo_allowlist`` is the
    spec default, blocking ALL relays out-of-the-box. Any change to
    this default is a release-gate violation per PLAN.md §9 box C1.
    """
    p = tmp_path / "no-such-file.toml"
    config = load_popolad_config(p)
    assert config.cloud.relay.repo_allowlist == ()


# ---------------------------------------------------------------------------
# C1 — locked-on bool keys reject ``false`` with spec-locked error messages.
# ---------------------------------------------------------------------------


def test_require_confirm_allowlist_flag_false_rejected(tmp_path: Path) -> None:
    """``require_confirm_allowlist_flag = false`` raises with the
    spec-locked error message — PLAN.md §9 box C1 evidence (M1)."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.relay]\nrequire_confirm_allowlist_flag = false\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    msg = str(excinfo.value)
    assert "require_confirm_allowlist_flag must stay true" in msg
    assert "Q-C-4 mitigation M1" in msg
    assert "v0.8.8 release lock" in msg


def test_secret_scan_enabled_false_rejected(tmp_path: Path) -> None:
    """``secret_scan_enabled = false`` raises with the spec-locked
    error message — PLAN.md §9 box C1 evidence (M3)."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.relay]\nsecret_scan_enabled = false\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    msg = str(excinfo.value)
    assert "secret_scan_enabled must stay true" in msg
    assert "Q-C-4 mitigation M3" in msg
    assert "v0.8.8 release lock" in msg


def test_dry_run_emits_audit_false_rejected(tmp_path: Path) -> None:
    """``dry_run_emits_audit = false`` raises with the spec-locked
    error message — PLAN.md §9 box C1 evidence (M2)."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.relay]\ndry_run_emits_audit = false\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    msg = str(excinfo.value)
    assert "dry_run_emits_audit must stay true" in msg
    assert "v0.8.8 release lock" in msg


def test_locked_bools_true_accepted(tmp_path: Path) -> None:
    """All three locked bools must accept ``true`` (the only legal value)."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.relay]\n"
        "require_confirm_allowlist_flag = true\n"
        "secret_scan_enabled = true\n"
        "dry_run_emits_audit = true\n",
    )
    config = load_popolad_config(p)
    assert config.cloud.relay.require_confirm_allowlist_flag is True
    assert config.cloud.relay.secret_scan_enabled is True
    assert config.cloud.relay.dry_run_emits_audit is True


# ---------------------------------------------------------------------------
# Range strictness — every numeric key has explicit bounds.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        CLOUD_RELAY_PROMPT_SIZE_CAP_MIN - 1,
        CLOUD_RELAY_PROMPT_SIZE_CAP_MAX + 1,
        0,
        -100,
    ],
)
def test_prompt_size_cap_out_of_range_rejected(tmp_path: Path, value: int) -> None:
    """``prompt_size_cap_bytes`` outside ``[1024, 1_048_576]`` rejects."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        f"[cloud.relay]\nprompt_size_cap_bytes = {value}\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    msg = str(excinfo.value)
    assert "prompt_size_cap_bytes" in msg
    assert "cloud.relay" in msg


@pytest.mark.parametrize(
    "value",
    [
        CLOUD_RELAY_IDEMPOTENCY_WINDOW_MIN_S - 1,
        CLOUD_RELAY_IDEMPOTENCY_WINDOW_MAX_S + 1,
        0,
        -100,
    ],
)
def test_idempotency_window_out_of_range_rejected(tmp_path: Path, value: int) -> None:
    """``idempotency_window_s`` outside ``[60, 86400]`` rejects."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        f"[cloud.relay]\nidempotency_window_s = {value}\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    msg = str(excinfo.value)
    assert "idempotency_window_s" in msg


def test_prompt_size_cap_boundary_accepted(tmp_path: Path) -> None:
    """Boundary values for ``prompt_size_cap_bytes`` accepted (inclusive)."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        f"[cloud.relay]\nprompt_size_cap_bytes = {CLOUD_RELAY_PROMPT_SIZE_CAP_MIN}\n",
    )
    config = load_popolad_config(p)
    assert config.cloud.relay.prompt_size_cap_bytes == CLOUD_RELAY_PROMPT_SIZE_CAP_MIN


# ---------------------------------------------------------------------------
# Type strictness — bool rejected for int; non-string rejected for str fields.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        '"5"',  # string
        "true",  # bool
        "false",  # bool
        "[1, 2]",  # array
    ],
)
def test_prompt_size_cap_invalid_type_rejected(tmp_path: Path, value: str) -> None:
    """Wrong-type ``prompt_size_cap_bytes`` rejects with int-type hint."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        f"[cloud.relay]\nprompt_size_cap_bytes = {value}\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    msg = str(excinfo.value).lower()
    assert "prompt_size_cap_bytes" in msg
    assert "integer" in msg or "int" in msg


def test_idempotency_window_bool_rejected(tmp_path: Path) -> None:
    """``idempotency_window_s = true`` rejected (defensive against bool ⊆ int)."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.relay]\nidempotency_window_s = true\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    assert "idempotency_window_s" in str(excinfo.value)


@pytest.mark.parametrize(
    "value",
    [
        "0",  # int
        "1",  # int
        '"true"',  # string
        "[true]",  # array
    ],
)
def test_locked_bool_invalid_type_rejected(tmp_path: Path, value: str) -> None:
    """Wrong-type ``require_confirm_allowlist_flag`` rejects with bool hint."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        f"[cloud.relay]\nrequire_confirm_allowlist_flag = {value}\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    msg = str(excinfo.value).lower()
    assert "require_confirm_allowlist_flag" in msg
    assert "bool" in msg


def test_audit_root_non_string_rejected(tmp_path: Path) -> None:
    """Non-string ``audit_root`` rejects (TOML int/array/etc.)."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.relay]\naudit_root = 12345\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    assert "audit_root" in str(excinfo.value)


# ---------------------------------------------------------------------------
# ``mode`` enum strictness.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("invalid_mode", ["AUTO", "Auto", "manual", "yes", ""])
def test_mode_invalid_value_rejected(tmp_path: Path, invalid_mode: str) -> None:
    """Only ``"auto"`` / ``"confirm"`` accepted for ``mode``."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        f'[cloud.relay]\nmode = "{invalid_mode}"\n',
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    assert "mode" in str(excinfo.value)


def test_mode_non_string_rejected(tmp_path: Path) -> None:
    """``mode = true`` / non-string rejects with type hint."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.relay]\nmode = true\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    assert "mode" in str(excinfo.value)


# ---------------------------------------------------------------------------
# ``repo_allowlist`` entry validation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_entry",
    [
        "https://github.com/foo/bar",  # URL form
        "foo",  # missing slash
        "foo/bar/baz",  # multi-segment
        "*/repo",  # glob
        "foo bar/repo",  # space
        "",  # empty
    ],
)
def test_repo_allowlist_invalid_entry_rejected(tmp_path: Path, bad_entry: str) -> None:
    """``repo_allowlist`` entries MUST match ``<org>/<repo>`` short form."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        f'[cloud.relay]\nrepo_allowlist = ["{bad_entry}"]\n',
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    assert "repo_allowlist" in str(excinfo.value)


def test_repo_allowlist_non_string_entry_rejected(tmp_path: Path) -> None:
    """Non-string entries in ``repo_allowlist`` rejected (e.g. int)."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.relay]\nrepo_allowlist = [123]\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    assert "repo_allowlist" in str(excinfo.value)


def test_repo_allowlist_non_list_rejected(tmp_path: Path) -> None:
    """``repo_allowlist`` MUST be a list, not a string scalar."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        '[cloud.relay]\nrepo_allowlist = "neolix-ai/popola-loom"\n',
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    assert "repo_allowlist" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Unknown keys — WARN log, NOT error.
# ---------------------------------------------------------------------------


def test_unknown_keys_warn_not_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown keys under ``[cloud.relay]`` (e.g. operator typo) WARN."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.relay]\n"
        'mode = "auto"\n'
        "repos_allowlist = []\n"  # typo (should be repo_allowlist)
        'allow_secret_shapes_default = "jwt"\n',  # future key
    )
    with caplog.at_level(logging.WARNING):
        config = load_popolad_config(p)
    assert config.cloud.relay.mode == "auto"
    warn_msgs = [
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert any("repos_allowlist" in m for m in warn_msgs), (
        f"WARN must mention typo'd key: {warn_msgs!r}"
    )


# ---------------------------------------------------------------------------
# Section-shape strictness.
# ---------------------------------------------------------------------------


def test_cloud_relay_section_must_be_table(tmp_path: Path) -> None:
    """Scalar ``[cloud] relay = "..."`` rejects."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        '[cloud]\nrelay = "not-a-table"\n',
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    assert "cloud.relay" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Backward compat — popolad.toml with only [hitl.cloud] still loads cleanly.
# ---------------------------------------------------------------------------


def test_v087_config_still_loads_with_v088_relay_loader(tmp_path: Path) -> None:
    """v0.8.7-era popolad.toml (no ``[cloud.relay]``) loads cleanly under
    the v0.8.8 T2.2.1 loader. Pins backwards compatibility."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[hitl.cloud]\n"
        "timeout_seconds = 600\n"
        "idempotency_window_s = 7200\n",
    )
    config = load_popolad_config(p)
    assert config.hitl.cloud.timeout_seconds == 600
    assert config.cloud.relay == CloudRelayConfig()
