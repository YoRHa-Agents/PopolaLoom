"""Tests for ``popola dispatch --cloud-target`` / ``--worker-name`` (v0.10.0).

PopolaLoom v0.10.0 Wave B3 — DECISIONS Q-6 + Q-7 + PLAN B3 AC 1-7.

This file exercises the seven acceptance criteria for B3:

1. ``--cloud-target`` / ``--worker-name`` Typer options exist on
   ``popola dispatch`` (signature smoke).
2. Auto-set ``--cli=cursor-cloud`` when ``--cloud-target`` is given AND
   ``--cli`` is empty.
3. Validation: mutual exclusion + required fields + ``ask-each-time``
   rejection — all exit with ``_EXIT_INVALID_ARGS`` (2).
4. ``_apply_cloud_preferences`` precedence: per-task flag > pref > default.
5. The legacy ``cloud_target_priority`` list-of-targets path is no longer
   consulted; ``out["use_private_worker"] = True`` injection is gone.
6. Legacy ``--cli-flag worker_name=`` / ``--cli-flag use_private_worker=true``
   escape hatches still flow through unchanged.
7. Tests cover ≥7 cases (flag-only, flag-overrides-pref, pref-only,
   default-only, mutual-exclusion, auto-CLI, escape-hatch).

The dispatch HTTP transport is stubbed via ``make_sync_client`` so these
tests do not need a running ``popolad``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from popolaloom.cli.init_cmd import write_user_preferences_for_cli
from popolaloom.cli.main import (
    _EXIT_INVALID_ARGS,
    _apply_cloud_preferences,
    _validate_cloud_target_flags,
)
from popolaloom.cli.main import (
    app as main_app,
)
from popolaloom.daemon.main import UserPreferencesConfig, UserPrefsCursorCloud


@pytest.fixture
def isolated_popola_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Isolate ``$POPOLA_HOME`` + worker-name env vars + cwd per test."""
    popola_home = tmp_path / "popola"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("POPOLA_HOME", str(popola_home))
    monkeypatch.delenv("POPOLA_WORKER_NAME", raising=False)
    monkeypatch.delenv("POPOLA_SELF_HOSTED_WORKER_NAME", raising=False)
    monkeypatch.chdir(project)
    yield popola_home


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _combined_output(result: object) -> str:
    """Concatenate ``stdout`` / ``stderr`` / ``output`` for assertion msgs."""
    parts: list[str] = []
    for attr in ("stdout", "stderr", "output"):
        try:
            value = getattr(result, attr, "") or ""
        except (ValueError, AttributeError):
            value = ""
        if value and value not in parts:
            parts.append(value)
    return "".join(parts)


