"""popolad daemon entry — ``python -m popolaloom.daemon`` (v0.2.0 Stage A A1).

Boots an asyncio + uvicorn server bound to a Unix Domain Socket so the
``popola`` CLI can talk to it via ``httpx.AsyncHTTPTransport(uds=...)``.

Spec / plan references:

- ``v0.2.0-plan.md`` §4 Stage A A1 (this file).
- ``spec.md`` §10 canonical paths (UDS + PID + log + events_dir layout).

Path layout (controlled by ``$POPOLA_HOME`` env var, default ``~/.popola``):

- ``$POPOLA_HOME/popolad.sock`` — Unix Domain Socket (server bind point).
- ``$POPOLA_HOME/popolad.pid`` — PID file (written at startup; removed on
  graceful shutdown).
- ``$POPOLA_HOME/events/`` — NDJSON event log directory (one file per task).
- ``$POPOLA_HOME/log/popolad.log`` — daemon stderr log (only when started
  via ``popolad start`` subcommand; direct ``python -m`` invocations log
  to inherited stderr).

Signal handling:

- ``SIGTERM`` / ``SIGINT`` → graceful shutdown (uvicorn ``server.should_exit
  = True`` + lifespan tear-down cancels in-flight tasks via SIGTERM grace).

# TODO(v0.3.0): integrate ``systemd-run --user --scope`` for cgroup limits;
# add log rotation (NFR-12) + Prometheus /metrics (NFR-3 baseline).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import uvicorn

from popolaloom.adapters.cursor_cloud import BackoffConfig
from popolaloom.daemon.rpc import create_app

logger = logging.getLogger("popolaloom.daemon")


# ── popolad.toml config loader (v0.8.7 T2.2.1) ───────────────────────────


CLOUD_HITL_TIMEOUT_MIN_S: int = 60
"""Lower bound for ``[hitl.cloud].timeout_seconds`` (mcp-tool-contract §3.1)."""

CLOUD_HITL_TIMEOUT_MAX_S: int = 86400
"""Upper bound for ``[hitl.cloud].timeout_seconds`` (24 h ceiling)."""

CLOUD_HITL_IDEMPOTENCY_WINDOW_MIN_S: int = 60
"""Lower bound for ``[hitl.cloud].idempotency_window_s``."""

CLOUD_HITL_IDEMPOTENCY_WINDOW_MAX_S: int = 86400
"""Upper bound for ``[hitl.cloud].idempotency_window_s``."""

CLOUD_HITL_MAX_CONCURRENT_MIN: int = 1
"""Lower bound for ``[hitl.cloud].max_concurrent_per_run`` (≥ 1)."""

CLOUD_HITL_MAX_CONCURRENT_MAX: int = 4
"""Upper bound for ``[hitl.cloud].max_concurrent_per_run`` (≤ 4 per contract §9)."""


# ── popolad.toml [cloud.backoff] loader (v0.8.8 T2.1.3) ──────────────────
#
# Schema + ranges sourced verbatim from
# ``.local/research/v0.8.8_multi_run/quota-config.md`` §2.1 +§2.3.
# The loader extends the v0.8.7 `[hitl.cloud]` validation pattern
# (``_require_int`` / ``_require_range``) to cover one bool field
# (``honor_retry_after``) and the inter-key invariant
# ``max_backoff_ms >= base_backoff_ms``. Unknown keys WARN (not error)
# so a forward-compat key like ``[cloud.backoff].route_overrides`` does
# not break a v0.8.8 deployment that has yet to upgrade.


CLOUD_BACKOFF_MAX_RETRIES_MIN: int = 0
"""Lower bound for ``[cloud.backoff].max_retries`` (0 = single-shot)."""

CLOUD_BACKOFF_MAX_RETRIES_MAX: int = 20
"""Upper bound for ``[cloud.backoff].max_retries`` (caps daemon hang at ~10 min)."""

CLOUD_BACKOFF_BASE_MS_MIN: int = 50
"""Lower bound for ``[cloud.backoff].base_backoff_ms`` (50 ms = ~1 frame)."""

CLOUD_BACKOFF_BASE_MS_MAX: int = 60_000
"""Upper bound for ``[cloud.backoff].base_backoff_ms`` (60 s base = misuse)."""

CLOUD_BACKOFF_MAX_MS_MAX: int = 600_000
"""Hard ceiling for ``[cloud.backoff].max_backoff_ms`` (10 min cap)."""

CLOUD_BACKOFF_JITTER_PCT_MIN: int = 0
"""Lower bound for ``[cloud.backoff].jitter_pct`` (0 = deterministic)."""

CLOUD_BACKOFF_JITTER_PCT_MAX: int = 100
"""Upper bound for ``[cloud.backoff].jitter_pct`` (100 = ±100 %)."""


_CLOUD_BACKOFF_KNOWN_KEYS: frozenset[str] = frozenset(
    {
        "max_retries",
        "base_backoff_ms",
        "max_backoff_ms",
        "jitter_pct",
        "honor_retry_after",
    }
)
"""All keys recognised by the v0.8.8 ``[cloud.backoff]`` loader.

Extra keys are tolerated but logged at WARNING (per ``quota-config.md``
§2.3 rule 4) so a typo like ``max_retires`` is caught at boot, not at
the next 429 storm.
"""


# ── popolad.toml [cloud.busy_strategy] loader (v0.8.8 T2.2.2) ─────────────
#
# Schema + ranges sourced verbatim from
# ``.local/research/v0.8.8_multi_run/quota-config.md`` §2.2 + §2.3.
# Disjoint from T2.1.3's ``[cloud.backoff]`` and T2.2.1's ``[cloud.relay]``
# blocks; lives under the same ``CloudConfig`` parent dataclass.

CLOUD_BUSY_STRATEGY_MODES: frozenset[str] = frozenset({"queue", "fail_fast"})
"""Allowed values for ``[cloud.busy_strategy].mode`` (per spec §2.2)."""

CLOUD_BUSY_QUEUE_POLL_MIN_S: int = 1
"""Lower bound for ``[cloud.busy_strategy].queue_poll_interval_s``."""

CLOUD_BUSY_QUEUE_POLL_MAX_S: int = 60
"""Upper bound for ``[cloud.busy_strategy].queue_poll_interval_s``."""

CLOUD_BUSY_QUEUE_MAX_WAIT_MIN_S: int = 60
"""Lower bound for ``[cloud.busy_strategy].queue_max_wait_s`` (when > 0).

``0`` is also accepted as a special "wait forever" sentinel per spec §2.2.
Operators must explicitly opt in to that — it bypasses the [60, 86_400]
range check.
"""

CLOUD_BUSY_QUEUE_MAX_WAIT_MAX_S: int = 86_400
"""Upper bound for ``[cloud.busy_strategy].queue_max_wait_s`` (24 h)."""


_CLOUD_BUSY_STRATEGY_KNOWN_KEYS: frozenset[str] = frozenset(
    {
        "mode",
        "queue_poll_interval_s",
        "queue_max_wait_s",
        "notify_on_dispatch",
    }
)
"""All keys recognised by the v0.8.8 ``[cloud.busy_strategy]`` loader.

