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

4. **Missing** — return :data:`None`. Callers must produce an actionable
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
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover — type-check-only imports
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "CURSOR_API_KEY_ENV",
    "CredentialBackendError",
    "CredentialResolver",
    "CredentialStatus",
    "KEYRING_SERVICE",
    "KEYRING_USERNAME",
    "REDACTION_PLACEHOLDER",
    "compute_fingerprint",
    "fingerprint",
    "is_keyring_available",
    "load_credential_metadata",
    "metadata_path",
    "redact",
    "redact_in_text",
    "resolve_cursor_api_key",
    "save_credential_metadata",
    "store_cursor_api_key",
    "delete_cursor_api_key",
    "credential_status",
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
_FINGERPRINT_LEN: Final[int] = 12
"""How many hex chars of the SHA-256 to expose in status output.

12 hex chars = 48 bits of entropy. Enough to disambiguate the operator's
"is the key I just set the same as the one stored?" question without
leaking a useful chunk of the key itself.
"""


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
    """

    configured: bool
    source: str
    backend_name: str
    fingerprint: str | None
    keyring_available: bool

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
        import keyring  # type: ignore[import-not-found]  # noqa: PLC0415 — lazy on purpose
    except ImportError:
        return None
    return keyring  # type: ignore[no-any-return]


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
            "run `pip install popolaloom[credentials]` or set CURSOR_API_KEY in the env"
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
    resolver = CredentialResolver(override=override)
    secret, source = resolver.resolve()
    backend_name: str
    if source == "env":
        backend_name = "environment variable"
    elif source == "keyring":
        backend_name = _keyring_backend_name()
    elif source == "override":
        backend_name = "explicit override"
    else:
        backend_name = "unset"
    return CredentialStatus(
        configured=secret is not None,
        source=source,
        backend_name=backend_name,
        fingerprint=compute_fingerprint(secret),
        keyring_available=is_keyring_available(),
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


def _utc_now_iso() -> str:
    """ISO-8601 ``YYYY-MM-DDTHH:MM:SSZ`` timestamp (no microseconds, UTC).

    Wrapped in a helper so tests can monkeypatch the clock without
    touching :mod:`datetime` globally.
    """
    from datetime import UTC, datetime

    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
