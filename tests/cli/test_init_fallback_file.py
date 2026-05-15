"""Tests for the v0.9.9 U2 0600 fallback file at ``~/.popola/cursor_api_key.env``.

Closes ``.local/feedbacks/feedback_for_v0.9.7.md:114-116`` (U2 ask
"另外初始化传入 secret 没能正确缓存，需要优化"). Q-V099-11 + Q-V099-12
lock the new behavior:

1. When ``popola init --cursor-api-key VAL`` runs on a host without a
   working keyring backend (typical headless Linux container), v0.9.9
   writes a 0600 file at ``$POPOLA_HOME/cursor_api_key.env``
   containing exactly ``CURSOR_API_KEY=<value>\\n`` instead of
   silently dropping the operator's secret (the v0.9.7 deliberate-bug
   behavior).
2. Stdout includes a follow-up line that names the file path AND the
   ``source`` command for fresh shells (operator-facing recovery
   instruction).
3. The write is idempotent — re-running the same flag with a new
   value replaces the file content (``O_TRUNC`` semantics).
4. On a host WITH a working keyring backend, the fallback file is
   NOT written (the keyring path stays primary).
5. The file is born with mode 0o600 — security-critical for a
   secret slot on disk.

Companion suites:

- ``tests/cli/test_init_credential_intake.py`` — replaces the
  v0.9.7 deliberate-bug-pinning ``test_cursor_api_key_without_
  keyring_backend_prints_hint_and_returns_zero`` with the new
  positive-behavior assertion.
- ``tests/daemon/test_daemon_auto_source.py`` — covers Q-V099-12
  (daemon startup auto-sources the fallback file).
"""

from __future__ import annotations

import os
import types
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from popolaloom import credentials as cred_mod
from popolaloom.cli.init_cmd import app as init_app


class _FakeBackend:
    """In-memory fake of the upstream ``keyring`` module's backend protocol.

    Mirrors :class:`tests.cli.test_init_credential_intake._FakeBackend`
    so the two suites share the same hermetic fixture shape.
    ``__module__`` is ``"fake.backend"`` (does NOT end with ``.fail``)
    so :func:`popolaloom.credentials.is_keyring_available` returns
    True.
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
def isolated_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, Path]]:
    """Yield ``(cwd, fake_home)`` with ``Path.home`` + ``$POPOLA_HOME`` patched.

    Mirrors the fixture in ``tests/cli/test_init_credential_intake.py``
    so the two suites share the same isolation contract. ``$POPOLA_HOME``
    is pinned at ``tmp_path / "popola"`` so the fallback file lands in
    a per-test directory rather than the developer's real
    ``~/.popola``. ``CURSOR_API_KEY`` is unset so an existing shell
    value never leaks into the resolver precedence chain.
    """
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
def runner() -> CliRunner:
    return CliRunner()


def _combined_output(result: object) -> str:
    """Return ``result.stdout`` + best-effort ``result.stderr`` + ``output``."""
    stdout = getattr(result, "stdout", "") or ""
    try:
        stderr = getattr(result, "stderr", "") or ""
    except (ValueError, AttributeError):
        stderr = ""
    output = getattr(result, "output", "") or ""
    return stdout + stderr + output


def _no_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force :func:`is_keyring_available` to return False (no backend).

    Patches the lazy-import seam ``credentials._import_keyring`` to
    return ``None`` so the helper's keyring path short-circuits to the
    fallback-file branch.
    """
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: None)


def _fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeBackend:
    """Wire a fake keyring backend so ``is_keyring_available`` returns True.

    The fake stores secrets in-process so tests can assert on
    ``backend.store`` after the helper runs. Returns the backend so
    tests can introspect what was stored.
    """
    backend = _FakeBackend()
    fake_module = types.ModuleType("keyring")
    fake_module.get_keyring = lambda: backend  # type: ignore[attr-defined]
    fake_module.get_password = lambda s, u: backend.get_password(s, u)  # type: ignore[attr-defined]
    fake_module.set_password = lambda s, u, v: backend.set_password(s, u, v)  # type: ignore[attr-defined]
    fake_module.delete_password = lambda s, u: backend.delete_password(s, u)  # type: ignore[attr-defined]
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: fake_module)
    return backend


# ── (a) on a host without keyring, fallback file is written 0o600 ──────