def _mock_dispatch_client(monkeypatch: pytest.MonkeyPatch, task_id: str) -> MagicMock:
    """Stub ``make_sync_client`` so dispatch never opens a real UDS."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"task_id": task_id}
    mock_client.__enter__.return_value.post.return_value = mock_response
    monkeypatch.setattr("popolaloom.cli.main.make_sync_client", lambda: mock_client)
    return mock_client


def _posted_body(mock_client: MagicMock) -> dict[str, Any]:
    """Extract the ``json=`` kwarg from the captured HTTP POST."""
    return mock_client.__enter__.return_value.post.call_args.kwargs["json"]


# ── _validate_cloud_target_flags unit tests ──────────────────────────────


def test_validate_accepts_self_hosted_with_worker_name() -> None:
    """``self-hosted`` + worker_name is the only valid self-hosted shape."""
    _validate_cloud_target_flags("self-hosted", "my-worker")


def test_validate_accepts_cursor_managed_without_worker_name() -> None:
    """``cursor-managed`` alone is the only valid cursor-managed shape."""
    _validate_cloud_target_flags("cursor-managed", "")


def test_validate_rejects_self_hosted_missing_worker_name() -> None:
    """``self-hosted`` without ``--worker-name`` exits 2 (No Silent Failures)."""
    with pytest.raises(Exception) as exc_info:
        _validate_cloud_target_flags("self-hosted", "")
    assert getattr(exc_info.value, "exit_code", None) == _EXIT_INVALID_ARGS


def test_validate_rejects_cursor_managed_with_worker_name() -> None:
    """Mutual exclusion: ``cursor-managed`` + ``--worker-name`` exits 2."""
    with pytest.raises(Exception) as exc_info:
        _validate_cloud_target_flags("cursor-managed", "my-worker")
    assert getattr(exc_info.value, "exit_code", None) == _EXIT_INVALID_ARGS


def test_validate_rejects_ask_each_time_at_dispatch_time() -> None:
    """``ask-each-time`` is only a pref default; never a per-task value."""
    with pytest.raises(Exception) as exc_info:
        _validate_cloud_target_flags("ask-each-time", "")
    assert getattr(exc_info.value, "exit_code", None) == _EXIT_INVALID_ARGS


def test_validate_rejects_unknown_cloud_target() -> None:
    """Unknown cloud-target values are rejected (No Silent Failures)."""
    with pytest.raises(Exception) as exc_info:
        _validate_cloud_target_flags("local", "")
    assert getattr(exc_info.value, "exit_code", None) == _EXIT_INVALID_ARGS


def test_validate_rejects_worker_name_without_cloud_target() -> None:
    """``--worker-name`` is only valid alongside ``--cloud-target=self-hosted``."""
    with pytest.raises(Exception) as exc_info:
        _validate_cloud_target_flags("", "stray-worker")
    assert getattr(exc_info.value, "exit_code", None) == _EXIT_INVALID_ARGS


# ── _apply_cloud_preferences unit tests (precedence resolver) ────────────


def test_apply_cloud_preferences_flag_only(tmp_path: Path) -> None:
    """Per-task flag-only path: prefs=None, no extras → flag wins."""
    out = _apply_cloud_preferences(
        None,
        {},
        cwd=tmp_path,
        cloud_target_flag="self-hosted",
        worker_name_flag="probe-w1",
    )
    assert out == {"cloud_target": "self-hosted", "worker_name": "probe-w1"}


def test_apply_cloud_preferences_flag_overrides_pref(tmp_path: Path) -> None:
    """Per-task flag must override the pref value (precedence: flag > pref)."""
    prefs = UserPreferencesConfig(default_cloud_target="cursor-managed")
    out = _apply_cloud_preferences(
        prefs,
        {},
        cwd=tmp_path,
        cloud_target_flag="self-hosted",
        worker_name_flag="probe-w1",
    )
    assert out["cloud_target"] == "self-hosted"
    assert out["worker_name"] == "probe-w1"


def test_apply_cloud_preferences_pref_only(tmp_path: Path) -> None:
    """Pref-only path: pref's ``default_cloud_target`` is consumed when no flag."""
    prefs = UserPreferencesConfig(default_cloud_target="cursor-managed")
    out = _apply_cloud_preferences(prefs, {}, cwd=tmp_path)
    assert out["cloud_target"] == "cursor-managed"


def test_apply_cloud_preferences_default_only_no_pref(tmp_path: Path) -> None:
    """Default path: no flag, no pref → ``ask-each-time`` collapses to no-op."""
    out = _apply_cloud_preferences(None, {}, cwd=tmp_path)
    assert out == {}


def test_apply_cloud_preferences_default_only_with_ask_each_time_pref(
    tmp_path: Path,
) -> None:
    """Default path: pref=``ask-each-time`` collapses to no-op (no extras)."""
    prefs = UserPreferencesConfig(default_cloud_target="ask-each-time")
    out = _apply_cloud_preferences(prefs, {}, cwd=tmp_path)
    assert "cloud_target" not in out
    assert "worker_name" not in out


def test_apply_cloud_preferences_self_hosted_pref_with_marker(
    tmp_path: Path,
) -> None:
    """``default_cloud_target=self-hosted`` + ``.popola-worker`` marker resolves OK."""
    (tmp_path / ".popola-worker").write_text("marker-w1\n", encoding="utf-8")
    prefs = UserPreferencesConfig(default_cloud_target="self-hosted")
    out = _apply_cloud_preferences(prefs, {}, cwd=tmp_path)
    assert out["cloud_target"] == "self-hosted"
    assert out["worker_name"] == "marker-w1"


