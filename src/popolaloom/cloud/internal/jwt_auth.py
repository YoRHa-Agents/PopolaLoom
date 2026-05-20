"""JWT loader / validator / refresh for Cursor's session-JWT auth (path-B).

Per :file:`.local/.agent/active/v1.0.0-ga/DECISIONS.md` Q-14 (LOCKED) the
JWT can come from either of two sources, in this precedence order:

1. ``CURSOR_SESSION_JWT`` env variable (string-encoded ``accessToken``);
   this lets CI / Docker / ephemeral test envs avoid copying a real
   ``auth.json`` to disk.
2. ``~/.config/cursor/auth.json`` — the canonical Cursor desktop
   location. Probed structure (live-verified 2026-05-11):
   ``{"accessToken": "...", "refreshToken": "...", "apiKey": "..."}``.

Per Q-15 (LOCKED) refresh strategy is **lazy** (only on 401) plus
``fcntl.LOCK_EX`` on the auth.json fd during write to address the
concurrent-dispatch refresh race. The lock is held only during the
``POST /api/auth/refresh`` round-trip, NOT during the dispatch — so
concurrent dispatches do not serialize.

EXPERIMENTAL — see :mod:`popolaloom.cloud.internal.__init__` docstring
for the path-B stability commitment (NONE per Q-22). The refresh
endpoint URL + body shape are reverse-engineered and may change at any
time; callers MUST handle :class:`JWTAuthError` and fall back to
``--auth-mode=rest`` when refresh fails.
"""

from __future__ import annotations

import base64
import fcntl
import json
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)

DEFAULT_AUTH_JSON_PATH = Path.home() / ".config" / "cursor" / "auth.json"
ENV_VAR_JWT = "CURSOR_SESSION_JWT"
ENV_VAR_REFRESH_TOKEN = "CURSOR_SESSION_REFRESH_TOKEN"

_JWT_EXP_MIN_SAFETY_MARGIN_S = 30
"""Minimum seconds-to-expiry the loader treats as still-valid.

JWT exp inspection is best-effort (no signature check); we add a 30-second
safety margin so a token with <30s remaining is treated as expired and
refreshed early. This avoids the race where a token validates locally but
expires server-side before the request reaches Cursor.
"""


class JWTAuthError(RuntimeError):
    """Raised when the path-B JWT cannot be loaded / validated / refreshed.

    Carries a structured ``hint`` attribute with a bilingual operator
    message pointing at the actual fix; the CLI surface uses this to
    print a friendly error and exit non-zero (No Silent Failures rule).
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint or ""


@dataclass(frozen=True, slots=True)
class JWTBundle:
    """The four pieces a path-B caller needs to build an authorized RPC call.

    Attributes:
        access_token: The JWT to put in ``Authorization: Bearer <X>``.
        refresh_token: The refresh token used to mint a new access_token
            on 401. ``None`` when the bundle came from
            :data:`ENV_VAR_JWT` env var without a paired
            :data:`ENV_VAR_REFRESH_TOKEN` (CI scenarios) — refresh is
            then a no-op.
        source: ``"env"`` when env-var sourced, else ``"file"`` (auth.json
            path). Useful for diagnostic logging.
        path: The auth.json path when ``source == "file"``, else ``None``.
        exp_unix_s: Best-effort decoded JWT ``exp`` claim (seconds since
            epoch). ``None`` when the token doesn't carry an exp claim or
            cannot be decoded. Consumers SHOULD treat ``None`` as "trust
            until a 401 says otherwise" rather than refusing to use the
            bundle.
    """

    access_token: str
    refresh_token: str | None
    source: str
    path: Path | None
    exp_unix_s: int | None


def _decode_jwt_exp(token: str) -> int | None:
    """Best-effort decode of a JWT's ``exp`` claim (no signature check).

    Returns ``None`` for malformed tokens. The Cursor JWT format is
    standard JWS (header.payload.signature, base64url-encoded); we only
    care about the payload's ``exp`` field for the safety-margin check.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        # Pad the base64url payload to a multiple of 4.
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    exp = payload.get("exp")
    if isinstance(exp, int):
        return exp
    if isinstance(exp, float):
        return int(exp)
    return None


def _is_jwt_expired(token: str, now_unix_s: int | None = None) -> bool:
    """Return True iff the JWT's exp claim is within the safety margin.

    Tokens without a decodable exp claim are treated as **not expired**
    (per docstring: "trust until a 401 says otherwise"). The safety
    margin is :data:`_JWT_EXP_MIN_SAFETY_MARGIN_S` seconds.
    """
    exp = _decode_jwt_exp(token)
    if exp is None:
        return False
    now = now_unix_s if now_unix_s is not None else int(time.time())
    return exp - now < _JWT_EXP_MIN_SAFETY_MARGIN_S


