"""Minimal live API checks for Cursor Cloud Agents (Tier 4+).

Each ``@pytest.mark.real_cursor_cloud`` test **costs real Cursor Cloud API quota**
once ``CURSOR_API_KEY`` is set; without it, pytest collects tests and skips
them (see ``conftest.py``).
"""

from __future__ import annotations

import pytest

from popolaloom.adapters.cursor_cloud import CloudCursorClient, CursorCloudAuthError

pytestmark = [
    pytest.mark.real_cursor_cloud,
    pytest.mark.usefixtures("ensure_cursor_api_key"),
]


def test_create_and_immediately_cancel_smoke(cloud_smoke_agent_run: tuple[str, str]) -> None:
    """Create + immediate cancel exercises POST /agents + POST .../cancel (cheapest smoke)."""

    # This piggybacks on cloud_smoke_agent_run (session fixture) which performs real API IO.
    agent_id, run_id = cloud_smoke_agent_run
    assert agent_id.startswith("bc-")
    assert isinstance(run_id, str) and len(run_id) > 0


def test_get_agent_returns_metadata_smoke(
    cursor_cloud_client: CloudCursorClient,
    cloud_smoke_agent_run: tuple[str, str],
) -> None:
    """GET agent after create confirms metadata survives cancel path."""

    agent_id, _ = cloud_smoke_agent_run
    body = cursor_cloud_client.get_agent(agent_id)
    got = body.get("id", "")
    assert str(got).startswith("bc-"), f"id field unexpected shape: {sorted(body)!r}"


def test_get_run_returns_status_smoke(
    cursor_cloud_client: CloudCursorClient,
    cloud_smoke_agent_run: tuple[str, str],
) -> None:
    """GET run asserts the API exposes a recognizable status field."""

    agent_id, run_id = cloud_smoke_agent_run
    body = cursor_cloud_client.get_run(agent_id, run_id)
    assert isinstance(body.get("status"), str), (
        "run GET must include human-readable status "
        f"field; got keys={sorted(body)!r}"
    )


def test_auth_failure_distinguished(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid API key raises CursorCloudAuthError (sanity-check error mapping)."""

    monkeypatch.setenv("CURSOR_API_KEY", "invalid")
    client = CloudCursorClient("invalid")
    try:
        with pytest.raises(CursorCloudAuthError):
            client.create_agent(
                "x",
                model="composer-2",
                repo_url="https://github.com/YoRHa-Agents/popola-cloud-smoketest",
            )
    finally:
        client.close()
