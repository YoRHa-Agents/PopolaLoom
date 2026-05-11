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
import dataclasses
import logging
import os
import re
import signal
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import uvicorn

from popolaloom import credentials
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
        "v0.8.8 release lock: require_confirm_allowlist_flag must stay true (Q-C-4 mitigation M1)"
    ),
    "secret_scan_enabled": (
        "v0.8.8 release lock: secret_scan_enabled must stay true (Q-C-4 mitigation M3)"
    ),
    "dry_run_emits_audit": (
        "v0.8.8 release lock: dry_run_emits_audit must stay true (Q-C-4 mitigation M2)"
    ),
}
"""Spec-locked error messages for the three v0.8.8-locked bool keys.

Per ``relay-auto-safety.md`` §3.1 the loader MUST raise ``ValueError``
with these exact strings when an operator attempts to disable any of
the three release-gate mitigations. PLAN.md §9 box C1 evidence requires
the messages match verbatim.
"""


# ── popolad.toml [user_preferences] loader (v0.9.10) ─────────────────────

USER_PREF_VALID_RUNTIMES: Final[frozenset[str]] = frozenset({"local", "cloud", "ask-each-time"})
"""Accepted values for ``[user_preferences].default_runtime``."""

USER_PREF_VALID_CLOUD_TARGETS: Final[frozenset[str]] = frozenset({"self-hosted", "cursor-managed"})
"""Accepted entries for ``cloud_target_priority`` (order-sensitive).

Deprecated in v0.10.0 (DECISIONS Q-5): superseded by the single-value
:data:`USER_PREF_VALID_DEFAULT_CLOUD_TARGET` field. Kept for one minor
release with a one-time deprecation WARN so existing operator TOML files
keep loading.
"""

USER_PREF_VALID_DEFAULT_CLOUD_TARGET: Final[frozenset[str]] = frozenset(
    {"self-hosted", "cursor-managed", "ask-each-time"}
)
"""Accepted values for ``[user_preferences].default_cloud_target`` (Q-5).

Single-value successor to the legacy ``cloud_target_priority`` list; the
``ask-each-time`` sentinel preserves the v0.9.x interactive-prompt semantic
for operators who do not want to pin a default cloud target.
"""

USER_PREF_VALID_LOCAL_CLIS: Final[frozenset[str]] = frozenset(
    {"cursor", "claude", "codex", "copilot"}
)
"""Accepted local CLI adapter names for preferences."""

_USER_PREF_LEGACY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "default_runtime",
        "cloud_target_priority",
        "default_cloud_target",
        "default_local_cli",
        "fallback_chain",
        "hitl_enabled",
        "follow_devola_flow",
        "prompt_each_dispatch",
        "last_set_at",
        "last_set_by",
    }
)
"""Legacy flat keys recognised under ``[user_preferences]``."""

_USER_PREF_SECTION_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "routing",
        "defaults",
        "cursor",
        "cursor-cloud",
        "claude",
        "codex",
        "lark",
        "dispatch",
    }
)
"""Nested v2 child tables recognised under ``[user_preferences]``."""

_USER_PREF_KNOWN_KEYS: Final[frozenset[str]] = _USER_PREF_LEGACY_KEYS | _USER_PREF_SECTION_KEYS
"""All keys recognised under ``[user_preferences]``.

Unlike the cloud v0.8.8 blocks, this section is user-authored by the init
wizard and CI flags. Unknown keys are therefore rejected so typos do not
silently change dispatch behavior.
"""

_USER_PREF_ROUTING_KEYS: Final[frozenset[str]] = frozenset(
    {"default_runtime", "default_local_cli", "fallback_chain", "cloud_target_priority"}
)
_USER_PREF_DEFAULTS_KEYS: Final[frozenset[str]] = frozenset(
    {"wait_timeout_s", "hitl_enabled", "follow_devola_flow", "prompt_each_dispatch"}
)
_USER_PREF_CURSOR_KEYS: Final[frozenset[str]] = frozenset({"output_format", "cli_args"})
_USER_PREF_CURSOR_CLOUD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "model",
        "starting_ref",
        "auto_create_pr",
        "work_on_current_branch",
        "skip_reviewer_request",
        "default_cloud_target",
        "worker_name",
        "pool_name",
    }
)
_USER_PREF_CLAUDE_KEYS: Final[frozenset[str]] = frozenset({"max_turns"})
_USER_PREF_CODEX_KEYS: Final[frozenset[str]] = frozenset({"sandbox"})
_USER_PREF_LARK_KEYS: Final[frozenset[str]] = frozenset(
    {
        "notify_on_completed",
        "notify_on_failed",
        "notify_on_canceled",
        "notify_on_cancel_escalated",
        "prompt_truncate",
    }
)
_USER_PREF_DISPATCH_KEYS: Final[frozenset[str]] = frozenset(
    {"ambiguity_resolution", "ask_dimensions"}
)

