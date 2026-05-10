"""v0.9.9 F1 — supervisor stdout-silence timer tests (Q-V099-5 + Q-V099-14).

Covers the five surfaces the v0.9.9 plan binds for the F1 patch:

(a) text-mode 30s silence emits ``process.note`` with the verbatim
    text-mode hint from ``feedback_for_v0.9.7.md:33-34``;
(b) stream-json mode 30s silence emits ``process.note`` with the
    branched stream-json hint per Q-V099-14;
(c) the FIRST non-empty stdout line cancels the timer (no fire);
(d) ``_wait_and_finalize`` cancels the timer on exit-before-fire;
(e) other CLIs (claude / codex) emit the generic stdout-silence note.

Hermetic — every test monkeypatches the module-level constant
:data:`popolaloom.daemon.supervisor._SILENCE_TIMEOUT_SECS` to a small
value (≈ 0.05 s) so the silence path runs in milliseconds instead of
the 30 s production default.  Real :class:`subprocess.Popen` children
(short Python wrappers placed under ``tmp_path`` with the canonical
adapter binary name) drive the spawn / drain / wait pipeline so the
cancellation hooks are exercised end-to-end.  This avoids mocking
``Popen`` itself — the silence timer's interaction with the drain and
wait threads is exactly what we want to cover, not just the helper
functions in isolation.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from popolaloom.daemon import supervisor as supervisor_module
from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.supervisor import Supervisor


def _wait_for_event_type(
    event_log: EventLog,
    event_type: str,
    timeout_s: float = 4.0,
) -> dict[str, object] | None:
    """Poll ``event_log.tail()`` until an envelope of ``event_type`` appears."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for ev in event_log.tail():
            if ev["type"] == event_type:
                return ev
        time.sleep(0.01)
    return None


def _types_in(event_log: EventLog) -> list[str]:
    """Return event ``type`` strings, in order, for a final-state log."""
    return [ev["type"] for ev in event_log.tail()]


def _make_fake_binary(
    tmp_path: Path, name: str, *, sleep_s: float = 5.0
) -> str:
    """Drop an executable Python wrapper named ``name`` under ``tmp_path``.

    The wrapper sleeps for ``sleep_s`` seconds without printing anything
    so the supervisor's stdout-silence timer is guaranteed to fire (or
    be cancelled by the wait-thread) before the child finishes.  The
    important detail for the F1 tests is the basename: by spawning the
    child via ``cmd[0] = <tmp_path>/cursor-agent`` we exercise the real
    :func:`popolaloom.daemon.supervisor._detect_cli_name_from_cmd`
    branch instead of relying on ``sys.executable`` (which would map to
    ``python`` / ``python3`` and never trigger the cursor branch).
    """
    binary = tmp_path / name
    binary.write_text(
        f"#!{sys.executable}\n"
        "import time, sys\n"
        f"time.sleep({sleep_s})\n"
        "sys.exit(0)\n"
    )
    binary.chmod(0o755)
    return str(binary)


# ── case (a): cursor + text mode → verbatim feedback wording ─────────────


