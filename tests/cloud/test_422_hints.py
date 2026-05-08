"""Unit tests for the v0.8.6 422 / integration error catalog (T2.1.3).

Owned by L3 Subagent T2.1.3 — covers the new ``CursorCloudError`` subclasses
and the precedence-aware ``_map_http_error`` selector in
:mod:`popolaloom.adapters.cursor_cloud`.

Source: ``.local/research/v0.8.6_sse/422-error-catalog.md`` §3.1 master table
and §4 YAML implementation reference.

Tests cover:

- Every new exception class can be raised and exposes bilingual ``hint_en`` /
  ``hint_zh`` strings (each ≥1 ``https://...`` URL) and a ``cli_exit`` int
  that matches the catalog (`§3.1`).
- The ``_ERROR_CATALOG`` dict shape (1-to-1 with §4 YAML).
- The ``_map_http_error`` selector dispatches by ``error.code`` first (the
  canonical Cloud Agents v1 envelope), then by ``error.message`` regex (the
  documented precedence for 422 integration errors), then by HTTP status
  fallback.
- Backward-compat for ``CursorCloudConflictError`` retry default (OQ-4
  resolved as ``retry: false`` for v0.8.6 per ``DECISIONS.md``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import httpx
import pytest

from popolaloom.adapters.cursor_cloud import (
    _ERROR_CATALOG,
    CursorCloudApiKeyRevokedError,
    CursorCloudAuthError,
    CursorCloudConflictError,
    CursorCloudError,
    CursorCloudFeatureUnavailableError,
    CursorCloudNotFoundError,
    CursorCloudPlanRequiredError,
    CursorCloudRateLimitError,
    CursorCloudStreamExpiredError,
    CursorCloudValidationError,
    GithubAppMissingError,
    GithubAppPermissionError,
    RepoAllowlistError,
    _map_http_error,
)

_URL_PATTERN = re.compile(r"https://[^\s)]+")


def _make_response(
    status_code: int,
    *,
    json_body: dict[str, object] | None = None,
    text: str | None = None,
) -> httpx.Response:
    """Build a synthetic ``httpx.Response`` for selector dispatch tests."""
    if json_body is not None:
        return httpx.Response(status_code, json=json_body)
    if text is not None:
        return httpx.Response(status_code, text=text)
    return httpx.Response(status_code)


_NEW_CLASSES_AND_IDS: tuple[tuple[type[CursorCloudError], str], ...] = (
    (CursorCloudApiKeyRevokedError, "unauthorized_revoked"),
    (CursorCloudPlanRequiredError, "forbidden_plan_required"),
    (CursorCloudFeatureUnavailableError, "forbidden_feature"),
    (CursorCloudNotFoundError, "not_found_agent_or_run"),
    (CursorCloudStreamExpiredError, "stream_expired"),
    (RepoAllowlistError, "integration_repo_allowlist"),
    (GithubAppMissingError, "integration_github_app_missing"),
    (GithubAppPermissionError, "integration_github_app_perms"),
    (CursorCloudValidationError, "validation_request_body"),
    (CursorCloudRateLimitError, "rate_limit"),
)


@pytest.mark.parametrize(
    ("cls", "entry_id"),
    _NEW_CLASSES_AND_IDS,
)
def test_new_class_exposes_bilingual_hints_and_cli_exit(
    cls: type[CursorCloudError],
    entry_id: str,
) -> None:
    """AC (b)(c)(d): each new class can be raised + has bilingual hints + cli_exit.

    Both class-level access (``cls.hint_en``) and instance access
    (``raised.hint_en``) must yield the same string sourced from
    ``_ERROR_CATALOG``; each hint must contain ≥1 ``https://...`` URL.
    """
    entry = _ERROR_CATALOG[entry_id]

    assert cls.hint_en == entry["hint_en"]
    assert cls.hint_zh == entry["hint_zh"]
    assert cls.cli_exit == entry["cli_exit"]

    assert _URL_PATTERN.search(cls.hint_en), f"{cls.__name__}.hint_en missing URL"
    assert _URL_PATTERN.search(cls.hint_zh), f"{cls.__name__}.hint_zh missing URL"

    assert isinstance(cls.cli_exit, int)
    assert cls.cli_exit > 0

    raised = cls("boom")
    assert raised.hint_en == entry["hint_en"]
    assert raised.hint_zh == entry["hint_zh"]
    assert raised.cli_exit == entry["cli_exit"]
    assert isinstance(raised, CursorCloudError)


def test_catalog_has_exactly_16_entries_with_required_fields() -> None:
    """AC (a): ``_ERROR_CATALOG`` mirrors the §4 YAML — 16 entries, full shape."""
    expected_ids = {
        "unauthorized_invalid_key",
        "unauthorized_revoked",
        "forbidden_plan_required",
        "forbidden_role",
        "forbidden_feature",
        "not_found_agent_or_run",
        "conflict_agent_busy",
        "conflict_archived",
        "conflict_not_cancellable",
        "stream_expired",
        "integration_repo_allowlist",
        "integration_github_app_missing",
        "integration_github_app_perms",
        "validation_request_body",
        "rate_limit",
        "backend_5xx",
    }
    assert set(_ERROR_CATALOG.keys()) == expected_ids

    required_fields = {"http", "code", "subclass", "retry", "cli_exit", "hint_en", "hint_zh"}
    for entry_id, entry in _ERROR_CATALOG.items():
        missing = required_fields - set(entry.keys())
        assert not missing, f"entry {entry_id!r} missing fields {missing}"


def test_409_agent_busy_retry_is_off_in_v086() -> None:
    """OQ-4 (DECISIONS.md): 409 ``agent_busy`` is non-retryable in v0.8.6.

    Implementation note pinned by inline comment ``# v0.8.6 default; revisit
    in v0.8.8 per Q-C-5 (queue + notify)`` adjacent to the catalog row.
    """
    entry = _ERROR_CATALOG["conflict_agent_busy"]
    assert entry["retry"] is False, "409 agent_busy must surface immediately in v0.8.6"

    response = _make_response(
        409,
        json_body={"error": {"code": "agent_busy", "message": "another run is active"}},
    )
    raised = _map_http_error(response)
    assert isinstance(raised, CursorCloudConflictError)
    assert raised.is_retryable is False
    assert raised.status_code == 409


def test_selector_dispatches_401_unauthorized_to_auth_error() -> None:
    response = _make_response(
        401,
        json_body={"error": {"code": "unauthorized", "message": "bad key"}},
    )
    raised = _map_http_error(response)
    assert isinstance(raised, CursorCloudAuthError)
    assert raised.status_code == 401
    assert raised.is_retryable is False
    assert raised.cli_exit == 77
    assert "https://cursor.com/dashboard/integrations" in raised.hint_en


def test_selector_dispatches_401_revoked_key_to_subclass() -> None:
    response = _make_response(
        401,
        json_body={"error": {"code": "api_key_not_found", "message": "revoked"}},
    )
    raised = _map_http_error(response)
    assert isinstance(raised, CursorCloudApiKeyRevokedError)
    assert isinstance(raised, CursorCloudAuthError), "must subclass CursorCloudAuthError"
    assert raised.cli_exit == 77


def test_selector_dispatches_403_plan_required() -> None:
    response = _make_response(
        403,
        json_body={"error": {"code": "plan_required", "message": "free tier"}},
    )
    raised = _map_http_error(response)
    assert isinstance(raised, CursorCloudPlanRequiredError)
    assert raised.status_code == 403
    assert raised.is_retryable is False
    assert raised.cli_exit == 78


def test_selector_dispatches_404_not_found_for_run_or_agent() -> None:
    """404 ``run_not_found`` and ``agent_not_found`` both map to NotFound."""
    for code in ("agent_not_found", "run_not_found"):
        response = _make_response(404, json_body={"error": {"code": code}})
        raised = _map_http_error(response)
        assert isinstance(raised, CursorCloudNotFoundError), code
        assert raised.cli_exit == 100


def test_selector_dispatches_410_stream_expired() -> None:
    response = _make_response(
        410,
        json_body={"error": {"code": "stream_expired"}},
    )
    raised = _map_http_error(response)
    assert isinstance(raised, CursorCloudStreamExpiredError)
    assert raised.cli_exit == 75
    assert "popola status" in raised.hint_en


def test_selector_dispatches_5xx_to_retryable_base_error() -> None:
    """``backend_5xx`` catalog entry binds to base ``CursorCloudError`` (retryable)."""
    response = _make_response(
        503,
        json_body={"error": {"code": "internal_error", "message": "oops"}},
    )
    raised = _map_http_error(response)

    assert type(raised) is CursorCloudError
    assert raised.is_retryable is True
    assert raised.status_code == 503
    assert raised.cli_exit == 75


def test_selector_status_only_fallback_with_no_json_body() -> None:
    """Selector falls back to HTTP-status-first matching when body is non-JSON."""
    response = _make_response(409, text="agent_busy")
    raised = _map_http_error(response)
    assert isinstance(raised, CursorCloudConflictError)
    assert raised.status_code == 409


def test_selector_422_message_pattern_repo_allowlist() -> None:
    """422 with no ``error.code`` but allow-list message regex → RepoAllowlistError."""
    response = _make_response(
        422,
        json_body={
            "detail": "repository not in allowed list — install Cursor GitHub App"
        },
    )
    raised = _map_http_error(response)
    assert isinstance(raised, RepoAllowlistError)
    assert raised.cli_exit == 78
    assert "https://github.com/apps/cursor" in raised.hint_en


def test_selector_422_message_pattern_github_app_missing() -> None:
    """422 with GitHub App missing message → GithubAppMissingError."""
    response = _make_response(
        422,
        json_body={
            "error": {
                "message": "Cursor GitHub App is not installed on this organization",
            },
        },
    )
    raised = _map_http_error(response)
    assert isinstance(raised, GithubAppMissingError)
    assert raised.cli_exit == 78
    assert "https://github.com/apps/cursor" in raised.hint_en


def test_selector_422_message_pattern_github_app_permission() -> None:
    """422 with permission-denied message → GithubAppPermissionError."""
    response = _make_response(
        422,
        json_body={
            "error": {
                "code": "feature_unavailable",
                "message": "GitHub App permission denied on this repo",
            },
        },
    )
    raised = _map_http_error(response)
    assert isinstance(raised, GithubAppPermissionError)
    assert raised.cli_exit == 78
    assert "https://cursor.com/docs/integrations/github.md" in raised.hint_en


def test_selector_422_unrecognized_falls_back_to_validation_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC (e) + No-Silent-Failures: 422 unmatched → CursorCloudValidationError + WARNING.

    A 422 body that misses both the code allow-list AND every regex in the
    catalog must (1) raise :class:`CursorCloudValidationError` (the explicit
    fallback) rather than the generic base, and (2) log the full body at
    ``WARNING`` so operators can debug gateway-emitted 422 responses.
    """
    response = _make_response(
        422,
        json_body={"detail": "totally novel gateway error shape"},
    )
    with caplog.at_level("WARNING", logger="popolaloom.adapters.cursor_cloud"):
        raised = _map_http_error(response)

    assert isinstance(raised, CursorCloudValidationError)
    assert raised.status_code == 422
    assert raised.cli_exit == 64
    warn_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("unrecognized 422" in r.getMessage() for r in warn_records), (
        "must log full body at WARNING per No-Silent-Failures rule"
    )


