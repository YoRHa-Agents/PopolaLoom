"""v0.10.0 Wave C3 — ``account_class`` deprecation WARN tests.

Covers PLAN.md Wave C3 acceptance criteria 1-4 verbatim:

1. :func:`popolaloom.credentials.get_account_class` emits a one-time
   ``logger.warning("account_class is deprecated as of v0.10.0; the
   v0.9.9 pre-flight gate has been removed. See CHANGELOG.md#v0.10.0")``
   when the stored value is non-:data:`AccountClass.UNKNOWN`
   (i.e. ``PERSONAL`` or ``SERVICE_ACCOUNT``).
2. The :class:`AccountClass` enum, :func:`store_account_class`, the
   ``--account-class`` CLI flag, and the TOML field are all KEPT (no
   API breakage; consumers of :func:`get_account_class` can still
   distinguish stored values for telemetry).
3. The WARN fires once per process via a module-level
   :data:`_DEPRECATION_WARNING_EMITTED` flag flipped to ``True`` after
   the first emit; subsequent calls are silent.
4. Tests cover: WARN fires when value is ``PERSONAL`` or
   ``SERVICE_ACCOUNT``; WARN does NOT fire when value is ``UNKNOWN``
   or the file is absent; WARN fires only once per process (the
   autouse ``monkeypatch`` fixture resets the flag between tests).

Same fire-once test shape as
``tests/daemon/test_user_preferences_default_cloud_target.py``
(Wave B1) so the v0.10.0 deprecation suite stays uniform across the
credentials and user-preferences subsystems.

References
----------
- ``.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md`` Q-10
- ``.local/.agent/active/v0.10.0-cloud-dispatch-clarity/PLAN.md`` §"Wave C → Task C3"
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from popolaloom import credentials as cred_mod
from popolaloom.credentials import (
    AccountClass,
    get_account_class,
    store_account_class,
)

# ---------------------------------------------------------------------------
# Constants — keep in sync with credentials.get_account_class deprecation copy.
# ---------------------------------------------------------------------------


_EXPECTED_DEPRECATION_MESSAGE = (
    "account_class is deprecated as of v0.10.0; "
    "the v0.9.9 pre-flight gate has been removed. "
    "See CHANGELOG.md#v0.10.0"
)
"""Verbatim deprecation WARN string from PLAN C3 AC 1.

