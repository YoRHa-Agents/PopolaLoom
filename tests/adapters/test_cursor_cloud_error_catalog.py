"""Unit tests for v0.9.9 F4 — ``integration_github_app_branch_not_found`` entry.

Covers the new ``_ERROR_CATALOG`` entry that re-routes Cursor REST's misleading
``HTTP 400 validation_error: Failed to verify existence of branch '<x>' in
repository <org>/<repo>`` response (emitted when the Cursor GitHub App is
missing on the target org while ``auto_create_pr=true``) into the same
:class:`GithubAppMissingError` subclass already used by the sibling 422
``integration_github_app_missing`` entry — per Q-V099-7.

The entry is **insert-only** (no class minted, no
``_SUBCLASS_REGISTRY`` mutation, catalog grows 16 → 17) and is positioned
**before** ``validation_request_body`` so :func:`_score_entry`'s regex bonus
(+5) tips the selector toward the more-specific entry whenever the misleading
branch-verification message hits.

Test layout follows the existing ``tests/adapters/test_cursor_cloud.py`` /
``tests/adapters/test_cursor_cloud_coverage.py`` style: pure-function unit
tests with synthetic ``httpx.Response`` objects (no respx / network mocks
needed since the catalog selector is body-driven).
"""

from __future__ import annotations

import re

import httpx
import pytest

from popolaloom.adapters.cursor_cloud import (
    _ERROR_CATALOG,
    GithubAppMissingError,
    _build_error,
    _map_http_error,
    _score_entry,
)

_ENTRY_ID = "integration_github_app_branch_not_found"


@pytest.fixture
def entry() -> dict[str, object]:
    """Return the new catalog entry by id (raises KeyError if missing)."""
    return _ERROR_CATALOG[_ENTRY_ID]


@pytest.fixture
def validation_entry() -> dict[str, object]:
    """Return the sibling generic ``validation_request_body`` entry."""
    return _ERROR_CATALOG["validation_request_body"]


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


# ---------------------------------------------------------------------------
# (a) Regex must match all 3 positive variants (case + quote-style mix).
# ---------------------------------------------------------------------------

_POSITIVE_MESSAGES: tuple[str, ...] = (
    "Failed to verify existence of branch 'main' in repository "
    "YoRHa-Agents/PopolaLoom",
    'Failed to verify existence of branch "main" in repository '
    "YoRHa-Agents/PopolaLoom",
    "FAILED TO VERIFY EXISTENCE OF BRANCH 'Main' in repository "
    "YoRHa-Agents/PopolaLoom",
)


@pytest.mark.parametrize("message", _POSITIVE_MESSAGES)
def test_regex_matches_positive_variants(
    entry: dict[str, object],
    message: str,
) -> None:
    """AC #1 (positive arm): regex must match upper / mixed / quote-style variants.

    The ``(?i)`` inline flag handles case sensitivity (``Failed`` vs
    ``FAILED``) and the ``.+`` between ``branch`` and ``in repository``
    swallows both ASCII apostrophes and ASCII double-quotes (Unicode smart
    quotes would also be covered by ``.+`` since it is non-anchored).
    """
    pattern = entry["message_pattern"]
    assert isinstance(pattern, str), (
        f"message_pattern must be a string regex (was {type(pattern)!r})"
    )
    assert re.search(pattern, message), (
        f"regex {pattern!r} failed to match positive variant: {message!r}"
    )


# ---------------------------------------------------------------------------
# (b) Regex must REJECT 3 lookalike strings (so it does not over-match).
# ---------------------------------------------------------------------------

_NEGATIVE_MESSAGES: tuple[str, ...] = (
    "branch 'main' not found",
    "could not resolve branch 'main'",
    "repository acme/foo not accessible",
)


@pytest.mark.parametrize("message", _NEGATIVE_MESSAGES)
def test_regex_rejects_negative_variants(
    entry: dict[str, object],
    message: str,
) -> None:
    """AC #1 (negative arm): regex must NOT match plausible-looking alternates.

    Lookalike strings (``branch 'main' not found`` etc.) describe genuinely
    missing branches or repos that the new entry must NOT poach — the
    operator hint would be misleading in those cases.
    """
    pattern = entry["message_pattern"]
    assert isinstance(pattern, str)
    assert not re.search(pattern, message), (
        f"regex {pattern!r} unexpectedly matched negative variant: {message!r}"
    )


# ---------------------------------------------------------------------------
# (c) _score_entry must rank the new entry strictly above
#     validation_request_body when both could match the same response body.
# ---------------------------------------------------------------------------


def test_score_entry_prefers_new_entry_over_validation_request_body(
    entry: dict[str, object],
    validation_entry: dict[str, object],
) -> None:
    """AC #4: regex match (+5) must dominate the generic body fallback.

    Both entries declare ``code: validation_error`` and ``http: 400``, so the
    only differentiator is the new entry's ``message_pattern`` regex. Verify
    the score margin is exactly 5 (the documented :func:`_score_entry`
    contribution) and that the new entry is the strict winner.
    """
    err_code = "validation_error"
    err_message = (
        "Failed to verify existence of branch 'main' in repository "
        "YoRHa-Agents/PopolaLoom"
    )

    new_score = _score_entry(entry, 400, err_code, err_message)
    validation_score = _score_entry(validation_entry, 400, err_code, err_message)

    assert new_score is not None, "new entry must score for the misleading 400"
    assert validation_score is not None, (
        "sibling validation_request_body must still score (no regex constraint)"
    )
    assert new_score > validation_score, (
        f"new entry score ({new_score}) must beat "
        f"validation_request_body ({validation_score}) per _score_entry contract"
    )
    # +1 baseline + +10 code list hit + +5 regex hit = 16 for the new entry.
    # +1 baseline + +10 code list hit (no regex constraint)       = 11 for the
    # generic body entry.
    assert new_score == 16
    assert validation_score == 11
    assert new_score - validation_score == 5


