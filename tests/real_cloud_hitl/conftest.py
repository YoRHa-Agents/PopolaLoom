"""Shared fixtures for opt-in real cloud HITL E2E tests (v0.8.7 W2.3 T2.3.2).

This package is skipped at collection when **any** of the three required
environment variables is unset (empty or absent):

- ``CURSOR_API_KEY``         — opts into real Cursor Cloud Agents quota.
- ``LARK_HITL_TARGET_OPEN_ID`` — Lark ``open_id`` of the human approver who
  will receive (and click) the HITL card. The popolad daemon must have been
  launched with the same env var so its Lark notifier knows where to fan out.
- ``POPOLAD_BASE_URL``       — HTTP base URL (or ``unix://<sock>`` UDS path)
  of a running popolad instance with the cloud HITL bridge wired.

Setting all three opts into the **manual / monthly real-cloud E2E lane**
(see ``test_e2e.py`` module docstring for the full invocation recipe) per
Q-B-6 default in ``mcp-tool-contract.md`` §10. Mock E2E in default CI
lane is owned by sibling task T2.3.1 (``tests/e2e/test_cloud_hitl_mock.py``).

No silent failures — skip reasons explain how to enable the tier; per
workspace rule "secret 隔离" the lookup uses :func:`os.environ.get` only,
never reads from a config file.

Pattern mirrors ``tests/real_cursor_cloud/conftest.py`` verbatim (same
``pytest_collection_modifyitems`` skip mechanism, same fixture style).
"""

from __future__ import annotations

import os

import pytest

# Names of the three env vars that gate this tier; ordering preserved in skip
# messages so the operator sees them in the same order they appear in the
# test_e2e.py docstring's "Required environment variables" list.
REQUIRED_ENV_VARS: tuple[str, ...] = (
    "CURSOR_API_KEY",
    "LARK_HITL_TARGET_OPEN_ID",
    "POPOLAD_BASE_URL",
)

_COLLECTION_SKIP_REASON = (
    "real_cloud_hitl skipped: export "
    + " + ".join(REQUIRED_ENV_VARS)
    + " to enable (manual / monthly cadence per Q-B-6; "
    "real Lark webhook + popolad + cloud HITL round-trip; "
    "see tests/real_cloud_hitl/test_e2e.py docstring for the cold-start recipe)"
)


def _missing_env_vars() -> list[str]:
    """Return the names of any required env vars that are unset/empty."""
    return [
        name
        for name in REQUIRED_ENV_VARS
        if not os.environ.get(name, "").strip()
    ]


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip every test in this directory when any required env var is missing.

    Mirrors ``tests/real_cursor_cloud/conftest.py``'s collection-time skip
    so ``pytest`` (no marker) and ``pytest -m real_cloud_hitl`` both report
    the tests as **skipped** (NOT failed) in the agent / CI environment
    where the env vars are unset (per AC (j) of T2.3.2).

    The path / nodeid filter ensures we only mark items inside this package
    even though the hook receives every collected item across the whole
    test session.
    """
    if not _missing_env_vars():
        return
    skip_marker = pytest.mark.skip(reason=_COLLECTION_SKIP_REASON)
    for item in items:
        path_obj = getattr(item, "path", None)
        path_bits = getattr(path_obj, "parts", None)
        parts = tuple(path_bits) if path_bits else ()
        nodeid_s = getattr(item, "nodeid", "")
        looks_like_pkg = bool(parts) and "real_cloud_hitl" in parts
        fallback = isinstance(nodeid_s, str) and "real_cloud_hitl" in nodeid_s
        if not (looks_like_pkg or fallback):
            continue
        item.add_marker(skip_marker)


@pytest.fixture
def ensure_cloud_hitl_env() -> None:
    """Skip at runtime if any of the three required env vars is unset.

    Defense-in-depth — the collection-time skip in
    :func:`pytest_collection_modifyitems` should already prevent the test
    from running in an env without the vars, but this fixture catches the
    case where someone wires up the test with an explicit ``-p no:cacheprovider``
    or ``--collect-only`` that bypasses the collection hook.

    Per AC (b) of T2.3.2, this fixture **skips** (not fails) so the
    default agent CI lane stays green even when someone runs the directory
    explicitly (``pytest tests/real_cloud_hitl/``) without the env vars.
    """
    missing = _missing_env_vars()
    if missing:
        pytest.skip(
            f"real_cloud_hitl: missing required env vars {missing!r}; "
            "export them to enable real E2E "
            "(see tests/real_cloud_hitl/test_e2e.py docstring)"
        )
