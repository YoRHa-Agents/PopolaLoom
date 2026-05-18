"""``popola cloud worker dispatch`` v0.10.0 worker-existence pre-flight tests.

PopolaLoom v0.10.0 Wave D3 — DECISIONS Q-3 + Q-4 + Q-7 +
``PLAN.md §"Wave D → Task D3"`` AC 1-4.  Replaces the v0.9.9
``test_cloud_worker_dispatch_account_class.py`` (the gate it pinned was
DELETED per Q-4: research/01's 22 successful 2xx probes disconfirmed the
Spike-0 BRANCH_B verdict that justified the v0.9.9 hard-fail on personal
API keys).

This file pins the seven acceptance criteria for the new
``_enforce_self_hosted_worker_exists`` gate (C1):

* (a) ``--cloud-target=self-hosted --worker-name=ghost`` exits 78 and
  NEVER falls back to local (no ``--cli=cursor`` mention in stderr).
* (b) The bilingual hint includes ``popola cloud worker start --name``
  AND the Chinese fragment ``Worker '<name>' 不存在``.
* (c) ``--cloud-target=self-hosted --worker-name=existing`` does NOT
  exit (the dispatch proceeds and the mocked ``popolad`` POST receives
  the expected body shape).
* (d) ``--cloud-target=cursor-managed`` skips the worker-existence
  check entirely (``CloudCursorClient`` is never instantiated;
  ``list_workers()`` is never called).
* (e) ``--cloud-target=self-hosted --worker-name=existing-but-busy``
  logs WARN but does NOT exit (the run will queue at the gateway per
  Q-3 soft-warn semantics).
* (f) HTTP 5xx during ``list_workers()`` re-raises the underlying
  :class:`CursorCloudError` UNCHANGED (No Silent Failures workspace
  rule — see ``AGENTS.md``).
* (g) When ``[user_preferences].default_cloud_target=self-hosted`` is
  set AND no per-task ``--cloud-target`` / ``--worker-name`` flag is
  passed AND no worker-name marker resolves, ``popola dispatch`` fails
  with the ``--worker-name required`` validation error from B3
  (exit 2, with a bilingual hint pointing at the actual fix —
  ``popola cloud worker start --name`` — and NO ``--cli=cursor``
  fallback suggestion per Q-7).

Mocking strategy
----------------

The new gate's HTTP boundary is :meth:`CloudCursorClient.list_workers`.
Per the L0 brief and ``PLAN.md`` D3 AC 3, this file mocks at exactly that
boundary by monkey-patching
``popolaloom.adapters.cursor_cloud.CloudCursorClient`` with a
controllable ``_FakeCloudCursorClient`` that:

- Records every instantiation and every ``list_workers()`` call (so
  case (d) can assert zero invocations).
- Returns a canned list of worker rows OR raises a configurable
  exception (so case (f) can verify the No-Silent-Failures contract).
- Acts as both the constructor and the context-manager protocol so
  the C1 ``with CloudCursorClient(api_key) as client`` block works
  unchanged.

We patch the *source* module attribute (``popolaloom.adapters.cursor_cloud
.CloudCursorClient``) rather than ``cloud_worker_cmd``'s namespace
because :func:`popolaloom.cli.cloud_worker_cmd._enforce_self_hosted_worker_exists`
performs the import inside the function body — patching the source
module ensures the runtime ``from ... import CloudCursorClient`` lookup
resolves to our fake.

No live HTTP, no popolad UDS, no subprocess — fully hermetic via
``tmp_path`` + ``monkeypatch``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from popolaloom.cli import cloud_worker_cmd
from popolaloom.cli.cloud_worker_cmd import LocalWorkerProcess

# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _stub_jwt_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1.6.0 — ``popola cloud worker dispatch`` pre-loads the JWT bundle.

    Tests in this file exercise the worker-existence pre-flight, not
    JWT auth, so stub ``load_jwt_bundle`` to a sentinel so the eager
    JWT-load step is a no-op.
    """
    monkeypatch.setattr(
        "popolaloom.cloud.internal.jwt_auth.load_jwt_bundle",
        lambda: object(),
    )


