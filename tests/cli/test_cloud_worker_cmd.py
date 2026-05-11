"""``popola cloud worker`` v0.9.1 self-hosted worker CLI tests.

Hermetic — every test monkeypatches the three indirection points in
:mod:`popolaloom.cli.cloud_worker_cmd` (``_resolve_agent_binary``,
``_run_subprocess``, ``_fetch_management_endpoint`` and process detection)
so no real
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
- ``dispatch`` POSTs a ``cursor-cloud`` task to ``popolad`` by default,
  with ``--print-only`` / ``--dry-run`` preserving preview-only output.
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
    """Hermetic ``$POPOLA_HOME`` + ``$HOME`` so worker paths can't bleed.

    v0.9.9: pre-seeds ``credentials.toml`` with
    ``account_class = "service_account"`` so the worker-dispatch pre-flight
    gate (B2 / Q-V099-1 / Q-V099-8) does not refuse legacy fixtures that
    intentionally exercise the dispatch happy-path. Tests that want to
    exercise the gate write their own metadata file via
    ``credentials.store_account_class(...)`` after this fixture runs.
    """
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    metadata = tmp_path / "credentials.toml"
    metadata.write_text(
        '[cursor]\naccount_class = "service_account"\n', encoding="utf-8"
    )
    metadata.chmod(0o600)
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
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda worker_dir: [],
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


def test_default_worker_name_includes_repo_and_stable_hash(tmp_path: Path) -> None:
    """Generated worker names are deterministic and workspace-aware."""
    workspace = tmp_path / "Popola Loom!"
    rendered = cloud_worker_cmd._default_worker_name(workspace)
    rendered_again = cloud_worker_cmd._default_worker_name(workspace)
    assert rendered == rendered_again
    assert rendered.startswith("popolaloom-Popola-Loom-")
    assert len(rendered.rsplit("-", 1)[-1]) == 8


def test_parse_worker_start_cmdline_extracts_metadata(tmp_path: Path) -> None:
    """``agent worker start`` cmdlines expose worker dir, name, and management addr."""
    argv = [
        "/usr/local/bin/cursor-agent",
        "worker",
        "start",
        f"--worker-dir={tmp_path}",
        "--name",
        "popolaloom-PopolaLoom-deadbeef",
        "--management-addr=127.0.0.1:39231",
    ]
    parsed = cloud_worker_cmd._parse_worker_start_cmdline(1234, argv)
    assert parsed is not None
    assert parsed.pid == 1234
    assert parsed.worker_dir == tmp_path.resolve()
    assert parsed.name == "popolaloom-PopolaLoom-deadbeef"
    assert parsed.management_addr == "127.0.0.1:39231"


def test_detect_running_workers_matches_resolved_worker_dir(tmp_path: Path) -> None:
    """The procfs scanner matches normalized ``--worker-dir`` values."""
    worker_dir = tmp_path / "repo"
    worker_dir.mkdir()
    proc_root = tmp_path / "proc"
    (proc_root / "100").mkdir(parents=True)
    (proc_root / "200").mkdir()
    (proc_root / "abc").mkdir()
    (proc_root / "100" / "cmdline").write_bytes(
        b"agent\0worker\0start\0--worker-dir\0"
        + str(worker_dir).encode()
        + b"\0--name\0popolaloom-repo-12345678\0"
    )
    (proc_root / "200" / "cmdline").write_bytes(
        b"agent\0worker\0debug\0--worker-dir\0"
        + str(worker_dir).encode()
        + b"\0"
    )
    matches = cloud_worker_cmd._detect_running_workers_for_dir(
        worker_dir,
        proc_root=proc_root,
    )
    assert len(matches) == 1
    assert matches[0].pid == 100
    assert matches[0].name == "popolaloom-repo-12345678"


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


def test_worker_start_without_name_uses_workspace_default(
    runner: CliRunner,
    isolated_home: Path,
    fake_agent_binary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting ``--name`` passes the generated workspace-aware name upstream."""
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
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    argv = captured[0]
    name_idx = argv.index("--name")
    assert argv[name_idx + 1].startswith(
        f"popolaloom-{isolated_home.name}-"
    )


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


