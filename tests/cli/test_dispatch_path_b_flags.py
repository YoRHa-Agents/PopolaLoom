"""Tests for `popola dispatch` path-B flags (S5 + S4-C, v1.0.0 GA).

Per .local/.agent/active/v1.0.0-ga/DECISIONS.md:
- Q-13 (LOCKED): --auth-mode=rest is the default; session-jwt is opt-in.
- Q-17 (LOCKED): preset catalog (quick-fix, long-running-plan, exploration, review).
- Q-18 (LOCKED): --time-budget accepts integer-seconds | <int>s | <int>m | <int>h.
- Q-19 (LOCKED): path-B flags REJECT (exit 2) when --auth-mode=rest.
- Q-22 (LOCKED): path-B is experimental until popolad wires Connect-RPC.

``--auth-mode=session-jwt`` on ``cursor-cloud`` currently hard-exits: the supervisor
still uses REST ``POST /v1/agents`` (see ``_apply_path_b_flags`` docstring).
"""

from __future__ import annotations

import logging

import pytest
from typer.testing import CliRunner

from popolaloom.cli.main import (
    _BUILTIN_PRESETS,
    _EXIT_INVALID_ARGS,
    _apply_path_b_flags,
    _apply_preset,
    _parse_time_budget,
)
from popolaloom.cli.main import (
    app as main_app,
)

# ── _parse_time_budget (Q-18) ───────────────────────────────────────


def test_parse_time_budget_seconds_default() -> None:
    assert _parse_time_budget("60") == 60


def test_parse_time_budget_explicit_s_suffix() -> None:
    assert _parse_time_budget("90s") == 90


def test_parse_time_budget_minutes_suffix() -> None:
    assert _parse_time_budget("30m") == 1800


def test_parse_time_budget_hours_suffix() -> None:
    assert _parse_time_budget("1h") == 3600


def test_parse_time_budget_empty_returns_zero() -> None:
    assert _parse_time_budget("") == 0


def test_parse_time_budget_rejects_unparseable() -> None:
    import typer

    with pytest.raises(typer.BadParameter):
        _parse_time_budget("90q")
    with pytest.raises(typer.BadParameter):
        _parse_time_budget("forever")


# ── _apply_preset (Q-17) ────────────────────────────────────────────


def test_apply_preset_known_value_expands() -> None:
    """Known preset → expanded dict; explicit overrides win."""
    out = _apply_preset({}, "quick-fix", explicit={})
    assert out["mode"] == "agent"
    assert out["effort"] == "medium"
    assert out["time_budget"] == "600s"


def test_apply_preset_explicit_overrides_preset() -> None:
    """Explicit per-task flags override preset values (Q-17)."""
    out = _apply_preset({}, "long-running-plan", explicit={"mode": "ask"})
    assert out["mode"] == "ask"  # explicit wins
    assert out["long_running"] is True  # preset value retained


def test_apply_preset_long_running_plan_flag_combo() -> None:
    out = _apply_preset({}, "long-running-plan", explicit={})
    assert out["mode"] == "plan"
    assert out["effort"] == "high"
    assert out["time_budget"] == "3600s"
    assert out["long_running"] is True
    assert out["auto_proceed_after_plan"] is True


def test_apply_preset_unknown_exits_2() -> None:
    """Unknown preset → exit 2 (No Silent Failures)."""
    with pytest.raises(Exception) as exc_info:
        _apply_preset({}, "yolo", explicit={})
    assert getattr(exc_info.value, "exit_code", None) == _EXIT_INVALID_ARGS


def test_apply_preset_empty_returns_explicit_unchanged() -> None:
    """Empty preset name → explicit dict returned unchanged."""
    explicit = {"mode": "agent"}
    assert _apply_preset({}, "", explicit=explicit) is explicit


def test_builtin_presets_catalog_has_required_entries() -> None:
    """The 4 Q-17 preset names are all present."""
    for name in ("quick-fix", "long-running-plan", "exploration", "review"):
        assert name in _BUILTIN_PRESETS


# ── _apply_path_b_flags (Q-13 + Q-19) ───────────────────────────────


def test_apply_path_b_flags_rest_default_no_path_b_flags_is_noop() -> None:
    """Default --auth-mode=rest with NO path-B flags → no extras pollution."""
    extra: dict[str, object] = {}
    _apply_path_b_flags(
        extra,
        cli="cursor-cloud",
        auth_mode="rest",
        mode="",
        max_mode=False,
        effort="",
        time_budget="",
        long_running=False,
        auto_proceed_after_plan=False,
        preset="",
    )
    assert extra == {}