def test_text_mode_silence_emits_verbatim_feedback_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence in text mode → ``process.note`` with ``_CURSOR_TEXT_HINT``."""
    monkeypatch.setattr(supervisor_module, "_SILENCE_TIMEOUT_SECS", 0.05)
    fake_cursor = _make_fake_binary(tmp_path, "cursor-agent", sleep_s=2.0)

    log_path = tmp_path / "text-silence.jsonl"
    event_log = EventLog(log_path, fsync_interval_s=0)
    sup = Supervisor()

    sup.spawn(
        task_id="text-silence",
        cmd=[
            fake_cursor,
            "agent",
            "--print",
            "--output-format",
            "text",
            "do something",
        ],
        cwd=None,
        env=None,
        event_log=event_log,
    )
    try:
        note = _wait_for_event_type(event_log, "process.note", timeout_s=2.0)
        assert note is not None, (
            f"expected process.note to fire, got types: {_types_in(event_log)}"
        )
        assert note["data"]["task_id"] == "text-silence"
        assert note["data"]["kind"] == "stdout_silence"
        assert "elapsed_seconds" in note["data"]
        assert note["data"]["hint"] == supervisor_module._CURSOR_TEXT_HINT
    finally:
        sup._cancel_silence_timer("text-silence")
        sup.join("text-silence", timeout=3.0)
        event_log.close()


# ── case (b): cursor + stream-json mode → branched hint ──────────────────


def test_stream_json_mode_silence_emits_branched_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream-json silence → ``process.note`` with the branched wording."""
    monkeypatch.setattr(supervisor_module, "_SILENCE_TIMEOUT_SECS", 0.05)
    fake_cursor = _make_fake_binary(tmp_path, "cursor-agent", sleep_s=2.0)

    log_path = tmp_path / "stream-silence.jsonl"
    event_log = EventLog(log_path, fsync_interval_s=0)
    sup = Supervisor()

    sup.spawn(
        task_id="stream-silence",
        cmd=[
            fake_cursor,
            "agent",
            "--print",
            "--output-format",
            "stream-json",
            "long prompt",
        ],
        cwd=None,
        env=None,
        event_log=event_log,
    )
    try:
        note = _wait_for_event_type(event_log, "process.note", timeout_s=2.0)
        assert note is not None, (
            f"expected process.note for stream-json silence, "
            f"got types: {_types_in(event_log)}"
        )
        assert note["data"]["hint"] == supervisor_module._CURSOR_STREAM_JSON_HINT
        assert note["data"]["kind"] == "stdout_silence"
    finally:
        sup._cancel_silence_timer("stream-silence")
        sup.join("stream-silence", timeout=3.0)
        event_log.close()


# ── case (c): first non-empty stdout line cancels the timer ──────────────


def test_first_nonempty_stdout_line_cancels_silence_timer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chatty subprocess prevents the silence note from ever firing.

    The child prints once at t=0 (well before the 0.5 s timer) and then
    sleeps for ~2 s — long enough that we can wait past the timer
    deadline and still observe the silence note absent from the event
    log, which is the exact contract the drain-thread cancel hook must
    uphold.
    """
    monkeypatch.setattr(supervisor_module, "_SILENCE_TIMEOUT_SECS", 0.5)

    log_path = tmp_path / "chatty.jsonl"
    event_log = EventLog(log_path, fsync_interval_s=0)
    sup = Supervisor()

    sup.spawn(
        task_id="chatty",
        cmd=[
            sys.executable,
            "-c",
            (
                "import sys, time;"
                "print('hello'); sys.stdout.flush();"
                "time.sleep(2.0); print('done')"
            ),
        ],
        cwd=None,
        env=None,
        event_log=event_log,
    )
    try:
        first_stdout = _wait_for_event_type(
            event_log, "process.stdout", timeout_s=3.0
        )
        assert first_stdout is not None
        time.sleep(1.0)
        types = _types_in(event_log)
        assert "process.note" not in types, (
            f"silence timer should be cancelled by first stdout line, "
            f"but got types={types}"
        )
        assert sup.join("chatty", timeout=4.0)
        assert "process.note" not in _types_in(event_log)
    finally:
        sup._cancel_silence_timer("chatty")
        event_log.close()


# ── case (d): _wait_and_finalize cancels on exit-before-fire ─────────────


def test_wait_and_finalize_cancels_silence_timer_on_quick_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subprocess that exits before the timer fires must NOT emit the note.

    Reuses a fast timer (0.5 s) so the wait-thread's exit-before-fire
    cancel hook is the ONLY thing that can prevent the emit (the child
    here writes nothing to stdout / stderr at all, so the drain-thread
    cancel hook in case (c) cannot intervene).
    """
    monkeypatch.setattr(supervisor_module, "_SILENCE_TIMEOUT_SECS", 0.5)

    log_path = tmp_path / "fast-exit.jsonl"
    event_log = EventLog(log_path, fsync_interval_s=0)
    sup = Supervisor()

    sup.spawn(
        task_id="fast-exit",
        cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
        cwd=None,
        env=None,
        event_log=event_log,
    )
    try:
        assert sup.join("fast-exit", timeout=3.0)
        time.sleep(0.7)
        types = _types_in(event_log)
        assert "task.completed" in types
        assert "process.note" not in types, (
            f"silence timer should be cancelled by wait-and-finalize, "
            f"got types={types}"
        )
    finally:
        sup._cancel_silence_timer("fast-exit")
        event_log.close()


