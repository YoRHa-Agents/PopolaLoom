"""Cursor API key credential resolver — secure storage + redaction (v0.9.2+).

This module is the single source of truth for resolving the Cursor Cloud
Agents REST API key in PopolaLoom. It replaces direct
``os.environ.get("CURSOR_API_KEY")`` reads scattered across the dispatch /
runs / relay / cancel / attach call sites with a typed resolver that
honors an explicit precedence chain and never persists the secret to a
plaintext repo file.

Precedence (highest first):

1. **Explicit override** — passed in by callers (tests, CLI ``--api-key``
   flags) via :class:`CredentialResolver` constructor or
   :func:`resolve_cursor_api_key(override=...)`. Tests use this to inject
   fixtures without touching real env vars or the OS keyring.

2. **Environment variable** — ``CURSOR_API_KEY`` (case-sensitive). This
   keeps backward compatibility with all v0.8.x docs / CI workflows /
   ``.env.example`` scaffolds and stays the recommended path for
   ephemeral / CI sessions.

3. **OS keyring backend** — when the ``keyring`` extra is installed AND
   the operator opted in via ``popola auth cursor set`` (or the
   ``popola init --target=cloud-only --configure-cursor-auth`` prompt),
   the secret lives in the OS keychain (macOS Keychain, Windows
   Credential Manager, libsecret on Linux/freedesktop, KWallet, etc.).

4. **Fallback file status slot** — ``popola auth cursor status`` can report
   a locked-down ``$POPOLA_HOME/cursor_api_key.env`` when env/keyring are
   empty. Runtime resolution still uses the daemon auto-source hook so the
   env slot remains the actual dispatch precedence winner.

5. **Missing** — return :data:`None`. Callers must produce an actionable
   error message that points at all three of the above paths
   (``popola auth cursor set`` / ``CURSOR_API_KEY`` / cloud-only init
   wizard).

Security invariants (per the v0.9.2 plan + workspace No-Silent-Failures rule):

* The literal API key value MUST NEVER appear in stdout, stderr,
  ``logging.*`` output, NDJSON event payloads, audit rows, or handoff
  envelopes. :func:`redact` / :func:`redact_in_text` are the canonical
  helpers for sanitising arbitrary text before logging.
* Status / introspection commands surface only ``configured: bool``,
  ``backend: str``, and a short SHA-256 prefix fingerprint; never the
  raw value, never a "last 4 chars" preview (which leaks ~16 bits of
  entropy and lets an attacker correlate keys across logs).
* When the keyring backend is unavailable (extra not installed, no
  secure backend on the OS, libsecret crashed), the resolver fails
  loudly with an actionable error rather than silently falling back to
  a plaintext file. The single allowed fallback is the ``CURSOR_API_KEY``
  env var, which the operator explicitly chose to set.

Backend metadata (non-secret) lives in ``$POPOLA_HOME/credentials.toml``
with mode ``0600``; only the backend name + last_set_at timestamp are
recorded so ``popola auth cursor status`` can answer "is a secret stored?"
without unlocking the keyring.

Threat model: this module assumes the local OS keyring is at least as
secure as the operator's login session. We do NOT defend against:

* A root-level attacker reading ``/proc/<pid>/environ`` (env var path).
* A malicious process running as the same user (the keyring is
  unlocked for the session — by design).
* Operators who copy-paste their key into chat tools (out of scope —
  see USER_GUIDE §"Webhook secret rotation" for the rotation policy).
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover — type-check-only imports
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "AccountClass",
    "CURSOR_API_KEY_ENV",
    "CredentialBackendError",
    "CredentialResolver",
    "CredentialStatus",
    "KEYRING_SERVICE",
    "KEYRING_USERNAME",
    "REDACTION_PLACEHOLDER",
    "compute_fingerprint",
    "fingerprint",
    "get_account_class",
    "is_keyring_available",
    "load_credential_metadata",
    "load_env_fallback_into_environ",
    "metadata_path",
    "redact",
    "redact_in_text",
    "resolve_cursor_api_key",
    "save_credential_metadata",
    "store_account_class",
    "store_cursor_api_key",
    "delete_cursor_api_key",
    "credential_status",
    "write_env_fallback",
]


# ── public constants ─────────────────────────────────────────────────────


CURSOR_API_KEY_ENV: Final[str] = "CURSOR_API_KEY"
"""Env var name read for precedence rule #2 (back-compat with v0.8.x)."""

KEYRING_SERVICE: Final[str] = "popolaloom.cursor"
"""OS keyring service identifier (per :pep:`8` reverse-DNS-ish convention).

Stable across PopolaLoom v0.9.x — changing this value would orphan
existing operator-stored secrets, so it is part of the v0.9.x
SemVer-stable surface (see :doc:`docs/API_STABILITY`).
"""

KEYRING_USERNAME: Final[str] = "default"
"""Per-service username slot. Single-tenant for now; v0.10.x may add
named profiles (e.g. ``personal`` vs ``service-account``)."""

REDACTION_PLACEHOLDER: Final[str] = "<REDACTED:CURSOR_API_KEY>"
"""String inserted in place of a leaked API key in logs / events."""

