"""End-to-end wiring tests for ``--auth-mode=session-jwt`` (v1.1.0 Track 6).

Covers the CLI side of the Path-B Connect-RPC wiring per Track 6 brief:

- :func:`popolaloom.cli.main._apply_path_b_flags` no longer hard-exits on
  ``--auth-mode=session-jwt``; instead it eagerly verifies a JWT bundle
  is loadable, then injects ``__auth_mode__`` + the resolved Path-B
  knobs into the dispatch ``extra`` dict so the supervisor's
  ``_spawn_cloud_path_b`` branch can route to
  :class:`CursorCloudInternalClient`.
- :data:`popolaloom.cli.main._BUILTIN_PRESETS` gains a ``"grind"``
  preset matching Cursor's "Grind mode" UI feature naming.

Mocks ``popolaloom.cli.main.load_jwt_bundle`` (rebound at the cli.main
import site) so tests run without touching ``~/.config/cursor/auth.json``
or requiring the ``CURSOR_SESSION_JWT`` env var.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

import pytest
import typer

from popolaloom.cli.main import (
    _BUILTIN_PRESETS,
    _apply_path_b_flags,
    _apply_preset,
)
from popolaloom.cloud.internal.jwt_auth import JWTAuthError, JWTBundle


def _fake_jwt_bundle() -> JWTBundle:
    """Return a synthetic :class:`JWTBundle` for tests.

    No live JWT decoding happens — the Path-B CLI wiring only checks
    that ``load_jwt_bundle`` returns without raising.
    """
    return JWTBundle(
        access_token="fake-jwt-for-tests",
        refresh_token=None,
        source="env",
        path=None,
        exp_unix_s=int(time.time()) + 3600,
    )


def test_apply_path_b_flags_session_jwt_injects_extras() -> None:
    """v1.1.0 Track 6 — session-jwt no longer hard-exits, injects Path-B knobs.

    With a loadable JWT bundle the call mutates ``extra`` to carry
    ``__auth_mode__="session-jwt"`` plus every explicit Path-B flag the
    operator passed (``effort=high`` and ``long_running=True`` here).
    The supervisor then reads these back to build the Connect-RPC body.
    """
    extra: dict[str, Any] = {}
    with patch(
        "popolaloom.cloud.internal.jwt_auth.load_jwt_bundle",
        return_value=_fake_jwt_bundle(),
    ):
        _apply_path_b_flags(
            extra,
            cli="cursor-cloud",
            auth_mode="session-jwt",
            mode="",
            max_mode=False,
            effort="high",
            time_budget="",
            long_running=True,
            auto_proceed_after_plan=False,
            preset="",
        )
    assert extra["__auth_mode__"] == "session-jwt"
    assert extra["effort"] == "high"
    assert extra["long_running"] is True
    assert "max_mode" not in extra
    assert "auto_proceed_after_plan" not in extra


def test_apply_path_b_flags_session_jwt_missing_jwt() -> None:
    """v1.1.0 Track 6 — JWTAuthError surfaces friendly hint and exits 1.

    No-Silent-Failures: the operator-facing error message MUST carry the
    bilingual ``hint`` produced by :class:`JWTAuthError` so they know
    how to fix the configuration (``agent login`` or
    ``CURSOR_SESSION_JWT`` env).

    ``typer.Exit`` raises :class:`click.exceptions.Exit` (not
    :class:`SystemExit`); we use the workspace convention of catching
    ``Exception`` and inspecting ``exit_code`` (mirrors the legacy
    tests in ``tests/cli/test_dispatch_path_b_flags.py``).
    """
    err = JWTAuthError(
        "no JWT available",
        hint="Run `agent login` or export CURSOR_SESSION_JWT.",
    )
    extra: dict[str, Any] = {}
    with (
        patch(
            "popolaloom.cloud.internal.jwt_auth.load_jwt_bundle",
            side_effect=err,
        ),
        pytest.raises(typer.Exit) as exc_info,
    ):
        _apply_path_b_flags(
            extra,
            cli="cursor-cloud",
            auth_mode="session-jwt",
            mode="",
            max_mode=False,
            effort="high",
            time_budget="",
            long_running=True,
            auto_proceed_after_plan=False,
            preset="",
        )
    assert getattr(exc_info.value, "exit_code", None) == 1
    assert extra == {}


def test_apply_path_b_flags_session_jwt_missing_jwt_prints_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The operator-facing stderr carries the JWTAuthError hint verbatim."""
    err = JWTAuthError(
        "no JWT available",
        hint="Run `agent login` 在本机生成 ~/.config/cursor/auth.json",
    )
    with (
        patch(
            "popolaloom.cloud.internal.jwt_auth.load_jwt_bundle",
            side_effect=err,
        ),
        pytest.raises(typer.Exit),
    ):
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
            preset="",
        )
    captured = capsys.readouterr()
    assert "could not load a JWT" in captured.err
    assert "agent login" in captured.err


