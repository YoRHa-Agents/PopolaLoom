"""``popola cloud worker stop`` v0.9.9 (F6) — tests.

Covers two changes shipped in :mod:`popolaloom.cli.cloud_worker_cmd`
to close ``feedback_for_v0.9.7.md:88-96``:

Part A — :func:`_run_subprocess` rewrite:

* The 1-arg ``argv: list[str] -> int`` signature is preserved
  (Q-V099-15) so existing monkeypatches in
  :file:`tests/cli/test_cloud_worker_cmd.py` keep working.
* The child is spawned with ``start_new_session=True`` so it becomes
  the leader of its own process group; SIGTERM / SIGINT delivered
  to the wrapper cascade to the child via :func:`os.killpg`.
* Signal handlers are installed in a ``try/finally`` so they are
  always restored.

Part B — new ``popola cloud worker stop`` Typer verb:

* ``--name`` selector matches via :func:`_iter_proc_cmdlines`.
* ``--worker-dir`` selector matches via
  :func:`_detect_running_workers_for_dir`.
* SIGTERM-then-SIGKILL grace fallback (``--grace`` default 5.0s).
* ``--help`` text contains the verbatim Q-V099-6 caveat sentence
  (``Stops the worker even if a Cloud Agent session is currently
  claimed; compose with `popola cloud worker status --busy` to
  gate.``).
* Neither selector → :data:`_EXIT_INVALID_ARGS`.
* No matching worker → :data:`_EXIT_UNREACHABLE`.

All tests are hermetic: they monkeypatch the procfs scanner, the
``os.getpgid`` / ``os.killpg`` syscalls, and the liveness poll so no
real subprocess (or real signal delivery) leaks out of the test
process.  The signal-cascade integration test
(``test_run_subprocess_forwards_sigterm_to_child_process_group``)
spawns a tiny throwaway Python wrapper so we can exercise the
*real* ``setsid`` + ``killpg`` path end-to-end on a POSIX host.
"""

from __future__ import annotations

import inspect
import logging
import os
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import popolaloom
from popolaloom.cli import cloud_worker_cmd

# ---------------------------------------------------------------------------
# Fixtures (mirror :file:`tests/cli/test_cloud_worker_cmd.py::isolated_home`,
# including the v0.9.9 B2 `account_class = service_account` pre-seed so the
# pre-flight gate stays out of our way for unrelated regressions).
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    """Default Typer ``CliRunner``."""
    return CliRunner()


@pytest.fixture
def isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Hermetic ``$POPOLA_HOME`` + ``$HOME`` (mirrors the existing fixture).

    Pre-seeds ``credentials.toml`` with ``account_class = service_account``
    so the v0.9.9 B2 pre-flight gate (Q-V099-1 / Q-V099-8) stays out of
    the way of the tests in this module — they exercise the
    ``stop`` verb / ``_run_subprocess`` rewrite, which never reach
    the dispatch gate.
    """
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    metadata = tmp_path / "credentials.toml"
    metadata.write_text(
        '[cursor]\naccount_class = "service_account"\n', encoding="utf-8"
    )
    metadata.chmod(0o600)
    yield tmp_path


def _combined_output(result: Any) -> str:
    """Return ``stdout + stderr`` as a single string (Typer / Click compat)."""
    parts: list[str] = []
    for attr in ("stdout", "stderr", "output"):
        try:
            value = getattr(result, attr, "") or ""
        except (ValueError, AttributeError):
            value = ""
        if value and value not in parts:
            parts.append(value)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Part A — `_run_subprocess` rewrite
# ---------------------------------------------------------------------------


def test_run_subprocess_signature_preserved() -> None:
    """The 1-arg ``argv: list[str] -> int`` signature must stay (Q-V099-15).

    The B2 / B-historic monkeypatches in
    :file:`tests/cli/test_cloud_worker_cmd.py` use ``lambda argv: 0`` —
    breaking the signature would silently break ~10 existing tests.
    """
    sig = inspect.signature(cloud_worker_cmd._run_subprocess)
    params = list(sig.parameters.values())
    assert len(params) == 1, f"expected 1 positional param, got {params!r}"
    argv_param = params[0]
    assert argv_param.name == "argv", argv_param.name
    assert argv_param.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.POSITIONAL_ONLY,
    }, argv_param.kind
    assert argv_param.annotation == "list[str]" or argv_param.annotation is list, (
        f"unexpected annotation {argv_param.annotation!r}"
    )
    assert sig.return_annotation == "int" or sig.return_annotation is int, (
        f"unexpected return annotation {sig.return_annotation!r}"
    )


def test_run_subprocess_smoke_returns_zero(tmp_path: Path) -> None:
    """A trivial ``python -c`` child returns 0 through the new Popen path."""
    rc = cloud_worker_cmd._run_subprocess(
        [sys.executable, "-c", "import time; time.sleep(0.05)"]
    )
    assert rc == 0


def test_run_subprocess_propagates_nonzero_exit(tmp_path: Path) -> None:
    """Non-zero child exit code must propagate verbatim (No Silent Failures)."""
    rc = cloud_worker_cmd._run_subprocess(
        [sys.executable, "-c", "import sys; sys.exit(42)"]
    )
    assert rc == 42


def test_run_subprocess_uses_start_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child Popen must be created with ``start_new_session=True``.

    Establishing the new session is what lets the signal forwarder
    target the child's whole pgroup via :func:`os.killpg` (Q-V099-15
    + ``feedback_for_v0.9.7.md:88-96``).
    """
    captured_kwargs: dict[str, Any] = {}

    class _FakeChild:
        def __init__(self) -> None:
            self.pid = 99999

        def wait(self) -> int:
            return 0

    def fake_popen(argv: list[str], **kwargs: Any) -> _FakeChild:
        captured_kwargs.update(kwargs)
        return _FakeChild()

    monkeypatch.setattr(cloud_worker_cmd.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        cloud_worker_cmd.os, "getpgid", lambda pid: pid
    )

    rc = cloud_worker_cmd._run_subprocess(["echo", "hi"])
    assert rc == 0
    assert captured_kwargs.get("start_new_session") is True, captured_kwargs