def test_worker_start_reuses_existing_workspace_worker(
    runner: CliRunner,
    isolated_home: Path,
    fake_agent_binary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default ``start`` exits 0 without spawning when the workspace worker exists."""
    spawned: list[Any] = []
    worker = cloud_worker_cmd.LocalWorkerProcess(
        pid=4242,
        worker_dir=isolated_home.resolve(),
        name="popolaloom-PopolaLoom-deadbeef",
        management_addr="127.0.0.1:39231",
        argv=("agent", "worker", "start"),
    )
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda worker_dir: [worker],
    )
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
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert spawned == []
    out = _combined_output(result)
    assert "Reusing existing Cursor self-hosted worker" in out
    assert "pid=4242" in out
    assert "name=popolaloom-PopolaLoom-deadbeef" in out


def test_worker_start_allow_duplicate_bypasses_reuse(
    runner: CliRunner,
    isolated_home: Path,
    fake_agent_binary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--allow-duplicate`` preserves an explicit second-start escape hatch."""
    captured: list[list[str]] = []
    worker = cloud_worker_cmd.LocalWorkerProcess(
        pid=4242,
        worker_dir=isolated_home.resolve(),
        name="existing",
        management_addr=None,
        argv=("agent", "worker", "start"),
    )
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda worker_dir: [worker],
    )
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
            "second-worker",
            "--allow-duplicate",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert len(captured) == 1
    assert "second-worker" in captured[0]


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


def test_worker_dispatch_posts_to_daemon_with_existing_worker_routing(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default dispatch POSTs a cursor-cloud body targeting the worker name."""
    worker = cloud_worker_cmd.LocalWorkerProcess(
        pid=4242,
        worker_dir=isolated_home.resolve(),
        name="popolaloom-PopolaLoom-deadbeef",
        management_addr="127.0.0.1:39231",
        argv=("agent", "worker", "start"),
    )
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda worker_dir: [worker],
    )
    captured: list[dict[str, Any]] = []

    def fake_post(body: dict[str, Any]) -> httpx.Response:
        captured.append(body)
        return httpx.Response(200, json={"task_id": "cursor-cloud-123"})

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
            "fix the tests",
            "--worker-dir",
            str(isolated_home),
            "--repo-url",
            "https://github.com/acme/repo",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert _combined_output(result).strip() == "cursor-cloud-123"
    assert captured == [
        {
            "cli": "cursor-cloud",
            "prompt": "fix the tests",
            "cwd": str(isolated_home.resolve()),
            "extra": {
                "worker_name": "popolaloom-PopolaLoom-deadbeef",
                "repo_url": "https://github.com/acme/repo",
                "starting_ref": "main",
                "model": "composer-2",
            },
        }
    ]


def test_worker_dispatch_daemon_down_exits_nonzero(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection failure names ``popolad`` and exits non-zero."""
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda worker_dir: [],
    )

    def boom(_body: dict[str, Any]) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        cloud_worker_cmd,
        "_post_popolad_dispatch_request",
        boom,
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
            "--pr-url",
            "https://github.com/acme/repo/pull/1",
        ],
    )
    assert result.exit_code == cloud_worker_cmd._EXIT_UNREACHABLE
    out = _combined_output(result)
    assert "popolad not running" in out


