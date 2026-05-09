"""``popola cloud worker`` v0.9.1 self-hosted worker CLI tests.

Hermetic — every test monkeypatches the three indirection points in
:mod:`popolaloom.cli.cloud_worker_cmd` (``_resolve_agent_binary``,
``_run_subprocess``, ``_fetch_management_endpoint``) so no real
subprocess is spawned and no real network IO occurs.

Coverage summary (mirrors v0.9.1 plan §"Coverage targets"):

- argv construction for My Machines (no ``--pool``) and Self-Hosted
  Pool (``--pool``) modes.
- ``--pool`` without ``CURSOR_API_KEY`` exits ``77`` with an explicit
  service-account-API-key hint (No Silent Failures).
- ``--dry-run`` prints the argv and does not invoke the subprocess
  helper.
- ``status`` parses ``/healthz`` / ``/readyz`` / ``/metrics`` and
  surfaces values in both Rich and JSON modes.
- ``handoff`` emits both Markdown and JSON envelopes, requires either
  ``--worker-id`` or ``--worker-url``, and notes that no popola task id
  is created.
- Helper unit coverage for ``_validate_management_addr`` /
  ``_validate_label`` / ``_parse_worker_metrics`` /
  ``_format_quoted_argv`` so each pure helper has its own failure
  enumeration.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from popolaloom.cli import cloud_worker_cmd

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    """Default :class:`CliRunner` (Typer ≥ 0.9 drops ``mix_stderr``)."""
    return CliRunner()


@pytest.fixture
def isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Hermetic ``$POPOLA_HOME`` + ``$HOME`` so worker paths can't bleed."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    yield tmp_path


@pytest.fixture(autouse=True)
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``cloud_worker_cmd._console_out`` wide so substring asserts hold."""
    monkeypatch.setattr(
        cloud_worker_cmd, "_console_out", Console(width=200, height=50)
    )


@pytest.fixture
def fake_agent_binary(monkeypatch: pytest.MonkeyPatch) -> str:
    """Pretend ``agent`` resolves to a stable absolute path."""
    fake_path = "/usr/local/bin/agent-test"
    monkeypatch.setattr(
        cloud_worker_cmd, "_resolve_agent_binary", lambda: fake_path
    )
    return fake_path


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
# Helper unit tests (pure)
# ---------------------------------------------------------------------------


def test_validate_management_addr_accepts_host_port() -> None:
    """``host:port`` parses to the host + port pair."""
    assert cloud_worker_cmd._validate_management_addr("127.0.0.1:8080") == (
        "127.0.0.1",
        8080,
    )


def test_validate_management_addr_accepts_bare_port() -> None:
    """``:port`` defaults host to loopback (matches upstream CLI semantics)."""
    assert cloud_worker_cmd._validate_management_addr(":39231") == (
        "127.0.0.1",
        39231,
    )


def test_validate_management_addr_rejects_empty() -> None:
    """An empty addr surfaces ``typer.Exit(2)`` (No Silent Failures)."""
    with pytest.raises(typer.Exit) as excinfo:
        cloud_worker_cmd._validate_management_addr("")
    assert excinfo.value.exit_code == cloud_worker_cmd._EXIT_INVALID_ARGS


def test_validate_management_addr_rejects_non_numeric_port() -> None:
    """A non-integer port surfaces ``Exit(2)`` (catches typos like ``:abc``)."""
    with pytest.raises(typer.Exit) as excinfo:
        cloud_worker_cmd._validate_management_addr(":abc")
    assert excinfo.value.exit_code == cloud_worker_cmd._EXIT_INVALID_ARGS


def test_validate_management_addr_rejects_out_of_range_port() -> None:
    """Port ``0`` / ``65536`` are rejected (matches IANA range)."""
    with pytest.raises(typer.Exit):
        cloud_worker_cmd._validate_management_addr(":0")
    with pytest.raises(typer.Exit):
        cloud_worker_cmd._validate_management_addr(":65536")


def test_validate_management_addr_rejects_no_colon() -> None:
    """A bare hostname without ``:`` surfaces ``Exit(2)``."""
    with pytest.raises(typer.Exit):
        cloud_worker_cmd._validate_management_addr("localhost")


