"""v0.8.7 T2.2.1 — popolad.toml ``[hitl.cloud]`` config loader tests.

Covers AC (a)/(b)/(c)/(d)/(e) per
``.local/.agent/active/v0.8.7-cloud-hitl-prod/PLAN.md`` §4.2 T2.2.1:

- (a) happy load → :class:`PopoladConfig` populated with all three values.
- (b) ``timeout_seconds`` outside ``[CLOUD_HITL_TIMEOUT_MIN_S,
  CLOUD_HITL_TIMEOUT_MAX_S]`` → :class:`ValueError` with a clear,
  operator-readable message (workspace rule "No Silent Failures" — the
  operator must see the section, key, and legal range explicitly).
- (c) missing ``[hitl.cloud]`` section (or missing toml file entirely) →
  documented defaults (``1800 / 3600 / 1`` per
  ``mcp-tool-contract.md`` §9 default table).
- (d) wrong type for ``timeout_seconds`` (string ``"1800"`` or bool
  ``true``) → :class:`ValueError`. Booleans are rejected explicitly because
  Python's ``isinstance(True, int) == True`` would otherwise silently
  coerce ``timeout_seconds = true`` into ``1`` (the integer value of
  ``True``) — the loader's ``_require_int`` helper catches that case.
- (e) ``CloudHITLBridge.default_timeout_s`` is wired from the loaded
  config — both via direct constructor (the explicit pass) AND via
  :func:`configure_cloud_hitl_defaults` + :func:`build_default_bridge`
  (the daemon-startup wiring path that ``daemon/main.py``
  :func:`_apply_cloud_hitl_config` uses).

The function under test is :func:`popolaloom.daemon.main.load_popolad_config`;
the clamp constants are :data:`CLOUD_HITL_TIMEOUT_MIN_S` /
:data:`CLOUD_HITL_TIMEOUT_MAX_S` (= 60 / 86400 per the contract). The
TOML grammar is the standard ``tomllib`` / ``[hitl.cloud]`` table syntax
described in the loader docstring and ``USER_GUIDE.md`` Cloud HITL §.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from popolaloom.daemon.main import (
    CLOUD_HITL_TIMEOUT_MAX_S,
    CLOUD_HITL_TIMEOUT_MIN_S,
    CloudHITLConfig,
    PopoladConfig,
    load_popolad_config,
)
from popolaloom.hitl import cloud_bridge as _cloud_bridge_module
from popolaloom.hitl.cloud_bridge import (
    CloudHITLBridge,
    build_default_bridge,
    configure_cloud_hitl_defaults,
)
from popolaloom.hitl.sync import HITLStore

_MIGRATIONS = ("006_popola_hitl.sql", "007_popola_hitl_metadata.sql")


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply both 006 + 007 so ``popola_hitl`` + ``metadata`` exist.

    Mirrors the helper in ``tests/hitl/test_cloud_bridge_replay.py`` so
    the bridge fixture in AC (e) can drive the full v0.8.7 schema.
    """
    repo_root = Path(__file__).resolve().parents[2]
    for name in _MIGRATIONS:
        sql = (repo_root / "migrations" / name).read_text(encoding="utf-8")
        conn.executescript(sql)
    conn.commit()


def _write_toml(path: Path, body: str) -> Path:
    """Write ``body`` to ``path`` and return the path (helper for tmp_path)."""
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture()
def reset_cloud_defaults() -> Generator[None, None, None]:
    """Snapshot + restore module-level ``_CLOUD_HITL_DEFAULTS``.

    AC (e) mutates the module state via
    :func:`configure_cloud_hitl_defaults`; without this fixture the
    mutation would leak to every subsequent test in the suite (defaults
    are global by design — daemon startup is the single legitimate
    caller in production). The snapshot/restore pattern preserves the
    full pre-test state so we don't have to know the production
    baseline (``600.0 / 3600 / None``) verbatim from this file.
    """
    saved: dict[str, Any] = dict(_cloud_bridge_module._CLOUD_HITL_DEFAULTS)
    yield
    _cloud_bridge_module._CLOUD_HITL_DEFAULTS.clear()
    _cloud_bridge_module._CLOUD_HITL_DEFAULTS.update(saved)