def load_jwt_bundle(
    *,
    auth_json_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> JWTBundle:
    """Load a JWT bundle from env var (Q-14 priority) then file fallback.

    Args:
        auth_json_path: Override the default ``~/.config/cursor/auth.json``
            location. When ``None``, uses :data:`DEFAULT_AUTH_JSON_PATH`.
        env: Override the process env for tests. When ``None``, uses
            :data:`os.environ`.

    Returns:
        A :class:`JWTBundle` ready for use by
        :class:`CursorCloudInternalClient`.

    Raises:
        JWTAuthError: when neither the env var nor the file has a usable
            access token. The error's ``hint`` field carries a bilingual
            operator-facing message pointing at how to obtain a JWT
            (``agent login`` or ``CURSOR_SESSION_JWT`` env var).
    """
    env_map = env if env is not None else dict(os.environ)
    path = auth_json_path or DEFAULT_AUTH_JSON_PATH

    env_access = env_map.get(ENV_VAR_JWT, "").strip()
    if env_access:
        env_refresh = env_map.get(ENV_VAR_REFRESH_TOKEN, "").strip() or None
        return JWTBundle(
            access_token=env_access,
            refresh_token=env_refresh,
            source="env",
            path=None,
            exp_unix_s=_decode_jwt_exp(env_access),
        )

    if not path.exists():
        raise JWTAuthError(
            f"path-B requires a JWT, but neither {ENV_VAR_JWT} env var "
            f"nor {path} exists",
            hint=(
                f"Run `agent login` on this machine to populate {path}, "
                f"OR export {ENV_VAR_JWT}=<jwt> for CI / ephemeral envs. "
                f"路径-B 需要 JWT,可以运行 `agent login` 生成 {path},"
                f"或者通过 {ENV_VAR_JWT} 环境变量提供。"
            ),
        )

    try:
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, json.JSONDecodeError) as exc:
        raise JWTAuthError(
            f"failed to read {path}: {exc}",
            hint=(
                f"Verify {path} is valid JSON with an 'accessToken' field. "
                f"请检查 {path} 是否为有效 JSON 且包含 'accessToken' 字段。"
            ),
        ) from exc

    if not isinstance(data, dict):
        raise JWTAuthError(
            f"{path} top-level must be a JSON object, got {type(data).__name__}",
            hint=f"请确保 {path} 顶层是 JSON 对象。",
        )
    access = data.get("accessToken", "")
    if not isinstance(access, str) or not access:
        raise JWTAuthError(
            f"{path} missing or empty 'accessToken' field",
            hint=(
                f"Run `agent login` to refresh {path} and re-populate "
                f"accessToken. (运行 `agent login` 重新生成 accessToken)"
            ),
        )
    refresh = data.get("refreshToken")
    refresh_str = refresh if isinstance(refresh, str) and refresh else None
    return JWTBundle(
        access_token=access,
        refresh_token=refresh_str,
        source="file",
        path=path,
        exp_unix_s=_decode_jwt_exp(access),
    )


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[IO[str]]:
    """Open *path* with ``fcntl.LOCK_EX`` for the duration of the context.

    Used during the path-B refresh write to address the JWT refresh race
    (Q-15). The lock is released when the context exits, even on error.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch(mode=0o600)
    fp = path.open("r+", encoding="utf-8")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        yield fp
    finally:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        finally:
            fp.close()


def write_refreshed_bundle(
    bundle: JWTBundle,
    new_access_token: str,
    new_refresh_token: str | None,
    *,
    auth_json_path: Path | None = None,
) -> JWTBundle:
    """Persist a refreshed token pair to ``auth.json`` under file-lock.

    When ``bundle.source == "env"`` (the env-var path), the in-memory
    bundle is updated but no file write happens — env-var-sourced
    bundles are read-only by design (CI scenarios should rotate the
    secret externally).

    Args:
        bundle: The current bundle being refreshed.
        new_access_token: The refreshed access token from the upstream
            refresh endpoint.
        new_refresh_token: The refreshed refresh token (may be the same
            as the old one when the upstream rotates only the access
            token). ``None`` keeps the current refresh token unchanged.
        auth_json_path: Override the default file location (matches the
            optional kwarg on :func:`load_jwt_bundle`).

    Returns:
        A new :class:`JWTBundle` with the updated tokens. The original
        ``bundle`` is unchanged (frozen dataclass).

    Raises:
        JWTAuthError: when the file write fails after acquiring the lock.
    """
    new_refresh = new_refresh_token if new_refresh_token is not None else bundle.refresh_token
    new_exp = _decode_jwt_exp(new_access_token)
    if bundle.source == "env":
        return JWTBundle(
            access_token=new_access_token,
            refresh_token=new_refresh,
            source="env",
            path=None,
            exp_unix_s=new_exp,
        )

    path = auth_json_path or bundle.path or DEFAULT_AUTH_JSON_PATH
    try:
        with _exclusive_file_lock(path) as fp:
            fp.seek(0)
            try:
                data = json.load(fp) if fp.read(1) else {}
                fp.seek(0)
            except json.JSONDecodeError:
                data = {}
                fp.seek(0)
            if not isinstance(data, dict):
                data = {}
            data["accessToken"] = new_access_token
            if new_refresh is not None:
                data["refreshToken"] = new_refresh
            fp.seek(0)
            fp.truncate()
            json.dump(data, fp)
            fp.flush()
            os.fsync(fp.fileno())
    except OSError as exc:
        raise JWTAuthError(
            f"failed to persist refreshed JWT to {path}: {exc}",
            hint=f"请检查 {path} 的写入权限 (chmod 0600 expected)。",
        ) from exc
    return JWTBundle(
        access_token=new_access_token,
        refresh_token=new_refresh,
        source="file",
        path=path,
        exp_unix_s=new_exp,
    )


__all__ = [
    "DEFAULT_AUTH_JSON_PATH",
    "ENV_VAR_JWT",
    "ENV_VAR_REFRESH_TOKEN",
    "JWTAuthError",
    "JWTBundle",
    "_decode_jwt_exp",
    "_is_jwt_expired",
    "load_jwt_bundle",
    "write_refreshed_bundle",
]


def _bundle_about_to_expire(bundle: JWTBundle) -> bool:
    """Public-friendly wrapper around :func:`_is_jwt_expired`.

    Returns ``True`` iff the bundle's access_token's exp claim is within
    the safety margin (or already past). Used by callers that want to
    pre-emptively refresh before issuing the dispatch RPC.
    """
    return _is_jwt_expired(bundle.access_token)