def test_validate_label_splits_key_value() -> None:
    """``k=v`` parses to a tuple."""
    assert cloud_worker_cmd._validate_label("env=production") == (
        "env",
        "production",
    )


def test_validate_label_strips_whitespace() -> None:
    """Trailing whitespace is stripped from key + value."""
    assert cloud_worker_cmd._validate_label("  env  =  production  ") == (
        "env",
        "production",
    )


def test_validate_label_rejects_missing_equals() -> None:
    """A label without ``=`` surfaces ``Exit(2)``."""
    with pytest.raises(typer.Exit):
        cloud_worker_cmd._validate_label("backend")


def test_validate_label_rejects_empty_key() -> None:
    """A label with empty key (``=value``) surfaces ``Exit(2)``."""
    with pytest.raises(typer.Exit):
        cloud_worker_cmd._validate_label("=value")


def test_validate_label_rejects_empty_value() -> None:
    """A label with empty value (``key=``) surfaces ``Exit(2)``."""
    with pytest.raises(typer.Exit):
        cloud_worker_cmd._validate_label("key=")


def test_parse_worker_metrics_extracts_relevant_gauges() -> None:
    """Only ``cursor_self_hosted_worker_*`` lines are surfaced."""
    text = (
        "# HELP cursor_self_hosted_worker_connected ...\n"
        "# TYPE cursor_self_hosted_worker_connected gauge\n"
        "cursor_self_hosted_worker_connected 1\n"
        "cursor_self_hosted_worker_session_active 0\n"
        "cursor_self_hosted_worker_session_ends_total{reason=\"stream_end\"} 0\n"
        "go_gc_duration_seconds_sum 1.23\n"
    )
    parsed = cloud_worker_cmd._parse_worker_metrics(text)
    assert parsed["cursor_self_hosted_worker_connected"] == 1.0
    assert parsed["cursor_self_hosted_worker_session_active"] == 0.0
    # The labelled counter still parses (we strip the {labels} block).
    assert "cursor_self_hosted_worker_session_ends_total" in parsed
    # Unrelated metrics are ignored (forward compat with newer worker builds).
    assert "go_gc_duration_seconds_sum" not in parsed


def test_parse_worker_metrics_skips_malformed_value() -> None:
    """A non-float value is silently dropped (Prometheus-style robust)."""
    text = "cursor_self_hosted_worker_connected not_a_number\n"
    assert cloud_worker_cmd._parse_worker_metrics(text) == {}


def test_format_quoted_argv_quotes_spaces() -> None:
    """Spaces in argv tokens are quoted via :func:`shlex.quote`."""
    rendered = cloud_worker_cmd._format_quoted_argv(
        ["agent", "worker", "start", "--name", "popolaloom devpath"]
    )
    assert "'popolaloom devpath'" in rendered


def test_extract_worker_id_from_url_fragment_form() -> None:
    """The fragment form ``#workerId=<uuid>`` parses cleanly."""
    url = "https://cursor.com/agents#workerId=deadbeef-1234"
    assert cloud_worker_cmd._extract_worker_id_from_url(url) == "deadbeef-1234"


def test_extract_worker_id_from_url_query_form() -> None:
    """The query form ``?workerId=<uuid>`` parses cleanly."""
    url = "https://cursor.com/agents?workerId=cafe-1234"
    assert cloud_worker_cmd._extract_worker_id_from_url(url) == "cafe-1234"


def test_extract_worker_id_from_url_with_other_params() -> None:
    """Other ``&key=value`` params are stripped from the worker id."""
    url = "https://cursor.com/agents?foo=bar&workerId=abcd&baz=qux"
    assert cloud_worker_cmd._extract_worker_id_from_url(url) == "abcd"


def test_extract_worker_id_from_url_returns_none_when_absent() -> None:
    """A URL without a worker id marker returns ``None``."""
    assert (
        cloud_worker_cmd._extract_worker_id_from_url("https://cursor.com/agents")
        is None
    )


def test_format_unix_timestamp_renders_iso() -> None:
    """A Unix epoch float renders as an ISO-8601 UTC string."""
    rendered = cloud_worker_cmd._format_unix_timestamp(1778335163.0)
    assert rendered.startswith("2026-")
    assert rendered.endswith("+00:00")