# ── AC (a) — happy load returns CloudHITLConfig with all three values ──


def test_hitl_cloud_config_load_happy(tmp_path: Path) -> None:
    """v0.8.7 AC (a): a fully-populated ``[hitl.cloud]`` table loads into
    a :class:`PopoladConfig` whose ``hitl.cloud`` field carries every
    documented setting verbatim. The dataclass ``CloudHITLConfig`` is
    the typed shape (frozen) the daemon hands to
    :func:`_apply_cloud_hitl_config`.
    """
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[hitl.cloud]\n"
        "timeout_seconds = 1800\n"
        "idempotency_window_s = 3600\n"
        "max_concurrent_per_run = 1\n",
    )
    config = load_popolad_config(p)
    assert isinstance(config, PopoladConfig)
    assert isinstance(config.hitl.cloud, CloudHITLConfig)
    assert config.hitl.cloud.timeout_seconds == 1800
    assert config.hitl.cloud.idempotency_window_s == 3600
    assert config.hitl.cloud.max_concurrent_per_run == 1


def test_hitl_cloud_config_load_happy_with_non_default_values(
    tmp_path: Path,
) -> None:
    """Sanity check: non-default but in-range values pass through unchanged
    (proves the loader is a true config reader, not a hard-coded default
    factory disguised as one)."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[hitl.cloud]\n"
        "timeout_seconds = 600\n"
        "idempotency_window_s = 7200\n"
        "max_concurrent_per_run = 3\n",
    )
    config = load_popolad_config(p)
    assert config.hitl.cloud.timeout_seconds == 600
    assert config.hitl.cloud.idempotency_window_s == 7200
    assert config.hitl.cloud.max_concurrent_per_run == 3


# ── AC (b) — out-of-range timeout_seconds rejected ─────────────────────


@pytest.mark.parametrize(
    ("value", "boundary"),
    [
        (10, "below"),  # < CLOUD_HITL_TIMEOUT_MIN_S (60)
        (59, "below"),  # one tick under the lower bound
        (86401, "above"),  # one tick over the upper bound
        (99999, "above"),  # > CLOUD_HITL_TIMEOUT_MAX_S (86400)
    ],
)
def test_hitl_cloud_config_timeout_out_of_range_rejected(
    tmp_path: Path, value: int, boundary: str
) -> None:
    """v0.8.7 AC (b): ``timeout_seconds`` outside ``[60, 86400]`` raises
    :class:`ValueError` whose message cites the section, key, and
    legal range so the operator can fix the toml without grepping
    the source (workspace rule "No Silent Failures").
    """
    p = _write_toml(
        tmp_path / "popolad.toml",
        f"[hitl.cloud]\ntimeout_seconds = {value}\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    msg = str(excinfo.value)
    # The error message MUST cite the section + key + range so an
    # operator skimming popolad's stderr knows exactly which line
    # of popolad.toml to fix.
    assert "hitl.cloud" in msg, f"({boundary}) section not in message: {msg!r}"
    assert "timeout_seconds" in msg, f"({boundary}) key not in message: {msg!r}"
    assert str(CLOUD_HITL_TIMEOUT_MIN_S) in msg, (
        f"({boundary}) min bound {CLOUD_HITL_TIMEOUT_MIN_S} not in: {msg!r}"
    )
    assert str(CLOUD_HITL_TIMEOUT_MAX_S) in msg, (
        f"({boundary}) max bound {CLOUD_HITL_TIMEOUT_MAX_S} not in: {msg!r}"
    )


def test_hitl_cloud_config_idempotency_window_out_of_range_rejected(
    tmp_path: Path,
) -> None:
    """The same range-clamp invariant applies to ``idempotency_window_s``
    (defense-in-depth: every numeric field must be range-checked, not
    just the headline timeout)."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[hitl.cloud]\nidempotency_window_s = 30\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    msg = str(excinfo.value)
    assert "idempotency_window_s" in msg, msg


