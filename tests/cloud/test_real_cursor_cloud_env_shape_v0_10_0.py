"""Tier-4 live smoke for the v0.10.0 env-shape pivot (DECISIONS Q-12, PLAN D1).

These tests exercise the **real** Cursor Cloud REST API
(``https://api.cursor.com/{v0,v1}/...``) and consume real quota. Each test
is double-gated:

1. ``@pytest.mark.real_cursor_cloud`` (registered in ``pyproject.toml``
   line 151) — opt-in marker so default ``pytest`` runs ignore the file
   unless ``-m real_cursor_cloud`` is passed.
2. The session-scoped ``live_client`` fixture calls ``pytest.skip(...)``
   when ``CURSOR_API_KEY`` is unset, so even an explicit ``-m
   real_cursor_cloud`` run skips cleanly when the secret is absent
   (No-Silent-Failures: the skip reason explains the prerequisite).

What is covered (per PLAN.md D1 AC 3-7):

- **Test 1** ``test_minimum_config_201_with_dashboard_url`` — minimum-config
  POST (``prompt + repos[{url, startingRef}]`` with NO ``env`` field)
  returns a Dashboard URL ``https://cursor.com/agents/bc-...``.
- **Test 2** ``test_env_machine_201_with_dashboard_url`` — POST with
  ``env={type:"machine", name:<probe-worker>}`` returns a Dashboard URL
  AND the response body echoes ``env.type == "machine"`` (the env-shape
  pivot's wire-truth).
- **Test 3** ``test_github_app_preflight_refusal_when_repositories_empty``
  — for a ``github.com`` repo URL with no Cursor GitHub App installed
  for this account, :func:`cloud.preflight.check_github_app_installed`
  returns ``installed=False`` AND the subsequent ``create_agent`` raises
  :class:`GithubAppMissingError` (NOT a generic ``CursorCloudError`` /
  400) — i.e. the early-refuse and late-catch paths produce the same
  typed error.
- **Test 4** ``test_cleanup_archives_each_created_agent`` — exercises the
  same ``_request_json("POST", .../archive)`` cleanup primitive the session
  teardown uses, then asserts ``GET /v1/agents?includeArchived=false``
  no longer lists the archived ``bc-*`` id (so the cleanup contract is
  observably enforced from within the test, not just at teardown).
- **Test 5** ``test_workers_list_includes_probe_worker_when_started`` —
  additionally gated by ``POPOLA_PROBE_WORKER_NAME``; asserts
  :meth:`CloudCursorClient.list_workers` returns a row whose ``name``
  equals that env value (so the operator can verify the
  ``GET /v0/private-workers`` plumbing against a known worker).

Cleanup (PLAN.md D1 AC 6): the session-scoped ``live_client`` fixture
yields a ``(client, created_agents)`` tuple. Every test that creates a
``bc-*`` agent appends the id to ``created_agents`` BEFORE making any
other assertion, so a failing assertion later in the same test still
gets cleaned up. Teardown iterates ``created_agents`` and calls
``client._request_json("POST", f"/v1/agents/{id}/archive")`` on each id;
individual archive failures are logged at WARN (best-effort sweep — one
stale id never blocks the remaining cleanups). Per the No-Silent-Failures
workspace rule, every swallowed exception is logged with full context
including the offending agent id.

Cost-control (PLAN.md D1 AC 8 — budget ≤ 10 POST + 10 archive per
session): the 5 tests issue at most **3** ``POST /v1/agents`` calls
(Tests 1, 2, 4) and at most **4** ``POST /v1/agents/{id}/archive`` calls
(Test 4 inline + teardown of Tests 1+2; Test 4's cleanup id is removed
from the tracker so teardown does not double-archive). Test 3 raises
BEFORE its POST — it costs one ``GET /v1/repositories`` (the pre-flight)
plus zero POSTs. Test 5 calls only ``GET /v0/private-workers``. The
session also issues one ``GET /v1/me`` at fixture setup as a sanity
check (the result is logged but not asserted, so an upstream schema
drift on ``/v1/me`` cannot break the smoke).

References:

- ``.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md`` Q-12
- ``.local/.agent/active/v0.10.0-cloud-dispatch-clarity/PLAN.md`` §"Wave D
  → Task D1"
- ``.local/.agent/active/v0.10.0-cloud-dispatch-clarity/research/``
  ``01-path-2-live-probe.md`` §"Reproduction" L34-79 (the verbatim curl
  that returns 201) and §"GitHub-App branch-validation gotcha" L161-165
  (why a personal key with no installed GitHub App refuses
  ``github.com/...`` URLs)
- ``pyproject.toml`` line 151 (``real_cursor_cloud`` mark registration)
"""

