"""Unit tests for :mod:`popolaloom.cloud.preflight` (v0.10.0 Q-3 + Q-9).

The preflight helpers (``check_self_hosted_worker_exists`` and
``check_github_app_installed``) are consumed by the v0.10.0
cloud-dispatch pipeline. The integration-style flows are exercised
indirectly via ``tests/cli/test_cloud_worker_dispatch_worker_existence.py``;
this file adds direct unit coverage so the pure helpers are testable
in isolation, push the package coverage above the 94% floor, and
serve as documentation for the contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from popolaloom.cloud.preflight import (
    GithubAppCheckResult,
    WorkerExistenceResult,
    check_github_app_installed,
    check_self_hosted_worker_exists,
)


class _FakeClient:
    """Minimal stub for :class:`CloudCursorClient` used by both helpers."""

    def __init__(
        self,
        *,
        workers: list[dict[str, Any]] | None = None,
        repositories_payload: Any = None,
    ) -> None:
        self._workers = workers or []
        self._payload = repositories_payload

    def list_workers(self) -> list[dict[str, Any]]:
        return list(self._workers)

    def _request_json(self, method: str, path: str) -> Any:  # noqa: ARG002
        return self._payload


# ── check_self_hosted_worker_exists ────────────────────────────────


def test_check_self_hosted_worker_empty_name_skips_lookup() -> None:
    """Empty name returns found=False without performing the HTTP call."""
    client = _FakeClient(workers=[{"name": "probe-w1", "is_in_use": False}])
    out = check_self_hosted_worker_exists(client, "")
    assert isinstance(out, WorkerExistenceResult)
    assert out.found is False
    assert out.worker is None
    assert "skipped" in out.message.lower()


def test_check_self_hosted_worker_found_and_free() -> None:
    """Found worker with is_in_use=False returns happy path."""
    client = _FakeClient(
        workers=[
            {
                "worker_id": "uuid-1",
                "name": "probe-w1",
                "is_in_use": False,
                "active_bc_id": None,
            }
        ]
    )
    out = check_self_hosted_worker_exists(client, "probe-w1")
    assert out.found is True
    assert out.is_in_use is False
    assert out.worker is not None
    assert out.worker["worker_id"] == "uuid-1"
    assert "free to claim" in out.message


def test_check_self_hosted_worker_found_but_busy_soft_signal() -> None:
    """is_in_use=True returns SOFT signal (Q-3: dispatch still succeeds, run queues)."""
    client = _FakeClient(
        workers=[
            {
                "worker_id": "uuid-2",
                "name": "probe-w2",
                "is_in_use": True,
                "active_bc_id": "bc-active",
            }
        ]
    )
    out = check_self_hosted_worker_exists(client, "probe-w2")
    assert out.found is True
    assert out.is_in_use is True
    assert "in use" in out.message
    assert "bc-active" in out.message


def test_check_self_hosted_worker_not_found_lists_registered() -> None:
    """Missing name lists the registered names in the message."""
    client = _FakeClient(
        workers=[
            {"name": "alpha", "is_in_use": False},
            {"name": "beta", "is_in_use": False},
        ]
    )
    out = check_self_hosted_worker_exists(client, "gamma")
    assert out.found is False
    assert "'alpha'" in out.message
    assert "'beta'" in out.message


def test_check_self_hosted_worker_duplicate_names_uses_first() -> None:
    """When multiple workers share a display name, the FIRST is returned (+ NOTE)."""
    client = _FakeClient(
        workers=[
            {
                "worker_id": "uuid-first",
                "name": "duplicate",
                "is_in_use": False,
            },
            {
                "worker_id": "uuid-second",
                "name": "duplicate",
                "is_in_use": True,
                "active_bc_id": "bc-x",
            },
        ]
    )
    out = check_self_hosted_worker_exists(client, "duplicate")
    assert out.found is True
    assert out.worker is not None
    assert out.worker["worker_id"] == "uuid-first"
    assert "2 registered workers share display name" in out.message


# ── check_github_app_installed ────────────────────────────────────


def test_check_github_app_empty_repo_url_skips() -> None:
    """Empty repo_url returns installed=None (no HTTP call)."""
    client = _FakeClient()
    out = check_github_app_installed(client, "")
    assert isinstance(out, GithubAppCheckResult)
    assert out.installed is None
    assert "skipped" in out.message.lower()


def test_check_github_app_non_github_host_returns_none() -> None:
    """Non-github.com URLs are out-of-scope; installed=None."""
    client = _FakeClient()
    out = check_github_app_installed(client, "https://gitlab.example.com/x/y")
    assert out.installed is None
    assert "gitlab" in out.message.lower() or "not 'github.com'" in out.message


def test_check_github_app_scheme_less_url_normalised() -> None:
    """Schemeless 'github.com/owner/name' is auto-prefixed with https://."""
    client = _FakeClient(repositories_payload={"items": [{"name": "x"}]})
    out = check_github_app_installed(client, "github.com/owner/name")
    assert out.installed is True


