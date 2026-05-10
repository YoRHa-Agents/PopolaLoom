"""Tests for ``popola init --target=cloud-only --configure-cursor-auth`` (v0.9.2+).

The flag is the secure-credential-setup hook for the cloud-only init
flow:

* When the keyring extra is unavailable, the helper prints an
  actionable hint and returns without prompting (so a CI run on a
  vanilla Linux box without libsecret does not hang).
* When the keyring is available and the operator declines the prompt,
  no secret is stored.
* When the operator accepts and provides a value, the OS keyring
  receives the value AND a fingerprint banner is printed (the literal
  value is NEVER echoed).
* ``--dry-run`` short-circuits the credential prompt entirely (No
  Silent Failures: never prompt for a secret during a preview).
* The flag is rejected on a non-cloud-only / non-interactive top-level
  invocation (e.g. ``popola init --configure-cursor-auth``) — surfaces a
  ``BadParameter`` rather than silently no-op.
"""

from __future__ import annotations

import types
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from popolaloom import credentials as cred_mod
from popolaloom.cli.init_cmd import app as init_app


class _FakeBackend:
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
def isolated_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, Path]]:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path / "popola"))
    yield cwd, fake_home


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeBackend]:
    backend = _FakeBackend()
    fake_module = types.ModuleType("keyring")
    fake_module.get_keyring = lambda: backend  # type: ignore[attr-defined]
    fake_module.get_password = lambda s, u: backend.get_password(s, u)  # type: ignore[attr-defined]
    fake_module.set_password = lambda s, u, v: backend.set_password(s, u, v)  # type: ignore[attr-defined]
    fake_module.delete_password = lambda s, u: backend.delete_password(s, u)  # type: ignore[attr-defined]
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: fake_module)
    yield backend


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _combined_output(result: object) -> str:
    stdout = getattr(result, "stdout", "") or ""
    try:
        stderr = getattr(result, "stderr", "") or ""
    except (ValueError, AttributeError):
        stderr = ""
    output = getattr(result, "output", "") or ""
    return stdout + stderr + output


# ── happy path: prompt accepted, secret stored ──────────────────────────


def test_cloud_only_with_configure_cursor_auth_stores_secret(
    isolated_home: tuple[Path, Path],
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    cwd, _ = isolated_home
    # Stdin: "y\n" for the confirm, then the secret value
    result = runner.invoke(
        init_app,
        ["--target=cloud-only", "--configure-cursor-auth"],
        input="y\ncr_init_secret\n",
    )
    output = _combined_output(result)
    assert result.exit_code == 0, output
    assert (cwd / "popolad.toml").is_file()
    assert fake_backend.store.get(("popolaloom.cursor", "default")) == "cr_init_secret"
    # Fingerprint is printed but secret is not.
    assert "cr_init_secret" not in output


def test_cloud_only_with_configure_cursor_auth_declined(
    isolated_home: tuple[Path, Path],
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    cwd, _ = isolated_home
    result = runner.invoke(
        init_app,
        ["--target=cloud-only", "--configure-cursor-auth"],
        input="n\n",
    )
    assert result.exit_code == 0, _combined_output(result)
    assert (cwd / "popolad.toml").is_file()
    assert fake_backend.store == {}


def test_cloud_only_with_configure_cursor_auth_empty_input_aborts(
    isolated_home: tuple[Path, Path],
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    """Empty/whitespace prompt input → no secret stored, no crash."""
    result = runner.invoke(
        init_app,
        ["--target=cloud-only", "--configure-cursor-auth"],
        input="y\n   \n",
    )
    assert result.exit_code == 0, _combined_output(result)
    assert fake_backend.store == {}
    output = _combined_output(result)
    assert "Empty input" in output or "skipping" in output.lower()


# ── degraded path: keyring missing ──────────────────────────────────────


def test_cloud_only_configure_cursor_auth_without_keyring_extra(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keyring extra absent → the helper prints a hint and returns.

    The scaffold itself still succeeds; only the credential-setup step
    is skipped (with a clear message pointing the user at the env-var
    fallback).
    """
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: None)
    cwd, _ = isolated_home
    result = runner.invoke(
        init_app,
        ["--target=cloud-only", "--configure-cursor-auth"],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert (cwd / "popolad.toml").is_file()
    output = _combined_output(result)
    assert "keyring backend unavailable" in output.lower()
    # v0.9.7 (closes feedback_for_v0.9.4 line 1): the helper points operators
    # at the canonical installer flag, NOT at a raw `pip install`.
    assert "./install.sh install --with-credentials" in output
    assert "pip install" not in output
    assert "popolaloom[credentials]" not in output


# ── --dry-run short-circuits the prompt ────────────────────────────────


def test_dry_run_does_not_prompt_for_secret(
    isolated_home: tuple[Path, Path],
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    """``--dry-run`` skips the credential prompt entirely.

    Empty stdin would hang the prompt; the test passing without input
    proves the dry-run branch never asks.
    """
    cwd, _ = isolated_home
    result = runner.invoke(
        init_app,
        ["--target=cloud-only", "--configure-cursor-auth", "--dry-run"],
    )
    assert result.exit_code == 0, _combined_output(result)
    output = _combined_output(result)
    assert "DRY" in output
    assert fake_backend.store == {}
    # Files are NOT written.
    assert not (cwd / "popolad.toml").exists()


# ── flag is now allowed on every init path (v0.9.5 + feedback_for_v0.9.4) ─


def test_configure_cursor_auth_on_auto_detect_path_runs_helper(
    isolated_home: tuple[Path, Path],
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    """``popola init --configure-cursor-auth`` (no target/interactive) now runs the helper.

    v0.9.5 (closes ``.local/feedbacks/feedback_for_v0.9.4.md``): the flag
    is accepted on every init path. Previously this combination raised
    ``BadParameter`` on the bare auto-detect lane; with the v0.9.5 init-
    time credential intake the flag triggers
    :func:`popolaloom.cli.init_cmd._offer_cursor_credential_setup` after
    the auto-install loop completes. Operator declines the prompt → no
    secret stored, scaffold still succeeds.
    """
    cwd, _ = isolated_home
    (cwd / ".local").mkdir()  # silences the auto-detect 'local' branch
    result = runner.invoke(
        init_app,
        ["--configure-cursor-auth"],
        input="n\n",  # declines the keyring prompt
    )
    output = _combined_output(result)
    assert result.exit_code == 0, output
    assert "Secure Cursor API key storage" in output
    assert fake_backend.store == {}
    # Auto-detect still runs to completion: cursor (default fallback)
    # SKILL.md is on disk.
    assert (cwd / ".cursor" / "skills" / "popola-loom" / "SKILL.md").is_file()