from __future__ import annotations

import logging
import os
from collections.abc import Generator
from typing import Any

import pytest

from popolaloom.adapters.cursor_cloud import (
    CloudCursorClient,
    CursorCloudAuthError,
    GithubAppMissingError,
)
from popolaloom.cloud.preflight import check_github_app_installed

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.real_cursor_cloud


# Stable repo URL known to work on this account (research/01 §"Reproduction"
# L34-79: the only repo with a working integration for the personal API key
# in ``.local/.secrets/``). Non-``github.com`` host means the GitHub-App
# pre-flight returns ``installed=None`` (skip case) so Tests 1+2 do not
# pay the ``GET /v1/repositories`` round-trip.
_TEST_REPO_URL = "git.neodrive.neolix.net/nexis_ai/evaluation/evobench"

# Public GitHub repo used as the pre-flight refusal target — research/01
# PROBE_03 confirms ``GET /v1/repositories`` returns ``{"items": []}`` for
# this personal key, so :func:`check_github_app_installed` reports
# ``installed=False`` and the subsequent dispatch raises
# :class:`GithubAppMissingError` from the early-refuse path.
_GITHUB_TEST_REPO_URL = "https://github.com/octocat/Hello-World"

# Distinctive prompt prefix so an operator inspecting Cursor's Dashboard
# can attribute leftover smoke agents to this test file. The prefix never
# influences the cleanup correctness (which is id-based) — it only aids
# manual diagnosis if the teardown ever fails to archive an agent.
_TEST_PROMPT_PREFIX = "popola-loom v1.5.0 REST env=machine smoke probe - no-op, ignore"

# Default probe-worker name when ``POPOLA_PROBE_WORKER_NAME`` is unset.
# Used only by Test 2 (``env={type:'machine', name:<X>}``); the gateway
# accepts any ``name`` string per research/01 PROBE_34 (it does NOT
# pre-validate worker existence at schema time), so Test 2 succeeds even
# when no real worker matches this name. Test 5 separately asserts the
# name resolves to a real worker — and skips when the env var is unset.
_DEFAULT_PROBE_WORKER_NAME = "popola-probe-w1"

# Model id passed to ``create_agent``. ``composer-2`` used to be the
# stable live-smoke model, but Cursor's REST gateway now rejects it with
# ``invalid_model``. v1.5.0 validation uses the same model requested by
# the self-hosted-worker handoff so the probe reaches the env-routing
# assertion instead of failing at model validation.
_TEST_MODEL_ID = "gpt-5.5"


def _api_key_or_skip() -> str:
    """Return ``CURSOR_API_KEY`` (stripped) or ``pytest.skip`` if unset.

    Centralised so Test 5's additional ``POPOLA_PROBE_WORKER_NAME`` gate
    can call :func:`pytest.skip` with a different message while reusing
    the primary key check via the shared fixture.
    """
    api_key = os.environ.get("CURSOR_API_KEY", "").strip()
    if not api_key:
        pytest.skip(
            "CURSOR_API_KEY not set; export it to enable the v0.10.0 D1 "
            "live smoke (consumes real Cursor API quota — at most 10 POST "
            "+ 10 DELETE per session per PLAN.md D1 AC 8). The personal "
            "key at .local/.secrets/cursor_user_api_key.secret is suitable."
        )
    return api_key