@pytest.fixture(autouse=True)
def _short_dashboard_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1.6.0 — shrink the post-dispatch dashboard_url poll window for hermetic speed.

    Production default is 2.0 s; the tests don't seed the events log
    so they always hit the timeout path. 50 ms keeps each test fast
    while still exercising the timeout-WARN branch.
    """
    monkeypatch.setattr(
        "popolaloom.cli.main._DASHBOARD_URL_POLL_TOTAL_S", 0.05
    )
    monkeypatch.setattr(
        "popolaloom.cli.main._DASHBOARD_URL_POLL_INTERVAL_S", 0.01
    )


@pytest.fixture
def isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Hermetic ``$POPOLA_HOME`` + ``$HOME`` so worker paths cannot bleed."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.delenv("POPOLA_WORKER_NAME", raising=False)
    monkeypatch.delenv("POPOLA_SELF_HOSTED_WORKER_NAME", raising=False)
    monkeypatch.chdir(tmp_path)
    yield tmp_path


# ── _FakeCloudCursorClient — controllable fake for list_workers() ─────


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workers: list[dict[str, Any]] | None = None,
    raises: BaseException | None = None,
) -> SimpleNamespace:
    """Install a controllable :class:`CloudCursorClient` stub at the source module.

    Patches ``popolaloom.adapters.cursor_cloud.CloudCursorClient`` so the
    function-local ``from popolaloom.adapters.cursor_cloud import
    CloudCursorClient`` import inside
    :func:`popolaloom.cli.cloud_worker_cmd._enforce_self_hosted_worker_exists`
    picks up the fake on its next call.

    Returns a :class:`types.SimpleNamespace` tracker so tests can assert
    instantiation count, ``list_workers()`` call count, and the API key
    that was passed to the constructor.

    Args:
        monkeypatch: pytest's monkeypatch fixture (lifespan-bound).
        workers: List of worker rows ``client.list_workers()`` returns.
            Each row should follow the snake_case
            :class:`popolaloom.adapters.cursor_cloud.WorkerInfo` shape
            (``worker_id``, ``name``, ``is_in_use``, ``active_bc_id``,
            ``repo_url``, ``user_id``).  Defaults to an empty list (i.e.
            no workers registered — useful for the "ghost" case).
        raises: When non-``None``, ``list_workers()`` raises this
            exception instead of returning ``workers``.  Used by case (f)
            to inject a :class:`CursorCloudError` and verify the
            propagation contract.

    Returns:
        A namespace with ``instantiations: int``, ``list_workers_calls:
        int``, and ``last_api_key: str | None`` attributes.
    """
    workers_copy = list(workers or [])
    tracker = SimpleNamespace(
        instantiations=0,
        list_workers_calls=0,
        last_api_key=None,
    )

    class _FakeCloudCursorClient:
        """Stand-in for :class:`CloudCursorClient` with controlled behaviour."""

        def __init__(self, api_key: str, **_kwargs: Any) -> None:
            tracker.instantiations += 1
            tracker.last_api_key = api_key

        def __enter__(self) -> _FakeCloudCursorClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def list_workers(self, **_kwargs: Any) -> list[dict[str, Any]]:
            tracker.list_workers_calls += 1
            if raises is not None:
                raise raises
            return list(workers_copy)

    monkeypatch.setattr(
        "popolaloom.adapters.cursor_cloud.CloudCursorClient",
        _FakeCloudCursorClient,
    )
    return tracker


def _make_local_worker(name: str, worker_dir: Path) -> LocalWorkerProcess:
    """Build a :class:`LocalWorkerProcess` with a controlled ``--name`` value.

    The ``worker_dispatch_cmd`` function derives ``worker_name`` from
    ``_detect_running_workers_for_dir`` (when it returns ≥ 1 process)
    or falls back to ``_default_worker_name`` (a deterministic hash of
    the resolved worker dir).  Tests pin the name via this helper so
    the fake client's ``list_workers()`` matching is deterministic.
    """
    return LocalWorkerProcess(
        pid=4242,
        worker_dir=worker_dir.resolve(),
        name=name,
        management_addr="127.0.0.1:39231",
        argv=("agent", "worker", "start"),
    )


def _combined_output(result: Any) -> str:
    """Concatenate the runner result's ``stdout`` / ``stderr`` / ``output`` attrs.

    Typer's :class:`CliRunner` sometimes splits and sometimes mixes the
    two streams; this helper papers over the variation so assertion
    messages don't depend on the runner's stream-mixing mode.
    """
    parts: list[str] = []
    for attr in ("stdout", "stderr", "output"):
        try:
            value = getattr(result, attr, "") or ""
        except (ValueError, AttributeError):
            value = ""
        if value and value not in parts:
            parts.append(value)
    return "".join(parts)


# ── (a) ghost worker → exit 78, NEVER falls back to local ─────────────


def test_ghost_worker_exits_78_with_no_local_fallback(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (a): missing self-hosted worker exits 78; never `--cli=cursor` hint.

    The deleted v0.9.9 gate's exit-78 slot is REUSED for the new
    worker-existence gate (DECISIONS Q-4): same operator-meaning
    (``"change account / config / worker registration to proceed"``)
    so script branching keeps working across the gate semantics swap.
    Critically, the bilingual hint MUST NOT mention any ``--cli=cursor``
    local-CLI fallback path per Q-7 (``feedbacks/feedback_for_v0.10.0.md``
    L5+L11 forbids silent re-routing of cloud dispatch to local).
    """
    monkeypatch.setenv("CURSOR_API_KEY", "sk-test-ghost")
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda _worker_dir: [_make_local_worker("ghost", isolated_home)],
    )
    _install_fake_client(monkeypatch, workers=[])

    def must_not_be_called(_body: dict[str, Any]) -> httpx.Response:
        raise AssertionError(
            "popolad RPC must NOT be called when the gate refuses"
        )

    monkeypatch.setattr(
        cloud_worker_cmd,
        "_post_popolad_dispatch_request",
        must_not_be_called,
    )

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "dispatch",
            "fix the tests",
            "--worker-dir",
            str(isolated_home),
            "--repo-url",
            "https://github.com/acme/repo",
        ],
    )

    assert result.exit_code == cloud_worker_cmd._EXIT_PRE_FLIGHT_GATE, (
        f"expected exit 78 (pre-flight gate), got {result.exit_code}: "
        f"{_combined_output(result)!r}"
    )
    output = _combined_output(result)
    assert "ghost" in output, (
        f"expected the missing worker name in the bilingual hint: {output!r}"
    )
    assert "popola cloud worker start --name" in output, (
        "expected the bilingual hint to point at the actual fix; got "
        f"{output!r}"
    )
    assert "--cli=cursor" not in output, (
        "Q-7 violation: the hint must NOT suggest a local-CLI fallback "
        f"(`--cli=cursor`); got {output!r}"
    )