def test_apply_path_b_flags_rest_with_mode_rejects() -> None:
    """Q-19: --mode under --auth-mode=rest → exit 2 with bilingual hint."""
    extra: dict[str, object] = {}
    with pytest.raises(Exception) as exc_info:
        _apply_path_b_flags(
            extra,
            cli="cursor-cloud",
            auth_mode="rest",
            mode="plan",
            max_mode=False,
            effort="",
            time_budget="",
            long_running=False,
            auto_proceed_after_plan=False,
            preset="",
        )
    assert getattr(exc_info.value, "exit_code", None) == _EXIT_INVALID_ARGS


def test_apply_path_b_flags_session_jwt_exits_until_wired() -> None:
    """session-jwt rejects after validation so extras are not silently dropped downstream."""
    extra: dict[str, object] = {}
    with pytest.raises(Exception) as exc_info:
        _apply_path_b_flags(
            extra,
            cli="cursor-cloud",
            auth_mode="session-jwt",
            mode="plan",
            max_mode=True,
            effort="high",
            time_budget="30m",
            long_running=True,
            auto_proceed_after_plan=True,
            preset="",
        )
    assert getattr(exc_info.value, "exit_code", None) == _EXIT_INVALID_ARGS
    assert extra == {}


def test_apply_path_b_flags_preset_under_session_jwt_exits_until_wired() -> None:
    with pytest.raises(Exception) as exc_info:
        _apply_path_b_flags(
            {},
            cli="cursor-cloud",
            auth_mode="session-jwt",
            mode="",
            max_mode=False,
            effort="",
            time_budget="",
            long_running=False,
            auto_proceed_after_plan=False,
            preset="long-running-plan",
        )
    assert getattr(exc_info.value, "exit_code", None) == _EXIT_INVALID_ARGS


def test_apply_path_b_flags_invalid_auth_mode_exits_2() -> None:
    """Unknown --auth-mode → exit 2."""
    with pytest.raises(Exception) as exc_info:
        _apply_path_b_flags(
            {},
            cli="cursor-cloud",
            auth_mode="bogus",
            mode="",
            max_mode=False,
            effort="",
            time_budget="",
            long_running=False,
            auto_proceed_after_plan=False,
            preset="",
        )
    assert getattr(exc_info.value, "exit_code", None) == _EXIT_INVALID_ARGS


def test_apply_path_b_flags_non_cursor_cloud_cli_warns_and_drops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Path-B flags on --cli=cursor (not cursor-cloud) are dropped with WARN."""
    extra: dict[str, object] = {}
    with caplog.at_level(logging.WARNING, logger="popolaloom.cli.main"):
        _apply_path_b_flags(
            extra,
            cli="cursor",
            auth_mode="session-jwt",
            mode="plan",
            max_mode=False,
            effort="",
            time_budget="",
            long_running=False,
            auto_proceed_after_plan=False,
            preset="",
        )
    assert "auth_mode" not in extra
    assert any(
        "Path-B/session-jwt" in rec.message and "cursor" in rec.message
        for rec in caplog.records
    )


# ── Typer command signature smoke ───────────────────────────────────


def test_dispatch_command_exposes_all_path_b_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All 8 path-B flags + --auth-mode appear in `popola dispatch --help`.

    Forces COLUMNS=200 so Typer's rich-format help table does not truncate
    flag names (CI default 80-col would render ``--auth-mode`` as
    ``--auth…``).
    """
    monkeypatch.setenv("COLUMNS", "200")
    runner = CliRunner()
    result = runner.invoke(main_app, ["dispatch", "--help"])
    assert result.exit_code == 0, result.output
    output = result.output
    for needle in (
        "--auth-mode",
        "--mode",
        "--max-mode",
        "--effort",
        "--time-budget",
        "--long-running",
        "--auto-proceed-after",  # Typer/help may ellipsis the full spelling
        "--preset",
    ):
        assert needle in output, f"missing {needle} in dispatch --help"


def test_dispatch_help_marks_path_b_as_experimental(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The --auth-mode help text labels session-jwt as experimental."""
    monkeypatch.setenv("COLUMNS", "200")
    runner = CliRunner()
    result = runner.invoke(main_app, ["dispatch", "--help"])
    assert "EXPERIMENTAL" in result.output
