"""``popola cloud`` subcommand group — v0.8.8 T2.4.1 (Q-C-1 偏离默认).

Cloud-agent (``cursor-cloud`` runtime) introspection verbs. The first
verb is ``runs`` — list a cloud task's run history. Future verbs
(``cloud agents list`` / ``cloud cancel`` / ...) extend this same sub-app
without further CLI churn (per ``runs-subcommand-spec.md`` §2.2).

Surface (v0.8.8):

- ``popola cloud runs <task_id> [--limit N | --cursor S | --json |
  --include-events]`` — wraps Cursor Cloud Agents API
  ``GET /v1/agents/{id}/runs`` and prints a 6-column Rich table by
  default, or a §4-shaped JSON document under ``--json``.

The verb makes **two** distinct calls per invocation (no caching layer
between them — each call is a fresh authoritative read per spec §7.1):

1. **Daemon-bound** ``GET /status/{task_id}`` (UDS) — resolve
   ``cursor_agent_id`` and validate ``runtime=cloud``.
2. **Cloud-direct** ``GET /v1/agents/{agent_id}/runs`` (Cursor REST) +
   one cached ``GET /v1/agents/{agent_id}`` for the model column.

Exit-code matrix (per ``runs-subcommand-spec.md`` §7 + DECISIONS.md
OQ-1 / OQ-2):

| Failure                              | Exit |
| ------------------------------------ | ---- |
| daemon-down (Step 1)                 | ``1``  |
| local-runtime task                   | ``1``  |
| ``CURSOR_API_KEY`` unset             | ``77`` |
| popola task missing                  | ``4``  |
| Cursor 404 ``agent_not_found``       | ``4``  |
| Cursor 401 / 403 auth                | ``77`` |
| Cursor 403 ``plan_required``         | ``78`` |
| Cursor 429 rate-limited              | ``75`` |
| Cursor 5xx                           | ``75`` |
| ``--limit <= 0`` invalid             | ``2``  |

DECISIONS.md cross-refs:

- OQ-1 — 404 → exit ``4`` (parallel with the local-side
  ``error: task not found``); the v0.8.6 catalog's exit ``100`` for
  ``CursorCloudNotFoundError`` is treated as a ``popola dispatch``-only
  legacy.
- OQ-2 — 401 / 403 → exit ``77`` (catalog-aligned; corrects the
  user-brief's ``75`` typo).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import httpx
import typer
from rich.console import Console
from rich.table import Table

from popolaloom.adapters.cursor_cloud import (
    CloudCursorClient,
    CursorCloudAuthError,
    CursorCloudError,
    CursorCloudFeatureUnavailableError,
    CursorCloudNotFoundError,
    CursorCloudPlanRequiredError,
    CursorCloudRateLimitError,
)

logger = logging.getLogger(__name__)

__all__ = ["app"]


# ── exit code constants (per spec §7 + DECISIONS.md OQ-1 / OQ-2) ─────────


_EXIT_DAEMON_DOWN: int = 1
"""Daemon-down OR local-runtime-task path; mirrors ``_render_connect_error``."""

_EXIT_INVALID_ARGS: int = 2
"""Typer's reserved exit code for invalid CLI args (e.g. ``--limit 0``)."""

_EXIT_TASK_NOT_FOUND: int = 4
"""popola task missing OR Cursor 404 ``agent_not_found`` (DECISIONS.md OQ-1)."""

_EXIT_CLOUD_API_ERROR: int = 75
"""Cursor 429 / 5xx (rate-limited / overloaded / queue-timeout)."""

_EXIT_CLOUD_AUTH_ERROR: int = 77
"""Cursor 401 / 403 auth (DECISIONS.md OQ-2 — corrects user-brief's 75 typo)."""

_EXIT_CLOUD_FEATURE_UNAVAILABLE: int = 78
"""Cursor 403 ``plan_required`` / ``feature_unavailable``."""


# ── default constants ────────────────────────────────────────────────────


_DEFAULT_LIMIT: int = 20
"""Default page size — matches Cursor REST default per spec §5.1 / §1.1."""

_MAX_LIMIT: int = 100
"""Hard cap from Cursor REST docs — values >100 are clamped + WARN logged."""