def test_apply_path_b_flags_session_jwt_with_preset_merges_explicit_wins() -> None:
    """Q-17 contract preserved on Path-B: explicit flags override preset values.

    The new wiring still resolves the preset+explicit merge via
    :func:`_apply_preset` and then forwards the merged dict into
    ``extra``; explicit values take precedence over preset defaults.
    """
    extra: dict[str, Any] = {}
    with patch(
        "popolaloom.cloud.internal.jwt_auth.load_jwt_bundle",
        return_value=_fake_jwt_bundle(),
    ):
        _apply_path_b_flags(
            extra,
            cli="cursor-cloud",
            auth_mode="session-jwt",
            mode="ask",
            max_mode=False,
            effort="",
            time_budget="",
            long_running=False,
            auto_proceed_after_plan=False,
            preset="long-running-plan",
        )
    assert extra["__auth_mode__"] == "session-jwt"
    assert extra["mode"] == "ask"
    assert extra["effort"] == "high"
    assert extra["time_budget"] == "3600s"
    assert extra["long_running"] is True
    assert extra["auto_proceed_after_plan"] is True


def test_builtin_preset_grind_expands_to_full_dict() -> None:
    """v1.1.0 Track 6 — grind preset bundles the canonical Path-B knobs.

    Mirrors Cursor's "Grind mode" UI feature: plan + high effort +
    long_running + 4-hour budget + auto-proceed-after-plan.
    """
    out = _apply_preset({}, "grind", explicit={})
    assert out == {
        "mode": "plan",
        "effort": "high",
        "long_running": True,
        "time_budget": "14400s",
        "auto_proceed_after_plan": True,
    }


def test_builtin_presets_includes_grind() -> None:
    """The static preset catalog gains a ``"grind"`` entry (Track 6 AC2)."""
    assert "grind" in _BUILTIN_PRESETS
    assert _BUILTIN_PRESETS["grind"]["mode"] == "plan"
    assert _BUILTIN_PRESETS["grind"]["long_running"] is True
    assert _BUILTIN_PRESETS["grind"]["time_budget"] == "14400s"


def test_apply_path_b_flags_session_jwt_no_path_b_knobs_still_routes() -> None:
    """``--auth-mode=session-jwt`` alone (no Path-B knobs) still injects marker.

    The operator might want Path-B routing for future RPC features even
    without setting any of the current Path-B flags; the marker MUST be
    set so the supervisor branches to the Connect-RPC client.
    """
    extra: dict[str, Any] = {}
    with patch(
        "popolaloom.cloud.internal.jwt_auth.load_jwt_bundle",
        return_value=_fake_jwt_bundle(),
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
        )
    assert extra["__auth_mode__"] == "session-jwt"
    for k in (
        "mode",
        "max_mode",
        "effort",
        "time_budget",
        "long_running",
        "auto_proceed_after_plan",
    ):
        assert k not in extra, f"unexpected Path-B knob {k!r} in extras"


def test_apply_path_b_flags_session_jwt_non_cursor_cloud_unchanged_on_session_jwt() -> None:
    """``--cli=cursor`` + ``--auth-mode=session-jwt`` warns + drops (no JWT load).

    The non-cursor-cloud short-circuit (logger.warning + return) MUST
    fire BEFORE we attempt to load a JWT; otherwise an operator with
    no Cursor JWT would get a hard failure on a flag that's a no-op
    for their adapter. This test pins the existing branch ordering
    survives the Track 6 refactor.
    """
    extra: dict[str, Any] = {}
    with patch(
        "popolaloom.cloud.internal.jwt_auth.load_jwt_bundle",
        side_effect=JWTAuthError("would not be raised", hint="x"),
    ):
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
    # Path-B marker NOT injected for non-cursor-cloud adapters.
    assert "__auth_mode__" not in extra