def test_hitl_cloud_config_max_concurrent_out_of_range_rejected(
    tmp_path: Path,
) -> None:
    """``max_concurrent_per_run`` must be ≥ 1 (the contract caps it at 4
    per ``mcp-tool-contract.md`` §9 — a value of 0 would silently
    disable cloud HITL, which is a footgun)."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[hitl.cloud]\nmax_concurrent_per_run = 0\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    assert "max_concurrent_per_run" in str(excinfo.value)


# ── AC (c) — defaults when section / file absent ───────────────────────


def test_hitl_cloud_config_default_when_missing(tmp_path: Path) -> None:
    """v0.8.7 AC (c): a popolad.toml without ``[hitl.cloud]`` section
    returns the documented defaults (``1800 / 3600 / 1``) so existing
    v0.8.5 deployments keep working without touching their config.
    """
    p = _write_toml(
        tmp_path / "popolad.toml",
        '# popolad config without the new [hitl.cloud] section\n'
        '[some.other.legacy.section]\nkey = "value"\n',
    )
    config = load_popolad_config(p)
    assert config.hitl.cloud.timeout_seconds == 1800
    assert config.hitl.cloud.idempotency_window_s == 3600
    assert config.hitl.cloud.max_concurrent_per_run == 1


def test_hitl_cloud_config_default_when_file_missing(tmp_path: Path) -> None:
    """Corollary of AC (c): no popolad.toml at all → still defaults
    (the file is optional; v0.8.5 deployments never had one)."""
    p = tmp_path / "no-such-file.toml"
    assert not p.exists()
    config = load_popolad_config(p)
    assert config.hitl.cloud.timeout_seconds == 1800
    assert config.hitl.cloud.idempotency_window_s == 3600
    assert config.hitl.cloud.max_concurrent_per_run == 1


def test_hitl_cloud_config_default_when_section_empty(tmp_path: Path) -> None:
    """An empty ``[hitl.cloud]`` table (no keys) also returns defaults —
    proves the per-key defaults compose with the section-level default."""
    p = _write_toml(tmp_path / "popolad.toml", "[hitl.cloud]\n")
    config = load_popolad_config(p)
    assert config.hitl.cloud.timeout_seconds == 1800
    assert config.hitl.cloud.idempotency_window_s == 3600
    assert config.hitl.cloud.max_concurrent_per_run == 1


# ── AC (d) — wrong-type rejection (string + bool) ──────────────────────


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ('"1800"', "string"),  # string instead of int → rejected
        ("true", "bool"),  # bool — Python coerces to int silently otherwise
        ("false", "bool"),  # both bool literals must be rejected
        ("1.5", "float"),  # float is not a strict int either
    ],
)
def test_hitl_cloud_config_invalid_type_rejected(
    tmp_path: Path, value: str, kind: str
) -> None:
    """v0.8.7 AC (d): wrong-type values surface :class:`ValueError`
    immediately. The bool-rejection path is the most subtle one —
    Python's ``isinstance(True, int)`` returns ``True``, so without
    the explicit ``isinstance(value, bool)`` short-circuit in
    ``_require_int`` the loader would silently coerce
    ``timeout_seconds = true`` into the integer ``1`` (one second timeout —
    a nasty footgun). This test fences against that regression.
    """
    p = _write_toml(
        tmp_path / "popolad.toml",
        f"[hitl.cloud]\ntimeout_seconds = {value}\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    msg = str(excinfo.value)
    assert "timeout_seconds" in msg, f"({kind}) key missing in error: {msg!r}"
    # The message must mention the integer requirement so the operator
    # understands what went wrong (per workspace rule "No Silent Failures").
    assert "integer" in msg.lower() or "int" in msg.lower(), (
        f"({kind}) integer-type hint missing: {msg!r}"
    )


def test_hitl_cloud_config_section_must_be_table(tmp_path: Path) -> None:
    """If the operator typed ``[hitl] cloud = "..."`` (string instead of
    sub-table), the loader rejects with a clear error rather than
    silently dropping the cloud overrides."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        '[hitl]\ncloud = "not-a-table"\n',
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    assert "hitl.cloud" in str(excinfo.value)


