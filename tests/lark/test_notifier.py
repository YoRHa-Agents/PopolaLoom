"""v0.4.1 Stage L2.A — :func:`send_terminal_notification` unit tests.

Per the L2 task spec (~ 5 cases): cover the three happy paths
(COMPLETED / FAILED / CANCELED), the ``CANCELED + sigkill_escalated``
escalated-card branch, and the silent-skip path when ``lark-cli`` is
unavailable.

Tests stub the underlying :func:`subprocess.run` call inside
:func:`popolaloom.hitl.renderers.lark.send_lark_card` via the
``runner=`` test seam (passed through by
:func:`send_terminal_notification`'s :func:`asyncio.to_thread`
wrapper). All tests live in the default lane (no ``slow`` /
``nightly`` / ``real_lark`` markers).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.state import StateStore, TaskHandle, TaskState
from popolaloom.lark.notifier import (
    LARK_NOTIFICATION_LOG_KEYS,
    NotificationOutcome,
    send_terminal_notification,
)


class _StubCompletedProcess:
    """Minimal :class:`subprocess.CompletedProcess` stand-in (test seam)."""

    def __init__(
        self, returncode: int = 0, stdout: str = "", stderr: str = ""
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakePopolad:
    """Light fake of :class:`popolaloom.daemon.server.Popolad` for unit tests.

    Exposes ``_state`` (real :class:`StateStore`) + ``event_log()``
    method so the notifier can pull a per-task NDJSON log without
    spinning up the full Popolad construction stack.
    """

    def __init__(self, *, events_dir: Path) -> None:
        self._state = StateStore()
        self._events_dir = events_dir
        self._event_logs: dict[str, EventLog] = {}

    def event_log(self, task_id: str) -> EventLog | None:
        return self._event_logs.get(task_id)

    def make_event_log(self, task_id: str) -> EventLog:
        log = EventLog(self._events_dir / f"{task_id}.jsonl", fsync_interval_s=0)
        self._event_logs[task_id] = log
        return log


def _register_handle(
    popolad: _FakePopolad,
    task_id: str,
    *,
    cli: str = "cursor",
    cmd: list[str] | None = None,
    state: TaskState = TaskState.RUNNING,
    cancel_escalated: bool = False,
) -> TaskHandle:
    handle = TaskHandle(
        task_id=task_id,
        cli=cli,
        pid=12345,
        state=state,
        started_at=datetime.now(UTC),
        event_log_path=popolad._events_dir / f"{task_id}.jsonl",
        cmd=cmd or [cli, "--prompt", "do something useful"],
        cancel_escalated_to_sigkill=cancel_escalated,
    )
    popolad._state.register(handle)
    return handle


def _make_runner(
    captured: list[list[str]],
    *,
    returncode: int = 0,
    stdout: str = '{"message_id": "om_test"}',
) -> Any:
    """Build a stub `runner` capturing argv per invocation."""

    def _runner(argv: list[str], **_kw: Any) -> _StubCompletedProcess:
        captured.append(list(argv))
        return _StubCompletedProcess(returncode=returncode, stdout=stdout)

    return _runner


# ── 1: COMPLETED happy path ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_terminal_notification_completed_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COMPLETED → builds completion card, calls send_lark_card with kind=terminal.

    Asserts:
    - :class:`NotificationOutcome` ``ok=True`` is returned.
    - ``send_lark_card`` argv contains ``--target-id <target>``.
    - ``send_lark_card`` argv contains ``--metadata-key task_id=<task_id>``.
    - The serialised card JSON contains the ``green`` template +
      "任务完成" header (sanity-check the right builder fired).
    - Per workspace rule: footer 来源标注 present in card JSON.
    """
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_test_target_completed")
    monkeypatch.setenv("LARK_NOTIFY_ON_COMPLETED", "1")

    popolad = _FakePopolad(events_dir=tmp_path)
    log = popolad.make_event_log("task-good-1")
    _register_handle(popolad, "task-good-1", cli="cursor")

    captured_argv: list[list[str]] = []
    runner = _make_runner(captured_argv)

    with patch("popolaloom.lark.notifier.is_lark_runtime_available", return_value=True), \
         patch("popolaloom.hitl.renderers.lark.subprocess.run", runner):
        outcome = await send_terminal_notification(
            popolad, "task-good-1", TaskState.COMPLETED, exit_code=0
        )

    assert outcome.ok is True
    assert outcome.skipped is False
    assert "trigger=completed" in (outcome.reason or "")

    assert len(captured_argv) == 1
    argv = captured_argv[0]
    assert "--target-id" in argv
    assert argv[argv.index("--target-id") + 1] == "ou_test_target_completed"
    assert "--metadata-key" in argv
    metadata_value = argv[argv.index("--metadata-key") + 1]
    assert metadata_value == "task_id=task-good-1"

    card_json_idx = argv.index("--card") + 1
    card_payload = json.loads(argv[card_json_idx])
    assert card_payload["header"]["template"] == "green"
    assert "任务完成" in card_payload["header"]["title"]["content"]
    assert "本消息由飞书工具 Lark-Cli 发送" in argv[card_json_idx]

    log.fsync()
    envelopes = log.tail()
    log.close()
    events = [e["type"] for e in envelopes]
    assert "lark.send.ok" in events, (
        f"expected NDJSON envelope written, got: {events}"
    )