Extra keys WARN (per ``quota-config.md`` §2.3 rule 4) so a typo like
``queue_poll_interal_s`` is caught at boot, not at the next 409.
"""


# ── popolad.toml [cloud.relay] loader (v0.8.8 T2.2.1) ────────────────────
#
# Schema sourced verbatim from
# ``.local/research/v0.8.8_multi_run/relay-primitive.md`` §6.1 and
# ``relay-auto-safety.md`` §3.1. Three of the seven keys
# (``require_confirm_allowlist_flag`` / ``secret_scan_enabled`` /
# ``dry_run_emits_audit``) are **locked-on** for v0.8.8; the loader
# rejects any value other than ``true`` with the spec-locked error
# messages enumerated below. Pass criterion C1 of PLAN.md §9.

CLOUD_RELAY_PROMPT_SIZE_CAP_MIN: int = 1024
"""Lower bound for ``[cloud.relay].prompt_size_cap_bytes`` (1 KiB floor)."""

CLOUD_RELAY_PROMPT_SIZE_CAP_MAX: int = 1_048_576
"""Upper bound for ``[cloud.relay].prompt_size_cap_bytes`` (1 MiB ceiling)."""

CLOUD_RELAY_IDEMPOTENCY_WINDOW_MIN_S: int = 60
"""Lower bound for ``[cloud.relay].idempotency_window_s`` (60 s floor)."""

CLOUD_RELAY_IDEMPOTENCY_WINDOW_MAX_S: int = 86_400
"""Upper bound for ``[cloud.relay].idempotency_window_s`` (24 h ceiling)."""


_CLOUD_RELAY_VALID_MODES: frozenset[str] = frozenset({"auto", "confirm"})
"""Accepted values for ``[cloud.relay].mode`` (Q-C-4 deviation knob)."""


_CLOUD_RELAY_KNOWN_KEYS: frozenset[str] = frozenset(
    {
        "mode",
        "repo_allowlist",
        "prompt_size_cap_bytes",
        "idempotency_window_s",
        "audit_root",
        "require_confirm_allowlist_flag",
        "secret_scan_enabled",
        "dry_run_emits_audit",
    }
)
"""All keys recognised by the v0.8.8 ``[cloud.relay]`` loader.

Extra keys WARN (forward-compat with v0.8.9 additions like
``allow_secret_shapes_default``) per ``relay-auto-safety.md`` §3.1 rule 5.
"""


_CLOUD_RELAY_REPO_ENTRY_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$"
)
"""Pattern for valid ``repo_allowlist`` entries (``<org>/<repo>`` short form).

URL forms (``https://github.com/foo/bar``) and globs (``foo/*``) are
rejected so the runtime allowlist comparison is O(1) string equality
against the canonicalised target — see ``relay-auto-safety.md`` §3.2
rationale ("regex matches accidentally too much").
"""


_CLOUD_RELAY_LOCK_ERROR_MESSAGES: Final[dict[str, str]] = {
    "require_confirm_allowlist_flag": (
        "v0.8.8 release lock: require_confirm_allowlist_flag must stay true "
        "(Q-C-4 mitigation M1)"
    ),
    "secret_scan_enabled": (
        "v0.8.8 release lock: secret_scan_enabled must stay true "
        "(Q-C-4 mitigation M3)"
    ),
    "dry_run_emits_audit": (
        "v0.8.8 release lock: dry_run_emits_audit must stay true "
        "(Q-C-4 mitigation M2)"
    ),
}
"""Spec-locked error messages for the three v0.8.8-locked bool keys.

Per ``relay-auto-safety.md`` §3.1 the loader MUST raise ``ValueError``
with these exact strings when an operator attempts to disable any of
the three release-gate mitigations. PLAN.md §9 box C1 evidence requires
the messages match verbatim.
"""


@dataclass(frozen=True)
class CloudHITLConfig:
    """Validated ``[hitl.cloud]`` section of ``popolad.toml``.

    Defaults match :doc:`mcp-tool-contract` §9 (default timeout 1800,
    idempotency window 3600, max concurrent per run = 1). The loader
    rejects out-of-range values per workspace rule "No Silent Failures"
    (see :func:`load_popolad_config`).
    """

    timeout_seconds: int = 1800
    idempotency_window_s: int = 3600
    max_concurrent_per_run: int = 1


@dataclass(frozen=True)
class HITLConfig:
    """Validated ``[hitl]`` section (v0.8.7+ extends with ``[hitl.cloud]``)."""

    cloud: CloudHITLConfig = field(default_factory=CloudHITLConfig)


@dataclass(frozen=True)
class CloudRelayConfig:
    """Validated ``[cloud.relay]`` section of ``popolad.toml`` (v0.8.8 T2.2.1).

    Defaults track ``relay-primitive.md`` §6.1 and ``relay-auto-safety.md``
    §3.1 verbatim. Three of the eight keys are **locked-on** for v0.8.8
    per Q-C-4 mitigation set; the loader in :func:`_load_cloud_relay`
    rejects ``False`` for any of them with the spec-locked error message
    (PLAN.md §9 release-gate criterion C1).

    Attributes:
        mode: Either ``"auto"`` (Q-C-4 deviated default) or
            ``"confirm"`` (operator-flip back to v0.8.5 human-gate
            behavior). Per-invocation override via ``--no-confirm``.
        repo_allowlist: List of ``"org/repo"`` strings. **Empty list is
            the default** and BLOCKS all relays (M1 default-deny).
        prompt_size_cap_bytes: Max final prompt body size after summary
            truncation; above this exit ``1`` (``payload_too_large``).
        idempotency_window_s: Window during which
            ``(source_task, target_repo, idempotency_key)`` is treated
            as a duplicate.
        audit_root: Root dir for per-task audit NDJSON files; empty
            string ⇒ ``.local/.agent/archive/relay/`` (the
            :class:`popolaloom.relay.audit.RelayAuditWriter` default).
        require_confirm_allowlist_flag: **Locked ``True`` for v0.8.8**.
            When ``True``, an out-of-allowlist target requires
            ``--confirm-allowlist`` on the CLI.
        secret_scan_enabled: **Locked ``True`` for v0.8.8**. Disabling
            would defeat M3 secret-redaction pre-flight.
        dry_run_emits_audit: **Locked ``True`` for v0.8.8**. Ensures
            ``--dry-run`` invocations still produce an audit row so the
            "every relay attempt has a row" invariant (M2) holds.
    """

    mode: str = "auto"
    repo_allowlist: tuple[str, ...] = ()
    prompt_size_cap_bytes: int = 16_384
    idempotency_window_s: int = 3_600
    audit_root: str = ""
    require_confirm_allowlist_flag: bool = True
    secret_scan_enabled: bool = True
    dry_run_emits_audit: bool = True


@dataclass(frozen=True)
class BusyStrategyConfig:
    """Validated ``[cloud.busy_strategy]`` section of ``popolad.toml``.

    v0.8.8 T2.2.2 — defaults match ``quota-config.md`` §2.2:

    - ``mode = "queue"`` (Q-C-5 binding — daemon registers the dispatch
      into a pending queue keyed by ``agent_id``; ``"fail_fast"`` preserves
      v0.8.7 behavior with immediate ``cli_exit=102``).
    - ``queue_poll_interval_s = 5`` (matches existing :class:`CloudPollLoop`
      cadence so the daemon does not pay for two concurrent polls).
    - ``queue_max_wait_s = 1800`` (30 min ceiling per queued task; on
      expiry the queued task is converted to ``cli_exit=75`` per spec §6
      — ``75`` because the wait expired, not the agent).
    - ``notify_on_dispatch = True`` (emit ``cloud.busy_dispatched`` so
      attach UIs can dismiss "queued" badges).

    The loader (:func:`_load_cloud_busy_strategy`) rejects type / range /
    mode mismatches per workspace rule "No Silent Failures".
    """

    mode: str = "queue"
    queue_poll_interval_s: int = 5
    queue_max_wait_s: int = 1800
    notify_on_dispatch: bool = True


@dataclass(frozen=True)
class CloudConfig:
    """Validated ``[cloud]`` section (v0.8.8+ — `[cloud.backoff]` etc.).

    v0.8.8 introduces three sibling sub-tables:

    - ``[cloud.backoff]`` — quota-aware retry schedule (T2.1.3).
    - ``[cloud.busy_strategy]`` — async-queue handling for ``409 agent_busy``
      (T2.2.2).
    - ``[cloud.relay]`` — cross-PR relay settings (T2.2.1).

    Each sub-table is added in its own Stage 2 task with a disjoint code
    block in :func:`load_popolad_config`; all three live under the same
    parent ``CloudConfig`` so the dataclass shape stays stable across the
    Stage 2 wave.
    """

    backoff: BackoffConfig = field(default_factory=BackoffConfig)
    relay: CloudRelayConfig = field(default_factory=CloudRelayConfig)
    busy_strategy: BusyStrategyConfig = field(default_factory=BusyStrategyConfig)


@dataclass(frozen=True)
class PopoladConfig:
    """Top-level ``popolad.toml`` schema as consumed by the daemon."""

    hitl: HITLConfig = field(default_factory=HITLConfig)
    cloud: CloudConfig = field(default_factory=CloudConfig)


def get_popolad_config_path() -> Path:
    """Return ``$POPOLA_HOME/popolad.toml`` (regardless of file existence)."""
    return get_popola_home() / "popolad.toml"


def _require_int(
    value: Any,
    *,
    section: str,
    key: str,
    source: Path,
) -> int:
    """Coerce + validate that ``value`` is a strict ``int`` (rejects bool).

    Per workspace rule "No Silent Failures": booleans (which Python coerces
    to int silently) are rejected so an operator who typoed
    ``timeout_seconds = true`` sees an explicit error instead of a clamped
    integer 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"[{section}].{key} in {source} must be an integer; got {value!r} "
            f"(type {type(value).__name__})"
        )
    return int(value)