def test_apply_cloud_preferences_self_hosted_pref_no_worker_fails(
    tmp_path: Path,
) -> None:
    """Pref=self-hosted but no worker-name resolvable → exit 2 (Q-7 no-fallback)."""
    prefs = UserPreferencesConfig(default_cloud_target="self-hosted")
    with pytest.raises(Exception) as exc_info:
        _apply_cloud_preferences(prefs, {}, cwd=tmp_path)
    assert getattr(exc_info.value, "exit_code", None) == _EXIT_INVALID_ARGS


def test_apply_cloud_preferences_does_not_inject_use_private_worker(
    tmp_path: Path,
) -> None:
    """The legacy ``out.setdefault("use_private_worker", True)`` is REMOVED.

    Per AC 5: A1's ``_normalize_cloud_extra`` now handles back-compat for
    legacy extras; the resolver no longer injects ``use_private_worker``.
    """
    prefs = UserPreferencesConfig(default_cloud_target="self-hosted")
    out = _apply_cloud_preferences(
        prefs,
        {},
        cwd=tmp_path,
        cloud_target_flag="self-hosted",
        worker_name_flag="probe-w1",
    )
    assert "use_private_worker" not in out


def test_apply_cloud_preferences_ignores_legacy_cloud_target_priority(
    tmp_path: Path,
) -> None:
    """Legacy ``cloud_target_priority`` list is NOT consumed by the resolver.

    Per AC 5: B1's loader still parses ``cloud_target_priority`` (with a
    deprecation WARN), but the resolver collapses to the new
    ``default_cloud_target`` field. Setting the legacy list to a deprecated
    shape MUST NOT change the resolver's output.
    """
    prefs = UserPreferencesConfig(
        default_cloud_target="cursor-managed",
        cloud_target_priority=("self-hosted", "cursor-managed"),
    )
    out = _apply_cloud_preferences(prefs, {}, cwd=tmp_path)
    assert out["cloud_target"] == "cursor-managed"


def test_apply_cloud_preferences_escape_hatch_passthrough(tmp_path: Path) -> None:
    """``--cli-flag worker_name=W`` / ``use_private_worker=true`` flow through."""
    extra: dict[str, Any] = {"worker_name": "legacy-w", "use_private_worker": True}
    out = _apply_cloud_preferences(None, extra, cwd=tmp_path)
    assert out["worker_name"] == "legacy-w"
    assert out["use_private_worker"] is True


def test_apply_cloud_preferences_self_hosted_uses_extras_worker_name(
    tmp_path: Path,
) -> None:
    """``cloud_target=self-hosted`` with ``--cli-flag worker_name=W`` resolves OK."""
    prefs = UserPreferencesConfig(default_cloud_target="self-hosted")
    extra: dict[str, Any] = {"worker_name": "extras-w"}
    out = _apply_cloud_preferences(prefs, extra, cwd=tmp_path)
    assert out["cloud_target"] == "self-hosted"
    assert out["worker_name"] == "extras-w"


# ── Typer dispatch end-to-end tests ──────────────────────────────────────


def test_dispatch_flag_only_routes_to_cursor_cloud(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 1+2+4: ``--cloud-target=self-hosted --worker-name=W`` auto-routes."""
    mock_client = _mock_dispatch_client(monkeypatch, "self-hosted-flag-1234")

    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "do work",
            "--cloud-target=self-hosted",
            "--worker-name=probe-w1",
        ],
    )

    assert result.exit_code == 0, _combined_output(result)
    body = _posted_body(mock_client)
    assert body["cli"] == "cursor-cloud"
    assert body["extra"]["cloud_target"] == "self-hosted"
    assert body["extra"]["worker_name"] == "probe-w1"


def test_dispatch_explicit_rest_auth_overrides_session_jwt_pref_for_named_worker(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit REST path-A must not be upgraded by default_auth_mode prefs."""
    write_user_preferences_for_cli(
        UserPreferencesConfig(
            cursor_cloud=UserPrefsCursorCloud(default_auth_mode="session-jwt"),
        )
    )
    mock_client = _mock_dispatch_client(monkeypatch, "rest-path-a-worker-1234")

    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "claim named worker",
            "--cloud-target=self-hosted",
            "--worker-name=probe-w1",
            "--auth-mode=rest",
        ],
    )

    assert result.exit_code == 0, _combined_output(result)
    assert "[prefs] applying" not in _combined_output(result)
    body = _posted_body(mock_client)
    assert body["cli"] == "cursor-cloud"
    assert body["extra"]["cloud_target"] == "self-hosted"
    assert body["extra"]["worker_name"] == "probe-w1"
    assert "__auth_mode__" not in body["extra"]