def test_run_subprocess_installs_and_restores_signal_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handlers for SIGTERM + SIGINT are installed and restored via try/finally.

    Security-critical code path: a leaked forwarder would steal
    SIGTERM / SIGINT from later test cases / unrelated CLI flows.
    """
    installed: list[tuple[int, Any]] = []

    original_signal = signal.signal

    def tracking_signal(sig: int, handler: Any) -> Any:
        installed.append((sig, handler))
        return original_signal(sig, handler)

    monkeypatch.setattr(cloud_worker_cmd.signal, "signal", tracking_signal)

    class _FakeChild:
        def __init__(self) -> None:
            self.pid = 88888

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(
        cloud_worker_cmd.subprocess, "Popen", lambda *a, **kw: _FakeChild()
    )
    monkeypatch.setattr(cloud_worker_cmd.os, "getpgid", lambda pid: pid)

    pre_term = signal.getsignal(signal.SIGTERM)
    pre_int = signal.getsignal(signal.SIGINT)

    rc = cloud_worker_cmd._run_subprocess(["echo", "hi"])
    assert rc == 0

    sigs_seen = [entry[0] for entry in installed]
    assert signal.SIGTERM in sigs_seen
    assert signal.SIGINT in sigs_seen
    install_count = sum(1 for s in sigs_seen if s in {signal.SIGTERM, signal.SIGINT})
    assert install_count >= 4, sigs_seen

    assert signal.getsignal(signal.SIGTERM) == pre_term
    assert signal.getsignal(signal.SIGINT) == pre_int


@pytest.mark.skipif(
    not hasattr(os, "killpg") or sys.platform.startswith("win"),
    reason="requires POSIX killpg/setsid",
)
def test_run_subprocess_forwards_sigterm_to_child_process_group(
    tmp_path: Path,
) -> None:
    """Integration: SIGTERM to the wrapper cascades to the child via pgroup.

    Spawns a real Python wrapper that calls
    :func:`cloud_worker_cmd._run_subprocess` against a tiny throwaway
    Python child that registers a SIGTERM handler writing a marker
    file, then sends SIGTERM to the wrapper. Asserts the marker
    landed (proving the cascade reached the child via
    :func:`os.killpg`).  Without ``start_new_session=True`` + the
    forwarder, ``kill <wrapper-pid>`` left the child orphaned and
    the marker was never written.
    """
    marker = tmp_path / "child_caught.txt"
    ready = tmp_path / "child_ready.txt"
    child_script = tmp_path / "child.py"
    child_script.write_text(
        textwrap.dedent(
            f"""
            import signal, sys, time

            def handler(signum, frame):
                with open({str(marker)!r}, "w", encoding="utf-8") as f:
                    f.write(f"caught:{{signum}}")
                sys.exit(0)

            signal.signal(signal.SIGTERM, handler)
            with open({str(ready)!r}, "w", encoding="utf-8") as f:
                f.write("ready")
            for _ in range(600):
                time.sleep(0.05)
            sys.exit(99)
            """
        ),
        encoding="utf-8",
    )

    src_dir = Path(popolaloom.__file__).resolve().parent.parent
    wrapper_code = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(src_dir)!r})
        from popolaloom.cli.cloud_worker_cmd import _run_subprocess
        rc = _run_subprocess([{sys.executable!r}, {str(child_script)!r}])
        sys.exit(rc)
        """
    )

    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", wrapper_code],
        env={**os.environ},
    )
    try:
        # Wait until the child writes its readiness marker (handler installed)
        ready_deadline = time.monotonic() + 10.0
        while time.monotonic() < ready_deadline:
            if ready.exists():
                break
            if proc.poll() is not None:
                pytest.fail(
                    f"wrapper exited prematurely with rc={proc.returncode}"
                )
            time.sleep(0.05)
        else:
            pytest.fail("child never wrote its readiness marker")

        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    assert marker.exists(), (
        f"child marker file not written; wrapper exit={rc}"
    )
    body = marker.read_text(encoding="utf-8")
    assert body == f"caught:{int(signal.SIGTERM)}", body
    assert rc == 0, f"wrapper expected to exit 0, got {rc}"


