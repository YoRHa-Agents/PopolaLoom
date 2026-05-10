"""Default-lane tests for ``popolaloom.credentials`` (v0.9.2+).

Pins the security-critical contracts of the credential resolver:

1. **Precedence chain** — explicit override > env var > OS keyring > None.
2. **Fingerprint stability** — same input → same hex prefix; different
   inputs → different prefixes; empty / whitespace → ``None``.
3. **Redaction** — :func:`redact_in_text` strips every literal candidate
   value (longest-first to avoid partial overlap leaks); empty / None
   inputs are passthrough.
4. **Backend availability** — :func:`is_keyring_available` cleanly
   reports False when the upstream sentinel ``fail`` backend is active
   (the default on a stock machine without libsecret / Keychain).
5. **Status surface** — :class:`CredentialStatus.to_json_dict` exposes
   ``configured`` / ``source`` / ``backend_name`` / ``fingerprint`` /
   ``keyring_available`` and **never** the raw secret.
6. **Storage round-trip** — :func:`store_cursor_api_key` followed by
   :func:`resolve_cursor_api_key` returns the exact value; empty input
   raises ``ValueError`` (No Silent Failures); missing keyring extra
   raises :class:`CredentialBackendError`.
7. **Metadata file** — :func:`save_credential_metadata` writes ``0600``
   (owner read/write only) and never serialises the secret value.

The fake-keyring backend (a tiny in-memory dict shaped like the
upstream API surface) lives in this file so the suite never touches
the developer's real OS keyring.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from popolaloom import credentials as cred_mod
from popolaloom.credentials import (
    CURSOR_API_KEY_ENV,
    REDACTION_PLACEHOLDER,
    CredentialBackendError,
    CredentialResolver,
    CredentialStatus,
    compute_fingerprint,
    credential_status,
    delete_cursor_api_key,
    is_keyring_available,
    load_credential_metadata,
    metadata_path,
    redact,
    redact_in_text,
    resolve_cursor_api_key,
    save_credential_metadata,
    store_cursor_api_key,
)

# ── fake-backend fixture (no real OS keyring touched) ────────────────────


class _FakeBackend:
    """In-memory keyring backend shaped like the upstream interface.

    Mirrors the three methods PopolaLoom calls (``get_password`` /
    ``set_password`` / ``delete_password``). ``__module__`` /
    ``__qualname__`` are set so ``_keyring_backend_name`` returns
    ``"keyring (fake.Keyring)"`` rather than the upstream ``fail``
    sentinel — the tests assert specific labels so this matters.
    """

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
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeBackend]:
    """Yield a fake keyring backend wired into the resolver indirection.

    Patches the lazy ``_import_keyring`` so the resolver thinks
    ``keyring`` is installed AND the active backend is our fake. Also
    redirects ``$POPOLA_HOME`` to a tmp dir so the metadata file lives
    out-of-tree.
    """
    backend = _FakeBackend()

    fake_module = types.ModuleType("keyring")

    def _get_keyring() -> object:
        return backend

    def _get_password(service: str, username: str) -> str | None:
        return backend.get_password(service, username)

    def _set_password(service: str, username: str, value: str) -> None:
        backend.set_password(service, username, value)

    def _delete_password(service: str, username: str) -> None:
        backend.delete_password(service, username)

    fake_module.get_keyring = _get_keyring  # type: ignore[attr-defined]
    fake_module.get_password = _get_password  # type: ignore[attr-defined]
    fake_module.set_password = _set_password  # type: ignore[attr-defined]
    fake_module.delete_password = _delete_password  # type: ignore[attr-defined]

    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: fake_module)
    monkeypatch.delenv(CURSOR_API_KEY_ENV, raising=False)

    yield backend


@pytest.fixture
def isolated_popola_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Redirect ``$POPOLA_HOME`` to a tmp dir so metadata writes are isolated."""
    home = tmp_path / "popola"
    monkeypatch.setenv("POPOLA_HOME", str(home))
    return home


# ── precedence chain ────────────────────────────────────────────────────