# ── (b) bilingual hint includes `popola cloud worker start --name` ────


def test_bilingual_hint_includes_popola_cloud_worker_start_name(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (b): bilingual hint surfaces both the English fix and the Chinese fragment.

    The hint must remain bilingual so operators on either language
    surface get the same actionable next step.  Required substrings
    (PLAN.md C1 AC 7):

    * ``popola cloud worker start --name <X> --worker-dir <repo-root>``
      with ``<X>`` = the actual missing worker name.
    * Chinese fragment ``Worker '<name>' 不存在`` quoted exactly so a
      future regex pin (``Worker '\\S+' 不存在``) keeps matching.
    """
    monkeypatch.setenv("CURSOR_API_KEY", "sk-test-hint")
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda _worker_dir: [
            _make_local_worker("nonexistent-worker", isolated_home)
        ],
    )
    _install_fake_client(monkeypatch, workers=[])
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_post_popolad_dispatch_request",
        lambda _body: (_ for _ in ()).throw(
            AssertionError("RPC must not be called when the gate refuses")
        ),
    )

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "dispatch",
            "fix the tests",
            "--worker-dir",
            str(isolated_home),
            "--repo-url",
            "https://github.com/acme/repo",
        ],
    )

    assert result.exit_code == cloud_worker_cmd._EXIT_PRE_FLIGHT_GATE
    output = _combined_output(result)
    assert (
        "popola cloud worker start --name nonexistent-worker" in output
    ), f"missing English fix-instruction substring; got {output!r}"
    assert "Worker 'nonexistent-worker' 不存在" in output, (
        "missing Chinese bilingual fragment "
        f"'Worker '<name>' 不存在'; got {output!r}"
    )


# ── (c) existing free worker → dispatch proceeds with expected body ───


def test_existing_worker_proceeds_to_dispatch_with_expected_body(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (c): a registered free worker lets the dispatch through verbatim.

    When ``found=True, is_in_use=False`` the gate returns silently and
    ``worker_dispatch_cmd`` proceeds to ``_dispatch_to_popolad`` which
    POSTs the canonical ``cli=cursor-cloud`` body to popolad.  This test
    intercepts the POST via ``_post_popolad_dispatch_request`` and pins
    the exact body shape so a future regression in the body builder is
    caught at the unit-test layer.
    """
    monkeypatch.setenv("CURSOR_API_KEY", "sk-test-existing")

    worker_name = "existing"
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda _worker_dir: [
            _make_local_worker(worker_name, isolated_home)
        ],
    )
    tracker = _install_fake_client(
        monkeypatch,
        workers=[
            {
                "worker_id": "uuid-existing-1",
                "name": worker_name,
                "is_in_use": False,
                "active_bc_id": None,
                "repo_url": "https://github.com/acme/repo",
                "user_id": 7,
            }
        ],
    )

    captured: list[dict[str, Any]] = []

    def fake_post(body: dict[str, Any]) -> httpx.Response:
        captured.append(body)
        return httpx.Response(
            200, json={"task_id": "cursor-cloud-existing-1"}
        )

    monkeypatch.setattr(
        cloud_worker_cmd,
        "_post_popolad_dispatch_request",
        fake_post,
    )

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "dispatch",
            "ship the fix",
            "--worker-dir",
            str(isolated_home),
            "--repo-url",
            "https://github.com/acme/repo",
        ],
    )

    assert result.exit_code == 0, _combined_output(result)
    # v1.6.0 (feedback_for_v1.5.2 constraint #4): the verb prints the
    # task_id AND polls for ``cloud.queued.dashboard_url``. The events
    # log is not seeded in this test so the poller times out with the
    # bilingual stderr WARN — both are expected.
    out = _combined_output(result).strip()
    assert "cursor-cloud-existing-1" in out, (
        f"expected dispatch task_id on stdout; got {out!r}"
    )
    assert "dashboard_url not surfaced" in out, (
        "constraint #4: when the poller times out it MUST emit a WARN "
        "(never silently skip)"
    )
    assert tracker.list_workers_calls == 1, (
        "expected exactly one list_workers() call to validate the worker; "
        f"got {tracker.list_workers_calls}"
    )
    assert tracker.last_api_key == "sk-test-existing", (
        "expected CloudCursorClient to be constructed with the resolved "
        f"API key; got {tracker.last_api_key!r}"
    )
    assert len(captured) == 1, (
        f"expected exactly one popolad POST; got {len(captured)} ({captured!r})"
    )
    body = captured[0]
    assert body["cli"] == "cursor-cloud"
    assert body["prompt"] == "ship the fix"
    assert body["cwd"] == str(isolated_home.resolve())
    assert body["extra"]["worker_name"] == worker_name
    assert body["extra"]["repo_url"] == "https://github.com/acme/repo"
    assert body["extra"]["starting_ref"] == "main"
    assert body["extra"]["model"] == "composer-2"