# ---------------------------------------------------------------------------
# Part B — `popola cloud worker stop` Typer verb
# ---------------------------------------------------------------------------


def _make_fake_pid_alive_then_dead(
    *, alive_calls: int
) -> Any:
    """Return a ``_pid_alive`` stand-in that flips False after N calls."""
    state = {"calls": 0}

    def fake(pid: int) -> bool:
        state["calls"] += 1
        return state["calls"] <= alive_calls

    return fake


def test_worker_stop_by_name_signals_pgroup(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--name X`` finds the worker via the cmdline iterator + SIGTERMs its pgroup."""
    fake_pid = 12345
    fake_argv = [
        "agent",
        "worker",
        "start",
        "--worker-dir",
        str(isolated_home),
        "--name",
        "popolaloom-foo",
    ]
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_iter_proc_cmdlines",
        lambda *a, **kw: iter([(fake_pid, fake_argv)]),
    )
    monkeypatch.setattr(cloud_worker_cmd.os, "getpgid", lambda pid: pid + 1000)

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        cloud_worker_cmd.os,
        "killpg",
        lambda pgid, sig: sent.append((pgid, sig)),
    )
    monkeypatch.setattr(cloud_worker_cmd, "_pid_alive", lambda pid: False)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "worker", "stop", "--name", "popolaloom-foo"],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert sent == [(13345, signal.SIGTERM)], sent
    out = _combined_output(result)
    assert f"Stopped worker pid={fake_pid}" in out
    assert "SIGTERM" in out


def test_worker_stop_by_worker_dir_signals_pgroup(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--worker-dir Y`` finds the worker via the directory detector."""
    fake_pid = 22222
    fake_worker = cloud_worker_cmd.LocalWorkerProcess(
        pid=fake_pid,
        worker_dir=isolated_home.resolve(),
        name="popolaloom-bar",
        management_addr="127.0.0.1:39231",
        argv=("agent", "worker", "start", "--worker-dir", str(isolated_home)),
    )
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda worker_dir: [fake_worker],
    )
    monkeypatch.setattr(cloud_worker_cmd.os, "getpgid", lambda pid: pid + 7)

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        cloud_worker_cmd.os,
        "killpg",
        lambda pgid, sig: sent.append((pgid, sig)),
    )
    monkeypatch.setattr(cloud_worker_cmd, "_pid_alive", lambda pid: False)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "worker", "stop", "--worker-dir", str(isolated_home)],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert sent == [(fake_pid + 7, signal.SIGTERM)], sent
    out = _combined_output(result)
    assert f"Stopped worker pid={fake_pid}" in out


def test_worker_stop_grace_expiry_escalates_to_sigkill(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``--grace N`` waits N seconds for graceful exit, then SIGKILLs + WARN logs."""
    fake_pid = 33333
    fake_argv = [
        "agent",
        "worker",
        "start",
        "--worker-dir",
        str(isolated_home),
        "--name",
        "popolaloom-baz",
    ]
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_iter_proc_cmdlines",
        lambda *a, **kw: iter([(fake_pid, fake_argv)]),
    )
    monkeypatch.setattr(cloud_worker_cmd.os, "getpgid", lambda pid: pid + 1)

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        cloud_worker_cmd.os,
        "killpg",
        lambda pgid, sig: sent.append((pgid, sig)),
    )
    monkeypatch.setattr(cloud_worker_cmd, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cloud_worker_cmd.time, "sleep", lambda s: None)

    from popolaloom.cli.main import app as root_app

    with caplog.at_level(
        logging.WARNING, logger="popolaloom.cli.cloud_worker_cmd"
    ):
        result = runner.invoke(
            root_app,
            [
                "cloud",
                "worker",
                "stop",
                "--name",
                "popolaloom-baz",
                "--grace",
                "0.1",
            ],
        )
    assert result.exit_code == 0, _combined_output(result)
    assert (fake_pid + 1, signal.SIGTERM) in sent
    assert (fake_pid + 1, signal.SIGKILL) in sent
    assert any(
        "did not exit within" in record.getMessage()
        and record.levelno == logging.WARNING
        for record in caplog.records
    ), [r.getMessage() for r in caplog.records]
    out = _combined_output(result)
    assert "SIGKILL" in out
    assert f"pid={fake_pid}" in out


def test_worker_stop_no_match_exits_unreachable(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--name X`` with no matching cmdline → exit 1 + canonical stderr."""
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_iter_proc_cmdlines",
        lambda *a, **kw: iter([]),
    )

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "worker", "stop", "--name", "ghost-worker"],
    )
    assert result.exit_code == cloud_worker_cmd._EXIT_UNREACHABLE
    assert "no matching worker found" in _combined_output(result)