@pytest.fixture(scope="session")
def live_client() -> Generator[tuple[CloudCursorClient, list[str]], None, None]:
    """Session-scoped (client, created_agents) pair with auto-archive teardown.

    Per PLAN.md D1 AC 6 — the teardown calls
    ``client._request_json("POST", f"/v1/agents/{id}/archive")`` on every
    ``bc-*`` id appended to ``created_agents`` during the session. Tests
    that create an agent MUST append the id BEFORE any other assertion
    so the teardown still fires when an assertion later in the same
    test fails.

    The fixture also issues one ``GET /v1/me`` at setup as an
    api-key sanity check; the result is only logged (per Q-1: detection
    is informational), so upstream schema drift on ``/v1/me`` does not
    break the smoke.
    """
    api_key = _api_key_or_skip()
    created_agents: list[str] = []
    with CloudCursorClient(api_key) as client:
        try:
            try:
                me_info = client.me()
                logger.info(
                    "v0.10.0 D1 smoke session begin — api_key_class=%r "
                    "user_email=%r user_id=%r",
                    me_info.get("api_key_class"),
                    me_info.get("user_email"),
                    me_info.get("user_id"),
                )
            except CursorCloudAuthError as exc:
                # Per v0.10.0 release-gate UX: when CURSOR_API_KEY is set
                # but rejected at /v1/me (401/403), the entire smoke is
                # uninformative — every subsequent /v1/agents call would
                # also 401. Skip the session loudly with a fix hint rather
                # than report N false-positive failures. This is NOT a
                # silent failure: the skip reason carries the exact upstream
                # error so the operator sees what's wrong.
                pytest.skip(
                    "CURSOR_API_KEY rejected by Cursor REST at /v1/me "
                    f"({exc}); rotate the key at "
                    "https://cursor.com/dashboard/integrations and re-run."
                )
            except Exception as exc:
                # Other errors (timeouts, 5xx, schema drift on /v1/me)
                # are transient — log and proceed; the individual tests
                # will surface a real auth issue at their own /v1/agents
                # call site (No Silent Failures — failure does surface).
                logger.warning(
                    "v0.10.0 D1 smoke setup: client.me() pre-check failed "
                    "(%s); proceeding anyway — first /v1/agents call will "
                    "surface a real auth issue if the key is broken",
                    exc,
                )
            yield client, created_agents
        finally:
            failures: list[tuple[str, Exception]] = []
            for agent_id in list(created_agents):
                try:
                    client._request_json(  # noqa: SLF001
                        "POST",
                        f"/v1/agents/{agent_id}/archive",
                    )
                except Exception as exc:
                    # Best-effort sweep: log every cleanup failure with the
                    # offending agent id so the operator can manually clean
                    # up out-of-band, then continue archiving the rest.
                    # Per the No-Silent-Failures rule we record the error
                    # explicitly rather than dropping it on the floor.
                    failures.append((agent_id, exc))
                    logger.warning(
                        "v0.10.0 D1 smoke teardown: archive of agent %s "
                        "failed (best-effort sweep — manual cleanup may "
                        "be required at https://cursor.com/agents): %s",
                        agent_id,
                        exc,
                    )
            if failures:
                logger.warning(
                    "v0.10.0 D1 smoke teardown summary: %d/%d agent "
                    "archives failed; remaining ids: %s",
                    len(failures),
                    len(created_agents),
                    [aid for aid, _ in failures],
                )
            else:
                logger.info(
                    "v0.10.0 D1 smoke teardown summary: archived all %d "
                    "tracked agents successfully",
                    len(created_agents),
                )