_RUN_ID_TRUNCATION: int = 16
"""Number of chars kept in the table-rendered ``run_id`` (full id in --json)."""

_TERMINAL_RUN_STATES: frozenset[str] = frozenset({
    "finished",
    "cancelled",
    "expired",
    "error",
})
"""Cursor ``RunStatus`` values where ``wall_clock`` is computed from
``updatedAt - createdAt`` (i.e. the run is no longer ticking). Per spec
§3.2 every other state gets ``now - createdAt`` + a trailing ``…``."""


_TABLE_RENDER_WIDTH: int = 200
"""Force a generous render width so the 6-column table preserves the
17-char truncated ``run_id`` (16 + ``…``) and the verbatim ISO-8601
``created_at`` column. Rich's default 80-col width otherwise secondary-
truncates each cell, which breaks the spec §3.1 widths and the AC (b)
literal-string check in ``tests/cli/test_cloud_runs.py``. Real terminals
wider than 200 fall through unaffected; narrower terminals see horizontal
wrap, which is acceptable for the rare 6-column case."""


_console_out = Console(width=_TABLE_RENDER_WIDTH)


app = typer.Typer(
    name="cloud",
    help="Cloud-agent (cursor-cloud runtime) introspection verbs.",
    no_args_is_help=True,
    add_completion=False,
)


# v0.9.1+ — register the self-hosted worker subcommand group under
# ``popola cloud worker``.  Imported lazily inside the registration
# helper so an ``ImportError`` in the worker module surfaces as a clear
# CLI error (No Silent Failures) instead of a top-level import crash.
def _register_worker_subapp() -> None:
    """Attach :data:`cloud_worker_cmd.app` as ``popola cloud worker``.

    Lives in a helper so unit tests can import :mod:`cloud_cmd` without
    triggering the side-effect of building the Typer subapp tree
    (matters because ``test_cloud_runs.py`` imports the module before
    monkeypatching its indirection points).
    """
    from popolaloom.cli import cloud_worker_cmd

    app.add_typer(
        cloud_worker_cmd.app,
        name="worker",
        help=(
            "Self-hosted Cursor worker helpers (debug / start / status / handoff). "
            "Wraps `agent worker` for the My Machines + Self-Hosted Pool flows."
        ),
    )


_register_worker_subapp()


# ── transport (daemon UDS) ────────────────────────────────────────────────


def _socket_path() -> Path:
    """Resolve popolad UDS path: ``$POPOLA_HOME/popolad.sock`` or default.

    Mirrors :func:`popolaloom.cli.main._socket_path` so tests overriding
    ``$POPOLA_HOME`` see the same view as the rest of the CLI.
    """
    home = os.environ.get("POPOLA_HOME")
    base = Path(home).expanduser().resolve() if home else Path.home() / ".popola"
    return base / "popolad.sock"


def _make_sync_client(socket_path: Path | None = None) -> httpx.Client:
    """Construct an httpx.Client bound to the popolad UDS.

    Tests may monkeypatch this to inject a transport-mocked client; we
    keep it module-level (not imported from ``cli/main.py``) to avoid the
    cyclic-import dance in ``_register_subcommand_groups``.
    """
    sock = socket_path or _socket_path()
    transport = httpx.HTTPTransport(uds=str(sock))
    return httpx.Client(
        transport=transport,
        base_url="http://popolad",
        timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=10.0),
    )


def _render_connect_error(exc: httpx.HTTPError) -> NoReturn:
    """Print friendly daemon-down message + exit 1 (No Silent Failures).

    Mirrors :func:`popolaloom.cli.main._render_connect_error` verbatim so
    operators see the same wording regardless of which subcommand hit it.
    Typed as :data:`NoReturn` so callers don't need defensive
    ``raise AssertionError("unreachable")`` plumbing after the call.
    """
    typer.echo(
        "error: popolad not running, run `popola popolad start` to start it",
        err=True,
    )
    logger.debug("daemon connect error: %r", exc)
    raise typer.Exit(code=_EXIT_DAEMON_DOWN)


# ── cloud client helper (override-able for tests) ────────────────────────