def test_selector_validation_error_bypasses_integration_when_message_does_not_match() -> None:
    """422 + ``code: validation_error`` with vanilla message → generic validation."""
    response = _make_response(
        422,
        json_body={
            "error": {
                "code": "validation_error",
                "message": "repos[0].url is required",
            },
        },
    )
    raised = _map_http_error(response)
    assert isinstance(raised, CursorCloudValidationError)
    assert not isinstance(raised, RepoAllowlistError), (
        "must not pick integration entry when only generic validation regex applies"
    )
    assert raised.cli_exit == 64


def test_selector_400_validation_dispatches_to_validation_error() -> None:
    """400 ``missing_body`` → CursorCloudValidationError (status fallback path)."""
    response = _make_response(
        400,
        json_body={"error": {"code": "missing_body"}},
    )
    raised = _map_http_error(response)
    assert isinstance(raised, CursorCloudValidationError)
    assert raised.status_code == 400
    assert raised.cli_exit == 64


def test_selector_429_rate_limit_is_retryable() -> None:
    response = _make_response(
        429,
        json_body={"error": {"code": "rate_limit_exceeded"}},
    )
    raised = _map_http_error(response)
    assert isinstance(raised, CursorCloudRateLimitError)
    assert raised.is_retryable is True
    assert raised.cli_exit == 75