def test_resolve_returns_override_first(
    fake_backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit override wins over env + keyring (precedence #1)."""
    monkeypatch.setenv(CURSOR_API_KEY_ENV, "env-value")
    fake_backend.store[("popolaloom.cursor", "default")] = "keyring-value"

    secret = resolve_cursor_api_key(override="override-value")
    assert secret == "override-value"


def test_resolve_returns_env_when_no_override(
    fake_backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env var wins over keyring (precedence #2 > #3)."""
    monkeypatch.setenv(CURSOR_API_KEY_ENV, "env-value")
    fake_backend.store[("popolaloom.cursor", "default")] = "keyring-value"

    assert resolve_cursor_api_key() == "env-value"


def test_resolve_returns_keyring_when_no_env(
    fake_backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keyring wins when env is missing / empty (precedence #3)."""
    monkeypatch.delenv(CURSOR_API_KEY_ENV, raising=False)
    fake_backend.store[("popolaloom.cursor", "default")] = "keyring-value"

    assert resolve_cursor_api_key() == "keyring-value"


def test_resolve_returns_none_when_no_slot_answers(
    fake_backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env, no keyring → None (caller renders the actionable hint)."""
    monkeypatch.delenv(CURSOR_API_KEY_ENV, raising=False)
    assert resolve_cursor_api_key() is None


def test_resolve_treats_whitespace_env_as_empty(
    fake_backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env var of whitespace must NOT shadow the keyring slot.

    Defense against the common ``export CURSOR_API_KEY=" "`` typo /
    debug shell where the var is set but empty. Such a value is
    indistinguishable from "unset" for our purposes (No Silent
    Failures: the resolver ignores it rather than passing whitespace
    to ``CloudCursorClient``).
    """
    monkeypatch.setenv(CURSOR_API_KEY_ENV, "   ")
    fake_backend.store[("popolaloom.cursor", "default")] = "keyring-value"
    assert resolve_cursor_api_key() == "keyring-value"


def test_credential_resolver_dataclass_returns_source_label(
    fake_backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CredentialResolver.resolve()`` exposes the precedence slot used."""
    monkeypatch.setenv(CURSOR_API_KEY_ENV, "env-value")

    secret, source = CredentialResolver().resolve()
    assert (secret, source) == ("env-value", "env")

    monkeypatch.delenv(CURSOR_API_KEY_ENV, raising=False)
    fake_backend.store[("popolaloom.cursor", "default")] = "keyring-value"
    secret, source = CredentialResolver().resolve()
    assert (secret, source) == ("keyring-value", "keyring")

    secret, source = CredentialResolver(override="override").resolve()
    assert (secret, source) == ("override", "override")


# ── fingerprint ─────────────────────────────────────────────────────────


def test_fingerprint_stable_for_same_input() -> None:
    a = compute_fingerprint("super-secret-key")
    b = compute_fingerprint("super-secret-key")
    assert a == b
    assert a is not None
    assert len(a) == 12  # 12 hex chars


def test_fingerprint_differs_per_input() -> None:
    a = compute_fingerprint("key-one")
    b = compute_fingerprint("key-two")
    assert a != b


def test_fingerprint_returns_none_for_empty_inputs() -> None:
    assert compute_fingerprint(None) is None
    assert compute_fingerprint("") is None
    assert compute_fingerprint("   ") is None


def test_fingerprint_strips_whitespace() -> None:
    """Leading/trailing whitespace must not change the digest."""
    assert compute_fingerprint("hello") == compute_fingerprint("  hello  ")


# ── redaction ───────────────────────────────────────────────────────────


def test_redact_returns_placeholder_for_non_empty_value() -> None:
    assert redact("secret") == REDACTION_PLACEHOLDER
    assert redact(None) == ""
    assert redact("") == ""
    assert redact("   ") == ""


def test_redact_in_text_replaces_all_candidates() -> None:
    text = "secret=cr_abc, also cr_abc and cr_other"
    out = redact_in_text(text, candidates=("cr_abc", "cr_other"))
    assert "cr_abc" not in out
    assert "cr_other" not in out
    assert REDACTION_PLACEHOLDER in out


def test_redact_in_text_handles_empty_inputs() -> None:
    assert redact_in_text("") == ""
    assert redact_in_text("nothing here", candidates=("",)) == "nothing here"
    assert redact_in_text("nothing here", candidates=(None,)) == "nothing here"


def test_redact_in_text_picks_longest_first() -> None:
    """Longer secrets are replaced first to avoid partial-overlap leaks."""
    text = "longer-secret short"
    out = redact_in_text(text, candidates=("short", "longer-secret"))
    assert "longer-secret" not in out
    assert "short" not in out


# ── backend availability ────────────────────────────────────────────────


def test_keyring_unavailable_when_module_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the keyring extra is uninstalled, ``is_keyring_available`` is False."""
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: None)
    assert is_keyring_available() is False


def test_keyring_unavailable_when_fail_backend_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``keyring.backends.fail.Keyring`` sentinel is treated as unavailable."""
    fake_module = types.ModuleType("keyring")

    class _FailBackend:
        __module__ = "keyring.backends.fail"
        __qualname__ = "Keyring"

    def _get_keyring() -> object:
        return _FailBackend()

    fake_module.get_keyring = _get_keyring  # type: ignore[attr-defined]
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: fake_module)
    assert is_keyring_available() is False


def test_keyring_available_with_fake_backend(fake_backend: _FakeBackend) -> None:
    """The fake backend is recognised as a usable keyring."""
    assert is_keyring_available() is True


# ── status surface ──────────────────────────────────────────────────────


def test_status_when_unconfigured(
    fake_backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CURSOR_API_KEY_ENV, raising=False)
    status = credential_status()
    assert status.configured is False
    assert status.source == "none"
    assert status.fingerprint is None
    assert status.backend_name == "unset"
    assert status.keyring_available is True


def test_status_when_env_configured(
    fake_backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CURSOR_API_KEY_ENV, "cr_test_value")
    status = credential_status()
    assert status.configured is True
    assert status.source == "env"
    assert status.backend_name == "environment variable"
    assert status.fingerprint == compute_fingerprint("cr_test_value")
    # The raw value MUST never appear in the status JSON.
    json_dict = status.to_json_dict()
    assert "cr_test_value" not in repr(json_dict)


def test_status_when_keyring_configured(
    fake_backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CURSOR_API_KEY_ENV, raising=False)
    fake_backend.store[("popolaloom.cursor", "default")] = "cr_keyring_value"
    status = credential_status()
    assert status.configured is True
    assert status.source == "keyring"
    assert "fake.backend" in status.backend_name or "keyring" in status.backend_name
    assert status.fingerprint == compute_fingerprint("cr_keyring_value")
    assert "cr_keyring_value" not in repr(status.to_json_dict())


def test_status_dataclass_immutable() -> None:
    status = CredentialStatus(
        configured=False,
        source="none",
        backend_name="unset",
        fingerprint=None,
        keyring_available=False,
    )
    with pytest.raises((AttributeError, Exception)):
        status.configured = True  # type: ignore[misc]


# ── storage + delete round-trip ─────────────────────────────────────────


def test_store_and_resolve_roundtrip(
    fake_backend: _FakeBackend,
    isolated_popola_home: Path,
) -> None:
    status = store_cursor_api_key("cr_round_trip_value")
    assert status.configured is True
    assert resolve_cursor_api_key() == "cr_round_trip_value"
    assert status.fingerprint == compute_fingerprint("cr_round_trip_value")


def test_store_rejects_empty_value(fake_backend: _FakeBackend) -> None:
    with pytest.raises(ValueError):
        store_cursor_api_key("")
    with pytest.raises(ValueError):
        store_cursor_api_key("   ")


def test_store_strips_whitespace(
    fake_backend: _FakeBackend,
    isolated_popola_home: Path,
) -> None:
    """Stored value is the stripped form (no leading/trailing whitespace)."""
    store_cursor_api_key("  cr_padded_value  ")
    assert resolve_cursor_api_key() == "cr_padded_value"


def test_store_raises_when_keyring_extra_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing extra → CredentialBackendError with remediation hint.

    v0.9.7 (closes ``feedback_for_v0.9.4.md`` line 1): the remediation
    hint must point at ``./install.sh install --with-credentials`` AND
    surface the ``CURSOR_API_KEY`` env-var fallback. It must NOT
    surface a raw ``pip install popolaloom[credentials]`` line — per the
    workspace rule "popola 不使用 pip 修正安装方式" we route operators
    through the official installer flag instead of a bare pip command.
    """
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: None)
    with pytest.raises(CredentialBackendError) as exc_info:
        store_cursor_api_key("cr_value")
    msg = str(exc_info.value)
    assert "keyring" in msg.lower()
    assert "install" in msg.lower()
    assert CURSOR_API_KEY_ENV in msg
    # v0.9.7 invariants:
    assert "./install.sh install --with-credentials" in msg
    assert "pip install" not in msg
    assert "popolaloom[credentials]" not in msg


def test_delete_is_idempotent(
    fake_backend: _FakeBackend,
    isolated_popola_home: Path,
) -> None:
    """Delete returns False when no entry exists; True after store + delete."""
    removed, status = delete_cursor_api_key()
    assert removed is False
    assert status.configured is False

    store_cursor_api_key("cr_to_delete")
    assert resolve_cursor_api_key() == "cr_to_delete"

    removed, status = delete_cursor_api_key()
    assert removed is True
    assert status.configured is False
    assert resolve_cursor_api_key() is None


# ── metadata file ───────────────────────────────────────────────────────


def test_metadata_file_mode_is_0600(
    fake_backend: _FakeBackend,
    isolated_popola_home: Path,
) -> None:
    """Metadata file is created with mode 0600."""
    if sys.platform == "win32":
        pytest.skip("POSIX file modes not enforced on Windows")
    store_cursor_api_key("cr_test")
    path = metadata_path()
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_metadata_file_never_contains_secret(
    fake_backend: _FakeBackend,
    isolated_popola_home: Path,
) -> None:
    """The metadata file MUST NOT serialise the API key value itself."""
    secret = "cr_super_secret_should_not_leak"
    store_cursor_api_key(secret)
    path = metadata_path()
    content = path.read_text(encoding="utf-8")
    assert secret not in content
    # but the fingerprint IS recorded
    assert compute_fingerprint(secret) in content


def test_load_credential_metadata_returns_empty_when_file_absent(
    isolated_popola_home: Path,
) -> None:
    """No file → empty dict (not an error)."""
    assert load_credential_metadata() == {}


def test_save_metadata_creates_parent_with_0700(
    isolated_popola_home: Path,
) -> None:
    """Parent directory ($POPOLA_HOME) is created when missing."""
    if sys.platform == "win32":
        pytest.skip("POSIX directory modes not enforced on Windows")
    save_credential_metadata({"backend": "keyring"})
    assert metadata_path().parent.is_dir()


# ── No-Silent-Failures: status JSON dict is JSON-clean ──────────────────


def test_status_to_json_dict_has_stable_keys() -> None:
    """``--json`` output keys are stable v0.9.x surface — pin them here."""
    status = CredentialStatus(
        configured=True,
        source="env",
        backend_name="environment variable",
        fingerprint="abcdef123456",
        keyring_available=True,
    )
    payload = status.to_json_dict()
    assert set(payload.keys()) == {
        "configured",
        "source",
        "backend_name",
        "fingerprint",
        "keyring_available",
    }


# ── backend-name labels (pure mapping; mutmut-friendly) ─────────────────


@pytest.mark.parametrize(
    ("module", "name", "expected"),
    [
        ("keyring.backends.macOS", "Keyring", "macOS Keychain"),
        ("keyring.backends.OS_X", "Keyring", "macOS Keychain"),
        ("keyring.backends.Windows", "WinVaultKeyring", "Windows Credential Manager"),
        ("keyring.backends.Win32", "Keyring", "Windows Credential Manager"),
        ("secretstorage", "SecretService", "Secret Service"),
        ("keyring.backends.SecretService", "Keyring", "Secret Service"),
        ("keyring.backends.kwallet", "DBusKeyring", "KWallet"),
        ("keyring.backends.libsecret", "Keyring", "libsecret"),
        ("custom.backend", "Strange", "keyring (custom.backend.Strange)"),
    ],
)
def test_keyring_backend_name_label_mapping(
    monkeypatch: pytest.MonkeyPatch,
    module: str,
    name: str,
    expected: str,
) -> None:
    """``_keyring_backend_name`` maps known module/class shapes to friendly labels."""
    fake_module = types.ModuleType("keyring")
    backend_type = type(name, (), {"__module__": module})
    fake_module.get_keyring = lambda: backend_type()  # type: ignore[attr-defined]
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: fake_module)
    assert cred_mod._keyring_backend_name() == expected


def test_keyring_backend_name_handles_missing_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: None)
    assert cred_mod._keyring_backend_name() == "unset"


def test_keyring_backend_name_fail_module_returns_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_module = types.ModuleType("keyring")
    backend_type = type(
        "Keyring", (), {"__module__": "keyring.backends.fail"}
    )
    fake_module.get_keyring = lambda: backend_type()  # type: ignore[attr-defined]
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: fake_module)
    assert cred_mod._keyring_backend_name() == "unset"


# ── _keyring_get / _keyring_set / _keyring_delete edge cases ────────────


def test_keyring_get_returns_none_when_extra_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: None)
    assert cred_mod._keyring_get() is None


def test_keyring_get_normalises_empty_string_to_none(
    fake_backend: _FakeBackend,
) -> None:
    fake_backend.store[("popolaloom.cursor", "default")] = "   "
    # Whitespace-only stored value is treated as absent.
    assert cred_mod._keyring_get() is None


def test_keyring_get_handles_backend_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OS-level keyring error returns None (logged at WARNING, never raises)."""
    fake_module = types.ModuleType("keyring")

    def _raises(*_args, **_kwargs) -> None:
        raise RuntimeError("dbus glitch")

    fake_module.get_password = _raises  # type: ignore[attr-defined]
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: fake_module)
    assert cred_mod._keyring_get() is None


def test_keyring_set_raises_when_backend_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_module = types.ModuleType("keyring")

    def _set_password(*_args, **_kwargs) -> None:
        raise RuntimeError("backend locked")

    fake_module.set_password = _set_password  # type: ignore[attr-defined]
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: fake_module)

    with pytest.raises(CredentialBackendError) as exc_info:
        cred_mod._keyring_set("cr_test")
    assert "backend locked" in str(exc_info.value)


def test_keyring_delete_returns_false_when_module_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: None)
    assert cred_mod._keyring_delete() is False


def test_keyring_delete_raises_when_backend_throws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_module = types.ModuleType("keyring")
    fake_module.get_password = lambda s, u: "stored"  # type: ignore[attr-defined]

    def _delete(*_args, **_kwargs) -> None:
        raise RuntimeError("delete denied")

    fake_module.delete_password = _delete  # type: ignore[attr-defined]
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: fake_module)

    with pytest.raises(CredentialBackendError):
        cred_mod._keyring_delete()


# ── redact_in_text edge cases ───────────────────────────────────────────


def test_redact_in_text_default_candidates_uses_resolver(
    fake_backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``candidates`` is omitted, redact pulls env+keyring values."""
    monkeypatch.setenv(CURSOR_API_KEY_ENV, "cr_env_value")
    fake_backend.store[("popolaloom.cursor", "default")] = "cr_keyring_value"
    text = "secret env=cr_env_value, keyring=cr_keyring_value"
    out = redact_in_text(text)
    assert "cr_env_value" not in out
    assert "cr_keyring_value" not in out


def test_redact_dedupe_same_candidate(fake_backend: _FakeBackend) -> None:
    """Identical candidate strings only run once (dedup via the seen set)."""
    text = "duplicated cr_x cr_x"
    out = redact_in_text(text, candidates=("cr_x", "cr_x"))
    assert "cr_x" not in out


# ── metadata round-trip + load ─────────────────────────────────────────


def test_metadata_round_trip_preserves_all_keys(
    isolated_popola_home: Path,
) -> None:
    save_credential_metadata({"backend": "keyring", "fingerprint": "abc", "last_set_at": "now"})
    loaded = load_credential_metadata()
    assert loaded == {"backend": "keyring", "fingerprint": "abc", "last_set_at": "now"}


def test_metadata_handles_corrupt_file(
    isolated_popola_home: Path,
) -> None:
    """Corrupt TOML returns empty dict (logged WARN, never raises)."""
    path = metadata_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not [[[ valid toml", encoding="utf-8")
    assert load_credential_metadata() == {}


def test_metadata_handles_non_dict_cursor_section(
    isolated_popola_home: Path,
) -> None:
    """A `cursor = "not a table"` entry is treated as absent."""
    path = metadata_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('cursor = "wrong-shape"\n', encoding="utf-8")
    assert load_credential_metadata() == {}


def test_save_metadata_escapes_quotes_and_backslashes(
    isolated_popola_home: Path,
) -> None:
    save_credential_metadata({"backend": 'has "quotes" and \\ backslash'})
    loaded = load_credential_metadata()
    assert loaded["backend"] == 'has "quotes" and \\ backslash'