# ── case (e): other CLIs → generic stdout-silence note ───────────────────


@pytest.mark.parametrize(
    "binary_name",
    ["claude", "codex"],
)
def test_other_cli_emits_generic_silence_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binary_name: str,
) -> None:
    """Non-cursor CLIs → generic ``<cli> stdout has been silent ...`` note."""
    monkeypatch.setattr(supervisor_module, "_SILENCE_TIMEOUT_SECS", 0.05)
    fake_binary = _make_fake_binary(tmp_path, binary_name, sleep_s=2.0)

    log_path = tmp_path / f"{binary_name}-silence.jsonl"
    event_log = EventLog(log_path, fsync_interval_s=0)
    sup = Supervisor()

    sup.spawn(
        task_id=f"{binary_name}-silence",
        cmd=[fake_binary, "--some-flag", "go"],
        cwd=None,
        env=None,
        event_log=event_log,
    )
    try:
        note = _wait_for_event_type(event_log, "process.note", timeout_s=2.0)
        assert note is not None, (
            f"expected generic process.note for {binary_name}, "
            f"got types: {_types_in(event_log)}"
        )
        hint = note["data"]["hint"]
        assert isinstance(hint, str)
        assert hint.startswith(f"{binary_name} stdout has been silent")
        assert "30s" in hint
        assert hint != supervisor_module._CURSOR_TEXT_HINT
        assert hint != supervisor_module._CURSOR_STREAM_JSON_HINT
    finally:
        sup._cancel_silence_timer(f"{binary_name}-silence")
        sup.join(f"{binary_name}-silence", timeout=3.0)
        event_log.close()


# ── unit coverage for the small pure helpers ─────────────────────────────


def test_detect_cli_name_from_cmd_handles_basenames() -> None:
    """``cursor-agent`` (with or without path) → ``"cursor"``; others passthrough."""
    detect = supervisor_module._detect_cli_name_from_cmd
    assert detect(["/usr/local/bin/cursor-agent", "agent"]) == "cursor"
    assert detect(["cursor-agent", "agent"]) == "cursor"
    assert detect(["claude", "--print"]) == "claude"
    assert detect(["codex"]) == "codex"
    assert detect([]) is None


def test_detect_cursor_output_format_from_cmd() -> None:
    """``--output-format <fmt>`` is extracted; missing flag → ``None``."""
    detect = supervisor_module._detect_cursor_output_format_from_cmd
    assert (
        detect(["cursor-agent", "agent", "--print", "--output-format", "text", "p"])
        == "text"
    )
    assert (
        detect(
            [
                "cursor-agent",
                "agent",
                "--print",
                "--output-format",
                "stream-json",
                "p",
            ]
        )
        == "stream-json"
    )
    assert detect(["cursor-agent", "agent", "--print", "p"]) is None
    assert detect(["cursor-agent", "--output-format"]) is None


def test_silence_hint_for_branched_wording() -> None:
    """The branched hint helper picks the right string per Q-V099-14."""
    hint = supervisor_module._silence_hint_for
    assert hint("cursor", "text") == supervisor_module._CURSOR_TEXT_HINT
    assert hint("cursor", None) == supervisor_module._CURSOR_TEXT_HINT
    assert (
        hint("cursor", "stream-json")
        == supervisor_module._CURSOR_STREAM_JSON_HINT
    )
    claude_hint = hint("claude", None)
    assert claude_hint.startswith("claude stdout has been silent")
    assert "30s" in claude_hint
    none_hint = hint(None, None)
    assert none_hint.startswith("process stdout has been silent")
