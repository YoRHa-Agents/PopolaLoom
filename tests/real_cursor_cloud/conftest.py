"""Shared fixtures for opt-in Cursor Cloud Agent REST smoke tests.

This package is skipped at collection when ``CURSOR_API_KEY`` is unset
(empty or absent). Setting it opts into tests that consume **real** Cursor Cloud
API quota (see module docstrings on each case).

Override the target GitHub repo with ``POPOLA_TEST_CLOUD_REPO_URL`` when
needed (default public smoke repo).

No silent failures — skip reasons explain how to enable the tier.
"""

from __future__ import annotations

import os

import pytest

from popolaloom.adapters.cursor_cloud import CloudCursorClient

_COLLECTION_SKIP_REASON = (
    "real_cursor_cloud skipped: export CURSOR_API_KEY to enable (consumes Cursor "
    "Cloud API quota); POPOLA_TEST_CLOUD_REPO_URL overrides default repo URL"
)


def _cursor_api_key_from_env() -> str:
    return os.environ.get("CURSOR_API_KEY", "").strip()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every test in this directory when CURSOR_API_KEY is not set."""

    if _cursor_api_key_from_env():
        return
    skip_marker = pytest.mark.skip(reason=_COLLECTION_SKIP_REASON)
    for item in items:
        path_obj = getattr(item, "path", None)
        path_bits = getattr(path_obj, "parts", None)
        parts = tuple(path_bits) if path_bits else ()
        nodeid_s = getattr(item, "nodeid", "")
        looks_like_pkg = parts and "real_cursor_cloud" in parts
        fallback = isinstance(nodeid_s, str) and "real_cursor_cloud" in nodeid_s
        if not (looks_like_pkg or fallback):
            continue
        item.add_marker(skip_marker)


@pytest.fixture
def ensure_cursor_api_key() -> None:
    """Fails fast when a test reaches runtime without CURSOR_API_KEY (guard)."""

    if not _cursor_api_key_from_env():
        pytest.fail("CURSOR_API_KEY must be set for real_cursor_cloud tests")


@pytest.fixture
def test_repo_url() -> str:
    """GitHub HTTPS URL Cursor Cloud Agents can target."""

    raw = os.environ.get(
        "POPOLA_TEST_CLOUD_REPO_URL",
        "https://github.com/YoRHa-Agents/popola-cloud-smoketest",
    ).strip()
    if not raw:
        pytest.fail("POPOLA_TEST_CLOUD_REPO_URL, if set, must be non-empty")
    return raw


@pytest.fixture(scope="session")
def cursor_cloud_client() -> CloudCursorClient:
    """Synchronous REST client authenticated with CURSOR_API_KEY."""

    api_key = _cursor_api_key_from_env()
    if not api_key:
        pytest.skip(_COLLECTION_SKIP_REASON)
    client = CloudCursorClient(api_key)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="session")
def cloud_smoke_agent_run(
    cursor_cloud_client: CloudCursorClient,
    test_repo_url: str,
) -> tuple[str, str]:
    """Create one cloud agent, cancel its run immediately, return (agent_id, run_id)."""

    # This costs real Cursor API quota — opt in via CURSOR_API_KEY only.
    data = cursor_cloud_client.create_agent(
        "PopolaLoom tests/real_cursor_cloud: smoke job (immediate cancel)",
        model="composer-2",
        repo_url=test_repo_url,
        auto_create_pr=False,
        starting_ref="main",
    )
    agent = data.get("agent") or {}
    run = data.get("run") or {}
    agent_id = agent.get("id")
    run_id = run.get("id")
    if not agent_id or not run_id:
        pytest.fail(f"unexpected create_agent payload shape keys={sorted(data)!r}")
    cursor_cloud_client.cancel_run(agent_id, run_id)
    return agent_id, run_id