def _require_range(
    value: int,
    *,
    section: str,
    key: str,
    source: Path,
    lo: int,
    hi: int,
) -> int:
    """Reject ``value`` when outside ``[lo, hi]`` (No Silent Failures)."""
    if value < lo or value > hi:
        raise ValueError(
            f"[{section}].{key} in {source} must be in [{lo}, {hi}]; got {value}"
        )
    return value


def _require_bool(
    value: Any,
    *,
    section: str,
    key: str,
    source: Path,
) -> bool:
    """Coerce + validate that ``value`` is a strict ``bool`` (No Silent Failures).

    v0.8.8 T2.1.3 — sister helper to :func:`_require_int`; rejects ints
    (including 0/1 which ``bool(int)`` would coerce silently) and strings
    so an operator who typed ``honor_retry_after = 1`` sees an explicit
    error instead of "fortunate it parsed". Mirrors the ``[hitl.cloud]``
    loader's rejection style.
    """
    if not isinstance(value, bool):
        raise ValueError(
            f"[{section}].{key} in {source} must be a bool; got {value!r} "
            f"(type {type(value).__name__})"
        )
    return value


def _warn_unknown_keys(
    section_name: str,
    section: dict[str, Any],
    known: frozenset[str],
    source: Path,
) -> None:
    """Log a WARNING for any key not in ``known`` (No Silent Failures).

    v0.8.8 T2.1.3 — per ``quota-config.md`` §2.3 rule 4: unknown keys do
    NOT raise (the schema is forward-compatible — a future
    ``[cloud.backoff].route_overrides`` shouldn't break a v0.8.8 daemon),
    but they MUST surface in the log so a typo like ``max_retires`` is
    caught before it bites.
    """
    extras = sorted(set(section) - known)
    if extras:
        logger.warning(
            "[%s] in %s: unknown key(s) %s; ignored — typo? known keys: %s",
            section_name,
            source,
            ", ".join(extras),
            ", ".join(sorted(known)),
        )


def load_popolad_config(path: Path | None = None) -> PopoladConfig:
    """Load + validate ``popolad.toml`` (or return defaults when absent).

    Schema (v0.8.7 T2.2.1 + v0.8.8 T2.1.3, strict superset of v0.8.5's
    empty schema):

    .. code-block:: toml

        [hitl.cloud]
        timeout_seconds = 1800            # default; range [60, 86400]
        idempotency_window_s = 3600       # default; range [60, 86400]
        max_concurrent_per_run = 1        # default; range [1, 4]

        [cloud.backoff]
        max_retries        = 5            # default; range [0, 20]
        base_backoff_ms    = 500          # default; range [50, 60_000]
        max_backoff_ms     = 30000        # default; range
                                          #   [base_backoff_ms, 600_000]
        jitter_pct         = 25           # default; range [0, 100]
        honor_retry_after  = true         # default

    Returns:
        PopoladConfig: fully populated dataclass with validated ints/bools.

    Raises:
        ValueError: on out-of-range or non-int values per workspace rule
            "No Silent Failures" — operators must see config typos
            explicitly, not via silent clamping.
        OSError: when ``path`` exists but is unreadable.
        tomllib.TOMLDecodeError: when ``path`` is not valid TOML.

    The config file is optional: when ``path`` (default
    ``$POPOLA_HOME/popolad.toml``) does not exist, the function returns the
    documented defaults so existing v0.8.5 deployments keep working.
    """
    p = path if path is not None else get_popolad_config_path()
    if not p.is_file():
        logger.debug("popolad.toml not found at %s; using defaults", p)
        return PopoladConfig()
    with p.open("rb") as fh:
        raw = tomllib.load(fh)

    hitl_section = raw.get("hitl", {})
    if not isinstance(hitl_section, dict):
        raise ValueError(
            f"[hitl] in {p} must be a table; got {type(hitl_section).__name__}"
        )
    cloud_section = hitl_section.get("cloud", {})
    if not isinstance(cloud_section, dict):
        raise ValueError(
            f"[hitl.cloud] in {p} must be a table; "
            f"got {type(cloud_section).__name__}"
        )

    timeout_raw = cloud_section.get("timeout_seconds", 1800)
    timeout_int = _require_int(
        timeout_raw, section="hitl.cloud", key="timeout_seconds", source=p
    )
    timeout_int = _require_range(
        timeout_int,
        section="hitl.cloud",
        key="timeout_seconds",
        source=p,
        lo=CLOUD_HITL_TIMEOUT_MIN_S,
        hi=CLOUD_HITL_TIMEOUT_MAX_S,
    )

    window_raw = cloud_section.get("idempotency_window_s", 3600)
    window_int = _require_int(
        window_raw, section="hitl.cloud", key="idempotency_window_s", source=p
    )
    window_int = _require_range(
        window_int,
        section="hitl.cloud",
        key="idempotency_window_s",
        source=p,
        lo=CLOUD_HITL_IDEMPOTENCY_WINDOW_MIN_S,
        hi=CLOUD_HITL_IDEMPOTENCY_WINDOW_MAX_S,
    )

    max_concurrent_raw = cloud_section.get("max_concurrent_per_run", 1)
    max_concurrent_int = _require_int(
        max_concurrent_raw,
        section="hitl.cloud",
        key="max_concurrent_per_run",
        source=p,
    )
    max_concurrent_int = _require_range(
        max_concurrent_int,
        section="hitl.cloud",
        key="max_concurrent_per_run",
        source=p,
        lo=CLOUD_HITL_MAX_CONCURRENT_MIN,
        hi=CLOUD_HITL_MAX_CONCURRENT_MAX,
    )

    backoff_cfg = _load_cloud_backoff(raw, source=p)
    relay_cfg = _load_cloud_relay(raw, source=p)
    busy_cfg = _load_cloud_busy_strategy(raw, source=p)

    return PopoladConfig(
        hitl=HITLConfig(
            cloud=CloudHITLConfig(
                timeout_seconds=timeout_int,
                idempotency_window_s=window_int,
                max_concurrent_per_run=max_concurrent_int,
            )
        ),
        cloud=CloudConfig(
            backoff=backoff_cfg,
            relay=relay_cfg,
            busy_strategy=busy_cfg,
        ),
    )