_METADATA_FILENAME: Final[str] = "credentials.toml"
_METADATA_FILE_MODE: Final[int] = 0o600
_ENV_FALLBACK_FILENAME: Final[str] = "cursor_api_key.env"
"""Filename for the v0.9.9 U2 0600 fallback file (Q-V099-11).

Stored at ``$POPOLA_HOME/cursor_api_key.env`` next to ``credentials.toml``
so operators inspecting their ``~/.popola`` directory see both the
metadata + the fallback secret slot in one place. The file is mode
0600 (owner read/write only) — the same mode as the metadata file.
"""

_ENV_FALLBACK_FILE_MODE: Final[int] = 0o600
"""Required mode for the v0.9.9 U2 fallback file (Q-V099-11). 0o600 = owner-only."""

_FINGERPRINT_LEN: Final[int] = 12
"""How many hex chars of the SHA-256 to expose in status output.

12 hex chars = 48 bits of entropy. Enough to disambiguate the operator's
"is the key I just set the same as the one stored?" question without
leaking a useful chunk of the key itself.
"""


_ACCOUNT_CLASS_KEY: Final[str] = "account_class"
"""Metadata key under the ``[cursor]`` table holding the operator's
declared API-key class (Q-V099-1; v0.9.9 F5 + U1 pre-flight gate).

Values are :class:`AccountClass` members serialised by their string
value (``"personal"`` / ``"service_account"`` / ``"unknown"``). Stored
in plaintext alongside ``backend`` / ``last_set_at`` because the field
is a non-secret routing hint — it tells
:func:`popolaloom.cli.cloud_worker_cmd.worker_dispatch_cmd` whether the
configured key is allowed to drive the self-hosted worker REST surface
(only ``service_account`` is per the Spike-0 SCHEMA_INVESTIGATION.md
verdict; ``personal`` and ``unknown`` are blocked at pre-flight)."""


# ── account class enum ──────────────────────────────────────────────────


class AccountClass(StrEnum):
    """Declared class of the operator's stored Cursor API key (v0.9.9+).

    Drives the F5 + U1 pre-flight gate in
    :func:`popolaloom.cli.cloud_worker_cmd.worker_dispatch_cmd`. The
    Spike-0 schema investigation
    (``.local/.agent/active/v0.9.9-worker-observability/SCHEMA_INVESTIGATION.md``)
    confirmed that Cursor REST has no documented schema (as of
    2026-05-10) for routing ``POST /v1/agents`` to a self-hosted worker
    under a personal key with Dashboard visibility — only
    service-account / Enterprise pool keys are accepted. The enum is
    additive and defaults to :data:`UNKNOWN` for backward compat with
    existing ``credentials.toml`` files that pre-date v0.9.9.

    Inherits from :class:`enum.StrEnum` (Python 3.11+) so
    ``AccountClass.PERSONAL`` interpolates as ``"personal"`` in log
    output and direct equality checks (``AccountClass.PERSONAL ==
    "personal"``) hold without an explicit ``.value`` lookup. Same
    pattern as :class:`popolaloom.cli.init_cmd.InitTarget` (ruff UP042
    canonicalisation).
    """

    PERSONAL = "personal"
    """Personal API key (Cursor Dashboard → Integrations → API key).

    Cannot reach the self-hosted-worker REST routing fields under the
    public 2026-05-10 schema; ``popola cloud worker dispatch`` refuses
    pre-flight and points the operator at the My-Machines chat-trigger
    workaround OR ``popola cloud worker handoff``."""

    SERVICE_ACCOUNT = "service_account"
    """Enterprise team service-account API key.

    Required for ``--pool`` workers AND for ``popola cloud worker
    dispatch`` per the Self-Hosted-Pool docs. The pre-flight gate lets
    this class through unchanged."""

    UNKNOWN = "unknown"
    """Operator did not declare a class at ``popola auth cursor set`` time.

    Backward-compat default for credentials.toml files that pre-date
    v0.9.9 (the field is additive). Treated as ``personal`` by the
    pre-flight gate (refused with the bilingual hint) — the operator
    must re-run
    ``popola auth cursor set --api-key VAL --account-class=...`` to
    declare their key class."""


# ── exceptions ───────────────────────────────────────────────────────────


class CredentialBackendError(RuntimeError):
    """Raised when a persistent backend operation cannot complete safely.

    Examples:

    * ``keyring`` extra not installed when ``store_cursor_api_key`` is called.
    * ``keyring.errors.KeyringError`` raised by the OS backend during
      ``set_password`` / ``get_password`` / ``delete_password``.
    * Metadata file permissions cannot be tightened to ``0600``.

    Per the workspace **No Silent Failures** rule, every backend hiccup
    surfaces this exception with a short, actionable ``str()`` so the CLI
    layer can render a clean operator-facing error.
    """


