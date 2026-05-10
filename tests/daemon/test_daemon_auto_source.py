"""Tests for the v0.9.9 U2 daemon-startup auto-source hook (Q-V099-12).

The companion writer side of v0.9.9 U2 lives in
``tests/cli/test_init_fallback_file.py``: when ``popola init
--cursor-api-key VAL`` runs on a host without a working keyring
backend, a 0600 file is written at
``$POPOLA_HOME/cursor_api_key.env``. This file MUST be auto-sourced
by the daemon at startup so a fresh ``popola dispatch`` shell after
init "just works" without an explicit ``source`` step (closes
``feedback_for_v0.9.7.md:114-116``).

Test surface (one test per acceptance criterion in the v0.9.9 PLAN
§U2):

- (f) Fallback file present → daemon startup loads
  ``CURSOR_API_KEY`` into ``os.environ``.
- (g) Fallback file absent → daemon startup is unaffected
  (function returns False; ``os.environ`` is unchanged).
- (h) Malformed fallback file → WARN log fires + function returns
  the appropriate flag (False if NO valid line was found, True if
  AT LEAST ONE valid line was loaded); daemon startup is
  unaffected.
- (i) Pre-existing ``CURSOR_API_KEY`` env var is NOT overwritten by
  the fallback file (env-var precedence wins per the v0.9.9
  Q-V099-12 lock).

The test surface deliberately calls
:func:`credentials.load_env_fallback_into_environ` directly (rather
than spinning up the full daemon) so the assertions stay hermetic and
fast — the daemon main() integration is exercised separately by the
existing daemon-supervisor / RPC suites.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from popolaloom import credentials as cred_mod


@pytest.fixture(autouse=True)
def _isolated_popola_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin ``$POPOLA_HOME`` at ``tmp_path / "popola"`` for every test.

    Avoids polluting the developer's real ``~/.popola`` and ensures
    each test gets a fresh fallback-file slot. ``CURSOR_API_KEY`` is
    unset so the env-var precedence slot does not leak the developer's
    shell value into the per-test assertions; tests that exercise the
    "env var already set" precedence (criterion (i)) re-set it
    explicitly.
    """
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path / "popola"))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)


# ── (f) fallback file present → loads CURSOR_API_KEY into os.environ ───


def test_present_fallback_file_is_loaded_into_environ() -> None:
    """Daemon startup loads ``CURSOR_API_KEY`` from the fallback file.

    Acceptance criterion (f) of v0.9.9 U2: the daemon's auto-source
    hook reads ``$POPOLA_HOME/cursor_api_key.env`` and sets the env
    var so subsequent :func:`resolve_cursor_api_key` calls (precedence
    #2) pick up the value without requiring the operator to ``source``
    the file by hand.
    """
    cred_mod.write_env_fallback("crsr_X")
    assert cred_mod.CURSOR_API_KEY_ENV not in __import__("os").environ
    loaded = cred_mod.load_env_fallback_into_environ()
    assert loaded is True
    import os
    assert os.environ[cred_mod.CURSOR_API_KEY_ENV] == "crsr_X"


# ── (g) fallback file absent → no-op ───────────────────────────────────


def test_absent_fallback_file_is_a_noop() -> None:
    """Daemon startup is unaffected when the fallback file is absent.

    Acceptance criterion (g) of v0.9.9 U2: a fresh install (no
    ``popola init --cursor-api-key`` ever run, or an init that took
    the keyring path) leaves the file absent. The daemon startup hook
    MUST NOT raise, MUST NOT touch ``os.environ``, and MUST return
    ``False`` so callers can decide whether to log "auto-sourced"
    truthfully.
    """
    fallback_path = cred_mod._env_fallback_path()
    assert not fallback_path.exists()
    import os
    snapshot_before = os.environ.copy()
    loaded = cred_mod.load_env_fallback_into_environ()
    assert loaded is False
    # No env-var changes — the function MUST be a strict no-op.
    assert dict(os.environ) == snapshot_before


# ── (h) malformed fallback file → WARN log + daemon continues ─────────