def test_selector_corrupt_json_body_falls_back_to_status() -> None:
    """Body that fails ``response.json()`` does not crash the selector."""
    response = httpx.Response(404, content=b"<html>not json</html>")
    raised = _map_http_error(response)
    assert isinstance(raised, CursorCloudNotFoundError)
    assert raised.status_code == 404


def test_selector_legacy_envelope_shape_message_only() -> None:
    """Legacy ``{"error": "<title>", "message": "<text>"}`` is also tolerated."""
    response = _make_response(
        422,
        json_body={
            "error": "Unprocessable Entity",
            "message": "GitHub App is missing — install from https://github.com/apps/cursor",
        },
    )
    raised = _map_http_error(response)
    assert isinstance(raised, GithubAppMissingError)


def test_unknown_status_falls_through_to_base_error() -> None:
    """A status with no catalog entry returns base :class:`CursorCloudError`."""
    response = _make_response(418, text="i'm a teapot")
    raised = _map_http_error(response)
    assert type(raised) is CursorCloudError
    assert raised.status_code == 418
    assert raised.is_retryable is False


def test_existing_classes_keep_backward_compat_init_signature() -> None:
    """Existing v0.8.5 callers using positional ``message`` + status_code still work."""
    err = CursorCloudConflictError("conflict", status_code=409, is_retryable=False)
    assert err.status_code == 409
    assert err.is_retryable is False
    assert err.cli_exit == 102

    base = CursorCloudError("oops", status_code=500, is_retryable=True)
    assert base.status_code == 500
    assert base.is_retryable is True
    assert base.cli_exit == 1


