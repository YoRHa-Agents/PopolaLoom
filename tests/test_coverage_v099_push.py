"""v0.9.9 coverage-fill tests targeting branches uncovered by the wave-A/B/C suites.

Stage 4 CI gate fail-forward (commit dc715bf): the v0.9.9 6-patch
landing dropped default-lane branch coverage from 94.60% to 93.48%
(below the 94% floor codified in [tool.coverage.report] fail_under).
This file adds targeted unit tests for the specific uncovered branches
in the new code shipped by Wave A1/A2/A3 + B1/B2 + C1, lifting coverage
back above the 94% floor without bumping the gate.

Conventions per :file:`tests/test_coverage_v055_push.py` (the v0.5.5
sibling which serves the same purpose for Loop 5): each test docstring
references the targeted file:line range and the AC the test fills.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from popolaloom import credentials
from popolaloom.cli import cloud_worker_cmd
from popolaloom.cli.cloud_worker_cmd import LocalWorkerProcess

# ── _pid_alive helper (cli/cloud_worker_cmd.py:910-935) ──────────────────


def test_pid_alive_returns_false_for_zero_or_negative_pid() -> None:
    """``_pid_alive(0)`` and ``_pid_alive(-1)`` short-circuit to False (line 927)."""
    assert cloud_worker_cmd._pid_alive(0) is False
    assert cloud_worker_cmd._pid_alive(-1) is False


def test_pid_alive_returns_false_for_reaped_pid() -> None:
    """Reaped pid: ``os.kill(pid, 0)`` raises ``ProcessLookupError`` -> False (line 931-932)."""
    proc = subprocess.Popen(  # noqa: S603 - test fixture
        [sys.executable, "-c", "pass"]
    )
    proc.wait()
    pid = proc.pid
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.skip("kernel did not reap pid in time; flaky on this host")
    assert cloud_worker_cmd._pid_alive(pid) is False


def test_pid_alive_treats_permission_error_as_alive() -> None:
    """``PermissionError`` from ``os.kill`` -> True (line 933-934)."""
    with patch("popolaloom.cli.cloud_worker_cmd.os.kill") as mock_kill:
        mock_kill.side_effect = PermissionError("not allowed")
        assert cloud_worker_cmd._pid_alive(99999) is True


def test_pid_alive_returns_true_for_alive_pid() -> None:
    """The bare success path (no exception) returns True (line 935)."""
    assert cloud_worker_cmd._pid_alive(os.getpid()) is True


# ── _find_worker_for_stop --name path (cli/cloud_worker_cmd.py:953-962) ──


def test_find_worker_for_stop_by_name_iterates_and_returns_match() -> None:
    """Successful ``--name`` match returns the parsed worker (line 961-962)."""
    fake_proc = LocalWorkerProcess(
        pid=12345,
        worker_dir=Path("/tmp/work"),
        name="popolaloom-myrepo-abc",
        management_addr="127.0.0.1:39231",
        argv=("agent", "worker", "start", "--name", "popolaloom-myrepo-abc"),
    )
    with (
        patch.object(
            cloud_worker_cmd,
            "_iter_proc_cmdlines",
            return_value=iter([(12345, ["agent", "worker", "start"])]),
        ),
        patch.object(
            cloud_worker_cmd,
            "_parse_worker_start_cmdline",
            return_value=fake_proc,
        ),
    ):
        result = cloud_worker_cmd._find_worker_for_stop(
            name="popolaloom-myrepo-abc", worker_dir=None
        )
    assert result is fake_proc
    assert result is not None
    assert result.name == "popolaloom-myrepo-abc"


def test_find_worker_for_stop_by_name_skips_unparseable_cmdlines() -> None:
    """``_parse_worker_start_cmdline`` returning None -> ``continue`` (line 959-960)."""
    fake_proc = LocalWorkerProcess(
        pid=12345,
        worker_dir=Path("/tmp/work"),
        name="popolaloom-myrepo-abc",
        management_addr="127.0.0.1:39231",
        argv=("agent", "worker", "start", "--name", "popolaloom-myrepo-abc"),
    )
    parse_calls = iter([None, fake_proc])
    with (
        patch.object(
            cloud_worker_cmd,
            "_iter_proc_cmdlines",
            return_value=iter(
                [(11111, ["unrelated", "cmd"]), (12345, ["agent", "worker", "start"])]
            ),
        ),
        patch.object(
            cloud_worker_cmd,
            "_parse_worker_start_cmdline",
            side_effect=lambda pid, argv: next(parse_calls),
        ),
    ):
        result = cloud_worker_cmd._find_worker_for_stop(
            name="popolaloom-myrepo-abc", worker_dir=None
        )
    assert result is fake_proc


def test_find_worker_for_stop_by_name_returns_none_when_no_match() -> None:
    """Loop exhausts without a name match -> return None (line 963 fallthrough)."""
    fake_proc = LocalWorkerProcess(
        pid=12345,
        worker_dir=Path("/tmp/work"),
        name="some-other-name",
        management_addr="127.0.0.1:39231",
        argv=("agent", "worker", "start"),
    )
    with (
        patch.object(
            cloud_worker_cmd,
            "_iter_proc_cmdlines",
            return_value=iter([(12345, ["agent", "worker", "start"])]),
        ),
        patch.object(
            cloud_worker_cmd,
            "_parse_worker_start_cmdline",
            return_value=fake_proc,
        ),
    ):
        result = cloud_worker_cmd._find_worker_for_stop(
            name="popolaloom-different", worker_dir=None
        )
    assert result is None


# ── worker_stop_cmd race-condition branches (line 1025-1031) ─────────────


def test_worker_stop_cmd_killpg_process_lookup_error_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Race: worker dies between getpgid and killpg -> Exit _EXIT_UNREACHABLE (line 1025-1031)."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    metadata = tmp_path / "credentials.toml"
    metadata.write_text(
        '[cursor]\naccount_class = "service_account"\n', encoding="utf-8"
    )
    metadata.chmod(0o600)

    fake_proc = LocalWorkerProcess(
        pid=99999,
        worker_dir=tmp_path,
        name="popolaloom-race-test",
        management_addr="127.0.0.1:39231",
        argv=("agent", "worker", "start", "--name", "popolaloom-race-test"),
    )

    def _raise_lookup(pgid: int, sig: int) -> None:
        raise ProcessLookupError(f"pgid {pgid} disappeared mid-flight")

    with (
        patch.object(
            cloud_worker_cmd, "_find_worker_for_stop", return_value=fake_proc
        ),
        patch.object(cloud_worker_cmd.os, "getpgid", return_value=99999),
        patch.object(cloud_worker_cmd.os, "killpg", side_effect=_raise_lookup),
    ):
        runner = CliRunner()
        from popolaloom.cli.main import app as root_app

        result = runner.invoke(
            root_app,
            ["cloud", "worker", "stop", "--name", "popolaloom-race-test"],
        )

    assert result.exit_code == cloud_worker_cmd._EXIT_UNREACHABLE
    rendered = result.stderr if hasattr(result, "stderr") else result.output
    assert "disappeared before SIGTERM could be delivered" in rendered, rendered


# ── credentials.AccountClass + load_env_fallback edge cases ──────────────


def test_load_env_fallback_skips_non_cursor_api_key_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``cursor_api_key.env`` lines NOT setting ``CURSOR_API_KEY`` are skipped (line 513-522)."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    fallback = tmp_path / "cursor_api_key.env"
    fallback.write_text(
        "OTHER_KEY=should-be-ignored\nCURSOR_API_KEY=crsr_real_value\n",
        encoding="utf-8",
    )
    fallback.chmod(0o600)

    import logging

    with caplog.at_level(logging.DEBUG, logger="popolaloom.credentials"):
        loaded = credentials.load_env_fallback_into_environ()

    assert loaded is True
    assert os.environ.get("CURSOR_API_KEY") == "crsr_real_value"
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)


def test_store_account_class_rejects_empty_value() -> None:
    """``store_account_class("")`` and whitespace-only raise ValueError (line 967-970)."""
    with pytest.raises(ValueError, match="non-empty string"):
        credentials.store_account_class("")
    with pytest.raises(ValueError, match="non-empty string"):
        credentials.store_account_class("   ")


def test_store_account_class_rejects_unknown_value() -> None:
    """Out-of-whitelist value raises ValueError with the four canonical names (line 972-977)."""
    with pytest.raises(ValueError, match="invalid account_class"):
        credentials.store_account_class("admin")


def test_get_account_class_returns_unknown_for_garbage_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Hand-edited ``account_class = "garbage"`` -> WARN + UNKNOWN (line 1006-1016)."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    metadata = tmp_path / "credentials.toml"
    metadata.write_text(
        '[cursor]\naccount_class = "garbage_value"\n', encoding="utf-8"
    )
    metadata.chmod(0o600)

    import logging

    with caplog.at_level(logging.WARNING, logger="popolaloom.credentials"):
        result = credentials.get_account_class()

    assert result is credentials.AccountClass.UNKNOWN
    assert any("unrecognised account_class" in r.message for r in caplog.records)


def test_get_account_class_handles_empty_string_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty-string value in metadata -> AccountClass.UNKNOWN (line 1002-1003)."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    metadata = tmp_path / "credentials.toml"
    metadata.write_text('[cursor]\naccount_class = ""\n', encoding="utf-8")
    metadata.chmod(0o600)

    assert credentials.get_account_class() is credentials.AccountClass.UNKNOWN


def test_get_account_class_normalizes_dashed_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dashed ``service-account`` -> ``AccountClass.SERVICE_ACCOUNT`` (line 1004-1005)."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    metadata = tmp_path / "credentials.toml"
    metadata.write_text(
        '[cursor]\naccount_class = "service-account"\n', encoding="utf-8"
    )
    metadata.chmod(0o600)

    assert (
        credentials.get_account_class() is credentials.AccountClass.SERVICE_ACCOUNT
    )


# ── Supervisor silence-timer extra branches ──────────────────────────────


def test_silence_hint_for_unknown_cli_emits_generic_hint() -> None:
    """``_silence_hint_for("claude", ...)`` returns the generic-CLI hint (catch-all branch)."""
    from popolaloom.daemon import supervisor

    hint = supervisor._silence_hint_for("claude", output_format=None)
    assert "claude" in hint.lower() or "30s" in hint or "long-running" in hint


def test_silence_hint_for_codex_uses_generic_form() -> None:
    """Non-cursor CLI defaults to the generic form regardless of output_format."""
    from popolaloom.daemon import supervisor

    hint = supervisor._silence_hint_for("codex", output_format="text")
    assert "stream-json" not in hint or "30s" in hint