# ── 2: FAILED happy path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_terminal_notification_failed_renders_failure_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILED → builds failure card; argv carries kind=terminal + red template."""
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_test_target_failed")
    monkeypatch.setenv("LARK_NOTIFY_ON_FAILED", "1")

    popolad = _FakePopolad(events_dir=tmp_path)
    popolad.make_event_log("task-bad-1")
    _register_handle(popolad, "task-bad-1", cli="claude")

    captured_argv: list[list[str]] = []
    runner = _make_runner(captured_argv)

    with patch("popolaloom.lark.notifier.is_lark_runtime_available", return_value=True), \
         patch("popolaloom.hitl.renderers.lark.subprocess.run", runner):
        outcome = await send_terminal_notification(
            popolad, "task-bad-1", TaskState.FAILED, exit_code=1
        )

    assert outcome.ok is True
    assert "trigger=failed" in (outcome.reason or "")

    assert len(captured_argv) == 1
    card_json = captured_argv[0][captured_argv[0].index("--card") + 1]
    payload = json.loads(card_json)
    assert payload["header"]["template"] == "red"
    assert "任务失败" in payload["header"]["title"]["content"]


# ── 3: CANCELED w/o escalation → canceled card (NOT escalated) ──────────


@pytest.mark.asyncio
async def test_send_terminal_notification_canceled_no_escalation_uses_canceled_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CANCELED + sigkill_escalated=False → yellow canceled card (NOT orange)."""
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_test_target_canceled")
    monkeypatch.setenv("LARK_NOTIFY_ON_CANCELED", "1")
    # Escalated env is irrelevant when sigkill_escalated=False
    monkeypatch.setenv("LARK_NOTIFY_ON_CANCEL_ESCALATED", "1")

    popolad = _FakePopolad(events_dir=tmp_path)
    popolad.make_event_log("task-cancel-1")
    _register_handle(
        popolad, "task-cancel-1", cli="codex",
        state=TaskState.CANCELED, cancel_escalated=False,
    )

    captured_argv: list[list[str]] = []
    runner = _make_runner(captured_argv)

    with patch("popolaloom.lark.notifier.is_lark_runtime_available", return_value=True), \
         patch("popolaloom.hitl.renderers.lark.subprocess.run", runner):
        outcome = await send_terminal_notification(
            popolad, "task-cancel-1", TaskState.CANCELED, exit_code=-15
        )

    assert outcome.ok is True
    assert "trigger=canceled" in (outcome.reason or "")

    card_json = captured_argv[0][captured_argv[0].index("--card") + 1]
    payload = json.loads(card_json)
    assert payload["header"]["template"] == "yellow"
    assert "任务已取消" in payload["header"]["title"]["content"]
    # Specifically NOT the escalated card (which would be orange)
    assert "取消升级" not in payload["header"]["title"]["content"]


# ── 4: CANCELED + escalated + env on → cancel_escalated card ────────────


@pytest.mark.asyncio
async def test_send_terminal_notification_canceled_with_escalation_uses_escalated_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CANCELED + sigkill_escalated=True + ON_CANCEL_ESCALATED=1 → orange card.

    This proves the env-var gate flips the renderer to the
    cancel_escalated builder when the operator explicitly opts in.
    """
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_test_target_escalated")
    monkeypatch.setenv("LARK_NOTIFY_ON_CANCELED", "1")
    monkeypatch.setenv("LARK_NOTIFY_ON_CANCEL_ESCALATED", "1")

    popolad = _FakePopolad(events_dir=tmp_path)
    popolad.make_event_log("task-cancel-hard")
    _register_handle(
        popolad, "task-cancel-hard", cli="cursor",
        state=TaskState.CANCELED, cancel_escalated=True,
    )

    captured_argv: list[list[str]] = []
    runner = _make_runner(captured_argv)

    with patch("popolaloom.lark.notifier.is_lark_runtime_available", return_value=True), \
         patch("popolaloom.hitl.renderers.lark.subprocess.run", runner):
        outcome = await send_terminal_notification(
            popolad, "task-cancel-hard", TaskState.CANCELED, exit_code=-9
        )

    assert outcome.ok is True
    assert "trigger=cancel_escalated" in (outcome.reason or "")

    card_json = captured_argv[0][captured_argv[0].index("--card") + 1]
    payload = json.loads(card_json)
    assert payload["header"]["template"] == "orange"
    assert "取消升级 SIGKILL" in payload["header"]["title"]["content"]