def _load_cloud_backoff(raw: dict[str, Any], *, source: Path) -> BackoffConfig:
    """Parse + validate ``[cloud.backoff]`` per ``quota-config.md`` §2.1.

    v0.8.8 T2.1.3. The TOML loader returns dicts for tables; an absent
    ``[cloud]`` or ``[cloud.backoff]`` produces the documented defaults
    (a missing TOML file already short-circuits in the parent loader, but
    a partial TOML file with only ``[hitl.cloud]`` must still yield
    backoff defaults). All four numeric fields go through
    :func:`_require_int` + :func:`_require_range`; ``honor_retry_after``
    goes through :func:`_require_bool`. The inter-key invariant
    ``max_backoff_ms >= base_backoff_ms`` is enforced after both have
    parsed (per spec §2.3 rule 3) — operators get one error pointing at
    both keys, not two cascading errors.
    """
    cloud_top = raw.get("cloud", {})
    if not isinstance(cloud_top, dict):
        raise ValueError(
            f"[cloud] in {source} must be a table; got {type(cloud_top).__name__}"
        )
    backoff_section = cloud_top.get("backoff", {})
    if not isinstance(backoff_section, dict):
        raise ValueError(
            f"[cloud.backoff] in {source} must be a table; "
            f"got {type(backoff_section).__name__}"
        )

    _warn_unknown_keys(
        "cloud.backoff", backoff_section, _CLOUD_BACKOFF_KNOWN_KEYS, source
    )

    max_retries_raw = backoff_section.get("max_retries", 5)
    max_retries_int = _require_int(
        max_retries_raw, section="cloud.backoff", key="max_retries", source=source
    )
    max_retries_int = _require_range(
        max_retries_int,
        section="cloud.backoff",
        key="max_retries",
        source=source,
        lo=CLOUD_BACKOFF_MAX_RETRIES_MIN,
        hi=CLOUD_BACKOFF_MAX_RETRIES_MAX,
    )

    base_ms_raw = backoff_section.get("base_backoff_ms", 500)
    base_ms_int = _require_int(
        base_ms_raw,
        section="cloud.backoff",
        key="base_backoff_ms",
        source=source,
    )
    base_ms_int = _require_range(
        base_ms_int,
        section="cloud.backoff",
        key="base_backoff_ms",
        source=source,
        lo=CLOUD_BACKOFF_BASE_MS_MIN,
        hi=CLOUD_BACKOFF_BASE_MS_MAX,
    )

    max_ms_raw = backoff_section.get("max_backoff_ms", 30_000)
    max_ms_int = _require_int(
        max_ms_raw,
        section="cloud.backoff",
        key="max_backoff_ms",
        source=source,
    )
    # Inter-key invariant: max_backoff_ms must be >= base_backoff_ms AND
    # <= the hard ceiling. We check both in one ``_require_range`` call
    # using ``base_ms_int`` as the lower bound — the error message names
    # both keys so the operator sees the cause, not the symptom.
    max_ms_int = _require_range(
        max_ms_int,
        section="cloud.backoff",
        key="max_backoff_ms",
        source=source,
        lo=base_ms_int,
        hi=CLOUD_BACKOFF_MAX_MS_MAX,
    )

    jitter_raw = backoff_section.get("jitter_pct", 25)
    jitter_int = _require_int(
        jitter_raw,
        section="cloud.backoff",
        key="jitter_pct",
        source=source,
    )
    jitter_int = _require_range(
        jitter_int,
        section="cloud.backoff",
        key="jitter_pct",
        source=source,
        lo=CLOUD_BACKOFF_JITTER_PCT_MIN,
        hi=CLOUD_BACKOFF_JITTER_PCT_MAX,
    )

    honor_raw = backoff_section.get("honor_retry_after", True)
    honor_bool = _require_bool(
        honor_raw,
        section="cloud.backoff",
        key="honor_retry_after",
        source=source,
    )

    return BackoffConfig(
        max_retries=max_retries_int,
        base_backoff_ms=base_ms_int,
        max_backoff_ms=max_ms_int,
        jitter_pct=jitter_int,
        honor_retry_after=honor_bool,
    )


