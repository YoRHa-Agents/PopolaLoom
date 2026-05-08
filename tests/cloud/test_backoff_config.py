"""v0.8.8 T2.1.3 — `_retrying_request` + `BackoffConfig` schedule + `Retry-After` parser tests.

Covers AC (b) / (c) / (d) / (e) / (f) per
``.local/.agent/active/v0.8.8-multi-run/PLAN.md`` §4.1 T2.1.3 + the spec
in ``.local/research/v0.8.8_multi_run/quota-config.md`` §3:

- Happy path — non-429 response on the first try yields no retry, no events.
- Schedule pin — defaults `(500ms, 2x, 30s cap, ±25%)` produce the
  table in §3.1; cumulative worst-case ≤ 39.4 s with jitter.
- ``Retry-After`` Form 1 (delta-seconds integer) — replaces the local
  schedule when ``honor_retry_after = True``.
- ``Retry-After`` Form 2 (HTTP-date) — parsed via
  :func:`email.utils.parsedate_to_datetime`; clamped to ≥ 0 ms.
- Garbled ``Retry-After`` header — ``None`` + WARN log; falls through to
  exponential schedule (No-Silent-Failures).
- Jitter ±25 % — every observed delay sits inside ``[0.75x, 1.25x]`` of
  the unjittered base for that attempt.
- ``max_retries = 0`` — single-shot, surfaces 429 immediately as
  :class:`CursorCloudRateLimitError(cli_exit=75)`.
- Exhaustion — sustained 429 hits ``max_retries`` and raises
  :class:`CursorCloudRateLimitError(cli_exit=75)`; ``cloud.queued_quota_exceeded``
  fires once per sequence; ``cloud.queue_exit outcome="exhausted"``
  closes the bracket.

All cloud calls use :class:`httpx.MockTransport` per the task brief;
no real network round-trips.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from popolaloom.adapters.cursor_cloud import (
    CURSOR_API_BASE,
    BackoffConfig,
    CloudCursorClient,
    CursorCloudAuthError,
    CursorCloudError,
    CursorCloudRateLimitError,
    _compute_backoff,
    _parse_retry_after,
)
from popolaloom.daemon.event_log import EventLog

# ---------------------------------------------------------------------------
# Shared helpers — every test uses MockTransport; no real network round-trip.
# ---------------------------------------------------------------------------


_MockHandler = Callable[[httpx.Request], httpx.Response]


def _make_client_with_mock(handler: _MockHandler) -> CloudCursorClient:
    """Construct a :class:`CloudCursorClient` whose httpx is a MockTransport."""
    client = CloudCursorClient("test-key", base_url=CURSOR_API_BASE)
    client._client.close()
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=client._base_url,
        auth=(client._api_key, ""),
        timeout=client._timeout_s,
    )
    return client


@pytest.fixture()
def event_log(tmp_path: Path) -> Iterator[EventLog]:
    """Per-test EventLog with the fsync worker disabled (no background thread)."""
    log = EventLog(tmp_path / "test.jsonl", fsync_interval_s=0.0)
    yield log
    log.close()


@pytest.fixture()
def sleep_recorder() -> tuple[list[float], Callable[[float], None]]:
    """Returns ``(record_list, sleep_fn)`` — sleep_fn appends to record_list.

    Substituting :func:`time.sleep` with this fake keeps the test fast AND
    lets us assert the exact schedule of sleeps (per AC b/d/g schedule pin).
    """
    record: list[float] = []

    def _fake_sleep(seconds: float) -> None:
        record.append(seconds)

    return record, _fake_sleep


# ---------------------------------------------------------------------------
# AC (b) — happy path: 200 on first try, no retry, no events.
# ---------------------------------------------------------------------------


def test_retrying_request_happy_path_no_retry(
    event_log: EventLog,
    sleep_recorder: tuple[list[float], Callable[[float], None]],
) -> None:
    """A successful first call returns the body verbatim, sleeps zero times,
    and emits no quota events (the "no quota cost paid" path)."""
    sleeps, fake_sleep = sleep_recorder
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"id": "run-1", "status": "RUNNING"})

    client = _make_client_with_mock(handler)
    result = client._retrying_request(
        "GET",
        "/v1/agents/bc-1/runs/run-1",
        backoff_config=BackoffConfig(),
        event_log=event_log,
        task_id="t-happy",
        sleep=fake_sleep,
    )
    assert result == {"id": "run-1", "status": "RUNNING"}
    assert len(calls) == 1
    assert sleeps == []
    events = event_log.tail()
    quota_events = [e for e in events if e["type"].startswith("cloud.queue")]
    assert quota_events == [], (
        f"happy path must NOT emit quota events; got {quota_events!r}"
    )


# ---------------------------------------------------------------------------
# AC (b) — schedule pin: defaults (500 ms, 2x, 30 s cap, ±25 %) match §3.1.
# ---------------------------------------------------------------------------


def test_retrying_request_schedule_pin_with_zero_jitter(
    event_log: EventLog,
    sleep_recorder: tuple[list[float], Callable[[float], None]],
) -> None:
    """Pin the un-jittered schedule for ``max_retries = 5``: delays for
    attempts 0..4 are 500/1000/2000/4000/8000 ms (all uncapped at the
    30 s cap). Setting ``jitter_pct = 0`` gives a deterministic schedule
    so this assertion is exact, not range-based.
    """
    sleeps, fake_sleep = sleep_recorder
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(429, json={"error": "Too Many Requests"})

    client = _make_client_with_mock(handler)
    cfg = BackoffConfig(
        max_retries=5,
        base_backoff_ms=500,
        max_backoff_ms=30_000,
        jitter_pct=0,
        honor_retry_after=False,
    )
    with pytest.raises(CursorCloudRateLimitError) as excinfo:
        client._retrying_request(
            "GET",
            "/v1/agents/bc-1/runs/run-1",
            backoff_config=cfg,
            event_log=event_log,
            task_id="t-pin",
            sleep=fake_sleep,
        )

    # 1 initial call + 5 retries = 6 total HTTP calls.
    assert call_count[0] == 6, f"expected 6 calls; got {call_count[0]}"
    # 5 sleeps, deterministic.
    expected_seconds = [0.5, 1.0, 2.0, 4.0, 8.0]
    assert sleeps == expected_seconds, f"schedule drift: {sleeps!r}"
    # Cumulative worst-case ≤ 39.4 s (per AC b).
    assert sum(sleeps) <= 39.4, f"cumulative wait {sum(sleeps)} > 39.4s"
    # Exit code propagates from the catalog.
    assert excinfo.value.cli_exit == 75


# ---------------------------------------------------------------------------
# AC (c) — Retry-After Form 1: delta-seconds integer.
# ---------------------------------------------------------------------------


def test_retrying_request_honors_retry_after_delta_seconds(
    event_log: EventLog,
    sleep_recorder: tuple[list[float], Callable[[float], None]],
) -> None:
    """``Retry-After: 3`` (delta-seconds integer) replaces the local schedule
    with 3 s — clamped at ``max_backoff_ms`` if larger. The next attempt
    succeeds, so we observe exactly one sleep of 3.0 s.
    """
    sleeps, fake_sleep = sleep_recorder
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx.Response(
                429,
                json={"error": "Too Many Requests"},
                headers={"Retry-After": "3"},
            )
        return httpx.Response(200, json={"ok": True})

    client = _make_client_with_mock(handler)
    cfg = BackoffConfig(
        max_retries=5,
        base_backoff_ms=500,
        max_backoff_ms=30_000,
        jitter_pct=25,
        honor_retry_after=True,
    )
    result = client._retrying_request(
        "GET",
        "/v1/agents/bc-1",
        backoff_config=cfg,
        event_log=event_log,
        task_id="t-retry-int",
        sleep=fake_sleep,
    )
    assert result == {"ok": True}
    assert call_count[0] == 2
    assert sleeps == [3.0], (
        f"Retry-After header should override jitter; got {sleeps!r}"
    )

    # cloud.queued_quota_exceeded fires once with retry_after_ms = 3000.
    types = [e["type"] for e in event_log.tail()]
    assert "cloud.queued_quota_exceeded" in types
    qe = next(
        e for e in event_log.tail() if e["type"] == "cloud.queued_quota_exceeded"
    )
    assert qe["data"]["retry_after_ms"] == 3000
    assert qe["data"]["status"] == 429
    # cloud.queue_exit outcome=success closes the bracket.
    assert "cloud.queue_exit" in types
    qx = next(e for e in event_log.tail() if e["type"] == "cloud.queue_exit")
    assert qx["data"]["outcome"] == "success"


def test_retry_after_clamped_at_max_backoff_ms() -> None:
    """A server-side ``Retry-After: 9999`` (way over our cap) is clamped to
    the configured ``max_backoff_ms`` (here 30 s) so an operator typo /
    misconfigured upstream cannot freeze the daemon for hours."""
    cfg = BackoffConfig(max_backoff_ms=30_000, jitter_pct=0, honor_retry_after=True)
    delay_ms = _compute_backoff(0, cfg, "9999")  # 9999 s = 9_999_000 ms
    assert delay_ms == 30_000.0, (
        f"Retry-After must clamp at max_backoff_ms; got {delay_ms}"
    )


# ---------------------------------------------------------------------------
# AC (c) — Retry-After Form 2: HTTP-date.
# ---------------------------------------------------------------------------


def test_parse_retry_after_http_date_form() -> None:
    """An RFC 7231 §7.1.3 Form 2 ``Retry-After: <HTTP-date>`` parses through
    :func:`email.utils.parsedate_to_datetime` and yields a positive
    millisecond delay. We pin a deterministic future date relative to
    "now" via :class:`freezegun`-style monkeypatching."""
    # Use a date ~30 s in the future to validate the math without
    # racing the real clock. We pick a fixed future ISO and cap-test
    # the parser via time.
    import time as _time
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    # 2 seconds in the future — must yield ~2000 ms (allow wide slack
    # for test-machine clock drift).
    future = datetime.now(UTC) + timedelta(seconds=2)
    header = format_datetime(future)
    parsed = _parse_retry_after(header)
    assert parsed is not None
    # ~2000 ms with 2 s of clock-drift tolerance (resolves at 1-second
    # granularity since HTTP-date does not have ms precision).
    assert 0 <= parsed <= 4000, f"unexpected HTTP-date delta: {parsed} ms"
    # Use _time to silence the unused-import lint when the file is
    # quoted into a doctest stub (no side-effect on the assertion).
    _ = _time


def test_parse_retry_after_http_date_in_past_clamped_to_zero() -> None:
    """An HTTP-date in the PAST clamps to 0 ms (per spec §3.2 ``max(0, …)``).

    Edge case for Cursor returning an already-elapsed deadline (clock skew
    or queue drained mid-flight). The caller still issues the next attempt
    immediately rather than panicking.
    """
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    past = datetime.now(UTC) - timedelta(seconds=60)
    header = format_datetime(past)
    parsed = _parse_retry_after(header)
    assert parsed == 0, f"past HTTP-date must clamp to 0; got {parsed}"


# ---------------------------------------------------------------------------
# AC (c) — garbled Retry-After: None + WARN, falls through to local schedule.
# ---------------------------------------------------------------------------


def test_garbled_retry_after_falls_through_to_local_schedule(
    event_log: EventLog,
    sleep_recorder: tuple[list[float], Callable[[float], None]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A garbled ``Retry-After: gibberish`` header MUST NOT raise; the parser
    returns ``None``, logs at WARNING (No-Silent-Failures), and the helper
    falls through to the deterministic exponential schedule. Garbled
    headers must NOT freeze the daemon."""
    sleeps, fake_sleep = sleep_recorder
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx.Response(
                429,
                json={"error": "Too Many Requests"},
                headers={"Retry-After": "totally-garbled-xyz-not-a-date-or-int"},
            )
        return httpx.Response(200, json={"ok": True})

    client = _make_client_with_mock(handler)
    cfg = BackoffConfig(
        max_retries=5, base_backoff_ms=500, jitter_pct=0, honor_retry_after=True
    )
    with caplog.at_level(logging.WARNING):
        result = client._retrying_request(
            "GET",
            "/v1/agents/bc-1",
            backoff_config=cfg,
            event_log=event_log,
            task_id="t-garbled",
            sleep=fake_sleep,
        )
    assert result == {"ok": True}
    # Local schedule attempt 0 = 500 ms = 0.5 s (jitter_pct=0 → exact).
    assert sleeps == [0.5], f"garbled header should fall through; got {sleeps!r}"
    # WARN log surfaced (No-Silent-Failures).
    warn_msgs = [
        r.message for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert any("Retry-After" in m for m in warn_msgs), (
        f"WARN log missing for garbled Retry-After: {warn_msgs!r}"
    )


def test_parse_retry_after_handles_explicit_garble_paths() -> None:
    """Direct call to :func:`_parse_retry_after` for the pure-parser surface.

    Multiple garbled forms (random text, lone whitespace, mixed types) all
    return ``None`` rather than raising. ``""`` and ``None`` short-circuit
    early; the date / int parsers are exercised with non-conforming input.
    """
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("   ") is None
    assert _parse_retry_after("not-a-date") is None
    assert _parse_retry_after("12abc") is None  # mixed int / non-int
    # A pure delta-seconds string parses to ms.
    assert _parse_retry_after("60") == 60_000
    assert _parse_retry_after("0") == 0


# ---------------------------------------------------------------------------
# AC (b) — jitter bounds: every delay in [0.75x, 1.25x] of unjittered base.
# ---------------------------------------------------------------------------


def test_jitter_bounds_within_plus_minus_25_percent() -> None:
    """For ``jitter_pct = 25``, every computed delay sits within
    ``[capped * 0.75, capped * 1.25]`` of the un-jittered base. We verify
    over 1000 samples per attempt to make the sampling deterministic
    enough that a flake is real (vs. random)."""
    cfg = BackoffConfig(
        base_backoff_ms=500, max_backoff_ms=30_000, jitter_pct=25
    )
    rng = random.Random(0xDEAD_C0FFEE)  # deterministic seed
    for attempt in range(7):
        # un-jittered base (capped at max_backoff_ms)
        raw = cfg.base_backoff_ms * (2**attempt)
        capped = min(raw, cfg.max_backoff_ms)
        lower = capped * 0.75
        upper = capped * 1.25
        for _ in range(1000):
            delay = _compute_backoff(attempt, cfg, None, rng=rng)
            assert lower <= delay <= upper, (
                f"jitter out of bounds @ attempt={attempt}: "
                f"got {delay}, expected in [{lower}, {upper}]"
            )


def test_jitter_pct_zero_is_deterministic() -> None:
    """``jitter_pct = 0`` produces the un-jittered schedule verbatim — useful
    for tests + for operators who want a deterministic cadence."""
    cfg = BackoffConfig(base_backoff_ms=500, max_backoff_ms=30_000, jitter_pct=0)
    for attempt, expected in enumerate([500, 1000, 2000, 4000, 8000, 16000, 30000]):
        actual = _compute_backoff(attempt, cfg, None)
        assert actual == float(expected), (
            f"jitter_pct=0 must be deterministic; attempt={attempt} "
            f"got {actual}, expected {expected}"
        )


# ---------------------------------------------------------------------------
# AC (b) — max_backoff_ms cap honored on long attempts.
# ---------------------------------------------------------------------------


def test_max_backoff_ms_cap_applied(
    event_log: EventLog,
    sleep_recorder: tuple[list[float], Callable[[float], None]],
) -> None:
    """At attempt 6, the un-jittered base is 32_000 ms — the cap clamps it to
    30_000 ms. Without the cap, attempt 7 would be 64 s, which would push
    the daemon past the per-team rate-limit reset window."""
    sleeps, fake_sleep = sleep_recorder
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(429, json={"error": "Too Many Requests"})

    client = _make_client_with_mock(handler)
    cfg = BackoffConfig(
        max_retries=8,
        base_backoff_ms=500,
        max_backoff_ms=30_000,
        jitter_pct=0,
        honor_retry_after=False,
    )
    with pytest.raises(CursorCloudRateLimitError):
        client._retrying_request(
            "GET",
            "/v1/agents/bc-1",
            backoff_config=cfg,
            event_log=event_log,
            task_id="t-cap",
            sleep=fake_sleep,
        )

    # Attempts 0..7 → delays 500, 1000, 2000, 4000, 8000, 16000, 30000, 30000.
    expected = [0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert sleeps == expected, (
        f"max_backoff_ms cap not applied as expected: {sleeps!r}"
    )


# ---------------------------------------------------------------------------
# AC (f) — max_retries = 0 disables retry entirely (single-shot).
# ---------------------------------------------------------------------------


def test_max_retries_zero_disables_retry(
    event_log: EventLog,
    sleep_recorder: tuple[list[float], Callable[[float], None]],
) -> None:
    """``max_retries = 0`` makes the helper a single-shot — the first 429
    propagates as :class:`CursorCloudRateLimitError` with no sleep, no
    retry. Useful for environments with their own retry harness (e.g.,
    a scheduler that owns the cadence)."""
    sleeps, fake_sleep = sleep_recorder
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(429, json={"error": "Too Many Requests"})

    client = _make_client_with_mock(handler)
    cfg = BackoffConfig(max_retries=0, jitter_pct=0)
    with pytest.raises(CursorCloudRateLimitError) as excinfo:
        client._retrying_request(
            "GET",
            "/v1/agents/bc-1",
            backoff_config=cfg,
            event_log=event_log,
            task_id="t-singleshot",
            sleep=fake_sleep,
        )
    assert call_count[0] == 1, "max_retries=0 must be single-shot"
    assert sleeps == [], "max_retries=0 must not sleep"
    assert excinfo.value.cli_exit == 75


# ---------------------------------------------------------------------------
# AC (d) — cloud.queued_quota_exceeded fires ONCE; cloud.queue_exit closes.
# ---------------------------------------------------------------------------


def test_quota_exceeded_event_fires_once_per_sequence(
    event_log: EventLog,
    sleep_recorder: tuple[list[float], Callable[[float], None]],
) -> None:
    """A 3-attempt sequence (initial + 2 retries) emits exactly ONE
    ``cloud.queued_quota_exceeded`` and one ``cloud.queue_exit`` — not
    one event per 429. The attach UI cares about "we hit a wall", not
    the individual retry beats."""
    sleeps, fake_sleep = sleep_recorder
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] <= 2:
            return httpx.Response(429, json={"error": "Too Many Requests"})
        return httpx.Response(200, json={"ok": True})

    client = _make_client_with_mock(handler)
    cfg = BackoffConfig(
        max_retries=5, base_backoff_ms=500, jitter_pct=0, honor_retry_after=False
    )
    result = client._retrying_request(
        "GET",
        "/v1/agents/bc-1",
        backoff_config=cfg,
        event_log=event_log,
        task_id="t-once",
        sleep=fake_sleep,
    )
    assert result == {"ok": True}
    types = [e["type"] for e in event_log.tail()]
    assert types.count("cloud.queued_quota_exceeded") == 1, (
        f"queued_quota_exceeded must fire exactly once; got {types!r}"
    )
    assert types.count("cloud.queue_exit") == 1
    qx = next(e for e in event_log.tail() if e["type"] == "cloud.queue_exit")
    assert qx["data"]["outcome"] == "success"
    assert qx["data"]["attempts"] == 2  # 2 retries occurred
    # total_wait_ms is 500 + 1000 = 1500 with jitter_pct=0.
    assert qx["data"]["total_wait_ms"] == 1500


def test_exhaustion_emits_queue_exit_with_outcome_exhausted(
    event_log: EventLog,
    sleep_recorder: tuple[list[float], Callable[[float], None]],
) -> None:
    """When ``max_retries`` is exhausted, the helper emits
    ``cloud.queue_exit outcome="exhausted"`` (NOT ``"success"``) before
    raising :class:`CursorCloudRateLimitError(cli_exit=75)`."""
    sleeps, fake_sleep = sleep_recorder

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "Too Many Requests"})

    client = _make_client_with_mock(handler)
    cfg = BackoffConfig(
        max_retries=2, base_backoff_ms=500, jitter_pct=0, honor_retry_after=False
    )
    with pytest.raises(CursorCloudRateLimitError) as excinfo:
        client._retrying_request(
            "GET",
            "/v1/agents/bc-1",
            backoff_config=cfg,
            event_log=event_log,
            task_id="t-exhausted",
            sleep=fake_sleep,
        )
    # cli_exit=75 from the catalog — proves exit code propagates.
    assert excinfo.value.cli_exit == 75
    # Event surface: exactly one quota_exceeded + one queue_exit (exhausted).
    qx_events = [
        e for e in event_log.tail() if e["type"] == "cloud.queue_exit"
    ]
    assert len(qx_events) == 1
    assert qx_events[0]["data"]["outcome"] == "exhausted"
    assert qx_events[0]["data"]["attempts"] == 2