# ── data classes ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CredentialStatus:
    """Non-secret summary of the stored Cursor API key (for ``popola auth cursor status``).

    Attributes:
        configured: True iff a secret is reachable via either the env var
            (precedence #2) or the OS keyring (precedence #3). False iff
            both lookups returned None / empty.
        source: One of ``"env"`` / ``"keyring"`` / ``"override"`` /
            ``"none"``. Mirrors which precedence slot answered.
        backend_name: Human-readable backend label
            (e.g. ``"macOS Keychain"``, ``"Secret Service"``,
            ``"environment variable"``, ``"unset"``). Best-effort —
            falls back to ``"keyring"`` when the underlying library
            cannot identify the OS-specific backend.
        fingerprint: First :data:`_FINGERPRINT_LEN` hex chars of the
            SHA-256 of the resolved secret, or :data:`None` when no
            secret is configured. Stable across calls, suitable for
            "is this the same key?" checks without leaking entropy.
        keyring_available: True iff the optional ``keyring`` extra is
            importable AND a usable backend is registered. False forces
            the ``popola auth cursor set`` flow to fall back to the
            env-only path with a clear remediation hint.
        reason: Optional non-secret explanation when a lower-precedence
            fallback candidate exists but is refused.
    """

    configured: bool
    source: str
    backend_name: str
    fingerprint: str | None
    keyring_available: bool
    reason: str | None = None

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dict for ``popola auth cursor status --json``.

        Keys mirror the dataclass field names verbatim so ``--json``
        output is stable across v0.9.x.
        """
        return {
            "configured": self.configured,
            "source": self.source,
            "backend_name": self.backend_name,
            "fingerprint": self.fingerprint,
            "keyring_available": self.keyring_available,
            "reason": self.reason,
        }


# ── metadata file helpers ────────────────────────────────────────────────


def _popola_home() -> Path:
    """Return ``$POPOLA_HOME`` or ``~/.popola`` (matches CLI socket helpers).

    The metadata file lives next to ``popolad.sock`` / ``popolad.pid`` so
    operators inspecting their PopolaLoom state see all three artefacts
    in one directory.
    """
    home = os.environ.get("POPOLA_HOME")
    if home:
        return Path(home).expanduser().resolve()
    return Path.home() / ".popola"


def metadata_path() -> Path:
    """Return the path to the non-secret credentials metadata file.

    The file is ``$POPOLA_HOME/credentials.toml`` with mode ``0600``.
    Contents are intentionally bare (a single ``[cursor]`` table with
    ``backend`` and ``last_set_at`` keys); the API key value never
    touches this file.
    """
    return _popola_home() / _METADATA_FILENAME


def load_credential_metadata() -> dict[str, str]:
    """Read ``credentials.toml`` and return a flat ``{key: str}`` dict.

    Returns an empty dict when the file is absent (fresh install) or
    when parsing fails (treated as "not yet configured"; we never raise
    here because metadata is purely informational — the actual secret
    lookup goes through the keyring).
    """
    path = metadata_path()
    if not path.is_file():
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover — Python <3.11 fallback
        import tomli as tomllib  # type: ignore[no-redef, import-not-found]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "credentials metadata at %s could not be parsed (%s); treating as empty",
            path,
            exc,
        )
        return {}
    cursor_section = data.get("cursor") if isinstance(data, dict) else None
    if not isinstance(cursor_section, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in cursor_section.items()
        if isinstance(value, (str, int, float))
    }


def _env_fallback_path() -> Path:
    """Return the v0.9.9 U2 0600 fallback file path (Q-V099-11).

    Returns ``$POPOLA_HOME/cursor_api_key.env`` (sibling of
    ``credentials.toml``). The file is written by
    :func:`write_env_fallback` (when the keyring backend is unavailable
    and the operator passed ``popola init --cursor-api-key VAL``) and
    read by :func:`load_env_fallback_into_environ` at daemon startup so
    a fresh ``popola dispatch`` shell after init "just works" without
    requiring the operator to ``source`` the file by hand.

    The path is always returned (whether or not the file exists) so
    callers can use it in stdout messages + ``os.stat`` checks.
    """
    return _popola_home() / _ENV_FALLBACK_FILENAME


def write_env_fallback(raw_key: str) -> Path:
    """Atomically write the v0.9.9 U2 0600 fallback file (Q-V099-11).

    Writes ``export CURSOR_API_KEY=<raw_key>\\n`` into
    ``$POPOLA_HOME/cursor_api_key.env`` with mode ``0o600`` (owner-only)
    using ``os.open(..., O_WRONLY|O_CREAT|O_TRUNC, 0o600)`` so the file
    is *born* with the right permissions (avoids the race where a
    plaintext file briefly exists with the umask default before a
    follow-up ``chmod`` tightens it). ``O_TRUNC`` makes the call
    idempotent: re-running ``popola init --cursor-api-key VAL2`` after
    a previous ``--cursor-api-key VAL1`` replaces the file contents
    entirely instead of appending.

    The literal ``raw_key`` value never appears in stdout / stderr /
    log output; the caller is responsible for emitting an
    operator-facing line that names the file path + the ``source``
    command (per ``init_cmd._persist_cursor_api_key_noninteractive``
    branch in the same v0.9.9 patch).

    The parent directory is created with mode 0o700 when missing
    (mirrors :func:`save_credential_metadata`).

    Args:
        raw_key: the resolved Cursor API key (already stripped by
            :func:`init_cmd._resolve_cursor_api_key_input`).

    Returns:
        :class:`pathlib.Path`: the absolute path of the file that was
        written.

    Raises:
        OSError: if the file could not be opened or written, or if the
            post-write ``os.stat`` reveals a mode other than 0o600
            (defensive: a non-0o600 mode after our explicit
            ``os.open(..., 0o600)`` call would indicate the OS or
            file-system rejected the mode bits and is treated as a
            hard failure per workspace rule "No Silent Failures" —
            security-critical for a secret slot).
    """
    if not raw_key or not raw_key.strip():
        raise ValueError("cursor api_key must be a non-empty string")
    path = _env_fallback_path()
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Include ``export`` so operators can source the file directly in a shell.
    payload = f"export {CURSOR_API_KEY_ENV}={raw_key.strip()}\n".encode()
    fd = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        _ENV_FALLBACK_FILE_MODE,
    )
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    actual_mode = os.stat(path).st_mode & 0o777
    if actual_mode != _ENV_FALLBACK_FILE_MODE:
        raise OSError(
            f"cursor_api_key.env at {path} has unexpected mode "
            f"{actual_mode:#o}; expected {_ENV_FALLBACK_FILE_MODE:#o} "
            "(security-critical — refusing to leave a world/group-readable "
            "secret on disk)"
        )
    return path


def _display_fallback_path(path: Path) -> str:
    """Render fallback path compactly for non-secret status output."""
    try:
        relative = path.relative_to(Path.home())
    except ValueError:
        return str(path)
    return f"~/{relative.as_posix()}"


def _parse_env_fallback_value(
    contents: str,
    *,
    path: Path,
    log: logging.Logger,
) -> str | None:
    """Parse a fallback env file, preserving existing malformed-line warnings."""
    for lineno, raw_line in enumerate(contents.splitlines(), start=1):
        line = raw_line.strip()
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            log.warning(
                "malformed cursor_api_key.env at %s line %d: %r",
                path,
                lineno,
                raw_line,
            )
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or not value:
            log.warning(
                "malformed cursor_api_key.env at %s line %d: %r",
                path,
                lineno,
                raw_line,
            )
            continue
        if key != CURSOR_API_KEY_ENV:
            log.debug(
                "cursor_api_key.env at %s line %d sets %r; only %s is "
                "auto-loaded by the v0.9.9 daemon hook (skipping)",
                path,
                lineno,
                key,
                CURSOR_API_KEY_ENV,
            )
            continue
        return value
    return None


def load_env_fallback_into_environ(*, logger: logging.Logger | None = None) -> bool:
    """Load the v0.9.9 U2 0600 fallback file into ``os.environ`` (Q-V099-12).

    Called from the daemon bootstrap (``popolaloom.daemon.main.main``)
    early in startup so a fresh ``popola dispatch`` from any shell that
    runs through the daemon picks up the operator's init-time
    ``--cursor-api-key VAL`` without requiring a manual ``source`` of
    ``~/.popola/cursor_api_key.env``.

    Reads ``$POPOLA_HOME/cursor_api_key.env`` line-by-line and applies
    every line of shape ``CURSOR_API_KEY=<value>`` to ``os.environ``
    (single key for v0.9.9; the loop shape leaves room for future
    expansion to multiple env vars without API churn). Lines that
    don't match — including blank lines, ``#`` comments, and
    malformed key/value rows — are skipped at WARN level (per
    workspace rule "No Silent Failures": each rejection has an
    explicit log entry pointing at the file + line number + literal
    line text, but file *presence* is best-effort and never aborts
    daemon startup).

    Precedence rule (v0.9.9 Q-V099-12 lock): if ``CURSOR_API_KEY`` is
    already set in the environment when this function runs, the
    existing value WINS — the fallback file does NOT overwrite an
    explicit operator-set env var. This keeps the env-var precedence
    slot from :class:`CredentialResolver` consistent with the
    :func:`resolve_cursor_api_key` chain (#2 env, #3 keyring) — the
    fallback file plays the role of "auto-source on daemon startup",
    not "highest-precedence override".

    Args:
        logger: optional :class:`logging.Logger` for WARN messages on
            malformed lines. When ``None`` the module-level
            :data:`logger` is used.

    Returns:
        ``True`` iff a value was loaded into ``os.environ`` from the
        file; ``False`` when the file is absent OR every line was
        skipped OR the env var was already set (precedence-preserving
        no-op). Callers can use the return value as a smoke check
        ("was the daemon's auto-source helpful for this start?").
    """
    log = logger if logger is not None else globals()["logger"]
    path = _env_fallback_path()
    if not path.is_file():
        return False
    if os.environ.get(CURSOR_API_KEY_ENV, "").strip():
        log.debug(
            "cursor_api_key.env at %s present but %s already set in environ; "
            "env-var precedence wins (Q-V099-12)",
            path,
            CURSOR_API_KEY_ENV,
        )
        return False
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "could not read cursor_api_key.env at %s: %s; daemon continues",
            path,
            exc,
        )
        return False
    value = _parse_env_fallback_value(contents, path=path, log=log)
    if value is None:
        return False
    os.environ[CURSOR_API_KEY_ENV] = value
    return True


def save_credential_metadata(values: Mapping[str, str]) -> None:
    """Persist non-secret metadata to ``credentials.toml`` atomically.

    Writes the file with mode ``0600`` (owner read/write only) — defense
    in depth even though the file never holds the secret itself.
    Parent directory is created with mode ``0700`` when missing.

    Args:
        values: ``{backend, last_set_at}`` style flat dict; passed
            through into a single ``[cursor]`` TOML table. Non-string
            values are coerced via ``str()`` for safety.
    """
    path = metadata_path()
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    lines = ["# popola credentials metadata — non-secret. Do not commit.", "[cursor]"]
    for key in sorted(values):
        sanitized = str(values[key]).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key} = "{sanitized}"')
    contents = "\n".join(lines) + "\n"

    tmp_path = path.with_suffix(".toml.tmp")
    tmp_path.write_text(contents, encoding="utf-8")
    try:
        os.chmod(tmp_path, _METADATA_FILE_MODE)
    except OSError as exc:  # pragma: no cover — Windows / unusual FS
        logger.warning("could not chmod %s to 0600: %s", tmp_path, exc)
    os.replace(tmp_path, path)


# ── keyring backend (lazy import) ────────────────────────────────────────


def _import_keyring() -> object | None:
    """Return the ``keyring`` module if importable, else :data:`None`.

    Wrapped in a helper so tests can monkeypatch this single indirection
    point without poking at :mod:`sys.modules`. Returns the module itself
    (typed as ``object`` to avoid a hard import-time dependency on the
    upstream stub package).
    """
    try:
        # When the ``credentials`` extra is not installed (CI env),
        # ``keyring`` is absent and mypy raises ``import-not-found``;
        # the symmetric ``unused-ignore`` code handles the dev env that
        # DOES have keyring installed (otherwise mypy strict would
        # complain that the suppression is unused).
        import keyring  # type: ignore[import-not-found,unused-ignore]  # noqa: PLC0415 — lazy on purpose
    except ImportError:
        return None
    return keyring  # type: ignore[no-any-return,unused-ignore]


def is_keyring_available() -> bool:
    """True iff the ``keyring`` extra is importable AND has a backend.

    A "usable backend" means :func:`keyring.get_keyring` returns
    something other than :class:`keyring.backends.fail.Keyring` —
    the upstream sentinel for "no real backend was registered".
    """
    keyring_mod = _import_keyring()
    if keyring_mod is None:
        return False
    try:
        backend = keyring_mod.get_keyring()  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover — defensive only
        logger.debug("keyring.get_keyring() raised %s", exc)
        return False
    backend_module = type(backend).__module__
    backend_name = type(backend).__name__
    return not (
        backend_module.endswith(".fail")
        or (backend_name == "Keyring" and "fail" in backend_module)
    )


def _keyring_backend_name() -> str:
    """Return a human-readable label for the active keyring backend.

    Best-effort: ``keyring`` itself does not normalise this, so we
    inspect ``type(backend).__module__`` + ``__name__`` and apply a
    handful of known mappings. Unknown backends fall back to ``"keyring"``
    so status output stays stable.
    """
    keyring_mod = _import_keyring()
    if keyring_mod is None:
        return "unset"
    try:
        backend = keyring_mod.get_keyring()  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover — defensive only
        return "keyring"
    module = type(backend).__module__
    name = type(backend).__name__
    if "macOS" in module or "OS_X" in module or name == "Keyring" and "macOS" in module:
        return "macOS Keychain"
    if "Windows" in module or "Win32" in module:
        return "Windows Credential Manager"
    if "SecretService" in name or "secretservice" in module.lower():
        return "Secret Service"
    if "kwallet" in module.lower() or "KWallet" in name:
        return "KWallet"
    if "libsecret" in module.lower():
        return "libsecret"
    if module.endswith(".fail"):
        return "unset"
    return f"keyring ({module}.{name})"


def _keyring_get(service: str = KEYRING_SERVICE, username: str = KEYRING_USERNAME) -> str | None:
    """Look up a secret in the OS keyring; return :data:`None` on miss.

    Wraps ``keyring.get_password`` with a defensive ``try/except`` so an
    OS-level keyring error (e.g. transient libsecret D-Bus glitch) does
    not crash the daemon — it logs at WARNING and returns None, letting
    the resolver fall through to the next precedence slot.

    Empty string returns are normalised to :data:`None` (some backends
    return ``""`` for "absent" — equivalent to None for our precedence).
    """
    keyring_mod = _import_keyring()
    if keyring_mod is None:
        return None
    try:
        value = keyring_mod.get_password(service, username)  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover — backend-specific failure modes
        logger.warning("keyring.get_password(%s, %s) raised %s", service, username, exc)
        return None
    if not value:
        return None
    stripped = value.strip()
    return stripped or None


def _keyring_set(
    value: str,
    service: str = KEYRING_SERVICE,
    username: str = KEYRING_USERNAME,
) -> None:
    """Store ``value`` in the OS keyring; raise on backend failure.

    Raises :class:`CredentialBackendError` (not the upstream
    ``keyring.errors.KeyringError``) so the CLI layer can render a
    consistent error without conditionally importing keyring.
    """
    keyring_mod = _import_keyring()
    if keyring_mod is None:
        raise CredentialBackendError(
            "keyring extra is not installed; "
            "re-run `./install.sh install --with-credentials` "
            "(or `./install.sh update --with-credentials` on existing installs) "
            "to add it, or set `CURSOR_API_KEY` in the env / a 0o600 `.env` "
            "(precedence #2 fallback per credentials.py)"
        )
    try:
        keyring_mod.set_password(service, username, value)  # type: ignore[attr-defined]
    except Exception as exc:
        raise CredentialBackendError(
            f"OS keyring rejected secret store ({type(exc).__name__}: {exc}); "
            "consider exporting CURSOR_API_KEY instead"
        ) from exc


def _keyring_delete(service: str = KEYRING_SERVICE, username: str = KEYRING_USERNAME) -> bool:
    """Delete the keyring entry; return True iff something was removed.

    Returns False (NOT raising) when the entry is already absent — that
    is the desired idempotent behaviour for ``popola auth cursor clear``.
    """
    keyring_mod = _import_keyring()
    if keyring_mod is None:
        return False
    try:
        existing = keyring_mod.get_password(service, username)  # type: ignore[attr-defined]
        if not existing:
            return False
        keyring_mod.delete_password(service, username)  # type: ignore[attr-defined]
    except Exception as exc:
        raise CredentialBackendError(
            f"OS keyring failed to delete secret ({type(exc).__name__}: {exc})"
        ) from exc
    return True


# ── public API ───────────────────────────────────────────────────────────


def compute_fingerprint(api_key: str | None) -> str | None:
    """Return the first 12 hex chars of ``sha256(api_key)``, or None.

    Used by :class:`CredentialStatus` so operators can compare the
    fingerprint they see in ``popola auth cursor status`` against the
    one printed when they ran ``popola auth cursor set --fingerprint``
    earlier — without ever leaking the secret itself.

    Empty / whitespace-only input returns :data:`None` so callers can
    safely chain on a ``resolve_cursor_api_key()`` result.
    """
    if api_key is None:
        return None
    stripped = api_key.strip()
    if not stripped:
        return None
    digest = hashlib.sha256(stripped.encode("utf-8")).hexdigest()
    return digest[:_FINGERPRINT_LEN]


def fingerprint(api_key: str | None) -> str | None:
    """Alias for :func:`compute_fingerprint` (named for readability at call sites)."""
    return compute_fingerprint(api_key)


def redact(value: str | None, *, placeholder: str = REDACTION_PLACEHOLDER) -> str:
    """Return ``placeholder`` when ``value`` is non-empty, else empty string.

    Convenience for ``f\"key={redact(api_key)}\"`` style logging where the
    secret presence is meaningful but the value must never appear.
    """
    if value is None:
        return ""
    if not value.strip():
        return ""
    return placeholder


def redact_in_text(
    text: str,
    *,
    candidates: tuple[str | None, ...] | None = None,
    placeholder: str = REDACTION_PLACEHOLDER,
) -> str:
    """Strip every occurrence of any candidate API key from ``text``.

    By default the candidates are ``(env_var_value, keyring_value)`` —
    the two slots that could hold a real secret. Callers (e.g. the
    cloud HITL MCP tool, the relay audit writer) pass an explicit
    tuple when they want to guard against a key that was loaded earlier
    and then unset / rotated.

    Args:
        text: arbitrary text to sanitise (log line, error message, JSON
            blob, NDJSON event payload).
        candidates: tuple of candidate raw values; ``None`` / empty
            entries are skipped. When omitted, defaults to
            ``(env value, keyring value)`` resolved fresh at call time.
        placeholder: what to substitute (default
            :data:`REDACTION_PLACEHOLDER`).

    Returns:
        ``text`` with every literal candidate value replaced by
        ``placeholder``. Order-stable: longer secrets are replaced
        first to avoid partial-overlap leaks.
    """
    if not text:
        return text
    if candidates is None:
        env_value = os.environ.get(CURSOR_API_KEY_ENV)
        candidates = (env_value, _keyring_get())
    seen: set[str] = set()
    sorted_candidates = sorted(
        (c for c in candidates if c and c.strip()),
        key=len,
        reverse=True,
    )
    out = text
    for raw in sorted_candidates:
        stripped = raw.strip()
        if stripped in seen:
            continue
        seen.add(stripped)
        out = out.replace(stripped, placeholder)
    return out


# ── resolver ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CredentialResolver:
    """Resolve the Cursor API key, applying the documented precedence chain.

    Constructing a resolver with ``override`` is the test-only
    precedence-#1 hook. Production code paths should call
    :func:`resolve_cursor_api_key` (no override) so the env var and
    OS keyring stay the only configurable surfaces.
    """

    override: str | None = None
    env_var_name: str = CURSOR_API_KEY_ENV
    service_name: str = KEYRING_SERVICE
    username: str = KEYRING_USERNAME

    def resolve(self) -> tuple[str | None, str]:
        """Return ``(secret_or_None, source_label)``.

        ``source_label`` is one of ``"override"`` / ``"env"`` /
        ``"keyring"`` / ``"none"`` — surfaced verbatim by
        :class:`CredentialStatus` so operators can pinpoint which slot
        answered.
        """
        if self.override is not None and self.override.strip():
            return self.override.strip(), "override"
        env_value = os.environ.get(self.env_var_name, "")
        if env_value.strip():
            return env_value.strip(), "env"
        keyring_value = _keyring_get(service=self.service_name, username=self.username)
        if keyring_value:
            return keyring_value, "keyring"
        return None, "none"


def resolve_cursor_api_key(
    *,
    override: str | None = None,
    env_var_name: str = CURSOR_API_KEY_ENV,
    service_name: str = KEYRING_SERVICE,
    username: str = KEYRING_USERNAME,
) -> str | None:
    """Resolve the Cursor API key per documented precedence chain.

    Returns :data:`None` when no slot supplied a non-empty value. The
    caller is responsible for producing an actionable error message
    (callers should mention all three of: ``popola auth cursor set``,
    ``CURSOR_API_KEY``, and ``popola init --target=cloud-only``).
    """
    resolver = CredentialResolver(
        override=override,
        env_var_name=env_var_name,
        service_name=service_name,
        username=username,
    )
    secret, _source = resolver.resolve()
    return secret


def credential_status(*, override: str | None = None) -> CredentialStatus:
    """Build a non-secret :class:`CredentialStatus` for the auth status verb.

    Honours the precedence chain so ``source`` reflects which slot the
    secret was read from. Never returns the raw value — only the
    fingerprint + backend name.
    """
    keyring_available = is_keyring_available()
    resolver = CredentialResolver(override=override)
    secret, source = resolver.resolve()
    backend_name: str
    reason: str | None = None
    if source == "env":
        backend_name = "environment variable"
    elif source == "keyring":
        backend_name = _keyring_backend_name()
    elif source == "override":
        backend_name = "explicit override"
    else:
        backend_name = "unset"
        fallback_path = _env_fallback_path()
        if fallback_path.exists():
            try:
                mode = fallback_path.stat().st_mode & 0o777
            except OSError as exc:
                reason = f"fallback-file stat failed: {exc}"
            else:
                if mode != _ENV_FALLBACK_FILE_MODE:
                    reason = (
                        f"fallback-file mode {mode:#o} not "
                        f"{_ENV_FALLBACK_FILE_MODE:#o} (refusing to read)"
                    )
                else:
                    try:
                        contents = fallback_path.read_text(encoding="utf-8")
                    except OSError as exc:
                        reason = f"fallback-file read failed: {exc}"
                    else:
                        fallback_secret = _parse_env_fallback_value(
                            contents,
                            path=fallback_path,
                            log=logger,
                        )
                        if fallback_secret:
                            return CredentialStatus(
                                configured=True,
                                source="fallback-file",
                                backend_name=f"0o600 {_display_fallback_path(fallback_path)}",
                                fingerprint=compute_fingerprint(fallback_secret),
                                keyring_available=keyring_available,
                            )
    return CredentialStatus(
        configured=secret is not None,
        source=source,
        backend_name=backend_name,
        fingerprint=compute_fingerprint(secret),
        keyring_available=keyring_available,
        reason=reason,
    )


# ── set / clear (used by ``popola auth cursor`` + init wizard) ───────────


def store_cursor_api_key(api_key: str) -> CredentialStatus:
    """Persist ``api_key`` in the OS keyring + record non-secret metadata.

    Raises:
        ValueError: when ``api_key`` is empty / whitespace-only.
        CredentialBackendError: when the ``keyring`` extra is missing
            OR the backend rejects the write. Caller renders this with
            a remediation hint.
    """
    if not api_key or not api_key.strip():
        raise ValueError("cursor api_key must be a non-empty string")
    stripped = api_key.strip()
    _keyring_set(stripped)
    metadata = load_credential_metadata()
    metadata["backend"] = "keyring"
    metadata["fingerprint"] = compute_fingerprint(stripped) or ""
    metadata["last_set_at"] = _utc_now_iso()
    save_credential_metadata(metadata)
    return credential_status()


def delete_cursor_api_key() -> tuple[bool, CredentialStatus]:
    """Remove the keyring entry + metadata; return ``(removed, status)``.

    Idempotent: returns ``(False, status)`` when no entry was present.
    The metadata file's ``[cursor]`` table is cleared on every successful
    delete so ``popola auth cursor status`` does not falsely report
    ``configured: true`` after a delete.
    """
    removed = _keyring_delete()
    metadata_file = metadata_path()
    if metadata_file.exists():
        try:
            save_credential_metadata({})
        except OSError as exc:  # pragma: no cover — defensive only
            logger.warning("failed to clear credentials metadata at %s: %s", metadata_file, exc)
    return removed, credential_status()


# ── account class persistence (v0.9.9 F5 + U1) ──────────────────────────


_ACCOUNT_CLASS_VALID_INPUT: Final[frozenset[str]] = frozenset(
    {"personal", "service-account", "service_account", "unknown"}
)
"""Case-insensitive whitelist of accepted ``--account-class`` values.

Includes both the dashed (``service-account``) and underscored
(``service_account``) forms so the CLI choice flag and the on-disk
TOML key (which uses the underscored form, matching Python's
:class:`AccountClass` member name convention) round-trip cleanly.
"""


_DEPRECATION_WARNING_EMITTED: bool = False
"""Process-lifetime flag gating the one-time ``account_class`` deprecation WARN.

Per DECISIONS Q-10 / PLAN C3 AC 1+3, the v0.9.9 ``account_class``
field is kept for backward-compat (the enum, ``store_account_class``,
the ``--account-class`` CLI flag, and the on-disk TOML field all
remain to avoid API breakage and to preserve telemetry-grade
distinguishability for callers). The pre-flight gate that consumed
the value has been removed (Q-4); :func:`get_account_class` now
emits a single deprecation WARN per process when the stored value is
non-:data:`AccountClass.UNKNOWN`. Subsequent calls observe this flag
and stay silent. Tests reset the flag via ``monkeypatch`` between
scenarios; the autouse fixture in
``tests/test_credentials_account_class_deprecation.py`` flips it
back to ``False`` on test entry.

Same fire-once shape as
:data:`popolaloom.daemon.main._CLOUD_TARGET_PRIORITY_DEPRECATION_WARNED`
(Wave B1) so operators see a consistent v0.10.0 deprecation cadence
across the credentials and user-preferences subsystems.
"""


def store_account_class(value: str) -> None:
    """Persist the operator's declared account class into ``credentials.toml``.

    Validates ``value`` against the case-insensitive whitelist
    ``{"personal", "service-account", "service_account", "unknown"}``
    and normalises ``"service-account"`` → ``"service_account"`` so the
    on-disk form matches :class:`AccountClass` member values verbatim.

    Reuses :func:`load_credential_metadata` + :func:`save_credential_metadata`
    so the existing ``[cursor]`` TOML table layout, the 0o600 file
    mode, and the atomic-replace write are preserved. The operation is
    additive — it never clears ``backend`` / ``last_set_at`` /
    ``fingerprint`` (those belong to :func:`store_cursor_api_key`).

    Args:
        value: one of ``"personal"`` / ``"service-account"`` /
            ``"service_account"`` / ``"unknown"``; case-insensitive.

    Raises:
        ValueError: when ``value`` is empty or outside the whitelist
            (No Silent Failures — invalid CLI input must surface a
            clean ``typer.BadParameter`` upstream rather than silently
            recording an unrecognised class).
    """
    if value is None:
        raise ValueError("account_class must be a non-empty string")
    stripped = value.strip()
    if not stripped:
        raise ValueError("account_class must be a non-empty string")
    lowered = stripped.lower()
    if lowered not in _ACCOUNT_CLASS_VALID_INPUT:
        raise ValueError(
            f"invalid account_class {value!r}; expected one of "
            "{personal, service-account, service_account, unknown} "
            "(case-insensitive)"
        )
    normalized = "service_account" if lowered == "service-account" else lowered
    metadata = load_credential_metadata()
    metadata[_ACCOUNT_CLASS_KEY] = normalized
    save_credential_metadata(metadata)


def get_account_class() -> AccountClass:
    """Read the declared account class from ``credentials.toml``.

    Returns :data:`AccountClass.UNKNOWN` when the metadata file is
    absent (fresh install), the ``[cursor]`` table is empty, the
    ``account_class`` key is missing (pre-v0.9.9 file — backward-compat
    path), or the stored value is unrecognised (defensive: a
    hand-edited TOML with ``account_class = "garbage"`` does not crash
    callers; it simply normalises to the UNKNOWN sentinel).

    .. deprecated:: 0.10.0
        The v0.9.9 F5 + U1 pre-flight gate that consumed this value
        has been removed (DECISIONS Q-4 + Q-10): the live REST probes
        in ``research/01-path-2-live-probe.md`` disconfirmed the
        Spike-0 BRANCH_B verdict, so the gateway now accepts
        ``env: {type, name?}`` for both ``personal`` and
        ``service_account`` keys with no code-path divergence. The
        helper, the enum, :func:`store_account_class`, the
        ``--account-class`` CLI flag, and the TOML field are all KEPT
        for backward compat and telemetry, but the value is no longer
        consulted by dispatch routing. A one-time ``logger.warning``
        fires the first time this function returns a non-UNKNOWN
        value within a process so operators see the deprecation
        notice once per ``popolad`` lifecycle (gated by the
        :data:`_DEPRECATION_WARNING_EMITTED` module-level flag).
        Removal is targeted at v1.1+ per the
        :doc:`docs/API_STABILITY` deprecation cadence.

    Pure read; never writes. The unrecognised-value branch still
    logs its own (separate) WARNING because that is a configuration
    error the operator should fix — independent of the deprecation
    notice.
    """
    metadata = load_credential_metadata()
    raw = metadata.get(_ACCOUNT_CLASS_KEY)
    if not raw:
        return AccountClass.UNKNOWN
    candidate = str(raw).strip().lower()
    if not candidate:
        return AccountClass.UNKNOWN
    if candidate == "service-account":
        candidate = "service_account"
    try:
        result = AccountClass(candidate)
    except ValueError:
        logger.warning(
            "credentials.toml has unrecognised account_class %r at %s; "
            "treating as UNKNOWN (re-run `popola auth cursor set "
            "--account-class=...` to fix)",
            raw,
            metadata_path(),
        )
        return AccountClass.UNKNOWN

    if result is not AccountClass.UNKNOWN:
        global _DEPRECATION_WARNING_EMITTED
        if not _DEPRECATION_WARNING_EMITTED:
            logger.warning(
                "account_class is deprecated as of v0.10.0; "
                "the v0.9.9 pre-flight gate has been removed. "
                "See CHANGELOG.md#v0.10.0"
            )
            _DEPRECATION_WARNING_EMITTED = True

    return result


def _utc_now_iso() -> str:
    """ISO-8601 ``YYYY-MM-DDTHH:MM:SSZ`` timestamp (no microseconds, UTC).

    Wrapped in a helper so tests can monkeypatch the clock without
    touching :mod:`datetime` globally.
    """
    from datetime import UTC, datetime

    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