# ── 5: lark-cli unavailable → skip silently with explicit reason ────────


@pytest.mark.asyncio
async def test_send_terminal_notification_skips_when_lark_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``is_lark_runtime_available() == False`` → ``NotificationOutcome.skip``.

    Asserts:
    - returns ``ok=False, skipped=True, reason='lark_cli_unavailable'``.
    - The underlying ``subprocess.run`` is NEVER invoked.
    - An INFO log line records the skip reason (No Silent Failures).
    - The ``LARK_NOTIFICATION_LOG_KEYS`` constant is exported and
      matches the expected NDJSON event-type tuple.
    """
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_irrelevant_when_no_cli")
    monkeypatch.setenv("LARK_NOTIFY_ON_COMPLETED", "1")

    popolad = _FakePopolad(events_dir=tmp_path)
    popolad.make_event_log("task-no-cli")
    _register_handle(popolad, "task-no-cli")

    runner_spy: list[list[str]] = []
    runner = _make_runner(runner_spy)

    import logging
    with patch("popolaloom.lark.notifier.is_lark_runtime_available", return_value=False), \
         patch("popolaloom.hitl.renderers.lark.subprocess.run", runner), \
         caplog.at_level(logging.INFO, logger="popolaloom.lark.notifier"):
        outcome = await send_terminal_notification(
            popolad, "task-no-cli", TaskState.COMPLETED, exit_code=0
        )

    assert outcome == NotificationOutcome.skip("lark_cli_unavailable")
    assert outcome.ok is False
    assert outcome.skipped is True
    assert outcome.reason == "lark_cli_unavailable"
    assert runner_spy == [], (
        "lark-cli runner must NOT be invoked when is_lark_runtime_available is False"
    )
    assert any(
        "lark.notify.skipped" in rec.getMessage()
        and "lark_cli_unavailable" in rec.getMessage()
        for rec in caplog.records
    ), f"expected explicit skip log line; got: {[r.getMessage() for r in caplog.records]}"

    # Compatibility-contract sanity: the constant lives where v0.5.0 doctor
    # (per v0.4.1 plan §0.5 row #5) will import from.
    assert LARK_NOTIFICATION_LOG_KEYS == ("lark.send.ok", "lark.send.failed")


# ── 6: non-terminal state → skip silently with explicit reason ──────────


@pytest.mark.asyncio
async def test_send_terminal_notification_skips_for_non_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RUNNING / PENDING are dropped with ``non_terminal_state=...`` reason.

    Defensive: if a future caller fans into the notifier with a wrong
    state, we never crash — we surface the misuse via the skip reason
    so the L0 plan can see the leak in production logs.
    """
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_irrelevant")
    popolad = _FakePopolad(events_dir=tmp_path)
    popolad.make_event_log("task-running")
    _register_handle(popolad, "task-running", state=TaskState.RUNNING)

    with patch("popolaloom.lark.notifier.is_lark_runtime_available", return_value=True):
        outcome = await send_terminal_notification(
            popolad, "task-running", TaskState.RUNNING, exit_code=None
        )

    assert outcome.skipped is True
    assert outcome.reason is not None
    assert "non_terminal_state" in outcome.reason