def test_format_unix_timestamp_zero_renders_never() -> None:
    """A ``0`` timestamp (no heartbeat yet) renders as ``never``."""
    assert cloud_worker_cmd._format_unix_timestamp(0) == "never"


def test_format_unix_timestamp_none_renders_dash() -> None:
    """A missing metric (``None``) renders as ``-``."""
    assert cloud_worker_cmd._format_unix_timestamp(None) == "-"


def test_format_unix_timestamp_unparseable_renders_dash() -> None:
    """A non-numeric value falls back to ``-`` (No Silent Failures)."""
    assert cloud_worker_cmd._format_unix_timestamp("nope") == "-"


def test_build_start_argv_my_machines_default(tmp_path: Path) -> None:
    """My Machines mode: no ``--pool`` flag, no ``--pool-name``."""
    argv = cloud_worker_cmd._build_start_argv(
        binary="/bin/agent",
        worker_dir=tmp_path,
        name="dev-1",
        pool=False,
        pool_name=None,
        idle_release_timeout=None,
        labels=[],
        management_addr=None,
    )
    assert "--pool" not in argv
    assert "--pool-name" not in argv
    assert "--name" in argv and "dev-1" in argv
    assert "--worker-dir" in argv and str(tmp_path) in argv


def test_build_start_argv_pool_mode(tmp_path: Path) -> None:
    """Pool mode: ``--pool`` + optional ``--pool-name`` propagate."""
    argv = cloud_worker_cmd._build_start_argv(
        binary="/bin/agent",
        worker_dir=tmp_path,
        name=None,
        pool=True,
        pool_name="popolaloom",
        idle_release_timeout=600,
        labels=[("env", "prod"), ("hitl", "enabled")],
        management_addr=":8080",
    )
    assert "--pool" in argv
    pool_idx = argv.index("--pool-name")
    assert argv[pool_idx + 1] == "popolaloom"
    idle_idx = argv.index("--idle-release-timeout")
    assert argv[idle_idx + 1] == "600"
    addr_idx = argv.index("--management-addr")
    assert argv[addr_idx + 1] == ":8080"
    # Labels are emitted as repeatable ``--label key=value`` pairs.
    assert argv.count("--label") == 2
    assert "env=prod" in argv
    assert "hitl=enabled" in argv


def test_build_debug_argv_minimal(tmp_path: Path) -> None:
    """Debug argv has the ``debug`` subcommand + worker dir at minimum."""
    argv = cloud_worker_cmd._build_debug_argv(
        binary="/bin/agent",
        worker_dir=tmp_path,
        name=None,
        pool=False,
        pool_name=None,
        labels=[],
    )
    assert argv[:3] == ["/bin/agent", "worker", "debug"]
    assert "--worker-dir" in argv


# ---------------------------------------------------------------------------
# `popola cloud worker debug` — CLI wiring
# ---------------------------------------------------------------------------


def test_worker_debug_invokes_agent_subprocess(
    runner: CliRunner,
    isolated_home: Path,
    fake_agent_binary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola cloud worker debug`` shells out to ``agent worker debug``."""
    captured: list[list[str]] = []

    def fake_run(argv: list[str]) -> int:
        captured.append(argv)
        return 0

    monkeypatch.setattr(cloud_worker_cmd, "_run_subprocess", fake_run)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "worker", "debug", "--worker-dir", str(isolated_home)],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert len(captured) == 1
    argv = captured[0]
    assert argv[0] == fake_agent_binary
    assert argv[1:3] == ["worker", "debug"]
    assert "--worker-dir" in argv
    assert str(isolated_home) in argv


def test_worker_debug_pool_without_api_key_exits_77(
    runner: CliRunner,
    isolated_home: Path,
    fake_agent_binary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--pool`` without ``CURSOR_API_KEY`` fails with the canonical hint."""
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setattr(
        cloud_worker_cmd, "_run_subprocess", lambda argv: 0
    )
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "debug",
            "--worker-dir",
            str(isolated_home),
            "--pool",
        ],
    )
    assert result.exit_code == cloud_worker_cmd._EXIT_POOL_REQUIRES_API_KEY
    out = _combined_output(result)
    assert "service-account API key" in out
    assert "CURSOR_API_KEY" in out