def _extract_agent_id(response: dict[str, Any]) -> str:
    """Pluck ``response["agent"]["id"]`` defensively (typed-dict-friendly).

    Raises a :func:`pytest.fail` (not a bare ``AssertionError``) when the
    shape is unexpected so the failure surface includes the keys observed
    in the response — handy when Cursor's gateway evolves the wire shape.
    """
    agent = response.get("agent") if isinstance(response, dict) else None
    agent_id = agent.get("id") if isinstance(agent, dict) else None
    if not isinstance(agent_id, str) or not agent_id:
        pytest.fail(
            "unexpected create_agent response shape: "
            f"top_level_keys={sorted(response) if isinstance(response, dict) else None!r}, "
            f"agent_keys={sorted(agent) if isinstance(agent, dict) else 'N/A'!r}"
        )
    return agent_id


def test_minimum_config_201_with_dashboard_url(
    live_client: tuple[CloudCursorClient, list[str]],
) -> None:
    """AC 3 — POST with prompt + repos[{url, startingRef}] only (no env) → Dashboard URL.

    The minimum-config dispatch validates the env-shape pivot's "no env =
    cursor-managed cloud VM" default semantics (research/01 §"Schema
    fully nailed down" PROBE_30 — when ``env`` is omitted the gateway
    responds with ``env: {"type":"cloud"}``). The Dashboard URL emission
    is what proves the run is visible at ``cursor.com/agents/...``
    (PLAN §4 release-gate "Path-1 live smoke (cursor-managed)").
    """
    client, created_agents = live_client

    response = client.create_agent(
        prompt=f"{_TEST_PROMPT_PREFIX} (test_minimum_config_201_with_dashboard_url)",
        model=_TEST_MODEL_ID,
        repo_url=_TEST_REPO_URL,
        starting_ref="main",
    )
    agent_id = _extract_agent_id(response)
    created_agents.append(agent_id)

    agent = response.get("agent") or {}
    url = agent.get("url", "") if isinstance(agent, dict) else ""
    assert agent_id.startswith("bc-"), (
        f"unexpected agent.id prefix: {agent_id!r} (expected 'bc-...')"
    )
    assert isinstance(url, str), f"agent.url is not a str: {url!r}"
    assert url.startswith("https://cursor.com/agents/bc-"), (
        f"agent.url did not match Dashboard URL prefix: {url!r}"
    )
    env = agent.get("env") if isinstance(agent, dict) else None
    if env is not None:
        assert isinstance(env, dict), f"agent.env not a dict: {env!r}"
        # PROBE_30: a missing-env request comes back with type:"cloud"
        # (the gateway's default). Allow ``None`` too in case of future
        # response-shape evolution.
        assert env.get("type") in ("cloud", None), (
            f"env.type unexpected for no-env dispatch: full env={env!r}"
        )