# ---------------------------------------------------------------------------
# (d) _build_error / _map_http_error dispatch the 400 response to the
#     reused GithubAppMissingError subclass.
# ---------------------------------------------------------------------------


def test_build_error_returns_github_app_missing_subclass(
    entry: dict[str, object],
) -> None:
    """AC #2: ``_build_error`` instantiates :class:`GithubAppMissingError` (no new class)."""
    response = _make_response(
        400,
        json_body={
            "error": {
                "code": "validation_error",
                "message": (
                    "Failed to verify existence of branch 'main' in repository "
                    "YoRHa-Agents/PopolaLoom"
                ),
            },
        },
    )

    raised = _build_error(entry, 400, response)
    assert isinstance(raised, GithubAppMissingError), (
        "subclass dispatch must reuse GithubAppMissingError per Q-V099-7 "
        f"(got {type(raised).__name__})"
    )
    assert raised.status_code == 400
    assert raised.is_retryable is False


def test_map_http_error_routes_misleading_400_to_github_app_missing() -> None:
    """End-to-end: the full :func:`_map_http_error` selector picks the new entry.

    Exercises the full precedence chain (status filter → code list → regex
    score) and confirms the user sees :class:`GithubAppMissingError` rather
    than the generic :class:`CursorCloudValidationError` that the sibling
    400 entry would produce.
    """
    response = _make_response(
        400,
        json_body={
            "error": {
                "code": "validation_error",
                "message": (
                    "Failed to verify existence of branch 'main' in "
                    "repository YoRHa-Agents/PopolaLoom"
                ),
            },
        },
    )

    raised = _map_http_error(response)
    assert isinstance(raised, GithubAppMissingError)
    assert raised.status_code == 400
    assert raised.cli_exit == 78


# ---------------------------------------------------------------------------
# (e) Bilingual hint must mention the workaround sentence + both GitHub App
#     URLs (operator parity with the sibling 422 entry).
# ---------------------------------------------------------------------------


def test_hint_en_includes_workaround_and_both_urls(
    entry: dict[str, object],
) -> None:
    """AC #3 (English hint): both URLs + ``auto_create_pr=false`` workaround present."""
    hint_en = entry["hint_en"]
    assert isinstance(hint_en, str)
    assert "https://cursor.com/integrations/github" in hint_en
    assert "https://github.com/apps/cursor" in hint_en
    assert "auto_create_pr=false" in hint_en
    assert "Cursor GitHub App" in hint_en, (
        "must reference the misclassification root-cause explicitly"
    )


def test_hint_zh_includes_workaround_and_url(
    entry: dict[str, object],
) -> None:
    """AC #3 (Chinese hint): workaround sentence + integration URL present.

    The Chinese hint is a shorter operator summary (does NOT need to repeat
    both URLs) but MUST keep the workaround sentence + the primary integration
    URL the operator should click.
    """
    hint_zh = entry["hint_zh"]
    assert isinstance(hint_zh, str)
    assert "https://cursor.com/integrations/github" in hint_zh
    assert "auto_create_pr=false" in hint_zh
    assert "Cursor GitHub App" in hint_zh


# ---------------------------------------------------------------------------
# (f) cli_exit == 78 — parity with the sibling 422
#     ``integration_github_app_missing`` entry.
# ---------------------------------------------------------------------------


def test_cli_exit_matches_sibling_422_entry(
    entry: dict[str, object],
) -> None:
    """AC #5: ``cli_exit`` must equal the 422 sibling for operator parity."""
    sibling = _ERROR_CATALOG["integration_github_app_missing"]
    assert entry["cli_exit"] == sibling["cli_exit"] == 78


# ---------------------------------------------------------------------------
# Catalog growth + insertion-only invariant.
# ---------------------------------------------------------------------------


def test_catalog_count_grew_to_17() -> None:
    """AC #2 (catalog growth): the new entry brings count from 16 to 17."""
    assert _ENTRY_ID in _ERROR_CATALOG
    assert len(_ERROR_CATALOG) == 17, (
        f"expected catalog count 17 after F4 insert; got {len(_ERROR_CATALOG)}"
    )


def test_new_entry_positioned_before_validation_request_body() -> None:
    """Insertion order must place the new entry BEFORE the generic 400.

    Selector tiebreaking does not depend on order (the new entry's score is
    strictly higher), but the position is documented in the source comment
    and a reader-facing convention. Verify the order in case a future refactor
    inadvertently moves it.
    """
    keys = list(_ERROR_CATALOG.keys())
    new_idx = keys.index(_ENTRY_ID)
    val_idx = keys.index("validation_request_body")
    assert new_idx < val_idx, (
        f"{_ENTRY_ID!r} (idx={new_idx}) must precede "
        f"'validation_request_body' (idx={val_idx})"
    )


def test_new_entry_has_required_catalog_fields(
    entry: dict[str, object],
) -> None:
    """Sanity check: the new entry exposes the same shape as every catalog row."""
    required = {
        "http",
        "code",
        "message_pattern",
        "subclass",
        "retry",
        "cli_exit",
        "hint_en",
        "hint_zh",
    }
    missing = required - set(entry.keys())
    assert not missing, f"new entry missing fields {missing}"
    assert entry["http"] == 400
    assert entry["subclass"] == "GithubAppMissingError"
    assert entry["retry"] is False