def test_dispatch_flag_overrides_pref(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 4: per-task flag wins over ``[user_preferences].default_cloud_target``."""
    write_user_preferences_for_cli(
        UserPreferencesConfig(
            default_runtime="cloud",
            default_cloud_target="cursor-managed",
        )
    )
    mock_client = _mock_dispatch_client(monkeypatch, "flag-overrides-pref-1234")

    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "override",
            "--cloud-target=self-hosted",
            "--worker-name=probe-w1",
        ],
    )

    assert result.exit_code == 0, _combined_output(result)
    body = _posted_body(mock_client)
    assert body["cli"] == "cursor-cloud"
    assert body["extra"]["cloud_target"] == "self-hosted"
    assert body["extra"]["worker_name"] == "probe-w1"


def test_dispatch_pref_only_resolves_to_cursor_managed(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 4: pref-only path applies ``default_cloud_target`` from preferences."""
    write_user_preferences_for_cli(
        UserPreferencesConfig(
            default_runtime="cloud",
            default_cloud_target="cursor-managed",
        )
    )
    mock_client = _mock_dispatch_client(monkeypatch, "pref-only-1234")

    result = runner.invoke(main_app, ["dispatch", "pref dispatch", "--no-wizard"])

    assert result.exit_code == 0, _combined_output(result)
    body = _posted_body(mock_client)
    assert body["cli"] == "cursor-cloud"
    assert body["extra"]["cloud_target"] == "cursor-managed"
    assert "worker_name" not in body["extra"]


def test_dispatch_default_only_no_extras_when_target_is_ask_each_time(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 4: default ``ask-each-time`` collapses to a no-op (no extras emitted)."""
    mock_client = _mock_dispatch_client(monkeypatch, "default-only-1234")

    result = runner.invoke(
        main_app,
        ["dispatch", "default work", "--cli=cursor-cloud"],
    )

    assert result.exit_code == 0, _combined_output(result)
    body = _posted_body(mock_client)
    assert body["cli"] == "cursor-cloud"
    extra = body.get("extra", {})
    assert "cloud_target" not in extra
    assert "worker_name" not in extra


def test_dispatch_rejects_self_hosted_without_worker_name(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    """AC 3: ``--cloud-target=self-hosted`` alone exits ``_EXIT_INVALID_ARGS`` (2)."""
    result = runner.invoke(
        main_app,
        ["dispatch", "missing worker", "--cloud-target=self-hosted"],
    )

    assert result.exit_code == _EXIT_INVALID_ARGS
    assert "self-hosted" in _combined_output(result)
    assert "popola cloud worker start" in _combined_output(result)


def test_dispatch_rejects_cursor_managed_with_worker_name(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    """AC 3: ``--cloud-target=cursor-managed`` + ``--worker-name`` exits 2."""
    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "mutual exclusion",
            "--cloud-target=cursor-managed",
            "--worker-name=stray",
        ],
    )

    assert result.exit_code == _EXIT_INVALID_ARGS
    assert "mutual exclusion" in _combined_output(result).lower() or (
        "cursor-managed" in _combined_output(result)
        and "worker-name" in _combined_output(result)
    )


def test_dispatch_rejects_ask_each_time_at_dispatch_time(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    """AC 3: ``--cloud-target=ask-each-time`` is rejected at dispatch time."""
    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "ask only allowed in pref",
            "--cloud-target=ask-each-time",
        ],
    )

    assert result.exit_code == _EXIT_INVALID_ARGS
    assert "ask-each-time" in _combined_output(result)


def test_dispatch_rejects_worker_name_without_cloud_target(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    """AC 3: ``--worker-name`` alone (no ``--cloud-target``) exits 2."""
    result = runner.invoke(
        main_app,
        ["dispatch", "stray worker", "--worker-name=stray"],
    )

    assert result.exit_code == _EXIT_INVALID_ARGS


def test_dispatch_auto_sets_cli_to_cursor_cloud(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 2: ``--cloud-target=self-hosted --worker-name=W`` (no ``--cli``) auto-routes."""
    mock_client = _mock_dispatch_client(monkeypatch, "auto-cli-1234")

    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "auto cli",
            "--cloud-target=self-hosted",
            "--worker-name=auto-w",
        ],
    )

    assert result.exit_code == 0, _combined_output(result)
    body = _posted_body(mock_client)
    assert body["cli"] == "cursor-cloud"