# ── AC (b) + (e) — bridge picks up default_timeout_s from config ───────


def test_cloud_bridge_default_timeout_s_threaded_from_config(
    tmp_path: Path,
    reset_cloud_defaults: None,
) -> None:
    """v0.8.7 AC (b)/(e): the bridge's ``default_timeout_s`` field is wired
    from the loaded popolad.toml config — both via direct
    :class:`CloudHITLBridge` construction (the explicit pass) AND via
    :func:`configure_cloud_hitl_defaults` + :func:`build_default_bridge`
    (the daemon-startup wiring path that
    :func:`popolaloom.daemon.main._apply_cloud_hitl_config` uses).

    Operators set the value once in popolad.toml; every subsequent
    bridge construction inherits it without ``daemon/rpc.py`` touching
    the bridge constructor (T2.1.3 territory — keep the surgical patch
    surface minimal).
    """
    _ = reset_cloud_defaults  # fixture's restore runs at teardown
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[hitl.cloud]\ntimeout_seconds = 1234\nidempotency_window_s = 4567\n",
    )
    config = load_popolad_config(p)
    assert config.hitl.cloud.timeout_seconds == 1234

    db_path = tmp_path / "thread.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)

    # Path 1 — direct constructor (the bridge accepts the typed kwarg).
    store = HITLStore(conn)
    bridge_direct = CloudHITLBridge(
        store,
        lark_notifier=None,
        default_timeout_s=float(config.hitl.cloud.timeout_seconds),
    )
    assert bridge_direct._default_timeout_s == 1234.0

    # Path 2 — configure_cloud_hitl_defaults + build_default_bridge.
    # Mirrors the production wiring in `daemon/main.py::_apply_cloud_hitl_config`.
    configure_cloud_hitl_defaults(
        default_timeout_s=float(config.hitl.cloud.timeout_seconds),
        idempotency_window_s=int(config.hitl.cloud.idempotency_window_s),
    )
    bridge_via_factory = build_default_bridge(conn)
    assert bridge_via_factory._default_timeout_s == 1234.0

    # Explicit override at factory time still wins (callers retain control).
    bridge_override = build_default_bridge(conn, default_timeout_s=2222.0)
    assert bridge_override._default_timeout_s == 2222.0

    conn.close()


def test_cloud_bridge_no_arg_submit_request_uses_configured_default(
    tmp_path: Path,
    reset_cloud_defaults: None,
) -> None:
    """v0.8.7 AC (e) end-to-end: a no-``timeout_s`` ``submit_request`` call
    materialises the configured default into the row's ``deadline_at``.
    Proves the wiring is live (not just a stored attribute) — the
    deadline is a function of ``self._default_timeout_s`` via
    :func:`_ceil_deadline_seconds`.
    """
    _ = reset_cloud_defaults
    configure_cloud_hitl_defaults(default_timeout_s=900.0)

    db_path = tmp_path / "no-arg.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)

    bridge = build_default_bridge(conn)
    req = bridge.submit_request(
        task_id="t-no-arg",
        cursor_agent_id="ag",
        cursor_run_id="rn",
        prompt_title="t",
        prompt_body="b",
        options=[{"id": "yes", "label": "Y"}, {"id": "no", "label": "N"}],
    )
    # The HITLPrompt's deadline_seconds is the resolved default (900),
    # propagated by `_ceil_deadline_seconds(timeout_s=None, default=900.0)`.
    assert req.prompt.deadline_seconds == 900
    # And the row's deadline_at is roughly created_at + 900 s (allow wide
    # slack for slow CI: anything in the (895, 905) window proves wiring).
    delta = (req.deadline_at - req.created_at).total_seconds()
    assert 895.0 <= delta <= 905.0, f"deadline drift: {delta} s"

    conn.close()
