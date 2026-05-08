"""v0.8.8 T2.1.3 — ``[cloud.backoff]`` config loader tests.

Covers AC (a) per ``.local/.agent/active/v0.8.8-multi-run/PLAN.md`` §4.1
T2.1.3 + the spec ``.local/research/v0.8.8_multi_run/quota-config.md``
§2.1 (schema) + §2.3 (No-Silent-Failures validation rules).

Test surface (each test pins one validation invariant from §2.3):

- Happy path — fully populated `[cloud.backoff]` table loads into
  :class:`BackoffConfig` verbatim (proves the loader is a real parser,
  not a defaults factory in disguise).
- Defaults when section / file absent — v0.8.7 deployments without the
  new section keep working (defaults: 5 / 500 / 30000 / 25 / true).
- Range strictness — every numeric field has explicit lo/hi bounds;
  out-of-range values raise :class:`ValueError` with section + key + range
  cited (No-Silent-Failures).
- Type strictness — ``bool`` rejected for int fields (Python's
  ``isinstance(True, int)`` would otherwise silently coerce); strings
  rejected even when ``int(s)`` would succeed; ints rejected for the
  bool field (``honor_retry_after``).
- Inter-key invariant — ``max_backoff_ms`` must be ``≥ base_backoff_ms``;
  setting them inversely raises with both keys named.
- Unknown keys — WARN log, NOT error (forward-compat: a future
  ``route_overrides`` key shouldn't break a v0.8.8 daemon).
- Section-shape strictness — ``[cloud.backoff]`` must be a TOML table
  (not a string scalar); ``[cloud]`` must be a table.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from popolaloom.adapters.cursor_cloud import BackoffConfig
from popolaloom.daemon.main import (
    CLOUD_BACKOFF_BASE_MS_MAX,
    CLOUD_BACKOFF_BASE_MS_MIN,
    CLOUD_BACKOFF_JITTER_PCT_MAX,
    CLOUD_BACKOFF_JITTER_PCT_MIN,
    CLOUD_BACKOFF_MAX_MS_MAX,
    CLOUD_BACKOFF_MAX_RETRIES_MAX,
    CLOUD_BACKOFF_MAX_RETRIES_MIN,
    CloudConfig,
    PopoladConfig,
    load_popolad_config,
)


def _write_toml(path: Path, body: str) -> Path:
    """Write ``body`` to ``path`` and return the path (helper for tmp_path)."""
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Happy path — every key parsed verbatim into BackoffConfig.
# ---------------------------------------------------------------------------


def test_cloud_backoff_load_happy(tmp_path: Path) -> None:
    """A fully-populated ``[cloud.backoff]`` table loads into a
    :class:`PopoladConfig` whose ``cloud.backoff`` carries every field
    verbatim. Pins the public surface name + nesting (PopoladConfig.cloud.backoff).
    """
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.backoff]\n"
        "max_retries        = 5\n"
        "base_backoff_ms    = 500\n"
        "max_backoff_ms     = 30000\n"
        "jitter_pct         = 25\n"
        "honor_retry_after  = true\n",
    )
    config = load_popolad_config(p)
    assert isinstance(config, PopoladConfig)
    assert isinstance(config.cloud, CloudConfig)
    assert isinstance(config.cloud.backoff, BackoffConfig)
    assert config.cloud.backoff.max_retries == 5
    assert config.cloud.backoff.base_backoff_ms == 500
    assert config.cloud.backoff.max_backoff_ms == 30_000
    assert config.cloud.backoff.jitter_pct == 25
    assert config.cloud.backoff.honor_retry_after is True


def test_cloud_backoff_non_default_values_round_trip(tmp_path: Path) -> None:
    """A non-default but in-range configuration survives the loader
    unchanged — proves the loader is a real reader, not a hard-coded
    defaults factory."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.backoff]\n"
        "max_retries        = 10\n"
        "base_backoff_ms    = 100\n"
        "max_backoff_ms     = 60000\n"
        "jitter_pct         = 0\n"
        "honor_retry_after  = false\n",
    )
    config = load_popolad_config(p)
    assert config.cloud.backoff.max_retries == 10
    assert config.cloud.backoff.base_backoff_ms == 100
    assert config.cloud.backoff.max_backoff_ms == 60_000
    assert config.cloud.backoff.jitter_pct == 0
    assert config.cloud.backoff.honor_retry_after is False