# ── (d) cursor-managed target skips the worker-existence check ────────


def test_cursor_managed_target_skips_worker_existence_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (d): ``target=cursor-managed`` returns immediately; no list_workers call.

    The gate is keyed on ``target == "self-hosted"`` only — for any other
    value (``cursor-managed``, ``ask-each-time``, empty string), the
    function returns before instantiating :class:`CloudCursorClient`.
    This test pins the no-op contract by asserting both
    ``instantiations`` and ``list_workers_calls`` stay at zero, which
    keeps Cursor-managed dispatches free of the extra HTTP round trip
    (and means a flaky ``/v0/private-workers`` endpoint cannot break
    Cursor-managed routing).
    """
    tracker = _install_fake_client(monkeypatch, workers=[])

    cloud_worker_cmd._enforce_self_hosted_worker_exists(
        api_key="sk-test-cursor-managed",
        worker_name="any-worker",
        target="cursor-managed",
    )

    assert tracker.instantiations == 0, (
        "CloudCursorClient must not be instantiated for non-self-hosted "
        f"targets; got {tracker.instantiations} instantiations"
    )
    assert tracker.list_workers_calls == 0, (
        "list_workers() must not be called when target is cursor-managed; "
        f"got {tracker.list_workers_calls} calls"
    )


# ── (e) existing-but-busy worker → WARN but proceed (run will queue) ─


def test_existing_but_busy_worker_logs_warn_does_not_exit(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC (e): a busy registered worker WARNs (run will queue) but proceeds.

    Per DECISIONS Q-3 + PLAN.md C1 AC 5d, ``found=True, is_in_use=True``
    is a *soft* signal: Cursor's gateway accepts the POST and queues the
    run until the worker frees up.  The gate emits a WARN (so the
    operator can see the queue-wait coming) but does NOT exit; the
    dispatch proceeds and pop olad receives the request.
    """
    monkeypatch.setenv("CURSOR_API_KEY", "sk-test-busy")

    worker_name = "existing-but-busy"
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda _worker_dir: [
            _make_local_worker(worker_name, isolated_home)
        ],
    )
    _install_fake_client(
        monkeypatch,
        workers=[
            {
                "worker_id": "uuid-busy-1",
                "name": worker_name,
                "is_in_use": True,
                "active_bc_id": "bc-active-99",
                "repo_url": "https://github.com/acme/repo",
                "user_id": 7,
            }
        ],
    )

    captured: list[dict[str, Any]] = []

    def fake_post(body: dict[str, Any]) -> httpx.Response:
        captured.append(body)
        return httpx.Response(200, json={"task_id": "cursor-cloud-queued"})

    monkeypatch.setattr(
        cloud_worker_cmd,
        "_post_popolad_dispatch_request",
        fake_post,
    )

    from popolaloom.cli.main import app as root_app

    with caplog.at_level(
        logging.WARNING, logger="popolaloom.cli.cloud_worker_cmd"
    ):
        result = runner.invoke(
            root_app,
            [
                "cloud",
                "worker",
                "dispatch",
                "queue this",
                "--worker-dir",
                str(isolated_home),
                "--repo-url",
                "https://github.com/acme/repo",
            ],
        )

    assert result.exit_code == 0, _combined_output(result)
    assert len(captured) == 1, (
        "dispatch must reach popolad even when the worker is busy "
        f"(got {len(captured)} POSTs)"
    )

    busy_records = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "is currently busy" in record.getMessage()
        and "your run will queue" in record.getMessage()
    ]
    assert busy_records, (
        "expected a WARN log with both 'is currently busy' AND "
        "'your run will queue' in caplog (PLAN.md C1 AC 5d soft-warn); "
        f"records seen: {[r.getMessage() for r in caplog.records]!r}"
    )
    assert any(
        worker_name in record.getMessage() for record in busy_records
    ), (
        f"expected the worker name {worker_name!r} in the WARN payload"
    )