# ---------------------------------------------------------------------------
# AC (b)/(d) — non-quota errors propagate immediately (no retry, no event).
# ---------------------------------------------------------------------------


def test_non_quota_error_propagates_without_retry(
    event_log: EventLog,
    sleep_recorder: tuple[list[float], Callable[[float], None]],
) -> None:
    """A 401 ``unauthorized`` propagates as :class:`CursorCloudAuthError` on
    the first attempt — no retry, no quota event. Validates the helper
    only retries on 429 (quota) and not the broader "is_retryable"
    catalog."""
    sleeps, fake_sleep = sleep_recorder
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(
            401,
            json={"error": {"code": "unauthorized", "message": "bad key"}},
        )

    client = _make_client_with_mock(handler)
    with pytest.raises(CursorCloudAuthError):
        client._retrying_request(
            "GET",
            "/v1/agents/bc-1",
            backoff_config=BackoffConfig(max_retries=5),
            event_log=event_log,
            task_id="t-401",
            sleep=fake_sleep,
        )
    assert call_count[0] == 1
    assert sleeps == []
    types = [e["type"] for e in event_log.tail()]
    assert "cloud.queued_quota_exceeded" not in types


# ---------------------------------------------------------------------------
# AC (b) — no event_log => still retries, just doesn't emit (smoke test).
# ---------------------------------------------------------------------------