def test_worker_debug_pool_with_api_key_runs(
    runner: CliRunner,
    isolated_home: Path,
    fake_agent_binary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--pool`` with ``CURSOR_API_KEY`` exported reaches subprocess."""
    monkeypatch.setenv("CURSOR_API_KEY", "test-service-account-key")
    captured: list[list[str]] = []
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_run_subprocess",
        lambda argv: captured.append(argv) or 0,
    )
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "debug",
            "--worker-dir",
            str(isolated_home),
            "--pool",
            "--pool-name",
            "default",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert len(captured) == 1
    assert "--pool" in captured[0]


# ---------------------------------------------------------------------------
# `popola cloud worker start` — CLI wiring
# ---------------------------------------------------------------------------


def test_worker_start_dry_run_does_not_spawn(
    runner: CliRunner,
    isolated_home: Path,
    fake_agent_binary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` prints the argv and never invokes the subprocess hook."""
    spawned: list[Any] = []
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_run_subprocess",
        lambda argv: spawned.append(argv) or 0,
    )
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "start",
            "--worker-dir",
            str(isolated_home),
            "--name",
            "dryrun-test",
            "--management-addr",
            "127.0.0.1:39231",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert spawned == []
    out = _combined_output(result)
    assert "dry run" in out.lower()
    assert "worker start" in out
    assert "--name" in out
    assert "dryrun-test" in out


def test_worker_start_pool_without_api_key_exits_77(
    runner: CliRunner,
    isolated_home: Path,
    fake_agent_binary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``start --pool`` without ``CURSOR_API_KEY`` fails before spawning."""
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    spawned: list[Any] = []
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_run_subprocess",
        lambda argv: spawned.append(argv) or 0,
    )
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "start",
            "--worker-dir",
            str(isolated_home),
            "--pool",
        ],
    )
    assert result.exit_code == cloud_worker_cmd._EXIT_POOL_REQUIRES_API_KEY
    assert spawned == []
    out = _combined_output(result)
    assert "service-account API key" in out


def test_worker_start_invalid_management_addr_exits_2(
    runner: CliRunner,
    isolated_home: Path,
    fake_agent_binary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad ``--management-addr`` is rejected before subprocess spawn."""
    spawned: list[Any] = []
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_run_subprocess",
        lambda argv: spawned.append(argv) or 0,
    )
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "start",
            "--worker-dir",
            str(isolated_home),
            "--management-addr",
            "not-a-port",
        ],
    )
    assert result.exit_code == cloud_worker_cmd._EXIT_INVALID_ARGS
    assert spawned == []


def test_worker_start_my_machines_runs_subprocess(
    runner: CliRunner,
    isolated_home: Path,
    fake_agent_binary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """My Machines mode reaches the subprocess hook with no ``--pool``."""
    captured: list[list[str]] = []
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_run_subprocess",
        lambda argv: captured.append(argv) or 0,
    )
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "start",
            "--worker-dir",
            str(isolated_home),
            "--name",
            "my-machines-1",
            "--label",
            "env=dev",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert len(captured) == 1
    argv = captured[0]
    assert "--pool" not in argv
    assert "env=dev" in argv


# ---------------------------------------------------------------------------
# `popola cloud worker status` — CLI wiring
# ---------------------------------------------------------------------------


def _fake_management_endpoint_factory(
    responses: dict[str, tuple[int, str]],
) -> Any:
    """Build a fake ``_fetch_management_endpoint`` from a ``{path: (status, body)}`` map."""

    def fake_fetch(
        host: str, port: int, path: str, *, timeout_s: float = 3.0
    ) -> tuple[int, str]:
        normalized = path.lstrip("/")
        if normalized not in responses:
            raise httpx.ConnectError(f"path {normalized!r} not stubbed")
        return responses[normalized]

    return fake_fetch


