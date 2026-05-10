"""``popola auth cursor set --account-class`` v0.9.9 F5 + U1 tests.

Covers the additive ``account_class`` metadata field shipped by
:mod:`popolaloom.credentials` (Q-V099-1) and the
``popola auth cursor set --account-class=...`` capture flow that
persists it.

Acceptance-criteria coverage from the L0 brief:

* (a) ``--account-class=personal`` writes ``account_class=personal``
  into ``$POPOLA_HOME/credentials.toml`` under the ``[cursor]`` table.
* (b) ``--account-class=service-account`` normalises to
  ``service_account`` (the on-disk + :class:`AccountClass` member form).
* (c) Backward-compat: an existing ``credentials.toml`` without the
  ``account_class`` key loads cleanly with default
  :data:`AccountClass.UNKNOWN`.
* (d) Non-interactive run (no ``--account-class``, stdin is not a TTY)
  defaults to ``"unknown"`` without blocking on a prompt.
* (e) Invalid value (e.g. ``--account-class=admin``) raises
  :class:`typer.BadParameter` and exits non-zero.
* (f) Stdout (or combined output via Typer's CliRunner) contains the
  ``Recorded account_class=<value>`` confirmation line.

Hermetic via ``tmp_path`` + ``monkeypatch.setenv("POPOLA_HOME", ...)``;
the keyring is faked through ``cred_mod._import_keyring`` so no real
OS keychain is touched.
"""

from __future__ import annotations

import types
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from popolaloom import credentials as cred_mod
from popolaloom.cli.auth_cmd import app as auth_app


class _FakeBackend:
    """In-memory keyring stand-in (mirrors ``test_auth_cmd._FakeBackend``)."""

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
    tmp_path: Path,
) -> Iterator[_FakeBackend]:
    """Wire a fake keyring + isolate ``$POPOLA_HOME`` to ``tmp_path/popola``."""
    backend = _FakeBackend()
    fake_module = types.ModuleType("keyring")
    fake_module.get_keyring = lambda: backend  # type: ignore[attr-defined]
    fake_module.get_password = (  # type: ignore[attr-defined]
        lambda s, u: backend.get_password(s, u)
    )
    fake_module.set_password = (  # type: ignore[attr-defined]
        lambda s, u, v: backend.set_password(s, u, v)
    )
    fake_module.delete_password = (  # type: ignore[attr-defined]
        lambda s, u: backend.delete_password(s, u)
    )
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: fake_module)
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path / "popola"))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    yield backend


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _combined_output(result: object) -> str:
    """Concatenate ``stdout``/``stderr``/``output`` for assertion convenience."""
    stdout = getattr(result, "stdout", "") or ""
    try:
        stderr = getattr(result, "stderr", "") or ""
    except (ValueError, AttributeError):
        stderr = ""
    output = getattr(result, "output", "") or ""
    return stdout + stderr + output


def _read_metadata_text(tmp_path: Path) -> str:
    """Return the raw ``credentials.toml`` text written under POPOLA_HOME."""
    path = tmp_path / "popola" / "credentials.toml"
    assert path.is_file(), f"credentials.toml expected at {path}"
    return path.read_text(encoding="utf-8")


# ── (a) personal value persists verbatim ────────────────────────────────