def _load_cloud_relay(raw: dict[str, Any], *, source: Path) -> CloudRelayConfig:
    """Parse + validate ``[cloud.relay]`` per ``relay-primitive.md`` §6.1
    + ``relay-auto-safety.md`` §3.1.

    v0.8.8 T2.2.1. Three of the eight keys
    (``require_confirm_allowlist_flag``, ``secret_scan_enabled``,
    ``dry_run_emits_audit``) are release-gate-locked: setting any of them
    to ``false`` MUST raise :class:`ValueError` with the spec-locked
    error message in :data:`_CLOUD_RELAY_LOCK_ERROR_MESSAGES`. PLAN.md §9
    box C1 directly evidences this against the test surface.

    Validation rules (mirrors :func:`_load_cloud_backoff`):

    1. Section MUST be a TOML table when present.
    2. Each ``repo_allowlist`` entry MUST match
       :data:`_CLOUD_RELAY_REPO_ENTRY_RE` (``<org>/<repo>`` short form);
       URL forms / globs / multi-segment paths rejected.
    3. ``mode`` MUST be one of :data:`_CLOUD_RELAY_VALID_MODES`.
    4. Numeric fields go through :func:`_require_int` +
       :func:`_require_range`.
    5. ``audit_root`` MUST be a string (empty allowed = default).
    6. The three locked bool keys go through :func:`_require_bool`
       AND must be ``True``.
    7. Unknown keys WARN (forward-compat).
    """
    cloud_top = raw.get("cloud", {})
    if not isinstance(cloud_top, dict):
        raise ValueError(
            f"[cloud] in {source} must be a table; got {type(cloud_top).__name__}"
        )
    relay_section = cloud_top.get("relay", {})
    if not isinstance(relay_section, dict):
        raise ValueError(
            f"[cloud.relay] in {source} must be a table; "
            f"got {type(relay_section).__name__}"
        )

    _warn_unknown_keys(
        "cloud.relay", relay_section, _CLOUD_RELAY_KNOWN_KEYS, source
    )

    mode_raw = relay_section.get("mode", "auto")
    if not isinstance(mode_raw, str):
        raise ValueError(
            f"[cloud.relay].mode in {source} must be a string; "
            f"got {mode_raw!r} (type {type(mode_raw).__name__})"
        )
    if mode_raw not in _CLOUD_RELAY_VALID_MODES:
        raise ValueError(
            f"[cloud.relay].mode in {source} must be one of "
            f"{sorted(_CLOUD_RELAY_VALID_MODES)}; got {mode_raw!r}"
        )

    repo_allowlist_raw = relay_section.get("repo_allowlist", [])
    if not isinstance(repo_allowlist_raw, list):
        raise ValueError(
            f"[cloud.relay].repo_allowlist in {source} must be a list; "
            f"got {type(repo_allowlist_raw).__name__}"
        )
    repo_allowlist: list[str] = []
    for idx, entry in enumerate(repo_allowlist_raw):
        if not isinstance(entry, str):
            raise ValueError(
                f"[cloud.relay].repo_allowlist[{idx}] in {source} must be "
                f"a string; got {entry!r} (type {type(entry).__name__})"
            )
        if not _CLOUD_RELAY_REPO_ENTRY_RE.fullmatch(entry):
            raise ValueError(
                f"[cloud.relay].repo_allowlist[{idx}] in {source} must match "
                f"'<org>/<repo>' (regex {_CLOUD_RELAY_REPO_ENTRY_RE.pattern}); "
                f"got {entry!r}"
            )
        repo_allowlist.append(entry)

    cap_raw = relay_section.get("prompt_size_cap_bytes", 16_384)
    cap_int = _require_int(
        cap_raw,
        section="cloud.relay",
        key="prompt_size_cap_bytes",
        source=source,
    )
    cap_int = _require_range(
        cap_int,
        section="cloud.relay",
        key="prompt_size_cap_bytes",
        source=source,
        lo=CLOUD_RELAY_PROMPT_SIZE_CAP_MIN,
        hi=CLOUD_RELAY_PROMPT_SIZE_CAP_MAX,
    )

    window_raw = relay_section.get("idempotency_window_s", 3_600)
    window_int = _require_int(
        window_raw,
        section="cloud.relay",
        key="idempotency_window_s",
        source=source,
    )
    window_int = _require_range(
        window_int,
        section="cloud.relay",
        key="idempotency_window_s",
        source=source,
        lo=CLOUD_RELAY_IDEMPOTENCY_WINDOW_MIN_S,
        hi=CLOUD_RELAY_IDEMPOTENCY_WINDOW_MAX_S,
    )

    audit_root_raw = relay_section.get("audit_root", "")
    if not isinstance(audit_root_raw, str):
        raise ValueError(
            f"[cloud.relay].audit_root in {source} must be a string; "
            f"got {audit_root_raw!r} (type {type(audit_root_raw).__name__})"
        )

    confirm_flag_raw = relay_section.get("require_confirm_allowlist_flag", True)
    confirm_flag_bool = _require_bool(
        confirm_flag_raw,
        section="cloud.relay",
        key="require_confirm_allowlist_flag",
        source=source,
    )
    if not confirm_flag_bool:
        raise ValueError(
            _CLOUD_RELAY_LOCK_ERROR_MESSAGES["require_confirm_allowlist_flag"]
        )

    scan_raw = relay_section.get("secret_scan_enabled", True)
    scan_bool = _require_bool(
        scan_raw,
        section="cloud.relay",
        key="secret_scan_enabled",
        source=source,
    )
    if not scan_bool:
        raise ValueError(_CLOUD_RELAY_LOCK_ERROR_MESSAGES["secret_scan_enabled"])

    dry_audit_raw = relay_section.get("dry_run_emits_audit", True)
    dry_audit_bool = _require_bool(
        dry_audit_raw,
        section="cloud.relay",
        key="dry_run_emits_audit",
        source=source,
    )
    if not dry_audit_bool:
        raise ValueError(_CLOUD_RELAY_LOCK_ERROR_MESSAGES["dry_run_emits_audit"])

    return CloudRelayConfig(
        mode=mode_raw,
        repo_allowlist=tuple(repo_allowlist),
        prompt_size_cap_bytes=cap_int,
        idempotency_window_s=window_int,
        audit_root=audit_root_raw,
        require_confirm_allowlist_flag=confirm_flag_bool,
        secret_scan_enabled=scan_bool,
        dry_run_emits_audit=dry_audit_bool,
    )


def _load_cloud_busy_strategy(
    raw: dict[str, Any], *, source: Path
) -> BusyStrategyConfig:
    """Parse + validate ``[cloud.busy_strategy]`` per ``quota-config.md`` §2.2.

    v0.8.8 T2.2.2. The TOML loader returns dicts for tables; an absent
    ``[cloud]`` or ``[cloud.busy_strategy]`` produces the documented
    defaults so a v0.8.7 deployment that has not yet upgraded its config
    keeps working.

    Validation rules (mirrors :func:`_load_cloud_backoff` style):

    1. Section MUST be a TOML table when present.
    2. ``mode`` MUST be one of :data:`CLOUD_BUSY_STRATEGY_MODES`
       (``"queue"`` or ``"fail_fast"``).
    3. ``queue_poll_interval_s`` ∈
       ``[CLOUD_BUSY_QUEUE_POLL_MIN_S, CLOUD_BUSY_QUEUE_POLL_MAX_S]``.
    4. ``queue_max_wait_s`` is ``0`` (sentinel = "wait forever") OR
       ``[CLOUD_BUSY_QUEUE_MAX_WAIT_MIN_S, CLOUD_BUSY_QUEUE_MAX_WAIT_MAX_S]``.
    5. Inter-key invariant (per spec §2.3 rule 3): when ``mode = "queue"``
       AND ``queue_max_wait_s > 0``, ``queue_poll_interval_s ≤
       queue_max_wait_s`` — otherwise the queue would expire before its
       first poll.
    6. ``notify_on_dispatch`` MUST be a strict TOML bool.
    7. Unknown keys WARN (forward-compat).
    """
    cloud_top = raw.get("cloud", {})
    if not isinstance(cloud_top, dict):
        raise ValueError(
            f"[cloud] in {source} must be a table; got {type(cloud_top).__name__}"
        )
    busy_section = cloud_top.get("busy_strategy", {})
    if not isinstance(busy_section, dict):
        raise ValueError(
            f"[cloud.busy_strategy] in {source} must be a table; "
            f"got {type(busy_section).__name__}"
        )

    _warn_unknown_keys(
        "cloud.busy_strategy",
        busy_section,
        _CLOUD_BUSY_STRATEGY_KNOWN_KEYS,
        source,
    )

    mode_raw = busy_section.get("mode", "queue")
    if not isinstance(mode_raw, str):
        raise ValueError(
            f"[cloud.busy_strategy].mode in {source} must be a string; "
            f"got {mode_raw!r} (type {type(mode_raw).__name__})"
        )
    if mode_raw not in CLOUD_BUSY_STRATEGY_MODES:
        raise ValueError(
            f"[cloud.busy_strategy].mode in {source} must be one of "
            f"{sorted(CLOUD_BUSY_STRATEGY_MODES)}; got {mode_raw!r}"
        )

    poll_raw = busy_section.get("queue_poll_interval_s", 5)
    poll_int = _require_int(
        poll_raw,
        section="cloud.busy_strategy",
        key="queue_poll_interval_s",
        source=source,
    )
    poll_int = _require_range(
        poll_int,
        section="cloud.busy_strategy",
        key="queue_poll_interval_s",
        source=source,
        lo=CLOUD_BUSY_QUEUE_POLL_MIN_S,
        hi=CLOUD_BUSY_QUEUE_POLL_MAX_S,
    )

    max_wait_raw = busy_section.get("queue_max_wait_s", 1800)
    max_wait_int = _require_int(
        max_wait_raw,
        section="cloud.busy_strategy",
        key="queue_max_wait_s",
        source=source,
    )
    # ``0`` is a special sentinel for "wait forever" per spec §2.2; the
    # range check is skipped for that exact value so operators must
    # explicitly opt in by spelling ``= 0`` (any other value < lo or > hi
    # rejects). This mirrors the spec wording: "[60, 86_400] (or 0)".
    if max_wait_int != 0:
        max_wait_int = _require_range(
            max_wait_int,
            section="cloud.busy_strategy",
            key="queue_max_wait_s",
            source=source,
            lo=CLOUD_BUSY_QUEUE_MAX_WAIT_MIN_S,
            hi=CLOUD_BUSY_QUEUE_MAX_WAIT_MAX_S,
        )

    notify_raw = busy_section.get("notify_on_dispatch", True)
    notify_bool = _require_bool(
        notify_raw,
        section="cloud.busy_strategy",
        key="notify_on_dispatch",
        source=source,
    )

    # Inter-key invariant: when mode="queue" and queue_max_wait_s > 0,
    # the poll interval must fit within the wait window — else the queue
    # would expire before it is polled even once. The error names both
    # keys so the operator sees the relationship, not just one half.
    if (
        mode_raw == "queue"
        and max_wait_int > 0
        and poll_int > max_wait_int
    ):
        raise ValueError(
            f"[cloud.busy_strategy] in {source}: queue_poll_interval_s "
            f"({poll_int}) must be <= queue_max_wait_s ({max_wait_int}) "
            f"when mode='queue' (otherwise the queue expires before "
            f"its first poll)"
        )

    return BusyStrategyConfig(
        mode=mode_raw,
        queue_poll_interval_s=poll_int,
        queue_max_wait_s=max_wait_int,
        notify_on_dispatch=notify_bool,
    )


