"""Default-lane tests for ``popola auth cursor`` (v0.9.2+).

Exercises the three verbs (``set`` / ``status`` / ``clear``) plus the
flag matrix (``--api-key`` / ``--from-env`` / ``--validate`` /
``--json`` / ``--yes``) via :class:`typer.testing.CliRunner` with stdin
injection (per the Typer testing guide).

Security invariants pinned by this suite:

1. ``set`` rejects empty input (No Silent Failures).
2. ``set`` exits ``3`` when the keyring extra is unavailable.
3. ``set --from-env`` requires ``$CURSOR_API_KEY`` to be set.
4. ``--api-key`` and ``--from-env`` are mutually exclusive (exit 2).
5. ``status`` never prints the raw secret — only the fingerprint.
6. ``status --json`` keys are stable across invocations.
7. ``clear --yes`` is idempotent (no-op when nothing was stored).
8. The interactive prompt uses ``hide_input=True`` (the value never
   re-echoes; verified by feeding stdin and asserting absence in
   stdout).
"""

from __future__ import annotations

import json
import types
from collections.abc import Iterator

import pytest
from typer.testing import CliRunner

from popolaloom import credentials as cred_mod
from popolaloom.cli.auth_cmd import app as auth_app

# ── fake-backend fixture (shared across this suite) ──────────────────────


class _FakeBackend:
    """Minimal stand-in for the OS keyring (in-memory only)."""

    __module__ = "fake.backend"
    __qualname__ = "Keyring"

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, value: str) -> None:
        self.store[(service, username)] = value

    def delete_password(self, service: str, username: str) -> None:
        del self.store[(service, username)]


@pytest.fixture
def fake_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> Iterator[_FakeBackend]:
    """Wire a fake keyring into the resolver + redirect ``$POPOLA_HOME``."""
    backend = _FakeBackend()
    fake_module = types.ModuleType("keyring")
    fake_module.get_keyring = lambda: backend  # type: ignore[attr-defined]
    fake_module.get_password = lambda s, u: backend.get_password(s, u)  # type: ignore[attr-defined]
    fake_module.set_password = lambda s, u, v: backend.set_password(s, u, v)  # type: ignore[attr-defined]
    fake_module.delete_password = lambda s, u: backend.delete_password(s, u)  # type: ignore[attr-defined]
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: fake_module)
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path / "popola"))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    yield backend


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _combined_output(result: object) -> str:
    """Mirror the helper in test_init_cmd.py for cross-suite consistency."""
    stdout = getattr(result, "stdout", "") or ""
    try:
        stderr = getattr(result, "stderr", "") or ""
    except (ValueError, AttributeError):
        stderr = ""
    output = getattr(result, "output", "") or ""
    return stdout + stderr + output


# ── set verb ────────────────────────────────────────────────────────────


