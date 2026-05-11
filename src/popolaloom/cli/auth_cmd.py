"""``popola auth`` subcommand group — secure credential management (v0.9.2+).

Provides the operator-facing surface for the credential resolver in
:mod:`popolaloom.credentials`:

* ``popola auth cursor set [--api-key VAL | --from-env | --validate]``
* ``popola auth cursor status [--json]``
* ``popola auth cursor clear``

Design rules (locked in v0.9.2 plan §"Secret Handling Invariants"):

1. Never print, log, or echo the raw API key. ``set`` reads from the
   ``--api-key`` CLI flag (already on the operator's argv — they typed
   it), from ``--from-env`` (which copies ``CURSOR_API_KEY`` into the
   keyring then suggests unsetting the env var), or from a stdin prompt
   that uses :func:`typer.prompt(hide_input=True)`. Raw values are
   never re-echoed back to stdout.
2. ``status`` surfaces only ``configured`` / ``source`` / ``backend_name``
   / ``fingerprint`` / ``keyring_available``. Fingerprint is the first
   12 hex chars of ``sha256(secret)`` — enough to disambiguate "is this
   the same key I just set?" without leaking entropy.
3. Failures are loud: missing keyring extra, OS backend rejecting the
   write, empty input, and ``--validate`` failures all exit non-zero
   with a remediation hint pointing at all three precedence slots
   (``popola auth cursor set`` / ``CURSOR_API_KEY`` env / cloud-only
   init wizard).
4. ``--validate`` round-trips through ``GET /v1/me`` to confirm the key
   is accepted by Cursor BEFORE persisting it. The validation request
   is a single round-trip with a short (10s) timeout; failures emit
   the redacted error message verbatim so operators can self-diagnose
   typo / revocation / plan_required scenarios.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import NoReturn

import click
import typer

from popolaloom import credentials as _credentials_mod
from popolaloom.credentials import (
    CURSOR_API_KEY_ENV,
    CredentialBackendError,
    compute_fingerprint,
    credential_status,
    delete_cursor_api_key,
    is_keyring_available,
    redact_in_text,
    resolve_cursor_api_key,
    store_cursor_api_key,
)

logger = logging.getLogger(__name__)

__all__ = ["app", "compute_fingerprint", "resolve_cursor_api_key"]


# ── exit codes (mirrors popola cloud cmd matrix) ─────────────────────────


_EXIT_OK: int = 0
_EXIT_INVALID_ARGS: int = 2
_EXIT_BACKEND_UNAVAILABLE: int = 3
"""Keyring extra missing OR OS backend refused. Distinct from auth-failure
so scripts can branch (install the extra vs. revoke a bad key)."""

_EXIT_AUTH_VALIDATE_FAILED: int = 77
"""--validate round-trip rejected by Cursor (matches `popola cloud runs`
exit 77 for 401/403)."""


app = typer.Typer(
    name="auth",
    help=(
        "Manage credentials used by PopolaLoom's cloud dispatch surfaces. "
        "Currently scopes to Cursor Cloud Agents (`popola auth cursor`)."
    ),
    no_args_is_help=True,
    add_completion=False,
)


cursor_app = typer.Typer(
    name="cursor",
    help=(
        "Cursor API key credential management. Stores the secret in the "
        "OS keyring (macOS Keychain / Windows Credential Manager / "
        "libsecret on Linux); the env var CURSOR_API_KEY remains the "
        "highest-precedence override for CI."
    ),
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(cursor_app, name="cursor")


# ── helpers ──────────────────────────────────────────────────────────────


def _fail_no_keyring(*, action: str) -> NoReturn:
    """Exit ``3`` with a remediation hint when the keyring extra is missing.

    v0.9.7 (closes ``feedback_for_v0.9.4.md`` line 1): the remediation
    points at ``./install.sh install --with-credentials`` rather than a
    raw ``pip install popolaloom[credentials]`` (per the workspace rule
    "popola 不使用 pip 修正安装方式" — fix the install method, do not
    surface bare pip commands to operators).
    """
    typer.echo(
        f"error: cannot {action} because the OS keyring backend is unavailable.",
        err=True,
    )
    typer.echo(
        "  - re-run the installer with the credentials extra: "
        "`./install.sh install --with-credentials` "
        "(or `./install.sh update --with-credentials` on existing installs)",
        err=True,
    )
    typer.echo(
        "  - or use the env var path: `export CURSOR_API_KEY=<key>` "
        "(also picked up from a 0o600 `.env`)",
        err=True,
    )
    typer.echo(
        "  - or scaffold a cloud-only project: "
        "`popola init --target=cloud-only`",
        err=True,
    )
    raise typer.Exit(code=_EXIT_BACKEND_UNAVAILABLE)


_ACCOUNT_CLASS_CHOICES: tuple[str, ...] = ("personal", "service-account", "unknown")
"""User-facing labels accepted by ``--account-class``.