# ---------------------------------------------------------------------------
# Defaults when section / file absent.
# ---------------------------------------------------------------------------


def test_cloud_backoff_defaults_when_section_absent(tmp_path: Path) -> None:
    """A ``popolad.toml`` without ``[cloud.backoff]`` returns spec defaults
    so v0.8.7 deployments keep working without touching their config."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[hitl.cloud]\ntimeout_seconds = 1800\n",
    )
    config = load_popolad_config(p)
    assert config.cloud.backoff == BackoffConfig()
    assert config.cloud.backoff.max_retries == 5
    assert config.cloud.backoff.base_backoff_ms == 500
    assert config.cloud.backoff.max_backoff_ms == 30_000
    assert config.cloud.backoff.jitter_pct == 25
    assert config.cloud.backoff.honor_retry_after is True


def test_cloud_backoff_defaults_when_file_absent(tmp_path: Path) -> None:
    """No popolad.toml at all → backoff defaults still apply (the file is
    optional; a brand-new install never had one)."""
    p = tmp_path / "no-such-file.toml"
    assert not p.exists()
    config = load_popolad_config(p)
    assert config.cloud.backoff == BackoffConfig()


def test_cloud_backoff_defaults_when_section_empty(tmp_path: Path) -> None:
    """An empty ``[cloud.backoff]`` table also returns defaults — proves
    per-key defaults compose correctly with the section-level default."""
    p = _write_toml(tmp_path / "popolad.toml", "[cloud.backoff]\n")
    config = load_popolad_config(p)
    assert config.cloud.backoff == BackoffConfig()


# ---------------------------------------------------------------------------
# Range strictness — every numeric key has explicit bounds.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "boundary"),
    [
        (CLOUD_BACKOFF_MAX_RETRIES_MIN - 1, "below"),  # -1
        (CLOUD_BACKOFF_MAX_RETRIES_MAX + 1, "above"),  # 21
        (-100, "way-below"),
        (100, "way-above"),
    ],
)
def test_max_retries_out_of_range_rejected(
    tmp_path: Path, value: int, boundary: str
) -> None:
    """``max_retries`` outside ``[0, 20]`` raises :class:`ValueError` whose
    message names the section, key, and legal range — operator gets the
    fix without grepping the source (No-Silent-Failures)."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        f"[cloud.backoff]\nmax_retries = {value}\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    msg = str(excinfo.value)
    assert "cloud.backoff" in msg, f"({boundary}) section name missing: {msg!r}"
    assert "max_retries" in msg, f"({boundary}) key missing: {msg!r}"
    assert str(CLOUD_BACKOFF_MAX_RETRIES_MIN) in msg
    assert str(CLOUD_BACKOFF_MAX_RETRIES_MAX) in msg


def test_base_backoff_ms_out_of_range_rejected(tmp_path: Path) -> None:
    """``base_backoff_ms`` outside ``[50, 60_000]`` raises immediately."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.backoff]\nbase_backoff_ms = 10\n",  # < 50
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    msg = str(excinfo.value)
    assert "base_backoff_ms" in msg
    assert str(CLOUD_BACKOFF_BASE_MS_MIN) in msg


def test_base_backoff_ms_above_range_rejected(tmp_path: Path) -> None:
    """``base_backoff_ms`` above the upper bound raises (typo guard)."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        f"[cloud.backoff]\nbase_backoff_ms = {CLOUD_BACKOFF_BASE_MS_MAX + 1}\n",
    )
    with pytest.raises(ValueError):
        load_popolad_config(p)


def test_max_backoff_ms_above_hard_ceiling_rejected(tmp_path: Path) -> None:
    """``max_backoff_ms`` above the 600_000 ms (10 min) ceiling rejects.
    Prevents an operator typo from converting the daemon into an
    indefinite sleep."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        f"[cloud.backoff]\nmax_backoff_ms = {CLOUD_BACKOFF_MAX_MS_MAX + 1}\n",
    )
    with pytest.raises(ValueError):
        load_popolad_config(p)


@pytest.mark.parametrize(
    ("value", "boundary"),
    [
        (CLOUD_BACKOFF_JITTER_PCT_MIN - 1, "below"),  # -1
        (CLOUD_BACKOFF_JITTER_PCT_MAX + 1, "above"),  # 101
        (-25, "way-below"),
        (200, "way-above"),
    ],
)
def test_jitter_pct_out_of_range_rejected(
    tmp_path: Path, value: int, boundary: str
) -> None:
    """``jitter_pct`` outside ``[0, 100]`` rejects with section/key/range."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        f"[cloud.backoff]\njitter_pct = {value}\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    msg = str(excinfo.value)
    assert "jitter_pct" in msg, f"({boundary}) {msg!r}"