def test_check_github_app_installed_true_when_items_non_empty() -> None:
    """Non-empty items list = GitHub App is installed for some org."""
    client = _FakeClient(
        repositories_payload={
            "items": [
                {"name": "repo-1"},
                {"name": "repo-2"},
                {"name": "repo-3"},
            ]
        }
    )
    out = check_github_app_installed(client, "https://github.com/x/y")
    assert out.installed is True
    assert "3" in out.message


def test_check_github_app_installed_singular_message() -> None:
    """One repo → 'entry' singular grammar."""
    client = _FakeClient(repositories_payload={"items": [{"name": "x"}]})
    out = check_github_app_installed(client, "https://github.com/x/y")
    assert out.installed is True
    assert "entry" in out.message
    assert "entries" not in out.message


def test_check_github_app_installed_false_when_items_empty() -> None:
    """Empty items list → App not installed; message has the install URL."""
    client = _FakeClient(repositories_payload={"items": []})
    out = check_github_app_installed(client, "https://github.com/x/y")
    assert out.installed is False
    assert "cursor.com/integrations/github" in out.message


def test_check_github_app_raises_on_unexpected_payload_shape() -> None:
    """A non-dict payload (or a dict without 'items') raises CursorCloudError."""
    from popolaloom.adapters.cursor_cloud import CursorCloudError

    client = _FakeClient(repositories_payload={"wrong_key": []})
    with pytest.raises(CursorCloudError) as exc_info:
        check_github_app_installed(client, "https://github.com/x/y")
    assert "unexpected payload shape" in str(exc_info.value)


def test_check_github_app_raises_on_list_payload() -> None:
    """A top-level list (not dict) is also rejected."""
    from popolaloom.adapters.cursor_cloud import CursorCloudError

    client = _FakeClient(repositories_payload=[{"name": "x"}])
    with pytest.raises(CursorCloudError):
        check_github_app_installed(client, "https://github.com/x/y")


# ── v1.6.0 constraint #3: self-hosted target skips the GitHub-App gate ──


def test_check_github_app_installed_skipped_for_self_hosted() -> None:
    """v1.6.0 ``feedback_for_v1.5.2.md`` constraint #3: when the
    ``target`` kwarg is ``"self-hosted"`` the GitHub-App gate
    short-circuits to ``installed=None`` and the message explains the
    skip, even for a github.com URL that would otherwise probe
    ``GET /v1/repositories``.

    The client's ``_request_json`` must NOT be called — gate the
    behaviour at the function boundary so the contract holds for the
    Path-B ``cursor-cloud-internal`` transport too (it doesn't own
    ``_request_json``).
    """
    call_count = {"n": 0}

    class _SpyClient(_FakeClient):
        def _request_json(self, method: str, path: str) -> Any:
            call_count["n"] += 1
            return super()._request_json(method, path)

    client = _SpyClient(repositories_payload={"items": [{"name": "z"}]})
    out = check_github_app_installed(
        client,
        "https://github.com/owner/name",
        target="self-hosted",
    )
    assert out.installed is None
    assert call_count["n"] == 0, (
        "self-hosted target must short-circuit BEFORE calling _request_json "
        "(constraint #3); Path-B transports do not expose _request_json"
    )
    assert "self-hosted" in out.message
    assert "skipped" in out.message.lower()


def test_check_github_app_installed_managed_target_still_probes() -> None:
    """Non-self-hosted target preserves the v0.10.0 behaviour (probes the
    server). Empty repo_url early-exit still fires.
    """
    client = _FakeClient(
        repositories_payload={"items": [{"name": "z"}]},
    )
    out = check_github_app_installed(
        client,
        "https://github.com/x/y",
        target="cursor-managed",
    )
    assert out.installed is True

    out = check_github_app_installed(
        client,
        "https://github.com/x/y",
        target=None,
    )
    assert out.installed is True