def test_worker_status_renders_rich_table(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three endpoints feed the Rich table renderer."""
    fake = _fake_management_endpoint_factory(
        {
            "healthz": (
                200,
                json.dumps({"status": "ok", "timestamp": "2026-05-09T13:30:00Z"}),
            ),
            "readyz": (
                200,
                json.dumps(
                    {
                        "status": "ok",
                        "connected": True,
                        "claimed": False,
                        "timestamp": "2026-05-09T13:30:00Z",
                    }
                ),
            ),
            "metrics": (
                200,
                "cursor_self_hosted_worker_connected 1\n"
                "cursor_self_hosted_worker_session_active 0\n"
                "cursor_self_hosted_worker_connect_attempts_total 1\n"
                "cursor_self_hosted_worker_last_activity_unix_seconds 1778335163\n",
            ),
        }
    )
    monkeypatch.setattr(cloud_worker_cmd, "_fetch_management_endpoint", fake)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "status",
            "--management-addr",
            "127.0.0.1:39231",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "127.0.0.1:39231" in out
    assert "healthz.status" in out
    assert "readyz.connected" in out
    assert "metrics.connected" in out
    # v0.9.1 iteration: ``last_activity`` row is added so a stale
    # heartbeat is visible in the human-facing table.
    assert "metrics.last_activity" in out


def test_worker_status_json_mode_emits_dict(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json`` emits a parseable JSON payload with the canonical shape."""
    fake = _fake_management_endpoint_factory(
        {
            "healthz": (200, json.dumps({"status": "ok"})),
            "readyz": (
                200,
                json.dumps({"status": "ok", "connected": True, "claimed": False}),
            ),
            "metrics": (
                200,
                "cursor_self_hosted_worker_connected 1\n",
            ),
        }
    )
    monkeypatch.setattr(cloud_worker_cmd, "_fetch_management_endpoint", fake)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "status",
            "--management-addr",
            ":39231",
            "--json",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    payload = json.loads(_combined_output(result))
    assert payload["healthz"]["status"] == "ok"
    assert payload["readyz"]["connected"] is True
    assert (
        payload["metrics"]["values"]["cursor_self_hosted_worker_connected"]
        == 1
    )


def test_worker_status_unreachable_exits_1(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection failure exits ``1`` with a hint that names the bind addr."""

    def boom(*_a: Any, **_kw: Any) -> tuple[int, str]:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(cloud_worker_cmd, "_fetch_management_endpoint", boom)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "status",
            "--management-addr",
            "127.0.0.1:39999",
        ],
    )
    assert result.exit_code == cloud_worker_cmd._EXIT_UNREACHABLE
    out = _combined_output(result)
    assert "unreachable" in out.lower()
    assert "--management-addr" in out


def test_worker_status_unreachable_default_addr_hints_about_default(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the default addr is used, the hint calls out it's the default.

    v0.9.1 iteration: a worker started without ``--management-addr``
    has no management server bound, so the operator hitting the default
    port would otherwise see a generic "did you start the worker with
    ``--management-addr 127.0.0.1:39231``" message; the iteration adds
    a default-aware branch that explains the default origin and the
    opt-in nature of the management server.
    """

    def boom(*_a: Any, **_kw: Any) -> tuple[int, str]:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(cloud_worker_cmd, "_fetch_management_endpoint", boom)
    from popolaloom.cli.main import app as root_app

    # Note: no --management-addr passed → uses _DEFAULT_MANAGEMENT_ADDR.
    result = runner.invoke(root_app, ["cloud", "worker", "status"])
    assert result.exit_code == cloud_worker_cmd._EXIT_UNREACHABLE
    out = _combined_output(result)
    assert "defaults to" in out
    assert "opt-in" in out


def test_worker_status_invalid_timeout_exits_2(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-positive ``--timeout`` is rejected (No Silent Failures)."""
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_fetch_management_endpoint",
        lambda *a, **kw: (200, "{}"),
    )
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "status",
            "--management-addr",
            ":39231",
            "--timeout",
            "0",
        ],
    )
    assert result.exit_code == cloud_worker_cmd._EXIT_INVALID_ARGS


# ---------------------------------------------------------------------------
# `popola cloud worker handoff` — CLI wiring
# ---------------------------------------------------------------------------


def test_worker_handoff_markdown_with_worker_id(
    runner: CliRunner,
    isolated_home: Path,
) -> None:
    """``--worker-id`` builds the canonical Cloud Agents URL."""
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "handoff",
            "--worker-id",
            "deadbeef-1234",
            "--prompt",
            "review the README",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "https://cursor.com/agents#workerId=deadbeef-1234" in out
    assert "review the README" in out
    assert "popola_task_id" in out
    assert "did NOT create" in out


def test_worker_handoff_json_mode_emits_envelope(
    runner: CliRunner,
    isolated_home: Path,
    tmp_path: Path,
) -> None:
    """``--json`` emits a structured envelope with the contract fields."""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("rewrite the docs\n", encoding="utf-8")
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "handoff",
            "--worker-url",
            "https://cursor.com/agents#workerId=abcd",
            "--prompt-file",
            str(prompt_file),
            "--json",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    payload = json.loads(_combined_output(result))
    assert payload["kind"] == "popola.cloud.worker.handoff"
    assert payload["popola_task_id"] is None
    assert payload["worker_url"].endswith("workerId=abcd")
    assert payload["prompt"] == "rewrite the docs"
    # v0.9.1 iteration: ``worker_id`` is surfaced separately from the
    # URL so automating callers don't have to re-parse the fragment.
    assert payload["worker_id"] == "abcd"


def test_worker_handoff_json_with_id_includes_worker_id(
    runner: CliRunner, isolated_home: Path
) -> None:
    """``--worker-id`` is mirrored verbatim into the JSON envelope."""
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "handoff",
            "--worker-id",
            "deadbeef-1234",
            "--prompt",
            "hi",
            "--json",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    payload = json.loads(_combined_output(result))
    assert payload["worker_id"] == "deadbeef-1234"
    assert payload["worker_url"].endswith("workerId=deadbeef-1234")


def test_worker_handoff_url_without_marker_yields_null_worker_id(
    runner: CliRunner, isolated_home: Path
) -> None:
    """A URL without ``#workerId=`` surfaces ``worker_id: null``."""
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "handoff",
            "--worker-url",
            "https://cursor.com/agents",
            "--prompt",
            "hi",
            "--json",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    payload = json.loads(_combined_output(result))
    assert payload["worker_id"] is None


def test_worker_handoff_requires_url_or_id(
    runner: CliRunner, isolated_home: Path
) -> None:
    """Missing ``--worker-id`` AND ``--worker-url`` fails with a clear hint."""
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "handoff",
            "--prompt",
            "hello",
        ],
    )
    assert result.exit_code == cloud_worker_cmd._EXIT_INVALID_ARGS
    out = _combined_output(result)
    assert "--worker-id" in out
    assert "--worker-url" in out


def test_worker_handoff_rejects_both_url_and_id(
    runner: CliRunner, isolated_home: Path
) -> None:
    """Passing both URL forms together is rejected."""
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "handoff",
            "--worker-id",
            "abcd",
            "--worker-url",
            "https://cursor.com/agents#workerId=abcd",
            "--prompt",
            "hi",
        ],
    )
    assert result.exit_code == cloud_worker_cmd._EXIT_INVALID_ARGS