def test_env_machine_201_with_dashboard_url(
    live_client: tuple[CloudCursorClient, list[str]],
) -> None:
    """AC 4 — POST env={type:'machine', name:<probe>} → 201 + env.type='machine' echoed.

    This is the wire-level proof of the env-shape pivot for self-hosted-
    worker routing (research/01 §"Schema fully nailed down" PROBE_28/43/45
    — the only payload shape that lands a Dashboard-visible run on a
    My-Machines worker under personal API keys). Per PROBE_34 the
    gateway accepts any ``name`` string at schema time, so the test
    succeeds even when no real worker matches the supplied name (the
    run will sit in CREATING and eventually error if no worker claims
    it — but the schema acceptance + Dashboard URL emission, which is
    what we test here, fires regardless).
    """
    client, created_agents = live_client
    worker_name = (
        os.environ.get("POPOLA_PROBE_WORKER_NAME", "").strip()
        or _DEFAULT_PROBE_WORKER_NAME
    )

    response = client.create_agent(
        prompt=f"{_TEST_PROMPT_PREFIX} (test_env_machine_201_with_dashboard_url)",
        model=_TEST_MODEL_ID,
        repo_url=_TEST_REPO_URL,
        starting_ref="main",
        env={"type": "machine", "name": worker_name},
    )
    agent_id = _extract_agent_id(response)
    created_agents.append(agent_id)

    agent = response.get("agent") or {}
    url = agent.get("url", "") if isinstance(agent, dict) else ""
    env = agent.get("env") if isinstance(agent, dict) else None

    assert agent_id.startswith("bc-"), (
        f"unexpected agent.id prefix: {agent_id!r} (expected 'bc-...')"
    )
    assert isinstance(url, str) and url.startswith("https://cursor.com/agents/bc-"), (
        f"agent.url did not match Dashboard URL prefix: {url!r}"
    )
    assert isinstance(env, dict), (
        f"agent.env missing or wrong type in response: {env!r} "
        f"(full agent keys={sorted(agent) if isinstance(agent, dict) else None!r})"
    )
    assert env.get("type") == "machine", (
        f"env.type expected 'machine' but got {env.get('type')!r} "
        f"(full env={env!r})"
    )
    assert env.get("name") == worker_name, (
        f"env.name expected {worker_name!r} but got {env.get('name')!r} "
        f"(full env={env!r})"
    )


def test_github_app_preflight_refusal_when_repositories_empty(
    live_client: tuple[CloudCursorClient, list[str]],
) -> None:
    """AC 5 — pre-flight installed=False + subsequent dispatch raises typed error.

    Research/01 §"GitHub-App branch-validation gotcha" L161-165 +
    PROBE_03: the personal key has ``GET /v1/repositories`` returning
    ``{"items": []}`` because the Cursor GitHub App is not installed on
    the owning org — so any ``github.com/...`` dispatch fails the
    gateway's branch-existence check regardless of the actual branch.
    The v0.10.0 design (DECISIONS Q-9) catches this BEFORE the dispatch
    via :func:`check_github_app_installed` and refuses with the same
    bilingual hint as the late-catch catalog rule
    (``integration_github_app_branch_not_found``) — so the operator UX
    is identical regardless of which surface fires.

    This test asserts the early-refuse path: the typed
    :class:`GithubAppMissingError` (NOT a generic
    :class:`CursorCloudError` / 400 from the actual dispatch attempt).
    """
    client, _ = live_client

    preflight = check_github_app_installed(client, _GITHUB_TEST_REPO_URL)
    assert preflight.installed is False, (
        f"GitHub-App pre-flight expected installed=False for "
        f"{_GITHUB_TEST_REPO_URL!r} on this API key (research/01 PROBE_03: "
        f"GET /v1/repositories returns {{'items':[]}} for the personal key); "
        f"got installed={preflight.installed!r} message={preflight.message!r}"
    )

    with pytest.raises(GithubAppMissingError) as exc_info:
        client.create_agent(
            prompt=f"{_TEST_PROMPT_PREFIX} (test_github_app_preflight_refusal)",
            model=_TEST_MODEL_ID,
            repo_url=_GITHUB_TEST_REPO_URL,
            starting_ref="main",
        )
    err = exc_info.value
    assert err.cli_exit, (
        "GithubAppMissingError must carry a non-zero cli_exit per the v0.8.6 "
        "error catalog (cli_exit=78 — operator-facing 'change config to "
        "proceed' meaning); got "
        f"cli_exit={err.cli_exit!r}"
    )
    assert err.hint_en, (
        "GithubAppMissingError must carry a non-empty hint_en pointing at "
        "https://cursor.com/integrations/github (so the early-refuse and "
        "late-catch paths produce identical operator UX per Q-9)"
    )