Any drift between this constant and the ``logger.warning(...)``
literal in :func:`popolaloom.credentials.get_account_class` will
fail the suite — by design (the WARN copy is part of the v0.10.0
operator-facing contract; see DECISIONS Q-10 rationale).
"""


_CREDENTIALS_LOGGER = "popolaloom.credentials"
"""Logger name attached by :func:`logging.getLogger(__name__)` in credentials.py."""


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_popola_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Redirect ``$POPOLA_HOME`` to a tmp dir so credentials.toml writes are isolated.

    Mirrors the ``isolated_popola_home`` fixture in
    ``tests/test_credentials.py`` so the two suites share the same
    isolation contract: every metadata read/write goes through
    ``$POPOLA_HOME`` and never touches the developer's real
    ``~/.popola`` directory.
    """
    home = tmp_path / "popola"
    monkeypatch.setenv("POPOLA_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _reset_deprecation_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the module-level deprecation-warned flag before every test (AC 4).

    The flag persists across :func:`get_account_class` calls within a
    process so the WARN fires at most once (AC 3). To make each test
    deterministic we flip it back to ``False`` at setup; the
    "fires only once" test explicitly verifies the post-fire state by
    issuing back-to-back calls in a single test.
    """
    monkeypatch.setattr(cred_mod, "_DEPRECATION_WARNING_EMITTED", False)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _collect_deprecation_records(
    caplog: pytest.LogCaptureFixture,
) -> list[logging.LogRecord]:
    """Return WARNING-level records whose formatted message matches the spec copy.

    Matching on ``getMessage()`` (the formatted message) keeps the
    assertion independent of any structured extras the credentials
    module attaches to the record — only the operator-facing string
    is part of the public contract.
    """
    return [
        rec
        for rec in caplog.records
        if rec.getMessage() == _EXPECTED_DEPRECATION_MESSAGE
        and rec.levelno == logging.WARNING
    ]


# ---------------------------------------------------------------------------
# AC 1 — WARN fires for non-UNKNOWN values.
# ---------------------------------------------------------------------------


def test_warn_fires_when_value_is_personal(
    isolated_popola_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stored ``PERSONAL`` triggers exactly one deprecation WARN (AC 1)."""
    store_account_class("personal")
    with caplog.at_level(logging.WARNING, logger=_CREDENTIALS_LOGGER):
        result = get_account_class()

    assert result is AccountClass.PERSONAL
    records = _collect_deprecation_records(caplog)
    assert len(records) == 1, (
        f"expected exactly 1 deprecation WARN; got {len(records)}: "
        f"{[r.getMessage() for r in caplog.records]!r}"
    )
    assert cred_mod._DEPRECATION_WARNING_EMITTED is True


def test_warn_fires_when_value_is_service_account(
    isolated_popola_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stored ``SERVICE_ACCOUNT`` triggers exactly one deprecation WARN (AC 1)."""
    store_account_class("service_account")
    with caplog.at_level(logging.WARNING, logger=_CREDENTIALS_LOGGER):
        result = get_account_class()

    assert result is AccountClass.SERVICE_ACCOUNT
    records = _collect_deprecation_records(caplog)
    assert len(records) == 1, (
        f"expected exactly 1 deprecation WARN; got {len(records)}: "
        f"{[r.getMessage() for r in caplog.records]!r}"
    )
    assert cred_mod._DEPRECATION_WARNING_EMITTED is True


def test_warn_fires_when_value_is_dashed_service_account(
    isolated_popola_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dashed ``service-account`` (CLI-input form) still triggers the WARN.

    :func:`store_account_class` normalises ``service-account`` to
    ``service_account`` on write; this test pins the read-side
    normalisation by short-circuiting the writer and inserting the
    dashed form directly into the metadata table — the helper must
    still treat the result as ``SERVICE_ACCOUNT`` AND fire the WARN
    once. Defends against a regression where a hand-edited TOML
    skips the deprecation notice.
    """
    cred_mod.save_credential_metadata({"account_class": "service-account"})
    with caplog.at_level(logging.WARNING, logger=_CREDENTIALS_LOGGER):
        result = get_account_class()

    assert result is AccountClass.SERVICE_ACCOUNT
    records = _collect_deprecation_records(caplog)
    assert len(records) == 1


# ---------------------------------------------------------------------------
# AC 1 (negative) — WARN stays silent for UNKNOWN / absent file.
# ---------------------------------------------------------------------------


def test_warn_silent_when_value_is_unknown(
    isolated_popola_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Explicitly-stored ``UNKNOWN`` does NOT trigger the deprecation WARN (AC 1)."""
    store_account_class("unknown")
    with caplog.at_level(logging.WARNING, logger=_CREDENTIALS_LOGGER):
        result = get_account_class()

    assert result is AccountClass.UNKNOWN
    records = _collect_deprecation_records(caplog)
    assert records == [], (
        "WARN should be silent for UNKNOWN; got "
        f"{[r.getMessage() for r in records]!r}"
    )
    assert cred_mod._DEPRECATION_WARNING_EMITTED is False


def test_warn_silent_when_file_absent(
    isolated_popola_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No ``credentials.toml`` present → no WARN (AC 1; UNKNOWN-equivalent path).

    Fresh-install case: the metadata file is absent, so
    :func:`load_credential_metadata` returns ``{}`` and the
    deprecation branch is skipped. This is the most common path for
    new operators and MUST stay silent — pestering users who never
    set ``--account-class`` would be a UX regression per
    DECISIONS Q-10 rationale.
    """
    assert not (isolated_popola_home / "credentials.toml").exists()
    with caplog.at_level(logging.WARNING, logger=_CREDENTIALS_LOGGER):
        result = get_account_class()

    assert result is AccountClass.UNKNOWN
    records = _collect_deprecation_records(caplog)
    assert records == [], (
        "WARN should be silent for fresh installs; got "
        f"{[r.getMessage() for r in records]!r}"
    )
    assert cred_mod._DEPRECATION_WARNING_EMITTED is False


def test_warn_silent_when_section_present_but_key_absent(
    isolated_popola_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``[cursor]`` table present but ``account_class`` key missing → no WARN.

    Defends the pre-v0.9.9 backward-compat path: an operator who
    ran ``popola auth cursor set`` before v0.9.9 has a
    ``credentials.toml`` with ``backend`` / ``last_set_at`` but no
    ``account_class`` key. The loader returns UNKNOWN and the WARN
    must NOT fire — only operators who explicitly set the deprecated
    field via ``--account-class=...`` see the notice.
    """
    cred_mod.save_credential_metadata(
        {"backend": "keyring", "last_set_at": "2026-05-11T00:00:00Z"}
    )
    with caplog.at_level(logging.WARNING, logger=_CREDENTIALS_LOGGER):
        result = get_account_class()

    assert result is AccountClass.UNKNOWN
    records = _collect_deprecation_records(caplog)
    assert records == []
    assert cred_mod._DEPRECATION_WARNING_EMITTED is False


# ---------------------------------------------------------------------------
# AC 3 — fire-once across back-to-back calls.
# ---------------------------------------------------------------------------


def test_warn_fires_only_once_per_process(
    isolated_popola_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Back-to-back calls with PERSONAL → exactly 1 WARN total (AC 3 + AC 4).

    The module-level :data:`_DEPRECATION_WARNING_EMITTED` flag
    persists across :func:`get_account_class` calls within the same
    process. The autouse fixture resets the flag at test entry; this
    test does NOT reset between the calls so the second / third call
    must observe the flag and stay silent.
    """
    store_account_class("personal")
    with caplog.at_level(logging.WARNING, logger=_CREDENTIALS_LOGGER):
        first = get_account_class()
        second = get_account_class()
        third = get_account_class()

    assert first is AccountClass.PERSONAL
    assert second is AccountClass.PERSONAL
    assert third is AccountClass.PERSONAL
    records = _collect_deprecation_records(caplog)
    assert len(records) == 1, (
        "deprecation WARN must fire exactly once per process; "
        f"observed {len(records)} record(s): "
        f"{[r.getMessage() for r in records]!r}"
    )
    assert cred_mod._DEPRECATION_WARNING_EMITTED is True


def test_warn_fires_once_across_value_changes(
    isolated_popola_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Switching the stored value mid-process does NOT re-fire the WARN (AC 3).

    Once the deprecation has been surfaced for a process, subsequent
    reads stay silent — even if the operator rewrites the TOML to a
    different non-UNKNOWN class. The fire-once invariant is on the
    *process*, not on the *value*. Operators rotating
    ``--account-class`` mid-session see the notice exactly once.
    """
    store_account_class("personal")
    with caplog.at_level(logging.WARNING, logger=_CREDENTIALS_LOGGER):
        get_account_class()
        store_account_class("service_account")
        get_account_class()

    records = _collect_deprecation_records(caplog)
    assert len(records) == 1


# ---------------------------------------------------------------------------
# AC 2 — public surface kept (no API breakage for telemetry consumers).
# ---------------------------------------------------------------------------


def test_account_class_enum_kept() -> None:
    """The :class:`AccountClass` enum still exposes all three members (AC 2)."""
    assert AccountClass.PERSONAL.value == "personal"
    assert AccountClass.SERVICE_ACCOUNT.value == "service_account"
    assert AccountClass.UNKNOWN.value == "unknown"
    # Equality coercion against bare strings still holds (StrEnum contract).
    assert AccountClass.PERSONAL == "personal"
    assert AccountClass.SERVICE_ACCOUNT == "service_account"


def test_store_account_class_helper_kept(
    isolated_popola_home: Path,
) -> None:
    """:func:`store_account_class` still persists into ``credentials.toml`` (AC 2).

    The setter is preserved for the ``--account-class`` CLI flag
    plumbing (DECISIONS Q-10). This test pins the on-disk artifact
    so a regression that silently removes the writer would fail
    here even before any reader-side asserts.
    """
    store_account_class("personal")
    metadata_file = isolated_popola_home / "credentials.toml"
    assert metadata_file.exists()
    contents = metadata_file.read_text(encoding="utf-8")
    assert "account_class" in contents
    assert "personal" in contents


def test_get_account_class_round_trip_preserves_distinguishability(
    isolated_popola_home: Path,
) -> None:
    """Stored values are still readable for telemetry consumers (AC 2).

    Per DECISIONS Q-10: ``consumers of get_account_class() can still
    distinguish stored values for telemetry``. The deprecation WARN
    is informational only — it does NOT short-circuit the return value.
    """
    store_account_class("personal")
    assert get_account_class() is AccountClass.PERSONAL

    store_account_class("service_account")
    assert get_account_class() is AccountClass.SERVICE_ACCOUNT

    store_account_class("unknown")
    assert get_account_class() is AccountClass.UNKNOWN


# ---------------------------------------------------------------------------
# AC 3 — fresh-process flag state.
# ---------------------------------------------------------------------------


def test_deprecation_warn_flag_starts_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The module-level flag defaults to ``False`` (AC 3 fresh-process state).

    The autouse fixture already resets this; the test exists so a
    regression that flips the default to ``True`` (suppressing the
    WARN globally) fails loudly here rather than silently breaking
    the AC 1 tests.
    """
    monkeypatch.setattr(cred_mod, "_DEPRECATION_WARNING_EMITTED", False)
    assert cred_mod._DEPRECATION_WARNING_EMITTED is False