def test_set_with_api_key_flag_persists(
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    """`popola auth cursor set --api-key cr_value` stores via keyring."""
    result = runner.invoke(auth_app, ["cursor", "set", "--api-key", "cr_test_value"])
    output = _combined_output(result)
    assert result.exit_code == 0, output
    assert backend_value(fake_backend) == "cr_test_value"


def test_set_rejects_empty_api_key_flag(
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    """Empty ``--api-key ""`` exits non-zero (No Silent Failures)."""
    result = runner.invoke(auth_app, ["cursor", "set", "--api-key", "   "])
    assert result.exit_code != 0
    assert backend_value(fake_backend) is None


def test_set_mutually_exclusive_api_key_and_from_env(
    fake_backend: _FakeBackend,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--api-key`` + ``--from-env`` together → exit 2."""
    monkeypatch.setenv("CURSOR_API_KEY", "env-value")
    result = runner.invoke(
        auth_app,
        ["cursor", "set", "--api-key", "cr_value", "--from-env"],
    )
    assert result.exit_code == 2, _combined_output(result)


def test_set_from_env_requires_env_var(
    fake_backend: _FakeBackend,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--from-env`` without ``$CURSOR_API_KEY`` exits 2 with a hint."""
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    result = runner.invoke(auth_app, ["cursor", "set", "--from-env"])
    assert result.exit_code == 2, _combined_output(result)
    output = _combined_output(result)
    assert "CURSOR_API_KEY" in output


def test_set_from_env_copies_value(
    fake_backend: _FakeBackend,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--from-env`` migrates the env-var value into the keyring."""
    monkeypatch.setenv("CURSOR_API_KEY", "env-value")
    result = runner.invoke(auth_app, ["cursor", "set", "--from-env"])
    assert result.exit_code == 0, _combined_output(result)
    assert backend_value(fake_backend) == "env-value"


def test_set_without_keyring_extra_exits_3(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """No keyring backend → exit 3 with remediation text."""
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: None)
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path / "popola"))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    result = runner.invoke(auth_app, ["cursor", "set", "--api-key", "cr_value"])
    assert result.exit_code == 3, _combined_output(result)
    output = _combined_output(result)
    assert "keyring" in output.lower() or "extra" in output.lower()


def test_set_does_not_echo_secret_in_stdout(
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    """The literal API key never appears in stdout (defense in depth)."""
    secret = "cr_super_should_not_leak"
    result = runner.invoke(auth_app, ["cursor", "set", "--api-key", secret])
    assert result.exit_code == 0, _combined_output(result)
    output = _combined_output(result)
    assert secret not in output


def test_set_json_output_shape(
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    """``--json`` emits a stable envelope with status fields + ``validated``."""
    result = runner.invoke(
        auth_app,
        ["cursor", "set", "--api-key", "cr_test", "--json"],
    )
    assert result.exit_code == 0, _combined_output(result)
    payload = json.loads(result.stdout)
    assert payload["configured"] is True
    assert payload["source"] in {"keyring", "env"}
    assert "fingerprint" in payload
    assert "validated" in payload
    assert payload["validated"] is False  # not requested
    assert "cr_test" not in result.stdout


# ── status verb ─────────────────────────────────────────────────────────


def test_status_when_unconfigured(
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    """Unconfigured → human text mentions the three remediation paths."""
    result = runner.invoke(auth_app, ["cursor", "status"])
    assert result.exit_code == 0, _combined_output(result)
    output = _combined_output(result)
    assert "NOT configured" in output
    assert "popola auth cursor set" in output
    assert "CURSOR_API_KEY" in output


def test_status_when_keyring_configured(
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    secret = "cr_status_test_value"
    fake_backend.store[("popolaloom.cursor", "default")] = secret
    result = runner.invoke(auth_app, ["cursor", "status"])
    assert result.exit_code == 0, _combined_output(result)
    output = _combined_output(result)
    assert "configured" in output
    assert secret not in output  # the raw value MUST never appear
    # but the fingerprint should
    assert cred_mod.compute_fingerprint(secret) in output


def test_status_json_keys_are_stable(
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    """``status --json`` keys are part of the v0.9.x stable surface."""
    fake_backend.store[("popolaloom.cursor", "default")] = "cr_test"
    result = runner.invoke(auth_app, ["cursor", "status", "--json"])
    assert result.exit_code == 0, _combined_output(result)
    payload = json.loads(result.stdout)
    assert set(payload.keys()) == {
        "configured",
        "source",
        "backend_name",
        "fingerprint",
        "keyring_available",
    }
    assert payload["configured"] is True
    assert "cr_test" not in result.stdout


# ── clear verb ──────────────────────────────────────────────────────────


def test_clear_yes_is_idempotent(
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    """``clear --yes`` succeeds even when no entry was stored."""
    result = runner.invoke(auth_app, ["cursor", "clear", "--yes"])
    assert result.exit_code == 0, _combined_output(result)
    output = _combined_output(result)
    assert "no-op" in output.lower() or "no Cursor API key" in output


def test_clear_yes_removes_stored_entry(
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    fake_backend.store[("popolaloom.cursor", "default")] = "cr_value"
    result = runner.invoke(auth_app, ["cursor", "clear", "--yes"])
    assert result.exit_code == 0, _combined_output(result)
    assert backend_value(fake_backend) is None
    output = _combined_output(result)
    assert "removed" in output.lower()


def test_clear_without_yes_aborts_when_declined(
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    """Without ``-y``, the prompt's default is No → abort cleanly."""
    fake_backend.store[("popolaloom.cursor", "default")] = "cr_value"
    result = runner.invoke(auth_app, ["cursor", "clear"], input="n\n")
    assert result.exit_code == 0, _combined_output(result)
    assert backend_value(fake_backend) == "cr_value"  # untouched


def test_clear_json_envelope(
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    """``clear --json`` exposes ``removed`` and the post-clear status."""
    fake_backend.store[("popolaloom.cursor", "default")] = "cr_value"
    result = runner.invoke(auth_app, ["cursor", "clear", "--yes", "--json"])
    assert result.exit_code == 0, _combined_output(result)
    payload = json.loads(result.stdout)
    assert payload["removed"] is True
    assert payload["configured"] is False


# ── --validate flow (round-trips Cursor /v1/me) ────────────────────────


def test_set_validate_success_persists(
    fake_backend: _FakeBackend,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`set --validate` calls /v1/me, persists when 2xx."""
    from popolaloom.cli import auth_cmd

    captured: list[str] = []

    def _fake_validate(api_key: str, *, timeout_s: float = 10.0) -> str | None:
        captured.append(api_key)
        return None  # success

    monkeypatch.setattr(auth_cmd, "_validate_api_key_with_cursor", _fake_validate)
    result = runner.invoke(
        auth_app,
        ["cursor", "set", "--api-key", "cr_validate_ok", "--validate"],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert captured == ["cr_validate_ok"]
    assert backend_value(fake_backend) == "cr_validate_ok"
    output = _combined_output(result)
    assert "Validation: GET /v1/me succeeded" in output


def test_set_validate_failure_does_not_persist(
    fake_backend: _FakeBackend,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`set --validate` with rejection exits 77 and never writes the keyring."""
    from popolaloom.cli import auth_cmd

    def _fake_validate(api_key: str, *, timeout_s: float = 10.0) -> str | None:
        return "validate failed: CursorCloudAuthError: 401 unauthorized"

    monkeypatch.setattr(auth_cmd, "_validate_api_key_with_cursor", _fake_validate)
    result = runner.invoke(
        auth_app,
        ["cursor", "set", "--api-key", "cr_validate_bad", "--validate"],
    )
    assert result.exit_code == 77, _combined_output(result)
    assert backend_value(fake_backend) is None


def test_validate_helper_redacts_api_key_in_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network errors in /v1/me must not leak the literal key in the message."""
    from popolaloom.cli import auth_cmd

    class _StubClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def _request_json(self, method: str, path: str) -> dict:
            raise RuntimeError("boom: leaked-secret-cr_xyz")

        def close(self) -> None:
            pass

    monkeypatch.setattr(auth_cmd, "CloudCursorClient", _StubClient, raising=False)
    # Patch the inline import inside _validate_api_key_with_cursor too.
    import popolaloom.adapters.cursor_cloud as cursor_cloud_mod

    monkeypatch.setattr(cursor_cloud_mod, "CloudCursorClient", _StubClient)
    err = auth_cmd._validate_api_key_with_cursor("leaked-secret-cr_xyz")
    assert err is not None
    assert "leaked-secret-cr_xyz" not in err
    assert "<REDACTED:CURSOR_API_KEY>" in err


def test_validate_helper_returns_none_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 2xx /v1/me response with email returns None (success)."""
    from popolaloom.cli import auth_cmd

    class _StubClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def _request_json(self, method: str, path: str) -> dict:
            return {"userEmail": "alice@example.com"}

        def close(self) -> None:
            pass

    import popolaloom.adapters.cursor_cloud as cursor_cloud_mod

    monkeypatch.setattr(cursor_cloud_mod, "CloudCursorClient", _StubClient)
    assert auth_cmd._validate_api_key_with_cursor("cr_ok") is None


def test_validate_helper_handles_value_error_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty-key ValueError from CloudCursorClient surfaces as a redacted msg."""
    from popolaloom.cli import auth_cmd

    class _BadClient:
        def __init__(self, *args, **kwargs) -> None:
            raise ValueError("api_key must be non-empty")

    import popolaloom.adapters.cursor_cloud as cursor_cloud_mod

    monkeypatch.setattr(cursor_cloud_mod, "CloudCursorClient", _BadClient)
    err = auth_cmd._validate_api_key_with_cursor("")
    assert err is not None
    assert "client construction failed" in err


# ── interactive prompt path ────────────────────────────────────────────


def test_set_prompt_path_persists(
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    """No --api-key / --from-env → hidden-input prompt; value lands in keyring."""
    result = runner.invoke(
        auth_app,
        ["cursor", "set"],
        input="cr_prompted\n",
    )
    assert result.exit_code == 0, _combined_output(result)
    assert backend_value(fake_backend) == "cr_prompted"
    output = _combined_output(result)
    # hide_input=True → typer never echoes the value back.
    assert "cr_prompted" not in output


def test_set_prompt_path_rejects_empty(
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    """Empty / whitespace input from the prompt → exit 2."""
    result = runner.invoke(
        auth_app,
        ["cursor", "set"],
        input="   \n",
    )
    assert result.exit_code == 2, _combined_output(result)
    assert backend_value(fake_backend) is None


def test_set_keyring_backend_error_exits_3(
    fake_backend: _FakeBackend,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``store_cursor_api_key`` raises CredentialBackendError → exit 3."""
    from popolaloom.cli import auth_cmd

    def _raise(api_key: str):
        from popolaloom.credentials import CredentialBackendError

        raise CredentialBackendError("backend locked")

    monkeypatch.setattr(auth_cmd, "store_cursor_api_key", _raise)
    result = runner.invoke(auth_app, ["cursor", "set", "--api-key", "cr_x"])
    assert result.exit_code == 3, _combined_output(result)
    output = _combined_output(result)
    assert "backend locked" in output


def test_clear_env_only_warning_path(
    fake_backend: _FakeBackend,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When env var is set but keyring is empty, clear's note mentions env."""
    monkeypatch.setenv("CURSOR_API_KEY", "cr_env_only")
    # No keyring entry → clear is no-op + note about env still being set.
    result = runner.invoke(auth_app, ["cursor", "clear", "--yes"])
    assert result.exit_code == 0, _combined_output(result)
    output = _combined_output(result)
    assert "CURSOR_API_KEY" in output
    assert "still set" in output


# ── helper ──────────────────────────────────────────────────────────────


def backend_value(backend: _FakeBackend) -> str | None:
    return backend.get_password("popolaloom.cursor", "default")