# ── 7: target open_id unset → skip ───────────────────────────────────────


@pytest.mark.asyncio
async def test_send_terminal_notification_skips_when_target_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both ``LARK_NOTIFY_TARGET_OPEN_ID`` and ``LARK_HITL_TARGET_OPEN_ID`` unset → skip."""
    monkeypatch.delenv("LARK_NOTIFY_TARGET_OPEN_ID", raising=False)
    monkeypatch.delenv("LARK_HITL_TARGET_OPEN_ID", raising=False)

    popolad = _FakePopolad(events_dir=tmp_path)
    popolad.make_event_log("task-no-target")
    _register_handle(popolad, "task-no-target")

    with patch("popolaloom.lark.notifier.is_lark_runtime_available", return_value=True):
        outcome = await send_terminal_notification(
            popolad, "task-no-target", TaskState.COMPLETED, exit_code=0
        )

    assert outcome.skipped is True
    assert outcome.reason == "target_open_id_unset"


# ── 8: LARK_NOTIFY_TARGET_OPEN_ID overrides the HITL fallback ───────────


@pytest.mark.asyncio
async def test_send_terminal_notification_notify_target_overrides_hitl_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``LARK_NOTIFY_TARGET_OPEN_ID`` takes precedence over the HITL env var."""
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_hitl_fallback")
    monkeypatch.setenv("LARK_NOTIFY_TARGET_OPEN_ID", "ou_notify_explicit")
    monkeypatch.setenv("LARK_NOTIFY_ON_COMPLETED", "1")

    popolad = _FakePopolad(events_dir=tmp_path)
    popolad.make_event_log("task-override")
    _register_handle(popolad, "task-override")

    captured: list[list[str]] = []
    runner = _make_runner(captured)
    with patch("popolaloom.lark.notifier.is_lark_runtime_available", return_value=True), \
         patch("popolaloom.hitl.renderers.lark.subprocess.run", runner):
        outcome = await send_terminal_notification(
            popolad, "task-override", TaskState.COMPLETED, exit_code=0
        )

    assert outcome.ok is True
    target = captured[0][captured[0].index("--target-id") + 1]
    assert target == "ou_notify_explicit"


# ── 9: handle missing from StateStore → skip ────────────────────────────


@pytest.mark.asyncio
async def test_send_terminal_notification_skips_when_handle_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Race window: cancel removed handle before wait-thread fired → skip."""
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_anything")
    popolad = _FakePopolad(events_dir=tmp_path)
    # Intentionally do NOT register a handle.

    with patch("popolaloom.lark.notifier.is_lark_runtime_available", return_value=True):
        outcome = await send_terminal_notification(
            popolad, "task-vanished", TaskState.COMPLETED, exit_code=0
        )

    assert outcome.skipped is True
    assert outcome.reason == "handle_not_in_state_store"


# ── 10: env-off skip → ON_COMPLETED=0 ────────────────────────────────────


@pytest.mark.asyncio
async def test_send_terminal_notification_skips_when_env_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``LARK_NOTIFY_ON_COMPLETED=0`` → skip with ``env_off var=...``."""
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_x")
    monkeypatch.setenv("LARK_NOTIFY_ON_COMPLETED", "0")

    popolad = _FakePopolad(events_dir=tmp_path)
    popolad.make_event_log("t")
    _register_handle(popolad, "t")

    with patch("popolaloom.lark.notifier.is_lark_runtime_available", return_value=True):
        outcome = await send_terminal_notification(
            popolad, "t", TaskState.COMPLETED, exit_code=0
        )

    assert outcome.skipped is True
    assert outcome.reason is not None
    assert "env_off" in outcome.reason
    assert "LARK_NOTIFY_ON_COMPLETED" in outcome.reason


# ── 11: send-failure path (lark-cli rc != 0) → NotificationOutcome.failure ─


