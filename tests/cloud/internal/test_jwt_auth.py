"""Tests for popolaloom.cloud.internal.jwt_auth (v1.0.0 GA, S4 W-B).

Per .local/.agent/active/v1.0.0-ga/DECISIONS.md Q-14 (LOCKED) the loader
sources from env > file. Per Q-15 (LOCKED) refresh uses fcntl.LOCK_EX
on the auth.json fd during write.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from popolaloom.cloud.internal.jwt_auth import (
    DEFAULT_AUTH_JSON_PATH,
    ENV_VAR_JWT,
    ENV_VAR_REFRESH_TOKEN,
    JWTAuthError,
    JWTBundle,
    _bundle_about_to_expire,
    _decode_jwt_exp,
    _is_jwt_expired,
    load_jwt_bundle,
    write_refreshed_bundle,
)


def _fake_jwt(exp_unix_s: int | None = None) -> str:
    """Build a JWS-shape token (no signature check) with optional exp."""
    header_b64 = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload: dict[str, object] = {"sub": "test"}
    if exp_unix_s is not None:
        payload["exp"] = exp_unix_s
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    )
    return f"{header_b64}.{payload_b64}.sig"


def test_load_from_env_var_takes_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Q-14: env var wins over file when both are present."""
    auth_json = tmp_path / "auth.json"
    auth_json.write_text(
        json.dumps({"accessToken": "FILE_TOKEN", "refreshToken": "FILE_REF"})
    )
    monkeypatch.setenv(ENV_VAR_JWT, "ENV_TOKEN")
    monkeypatch.setenv(ENV_VAR_REFRESH_TOKEN, "ENV_REF")
    bundle = load_jwt_bundle(auth_json_path=auth_json)
    assert bundle.access_token == "ENV_TOKEN"
    assert bundle.refresh_token == "ENV_REF"
    assert bundle.source == "env"
    assert bundle.path is None


def test_load_from_file_when_no_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Q-14: file fallback when env var missing."""
    auth_json = tmp_path / "auth.json"
    auth_json.write_text(
        json.dumps(
            {"accessToken": "FILE_TOKEN", "refreshToken": "FILE_REF", "apiKey": "X"}
        )
    )
    monkeypatch.delenv(ENV_VAR_JWT, raising=False)
    bundle = load_jwt_bundle(auth_json_path=auth_json)
    assert bundle.access_token == "FILE_TOKEN"
    assert bundle.refresh_token == "FILE_REF"
    assert bundle.source == "file"
    assert bundle.path == auth_json


def test_load_raises_when_neither_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JWTAuthError carries a bilingual hint pointing at `cursor login`."""
    monkeypatch.delenv(ENV_VAR_JWT, raising=False)
    with pytest.raises(JWTAuthError) as exc_info:
        load_jwt_bundle(auth_json_path=tmp_path / "missing.json")
    assert "cursor login" in exc_info.value.hint
    assert "JWT" in str(exc_info.value)