def get_popola_home() -> Path:
    """Return the popola home dir (``$POPOLA_HOME`` or ``~/.popola``).

    Always ensures the directory exists (mkdir parents=True).
    """
    home = os.environ.get("POPOLA_HOME")
    path = Path(home).expanduser().resolve() if home else Path.home() / ".popola"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_socket_path() -> Path:
    """Return the canonical UDS path: ``$POPOLA_HOME/popolad.sock``."""
    return get_popola_home() / "popolad.sock"


def get_pid_path() -> Path:
    """Return the canonical PID file path: ``$POPOLA_HOME/popolad.pid``."""
    return get_popola_home() / "popolad.pid"


def get_events_dir() -> Path:
    """Return the canonical events dir: ``$POPOLA_HOME/events``."""
    events = get_popola_home() / "events"
    events.mkdir(parents=True, exist_ok=True)
    return events


def write_pid_file(pid_path: Path | None = None) -> Path:
    """Write current process pid to ``pid_path`` and return that path."""
    pid_path = pid_path or get_pid_path()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    return pid_path


def remove_pid_file(pid_path: Path | None = None) -> None:
    """Best-effort PID file removal (logs but does not raise on failure)."""
    pid_path = pid_path or get_pid_path()
    try:
        if pid_path.exists():
            pid_path.unlink()
    except OSError as exc:
        logger.warning("Failed to remove PID file %s: %s", pid_path, exc)


def remove_socket(socket_path: Path | None = None) -> None:
    """Best-effort UDS file cleanup (logs but does not raise on failure)."""
    socket_path = socket_path or get_socket_path()
    try:
        if socket_path.exists():
            socket_path.unlink()
    except OSError as exc:
        logger.warning("Failed to remove socket %s: %s", socket_path, exc)