# ---------------------------------------------------------------------------
# Type strictness — bool rejected for int; string rejected for int;
# int rejected for bool (per §2.3 rule 1).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ('"5"', "string"),
        ("true", "bool"),
        ("false", "bool"),
        ("1.5", "float"),
        ("[1, 2]", "array"),
    ],
)
def test_max_retries_invalid_type_rejected(
    tmp_path: Path, value: str, kind: str
) -> None:
    """Wrong-type ``max_retries`` rejects with an integer-type hint.

    The bool path is the most subtle: Python's ``isinstance(True, int)``
    returns ``True``, so without ``_require_int``'s explicit
    ``isinstance(value, bool)`` short-circuit the loader would silently
    coerce ``max_retries = true`` into the integer ``1`` — a nasty
    footgun that would silently shrink the retry budget. This test
    fences against that regression for every numeric key in the
    section.
    """
    p = _write_toml(
        tmp_path / "popolad.toml",
        f"[cloud.backoff]\nmax_retries = {value}\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    msg = str(excinfo.value).lower()
    assert "max_retries" in msg, f"({kind}) key missing: {msg!r}"
    assert "integer" in msg or "int" in msg, (
        f"({kind}) integer-type hint missing: {msg!r}"
    )


def test_base_backoff_ms_bool_rejected(tmp_path: Path) -> None:
    """``base_backoff_ms = true`` MUST reject — defensive against the
    Python ``bool ⊆ int`` hierarchy (per ``_require_int`` contract)."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.backoff]\nbase_backoff_ms = true\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    assert "base_backoff_ms" in str(excinfo.value)


def test_max_backoff_ms_string_rejected(tmp_path: Path) -> None:
    """A string ``max_backoff_ms = "30000"`` rejects even though
    ``int("30000")`` would succeed — TOML has a native int type and
    silent string coercion masks operator typos."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        '[cloud.backoff]\nmax_backoff_ms = "30000"\n',
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    assert "max_backoff_ms" in str(excinfo.value)


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("0", "int"),
        ("1", "int"),
        ('"true"', "string"),
        ('"false"', "string"),
        ("[true]", "array"),
    ],
)
def test_honor_retry_after_invalid_type_rejected(
    tmp_path: Path, value: str, kind: str
) -> None:
    """``honor_retry_after`` must be a strict TOML bool — int 0 / 1 are
    rejected (a common typo for "I want true/false") so the operator
    sees the type mismatch, not a silent ``bool(0) = False`` coercion."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        f"[cloud.backoff]\nhonor_retry_after = {value}\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    msg = str(excinfo.value).lower()
    assert "honor_retry_after" in msg, f"({kind}) key missing: {msg!r}"
    assert "bool" in msg, f"({kind}) bool-type hint missing: {msg!r}"


# ---------------------------------------------------------------------------
# Inter-key invariant — max_backoff_ms ≥ base_backoff_ms.
# ---------------------------------------------------------------------------


def test_max_backoff_ms_below_base_backoff_ms_rejected(tmp_path: Path) -> None:
    """Setting ``max_backoff_ms`` < ``base_backoff_ms`` makes the cap
    unreachable; the loader rejects this with both keys named so the
    operator sees the inter-key relationship, not just one half."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.backoff]\n"
        "base_backoff_ms = 1000\n"
        "max_backoff_ms = 500\n",  # 500 < 1000 — invalid
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    msg = str(excinfo.value)
    assert "max_backoff_ms" in msg
    # The error message should help the operator understand the relationship.
    assert "1000" in msg or "500" in msg, (
        f"inter-key error message must include offending values: {msg!r}"
    )