def _build_cloud_client(api_key: str) -> CloudCursorClient:
    """Construct a :class:`CloudCursorClient` for the runs lookup.

    Factored out so :class:`tests.cli.test_cloud_runs` can monkeypatch
    a transport-mocked variant without touching the real network. The
    default path mints a fresh client from ``CURSOR_API_KEY``.
    """
    return CloudCursorClient(api_key)


# ── runs (T2.4.1) ────────────────────────────────────────────────────────


@app.command(name="runs")
def runs(
    task_id: str = typer.Argument(
        ...,
        help="Task identifier returned by `popola dispatch --cli=cursor-cloud`.",
    ),
    limit: int = typer.Option(
        _DEFAULT_LIMIT,
        "--limit",
        help="Max rows per page. Default 20, max 100.",
    ),
    cursor: str | None = typer.Option(
        None,
        "--cursor",
        help="Pagination cursor from a previous page.",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON instead of a Rich table.",
    ),
    include_events: bool = typer.Option(
        False,
        "--include-events",
        help="Add events_summary per row (slower; 1 extra round-trip).",
    ),
) -> None:
    """List all cloud-agent runs for a popola cloud task, newest first.

    Wraps GET /v1/agents/{id}/runs on Cursor Cloud Agents API. Requires
    CURSOR_API_KEY in the environment (same as `popola dispatch
    --cli=cursor-cloud`).

    TASK_ID must be a popola task whose runtime=cloud (use `popola list`
    to find one). Local-runtime tasks fail with exit 1.
    """
    # Step 0 — validate flags BEFORE any network call (fail fast).
    if limit <= 0:
        typer.echo(
            f"error: --limit must be > 0 (got {limit})",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)

    # Spec §5.1: clamp values >100 with a stderr WARN per No-Silent-Failures.
    effective_limit = limit
    if effective_limit > _MAX_LIMIT:
        typer.echo(
            f"warning: --limit {limit} exceeds Cursor API max ({_MAX_LIMIT}); "
            f"clamped to {_MAX_LIMIT}.",
            err=True,
        )
        effective_limit = _MAX_LIMIT

    # Step 1 — fail fast on missing credentials (avoid a daemon round-trip).
    # v0.9.2: route through the resolver so OS keyring storage answers
    # in addition to the historical CURSOR_API_KEY env path.
    from popolaloom.credentials import resolve_cursor_api_key

    api_key = resolve_cursor_api_key()
    if not api_key:
        typer.echo(
            "error: no Cursor API key configured for 'popola cloud runs' "
            "(set CURSOR_API_KEY env or run `popola auth cursor set`)",
            err=True,
        )
        raise typer.Exit(code=_EXIT_CLOUD_AUTH_ERROR)

    # Step 2 — daemon-bound GET /status/{task_id} → cursor_agent_id + runtime.
    agent_id = _resolve_agent_id_from_daemon(task_id)

    # Step 3 — cloud-direct GET /v1/agents/{id}/runs.
    body, model_id = _fetch_cloud_runs(
        api_key=api_key,
        agent_id=agent_id,
        limit=effective_limit,
        cursor=cursor,
        include_events=include_events,
    )

    # Step 4 — render.
    if json_out:
        _emit_json(
            task_id=task_id,
            agent_id=agent_id,
            body=body,
            model_id=model_id,
        )
    else:
        _emit_table(
            task_id=task_id,
            agent_id=agent_id,
            body=body,
            model_id=model_id,
        )


# ── step 2: daemon-bound status lookup ───────────────────────────────────