@pytest.mark.asyncio
async def test_send_terminal_notification_returns_failure_when_lark_cli_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``lark-cli`` returncode != 0 across all retries → failure outcome."""
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_failure_path")
    monkeypatch.setenv("LARK_NOTIFY_ON_COMPLETED", "1")

    popolad = _FakePopolad(events_dir=tmp_path)
    popolad.make_event_log("task-cli-bad")
    _register_handle(popolad, "task-cli-bad")

    def bad_runner(argv: list[str], **_kw: Any) -> _StubCompletedProcess:
        return _StubCompletedProcess(
            returncode=2, stdout="", stderr="lark-cli: server error 500"
        )

    with patch("popolaloom.lark.notifier.is_lark_runtime_available", return_value=True), \
         patch("popolaloom.hitl.renderers.lark.subprocess.run", bad_runner), \
         patch("popolaloom.hitl.renderers.lark.time.sleep"):  # skip backoff sleeps
        outcome = await send_terminal_notification(
            popolad, "task-cli-bad", TaskState.COMPLETED, exit_code=0
        )

    assert outcome.ok is False
    assert outcome.skipped is False
    assert outcome.reason is not None
    assert "send_failed" in outcome.reason


# ── 12: card-build failure → returns failure (No Silent Failures) ───────


@pytest.mark.asyncio
async def test_send_terminal_notification_handles_card_build_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``_select_terminal_card`` raises (e.g. invalid handle) → failure outcome."""
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_card_break")
    monkeypatch.setenv("LARK_NOTIFY_ON_FAILED", "1")

    popolad = _FakePopolad(events_dir=tmp_path)
    popolad.make_event_log("t-card-break")
    _register_handle(popolad, "t-card-break")

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("synthetic card builder failure")

    with patch("popolaloom.lark.notifier.is_lark_runtime_available", return_value=True), \
         patch("popolaloom.lark.notifier._select_terminal_card", side_effect=boom):
        outcome = await send_terminal_notification(
            popolad, "t-card-break", TaskState.FAILED, exit_code=1
        )

    assert outcome.ok is False
    assert outcome.skipped is False
    assert outcome.reason is not None
    assert "card_build_failed" in outcome.reason


# ── 13: prompt summary truncated by env override ────────────────────────


@pytest.mark.asyncio
async def test_send_terminal_notification_respects_prompt_truncate_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``LARK_NOTIFY_PROMPT_TRUNCATE=60`` truncates prompt to 60 chars + ellipsis."""
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_trunc")
    monkeypatch.setenv("LARK_NOTIFY_ON_COMPLETED", "1")
    monkeypatch.setenv("LARK_NOTIFY_PROMPT_TRUNCATE", "60")

    long_cmd = ["cursor", "--prompt", "y" * 200]
    popolad = _FakePopolad(events_dir=tmp_path)
    popolad.make_event_log("task-trunc")
    _register_handle(popolad, "task-trunc", cmd=long_cmd)

    captured: list[list[str]] = []
    runner = _make_runner(captured)
    with patch("popolaloom.lark.notifier.is_lark_runtime_available", return_value=True), \
         patch("popolaloom.hitl.renderers.lark.subprocess.run", runner):
        outcome = await send_terminal_notification(
            popolad, "task-trunc", TaskState.COMPLETED, exit_code=0
        )

    assert outcome.ok is True
    card = json.loads(captured[0][captured[0].index("--card") + 1])
    body_text = card["body"]["elements"][0]["text"]["content"]
    assert "…" in body_text


# ── 14: prompt-truncate env var fallbacks (invalid integer) ─────────────


@pytest.mark.asyncio
async def test_send_terminal_notification_invalid_truncate_env_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Invalid ``LARK_NOTIFY_PROMPT_TRUNCATE`` → default 200 + INFO log."""
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_x")
    monkeypatch.setenv("LARK_NOTIFY_ON_COMPLETED", "1")
    monkeypatch.setenv("LARK_NOTIFY_PROMPT_TRUNCATE", "not-an-int")

    popolad = _FakePopolad(events_dir=tmp_path)
    popolad.make_event_log("t-bad-trunc")
    _register_handle(popolad, "t-bad-trunc")

    captured: list[list[str]] = []
    runner = _make_runner(captured)

    import logging
    with patch("popolaloom.lark.notifier.is_lark_runtime_available", return_value=True), \
         patch("popolaloom.hitl.renderers.lark.subprocess.run", runner), \
         caplog.at_level(logging.INFO, logger="popolaloom.lark.notifier"):
        outcome = await send_terminal_notification(
            popolad, "t-bad-trunc", TaskState.COMPLETED, exit_code=0
        )
    assert outcome.ok is True
    assert any(
        "LARK_NOTIFY_PROMPT_TRUNCATE" in rec.getMessage()
        for rec in caplog.records
    )