def test_load_raises_when_file_lacks_access_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty / missing accessToken is rejected (No Silent Failures)."""
    auth_json = tmp_path / "auth.json"
    auth_json.write_text(json.dumps({"refreshToken": "X"}))
    monkeypatch.delenv(ENV_VAR_JWT, raising=False)
    with pytest.raises(JWTAuthError) as exc_info:
        load_jwt_bundle(auth_json_path=auth_json)
    assert "accessToken" in str(exc_info.value)


def test_decode_jwt_exp_well_formed() -> None:
    """JWT exp claim is decoded from the payload (no signature check)."""
    target = 1_900_000_000
    token = _fake_jwt(target)
    assert _decode_jwt_exp(token) == target


def test_decode_jwt_exp_returns_none_for_malformed() -> None:
    """Tokens without 3 dot-separated parts return None."""
    assert _decode_jwt_exp("not.a.jwt.either") is None
    assert _decode_jwt_exp("nodot") is None
    assert _decode_jwt_exp("a.b") is None


def test_is_jwt_expired_within_safety_margin() -> None:
    """A token with <30s remaining is treated as expired."""
    soon = int(time.time()) + 10
    token = _fake_jwt(soon)
    assert _is_jwt_expired(token) is True


def test_is_jwt_expired_outside_safety_margin() -> None:
    """A token with >>30s remaining is fresh."""
    far = int(time.time()) + 7200
    token = _fake_jwt(far)
    assert _is_jwt_expired(token) is False


def test_is_jwt_expired_no_exp_claim_treated_fresh() -> None:
    """Tokens without an exp claim are NOT treated as expired ('trust until 401')."""
    token = _fake_jwt(None)
    assert _is_jwt_expired(token) is False


def test_bundle_about_to_expire_wraps_is_expired() -> None:
    """The convenience wrapper matches the underlying check."""
    far = int(time.time()) + 7200
    bundle = JWTBundle(
        access_token=_fake_jwt(far),
        refresh_token=None,
        source="env",
        path=None,
        exp_unix_s=far,
    )
    assert _bundle_about_to_expire(bundle) is False


def test_write_refreshed_bundle_persists_to_file_under_lock(
    tmp_path: Path,
) -> None:
    """Q-15: refresh writes new tokens; the file is rewritten atomically."""
    auth_json = tmp_path / "auth.json"
    auth_json.write_text(json.dumps({"accessToken": "old", "refreshToken": "ref"}))
    bundle = JWTBundle(
        access_token="old",
        refresh_token="ref",
        source="file",
        path=auth_json,
        exp_unix_s=None,
    )
    refreshed = write_refreshed_bundle(
        bundle,
        new_access_token=_fake_jwt(int(time.time()) + 7200),
        new_refresh_token="new-ref",
        auth_json_path=auth_json,
    )
    assert refreshed.access_token != "old"
    assert refreshed.refresh_token == "new-ref"
    persisted = json.loads(auth_json.read_text())
    assert persisted["accessToken"] == refreshed.access_token
    assert persisted["refreshToken"] == "new-ref"


def test_write_refreshed_bundle_env_source_does_not_touch_disk(
    tmp_path: Path,
) -> None:
    """env-var-sourced bundles are read-only; refresh updates in-memory only."""
    bundle = JWTBundle(
        access_token="old",
        refresh_token="ref",
        source="env",
        path=None,
        exp_unix_s=None,
    )
    new_token = _fake_jwt(int(time.time()) + 7200)
    refreshed = write_refreshed_bundle(
        bundle,
        new_access_token=new_token,
        new_refresh_token=None,
    )
    assert refreshed.access_token == new_token
    assert refreshed.source == "env"
    assert refreshed.path is None


def test_default_auth_json_path_constant() -> None:
    """The canonical path is ~/.config/cursor/auth.json (sanity check)."""
    assert DEFAULT_AUTH_JSON_PATH.name == "auth.json"
    assert DEFAULT_AUTH_JSON_PATH.parent.name == "cursor"


def test_decode_jwt_exp_accepts_float_payload() -> None:
    """JWT exp claim may be a float; decoder coerces to int."""
    import base64 as _b64
    import json as _json

    header_b64 = _b64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload_b64 = (
        _b64.urlsafe_b64encode(_json.dumps({"exp": 1_900_000_000.5}).encode())
        .rstrip(b"=")
        .decode()
    )
    token = f"{header_b64}.{payload_b64}.sig"
    assert _decode_jwt_exp(token) == 1_900_000_000


def test_load_raises_when_file_is_not_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corrupt JSON file is wrapped in JWTAuthError with bilingual hint."""
    auth_json = tmp_path / "auth.json"
    auth_json.write_text("not json at all")
    monkeypatch.delenv(ENV_VAR_JWT, raising=False)
    with pytest.raises(JWTAuthError) as exc_info:
        load_jwt_bundle(auth_json_path=auth_json)
    assert "valid JSON" in exc_info.value.hint
    assert "请检查" in exc_info.value.hint


def test_load_raises_when_file_top_level_not_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON array (not object) at top-level is rejected."""
    auth_json = tmp_path / "auth.json"
    auth_json.write_text("[1, 2, 3]")
    monkeypatch.delenv(ENV_VAR_JWT, raising=False)
    with pytest.raises(JWTAuthError) as exc_info:
        load_jwt_bundle(auth_json_path=auth_json)
    assert "top-level must be a JSON object" in str(exc_info.value)


def test_write_refreshed_bundle_creates_parent_dir_when_missing(
    tmp_path: Path,
) -> None:
    """The file-lock helper creates parent dirs and the auth file at 0o600."""
    nested = tmp_path / "deep" / "nested" / "auth.json"
    bundle = JWTBundle(
        access_token="old",
        refresh_token=None,
        source="file",
        path=nested,
        exp_unix_s=None,
    )
    refreshed = write_refreshed_bundle(
        bundle,
        new_access_token=_fake_jwt(int(time.time()) + 7200),
        new_refresh_token="r",
        auth_json_path=nested,
    )
    assert refreshed.access_token != "old"
    assert nested.exists()
    persisted = json.loads(nested.read_text())
    assert persisted["accessToken"] == refreshed.access_token
    assert persisted["refreshToken"] == "r"


def test_write_refreshed_bundle_handles_existing_corrupt_json(
    tmp_path: Path,
) -> None:
    """When the existing auth.json is corrupt, refresh replaces it cleanly."""
    auth_json = tmp_path / "auth.json"
    auth_json.write_text("totally not json")
    bundle = JWTBundle(
        access_token="old",
        refresh_token=None,
        source="file",
        path=auth_json,
        exp_unix_s=None,
    )
    new_token = _fake_jwt(int(time.time()) + 7200)
    refreshed = write_refreshed_bundle(
        bundle,
        new_access_token=new_token,
        new_refresh_token=None,
        auth_json_path=auth_json,
    )
    assert refreshed.access_token == new_token
    persisted = json.loads(auth_json.read_text())
    assert persisted["accessToken"] == new_token


def test_write_refreshed_bundle_preserves_existing_refresh_when_new_is_none(
    tmp_path: Path,
) -> None:
    """new_refresh_token=None keeps the current bundle's refresh_token."""
    auth_json = tmp_path / "auth.json"
    auth_json.write_text(
        json.dumps({"accessToken": "old", "refreshToken": "keep-me"})
    )
    bundle = JWTBundle(
        access_token="old",
        refresh_token="keep-me",
        source="file",
        path=auth_json,
        exp_unix_s=None,
    )
    refreshed = write_refreshed_bundle(
        bundle,
        new_access_token=_fake_jwt(int(time.time()) + 7200),
        new_refresh_token=None,
        auth_json_path=auth_json,
    )
    assert refreshed.refresh_token == "keep-me"