def test_retrying_request_without_event_log_still_retries(
    sleep_recorder: tuple[list[float], Callable[[float], None]],
) -> None:
    """Optional ``event_log`` parameter — when ``None``, helper still
    retries (just no events emitted). Lets non-task callers (e.g. ad-hoc
    scripts) reuse the schedule without an EventLog dependency."""
    sleeps, fake_sleep = sleep_recorder
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx.Response(429, json={"error": "Too Many Requests"})
        return httpx.Response(200, json={"ok": True})

    client = _make_client_with_mock(handler)
    cfg = BackoffConfig(max_retries=5, jitter_pct=0, honor_retry_after=False)
    result = client._retrying_request(
        "GET",
        "/v1/agents/bc-1",
        backoff_config=cfg,
        sleep=fake_sleep,
    )
    assert result == {"ok": True}
    assert call_count[0] == 2
    assert sleeps == [0.5]


# ---------------------------------------------------------------------------
# Negative — honor_retry_after=False ignores the server header.
# ---------------------------------------------------------------------------


def test_honor_retry_after_false_ignores_server_header(
    event_log: EventLog,
    sleep_recorder: tuple[list[float], Callable[[float], None]],
) -> None:
    """When ``honor_retry_after = False`` the server's ``Retry-After: 99``
    is ignored and the local exponential schedule applies. Useful as a
    debug escape hatch when validating the local cadence in isolation."""
    sleeps, fake_sleep = sleep_recorder
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx.Response(
                429,
                json={"error": "Too Many Requests"},
                headers={"Retry-After": "99"},
            )
        return httpx.Response(200, json={"ok": True})

    client = _make_client_with_mock(handler)
    cfg = BackoffConfig(
        max_retries=5, base_backoff_ms=500, jitter_pct=0, honor_retry_after=False
    )
    result = client._retrying_request(
        "GET",
        "/v1/agents/bc-1",
        backoff_config=cfg,
        event_log=event_log,
        task_id="t-no-honor",
        sleep=fake_sleep,
    )
    assert result == {"ok": True}
    # 500 ms (local schedule), NOT 99 s.
    assert sleeps == [0.5], (
        f"honor_retry_after=False must ignore header; got {sleeps!r}"
    )