# ── 15: CANCELED + escalated + ON_CANCEL_ESCALATED=0 falls back to canceled card ─


@pytest.mark.asyncio
async def test_send_terminal_notification_cancel_escalated_off_uses_canceled_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sigkill_escalated=True but env opt-out → fall back to plain canceled card."""
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_x")
    monkeypatch.setenv("LARK_NOTIFY_ON_CANCELED", "1")
    monkeypatch.setenv("LARK_NOTIFY_ON_CANCEL_ESCALATED", "0")  # default OFF

    popolad = _FakePopolad(events_dir=tmp_path)
    popolad.make_event_log("t-esc-off")
    _register_handle(
        popolad, "t-esc-off",
        state=TaskState.CANCELED, cancel_escalated=True,
    )

    captured: list[list[str]] = []
    runner = _make_runner(captured)
    with patch("popolaloom.lark.notifier.is_lark_runtime_available", return_value=True), \
         patch("popolaloom.hitl.renderers.lark.subprocess.run", runner):
        outcome = await send_terminal_notification(
            popolad, "t-esc-off", TaskState.CANCELED, exit_code=-9
        )

    assert outcome.ok is True
    assert "trigger=canceled" in (outcome.reason or "")
    card = json.loads(captured[0][captured[0].index("--card") + 1])
    assert card["header"]["template"] == "yellow"
    assert "取消升级" not in card["header"]["title"]["content"]


# ── 16: NotificationOutcome dataclass is frozen ─────────────────────────


def test_notification_outcome_is_frozen() -> None:
    """The dataclass is frozen so the v0.5.0 contract surface is immutable."""
    from dataclasses import FrozenInstanceError

    outcome = NotificationOutcome.success()
    with pytest.raises(FrozenInstanceError):
        outcome.ok = False  # type: ignore[misc]


# ── 17: prompt_summary attribute (non-cmd path) ─────────────────────────


@pytest.mark.asyncio
async def test_send_terminal_notification_uses_handle_prompt_summary_when_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the handle exposes ``.prompt_summary``, use it instead of cmd join.

    Forward-compatible with v0.5.0 if a ``prompt_summary`` field is
    added to :class:`TaskHandle`; v0.4.1 just sets it via setattr.
    """
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_x")
    monkeypatch.setenv("LARK_NOTIFY_ON_COMPLETED", "1")

    popolad = _FakePopolad(events_dir=tmp_path)
    popolad.make_event_log("t-summary")
    handle = _register_handle(popolad, "t-summary")
    handle.__dict__["prompt_summary"] = "explicit summary 12345"  # type: ignore[index]

    captured: list[list[str]] = []
    runner = _make_runner(captured)
    with patch("popolaloom.lark.notifier.is_lark_runtime_available", return_value=True), \
         patch("popolaloom.hitl.renderers.lark.subprocess.run", runner):
        outcome = await send_terminal_notification(
            popolad, "t-summary", TaskState.COMPLETED, exit_code=0
        )
    assert outcome.ok is True
    card = json.loads(captured[0][captured[0].index("--card") + 1])
    body_text = card["body"]["elements"][0]["text"]["content"]
    assert "explicit summary 12345" in body_text


# ── 18: popolad without _state attribute → skip (defensive) ─────────────


@pytest.mark.asyncio
async def test_send_terminal_notification_skips_when_state_store_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stripping ``_state`` from popolad triggers ``popolad_state_store_missing`` skip."""
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_anything")

    class _BareFake:
        # No ``_state`` attribute at all (mimics test stub)
        pass

    popolad = _BareFake()

    with patch("popolaloom.lark.notifier.is_lark_runtime_available", return_value=True):
        outcome = await send_terminal_notification(
            popolad, "t", TaskState.COMPLETED, exit_code=0  # type: ignore[arg-type]
        )
    assert outcome.skipped is True
    assert outcome.reason == "popolad_state_store_missing"