def test_worker_handoff_rejects_invalid_url_scheme(
    runner: CliRunner, isolated_home: Path
) -> None:
    """``--worker-url`` must start with ``http(s)://``."""
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "handoff",
            "--worker-url",
            "ftp://oops.example/",
            "--prompt",
            "hi",
        ],
    )
    assert result.exit_code == cloud_worker_cmd._EXIT_INVALID_ARGS


def test_worker_handoff_rejects_empty_prompt(
    runner: CliRunner, isolated_home: Path, tmp_path: Path
) -> None:
    """An empty prompt file is rejected (No Silent Failures)."""
    empty = tmp_path / "empty.txt"
    empty.write_text("   \n\n", encoding="utf-8")
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "worker",
            "handoff",
            "--worker-id",
            "abcd",
            "--prompt-file",
            str(empty),
        ],
    )
    assert result.exit_code == cloud_worker_cmd._EXIT_INVALID_ARGS


# ---------------------------------------------------------------------------
# Subapp registration regression
# ---------------------------------------------------------------------------


def test_worker_subapp_registered_under_cloud(runner: CliRunner) -> None:
    """``popola cloud --help`` lists the new ``worker`` group."""
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["cloud", "--help"])
    assert result.exit_code == 0, _combined_output(result)
    assert "worker" in _combined_output(result)


def test_worker_help_text_lists_four_verbs(runner: CliRunner) -> None:
    """``popola cloud worker --help`` exposes debug / start / status / handoff."""
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["cloud", "worker", "--help"])
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    for verb in ("debug", "start", "status", "handoff"):
        assert verb in out, f"missing `{verb}` verb in:\n{out}"