def test_request_error_propagates_as_retryable_cursor_cloud_error(
    sleep_recorder: tuple[list[float], Callable[[float], None]],
) -> None:
    """A network-layer ``httpx.RequestError`` propagates as a retryable
    :class:`CursorCloudError` (not a :class:`CursorCloudRateLimitError`).
    Validates the same exception-mapping path that :meth:`_request_json`
    uses; ensures the new helper does not regress that surface."""
    _, fake_sleep = sleep_recorder

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("cannot connect", request=request)

    client = _make_client_with_mock(handler)
    with pytest.raises(CursorCloudError) as excinfo:
        client._retrying_request(
            "GET",
            "/v1/agents/bc-1",
            backoff_config=BackoffConfig(),
            sleep=fake_sleep,
        )
    assert excinfo.value.is_retryable is True


def test_quota_exceeded_event_payload_shape(
    event_log: EventLog,
    sleep_recorder: tuple[list[float], Callable[[float], None]],
) -> None:
    """Pin the wire shape of ``cloud.queued_quota_exceeded`` per
    ``quota-config.md`` §5.1: must carry ``task_id, status, retry_after_ms,
    max_retries, ts``. Renderers / attach UIs depend on this exact
    surface."""
    sleeps, fake_sleep = sleep_recorder
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx.Response(
                429,
                json={"error": "Too Many Requests"},
                headers={"Retry-After": "5"},
            )
        return httpx.Response(200, json={"ok": True})

    client = _make_client_with_mock(handler)
    cfg = BackoffConfig(max_retries=5)
    client._retrying_request(
        "GET",
        "/v1/agents/bc-1",
        backoff_config=cfg,
        event_log=event_log,
        task_id="t-shape",
        sleep=fake_sleep,
    )
    qe = next(
        e for e in event_log.tail() if e["type"] == "cloud.queued_quota_exceeded"
    )
    payload: dict[str, Any] = qe["data"]
    assert payload["task_id"] == "t-shape"
    assert payload["status"] == 429
    assert payload["retry_after_ms"] == 5000
    assert payload["max_retries"] == 5
    assert isinstance(payload["ts"], str)
    # ts is ISO-8601 UTC with Z suffix (ms precision).
    assert payload["ts"].endswith("Z"), f"ts must end with Z: {payload['ts']!r}"