def _configure_logging(level: int = logging.INFO) -> None:
    """Configure structured stderr logging for the daemon process.

    Format: ``%(asctime)s %(levelname)s %(name)s %(message)s`` — verbose
    enough for journalctl / log file scraping but no third-party dep.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def _build_persistence_safely() -> Any:
    """Build :class:`TaskPersistence` for the daemon process; tolerate failures.

    Returns ``None`` and logs a warning when ArkTower migrations cannot be
    located (e.g. a wheel install missing the migrations data dir, see
    :func:`popolaloom.daemon.repository._arktower_migrations_dir`).  v0.2.0
    Stage E rehydrate (R-002 closure / S1 self-bootstrap) needs a real
    persistence to recover, but the daemon must still boot for
    ``--no-persistence`` debug runs.
    """
    try:
        from popolaloom.daemon.repository import make_persistence

        return make_persistence()
    except Exception:
        logger.exception(
            "Failed to build TaskPersistence; daemon will boot without "
            "ArkTower persistence (rehydrate disabled, dispatch falls back "
            "to in-memory ArkTask schema parity)"
        )
        return None


def _build_default_popolad(
    events_dir: Path,
    *,
    config: PopoladConfig | None = None,
) -> Any:
    """Construct the production-mode :class:`Popolad` for the daemon process.

    Wires in:

    - The unified 4-arg :func:`popolaloom.adapters.build_command` adapter.
    - A :class:`TaskPersistence` (ArkTower SQLite) when available so
      :meth:`Popolad.rehydrate_from_persistence` can recover in-flight
      tasks across daemon restarts (S1 self-bootstrap requirement).
    - A :class:`PopolaEventBusBridge` subscribed to ArkTower's
      :class:`EventBus` so ``TASK_TRANSITION`` propagates as
      ``task.transition`` NDJSON events.
    - v0.4.1 Stage L2.C: a :class:`LarkSupervisor` wrapping a
      :class:`LarkListener` when ``lark-cli`` is on PATH AND
      ``LARK_HITL_TARGET_OPEN_ID`` (or ``LARK_NOTIFY_TARGET_OPEN_ID``)
      is set. The supervisor is started as a background asyncio task
      on the currently-running loop (this function is called from
      :func:`main` which is itself async), so the daemon does not
      block on lark-cli during construction. When env vars or the
      binary are missing the wiring is skipped with a single INFO log
      (``lark.supervisor.skipped reason=...``) per workspace rule
      "No Silent Failures" — Lark is always optional.
    - v0.8.7 T2.2.1: applies the ``[hitl.cloud]`` defaults from
      ``popolad.toml`` (or :class:`PopoladConfig` defaults when absent)
      onto the cloud HITL bridge so :func:`bridge_for_daemon` picks up
      the configured ``default_timeout_s`` without rpc.py changes.
    """
    from popolaloom.adapters import build_command
    from popolaloom.daemon.event_bus import PopolaEventBusBridge
    from popolaloom.daemon.server import Popolad

    persistence = _build_persistence_safely()
    bridge: PopolaEventBusBridge | None = None
    popolad = Popolad(
        events_dir=events_dir,
        adapter=build_command,
        persistence=persistence,
    )
    if persistence is not None:
        bridge = PopolaEventBusBridge(
            persistence.event_bus,
            popolad.event_log_for_arktower_id,
        )
        popolad._event_bus_bridge = bridge
        bridge.subscribe()

    _maybe_wire_lark_supervisor(popolad)
    _apply_cloud_hitl_config(popolad, config or PopoladConfig())
    return popolad


def _apply_cloud_hitl_config(popolad: Any, config: PopoladConfig) -> None:
    """Wire ``[hitl.cloud]`` settings onto :mod:`popolaloom.hitl.cloud_bridge`.

    v0.8.7 T2.2.1: pushes ``default_timeout_s`` (from
    ``[hitl.cloud].timeout_seconds``) and the per-task event-log resolver
    (``popolad.event_log``) into the cloud bridge module-level state so
    every subsequent :func:`bridge_for_daemon` call honors the config
    without modifying ``daemon/rpc.py`` (T2.1.3 territory).
    """
    from popolaloom.hitl import cloud_bridge

    resolver = getattr(popolad, "event_log", None)
    if not callable(resolver):
        resolver = None

    cloud_bridge.configure_cloud_hitl_defaults(
        default_timeout_s=float(config.hitl.cloud.timeout_seconds),
        idempotency_window_s=int(config.hitl.cloud.idempotency_window_s),
        event_log_resolver=resolver,
    )
    logger.info(
        "cloud_hitl.config applied timeout_seconds=%d idempotency_window_s=%d "
        "max_concurrent_per_run=%d",
        config.hitl.cloud.timeout_seconds,
        config.hitl.cloud.idempotency_window_s,
        config.hitl.cloud.max_concurrent_per_run,
    )


def _maybe_wire_lark_supervisor(popolad: Any) -> None:
    """Construct + schedule a :class:`LarkSupervisor` when env vars opt in.

    v0.4.1 Stage L2.C: the daemon supervises ``lark-cli event consume``
    automatically when both gating conditions are met:

    1. :func:`popolaloom.lark.is_lark_runtime_available` returns ``True``
       (i.e. ``lark-cli`` is on the daemon's PATH).
    2. :func:`popolaloom.lark.lark_target_open_id` resolves a non-empty
       Lark open_id (i.e. ``LARK_HITL_TARGET_OPEN_ID`` is set; the new
       ``LARK_NOTIFY_TARGET_OPEN_ID`` is consulted by
       :mod:`popolaloom.lark.notifier` for outbound notifications, but
       the listener target is the existing HITL env var because the
       inbound side reuses the same chat).

    Either condition false → log INFO ``lark.supervisor.skipped
    reason=...`` and return without touching ``popolad``.

    The supervisor's :meth:`LarkSupervisor.start` is async; we capture
    the running loop and schedule the start as a background task via
    :meth:`asyncio.AbstractEventLoop.create_task` so this function
    stays sync (the chaos tests in
    ``tests/matrix/chaos/test_chaos_uds_socket_*.py`` mock
    ``_build_default_popolad`` to return a MagicMock and expect a
    sync return shape — making this async would break them).

    Per workspace rule "No Silent Failures": LarkSupervisor.start
    failures are logged + bubbled into the supervisor's ``on_event``
    stream (which the supervisor itself surfaces); they do NOT abort
    daemon startup because Lark is optional.
    """
    from popolaloom.lark import is_lark_runtime_available, lark_target_open_id

    if not is_lark_runtime_available():
        logger.info("lark.supervisor.skipped reason=lark_cli_unavailable")
        return
    target = lark_target_open_id()
    if target is None:
        logger.info(
            "lark.supervisor.skipped reason=lark_target_open_id_unset"
        )
        return

    from popolaloom.lark import lark_allowed_responders
    from popolaloom.lark.listener import DEFAULT_EVENTS, LarkListener
    from popolaloom.lark.supervisor import LarkSupervisor

    callbacks = _build_lark_callbacks(popolad)
    listener = LarkListener(
        callbacks=callbacks,
        allowed_responders=lark_allowed_responders(),
        events=DEFAULT_EVENTS,
    )
    supervisor = LarkSupervisor(
        listener=listener,
        on_event=_make_supervisor_event_logger(),
    )
    popolad._lark_supervisor = supervisor

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "lark.supervisor.skipped reason=no_running_loop "
            "(LarkSupervisor.start could not be scheduled; tests must "
            "call it manually)"
        )
        return

    if hasattr(popolad, "attach_loop"):
        popolad.attach_loop(loop)
    loop.create_task(_safe_supervisor_start(supervisor))
    logger.info(
        "lark.supervisor.scheduled target=%s events=%s",
        target,
        ",".join(DEFAULT_EVENTS),
    )


async def _safe_supervisor_start(supervisor: Any) -> None:
    """Wrap :meth:`LarkSupervisor.start` with No-Silent-Failures logging.

    The supervisor itself catches listener-startup failures and
    surfaces them via its ``on_event`` callback, so this wrapper only
    needs to guard against unexpected exceptions in the start
    coroutine (e.g. ``lark-cli`` binary disappeared between PATH check
    and exec). Logged + swallowed: daemon must keep serving even when
    Lark is broken.
    """
    try:
        await supervisor.start()
    except Exception:
        logger.exception("lark.supervisor.start_failed; daemon continues without Lark")


def _build_lark_callbacks(popolad: Any) -> Any:
    """Build :class:`LarkEventCallbacks` that route into the HITL store.

    When :attr:`Popolad.hitl_store` is wired (v0.3.0 F4.C — set later
    by :func:`popolaloom.daemon.rpc.create_app` lifespan or by tests)
    incoming card-action / text-feedback events are folded into the
    cloud HITL bridge so the HITL prompt is marked answered. When
    ``hitl_store`` is ``None`` (early in daemon boot or in tests that
    don't wire HITL) the callbacks log at DEBUG and drop — this
    matches the v0.3.0 contract where unwired channels are silent
    observers, not errors.

    v0.8.7 C1 wiring (REVIEW.md): card-action callbacks no longer call
    ``store.fold_reply`` directly. They route through
    :meth:`CloudHITLBridge.submit_answer` with the ``expected_cursor_*``
    kwargs derived from the row's stored ``metadata`` JSON column so a
    forwarded card click from a different ``cursor_run_id`` is rejected
    at the bridge layer (the row stays ``pending`` and the audit trail
    records the rejection). Text feedback callbacks keep the
    ``store.fold_reply`` shape because their cursor context is only
    visible via the bridge's lookup; they layer the same lookup before
    folding.

    The callbacks always log the receipt of an event so operators
    grepping daemon logs can confirm the listener is alive even before
    HITL is wired (per workspace rule "No Silent Failures": every drop
    has an explicit reason in the log).
    """
    from popolaloom.hitl import HITLReply
    from popolaloom.hitl.cloud_bridge import bridge_for_daemon
    from popolaloom.lark.listener import LarkEventCallbacks

    async def on_card_action(
        event: dict[str, Any], parsed: tuple[str, str]
    ) -> None:
        hitl_id, option_id = parsed
        store = getattr(popolad, "hitl_store", None)
        if store is None:
            logger.debug(
                "lark.listener.card_action: hitl_store unwired; dropping "
                "hitl_id=%s option=%s",
                hitl_id,
                option_id,
            )
            return
        sender = _extract_sender_open_id(event)

        # C1 wiring: build a cloud-bridge instance (no Lark fan-out — we
        # are *receiving* a card click) and derive the expected cursor
        # tuple from the row's metadata. The bridge's submit_answer
        # rejects with ``mis-route:...`` when the inbound and stored
        # tuples disagree, so a forwarded / replayed Lark click cannot
        # answer a row owned by a different cursor_run_id (SECURITY R5).
        #
        # Backward-compat fallback: when ``store`` is a partial test fake
        # (no ``.conn`` attribute on real HITLStore), the bridge cannot
        # be constructed; route through the legacy ``store.fold_reply``
        # path so v0.8.5-era tests keep their wiring assertions.
        try:
            bridge = bridge_for_daemon(store, send_lark=False)
        except (AttributeError, TypeError):
            bridge = None
        if bridge is None:
            reply = HITLReply(
                hitl_id=hitl_id,
                option_id=option_id,
                via="lark",
                responder=sender,
            )
            try:
                await asyncio.to_thread(store.fold_reply, reply)
            except Exception:
                logger.exception(
                    "lark.listener.card_action: fold_reply raised hitl_id=%s",
                    hitl_id,
                )
            return

        existing = bridge.get_request(hitl_id)
        expected_agent: str | None = None
        expected_run: str | None = None
        if existing is not None:
            expected_agent = existing.cursor_agent_id
            expected_run = existing.cursor_run_id

        def _answer() -> tuple[bool, str | None]:
            return bridge.submit_answer(
                hitl_id,
                option_id,
                responder_id=sender or "",
                channel="lark",
                expected_cursor_agent_id=expected_agent,
                expected_cursor_run_id=expected_run,
            )

        try:
            ok, descriptor = await asyncio.to_thread(_answer)
        except Exception:
            logger.exception(
                "lark.listener.card_action: submit_answer raised hitl_id=%s",
                hitl_id,
            )
            return

        if not ok and descriptor and descriptor.startswith("mis-route:"):
            logger.warning(
                "lark.listener.card_action rejected mis-route hitl_id=%s "
                "sender=%s descriptor=%s",
                hitl_id,
                sender,
                descriptor,
            )
            return
        if not ok:
            logger.info(
                "lark.listener.card_action lost race hitl_id=%s descriptor=%s",
                hitl_id,
                descriptor,
            )
            return
        # Reply object retained for symmetry with text-feedback path
        # (audit consumers may emit downstream events keyed off it).
        _ = HITLReply(
            hitl_id=hitl_id,
            option_id=option_id,
            via="lark",
            responder=sender,
        )

    async def on_text_feedback(
        event: dict[str, Any], parsed: dict[str, str]
    ) -> None:
        hitl_id = parsed.get("hitl_id", "")
        option_id = parsed.get("option_id", "")
        store = getattr(popolad, "hitl_store", None)
        if store is None:
            logger.debug(
                "lark.listener.text_feedback: hitl_store unwired; dropping "
                "hitl_id=%s option=%s",
                hitl_id,
                option_id,
            )
            return
        sender = _extract_sender_open_id(event)
        reply = HITLReply(
            hitl_id=hitl_id,
            option_id=option_id,
            via="lark",
            reason=parsed.get("reason"),
            responder=sender,
        )
        try:
            await asyncio.to_thread(store.fold_reply, reply)
        except Exception:
            logger.exception(
                "lark.listener.text_feedback: fold_reply raised hitl_id=%s",
                hitl_id,
            )

    async def on_unauthorized(event: dict[str, Any], sender: str) -> None:
        header = event.get("header")
        event_id = header.get("event_id") if isinstance(header, dict) else None
        logger.warning(
            "lark.listener.unauthorized sender=%s event_id=%s",
            sender,
            event_id,
        )

    return LarkEventCallbacks(
        on_card_action=on_card_action,
        on_text_feedback=on_text_feedback,
        on_unauthorized=on_unauthorized,
    )


def _extract_sender_open_id(event: dict[str, Any]) -> str | None:
    """Best-effort sender open_id extraction (mirrors listener's helper).

    Inlined here to avoid pulling in the listener module's private
    helper (``listener._extract_sender_open_id``); keeps the daemon
    main file self-contained for the wiring path.
    """
    inner = event.get("event") if isinstance(event.get("event"), dict) else event
    if not isinstance(inner, dict):
        return None
    sender = inner.get("sender")
    if isinstance(sender, dict):
        sender_id = sender.get("sender_id")
        if isinstance(sender_id, dict):
            oid = sender_id.get("open_id")
            if isinstance(oid, str) and oid:
                return oid
        oid = sender.get("open_id")
        if isinstance(oid, str) and oid:
            return oid
    operator = inner.get("operator")
    if isinstance(operator, dict):
        oid = operator.get("open_id")
        if isinstance(oid, str) and oid:
            return oid
    return None


def _make_supervisor_event_logger() -> Any:
    """Build a :class:`LarkSupervisor` ``on_event`` logger callback.

    The supervisor emits one of ``listener.started`` /
    ``listener.died`` / ``listener.restarted`` /
    ``listener.escalated`` per lifecycle event; we surface them at
    INFO so operators grep ``lark.supervisor.event`` to track listener
    health alongside the existing ``lark.send.*`` envelopes (v0.3.3
    round-3 lark_health real fixture pattern).
    """
    async def _on_event(event: dict[str, str]) -> None:
        logger.info(
            "lark.supervisor.event %s",
            " ".join(f"{k}={v}" for k, v in event.items()),
        )

    return _on_event


async def main(
    *,
    socket_path: Path | None = None,
    events_dir: Path | None = None,
    pid_path: Path | None = None,
    log_level: str = "info",
) -> None:
    """Run the popolad daemon until SIGTERM/SIGINT.

    Args:
        socket_path: UDS bind path (default ``$POPOLA_HOME/popolad.sock``).
        events_dir: NDJSON events directory (default ``$POPOLA_HOME/events``).
        pid_path: PID file path (default ``$POPOLA_HOME/popolad.pid``).
        log_level: uvicorn / root logger level string.

    Behavior:

    1. Configure stderr logging.
    2. Compute socket / pid / events paths (env-overridable).
    3. Cleanup any stale socket file (last daemon may have crashed).
    4. Write PID file.
    5. Construct production-wired :class:`Popolad` (ArkTower persistence +
       event-bus bridge); pass into :func:`create_app`.
    6. Build uvicorn server with ``uds=`` parameter.
    7. Install asyncio signal handlers (SIGTERM / SIGINT) → graceful shutdown.
    8. ``await server.serve()``.
    9. On exit (graceful or exception), remove PID + socket files.
    """
    _configure_logging(level=getattr(logging, log_level.upper(), logging.INFO))

    socket_path = socket_path or get_socket_path()
    events_dir = events_dir or get_events_dir()
    pid_path = pid_path or get_pid_path()

    if socket_path.exists():
        logger.info("Removing stale socket file: %s", socket_path)
        try:
            socket_path.unlink()
        except OSError as exc:
            logger.error("Could not remove stale socket %s: %s", socket_path, exc)
            raise

    write_pid_file(pid_path)
    logger.info(
        "popolad starting (pid=%d, sock=%s, events=%s)",
        os.getpid(),
        socket_path,
        events_dir,
    )

    try:
        popolad_config = load_popolad_config()
    except (ValueError, OSError) as exc:
        logger.error(
            "popolad.toml is invalid: %s; refusing to start (No Silent Failures)",
            exc,
        )
        raise

    popolad = _build_default_popolad(events_dir, config=popolad_config)
    app = create_app(popolad=popolad)

    config = uvicorn.Config(
        app=app,
        uds=str(socket_path),
        log_level=log_level,
        access_log=False,
        loop="asyncio",
        lifespan="on",
    )
    server = uvicorn.Server(config)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler(sig: int) -> None:
        logger.info(
            "Received signal %d (%s); initiating graceful shutdown",
            sig,
            signal.Signals(sig).name,
        )
        server.should_exit = True
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler, sig)
        except NotImplementedError:
            logger.warning("add_signal_handler not supported for %s; relying on default", sig)

    try:
        await server.serve()
    finally:
        logger.info("popolad exiting; cleaning up PID + socket")
        remove_pid_file(pid_path)
        remove_socket(socket_path)


def run() -> None:
    """Synchronous entry — wraps :func:`main` in :func:`asyncio.run`.

    This is what ``python -m popolaloom.daemon`` invokes via ``__main__.py``.
    Splitting ``main`` (async) from ``run`` (sync) lets tests ``await main()``
    in their own loop without monkey-patching :func:`asyncio.run`.
    """
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("popolad interrupted by KeyboardInterrupt; cleanup attempted")
    except Exception:
        logger.exception("popolad failed with unhandled exception")
        raise


if __name__ == "__main__":  # pragma: no cover - module entry
    run()


def __getattr__(name: str) -> Any:  # pragma: no cover - debug aid
    """Module-level fallback: surface Popolad / create_app for ``python -m`` REPL.

    Used by debug-style imports like ``from popolaloom.daemon.main import
    Popolad``; primary public surface is in :mod:`popolaloom.daemon`.
    """
    if name == "Popolad":
        from popolaloom.daemon.server import Popolad  # noqa: PLC0415

        return Popolad
    if name == "create_app":
        return create_app
    raise AttributeError(name)