Mirrors the case-insensitive whitelist enforced by
:func:`popolaloom.credentials.store_account_class`. ``service-account`` is
normalised to ``service_account`` (the on-disk + :class:`AccountClass`
member form) inside :func:`_normalize_account_class`.
"""


def _normalize_account_class(raw: str) -> str:
    """Validate + normalise a raw ``--account-class`` value (case-insensitive).

    Returns the canonical on-disk form (``personal`` /
    ``service_account`` / ``unknown``) suitable for forwarding to
    :func:`popolaloom.credentials.store_account_class`. Raises
    :class:`typer.BadParameter` (Click rejects with a non-zero exit and
    the standard "Invalid value" usage line) when ``raw`` is empty or
    outside the whitelist — surfaces the bad input loudly per the
    workspace No Silent Failures rule.
    """
    if raw is None or not raw.strip():
        raise typer.BadParameter(
            f"--account-class must be one of {{{', '.join(_ACCOUNT_CLASS_CHOICES)}}}"
        )
    lowered = raw.strip().lower()
    if lowered not in _ACCOUNT_CLASS_CHOICES:
        raise typer.BadParameter(
            f"invalid --account-class {raw!r}; "
            f"expected one of {{{', '.join(_ACCOUNT_CLASS_CHOICES)}}} (case-insensitive)"
        )
    return "service_account" if lowered == "service-account" else lowered


def _resolve_account_class_value(
    *,
    cli_value: str | None,
    no_prompt: bool,
) -> tuple[str, str]:
    """Return ``(normalized_value, user_facing_value)`` for the account-class field.

    Applies the v0.9.9 U1 contract:

    * When ``cli_value`` is supplied (operator passed
      ``--account-class=...``), normalise + return; never prompt.
    * When ``--no-prompt`` is set OR stdin is not a TTY (CI / piped
      input), default to ``"unknown"``; never prompt — keeps the
      keyring-write success path unchanged for headless environments.
    * Otherwise, prompt interactively; on
      :class:`typer.Abort` / EOF / Ctrl-C, default to ``"unknown"`` so
      the keyring-write still completes (the prompt is a routing-hint
      capture, not a hard prerequisite).

    Returns a 2-tuple of:
      * normalized_value: the on-disk form (``personal`` /
        ``service_account`` / ``unknown``); forwarded to
        :func:`popolaloom.credentials.store_account_class`.
      * user_facing_value: the value to echo in the
        ``Recorded account_class=...`` confirmation line; same as the
        normalized value (kept as a separate slot in case future
        revisions diverge them).
    """
    if cli_value is not None:
        normalized = _normalize_account_class(cli_value)
        return normalized, normalized

    stdin_is_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
    if no_prompt or not stdin_is_tty:
        return "unknown", "unknown"

    typer.echo(
        f"Account class? [{'/'.join(_ACCOUNT_CLASS_CHOICES)}] (default: unknown):"
    )
    try:
        raw = typer.prompt("> ", default="unknown", show_default=False)
    except (click.exceptions.Abort, typer.Abort, EOFError) as exc:
        logger.debug(
            "account-class prompt aborted (%s: %s); defaulting to 'unknown'",
            type(exc).__name__,
            exc,
        )
        return "unknown", "unknown"
    if raw is None or not str(raw).strip():
        return "unknown", "unknown"
    try:
        normalized = _normalize_account_class(str(raw))
    except typer.BadParameter as exc:
        typer.echo(
            f"  warning: {exc.format_message()}; recording account_class=unknown",
            err=True,
        )
        return "unknown", "unknown"
    return normalized, normalized


def _validate_api_key_with_cursor(api_key: str, *, timeout_s: float = 10.0) -> str | None:
    """Round-trip ``GET /v1/me`` to confirm Cursor accepts ``api_key``.

    Returns :data:`None` on success or a redacted error message on
    failure. The error message is suitable for printing to stderr —
    the literal API key is stripped via
    :func:`redact_in_text(candidates=(api_key,))`.

    Uses :class:`popolaloom.adapters.cursor_cloud.CloudCursorClient` so
    the auth pipeline (HTTP Basic with empty password) matches the
    dispatch path verbatim.
    """
    from popolaloom.adapters.cursor_cloud import (
        CloudCursorClient,
        CursorCloudError,
    )

    try:
        client = CloudCursorClient(api_key, timeout_s=timeout_s)
    except ValueError as exc:
        return redact_in_text(f"client construction failed: {exc}", candidates=(api_key,))

    try:
        # GET /v1/me is the documented "is my key alive?" endpoint per
        # Cursor's Cloud Agents OpenAPI spec; cheap (no agent state).
        response = client._request_json("GET", "/v1/me")
        if isinstance(response, dict):
            email = response.get("userEmail")
            if isinstance(email, str) and email:
                return None
        # Some accounts return a sparse payload — accept anything 2xx.
        return None
    except CursorCloudError as exc:
        return redact_in_text(
            f"validate failed: {type(exc).__name__}: {exc}",
            candidates=(api_key,),
        )
    except Exception as exc:  # noqa: BLE001 — defensive: report any networking failure
        return redact_in_text(
            f"validate failed: {type(exc).__name__}: {exc}",
            candidates=(api_key,),
        )
    finally:
        try:
            client.close()
        except Exception as exc:  # pragma: no cover — defensive only
            logger.debug("CloudCursorClient.close() raised: %s", exc)


# ── set verb ─────────────────────────────────────────────────────────────


@cursor_app.command("set")
def cmd_set(  # noqa: PLR0913 — explicit flag matrix is part of the contract
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help=(
            "Cursor API key value. Mutually exclusive with --from-env. "
            "When omitted, an interactive (hidden-input) prompt asks for it."
        ),
    ),
    from_env: bool = typer.Option(
        False,
        "--from-env",
        help=(
            f"Copy the current ${CURSOR_API_KEY_ENV} value into the keyring. "
            "Useful when migrating from the env-var path to the secure store."
        ),
    ),
    validate: bool = typer.Option(
        False,
        "--validate/--no-validate",
        help=(
            "Round-trip GET /v1/me to confirm Cursor accepts the key BEFORE "
            "persisting it. Default off — turn on when typing a fresh key."
        ),
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit a JSON status envelope on stdout instead of human text.",
    ),
    account_class: str | None = typer.Option(
        None,
        "--account-class",
        help=(
            "Declare the API-key class for the v0.9.9 worker-dispatch "
            "pre-flight gate (case-insensitive). One of "
            "{personal, service-account, unknown}. When omitted on an "
            "interactive terminal an inline prompt asks for the class; "
            "non-interactive runs (--no-prompt, piped stdin, or CI) "
            "default to 'unknown'. The value persists into "
            "$POPOLA_HOME/credentials.toml under [cursor].account_class "
            "and never travels alongside the secret itself."
        ),
    ),
    no_prompt: bool = typer.Option(
        False,
        "--no-prompt",
        help=(
            "Skip every interactive prompt (including the v0.9.9 "
            "account-class capture). When set without --account-class, "
            "account_class defaults to 'unknown'."
        ),
    ),
) -> None:
    """Persist a Cursor API key into the OS keyring.

    Precedence after a successful ``set`` (next dispatch will see):

    1. ``--api-key`` value passed to this invocation (``--override``
       inside the resolver — not used by the CLI directly).
    2. ``$CURSOR_API_KEY`` (still wins over keyring; export-then-set
       deliberately keeps the env override active until you ``unset``
       it).
    3. The freshly-stored keyring entry.
    """
    if api_key is not None and from_env:
        typer.echo("error: --api-key and --from-env are mutually exclusive", err=True)
        raise typer.Exit(code=_EXIT_INVALID_ARGS)

    raw: str | None = None
    if from_env:
        env_value = (
            __import__("os").environ.get(CURSOR_API_KEY_ENV, "").strip()
        )
        if not env_value:
            typer.echo(
                f"error: --from-env requires ${CURSOR_API_KEY_ENV} to be set",
                err=True,
            )
            raise typer.Exit(code=_EXIT_INVALID_ARGS)
        raw = env_value
    elif api_key is not None:
        raw = api_key.strip()
    else:
        # Interactive hidden-input prompt — never echoes back.
        raw = typer.prompt(
            "Cursor API key (will be stored in the OS keyring; input hidden)",
            hide_input=True,
            confirmation_prompt=False,
        )
        if raw is not None:
            raw = raw.strip()

    if not raw:
        typer.echo("error: empty API key — refusing to store", err=True)
        raise typer.Exit(code=_EXIT_INVALID_ARGS)

    if not is_keyring_available():
        _fail_no_keyring(action="store the Cursor API key")

    if validate:
        validation_error = _validate_api_key_with_cursor(raw)
        if validation_error is not None:
            typer.echo(f"error: {validation_error}", err=True)
            raise typer.Exit(code=_EXIT_AUTH_VALIDATE_FAILED)

    resolved_class, user_facing_class = _resolve_account_class_value(
        cli_value=account_class,
        no_prompt=no_prompt,
    )

    try:
        status = store_cursor_api_key(raw)
    except CredentialBackendError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=_EXIT_BACKEND_UNAVAILABLE) from exc
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=_EXIT_INVALID_ARGS) from exc

    try:
        _credentials_mod.store_account_class(resolved_class)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=_EXIT_INVALID_ARGS) from exc

    if json_out:
        payload = status.to_json_dict()
        payload["validated"] = validate
        payload["account_class"] = user_facing_class
        typer.echo(json.dumps(payload, sort_keys=True))
    else:
        typer.echo(
            "Cursor API key stored in the OS keyring "
            f"(backend: {status.backend_name}, fingerprint: {status.fingerprint})."
        )
        typer.echo(f"  Recorded account_class={user_facing_class}")
        if from_env:
            typer.echo(
                "  Tip: you can now `unset CURSOR_API_KEY` for this shell — "
                "the keyring entry will answer subsequent dispatches.",
            )
        if validate:
            typer.echo("  Validation: GET /v1/me succeeded.")

    raise typer.Exit(code=_EXIT_OK)


# ── status verb ──────────────────────────────────────────────────────────


@cursor_app.command("status")
def cmd_status(
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit a JSON status envelope on stdout instead of a Rich table.",
    ),
) -> None:
    """Print whether a Cursor API key is configured (without revealing it).

    Surfaces the precedence slot the resolver would use, the backend
    name, and a 12-char SHA-256 fingerprint. The raw value is NEVER
    rendered.
    """
    status = credential_status()
    if json_out:
        typer.echo(json.dumps(status.to_json_dict(), sort_keys=True))
        raise typer.Exit(code=_EXIT_OK)

    if status.configured:
        typer.echo("Cursor API key: configured")
        typer.echo(f"  source:           {status.source}")
        typer.echo(f"  backend:          {status.backend_name}")
        typer.echo(f"  fingerprint:      {status.fingerprint}")
        typer.echo(f"  keyring available: {status.keyring_available}")
    else:
        typer.echo("Cursor API key: NOT configured")
        typer.echo(f"  keyring available: {status.keyring_available}")
        if status.reason:
            typer.echo(f"  reason:           {status.reason}")
        typer.echo("")
        typer.echo("To configure, choose one:")
        typer.echo("  - `popola auth cursor set` (stores in OS keyring)")
        typer.echo(f"  - `export {CURSOR_API_KEY_ENV}=<key>` (ephemeral)")
        typer.echo(
            "  - `popola init --target=cloud-only` (scaffolds .env.example "
            "with the placeholder)"
        )
    raise typer.Exit(code=_EXIT_OK)


# ── clear verb ───────────────────────────────────────────────────────────


@cursor_app.command("clear")
def cmd_clear(
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the interactive confirmation prompt (CI-friendly).",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit a JSON envelope on stdout instead of human text.",
    ),
) -> None:
    """Remove the keyring-backed Cursor API key (env var is untouched).

    Idempotent: succeeds silently when no entry was present. Does NOT
    touch ``$CURSOR_API_KEY`` — that env var is owned by the operator's
    shell / CI and clearing it via PopolaLoom would surprise scripts.
    """
    if not yes:
        confirmed = typer.confirm(
            "Remove the Cursor API key from the OS keyring?",
            default=False,
        )
        if not confirmed:
            typer.echo("Aborted.", err=True)
            raise typer.Exit(code=_EXIT_OK)

    try:
        removed, status = delete_cursor_api_key()
    except CredentialBackendError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=_EXIT_BACKEND_UNAVAILABLE) from exc

    if json_out:
        payload = status.to_json_dict()
        payload["removed"] = removed
        typer.echo(json.dumps(payload, sort_keys=True))
    else:
        if removed:
            typer.echo("Cursor API key removed from the OS keyring.")
        else:
            typer.echo("No Cursor API key was stored in the OS keyring (no-op).")
        if status.configured and status.source == "env":
            typer.echo(
                f"  Note: ${CURSOR_API_KEY_ENV} is still set and will continue "
                "to authenticate cloud dispatch."
            )
    raise typer.Exit(code=_EXIT_OK)


# Re-exports kept for tests / downstream callers (see ``__all__`` at module top).
_REEXPORTS = (compute_fingerprint, resolve_cursor_api_key)