def test_worker_dispatch_json_prints_daemon_payload(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json`` prints the daemon response payload for direct dispatch."""
    worker = cloud_worker_cmd.LocalWorkerProcess(
        pid=4242,
        worker_dir=isolated_home.resolve(),
        name="popolaloom-json-worker",
        management_addr=None,
        argv=("agent", "worker", "start"),
    )
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda worker_dir: [worker],
    )

    def fake_post(_body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            json={"task_id": "cursor-cloud-json", "state": "queued"},
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
            "fix the tests",
            "--worker-dir",
            str(isolated_home),
            "--repo-url",
            "https://github.com/acme/repo",
            "--json",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    assert json.loads(_combined_output(result)) == {
        "task_id": "cursor-cloud-json",
        "state": "queued",
    }


def test_worker_dispatch_print_only_does_not_call_daemon(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--print-only`` preserves side-effect-free command preview mode."""
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda worker_dir: [],
    )

    def fail_if_called(_body: dict[str, Any]) -> httpx.Response:
        raise AssertionError("print-only must not POST to popolad")

    monkeypatch.setattr(
        cloud_worker_cmd,
        "_post_popolad_dispatch_request",
        fail_if_called,
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
            "--pr-url",
            "https://github.com/acme/repo/pull/1",
            "--print-only",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "No running worker found" in out
    assert "popola dispatch" in out
    assert "--cli=cursor-cloud" in out
    assert "pr_url=https://github.com/acme/repo/pull/1" in out
    assert "starting_ref=main" in out


def test_worker_dispatch_print_only_json_uses_generated_name_when_no_worker(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON preview still exposes deterministic fallback routing."""
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda worker_dir: [],
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
            "--pr-url",
            "https://github.com/acme/repo/pull/1",
            "--print-only",
            "--json",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    payload = json.loads(_combined_output(result))
    assert payload["worker"]["found"] is False
    assert payload["worker"]["name"].startswith(
        f"popolaloom-{isolated_home.name}-"
    )
    assert "pr_url=https://github.com/acme/repo/pull/1" in payload["command"]


@pytest.mark.parametrize(
    "response, expected",
    [
        (httpx.Response(404, json={"detail": "missing adapter"}), "unknown cli"),
        (httpx.Response(400, json={"detail": "bad extra"}), "dispatch failed"),
        (httpx.Response(503, text="upstream unavailable"), "unexpected status 503"),
        (httpx.Response(200, json={"state": "queued"}), "missing task_id"),
        (httpx.Response(200, json=["not", "an", "object"]), "must be a JSON object"),
    ],
)
def test_dispatch_to_popolad_error_responses_are_explicit(
    response: httpx.Response,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-happy daemon responses fail loudly with actionable messages."""
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_post_popolad_dispatch_request",
        lambda body: response,
    )

    with pytest.raises(typer.Exit) as exc_info:
        cloud_worker_cmd._dispatch_to_popolad(
            {"cli": "cursor-cloud", "prompt": "x", "extra": {}}
        )

    assert exc_info.value.exit_code == cloud_worker_cmd._EXIT_UNREACHABLE
    assert expected in capsys.readouterr().err


@pytest.mark.parametrize(
    "args, expected",
    [
        (
            [
                "--repo-url",
                "https://github.com/acme/repo",
                "--pr-url",
                "https://github.com/acme/repo/pull/1",
            ],
            "pass --repo-url OR --pr-url",
        ),
        ([], "pass either --repo-url or --pr-url"),
        (
            ["--repo-url", "https://github.com/acme/repo", "--starting-ref", ""],
            "--starting-ref must be non-empty",
        ),
        (
            ["--repo-url", "https://github.com/acme/repo", "--model", ""],
            "--model must be non-empty",
        ),
    ],
)
def test_worker_dispatch_rejects_invalid_args(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    expected: str,
) -> None:
    """Argument validation fails before daemon dispatch."""
    called: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_post_popolad_dispatch_request",
        lambda body: called.append(body) or httpx.Response(200, json={"task_id": "x"}),
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
            *args,
        ],
    )

    assert result.exit_code == cloud_worker_cmd._EXIT_INVALID_ARGS
    assert expected in _combined_output(result)
    assert called == []


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


def test_worker_help_text_lists_worker_verbs(runner: CliRunner) -> None:
    """``popola cloud worker --help`` exposes every worker verb."""
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["cloud", "worker", "--help"])
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    for verb in ("debug", "start", "status", "handoff", "dispatch"):
        assert verb in out, f"missing `{verb}` verb in:\n{out}"