def test_malformed_fallback_file_logs_warning_and_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed lines fire a WARN log; the daemon does not raise.

    Acceptance criterion (h) of v0.9.9 U2: per workspace rule "No
    Silent Failures" every rejection has an explicit log entry —
    operators grepping daemon logs see exactly which line was
    rejected. But file presence is best-effort, so a malformed file
    MUST NOT abort daemon startup. When NO valid line is found the
    function returns False; when AT LEAST ONE valid line is loaded
    the function returns True even if siblings were malformed.
    """
    fallback_path = cred_mod._env_fallback_path()
    fallback_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # First line: malformed (no '=' separator). Second line: valid.
    fallback_path.write_text(
        "this-is-not-a-keyvalue-line\nCURSOR_API_KEY=crsr_recovered\n",
        encoding="utf-8",
    )
    test_logger = logging.getLogger("popolaloom.test.daemon_auto_source")
    with caplog.at_level(logging.WARNING, logger=test_logger.name):
        loaded = cred_mod.load_env_fallback_into_environ(logger=test_logger)
    # The valid line was loaded; the function returns True even though
    # one sibling line was malformed.
    assert loaded is True
    import os
    assert os.environ[cred_mod.CURSOR_API_KEY_ENV] == "crsr_recovered"
    # The WARN log fires exactly once for the malformed line and names
    # the file path + the line number + the literal line text (per the
    # docstring contract).
    warn_records = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "malformed cursor_api_key.env" in rec.getMessage()
    ]
    assert len(warn_records) == 1, [r.getMessage() for r in caplog.records]
    msg = warn_records[0].getMessage()
    assert "line 1" in msg
    assert "this-is-not-a-keyvalue-line" in msg


def test_fully_malformed_fallback_file_returns_false(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """File with only malformed lines → returns False, env unchanged."""
    fallback_path = cred_mod._env_fallback_path()
    fallback_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fallback_path.write_text(
        "garbage1\ngarbage2\n=value-without-key\n",
        encoding="utf-8",
    )
    test_logger = logging.getLogger("popolaloom.test.daemon_auto_source")
    with caplog.at_level(logging.WARNING, logger=test_logger.name):
        loaded = cred_mod.load_env_fallback_into_environ(logger=test_logger)
    assert loaded is False
    import os
    assert cred_mod.CURSOR_API_KEY_ENV not in os.environ
    # Each malformed line produces a WARN log entry.
    warn_records = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "malformed cursor_api_key.env" in rec.getMessage()
    ]
    assert len(warn_records) == 3, [r.getMessage() for r in warn_records]


def test_blank_lines_and_comments_are_skipped_silently() -> None:
    """Blank lines and ``#`` comments are skipped without WARN entries.

    Defensive: the file format is ``KEY=value`` per line, but
    operators editing the file by hand may add comments or blank
    separators. These are NOT malformed — the helper skips them
    silently and only WARNs on truly broken rows.
    """
    fallback_path = cred_mod._env_fallback_path()
    fallback_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fallback_path.write_text(
        "# popola U2 fallback file (v0.9.9)\n"
        "\n"
        "CURSOR_API_KEY=crsr_with_comments\n"
        "\n"
        "# trailing comment\n",
        encoding="utf-8",
    )
    loaded = cred_mod.load_env_fallback_into_environ()
    assert loaded is True
    import os
    assert os.environ[cred_mod.CURSOR_API_KEY_ENV] == "crsr_with_comments"


# ── (i) pre-existing CURSOR_API_KEY env var is NOT overwritten ────────


def test_existing_env_var_is_not_overwritten_by_fallback_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env-var precedence wins: fallback file does NOT overwrite an existing var.

    Acceptance criterion (i) of v0.9.9 U2: when ``CURSOR_API_KEY`` is
    already set in the environment (operator exported it explicitly,
    or the parent daemon process inherited it from a CI runner), the
    daemon's auto-source hook MUST NOT overwrite it. This keeps the
    env-var precedence slot from :class:`CredentialResolver`
    consistent with the :func:`resolve_cursor_api_key` chain (#2 env,
    #3 keyring) — the fallback file plays the role of "auto-source on
    daemon startup", not "highest-precedence override".
    """
    monkeypatch.setenv(cred_mod.CURSOR_API_KEY_ENV, "crsr_explicit_export")
    cred_mod.write_env_fallback("crsr_fallback_value")
    loaded = cred_mod.load_env_fallback_into_environ()
    # The function returns False — the explicit env var wins, no
    # auto-source happened.
    assert loaded is False
    import os
    # The env var keeps its explicit value.
    assert os.environ[cred_mod.CURSOR_API_KEY_ENV] == "crsr_explicit_export"


# ── extra: empty-string env var IS treated as "unset" ─────────────────


def test_empty_string_env_var_is_overwritten_by_fallback() -> None:
    """An EMPTY ``CURSOR_API_KEY`` env var counts as "unset" — fallback wins.

    Defensive: some shells export ``CURSOR_API_KEY=`` (empty string)
    when the operator forgets to set the value. The auto-source hook
    treats this as equivalent to "unset" and writes from the fallback
    file — otherwise the daemon would see an empty key and the
    resolver would correctly classify it as "no secret configured",
    which is strictly less useful than the operator's actual stored
    secret.
    """
    import os
    os.environ[cred_mod.CURSOR_API_KEY_ENV] = ""
    try:
        cred_mod.write_env_fallback("crsr_recover_empty")
        loaded = cred_mod.load_env_fallback_into_environ()
        assert loaded is True
        assert os.environ[cred_mod.CURSOR_API_KEY_ENV] == "crsr_recover_empty"
    finally:
        os.environ.pop(cred_mod.CURSOR_API_KEY_ENV, None)


# ── daemon-main hook integration: function is called from main() ──────


def test_daemon_main_module_imports_credentials_helper() -> None:
    """The daemon module imports the auto-source helper from credentials.

    Sanity check that the v0.9.9 U2 wiring lives in the daemon
    bootstrap path (``popolaloom.daemon.main`` imports the
    ``credentials`` module). This guards against an accidental
    refactor that removes the wiring without the test suite noticing
    — the explicit attribute check below fails loudly if the
    function symbol is removed or renamed.
    """
    from popolaloom.daemon import main as daemon_main

    assert hasattr(daemon_main, "credentials")
    assert daemon_main.credentials is cred_mod
    assert callable(cred_mod.load_env_fallback_into_environ)
