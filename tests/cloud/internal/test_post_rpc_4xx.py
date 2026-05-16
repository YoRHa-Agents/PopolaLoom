"""v1.3.0 P4 — ``_post_rpc`` honest 4xx Connect-Protocol envelope tests.

Validates the post-patch :func:`_post_rpc` surfacing logic. Pre-1.3.0 the
client mis-labeled every 4xx as either "JWT expired" (401) or "method
path missing" (404); the user observed a 400 ``invalid_argument`` body
being surfaced as the misleading 404 hint (see
``.local/feedbacks/feedback_for_v1.2.0.md`` §2 "实测 wire 规格"). P4
introduces:

- A Connect-Protocol error-envelope extractor that pulls
  ``(code, message, details)`` from the upstream JSON body.
- A discriminating ``error_kind`` tag on :class:`CursorCloudInternalError`
  so operators can distinguish ``path_b_rpc_400_invalid_argument`` from
  ``path_b_rpc_404``.
- Bilingual hints that explicitly tell operators what to do, and a
  ``__str__`` override that interleaves the structured fields into the
  log message so plain ``str(exc)`` is diagnosable.

Test wire-format and structure are reverse-engineered from the user's
field report; the live curl response was archived in feedback §2.
"""

from __future__ import annotations

import json as _json
import time
from typing import Any

import httpx
import pytest

from popolaloom.cloud.internal.cursor_cloud_internal import (
    CursorCloudInternalClient,
    CursorCloudInternalError,
)
from popolaloom.cloud.internal.jwt_auth import JWTBundle


def _fake_bundle() -> JWTBundle:
    """Build a minimal :class:`JWTBundle` mirroring ``test_rpc_mock.py``."""
    return JWTBundle(
        access_token="header.payload.sig",
        refresh_token="refresh-tok",
        source="env",
        path=None,
        exp_unix_s=int(time.time()) + 7200,
    )


def _mock_transport(
    status_code: int, body: dict[str, Any] | str | None
) -> httpx.MockTransport:
    """Return an ``httpx.MockTransport`` that replies with ``status_code`` + ``body``.

    When ``body`` is a ``dict`` we serialize it as JSON with the
    ``application/json`` content-type; ``str`` is sent raw; ``None``
    yields an empty body. This lets the test cover both happy-path
    Connect-Protocol envelopes and corrupt-body fallbacks.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        if isinstance(body, dict):
            return httpx.Response(
                status_code,
                content=_json.dumps(body),
                headers={"content-type": "application/json"},
            )
        if isinstance(body, str):
            return httpx.Response(status_code, content=body)
        return httpx.Response(status_code)

    return httpx.MockTransport(handler)


def test_400_invalid_argument_surfaces_details() -> None:
    """400 + ``invalid_argument`` envelope → structured fields surface.

    Verifies the most-impactful P4 fix: pre-1.3.0 this body produced a
    misleading 404-style "service path missing" message; post-patch the
    operator sees the actual upstream ``connect_code`` /
    ``connect_message`` / ``details_summary`` (feedback §2).
    """
    body = {
        "code": "invalid_argument",
        "message": "At least one model details is required",
        "details": [
            {
                "debug": {
                    "details": {"detail": "missing devcontainer_starting_point"}
                }
            }
        ],
    }
    client = httpx.Client(transport=_mock_transport(400, body))
    with (
        CursorCloudInternalClient(_fake_bundle(), http_client=client) as rpc,
        pytest.raises(CursorCloudInternalError) as exc_info,
    ):
        rpc._post_rpc(  # noqa: SLF001 — directly drive the wire layer.
            "StartBackgroundComposerFromSnapshot",
            {"prompt": "x"},
        )

    err = exc_info.value
    assert err.error_kind == "path_b_rpc_400_invalid_argument"
    assert err.connect_code == "invalid_argument"
    assert err.connect_message == "At least one model details is required"
    assert "missing devcontainer_starting_point" in (err.details_summary or "")
    assert err.status_code == 400
    rendered = str(err)
    assert "kind=path_b_rpc_400_invalid_argument" in rendered
    assert "connect_code=invalid_argument" in rendered


def test_404_hint_mentions_both_causes() -> None:
    """404 → hint cites BOTH (a) path renamed AND (b) validation fallback.

    Per feedback §2, Cursor's Connect-Protocol layer is known to surface
    body-validation failures as 404 — so a single-cause hint is
    misleading. The P4 hint must list both possibilities AND name the
    ``--auth-mode=rest`` escape hatch.
    """
    client = httpx.Client(
        transport=_mock_transport(404, {"code": "unimplemented"})
    )
    with (
        CursorCloudInternalClient(_fake_bundle(), http_client=client) as rpc,
        pytest.raises(CursorCloudInternalError) as exc_info,
    ):
        rpc._post_rpc(  # noqa: SLF001
            "StartBackgroundComposerFromSnapshot",
            {},
        )

    err = exc_info.value
    assert err.error_kind == "path_b_rpc_404"
    assert err.status_code == 404
    assert "method path" in err.hint or "方法路径" in err.hint
    assert "Connect-Protocol" in err.hint or "校验" in err.hint
    assert "--auth-mode=rest" in err.hint


def test_401_kind_and_hint() -> None:
    """401 → ``path_b_rpc_401_auth`` kind + ``cursor login`` hint."""
    client = httpx.Client(
        transport=_mock_transport(401, {"code": "unauthenticated"})
    )
    with (
        CursorCloudInternalClient(_fake_bundle(), http_client=client) as rpc,
        pytest.raises(CursorCloudInternalError) as exc_info,
    ):
        rpc._post_rpc(  # noqa: SLF001
            "StartBackgroundComposerFromSnapshot",
            {},
        )

    err = exc_info.value
    assert err.error_kind == "path_b_rpc_401_auth"
    assert err.status_code == 401
    assert "cursor login" in err.hint