def test_dispatch_explicit_cli_cursor_cloud_keeps_cli(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``--cli=cursor-cloud`` is preserved; flags still flow through."""
    mock_client = _mock_dispatch_client(monkeypatch, "explicit-cli-1234")

    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "explicit",
            "--cli=cursor-cloud",
            "--cloud-target=self-hosted",
            "--worker-name=explicit-w",
        ],
    )

    assert result.exit_code == 0, _combined_output(result)
    body = _posted_body(mock_client)
    assert body["cli"] == "cursor-cloud"
    assert body["extra"]["cloud_target"] == "self-hosted"
    assert body["extra"]["worker_name"] == "explicit-w"


def test_dispatch_escape_hatch_worker_name_still_works(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 6: ``--cli=cursor-cloud --cli-flag worker_name=W`` legacy escape hatch.

    The value flows into the same ``extra`` dict (via ``--cli-flag``) and
    A1's ``_normalize_cloud_extra`` translates it to the env-shape under
    ``cursor-cloud``. Per AC 6, dispatch MUST NOT block the legacy flag.
    """
    mock_client = _mock_dispatch_client(monkeypatch, "escape-hatch-1234")

    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "legacy work",
            "--cli=cursor-cloud",
            "--cli-flag",
            "worker_name=legacy-w",
        ],
    )

    assert result.exit_code == 0, _combined_output(result)
    body = _posted_body(mock_client)
    assert body["cli"] == "cursor-cloud"
    assert body["extra"]["worker_name"] == "legacy-w"


def test_dispatch_escape_hatch_use_private_worker_still_works(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 6: ``--cli-flag use_private_worker=true`` legacy escape hatch."""
    mock_client = _mock_dispatch_client(monkeypatch, "escape-hatch-upw-1234")

    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "legacy upw",
            "--cli=cursor-cloud",
            "--cli-flag",
            "use_private_worker=true",
            "--cli-flag",
            "worker_name=legacy-w",
        ],
    )

    assert result.exit_code == 0, _combined_output(result)
    body = _posted_body(mock_client)
    assert body["cli"] == "cursor-cloud"
    assert body["extra"]["use_private_worker"] is True
    assert body["extra"]["worker_name"] == "legacy-w"


def test_dispatch_self_hosted_pref_with_marker_routes_self_hosted(
    isolated_popola_home: Path,
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 4 + Q-7: pref=self-hosted + worker marker → resolved to self-hosted."""
    write_user_preferences_for_cli(
        UserPreferencesConfig(
            default_runtime="cloud",
            default_cloud_target="self-hosted",
        )
    )
    project = Path.cwd()
    (project / ".popola-worker").write_text("marker-w1\n", encoding="utf-8")
    mock_client = _mock_dispatch_client(monkeypatch, "marker-1234")

    result = runner.invoke(main_app, ["dispatch", "marker dispatch", "--no-wizard"])

    assert result.exit_code == 0, _combined_output(result)
    body = _posted_body(mock_client)
    assert body["cli"] == "cursor-cloud"
    assert body["extra"]["cloud_target"] == "self-hosted"
    assert body["extra"]["worker_name"] == "marker-w1"


def test_dispatch_self_hosted_pref_without_worker_fails(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    """AC 3 + Q-7: pref=self-hosted + no worker-name → exit 2 with bilingual hint.

    Crucially, the hint MUST point at ``popola cloud worker start --name``
    (the actual fix), NOT at any local-CLI fallback path.
    """
    write_user_preferences_for_cli(
        UserPreferencesConfig(
            default_runtime="cloud",
            default_cloud_target="self-hosted",
        )
    )

    result = runner.invoke(main_app, ["dispatch", "no worker hint", "--no-wizard"])

    assert result.exit_code == _EXIT_INVALID_ARGS
    output = _combined_output(result)
    assert "popola cloud worker start --name" in output
    assert "--cli=cursor" not in output