def _resolve_agent_id_from_daemon(task_id: str) -> str:
    """Daemon-bound ``GET /status/{task_id}`` → ``cursor_agent_id``.

    Per spec §6.1 + §7.1 step 1, this is the only daemon-side
    interaction; everything past this point is cloud-direct REST.

    Failure routing (per spec §7):

    - Connection error → :func:`_render_connect_error` (exit 1).
    - HTTP 404 → ``error: task not found: <task_id>`` (exit 4).
    - Non-200 / non-404 → exit 1 (same as ``popola status``).
    - ``runtime != "cloud"`` → exit 1 (``error: not a cloud task; ...``).
    - ``cursor_agent_id`` missing → exit 4 (the dispatch may not yet
      have hydrated; user should retry shortly).
    """
    try:
        with _make_sync_client() as client:
            response = client.get(f"/status/{task_id}")
    except httpx.ConnectError as exc:
        _render_connect_error(exc)

    if response.status_code == 404:
        typer.echo(f"error: task not found: {task_id}", err=True)
        raise typer.Exit(code=_EXIT_TASK_NOT_FOUND)
    if response.status_code != 200:
        typer.echo(
            f"error: status unexpected {response.status_code}: {response.text}",
            err=True,
        )
        raise typer.Exit(code=_EXIT_DAEMON_DOWN)

    info = response.json() if response.content else {}
    if not isinstance(info, dict):
        typer.echo(
            f"error: status response was not a JSON object: {response.text}",
            err=True,
        )
        raise typer.Exit(code=_EXIT_DAEMON_DOWN)

    runtime = info.get("runtime")
    if runtime != "cloud":
        typer.echo(
            "error: not a cloud task; use 'popola list' to find a cloud task",
            err=True,
        )
        raise typer.Exit(code=_EXIT_DAEMON_DOWN)

    agent_id_raw = info.get("cursor_agent_id")
    if not isinstance(agent_id_raw, str) or not agent_id_raw:
        # Per spec §6.1: missing ``cursor_agent_id`` is exit 4 with a
        # hint to retry once the dispatch has hydrated the agent id.
        typer.echo(
            f"error: task not found: {task_id} "
            "(cursor_agent_id not yet populated; retry once dispatch has hydrated)",
            err=True,
        )
        raise typer.Exit(code=_EXIT_TASK_NOT_FOUND)
    return agent_id_raw


# ── step 3: cloud-direct GET /v1/agents/{id}/runs ────────────────────────


def _fetch_cloud_runs(
    *,
    api_key: str,
    agent_id: str,
    limit: int,
    cursor: str | None,
    include_events: bool,
) -> tuple[dict[str, Any], str | None]:
    """Cloud-direct ``GET /v1/agents/{agent_id}/runs`` + ``GET /v1/agents/{id}``.

    Maps the Cursor REST error matrix (§7) onto popola exit codes via
    :func:`_handle_cloud_error` and returns ``(body, model_id)`` where
    ``body`` is the verbatim Cursor JSON (``items``/``nextCursor``) and
    ``model_id`` is the parent agent's request-time model id (or
    ``None`` on lookup miss).

    When ``include_events`` is ``True`` we mutate ``body["items"][i]`` to
    inject an ``events_summary`` dict per row (best-effort; per-row
    failure → ``null`` + stderr WARN, No-Silent-Failures per spec §4.2).
    """
    client = _build_cloud_client(api_key)
    try:
        try:
            body = client.list_runs(
                agent_id,
                limit=limit,
                cursor=cursor,
            )
        except CursorCloudError as exc:
            _handle_cloud_error(exc, agent_id=agent_id)

        model_id = _resolve_agent_model(client, agent_id)

        if include_events:
            _inject_events_summary(client, agent_id, body)

        return body, model_id
    finally:
        try:
            client.close()
        except Exception as exc:  # noqa: BLE001 — close failure is non-fatal
            logger.debug("cloud client.close() failed: %s", exc)