def test_cleanup_archives_each_created_agent(
    live_client: tuple[CloudCursorClient, list[str]],
) -> None:
    """AC 6 — exercise the same archive primitive the teardown uses; assert listing excludes.

    Mechanism: this test creates a fresh ``bc-*`` agent, immediately
    archives it via the SAME ``_request_json("POST", .../archive)`` primitive
    the session-scoped teardown uses, then asserts
    ``GET /v1/agents?includeArchived=false`` no longer returns the id.
    The archived id is removed from ``created_agents`` so the teardown
    does not double-archive.

    Note: this assertion does NOT check for absence of all
    ``_TEST_PROMPT_PREFIX``-named agents in the listing — Tests 1 and 2
    create agents that are still alive while Test 4 runs (their
    teardown happens at session end). The id-precise assertion below
    is the correct scope for this test.
    """
    client, created_agents = live_client

    response = client.create_agent(
        prompt=f"{_TEST_PROMPT_PREFIX} (test_cleanup_archives_each_created_agent)",
        model=_TEST_MODEL_ID,
        repo_url=_TEST_REPO_URL,
        starting_ref="main",
    )
    agent_id = _extract_agent_id(response)
    created_agents.append(agent_id)
    assert agent_id.startswith("bc-")

    client._request_json(  # noqa: SLF001
        "POST",
        f"/v1/agents/{agent_id}/archive",
    )
    if agent_id in created_agents:
        created_agents.remove(agent_id)

    listing = client._request_json(  # noqa: SLF001
        "GET",
        "/v1/agents",
        params={"includeArchived": "false"},
    )
    items = listing.get("items") if isinstance(listing, dict) else None
    assert isinstance(items, list), (
        f"GET /v1/agents response missing 'items' list: "
        f"keys={sorted(listing) if isinstance(listing, dict) else None!r}"
    )
    listed_ids = {row.get("id") for row in items if isinstance(row, dict)}
    assert agent_id not in listed_ids, (
        f"agent {agent_id!r} was archived but still appears in "
        f"GET /v1/agents?includeArchived=false (listed_ids sample: "
        f"{sorted(i for i in listed_ids if isinstance(i, str))[:5]!r}, "
        f"total listed={len(listed_ids)})"
    )


def test_workers_list_includes_probe_worker_when_started(
    live_client: tuple[CloudCursorClient, list[str]],
) -> None:
    """AC 7 — gated by POPOLA_PROBE_WORKER_NAME; list_workers includes the name.

    This test asserts the ``GET /v0/private-workers`` plumbing
    (:meth:`CloudCursorClient.list_workers`) wires through to the
    operator-visible name. The probe worker MUST be started out-of-band
    by the operator BEFORE running this test:

        popola cloud worker start --name <name> --worker-dir <repo-root>
        export POPOLA_PROBE_WORKER_NAME=<name>
        pytest tests/cloud/test_real_cursor_cloud_env_shape_v0_10_0.py \\
            -m real_cursor_cloud -k workers_list

    Without ``POPOLA_PROBE_WORKER_NAME`` set the test skips — the test
    cannot assert "the operator's worker is registered" if the operator
    did not name one.
    """
    probe_name = os.environ.get("POPOLA_PROBE_WORKER_NAME", "").strip()
    if not probe_name:
        pytest.skip(
            "POPOLA_PROBE_WORKER_NAME not set; this test verifies an "
            "out-of-band probe worker (started by the operator before "
            "running the smoke) appears in GET /v0/private-workers. To "
            "enable: start a worker via `popola cloud worker start "
            "--name <name> --worker-dir <repo-root>` and export "
            "POPOLA_PROBE_WORKER_NAME=<name> before re-running."
        )
    client, _ = live_client

    workers = client.list_workers()
    names = [w.get("name") for w in workers]
    assert probe_name in names, (
        f"POPOLA_PROBE_WORKER_NAME={probe_name!r} did not match any worker "
        f"in GET /v0/private-workers (registered names: "
        f"{sorted({str(n) for n in names if n})!r}); verify the probe "
        "worker is started AND registered with the same API key (use "
        "`popola cloud worker list` to compare against the daemon's view)"
    )