# ── (f) HTTP 5xx during list_workers() → re-raises CursorCloudError ───


def test_http_5xx_during_list_workers_reraises_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (f): transient upstream errors propagate UNCHANGED (No Silent Failures).

    The gate MUST NOT swallow :class:`CursorCloudError` from
    ``list_workers()``.  Doing so would silently allow a dispatch when
    the gate is unable to determine whether the named worker exists —
    which is precisely the failure mode the No-Silent-Failures rule
    (``AGENTS.md`` workspace rule) forbids.  The error propagates so
    the caller's catalog formatting / bilingual hint logic kicks in.
    """
    from popolaloom.adapters.cursor_cloud import CursorCloudError

    upstream_error = CursorCloudError(
        "cursor-cloud upstream returned 502 Bad Gateway during "
        "GET /v0/private-workers"
    )

    _install_fake_client(monkeypatch, raises=upstream_error)

    with pytest.raises(CursorCloudError) as exc_info:
        cloud_worker_cmd._enforce_self_hosted_worker_exists(
            api_key="sk-test-5xx",
            worker_name="probe-w1",
            target="self-hosted",
        )

    assert "502 Bad Gateway" in str(exc_info.value), (
        f"propagated error must carry the upstream message; got {exc_info.value!r}"
    )
    assert exc_info.value is upstream_error, (
        "the error must be propagated UNCHANGED — not wrapped or "
        "rewritten — so the caller's catalog / bilingual hint logic "
        "sees the same exception instance"
    )


# ── (g) default_cloud_target=self-hosted + no worker name → exit 2 ────


def test_default_cloud_target_self_hosted_no_worker_fails_validation(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (g): pref=self-hosted + no flag + no marker → exit 2 from B3 validator.

    ``popola dispatch`` with ``default_cloud_target=self-hosted`` and no
    per-task ``--cloud-target`` / ``--worker-name`` flag triggers the
    B3 resolver (``_apply_cloud_preferences``).  When no worker name is
    recoverable from (a) the per-task flag, (b) the legacy
    ``--cli-flag worker_name=`` extra, (c) ``POPOLA_WORKER_NAME`` /
    ``POPOLA_SELF_HOSTED_WORKER_NAME`` env, or (d) a ``.popola-worker`` /
    ``.popola/worker_name`` file marker, the resolver hard-fails with
    ``_EXIT_INVALID_ARGS`` (2) and the same bilingual hint shape as the
    C1 gate — pointing at ``popola cloud worker start --name`` and
    explicitly NOT suggesting a ``--cli=cursor`` local fallback (Q-7).

    The fixture deletes both ``POPOLA_*WORKER_NAME`` env vars and the
    isolated cwd has no ``.popola-worker`` / ``.popola/worker_name`` —
    so the resolver hits the no-worker-recoverable path verbatim.
    """
    from popolaloom.cli.init_cmd import write_user_preferences_for_cli
    from popolaloom.cli.main import app as main_app
    from popolaloom.daemon.main import UserPreferencesConfig

    write_user_preferences_for_cli(
        UserPreferencesConfig(
            default_runtime="cloud",
            default_cloud_target="self-hosted",
        )
    )

    result = runner.invoke(
        main_app,
            ["dispatch", "no worker pref", "--no-wizard"],
    )

    assert result.exit_code == cloud_worker_cmd._EXIT_INVALID_ARGS, (
        f"expected exit 2 (invalid args from B3 validator), "
        f"got {result.exit_code}: {_combined_output(result)!r}"
    )
    output = _combined_output(result)
    assert "popola cloud worker start --name" in output, (
        "expected the bilingual hint to point at the actual fix; got "
        f"{output!r}"
    )
    assert "--cli=cursor" not in output, (
        "Q-7 violation: the hint must NOT suggest a local-CLI fallback "
        f"(`--cli=cursor`); got {output!r}"
    )