def test_every_catalog_hint_contains_https_url() -> None:
    """AC §3.2 contract: every catalog hint embeds at least one ``https://...`` URL."""
    for entry_id, entry in _ERROR_CATALOG.items():
        assert _URL_PATTERN.search(str(entry["hint_en"])), (
            f"{entry_id}.hint_en missing URL"
        )
        assert _URL_PATTERN.search(str(entry["hint_zh"])), (
            f"{entry_id}.hint_zh missing URL"
        )


def test_every_catalog_subclass_resolves_in_module() -> None:
    """Sanity: every ``subclass`` name in catalog resolves to an importable class."""
    from popolaloom.adapters import cursor_cloud as mod

    for entry_id, entry in _ERROR_CATALOG.items():
        name = entry["subclass"]
        cls: object = getattr(mod, name, None)
        assert cls is not None, f"entry {entry_id} subclass {name!r} not exported"
        assert isinstance(cls, type), f"{name} is not a class"
        assert issubclass(cls, CursorCloudError), f"{name} must extend CursorCloudError"


def _all_status_codes_dispatched() -> Iterable[int]:
    """Collect distinct HTTP statuses with a dedicated catalog mapping (sanity)."""
    seen: set[int] = set()
    for entry in _ERROR_CATALOG.values():
        http = entry["http"]
        if isinstance(http, list):
            seen.update(int(x) for x in http)
        else:
            seen.add(int(http))
    return seen


def test_catalog_covers_all_documented_v1_status_codes() -> None:
    """Sanity: catalog spans 401/403/404/409/410/422/400/429/500/502/503/504."""
    coverage = set(_all_status_codes_dispatched())
    expected = {400, 401, 403, 404, 409, 410, 422, 429, 500, 502, 503, 504}
    missing = expected - coverage
    assert not missing, f"catalog missing dispatch for {sorted(missing)}"