def test_account_class_personal_persists_value(
    fake_backend: _FakeBackend,
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """AC (a): ``--account-class=personal`` writes ``account_class=personal``."""
    result = runner.invoke(
        auth_app,
        [
            "cursor",
            "set",
            "--api-key",
            "cr_test_personal",
            "--account-class=personal",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    metadata = cred_mod.load_credential_metadata()
    assert metadata.get("account_class") == "personal"
    assert cred_mod.get_account_class() == cred_mod.AccountClass.PERSONAL
    assert 'account_class = "personal"' in _read_metadata_text(tmp_path)


# ── (b) service-account normalises to service_account ──────────────────


def test_account_class_service_account_normalises(
    fake_backend: _FakeBackend,
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """AC (b): the dashed form is normalised to the underscored on-disk form."""
    result = runner.invoke(
        auth_app,
        [
            "cursor",
            "set",
            "--api-key",
            "cr_test_sa",
            "--account-class=service-account",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    metadata = cred_mod.load_credential_metadata()
    assert metadata.get("account_class") == "service_account"
    assert cred_mod.get_account_class() == cred_mod.AccountClass.SERVICE_ACCOUNT


def test_account_class_case_insensitive_input_is_accepted(
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    """Mixed-case input round-trips cleanly (case-insensitive whitelist)."""
    result = runner.invoke(
        auth_app,
        [
            "cursor",
            "set",
            "--api-key",
            "cr_case_test",
            "--account-class=Service-Account",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert cred_mod.get_account_class() == cred_mod.AccountClass.SERVICE_ACCOUNT


# ── (c) backward-compat with pre-v0.9.9 credentials.toml ───────────────


def test_pre_v0_9_9_metadata_loads_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AC (c): an existing metadata file without the field defaults to UNKNOWN."""
    home = tmp_path / "popola"
    home.mkdir(parents=True, exist_ok=True)
    (home / "credentials.toml").write_text(
        "# legacy v0.9.7 metadata\n"
        "[cursor]\n"
        'backend = "keyring"\n'
        'fingerprint = "aabbccddeeff"\n'
        'last_set_at = "2026-05-09T08:00:00Z"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("POPOLA_HOME", str(home))
    metadata = cred_mod.load_credential_metadata()
    assert "account_class" not in metadata
    assert metadata.get("backend") == "keyring"
    assert cred_mod.get_account_class() == cred_mod.AccountClass.UNKNOWN


# ── (d) non-interactive default: 'unknown' without prompting ───────────


def test_non_interactive_default_records_unknown_without_prompt(
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    """AC (d): no ``--account-class`` + non-TTY stdin defaults to 'unknown'.

    ``CliRunner.invoke`` runs with a non-TTY stdin by default, so the
    auth-set body MUST fall through to the ``unknown`` default rather
    than blocking on the prompt loop.
    """
    result = runner.invoke(
        auth_app,
        ["cursor", "set", "--api-key", "cr_no_class_value"],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert cred_mod.get_account_class() == cred_mod.AccountClass.UNKNOWN
    assert "Recorded account_class=unknown" in _combined_output(result)


def test_no_prompt_flag_skips_prompt_and_defaults_unknown(
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    """``--no-prompt`` is honoured even on a TTY (CI smoke path)."""
    result = runner.invoke(
        auth_app,
        ["cursor", "set", "--api-key", "cr_np_value", "--no-prompt"],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert cred_mod.get_account_class() == cred_mod.AccountClass.UNKNOWN


# ── (e) invalid value rejected via Click's BadParameter surface ────────


def test_invalid_account_class_value_exits_non_zero(
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    """AC (e): ``--account-class=admin`` raises BadParameter (exit != 0).

    Typer maps :class:`typer.BadParameter` to Click's standard
    "Invalid value" rendering on stderr with a non-zero exit. The
    keyring write must NOT happen on the rejected path (No Silent
    Failures + don't half-persist).
    """
    result = runner.invoke(
        auth_app,
        [
            "cursor",
            "set",
            "--api-key",
            "cr_must_not_persist",
            "--account-class=admin",
        ],
    )
    assert result.exit_code != 0, _combined_output(result)
    assert fake_backend.get_password("popolaloom.cursor", "default") is None


# ── (f) stdout includes the Recorded account_class=... confirmation ────


def test_set_emits_recorded_account_class_confirmation(
    fake_backend: _FakeBackend,
    runner: CliRunner,
) -> None:
    """AC (f): success path prints ``Recorded account_class=<value>``."""
    result = runner.invoke(
        auth_app,
        [
            "cursor",
            "set",
            "--api-key",
            "cr_recorded_value",
            "--account-class=service-account",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert "Recorded account_class=service_account" in _combined_output(result)


# ── credentials.store_account_class invariants ──────────────────────────


def test_store_account_class_rejects_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``credentials.store_account_class`` raises ValueError on bad input."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path / "popola"))
    for bad in ("admin", "", "   ", "ServiceAccount"):
        with pytest.raises(ValueError):
            cred_mod.store_account_class(bad)


def test_store_account_class_normalises_dashed_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The lower-level helper normalises dashes regardless of caller."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path / "popola"))
    cred_mod.store_account_class("service-account")
    assert cred_mod.get_account_class() == cred_mod.AccountClass.SERVICE_ACCOUNT
    cred_mod.store_account_class("PERSONAL")
    assert cred_mod.get_account_class() == cred_mod.AccountClass.PERSONAL


def test_get_account_class_unknown_for_garbage_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defensive: a hand-edited unrecognised value falls back to UNKNOWN + warns."""
    home = tmp_path / "popola"
    home.mkdir(parents=True, exist_ok=True)
    (home / "credentials.toml").write_text(
        "[cursor]\naccount_class = \"corrupt-value\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("POPOLA_HOME", str(home))
    with caplog.at_level("WARNING", logger="popolaloom.credentials"):
        result = cred_mod.get_account_class()
    assert result == cred_mod.AccountClass.UNKNOWN
    assert any("unrecognised account_class" in r.getMessage() for r in caplog.records)
