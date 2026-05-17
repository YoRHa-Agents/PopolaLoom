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
from pathlib import Path

import pytest

from popolaloom.cli.main import (
    _BUILTIN_PRESETS,
    _EXIT_INVALID_ARGS,
    _apply_path_b_flags,
    _apply_preset,
    _parse_time_budget,
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


def test_apply_preset_loads_user_overlay_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom preset from ~/.config/popola/presets.toml overlays the built-ins."""
    monkeypatch.setenv("HOME", str(tmp_path))
    overlay_dir = tmp_path / ".config" / "popola"
    overlay_dir.mkdir(parents=True)
    (overlay_dir / "presets.toml").write_text(
        '[team-rapid]\nmode = "agent"\neffort = "low"\ntime_budget = "300s"\n'
    )
    out = _apply_preset({}, "team-rapid", explicit={})
    assert out["mode"] == "agent"
    assert out["effort"] == "low"
    assert out["time_budget"] == "300s"


def test_apply_preset_corrupt_overlay_falls_back_to_builtins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Corrupt TOML overlay → WARN + fall back to built-ins (No Silent Failures)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    overlay_dir = tmp_path / ".config" / "popola"
    overlay_dir.mkdir(parents=True)
    (overlay_dir / "presets.toml").write_text("this is not valid TOML at all =")
    with caplog.at_level(logging.WARNING, logger="popolaloom.cli.main"):
        out = _apply_preset({}, "quick-fix", explicit={})
    assert out["mode"] == "agent"  # built-in preserved
    assert any("failed to load custom presets" in r.message for r in caplog.records)


def test_apply_preset_overlay_ignores_non_dict_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Top-level non-table entries in presets.toml are silently ignored."""
    monkeypatch.setenv("HOME", str(tmp_path))
    overlay_dir = tmp_path / ".config" / "popola"
    overlay_dir.mkdir(parents=True)
    (overlay_dir / "presets.toml").write_text(
        'version = "1"\n[only-this-counts]\nmode = "plan"\n'
    )
    out = _apply_preset({}, "only-this-counts", explicit={})
    assert out["mode"] == "plan"


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
    """v1.1.0 (Track 6) — session-jwt is now WIRED through popolad.

    The historical assertion (exit ``_EXIT_INVALID_ARGS`` because
    ``--auth-mode=session-jwt`` was not yet wired) is obsolete; v1.1.0
    Track 6 wires the Connect-RPC ``StartBackgroundComposerFromSnapshot``
    end-to-end. The legacy test name is preserved so the diff stays
    bisectable but the body now pins the new (wired) contract:

    - With a loadable JWT the call returns normally (no exception).
    - ``extra`` is mutated to carry ``__auth_mode__="session-jwt"``
      plus the explicit Path-B knobs the operator passed.

    The full per-knob coverage (effort / long_running / preset
    interaction / JWTAuthError-on-missing-JWT) lives in
    :mod:`tests.cli.test_path_b_e2e_wiring`.
    """
    import time
    from unittest.mock import patch

    from popolaloom.cloud.internal.jwt_auth import JWTBundle

    fake_bundle = JWTBundle(
        access_token="fake-jwt-test",
        refresh_token=None,
        source="env",
        path=None,
        exp_unix_s=int(time.time()) + 3600,
    )
    extra: dict[str, object] = {}
    with patch(
        "popolaloom.cloud.internal.jwt_auth.load_jwt_bundle",
        return_value=fake_bundle,
    ):
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
    assert extra["__auth_mode__"] == "session-jwt"
    assert extra["mode"] == "plan"
    assert extra["effort"] == "high"
    assert extra["long_running"] is True
    assert extra["auto_proceed_after_plan"] is True
    assert extra["max_mode"] is True
    assert extra["time_budget"] == "30m"


def test_apply_path_b_flags_preset_under_session_jwt_exits_until_wired() -> None:
    """v1.1.0 (Track 6) — preset under session-jwt expands and routes (no exit).

    Like :func:`test_apply_path_b_flags_session_jwt_exits_until_wired`,
    the legacy name pinned a removed behavior. Body now pins the new
    contract: preset values populate ``extra`` and ``__auth_mode__``
    is set so the supervisor branches to Connect-RPC.
    """
    import time
    from unittest.mock import patch

    from popolaloom.cloud.internal.jwt_auth import JWTBundle

    fake_bundle = JWTBundle(
        access_token="fake-jwt-test",
        refresh_token=None,
        source="env",
        path=None,
        exp_unix_s=int(time.time()) + 3600,
    )
    extra: dict[str, object] = {}
    with patch(
        "popolaloom.cloud.internal.jwt_auth.load_jwt_bundle",
        return_value=fake_bundle,
    ):
        _apply_path_b_flags(
            extra,
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
    assert extra["__auth_mode__"] == "session-jwt"
    assert extra["mode"] == "plan"
    assert extra["effort"] == "high"
    assert extra["long_running"] is True
    assert extra["auto_proceed_after_plan"] is True
    assert extra["time_budget"] == "3600s"


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


# ── v1.3.0 P2: --thinking-level Path-B knob ─────────────────────────


def test_thinking_level_under_session_jwt_forwards_to_extra() -> None:
    """v1.3.0 P2 — ``--thinking-level=high`` propagates through to extras."""
    import time
    from unittest.mock import patch

    from popolaloom.cloud.internal.jwt_auth import JWTBundle

    fake_bundle = JWTBundle(
        access_token="fake-jwt-test",
        refresh_token=None,
        source="env",
        path=None,
        exp_unix_s=int(time.time()) + 3600,
    )
    extra: dict[str, object] = {}
    with patch(
        "popolaloom.cloud.internal.jwt_auth.load_jwt_bundle",
        return_value=fake_bundle,
    ):
        _apply_path_b_flags(
            extra,
            cli="cursor-cloud",
            auth_mode="session-jwt",
            mode="",
            max_mode=False,
            effort="",
            time_budget="",
            long_running=False,
            auto_proceed_after_plan=False,
            preset="",
            thinking_level="high",
        )
    assert extra.get("__auth_mode__") == "session-jwt"
    assert extra.get("thinking_level") == "high"


def test_thinking_level_under_rest_rejected() -> None:
    """v1.3.0 P2 — ``--thinking-level`` under ``--auth-mode=rest`` exits 2."""
    extra: dict[str, object] = {}
    with pytest.raises(Exception) as exc_info:
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
            thinking_level="high",
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


def test_dispatch_command_exposes_all_path_b_flags() -> None:
    """All 9 path-B parameters + auth_mode appear in `popola dispatch` signature.

    Inspects the underlying Python function signature rather than the
    rendered ``--help`` text — Typer's rich-format help truncates flag
    names in narrow terminals (CI default 80-col) regardless of the
    COLUMNS env var because CliRunner uses its own width detection.

    v1.3.0 P2 adds ``thinking_level`` to the surface; the assertion list
    grows by one entry.
    """
    import inspect

    from popolaloom.cli.main import dispatch as _dispatch_fn

    params = inspect.signature(_dispatch_fn).parameters
    for needle in (
        "auth_mode",
        "mode",
        "max_mode",
        "effort",
        "time_budget",
        "long_running",
        "auto_proceed_after_plan",
        "preset",
        "thinking_level",
    ):
        assert needle in params, (
            f"missing {needle} in dispatch signature: {sorted(params)}"
        )


def test_dispatch_help_marks_path_b_as_experimental() -> None:
    """The --auth-mode help text labels session-jwt as experimental.

    Inspect the param's default Typer.Option help string rather than
    the rendered output (see test_dispatch_command_exposes_all_path_b_flags
    for the rationale).
    """
    import inspect

    from popolaloom.cli.main import dispatch as _dispatch_fn

    auth_mode_param = inspect.signature(_dispatch_fn).parameters["auth_mode"]
    typer_option = auth_mode_param.default
    help_text = getattr(typer_option, "help", "")
    assert "EXPERIMENTAL" in help_text


# ── v1.5.0 — Path-B skip-branch / PR Typer flags ────────────────────


def test_dispatch_signature_exposes_v1_5_0_path_b_flags() -> None:
    """v1.5.0 — all 5 new Typer flags appear in the dispatch signature."""
    import inspect

    from popolaloom.cli.main import dispatch as _dispatch_fn

    params = inspect.signature(_dispatch_fn).parameters
    for needle in (
        "auto_branch",
        "auto_create_pr",
        "work_on_current_branch",
        "skip_reviewer_request",
        "allow_fallback",
    ):
        assert needle in params, (
            f"v1.5.0 dispatch must expose {needle}: {sorted(params)}"
        )


def test_apply_path_b_flags_no_auto_branch_writes_extras() -> None:
    """v1.5.0 — passing ``auto_branch=False`` writes ``extra['auto_branch']``."""
    pytest.importorskip("popolaloom.cloud.internal.jwt_auth")
    import popolaloom.cloud.internal.jwt_auth as _jwt_mod
    from popolaloom.cloud.internal.jwt_auth import JWTBundle

    fake_bundle = JWTBundle(
        access_token="hdr.body.sig",
        refresh_token=None,
        source="env",
        path=None,
        exp_unix_s=2_000_000_000,
    )

    original_loader = _jwt_mod.load_jwt_bundle
    _jwt_mod.load_jwt_bundle = lambda: fake_bundle  # type: ignore[assignment]
    try:
        extra: dict[str, object] = {}
        _apply_path_b_flags(
            extra,
            cli="cursor-cloud",
            auth_mode="session-jwt",
            mode="",
            max_mode=False,
            effort="",
            time_budget="",
            long_running=False,
            auto_proceed_after_plan=False,
            preset="",
            auto_branch=False,
        )
    finally:
        _jwt_mod.load_jwt_bundle = original_loader  # type: ignore[assignment]

    assert extra["auto_branch"] is False


def test_apply_path_b_flags_skip_pr_knobs_write_extras() -> None:
    """v1.5.0 — all 3 skip-PR knobs land in extras when True."""
    pytest.importorskip("popolaloom.cloud.internal.jwt_auth")
    import popolaloom.cloud.internal.jwt_auth as _jwt_mod
    from popolaloom.cloud.internal.jwt_auth import JWTBundle

    fake_bundle = JWTBundle(
        access_token="hdr.body.sig",
        refresh_token=None,
        source="env",
        path=None,
        exp_unix_s=2_000_000_000,
    )

    original_loader = _jwt_mod.load_jwt_bundle
    _jwt_mod.load_jwt_bundle = lambda: fake_bundle  # type: ignore[assignment]
    try:
        extra: dict[str, object] = {}
        _apply_path_b_flags(
            extra,
            cli="cursor-cloud",
            auth_mode="session-jwt",
            mode="",
            max_mode=False,
            effort="",
            time_budget="",
            long_running=False,
            auto_proceed_after_plan=False,
            preset="",
            auto_create_pr=True,
            work_on_current_branch=True,
            skip_reviewer_request=True,
        )
    finally:
        _jwt_mod.load_jwt_bundle = original_loader  # type: ignore[assignment]

    assert extra["auto_create_pr"] is True
    assert extra["work_on_current_branch"] is True
    assert extra["skip_reviewer_request"] is True


def test_session_jwt_self_hosted_worker_combo_emits_pool_downgrade_warn(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """v1.5.0 PLAN Phase L — operator combining session-jwt + self-hosted +
    worker_name gets a strong stderr warning about Cursor's path-B server
    silently downgrading env={type:machine,name:X} to env={type:pool}.

    Per the No-Silent-Fallback invariant we do NOT auto-switch transports;
    the warning surfaces the empirically-discovered upstream limitation and
    points at the REST workaround. Verified live against ``api2.cursor.sh``
    2026-05-17 (Cursor's view: env type downgraded to ``pool``).
    """
    pytest.importorskip("popolaloom.cloud.internal.jwt_auth")
    import popolaloom.cloud.internal.jwt_auth as _jwt_mod
    from popolaloom.cloud.internal.jwt_auth import JWTBundle

    fake_bundle = JWTBundle(
        access_token="hdr.body.sig",
        refresh_token=None,
        source="env",
        path=None,
        exp_unix_s=2_000_000_000,
    )
    original_loader = _jwt_mod.load_jwt_bundle
    _jwt_mod.load_jwt_bundle = lambda: fake_bundle  # type: ignore[assignment]
    try:
        # Mirrors the marker shape `_apply_cloud_preferences` would
        # produce for `--cloud-target=self-hosted --worker-name=<X>`.
        extra: dict[str, object] = {
            "cloud_target": "self-hosted",
            "worker_name": "popolaloom-dev-worker-v15",
        }
        _apply_path_b_flags(
            extra,
            cli="cursor-cloud",
            auth_mode="session-jwt",
            mode="",
            max_mode=False,
            effort="",
            time_budget="",
            long_running=False,
            auto_proceed_after_plan=False,
            preset="",
        )
    finally:
        _jwt_mod.load_jwt_bundle = original_loader  # type: ignore[assignment]

    captured = capsys.readouterr()
    combined = captured.err + captured.out
    assert "path-B" in combined or "session-jwt" in combined
    assert "popolaloom-dev-worker-v15" in combined
    assert "downgrades" in combined
    assert "pool" in combined
    assert "--auth-mode=rest" in combined
    assert "no-silent-fallback" in combined


def test_apply_path_b_flags_default_auto_branch_not_written() -> None:
    """v1.5.0 — default ``auto_branch=True`` is the supervisor's default;
    don't write it to extras to avoid noise on dispatches that didn't
    opt in to the v1.5.0 flag surface.
    """
    pytest.importorskip("popolaloom.cloud.internal.jwt_auth")
    import popolaloom.cloud.internal.jwt_auth as _jwt_mod
    from popolaloom.cloud.internal.jwt_auth import JWTBundle

    fake_bundle = JWTBundle(
        access_token="hdr.body.sig",
        refresh_token=None,
        source="env",
        path=None,
        exp_unix_s=2_000_000_000,
    )
    original_loader = _jwt_mod.load_jwt_bundle
    _jwt_mod.load_jwt_bundle = lambda: fake_bundle  # type: ignore[assignment]
    try:
        extra: dict[str, object] = {}
        _apply_path_b_flags(
            extra,
            cli="cursor-cloud",
            auth_mode="session-jwt",
            mode="",
            max_mode=False,
            effort="",
            time_budget="",
            long_running=False,
            auto_proceed_after_plan=False,
            preset="",
        )
    finally:
        _jwt_mod.load_jwt_bundle = original_loader  # type: ignore[assignment]
    assert "auto_branch" not in extra
    assert "auto_create_pr" not in extra
    assert "work_on_current_branch" not in extra
    assert "skip_reviewer_request" not in extra