USER_PREF_VALID_CURSOR_OUTPUT_FORMATS: Final[frozenset[str]] = frozenset({"text", "stream-json"})
USER_PREF_VALID_CODEX_SANDBOXES: Final[frozenset[str]] = frozenset(
    {"read-only", "workspace-write", "danger-full-access"}
)
USER_PREF_VALID_AMBIGUITY_RESOLUTION: Final[frozenset[str]] = frozenset(
    {"prompt", "use-defaults", "fail"}
)
USER_PREF_VALID_ASK_DIMENSIONS: Final[frozenset[str]] = frozenset(
    {"target", "model", "thinking_depth", "special_modes"}
)


_CLOUD_TARGET_PRIORITY_DEPRECATION_WARNED: bool = False
"""Process-lifetime flag gating the one-time ``cloud_target_priority`` WARN.

Per DECISIONS Q-5 / PLAN B1 AC 6, the legacy list-of-targets
``cloud_target_priority`` is read-only-with-deprecation-warn during the
v0.10.x window. The WARN fires only when (a) the operator's TOML
explicitly sets ``cloud_target_priority`` and (b) ``default_cloud_target``
is still at its ``"ask-each-time"`` default (i.e. the operator has not
yet migrated). The flag is module-level so the WARN fires at most once
per process; tests reset it via ``monkeypatch`` between scenarios.
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
class UserPrefsRouting:
    """Routing preferences shared by local and cloud dispatch."""

    default_runtime: str = "local"
    default_local_cli: str = "cursor"
    fallback_chain: tuple[str, ...] = ()
    cloud_target_priority: tuple[str, ...] = ("self-hosted", "cursor-managed")


@dataclass(frozen=True)
class UserPrefsDefaults:
    """General dispatch defaults that are not adapter-specific."""

    wait_timeout_s: int = 60
    hitl_enabled: bool = True
    follow_devola_flow: bool = False
    prompt_each_dispatch: bool = False


@dataclass(frozen=True)
class UserPrefsCursor:
    """Local Cursor CLI defaults."""

    output_format: str = "text"
    cli_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class UserPrefsCursorCloud:
    """Cursor Cloud defaults."""

    model: str = "default"
    starting_ref: str = "main"
    auto_create_pr: bool = False
    work_on_current_branch: bool = False
    skip_reviewer_request: bool = False
    default_cloud_target: str = "ask-each-time"
    worker_name: str = ""
    pool_name: str = ""


@dataclass(frozen=True)
class UserPrefsClaude:
    """Claude Code defaults."""

    max_turns: int = 0


@dataclass(frozen=True)
class UserPrefsCodex:
    """Codex defaults."""

    sandbox: str = "workspace-write"


@dataclass(frozen=True)
class UserPrefsLark:
    """Lark notification defaults."""

    notify_on_completed: bool = True
    notify_on_failed: bool = True
    notify_on_canceled: bool = True
    notify_on_cancel_escalated: bool = False
    prompt_truncate: int = 200


@dataclass(frozen=True)
class UserPrefsDispatch:
    """Dispatch ambiguity-resolution preferences."""

    ambiguity_resolution: str = "prompt"
    ask_dimensions: tuple[str, ...] = (
        "target",
        "model",
        "thinking_depth",
        "special_modes",
    )


@dataclass(frozen=True)
class UserPreferencesConfig:
    """Validated ``[user_preferences]`` section of ``popolad.toml``.

    v1.1.0 stores preferences as nested adapter-specific child tables.
    Compatibility properties keep the historical flat attributes readable
    for existing dispatch code and tests while newly-written TOML uses
    ``schema_version = 2``.
    """

    schema_version: int = field(default=2, init=False)
    routing: UserPrefsRouting = field(default_factory=UserPrefsRouting, init=False)
    defaults: UserPrefsDefaults = field(default_factory=UserPrefsDefaults, init=False)
    cursor: UserPrefsCursor = field(default_factory=UserPrefsCursor, init=False)
    cursor_cloud: UserPrefsCursorCloud = field(default_factory=UserPrefsCursorCloud, init=False)
    claude: UserPrefsClaude = field(default_factory=UserPrefsClaude, init=False)
    codex: UserPrefsCodex = field(default_factory=UserPrefsCodex, init=False)
    lark: UserPrefsLark = field(default_factory=UserPrefsLark, init=False)
    dispatch: UserPrefsDispatch = field(default_factory=UserPrefsDispatch, init=False)
    last_set_at: str = field(default="", init=False)
    last_set_by: str = field(default="", init=False)

    def __init__(
        self,
        *,
        schema_version: int = 2,
        routing: UserPrefsRouting | None = None,
        defaults: UserPrefsDefaults | None = None,
        cursor: UserPrefsCursor | None = None,
        cursor_cloud: UserPrefsCursorCloud | None = None,
        claude: UserPrefsClaude | None = None,
        codex: UserPrefsCodex | None = None,
        lark: UserPrefsLark | None = None,
        dispatch: UserPrefsDispatch | None = None,
        last_set_at: str = "",
        last_set_by: str = "",
        default_runtime: str | None = None,
        cloud_target_priority: tuple[str, ...] | list[str] | None = None,
        default_cloud_target: str | None = None,
        default_local_cli: str | None = None,
        fallback_chain: tuple[str, ...] | list[str] | None = None,
        hitl_enabled: bool | None = None,
        follow_devola_flow: bool | None = None,
        prompt_each_dispatch: bool | None = None,
    ) -> None:
        """Create a v2 config while accepting v1 flat constructor kwargs."""
        routing_value = routing or UserPrefsRouting()
        defaults_value = defaults or UserPrefsDefaults()
        cursor_cloud_value = cursor_cloud or UserPrefsCursorCloud()
        if default_runtime is not None:
            routing_value = dataclasses.replace(routing_value, default_runtime=default_runtime)
        if cloud_target_priority is not None:
            routing_value = dataclasses.replace(
                routing_value, cloud_target_priority=tuple(cloud_target_priority)
            )
        if default_local_cli is not None:
            routing_value = dataclasses.replace(routing_value, default_local_cli=default_local_cli)
        if fallback_chain is not None:
            routing_value = dataclasses.replace(
                routing_value, fallback_chain=tuple(fallback_chain)
            )
        if default_cloud_target is not None:
            cursor_cloud_value = dataclasses.replace(
                cursor_cloud_value, default_cloud_target=default_cloud_target
            )
        if hitl_enabled is not None:
            defaults_value = dataclasses.replace(defaults_value, hitl_enabled=hitl_enabled)
        if follow_devola_flow is not None:
            defaults_value = dataclasses.replace(
                defaults_value, follow_devola_flow=follow_devola_flow
            )
        if prompt_each_dispatch is not None:
            defaults_value = dataclasses.replace(
                defaults_value, prompt_each_dispatch=prompt_each_dispatch
            )

        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "routing", routing_value)
        object.__setattr__(self, "defaults", defaults_value)
        object.__setattr__(self, "cursor", cursor or UserPrefsCursor())
        object.__setattr__(self, "cursor_cloud", cursor_cloud_value)
        object.__setattr__(self, "claude", claude or UserPrefsClaude())
        object.__setattr__(self, "codex", codex or UserPrefsCodex())
        object.__setattr__(self, "lark", lark or UserPrefsLark())
        object.__setattr__(self, "dispatch", dispatch or UserPrefsDispatch())
        object.__setattr__(self, "last_set_at", last_set_at)
        object.__setattr__(self, "last_set_by", last_set_by)

    @property
    def default_runtime(self) -> str:
        return self.routing.default_runtime

    @property
    def cloud_target_priority(self) -> tuple[str, ...]:
        return self.routing.cloud_target_priority

    @property
    def default_cloud_target(self) -> str:
        return self.cursor_cloud.default_cloud_target

    @property
    def default_local_cli(self) -> str:
        return self.routing.default_local_cli

    @property
    def fallback_chain(self) -> tuple[str, ...]:
        return self.routing.fallback_chain

    @property
    def hitl_enabled(self) -> bool:
        return self.defaults.hitl_enabled

    @property
    def follow_devola_flow(self) -> bool:
        return self.defaults.follow_devola_flow

    @property
    def prompt_each_dispatch(self) -> bool:
        return self.defaults.prompt_each_dispatch


@dataclass(frozen=True)
class PopoladConfig:
    """Top-level ``popolad.toml`` schema as consumed by the daemon."""

    hitl: HITLConfig = field(default_factory=HITLConfig)
    cloud: CloudConfig = field(default_factory=CloudConfig)
    user_preferences: UserPreferencesConfig | None = None


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
        raise ValueError(f"[{section}].{key} in {source} must be in [{lo}, {hi}]; got {value}")
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


def _require_str(
    value: Any,
    *,
    section: str,
    key: str,
    source: Path,
) -> str:
    """Validate that ``value`` is a TOML string."""
    if not isinstance(value, str):
        raise ValueError(
            f"[{section}].{key} in {source} must be a string; got {value!r} "
            f"(type {type(value).__name__})"
        )
    return value


def _require_str_list(
    value: Any,
    *,
    section: str,
    key: str,
    source: Path,
) -> list[str]:
    """Validate that ``value`` is a TOML list of strings."""
    if not isinstance(value, list):
        raise ValueError(
            f"[{section}].{key} in {source} must be a list of strings; got {type(value).__name__}"
        )
    out: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(
                f"[{section}].{key}[{idx}] in {source} must be a string; "
                f"got {item!r} (type {type(item).__name__})"
            )
        out.append(item)
    return out


def _require_iso_string(
    value: Any,
    *,
    section: str,
    key: str,
    source: Path,
) -> str:
    """Validate an optional ISO-8601 timestamp string."""
    text = _require_str(value, section=section, key=key, source=source)
    if not text:
        return text
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"[{section}].{key} in {source} must be an ISO-8601 string; got {text!r}"
        ) from exc
    return text


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


def _reject_unknown_keys(
    section_name: str,
    section: dict[str, Any],
    known: frozenset[str],
    source: Path,
) -> None:
    """Reject keys not in ``known`` (used for strict user-facing config)."""
    extras = sorted(set(section) - known)
    if extras:
        raise ValueError(
            f"[{section_name}] in {source}: unknown key(s) {', '.join(extras)}; "
            f"known keys: {', '.join(sorted(known))}"
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
        raise ValueError(f"[hitl] in {p} must be a table; got {type(hitl_section).__name__}")
    cloud_section = hitl_section.get("cloud", {})
    if not isinstance(cloud_section, dict):
        raise ValueError(
            f"[hitl.cloud] in {p} must be a table; got {type(cloud_section).__name__}"
        )

    timeout_raw = cloud_section.get("timeout_seconds", 1800)
    timeout_int = _require_int(timeout_raw, section="hitl.cloud", key="timeout_seconds", source=p)
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
    user_preferences_cfg = _load_user_preferences(raw, source=p)

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
        user_preferences=user_preferences_cfg,
    )


def _load_user_preferences(
    raw: dict[str, Any],
    *,
    source: Path,
) -> UserPreferencesConfig | None:
    """Parse + validate optional ``[user_preferences]``.

    Missing section returns ``None`` to preserve the v0.9.9 CLI contract.
    A present empty section returns defaults. v1 flat keys are accepted,
    migrated to v2 in memory, and persisted back to disk with a ``.v1.bak``
    backup when the source file exists.
    """
    if "user_preferences" not in raw:
        return None
    section = raw["user_preferences"]
    if not isinstance(section, dict):
        raise ValueError(
            f"[user_preferences] in {source} must be a table; got {type(section).__name__}"
        )

    _reject_unknown_keys("user_preferences", section, _USER_PREF_KNOWN_KEYS, source)
    migrated = _migrate_flat_to_nested(section, source=source)
    prefs = _parse_user_preferences_v2(migrated, source=source)
    if migrated is not section:
        _persist_user_preferences_migration(raw, migrated, source=source)
    return prefs


def _migrate_flat_to_nested(
    section: dict[str, Any],
    *,
    source: Path,
) -> dict[str, Any]:
    """Return a v2-shaped section, preserving nested keys over legacy flats."""
    schema_version = _require_int(
        section.get("schema_version", 1),
        section="user_preferences",
        key="schema_version",
        source=source,
    )
    migration_keys = set(section) & (_USER_PREF_LEGACY_KEYS - {"last_set_at", "last_set_by"})
    has_legacy = bool(migration_keys)
    if schema_version == 2 and not has_legacy:
        return section
    if schema_version not in {1, 2}:
        raise ValueError(
            f"[user_preferences].schema_version in {source} must be 1 or 2; got {schema_version}"
        )

    out: dict[str, Any] = {
        key: value
        for key, value in section.items()
        if key in _USER_PREF_SECTION_KEYS and key != "schema_version"
    }
    out["schema_version"] = 2
    routing = dict(out.get("routing", {}))
    defaults = dict(out.get("defaults", {}))
    cursor_cloud = dict(out.get("cursor-cloud", {}))

    legacy_routing = {
        "default_runtime",
        "default_local_cli",
        "fallback_chain",
        "cloud_target_priority",
    }
    for key in legacy_routing:
        if key in section and key not in routing:
            routing[key] = section[key]
    for key in {"hitl_enabled", "follow_devola_flow", "prompt_each_dispatch"}:
        if key in section and key not in defaults:
            defaults[key] = section[key]
    if "default_cloud_target" in section and "default_cloud_target" not in cursor_cloud:
        cursor_cloud["default_cloud_target"] = section["default_cloud_target"]
    if routing:
        out["routing"] = routing
    if defaults:
        out["defaults"] = defaults
    if cursor_cloud:
        out["cursor-cloud"] = cursor_cloud
    if "last_set_at" in section:
        out["last_set_at"] = section["last_set_at"]
    if "last_set_by" in section:
        out["last_set_by"] = section["last_set_by"]

    logger.info(
        "migrated [user_preferences] from flat schema v%s to nested schema v2 "
        "(legacy keys remain readable); source=%s",
        schema_version,
        source,
    )
    return out


def _table(section: dict[str, Any], key: str, *, source: Path) -> dict[str, Any]:
    value = section.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(
            f"[user_preferences.{key}] in {source} must be a table; got {type(value).__name__}"
        )
    return value


def _validate_enum(
    value: str,
    valid: frozenset[str],
    *,
    section: str,
    key: str,
    source: Path,
) -> str:
    if value not in valid:
        raise ValueError(
            f"[{section}].{key} in {source} must be one of {sorted(valid)}; got {value!r}"
        )
    return value


def _validate_str_list_members(
    values: list[str],
    valid: frozenset[str],
    *,
    section: str,
    key: str,
    source: Path,
) -> tuple[str, ...]:
    for idx, item in enumerate(values):
        if item not in valid:
            raise ValueError(
                f"[{section}].{key}[{idx}] in {source} must be one of "
                f"{sorted(valid)}; got {item!r}"
            )
    return tuple(values)


def _parse_user_preferences_v2(
    section: dict[str, Any],
    *,
    source: Path,
) -> UserPreferencesConfig:
    schema_version = _require_int(
        section.get("schema_version", 2),
        section="user_preferences",
        key="schema_version",
        source=source,
    )
    if schema_version != 2:
        raise ValueError(
            f"[user_preferences].schema_version in {source} must be 2 after "
            f"migration; got {schema_version}"
        )

    routing_section = _table(section, "routing", source=source)
    defaults_section = _table(section, "defaults", source=source)
    cursor_section = _table(section, "cursor", source=source)
    cursor_cloud_section = _table(section, "cursor-cloud", source=source)
    claude_section = _table(section, "claude", source=source)
    codex_section = _table(section, "codex", source=source)
    lark_section = _table(section, "lark", source=source)
    dispatch_section = _table(section, "dispatch", source=source)

    _reject_unknown_keys(
        "user_preferences.routing", routing_section, _USER_PREF_ROUTING_KEYS, source
    )
    _reject_unknown_keys(
        "user_preferences.defaults", defaults_section, _USER_PREF_DEFAULTS_KEYS, source
    )
    _reject_unknown_keys("user_preferences.cursor", cursor_section, _USER_PREF_CURSOR_KEYS, source)
    _reject_unknown_keys(
        "user_preferences.cursor-cloud", cursor_cloud_section, _USER_PREF_CURSOR_CLOUD_KEYS, source
    )
    _reject_unknown_keys("user_preferences.claude", claude_section, _USER_PREF_CLAUDE_KEYS, source)
    _reject_unknown_keys("user_preferences.codex", codex_section, _USER_PREF_CODEX_KEYS, source)
    _reject_unknown_keys("user_preferences.lark", lark_section, _USER_PREF_LARK_KEYS, source)
    _reject_unknown_keys(
        "user_preferences.dispatch", dispatch_section, _USER_PREF_DISPATCH_KEYS, source
    )

    default_runtime = _validate_enum(
        _require_str(
            routing_section.get("default_runtime", "local"),
            section="user_preferences.routing",
            key="default_runtime",
            source=source,
        ),
        USER_PREF_VALID_RUNTIMES,
        section="user_preferences.routing",
        key="default_runtime",
        source=source,
    )
    default_local_cli = _validate_enum(
        _require_str(
            routing_section.get("default_local_cli", "cursor"),
            section="user_preferences.routing",
            key="default_local_cli",
            source=source,
        ),
        USER_PREF_VALID_LOCAL_CLIS,
        section="user_preferences.routing",
        key="default_local_cli",
        source=source,
    )
    fallback_chain = _validate_str_list_members(
        _require_str_list(
            routing_section.get("fallback_chain", []),
            section="user_preferences.routing",
            key="fallback_chain",
            source=source,
        ),
        USER_PREF_VALID_LOCAL_CLIS,
        section="user_preferences.routing",
        key="fallback_chain",
        source=source,
    )
    cloud_target_priority = _validate_str_list_members(
        _require_str_list(
            routing_section.get("cloud_target_priority", ["self-hosted", "cursor-managed"]),
            section="user_preferences.routing",
            key="cloud_target_priority",
            source=source,
        ),
        USER_PREF_VALID_CLOUD_TARGETS,
        section="user_preferences.routing",
        key="cloud_target_priority",
        source=source,
    )
    cursor_output_format = _validate_enum(
        _require_str(
            cursor_section.get("output_format", "text"),
            section="user_preferences.cursor",
            key="output_format",
            source=source,
        ),
        USER_PREF_VALID_CURSOR_OUTPUT_FORMATS,
        section="user_preferences.cursor",
        key="output_format",
        source=source,
    )
    default_cloud_target = _validate_enum(
        _require_str(
            cursor_cloud_section.get("default_cloud_target", "ask-each-time"),
            section="user_preferences.cursor-cloud",
            key="default_cloud_target",
            source=source,
        ),
        USER_PREF_VALID_DEFAULT_CLOUD_TARGET,
        section="user_preferences.cursor-cloud",
        key="default_cloud_target",
        source=source,
    )
    codex_sandbox = _validate_enum(
        _require_str(
            codex_section.get("sandbox", "workspace-write"),
            section="user_preferences.codex",
            key="sandbox",
            source=source,
        ),
        USER_PREF_VALID_CODEX_SANDBOXES,
        section="user_preferences.codex",
        key="sandbox",
        source=source,
    )
    ambiguity_resolution = _validate_enum(
        _require_str(
            dispatch_section.get("ambiguity_resolution", "prompt"),
            section="user_preferences.dispatch",
            key="ambiguity_resolution",
            source=source,
        ),
        USER_PREF_VALID_AMBIGUITY_RESOLUTION,
        section="user_preferences.dispatch",
        key="ambiguity_resolution",
        source=source,
    )
    ask_dimensions = _validate_str_list_members(
        _require_str_list(
            dispatch_section.get(
                "ask_dimensions", ["target", "model", "thinking_depth", "special_modes"]
            ),
            section="user_preferences.dispatch",
            key="ask_dimensions",
            source=source,
        ),
        USER_PREF_VALID_ASK_DIMENSIONS,
        section="user_preferences.dispatch",
        key="ask_dimensions",
        source=source,
    )

    global _CLOUD_TARGET_PRIORITY_DEPRECATION_WARNED
    if (
        not _CLOUD_TARGET_PRIORITY_DEPRECATION_WARNED
        and "cloud_target_priority" in routing_section
        and default_cloud_target == "ask-each-time"
    ):
        logger.warning(
            "cloud_target_priority is deprecated as of v0.10.0; use default_cloud_target instead"
        )
        _CLOUD_TARGET_PRIORITY_DEPRECATION_WARNED = True

    return UserPreferencesConfig(
        schema_version=2,
        routing=UserPrefsRouting(
            default_runtime=default_runtime,
            default_local_cli=default_local_cli,
            fallback_chain=fallback_chain,
            cloud_target_priority=cloud_target_priority,
        ),
        defaults=UserPrefsDefaults(
            wait_timeout_s=_require_int(
                defaults_section.get("wait_timeout_s", 60),
                section="user_preferences.defaults",
                key="wait_timeout_s",
                source=source,
            ),
            hitl_enabled=_require_bool(
                defaults_section.get("hitl_enabled", True),
                section="user_preferences.defaults",
                key="hitl_enabled",
                source=source,
            ),
            follow_devola_flow=_require_bool(
                defaults_section.get("follow_devola_flow", False),
                section="user_preferences.defaults",
                key="follow_devola_flow",
                source=source,
            ),
            prompt_each_dispatch=_require_bool(
                defaults_section.get("prompt_each_dispatch", False),
                section="user_preferences.defaults",
                key="prompt_each_dispatch",
                source=source,
            ),
        ),
        cursor=UserPrefsCursor(
            output_format=cursor_output_format,
            cli_args=tuple(
                _require_str_list(
                    cursor_section.get("cli_args", []),
                    section="user_preferences.cursor",
                    key="cli_args",
                    source=source,
                )
            ),
        ),
        cursor_cloud=UserPrefsCursorCloud(
            model=_require_str(
                cursor_cloud_section.get("model", "default"),
                section="user_preferences.cursor-cloud",
                key="model",
                source=source,
            ),
            starting_ref=_require_str(
                cursor_cloud_section.get("starting_ref", "main"),
                section="user_preferences.cursor-cloud",
                key="starting_ref",
                source=source,
            ),
            auto_create_pr=_require_bool(
                cursor_cloud_section.get("auto_create_pr", False),
                section="user_preferences.cursor-cloud",
                key="auto_create_pr",
                source=source,
            ),
            work_on_current_branch=_require_bool(
                cursor_cloud_section.get("work_on_current_branch", False),
                section="user_preferences.cursor-cloud",
                key="work_on_current_branch",
                source=source,
            ),
            skip_reviewer_request=_require_bool(
                cursor_cloud_section.get("skip_reviewer_request", False),
                section="user_preferences.cursor-cloud",
                key="skip_reviewer_request",
                source=source,
            ),
            default_cloud_target=default_cloud_target,
            worker_name=_require_str(
                cursor_cloud_section.get("worker_name", ""),
                section="user_preferences.cursor-cloud",
                key="worker_name",
                source=source,
            ),
            pool_name=_require_str(
                cursor_cloud_section.get("pool_name", ""),
                section="user_preferences.cursor-cloud",
                key="pool_name",
                source=source,
            ),
        ),
        claude=UserPrefsClaude(
            max_turns=_require_int(
                claude_section.get("max_turns", 0),
                section="user_preferences.claude",
                key="max_turns",
                source=source,
            )
        ),
        codex=UserPrefsCodex(sandbox=codex_sandbox),
        lark=UserPrefsLark(
            notify_on_completed=_require_bool(
                lark_section.get("notify_on_completed", True),
                section="user_preferences.lark",
                key="notify_on_completed",
                source=source,
            ),
            notify_on_failed=_require_bool(
                lark_section.get("notify_on_failed", True),
                section="user_preferences.lark",
                key="notify_on_failed",
                source=source,
            ),
            notify_on_canceled=_require_bool(
                lark_section.get("notify_on_canceled", True),
                section="user_preferences.lark",
                key="notify_on_canceled",
                source=source,
            ),
            notify_on_cancel_escalated=_require_bool(
                lark_section.get("notify_on_cancel_escalated", False),
                section="user_preferences.lark",
                key="notify_on_cancel_escalated",
                source=source,
            ),
            prompt_truncate=_require_int(
                lark_section.get("prompt_truncate", 200),
                section="user_preferences.lark",
                key="prompt_truncate",
                source=source,
            ),
        ),
        dispatch=UserPrefsDispatch(
            ambiguity_resolution=ambiguity_resolution,
            ask_dimensions=ask_dimensions,
        ),
        last_set_at=_require_iso_string(
            section.get("last_set_at", ""),
            section="user_preferences",
            key="last_set_at",
            source=source,
        ),
        last_set_by=_require_str(
            section.get("last_set_by", ""),
            section="user_preferences",
            key="last_set_by",
            source=source,
        ),
    )


def _persist_user_preferences_migration(
    raw: dict[str, Any],
    migrated_section: dict[str, Any],
    *,
    source: Path,
) -> None:
    """Write ``popolad.toml.v1.bak`` + migrated v2 TOML when loading a file."""
    if not source.exists():
        return
    try:
        import shutil

        import tomli_w

        backup = source.with_name(f"{source.name}.v1.bak")
        if not backup.exists():
            shutil.copy2(source, backup)
        updated = dict(raw)
        updated["user_preferences"] = migrated_section
        source.write_text(tomli_w.dumps(updated), encoding="utf-8", newline="\n")
    except OSError as exc:
        logger.warning(
            "failed to persist [user_preferences] v2 migration for %s: %s",
            source,
            exc,
        )


def user_preferences_to_toml_dict(
    config: UserPreferencesConfig,
) -> dict[str, Any]:
    """Return a TOML-serializable nested ``[user_preferences]`` dict."""
    return {
        "schema_version": 2,
        "routing": {
            "default_runtime": config.routing.default_runtime,
            "default_local_cli": config.routing.default_local_cli,
            "fallback_chain": list(config.routing.fallback_chain),
            "cloud_target_priority": list(config.routing.cloud_target_priority),
        },
        "defaults": {
            "wait_timeout_s": config.defaults.wait_timeout_s,
            "hitl_enabled": config.defaults.hitl_enabled,
            "follow_devola_flow": config.defaults.follow_devola_flow,
            "prompt_each_dispatch": config.defaults.prompt_each_dispatch,
        },
        "cursor": {
            "output_format": config.cursor.output_format,
            "cli_args": list(config.cursor.cli_args),
        },
        "cursor-cloud": {
            "model": config.cursor_cloud.model,
            "starting_ref": config.cursor_cloud.starting_ref,
            "auto_create_pr": config.cursor_cloud.auto_create_pr,
            "work_on_current_branch": config.cursor_cloud.work_on_current_branch,
            "skip_reviewer_request": config.cursor_cloud.skip_reviewer_request,
            "default_cloud_target": config.cursor_cloud.default_cloud_target,
            "worker_name": config.cursor_cloud.worker_name,
            "pool_name": config.cursor_cloud.pool_name,
        },
        "claude": {"max_turns": config.claude.max_turns},
        "codex": {"sandbox": config.codex.sandbox},
        "lark": {
            "notify_on_completed": config.lark.notify_on_completed,
            "notify_on_failed": config.lark.notify_on_failed,
            "notify_on_canceled": config.lark.notify_on_canceled,
            "notify_on_cancel_escalated": config.lark.notify_on_cancel_escalated,
            "prompt_truncate": config.lark.prompt_truncate,
        },
        "dispatch": {
            "ambiguity_resolution": config.dispatch.ambiguity_resolution,
            "ask_dimensions": list(config.dispatch.ask_dimensions),
        },
        "last_set_at": config.last_set_at,
        "last_set_by": config.last_set_by,
    }


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
        raise ValueError(f"[cloud] in {source} must be a table; got {type(cloud_top).__name__}")
    backoff_section = cloud_top.get("backoff", {})
    if not isinstance(backoff_section, dict):
        raise ValueError(
            f"[cloud.backoff] in {source} must be a table; got {type(backoff_section).__name__}"
        )

    _warn_unknown_keys("cloud.backoff", backoff_section, _CLOUD_BACKOFF_KNOWN_KEYS, source)

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
        raise ValueError(f"[cloud] in {source} must be a table; got {type(cloud_top).__name__}")
    relay_section = cloud_top.get("relay", {})
    if not isinstance(relay_section, dict):
        raise ValueError(
            f"[cloud.relay] in {source} must be a table; got {type(relay_section).__name__}"
        )

    _warn_unknown_keys("cloud.relay", relay_section, _CLOUD_RELAY_KNOWN_KEYS, source)

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
        raise ValueError(_CLOUD_RELAY_LOCK_ERROR_MESSAGES["require_confirm_allowlist_flag"])

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


def _load_cloud_busy_strategy(raw: dict[str, Any], *, source: Path) -> BusyStrategyConfig:
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
        raise ValueError(f"[cloud] in {source} must be a table; got {type(cloud_top).__name__}")
    busy_section = cloud_top.get("busy_strategy", {})
    if not isinstance(busy_section, dict):
        raise ValueError(
            f"[cloud.busy_strategy] in {source} must be a table; got {type(busy_section).__name__}"
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
    if mode_raw == "queue" and max_wait_int > 0 and poll_int > max_wait_int:
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
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
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
        logger.info("lark.supervisor.skipped reason=lark_target_open_id_unset")
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

    async def on_card_action(event: dict[str, Any], parsed: tuple[str, str]) -> None:
        hitl_id, option_id = parsed
        store = getattr(popolad, "hitl_store", None)
        if store is None:
            logger.debug(
                "lark.listener.card_action: hitl_store unwired; dropping hitl_id=%s option=%s",
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
                "lark.listener.card_action rejected mis-route hitl_id=%s sender=%s descriptor=%s",
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

    async def on_text_feedback(event: dict[str, Any], parsed: dict[str, str]) -> None:
        hitl_id = parsed.get("hitl_id", "")
        option_id = parsed.get("option_id", "")
        store = getattr(popolad, "hitl_store", None)
        if store is None:
            logger.debug(
                "lark.listener.text_feedback: hitl_store unwired; dropping hitl_id=%s option=%s",
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

    # v0.9.9 U2 (Q-V099-12): auto-source ``~/.popola/cursor_api_key.env`` so a
    # fresh ``popola dispatch`` shell after ``popola init --cursor-api-key VAL``
    # picks up the operator's secret without requiring a manual ``source``.
    # The helper is best-effort: an absent file or an existing
    # ``CURSOR_API_KEY`` env var are no-ops (env-var precedence wins);
    # malformed lines log a WARN and the daemon continues.
    if credentials.load_env_fallback_into_environ(logger=logger):
        logger.info("cursor_api_key.env auto-sourced into environ (v0.9.9 U2 fallback)")

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