def test_worker_stop_neither_selector_exits_invalid_args(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither ``--name`` nor ``--worker-dir`` → exit 2 + canonical stderr."""
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "worker", "stop"],
    )
    assert result.exit_code == cloud_worker_cmd._EXIT_INVALID_ARGS
    assert "pass --name OR --worker-dir" in _combined_output(result)


_Q_V099_6_CAVEAT = (
    "Stops the worker even if a Cloud Agent session is currently claimed; "
    "compose with `popola cloud worker status --busy` to gate."
)


def test_worker_stop_docstring_contains_q_v099_6_caveat() -> None:
    """The docstring (source of ``--help`` long description) carries the caveat verbatim."""
    doc = cloud_worker_cmd.worker_stop_cmd.__doc__ or ""
    assert _Q_V099_6_CAVEAT in doc, doc


def test_worker_stop_help_text_surfaces_q_v099_6_caveat(
    runner: CliRunner,
    isolated_home: Path,
) -> None:
    """``popola cloud worker stop --help`` renders the Q-V099-6 caveat sentence.

    Help text gets word-wrapped by the Rich/Click renderer to the
    terminal width, so we normalise whitespace before substring
    matching.  The summary line + the caveat sentence MUST both
    appear after normalisation; this is the operator-facing
    contract locked by Q-V099-6.
    """
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "worker", "stop", "--help"],
        env={"COLUMNS": "240"},
    )
    assert result.exit_code == 0, _combined_output(result)
    rendered = " ".join(_combined_output(result).split())
    summary = (
        "Stop a running cloud worker (SIGTERM-then-SIGKILL after --grace seconds)."
    )
    assert " ".join(summary.split()) in rendered, rendered
    assert " ".join(_Q_V099_6_CAVEAT.split()) in rendered, rendered


def test_worker_stop_registered_in_worker_subapp(runner: CliRunner) -> None:
    """``popola cloud worker --help`` lists the new ``stop`` verb."""
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["cloud", "worker", "--help"])
    assert result.exit_code == 0, _combined_output(result)
    assert "stop" in _combined_output(result)


def test_worker_stop_pid_disappears_before_signal(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A race where the pid disappears between detection and signalling
    surfaces as ``_EXIT_UNREACHABLE`` (No Silent Failures).
    """
    fake_pid = 44444
    fake_argv = [
        "agent",
        "worker",
        "start",
        "--worker-dir",
        str(isolated_home),
        "--name",
        "popolaloom-race",
    ]
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_iter_proc_cmdlines",
        lambda *a, **kw: iter([(fake_pid, fake_argv)]),
    )

    def boom(pid: int) -> int:
        raise ProcessLookupError(pid)

    monkeypatch.setattr(cloud_worker_cmd.os, "getpgid", boom)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "worker", "stop", "--name", "popolaloom-race"],
    )
    assert result.exit_code == cloud_worker_cmd._EXIT_UNREACHABLE
    assert "disappeared" in _combined_output(result)


def test_worker_stop_eventual_exit_during_grace(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker exits gracefully mid-poll → SIGKILL is NOT issued."""
    fake_pid = 55555
    fake_argv = [
        "agent",
        "worker",
        "start",
        "--worker-dir",
        str(isolated_home),
        "--name",
        "popolaloom-graceful",
    ]
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_iter_proc_cmdlines",
        lambda *a, **kw: iter([(fake_pid, fake_argv)]),
    )
    monkeypatch.setattr(cloud_worker_cmd.os, "getpgid", lambda pid: pid + 2)

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        cloud_worker_cmd.os,
        "killpg",
        lambda pgid, sig: sent.append((pgid, sig)),
    )
    monkeypatch.setattr(
        cloud_worker_cmd, "_pid_alive", _make_fake_pid_alive_then_dead(alive_calls=2)
    )
    monkeypatch.setattr(cloud_worker_cmd.time, "sleep", lambda s: None)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "stop",
            "--name",
            "popolaloom-graceful",
            "--grace",
            "5.0",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    sigs_only = [sig for _pgid, sig in sent]
    assert signal.SIGTERM in sigs_only
    assert signal.SIGKILL not in sigs_only, sent
    out = _combined_output(result)
    assert "Stopped worker" in out
    assert "SIGTERM" in out