def test_init_without_keyring_writes_fallback_file_with_correct_payload(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola init --cursor-api-key`` on a host without keyring writes the file.

    Acceptance criterion (a) of v0.9.9 U2: the file at
    ``$POPOLA_HOME/cursor_api_key.env`` exists, has mode 0o600, and
    contains exactly ``export CURSOR_API_KEY=<raw_key>\\n``.
    """
    cwd, _ = isolated_home
    (cwd / ".cursor").mkdir()
    _no_keyring(monkeypatch)

    result = runner.invoke(init_app, ["--cursor-api-key", "crsr_test_VAL"])
    assert result.exit_code == 0, _combined_output(result)

    fallback_path = cred_mod._env_fallback_path()
    assert fallback_path.is_file()
    assert fallback_path.read_text(encoding="utf-8") == "export CURSOR_API_KEY=crsr_test_VAL\n"


# ── (b) stdout follow-up line includes ``source`` instruction ──────────


def test_stdout_follow_up_line_names_source_instruction(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stdout MUST include the ``source ~/.popola/cursor_api_key.env`` instruction.

    Acceptance criterion (b) of v0.9.9 U2: operators on a fresh shell
    after init MUST be able to discover the manual ``source`` recovery
    path from the init stdout alone — they should not need to grep
    USER_GUIDE.md to learn how to use the file.
    """
    cwd, _ = isolated_home
    (cwd / ".cursor").mkdir()
    _no_keyring(monkeypatch)

    result = runner.invoke(init_app, ["--cursor-api-key", "crsr_X"])
    out = _combined_output(result)
    assert result.exit_code == 0, out
    assert "Wrote fallback to" in out
    assert "cursor_api_key.env" in out
    assert "source" in out
    assert "auto-source" in out
    # Mode advertisement so operators can verify the file is 0600
    # without re-reading USER_GUIDE.md.
    assert "mode 0600" in out


# ── (c) idempotent re-write replaces the file content ─────────────────


def test_fallback_file_rewrite_replaces_previous_value(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running with VAL2 after VAL1 replaces the file content (not appends).

    Acceptance criterion (c) of v0.9.9 U2: the writer uses
    ``O_WRONLY|O_CREAT|O_TRUNC`` so a second invocation of
    ``popola init --cursor-api-key VAL2`` overwrites the file in place
    rather than appending VAL2 below VAL1. This pins the
    ``write_env_fallback`` ``O_TRUNC`` flag against an accidental
    switch to ``O_APPEND``.
    """
    cwd, _ = isolated_home
    (cwd / ".cursor").mkdir()
    _no_keyring(monkeypatch)

    result1 = runner.invoke(init_app, ["--cursor-api-key", "crsr_VAL1"])
    assert result1.exit_code == 0, _combined_output(result1)
    fallback_path = cred_mod._env_fallback_path()
    assert fallback_path.read_text(encoding="utf-8") == "export CURSOR_API_KEY=crsr_VAL1\n"

    result2 = runner.invoke(init_app, ["--cursor-api-key", "crsr_VAL2"])
    assert result2.exit_code == 0, _combined_output(result2)
    # The file MUST be replaced, not appended.
    assert fallback_path.read_text(encoding="utf-8") == "export CURSOR_API_KEY=crsr_VAL2\n"
    # The previous value MUST be gone.
    assert "crsr_VAL1" not in fallback_path.read_text(encoding="utf-8")


# ── (d) operator with working keyring sees no fallback file ───────────


def test_keyring_available_does_not_write_fallback_file(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the keyring is available, the keyring path stays primary (no fallback).

    Acceptance criterion (d) of v0.9.9 U2: an operator on a host with
    a working keyring backend (macOS Keychain, Windows Credential
    Manager, libsecret on Linux) MUST NOT see a fallback file written
    on disk — the keyring is always preferred when available, and the
    on-disk slot is reserved for the headless degraded path.
    """
    cwd, _ = isolated_home
    (cwd / ".cursor").mkdir()
    backend = _fake_keyring(monkeypatch)

    result = runner.invoke(init_app, ["--cursor-api-key", "crsr_keyring_only"])
    out = _combined_output(result)
    assert result.exit_code == 0, out
    assert backend.store.get(("popolaloom.cursor", "default")) == "crsr_keyring_only"
    # Fallback file must NOT exist (keyring path took the secret).
    fallback_path = cred_mod._env_fallback_path()
    assert not fallback_path.exists(), (
        "fallback file MUST NOT be written when the keyring backend is "
        "available — the keyring path is the primary slot for v0.9.9"
    )


# ── (e) mode 0o600 verified directly via os.stat ──────────────────────


def test_fallback_file_has_mode_0o600(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback file is born with mode 0o600 (security-critical).

    Acceptance criterion (e) of v0.9.9 U2: a world-readable or
    group-readable secret slot on disk is a security regression. The
    writer uses ``os.open(..., O_CREAT, 0o600)`` so the file is born
    with the right permissions (avoids the race where a plaintext
    file briefly exists with the umask default before a follow-up
    chmod tightens it). This test verifies the post-write
    permissions directly via :func:`os.stat`.
    """
    cwd, _ = isolated_home
    (cwd / ".cursor").mkdir()
    _no_keyring(monkeypatch)

    result = runner.invoke(init_app, ["--cursor-api-key", "crsr_perm_check"])
    assert result.exit_code == 0, _combined_output(result)
    fallback_path = cred_mod._env_fallback_path()
    assert fallback_path.is_file()
    actual_mode = os.stat(fallback_path).st_mode & 0o777
    assert actual_mode == 0o600, (
        f"fallback file MUST be mode 0o600; got {oct(actual_mode)}"
    )


# ── direct unit-level coverage of the new helpers ─────────────────────


class TestWriteEnvFallback:
    """Direct branch coverage for :func:`credentials.write_env_fallback`.

    The CLI path tests above exercise the helper indirectly through
    ``popola init``; these tests pin the boundary semantics directly
    so a future refactor (e.g. switching to ``mkstemp + rename``)
    preserves the same observable contract.
    """

    def test_writes_payload_with_trailing_newline(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("POPOLA_HOME", str(tmp_path / "popola"))
        path = cred_mod.write_env_fallback("crsr_alpha")
        assert path == cred_mod._env_fallback_path()
        assert path.read_text(encoding="utf-8") == "export CURSOR_API_KEY=crsr_alpha\n"

    def test_strips_whitespace_around_key_value(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The helper strips surrounding whitespace from ``raw_key`` before write."""
        monkeypatch.setenv("POPOLA_HOME", str(tmp_path / "popola"))
        path = cred_mod.write_env_fallback("  crsr_beta  ")
        assert path.read_text(encoding="utf-8") == "export CURSOR_API_KEY=crsr_beta\n"

    def test_empty_or_whitespace_raises_value_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Empty / whitespace-only keys raise :class:`ValueError`.

        Mirrors the contract on :func:`store_cursor_api_key` so the
        two paths reject the same invalid inputs.
        """
        monkeypatch.setenv("POPOLA_HOME", str(tmp_path / "popola"))
        for bad in ("", "   ", "\t\n  "):
            with pytest.raises(ValueError, match="non-empty"):
                cred_mod.write_env_fallback(bad)

    def test_idempotent_truncates_previous_content(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Re-writing replaces the file content (``O_TRUNC`` semantics)."""
        monkeypatch.setenv("POPOLA_HOME", str(tmp_path / "popola"))
        cred_mod.write_env_fallback("crsr_first")
        path = cred_mod.write_env_fallback("crsr_second")
        assert path.read_text(encoding="utf-8") == "export CURSOR_API_KEY=crsr_second\n"

    def test_creates_parent_directory_when_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The helper creates ``$POPOLA_HOME`` (mode 0o700) when missing.

        Mirrors :func:`save_credential_metadata`'s behaviour so a
        first-run write does not require pre-existing infrastructure.
        """
        target = tmp_path / "fresh-popola"
        assert not target.exists()
        monkeypatch.setenv("POPOLA_HOME", str(target))
        path = cred_mod.write_env_fallback("crsr_first_run")
        assert path.is_file()
        assert path.parent == target.resolve()
        # Parent created with mode 0o700 (defense in depth: even
        # though the file itself is 0o600, a 0o755 parent still leaks
        # the FILE NAME via directory listing — 0o700 hides both).
        parent_mode = os.stat(path.parent).st_mode & 0o777
        assert parent_mode == 0o700, oct(parent_mode)