def _resolve_agent_model(client: CloudCursorClient, agent_id: str) -> str | None:
    """1-cached ``GET /v1/agents/{id}`` → model id (``None`` on miss).

    Per spec §3.1 column 6: the parent agent's request-time model is the
    fallback we surface in the table (``-`` when this lookup fails). We
    swallow any error here (the table column is informational) but log
    at ``WARNING`` for No-Silent-Failures discipline.
    """
    try:
        agent_body = client.get_agent(agent_id)
    except CursorCloudError as exc:
        logger.warning(
            "cursor-cloud get_agent(%s) failed for model fallback: %s",
            agent_id,
            exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — non-Cursor errors here are non-fatal
        logger.warning(
            "cursor-cloud get_agent(%s) raised %s: %s",
            agent_id,
            type(exc).__name__,
            exc,
        )
        return None

    if not isinstance(agent_body, dict):
        return None
    model_obj = agent_body.get("model")
    if isinstance(model_obj, str) and model_obj:
        return model_obj
    if isinstance(model_obj, dict):
        # Cursor REST returns ``model: {"id": "..."}`` on the agent body.
        nested = model_obj.get("id")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _inject_events_summary(
    client: CloudCursorClient,
    agent_id: str,
    body: dict[str, Any],
) -> None:
    """For each row, fetch ``GET /runs/{runId}`` + assemble events_summary.

    Spec §4.2: per-row failure degrades to ``events_summary = None`` plus
    a stderr WARN (No-Silent-Failures). The table row still renders.
    """
    items = body.get("items")
    if not isinstance(items, list):
        return
    for row in items:
        if not isinstance(row, dict):
            continue
        run_id_obj = row.get("id")
        if not isinstance(run_id_obj, str) or not run_id_obj:
            row["events_summary"] = None
            continue
        try:
            run_body = client.get_run(agent_id, run_id_obj)
        except CursorCloudError as exc:
            typer.echo(
                f"warning: failed to fetch events_summary for run {run_id_obj}: "
                f"{type(exc).__name__}: {exc}",
                err=True,
            )
            row["events_summary"] = None
            continue
        except Exception as exc:  # noqa: BLE001 — best-effort per-row degrade
            typer.echo(
                f"warning: unexpected error fetching events_summary for run "
                f"{run_id_obj}: {type(exc).__name__}: {exc}",
                err=True,
            )
            row["events_summary"] = None
            continue
        row["events_summary"] = _summarise_run_events(run_body)


def _summarise_run_events(run_body: Any) -> dict[str, Any] | None:
    """Compress a ``GET /runs/{runId}`` response into the spec §4.2 shape.

    The Cursor REST per-run body is loosely shaped; we extract the
    fields that are documented in v0.8.8 and fill the rest with safe
    defaults. Returns ``None`` only when the body is unusable.
    """
    if not isinstance(run_body, dict):
        return None

    events = run_body.get("events")
    if isinstance(events, list):
        tool_call_count = 0
        assistant_message_count = 0
        had_error = False
        first_event_at: str | None = None
        last_event_at: str | None = None
        for ev in events:
            if not isinstance(ev, dict):
                continue
            ev_type = ev.get("type") or ev.get("kind")
            if isinstance(ev_type, str):
                lowered = ev_type.lower()
                if "tool" in lowered:
                    tool_call_count += 1
                elif "assistant" in lowered or "message" in lowered:
                    assistant_message_count += 1
                if "error" in lowered:
                    had_error = True
            timestamp = (
                ev.get("createdAt")
                or ev.get("timestamp")
                or ev.get("time")
            )
            if isinstance(timestamp, str) and timestamp:
                if first_event_at is None or timestamp < first_event_at:
                    first_event_at = timestamp
                if last_event_at is None or timestamp > last_event_at:
                    last_event_at = timestamp
        return {
            "tool_call_count": tool_call_count,
            "assistant_message_count": assistant_message_count,
            "had_error": had_error,
            "first_event_at": first_event_at,
            "last_event_at": last_event_at,
        }

    # Fallback when no per-event list is exposed: surface the run-level
    # status as ``had_error`` only, with zero counts and no timestamps.
    status = run_body.get("status")
    had_error = isinstance(status, str) and status.upper() in {"ERROR", "FAILED"}
    return {
        "tool_call_count": 0,
        "assistant_message_count": 0,
        "had_error": had_error,
        "first_event_at": run_body.get("createdAt"),
        "last_event_at": run_body.get("updatedAt"),
    }


# ── error matrix (spec §7 + DECISIONS.md OQ-1 / OQ-2) ────────────────────


def _handle_cloud_error(exc: CursorCloudError, *, agent_id: str) -> NoReturn:
    """Map a :class:`CursorCloudError` onto a popola exit code + message.

    Always raises :class:`typer.Exit` (the function is typed
    :data:`NoReturn` so callers don't need
    ``raise AssertionError("unreachable")`` plumbing afterwards).

    Routing per spec §7 + DECISIONS.md OQ-1 / OQ-2 (which corrects the
    user-brief's ``75`` typo for auth → ``77`` to align with the v0.8.6
    catalog):

    - 404 → exit ``4`` (DECISIONS.md OQ-1: parallel with local-side
      ``error: task not found``); the v0.8.6 catalog's ``cli_exit=100``
      is treated as a ``popola dispatch``-only legacy.
    - 401 / 403 (auth subclasses) → exit ``77`` (DECISIONS.md OQ-2).
    - 403 ``plan_required`` / ``feature_unavailable`` → exit ``78``.
    - 429 / 5xx → exit ``75`` + observed ``Retry-After`` from the
      catalog hint (the catalog text already mentions Retry-After
      semantics; we additionally surface the exception's status code).
    - Anything else → exit ``75`` (treat as cloud-API failure).
    """
    # 404 — agent gone (v0.8.8 user-locked exit 4; see DECISIONS.md OQ-1
    # + runs-subcommand-spec.md §7 footnote 2).
    if isinstance(exc, CursorCloudNotFoundError):
        typer.echo(
            f"error: cursor agent not found (may have been deleted): {agent_id}",
            err=True,
        )
        _emit_bilingual_hint(exc)
        raise typer.Exit(code=_EXIT_TASK_NOT_FOUND)

    # 403 plan_required / feature_unavailable — exit 78.
    if isinstance(exc, (CursorCloudPlanRequiredError, CursorCloudFeatureUnavailableError)):
        typer.echo(f"error: cloud feature unavailable: {exc}", err=True)
        _emit_bilingual_hint(exc)
        raise typer.Exit(code=_EXIT_CLOUD_FEATURE_UNAVAILABLE)

    # 401 / 403 auth — exit 77 (catalog-aligned per DECISIONS.md OQ-2).
    if isinstance(exc, CursorCloudAuthError):
        typer.echo(f"error: cursor API auth failed: {exc}", err=True)
        _emit_bilingual_hint(exc)
        raise typer.Exit(code=_EXIT_CLOUD_AUTH_ERROR)

    # 429 — exit 75 + observed Retry-After (from catalog hint).
    if isinstance(exc, CursorCloudRateLimitError):
        typer.echo(f"error: cursor API rate-limited: {exc}", err=True)
        # The catalog hint mentions the ``Retry-After`` header; surfacing
        # the catalog text gives the user the actionable 60s default
        # AND the docs link.
        _emit_bilingual_hint(exc)
        raise typer.Exit(code=_EXIT_CLOUD_API_ERROR)

    # 5xx + everything else with cli_exit=75 fall here. Use the
    # exception's own ``cli_exit`` when set so spec §7 1:1 mappings
    # (e.g. RepoAllowlistError exit 78) propagate naturally.
    typer.echo(f"error: cursor API error: {exc}", err=True)
    _emit_bilingual_hint(exc)
    cli_exit = getattr(exc, "cli_exit", _EXIT_CLOUD_API_ERROR)
    if not isinstance(cli_exit, int) or cli_exit < 1:
        cli_exit = _EXIT_CLOUD_API_ERROR
    raise typer.Exit(code=cli_exit)


def _emit_bilingual_hint(exc: CursorCloudError) -> None:
    """Print the catalog's ``hint_zh`` / ``hint_en`` to stderr."""
    if getattr(exc, "hint_zh", None):
        typer.echo(f"hint (zh): {exc.hint_zh}", err=True)
    if getattr(exc, "hint_en", None):
        typer.echo(f"hint (en): {exc.hint_en}", err=True)


# ── render: --json (spec §4.1) ───────────────────────────────────────────


def _emit_json(
    *,
    task_id: str,
    agent_id: str,
    body: dict[str, Any],
    model_id: str | None,
) -> None:
    """Emit machine-readable JSON per ``runs-subcommand-spec.md`` §4.1.

    Schema (validated by ``tests/cli/fixtures/cloud_runs_v1.json`` —
    AC (e)):

    .. code-block:: json

        {
          "task_id": "...", "agent_id": "...",
          "runs": [{"run_id": "...", "run_index": int, "state": "lower",
                     "created_at": "ISO", "updated_at": "ISO",
                     "wall_clock_s": float, "model": "id" | null,
                     "events_summary": dict | null}],
          "next_cursor": str | null,
          "has_more": bool
        }
    """
    items_raw = body.get("items")
    items: list[dict[str, Any]] = items_raw if isinstance(items_raw, list) else []
    next_cursor = body.get("nextCursor")
    if not isinstance(next_cursor, str):
        next_cursor = None

    runs_out = _build_runs_table(items, model_id=model_id)

    payload: dict[str, Any] = {
        "task_id": task_id,
        "agent_id": agent_id,
        "runs": runs_out,
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
    }
    typer.echo(json.dumps(payload, ensure_ascii=False))


def _str_field(raw: dict[str, Any], key: str) -> str:
    """Return ``raw[key]`` when it is a non-empty ``str``; else ``""``.

    Centralised here so :func:`_build_runs_table` can pull every cell into
    a typed ``str`` local in one line. Without this helper, mypy rejects
    inline ``raw.get(K) if isinstance(raw.get(K), str) else ""`` because the
    second ``raw.get(K)`` returns ``Any | None`` (mypy doesn't propagate
    the isinstance narrowing across two separate ``.get()`` calls).
    """
    value = raw.get(key)
    if isinstance(value, str):
        return value
    return ""


def _build_runs_table(
    items: list[dict[str, Any]],
    *,
    model_id: str | None,
) -> list[dict[str, Any]]:
    """Build the ``runs[]`` array for both the JSON path and the table.

    Derives ``run_index`` (newest = highest, per spec §3.1 column 2 +
    DECISIONS.md OQ-3 anti-stale stance), ``wall_clock_s`` (terminal:
    ``updatedAt - createdAt``; live: ``now - createdAt``), and folds in
    ``model_id`` so the caller can render the table (which also wants
    truncated ``run_id``) directly off the same row dicts.
    """
    out: list[dict[str, Any]] = []
    n = len(items)
    for idx, raw in enumerate(items):
        if not isinstance(raw, dict):
            continue
        # Pull every cell into a local ``str`` first so the rest of the
        # loop (and the helpers it calls) does not have to re-narrow
        # ``raw.get(...)`` returns. mypy was rejecting the inline
        # ``raw.get(K) if isinstance(...) else ""`` form because the
        # ``raw.get()`` call there returns ``Any | None`` even after the
        # isinstance check on a separate ``raw.get(K)`` call.
        run_id = _str_field(raw, "id")
        status = _str_field(raw, "status")
        created_at = _str_field(raw, "createdAt")
        updated_at = _str_field(raw, "updatedAt")
        events_summary_raw = raw.get("events_summary")
        events_summary: dict[str, Any] | None = (
            events_summary_raw if isinstance(events_summary_raw, dict) else None
        )

        # newest=highest: items[0] (newest-first) gets index n-1; items[n-1] gets 0.
        run_index = (n - 1) - idx
        state = status.lower() if status else ""
        wall_clock_s, _ = _compute_wall_clock(created_at, updated_at, state)

        out.append({
            "run_id": run_id,
            "run_index": run_index,
            "state": state,
            "created_at": created_at,
            "updated_at": updated_at,
            "wall_clock_s": wall_clock_s,
            "model": model_id,
            "events_summary": events_summary,
        })
    return out


def _compute_wall_clock(
    created_at: str,
    updated_at: str,
    state: str,
) -> tuple[float, bool]:
    """Return ``(wall_clock_s, is_live)`` for a run.

    Per spec §3.1 / §3.2:

    - Terminal state (``finished`` / ``cancelled`` / ``expired`` /
      ``error``) → ``updatedAt - createdAt``.
    - Otherwise → ``now - createdAt`` (the run is still ticking;
      table renderer adds a ``…`` suffix).

    Returns ``(0.0, False)`` when ``createdAt`` is unparsable so JSON
    output stays a valid number.
    """
    created_dt = _parse_iso8601(created_at)
    if created_dt is None:
        return 0.0, False
    is_terminal = state in _TERMINAL_RUN_STATES
    end_dt = (
        (_parse_iso8601(updated_at) or created_dt)
        if is_terminal
        else datetime.now(UTC)
    )
    delta = (end_dt - created_dt).total_seconds()
    return max(0.0, float(delta)), not is_terminal


def _parse_iso8601(raw: str) -> datetime | None:
    """Best-effort ISO-8601 parse → tz-aware :class:`datetime`.

    Handles the common Cursor REST shape ``"2026-04-13T18:30:00.000Z"``
    by swapping the trailing ``Z`` for ``+00:00`` so :func:`datetime.fromisoformat`
    (Python 3.11+ accepts both directly, but we run on 3.11+ everywhere)
    parses without raising.
    """
    if not raw:
        return None
    candidate = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# ── render: 6-column Rich table (spec §3.1) ──────────────────────────────


def _emit_table(
    *,
    task_id: str,
    agent_id: str,
    body: dict[str, Any],
    model_id: str | None,
) -> None:
    """Render the default 6-column Rich table per ``runs-subcommand-spec.md`` §3.1.

    Columns (in order): ``run_id`` (truncated to 16 chars + ``…``) /
    ``run_index`` (newest=highest) / ``state`` (lowercased) /
    ``created_at`` (verbatim ISO-8601) / ``wall_clock`` (HH:MM:SS or
    ``N.Ns``; live runs get a trailing ``…``) / ``model`` (parent agent
    fallback or ``-`` on miss).
    """
    items_raw = body.get("items")
    items: list[dict[str, Any]] = items_raw if isinstance(items_raw, list) else []
    next_cursor = body.get("nextCursor")
    if not isinstance(next_cursor, str):
        next_cursor = None

    rows = _build_runs_table(items, model_id=model_id)

    if not rows:
        if next_cursor is None:
            typer.echo(f"No runs for task {task_id}")
        else:
            typer.echo(f"No runs in this page for task {task_id}")
            _print_pagination_footer(task_id, next_cursor)
        return

    table = Table(
        title=f"Runs for {task_id} (agent {agent_id})",
        show_header=True,
        header_style="bold",
    )
    table.add_column("run_id")
    table.add_column("run_index", justify="right")
    table.add_column("state")
    table.add_column("created_at")
    table.add_column("wall_clock")
    table.add_column("model")

    for row in rows:
        run_id = row["run_id"]
        truncated = (
            run_id[:_RUN_ID_TRUNCATION] + "…"
            if isinstance(run_id, str) and len(run_id) > _RUN_ID_TRUNCATION
            else (run_id or "-")
        )
        is_live = row["state"] not in _TERMINAL_RUN_STATES
        wall_clock_str = _format_wall_clock(
            float(row["wall_clock_s"]),
            is_live=is_live,
        )
        model_str = row["model"] if isinstance(row["model"], str) and row["model"] else "-"

        table.add_row(
            truncated,
            str(row["run_index"]),
            row["state"] or "-",
            row["created_at"] or "-",
            wall_clock_str,
            model_str,
        )

    _console_out.print(table)

    if next_cursor is not None:
        _print_pagination_footer(task_id, next_cursor)


def _format_wall_clock(seconds: float, *, is_live: bool) -> str:
    """Format ``wall_clock_s`` per spec §3.1 column 5 + §3.2 live suffix.

    - ``< 60 s`` → ``"N.Ns"`` (1 decimal place).
    - ``>= 60 s`` → ``"HH:MM:SS"``.
    - ``is_live = True`` → suffix ``"…"`` to signal still-ticking.
    """
    if seconds < 0:
        seconds = 0.0
    if seconds < 60.0:
        base = f"{seconds:.1f}s"
    else:
        total_int = int(seconds)
        hours, rem = divmod(total_int, 3600)
        minutes, secs = divmod(rem, 60)
        base = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    if is_live:
        return base + "…"
    return base


def _print_pagination_footer(task_id: str, next_cursor: str) -> None:
    """Print the §3.4 pagination footer to stdout (suppressed in --json)."""
    typer.echo(
        f"... more available. To continue:\n"
        f"  popola cloud runs {task_id} --cursor={next_cursor}"
    )


# ── module-level test hook ───────────────────────────────────────────────
#
# Tests can monkeypatch ``_make_sync_client`` (daemon UDS) and
# ``_build_cloud_client`` (cloud REST) to inject :class:`httpx.MockTransport`
# instances; everything else is pure logic. The two indirection points
# above are the only seam needed by ``tests/cli/test_cloud_runs.py``.
