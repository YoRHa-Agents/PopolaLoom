"""Unit tests for the v0.9.9 F4 + v0.10.0 Q-9 catalog entries.

Covers the ``_ERROR_CATALOG`` entries that re-route Cursor REST's most
common operator-actionable 400/422 responses into bilingual hints.

v0.9.9 F4 introduced ``integration_github_app_branch_not_found`` (catalog
grew 16 → 17) — re-routes the misleading
``HTTP 400 validation_error: Failed to verify existence of branch '<x>'
in repository <org>/<repo>`` response (emitted when the Cursor GitHub
App is missing on the target org while ``auto_create_pr=true``) into the
same :class:`GithubAppMissingError` subclass already used by the sibling
422 ``integration_github_app_missing`` entry — per Q-V099-7.

v0.10.0 Q-9 (Wave C2) added two more entries (catalog grew 17 → 19):

- ``repository_required`` (HTTP 400 + ``error.code = "repository_required"``)
  — the gateway returns this when ``repos[]`` is missing entirely; routed
  to :class:`CursorCloudValidationError` with ``cli_exit=2`` (CLI usage).
- ``pr_resolution_failed`` (HTTP 400 + ``error.code = "pr_resolution_failed"``)
  — emitted when ``repos[0].prUrl`` targets a repo whose owning org has
  not installed the Cursor GitHub App. Reuses :class:`GithubAppMissingError`
  + ``cli_exit=78`` for ABI parity with the branch-validation variant.

v0.10.0 Q-9 also EXTENDED the ``integration_github_app_branch_not_found``
regex to match a SECOND missing-App message variant ``"Failed to determine
repository default branch"`` observed when no ``startingRef`` is provided
(research/02-path-1-visibility-probe.md §3.1 row 1).

The new entries are **insert-only** (no class minted, no
``_SUBCLASS_REGISTRY`` mutation) and positioned BEFORE ``validation_request_body``
so :func:`_score_entry`'s code-list bonus (+10) tips the selector toward
the more-specific entry whenever the codes hit.

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
    CursorCloudValidationError,
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
    # v0.10.0 (DECISIONS Q-9): the gateway emits this second variant when
    # ``startingRef`` is omitted AND the Cursor GitHub App is missing on
    # the owning org (research/02-path-1-visibility-probe.md §3.1 row 1).
    # Same actionable fix → same catalog entry.
    "Failed to determine repository default branch",
    "failed to determine repository default branch for "
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


def test_catalog_count_grew_to_19_v0_10_0() -> None:
    """AC3 (a): catalog grew 16 → 17 → 19 across v0.9.9 + v0.10.0.

    v0.9.9 F4 added ``integration_github_app_branch_not_found`` (16 → 17).
    v0.10.0 Q-9 added ``repository_required`` and ``pr_resolution_failed``
    (17 → 19). Renamed from ``test_catalog_count_grew_to_17`` so a grep
    for the old name surfaces this rename annotation.
    """
    assert _ENTRY_ID in _ERROR_CATALOG
    assert "repository_required" in _ERROR_CATALOG
    assert "pr_resolution_failed" in _ERROR_CATALOG
    assert len(_ERROR_CATALOG) == 19, (
        f"expected catalog count 19 after Q-9 inserts; got {len(_ERROR_CATALOG)}"
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


# ---------------------------------------------------------------------------
# v0.10.0 Q-9 — extended regex for ``integration_github_app_branch_not_found``
# matches BOTH the v0.9.9 string AND the new "Failed to determine repository
# default branch" variant.
# ---------------------------------------------------------------------------


def test_v0_10_0_extended_regex_matches_both_variants(
    entry: dict[str, object],
) -> None:
    """AC3 (b): extended regex matches BOTH v0.9.9 and v0.10.0 message variants.

    The v0.10.0 Q-9 expansion ORs in the second message variant
    ``"Failed to determine repository default branch"`` so a single
    catalog row covers both wire-level reports of the missing-Cursor-App
    failure mode (the actionable fix is identical for both variants —
    install the App at https://cursor.com/integrations/github).
    """
    pattern = entry["message_pattern"]
    assert isinstance(pattern, str)
    # v0.9.9 variant — branch validation flavor.
    assert re.search(
        pattern,
        "Failed to verify existence of branch 'main' in repository acme/foo",
    ), "v0.9.9 branch-validation message must continue matching"
    # v0.10.0 variant — default-branch determination flavor.
    assert re.search(
        pattern,
        "Failed to determine repository default branch",
    ), "v0.10.0 default-branch message must match the extended regex"


def test_v0_10_0_extended_regex_negative_examples(
    entry: dict[str, object],
) -> None:
    """AC3 (b) negative arm: the extended regex must NOT over-match siblings."""
    pattern = entry["message_pattern"]
    assert isinstance(pattern, str)
    not_matching = [
        "branch 'main' not found",
        "could not resolve branch 'main'",
        "repository acme/foo not accessible",
        "default branch is set to main",
    ]
    for msg in not_matching:
        assert not re.search(pattern, msg), (
            f"regex {pattern!r} unexpectedly matched negative variant: {msg!r}"
        )


# ---------------------------------------------------------------------------
# v0.10.0 Q-9 — new catalog entry ``repository_required``.
# ---------------------------------------------------------------------------


@pytest.fixture
def repository_required_entry() -> dict[str, object]:
    """Return the new ``repository_required`` catalog entry."""
    return _ERROR_CATALOG["repository_required"]


def test_repository_required_selects_on_400_with_code(
    repository_required_entry: dict[str, object],
) -> None:
    """AC3 (c): ``repository_required`` selects on HTTP 400 + matching ``error.code``.

    Per Q-9 the entry uses the explicit ``code`` list (not regex) so any
    400 with ``error.code = "repository_required"`` routes to the friendly
    "set --repo-url" hint.
    """
    response = _make_response(
        400,
        json_body={
            "error": {
                "code": "repository_required",
                "message": "no repository was specified for this dispatch",
            },
        },
    )
    raised = _map_http_error(response)
    assert isinstance(raised, CursorCloudValidationError)
    assert raised.status_code == 400
    assert raised.cli_exit == 2, (
        "Q-9 routes repository_required to cli_exit=2 (CLI usage error) "
        "so shell scripts can branch on the missing-repo case"
    )
    assert raised.is_retryable is False


def test_repository_required_entry_basic_shape(
    repository_required_entry: dict[str, object],
) -> None:
    """AC3 (c) shape: HTTP 400, no regex, ``cli_exit=2``, code in list."""
    assert repository_required_entry["http"] == 400
    assert repository_required_entry["message_pattern"] is None
    assert "repository_required" in repository_required_entry["code"]  # type: ignore[operator]
    assert repository_required_entry["cli_exit"] == 2
    assert repository_required_entry["subclass"] == "CursorCloudValidationError"
    assert repository_required_entry["retry"] is False


def test_repository_required_score_above_validation_request_body(
    repository_required_entry: dict[str, object],
) -> None:
    """AC3 (e): ``_score_entry`` selects ``repository_required`` over the generic 400 entry.

    Both entries declare ``http: 400`` but ``repository_required`` lists
    the explicit code (+10) while ``validation_request_body`` does NOT
    list ``"repository_required"`` in its code list — the latter scores
    ``None`` (not a candidate) and the former wins.
    """
    err_code = "repository_required"
    err_message = "no repository was specified"
    new_score = _score_entry(repository_required_entry, 400, err_code, err_message)
    validation_score = _score_entry(
        _ERROR_CATALOG["validation_request_body"], 400, err_code, err_message
    )
    assert new_score is not None, "repository_required must score on its own code"
    # validation_request_body lists ``["validation_error", "missing_body"]`` so
    # ``"repository_required"`` is NOT in its code list → not a candidate.
    assert validation_score is None, (
        "validation_request_body must NOT poach the repository_required code"
    )


# ---------------------------------------------------------------------------
# v0.10.0 Q-9 — new catalog entry ``pr_resolution_failed``.
# ---------------------------------------------------------------------------


@pytest.fixture
def pr_resolution_failed_entry() -> dict[str, object]:
    """Return the new ``pr_resolution_failed`` catalog entry."""
    return _ERROR_CATALOG["pr_resolution_failed"]


def test_pr_resolution_failed_selects_on_400_with_code(
    pr_resolution_failed_entry: dict[str, object],
) -> None:
    """AC3 (d): ``pr_resolution_failed`` selects on HTTP 400 + that code → ``cli_exit=78``.

    Reuses :class:`GithubAppMissingError` for ABI parity with the sibling
    ``integration_github_app_branch_not_found`` entry — the actionable
    fix is identical (install / configure the Cursor GitHub App).
    """
    response = _make_response(
        400,
        json_body={
            "error": {
                "code": "pr_resolution_failed",
                "message": "could not resolve PR https://github.com/acme/app/pull/1",
            },
        },
    )
    raised = _map_http_error(response)
    assert isinstance(raised, GithubAppMissingError)
    assert raised.status_code == 400
    assert raised.cli_exit == 78, (
        "Q-9 routes pr_resolution_failed to cli_exit=78 for parity with "
        "the sibling integration_github_app_branch_not_found entry"
    )
    assert raised.is_retryable is False


def test_pr_resolution_failed_entry_basic_shape(
    pr_resolution_failed_entry: dict[str, object],
) -> None:
    """AC3 (d) shape: HTTP 400, no regex, ``cli_exit=78``, ``GithubAppMissingError`` subclass."""
    assert pr_resolution_failed_entry["http"] == 400
    assert pr_resolution_failed_entry["message_pattern"] is None
    assert "pr_resolution_failed" in pr_resolution_failed_entry["code"]  # type: ignore[operator]
    assert pr_resolution_failed_entry["cli_exit"] == 78
    assert pr_resolution_failed_entry["subclass"] == "GithubAppMissingError"
    assert pr_resolution_failed_entry["retry"] is False


def test_pr_resolution_failed_hint_mentions_github_app(
    pr_resolution_failed_entry: dict[str, object],
) -> None:
    """AC3 (d) hint shape: bilingual hints both mention the Cursor GitHub App fix."""
    hint_en = pr_resolution_failed_entry["hint_en"]
    hint_zh = pr_resolution_failed_entry["hint_zh"]
    assert isinstance(hint_en, str)
    assert isinstance(hint_zh, str)
    # English hint must surface the install URL the operator should click.
    assert "https://cursor.com/integrations/github" in hint_en
    assert "Cursor GitHub App" in hint_en
    # Chinese hint must also mention the App and the URL.
    assert "https://cursor.com/integrations/github" in hint_zh
    assert "Cursor GitHub App" in hint_zh


# ---------------------------------------------------------------------------
# AC3 (e): ``_score_entry`` selects the more-specific rule when multiple
# could match. Cross-cuts the new and existing entries.
# ---------------------------------------------------------------------------


def test_score_entry_picks_branch_not_found_over_validation_for_v0_10_0_message(
    entry: dict[str, object],
    validation_entry: dict[str, object],
) -> None:
    """AC3 (e): the extended regex catches the new variant AND beats the generic body entry.

    Specifically: a 400 carrying ``code: validation_error`` AND
    ``message: "Failed to determine repository default branch"`` must
    score the ``integration_github_app_branch_not_found`` entry strictly
    above the generic ``validation_request_body`` entry — the regex hit
    contributes +5 over the baseline +10 code-match shared by both.
    """
    err_code = "validation_error"
    err_message = "Failed to determine repository default branch"

    new_score = _score_entry(entry, 400, err_code, err_message)
    val_score = _score_entry(validation_entry, 400, err_code, err_message)

    assert new_score is not None
    assert val_score is not None
    assert new_score > val_score, (
        f"new entry score ({new_score}) must beat "
        f"validation_request_body ({val_score}) for the v0.10.0 variant"
    )
    # Same arithmetic as the v0.9.9 variant test below: +1 baseline + +10
    # code list hit + +5 regex hit = 16; vs +1 + +10 = 11.
    assert new_score == 16
    assert val_score == 11


def test_score_entry_picks_pr_resolution_failed_over_validation_400(
    pr_resolution_failed_entry: dict[str, object],
    validation_entry: dict[str, object],
) -> None:
    """AC3 (e): ``pr_resolution_failed`` wins over generic 400 for its code.

    Unlike the regex-match case, this is a code-list-only differentiator:
    ``pr_resolution_failed`` scores +10 (code-list hit) but the generic
    ``validation_request_body`` entry's code list does NOT include
    ``"pr_resolution_failed"`` → not a candidate (returns ``None``).
    """
    err_code = "pr_resolution_failed"
    err_message = "could not resolve PR"

    new_score = _score_entry(
        pr_resolution_failed_entry, 400, err_code, err_message
    )
    val_score = _score_entry(validation_entry, 400, err_code, err_message)

    assert new_score is not None
    assert val_score is None, (
        "validation_request_body must NOT poach the pr_resolution_failed code"
    )


def test_repository_required_positioned_before_validation_request_body() -> None:
    """v0.10.0 Q-9: ``repository_required`` precedes the generic 400 entry by convention."""
    keys = list(_ERROR_CATALOG.keys())
    repo_idx = keys.index("repository_required")
    val_idx = keys.index("validation_request_body")
    assert repo_idx < val_idx, (
        f"'repository_required' (idx={repo_idx}) must precede "
        f"'validation_request_body' (idx={val_idx})"
    )


def test_pr_resolution_failed_positioned_before_validation_request_body() -> None:
    """v0.10.0 Q-9: ``pr_resolution_failed`` precedes the generic 400 entry."""
    keys = list(_ERROR_CATALOG.keys())
    pr_idx = keys.index("pr_resolution_failed")
    val_idx = keys.index("validation_request_body")
    assert pr_idx < val_idx, (
        f"'pr_resolution_failed' (idx={pr_idx}) must precede "
        f"'validation_request_body' (idx={val_idx})"
    )