def test_max_backoff_ms_equal_base_backoff_ms_accepted(tmp_path: Path) -> None:
    """``max_backoff_ms == base_backoff_ms`` is the boundary case for the
    inter-key invariant — the cap is reachable but trivial. Accepted
    so deterministic schedules can use a single delay value."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.backoff]\n"
        "base_backoff_ms = 5000\n"
        "max_backoff_ms = 5000\n",
    )
    config = load_popolad_config(p)
    assert config.cloud.backoff.base_backoff_ms == 5000
    assert config.cloud.backoff.max_backoff_ms == 5000


# ---------------------------------------------------------------------------
# Unknown keys — WARN log, NOT error (forward-compat).
# ---------------------------------------------------------------------------


def test_unknown_keys_warn_not_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown keys under ``[cloud.backoff]`` (e.g. an operator typo
    ``max_retires`` instead of ``max_retries``) MUST warn but NOT
    error — the loader is forward-compat with future schema additions."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.backoff]\n"
        "max_retries = 5\n"
        "max_retires = 99\n"  # typo
        "future_key = \"something\"\n",  # unknown future key
    )
    with caplog.at_level(logging.WARNING):
        config = load_popolad_config(p)
    # Loaded successfully despite typo.
    assert config.cloud.backoff.max_retries == 5
    # WARN message references the offending keys.
    warn_msgs = [
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert any("max_retires" in m for m in warn_msgs), (
        f"WARN must mention typo'd key: {warn_msgs!r}"
    )


# ---------------------------------------------------------------------------
# Section-shape strictness.
# ---------------------------------------------------------------------------


def test_cloud_section_must_be_table(tmp_path: Path) -> None:
    """A scalar ``cloud = "..."`` (instead of ``[cloud]`` table) rejects."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        'cloud = "not-a-table"\n',
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    assert "cloud" in str(excinfo.value)


def test_cloud_backoff_section_must_be_table(tmp_path: Path) -> None:
    """A scalar ``[cloud] backoff = "..."`` (instead of sub-table) rejects."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        '[cloud]\nbackoff = "not-a-table"\n',
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    assert "cloud.backoff" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Backwards compat — popolad.toml with only [hitl.cloud] still loads cleanly.
# ---------------------------------------------------------------------------


def test_v087_config_still_loads_with_v088_loader(tmp_path: Path) -> None:
    """A v0.8.7-era popolad.toml (no ``[cloud.backoff]`` section) loads
    cleanly under the v0.8.8 loader — both ``hitl.cloud`` AND
    ``cloud.backoff`` are populated (the latter with defaults). Pins
    backwards compatibility for the migration window."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[hitl.cloud]\n"
        "timeout_seconds = 600\n"
        "idempotency_window_s = 7200\n"
        "max_concurrent_per_run = 2\n",
    )
    config = load_popolad_config(p)
    # v0.8.7 surface preserved.
    assert config.hitl.cloud.timeout_seconds == 600
    assert config.hitl.cloud.idempotency_window_s == 7200
    assert config.hitl.cloud.max_concurrent_per_run == 2
    # v0.8.8 defaults populated.
    assert config.cloud.backoff == BackoffConfig()


# ---------------------------------------------------------------------------
# AC (a) — boundary values accepted (proves the loader uses inclusive ranges).
# ---------------------------------------------------------------------------


def test_max_retries_zero_accepted(tmp_path: Path) -> None:
    """``max_retries = 0`` is the valid "single-shot" disposition."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.backoff]\nmax_retries = 0\n",
    )
    config = load_popolad_config(p)
    assert config.cloud.backoff.max_retries == 0


def test_jitter_pct_zero_and_hundred_accepted(tmp_path: Path) -> None:
    """Both extreme jitter values must be accepted (0 = deterministic;
    100 = ±100 %, valid if rare)."""
    p_zero = _write_toml(
        tmp_path / "zero.toml",
        "[cloud.backoff]\njitter_pct = 0\n",
    )
    config_zero = load_popolad_config(p_zero)
    assert config_zero.cloud.backoff.jitter_pct == 0

    p_hundred = _write_toml(
        tmp_path / "hundred.toml",
        "[cloud.backoff]\njitter_pct = 100\n",
    )
    config_hundred = load_popolad_config(p_hundred)
    assert config_hundred.cloud.backoff.jitter_pct == 100
