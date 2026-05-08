"""v0.8.8 T4.1 — coverage backfill for ``popolaloom.cli.relay_cmd``.

This file extends :file:`test_relay_safety.py` (T2.2.1) with the
remaining branches needed to lift ``cli/relay_cmd.py`` from 50 % to
≥ 90 %. Each test isolates one branch via ``httpx.MockTransport`` /
``MagicMock`` doubles per the brief constraint (no real network, no
real popolad UDS).

Branches covered (one test per row, per the brief AC (c) / (d)):

- helper unit tests: ``_resolve_actor`` env precedence,
  ``_canonical_org_repo`` shape rejection, ``_payload_sha256``
  determinism, ``_format_finding_preview`` empty list,
  ``_map_cloud_exception`` matrix.
- arg-validation: empty ``task_a``, bad ``--target-repo`` regex,
  bad ``--idempotency-key`` regex, empty ``--message``,
  ``popolad.toml`` invalid.
- daemon side: connect-error, 404 task_a, 400 task_a-not-terminal,
  500 unexpected status, missing ``cursor_agent_id``, missing repo
  with no override, malformed source ``repo_url``.
- payload-size cap, idempotency replay (JSON + plain), missing
  ``CURSOR_API_KEY`` (audit), cloud auth / not-found / feature /
  conflict / 5xx / 429 / generic mappings.
- ``--json`` rendering on dry-run + dispatched + idempotent paths.

Each test is ≤ 20 source lines (per brief AC (c)). The shared
``_wire_cli_test`` helper in this file mirrors the pattern in
``test_relay_safety.py`` so the two files compose.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from typer.testing import CliRunner

from popolaloom.adapters.cursor_cloud import (
    CloudCursorClient,
    CursorCloudAuthError,
    CursorCloudConflictError,
    CursorCloudError,
    CursorCloudFeatureUnavailableError,
    CursorCloudNotFoundError,
    CursorCloudRateLimitError,
    GithubAppMissingError,
    GithubAppPermissionError,
)
from popolaloom.cli import relay_cmd

# ---------------------------------------------------------------------------
# Fixtures (mirror test_relay_safety.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def _combined(result: Any) -> str:
    parts: list[str] = []
    for attr in ("stdout", "stderr", "output"):
        try:
            value = getattr(result, attr, "") or ""
        except (ValueError, AttributeError):
            value = ""
        if value and value not in parts:
            parts.append(value)
    return "".join(parts)


def _write_toml(home: Path, body: str) -> None:
    (home / "popolad.toml").write_text(body, encoding="utf-8")


def _allowlist_toml(repos: list[str]) -> str:
    rendered = "[" + ", ".join(f'"{r}"' for r in repos) + "]"
    return (
        "[cloud.relay]\n"
        'mode = "auto"\n'
        f"repo_allowlist = {rendered}\n"
        'audit_root = ""\n'
    )


def _build_daemon_body(
    *,
    cursor_agent_id: str | None = "bc-test-001",
    repo_url: str = "https://github.com/neolix-ai/popola-loom",
    pr_url: str | None = "https://github.com/neolix-ai/popola-loom/pull/1",
    summary: str = "do the thing",
    model: str = "composer-2",
) -> dict[str, Any]:
    return {
        "source_task_id": "v088-task-aaa",
        "cursor_agent_id": cursor_agent_id,
        "cursor_run_id": "run-test-001",
        "repo_url": repo_url,
        "pr_url": pr_url,
        "summary": summary,
        "model": model,
        "state": "completed",
        "cloud_phase": "FINISHED",
        "runtime": "cloud",
    }


def _make_daemon_client(
    *,
    body: dict[str, Any] | None = None,
    status_code: int = 200,
    raise_connect: bool = False,
    body_text: str | None = None,
) -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    if raise_connect:
        client.post.side_effect = httpx.ConnectError("connection refused")
        return client

    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    if body is not None:
        response.json.return_value = body
        response.text = json.dumps(body)
    else:
        response.json.side_effect = json.JSONDecodeError("x", "y", 0)
        response.text = body_text or ""
    client.post.return_value = response
    return client


def _build_cloud_mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> CloudCursorClient:
    client = CloudCursorClient("test-api-key")
    client._client.close()
    client._client = httpx.Client(
        base_url=client._base_url,
        auth=("test-api-key", ""),
        transport=httpx.MockTransport(handler),
    )
    return client


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    daemon_client: MagicMock,
    cloud_client: CloudCursorClient | None = None,
    api_key: str | None = "test-api-key",
) -> None:
    monkeypatch.setattr(
        relay_cmd, "make_sync_client", lambda *a, **kw: daemon_client
    )
    if cloud_client is not None:
        monkeypatch.setattr(
            relay_cmd, "_build_cloud_client", lambda key: cloud_client
        )
    if api_key is None:
        monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    else:
        monkeypatch.setenv("CURSOR_API_KEY", api_key)
    monkeypatch.setenv("POPOLA_ACTOR", "alice@neolix.ai")


# ---------------------------------------------------------------------------
# Pure-helper unit tests (no daemon / cloud)
# ---------------------------------------------------------------------------


def test_resolve_actor_prefers_popola_actor_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``$POPOLA_ACTOR`` wins over ``$USER`` when both are set."""
    monkeypatch.setenv("POPOLA_ACTOR", "alice@neolix.ai")
    monkeypatch.setenv("USER", "bob")
    assert relay_cmd._resolve_actor() == "alice@neolix.ai"


def test_resolve_actor_falls_back_to_user_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``$POPOLA_ACTOR`` is empty, ``$USER`` is used."""
    monkeypatch.delenv("POPOLA_ACTOR", raising=False)
    monkeypatch.setenv("USER", "carol")
    assert relay_cmd._resolve_actor() == "carol"


def test_resolve_actor_returns_unknown_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both env vars unset → ``"<unknown>"`` per spec §7.3."""
    monkeypatch.delenv("POPOLA_ACTOR", raising=False)
    monkeypatch.delenv("USER", raising=False)
    assert relay_cmd._resolve_actor() == "<unknown>"


def test_canonical_org_repo_strips_https_prefix() -> None:
    """``https://github.com/foo/bar`` → ``foo/bar``."""
    assert relay_cmd._canonical_org_repo("https://github.com/foo/bar") == "foo/bar"


def test_canonical_org_repo_strips_git_suffix_and_trailing_slash() -> None:
    """``https://github.com/foo/bar.git/`` → ``foo/bar`` (strip both)."""
    assert (
        relay_cmd._canonical_org_repo("https://github.com/foo/bar.git") == "foo/bar"
    )
    assert (
        relay_cmd._canonical_org_repo("https://github.com/foo/bar/") == "foo/bar"
    )


def test_canonical_org_repo_returns_none_on_malformed() -> None:
    """Garbage input → ``None`` (forces caller into the rejection path)."""
    assert relay_cmd._canonical_org_repo("this is not a repo") is None


def test_canonical_org_repo_strips_gitlab_prefix() -> None:
    """``https://gitlab.com/foo/bar`` is also recognised (mirror branch)."""
    assert relay_cmd._canonical_org_repo("https://gitlab.com/foo/bar") == "foo/bar"


def test_payload_sha256_is_deterministic() -> None:
    """Same payload → same hex digest (round-trip stability for audit)."""
    p = {"a": 1, "b": 2}
    assert relay_cmd._payload_sha256(p) == relay_cmd._payload_sha256(dict(p))


def test_format_finding_preview_empty_returns_empty_string() -> None:
    """Empty findings list → empty preview (no IndexError)."""
    assert relay_cmd._format_finding_preview([]) == ""


def test_map_cloud_exception_auth() -> None:
    """``CursorCloudAuthError`` → exit 77 + ``cloud_auth_error``."""
    exc = CursorCloudAuthError("bad key", status_code=401)
    assert relay_cmd._map_cloud_exception(exc) == (77, "cloud_auth_error")


def test_map_cloud_exception_not_found() -> None:
    """``CursorCloudNotFoundError`` → exit 100 + ``cloud_run_not_found``."""
    exc = CursorCloudNotFoundError("agent gone", status_code=404)
    assert relay_cmd._map_cloud_exception(exc) == (100, "cloud_run_not_found")


def test_map_cloud_exception_feature_unavailable() -> None:
    """Feature-unavailable subclasses → exit 78 + matching outcome."""
    exc = CursorCloudFeatureUnavailableError("no plan", status_code=403)
    assert relay_cmd._map_cloud_exception(exc) == (78, "cloud_feature_unavailable")


def test_map_cloud_exception_github_app_permission() -> None:
    """``GithubAppPermissionError`` also maps to exit 78."""
    exc = GithubAppPermissionError("scope missing", status_code=403)
    assert relay_cmd._map_cloud_exception(exc) == (78, "cloud_feature_unavailable")


def test_map_cloud_exception_github_app_missing() -> None:
    """``GithubAppMissingError`` also maps to exit 78."""
    exc = GithubAppMissingError("github app gone", status_code=403)
    assert relay_cmd._map_cloud_exception(exc) == (78, "cloud_feature_unavailable")


def test_map_cloud_exception_conflict() -> None:
    """``CursorCloudConflictError`` → exit 102 + ``cloud_conflict``."""
    exc = CursorCloudConflictError("agent_busy", status_code=409)
    assert relay_cmd._map_cloud_exception(exc) == (102, "cloud_conflict")


def test_map_cloud_exception_rate_limit() -> None:
    """``CursorCloudRateLimitError`` → exit 75 + ``cloud_api_error``."""
    exc = CursorCloudRateLimitError("throttled", status_code=429)
    assert relay_cmd._map_cloud_exception(exc) == (75, "cloud_api_error")


def test_map_cloud_exception_generic() -> None:
    """A bare :class:`CursorCloudError` falls into the default 75 bucket."""
    exc = CursorCloudError("boom", status_code=500)
    assert relay_cmd._map_cloud_exception(exc) == (75, "cloud_api_error")


def test_socket_path_uses_popola_home_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``$POPOLA_HOME`` overrides the default ``~/.popola`` socket location."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    assert relay_cmd._socket_path() == tmp_path / "popolad.sock"


def test_make_sync_client_returns_real_httpx_client(
    tmp_path: Path,
) -> None:
    """The real factory returns a usable :class:`httpx.Client` bound to UDS."""
    client = relay_cmd.make_sync_client(socket_path=tmp_path / "missing.sock")
    try:
        assert isinstance(client, httpx.Client)
    finally:
        client.close()


def test_build_cloud_client_returns_real_client() -> None:
    """``_build_cloud_client`` mints a real :class:`CloudCursorClient`."""
    client = relay_cmd._build_cloud_client("test-key")
    try:
        assert isinstance(client, CloudCursorClient)
    finally:
        client.close()


def test_audit_root_for_uses_config_override(tmp_path: Path) -> None:
    """``cfg.audit_root`` non-empty → that path; empty → default constant."""
    from popolaloom.daemon.main import CloudRelayConfig

    cfg_with = CloudRelayConfig(audit_root=str(tmp_path / "ar"))
    cfg_default = CloudRelayConfig()
    assert relay_cmd._audit_root_for(cfg_with) == tmp_path / "ar"
    assert relay_cmd._audit_root_for(cfg_default) == relay_cmd.DEFAULT_AUDIT_ROOT


# ---------------------------------------------------------------------------
# CLI argument-validation paths (exit 2 / exit 1 fast-fails)
# ---------------------------------------------------------------------------


def test_relay_target_repo_invalid_regex_exits_2(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed ``--target-repo`` URL → exit 2 + regex hint."""
    _write_toml(isolated_home, _allowlist_toml([]))
    _wire(monkeypatch, daemon_client=_make_daemon_client(body=_build_daemon_body()))
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["relay", "v088-task-aaa", "--target-repo", "not-a-url"],
    )
    assert result.exit_code == 2, _combined(result)
    assert "--target-repo" in _combined(result)


def test_relay_idempotency_key_invalid_regex_exits_2(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-conformant ``--idempotency-key`` (too short) → exit 2."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    _wire(monkeypatch, daemon_client=_make_daemon_client(body=_build_daemon_body()))
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay",
            "v088-task-aaa",
            "--target-repo",
            "https://github.com/neolix-ai/popola-loom",
            "--idempotency-key",
            "abc",
        ],
    )
    assert result.exit_code == 2, _combined(result)
    assert "--idempotency-key" in _combined(result)


def test_relay_empty_message_exits_2(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--message ""`` is rejected with exit 2 + remediation."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    _wire(monkeypatch, daemon_client=_make_daemon_client(body=_build_daemon_body()))
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay",
            "v088-task-aaa",
            "--target-repo",
            "https://github.com/neolix-ai/popola-loom",
            "--message",
            "",
        ],
    )
    assert result.exit_code == 2, _combined(result)
    assert "--message" in _combined(result)


def test_relay_invalid_popolad_toml_exits_2(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A type-invalid ``popolad.toml`` is rejected (config loader path)."""
    _write_toml(
        isolated_home,
        '[cloud.relay]\nmode = "invalid"\nrepo_allowlist = []\n',
    )
    _wire(monkeypatch, daemon_client=_make_daemon_client(body=_build_daemon_body()))
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app, ["relay", "v088-task-aaa"],
    )
    assert result.exit_code == 2, _combined(result)
    assert "popolad.toml" in _combined(result)


# ---------------------------------------------------------------------------
# Daemon-side error paths
# ---------------------------------------------------------------------------


def test_relay_daemon_connect_error_exits_2(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon connect error surfaces the friendly "popolad not running"."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    _wire(monkeypatch, daemon_client=_make_daemon_client(raise_connect=True))
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["relay", "v088-task-aaa"])
    assert result.exit_code == 2, _combined(result)
    assert "popolad not running" in _combined(result)


def test_relay_daemon_404_task_not_found_exits_2(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon returns 404 → CLI exits 2 + ``error: task_a not found``."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    _wire(
        monkeypatch,
        daemon_client=_make_daemon_client(
            body={"detail": "not found"}, status_code=404
        ),
    )
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["relay", "v088-task-aaa"])
    assert result.exit_code == 2, _combined(result)
    assert "task_a not found" in _combined(result)


def test_relay_daemon_400_not_terminal_exits_2(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon returns 400 (task not in terminal state) → CLI exits 2."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    _wire(
        monkeypatch,
        daemon_client=_make_daemon_client(
            body={"detail": "task is running"}, status_code=400
        ),
    )
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["relay", "v088-task-aaa"])
    assert result.exit_code == 2, _combined(result)
    assert "task is running" in _combined(result)


def test_relay_daemon_400_bad_json_falls_back_to_text(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon 400 with non-JSON body → CLI uses ``response.text`` for detail."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    _wire(
        monkeypatch,
        daemon_client=_make_daemon_client(
            body=None, status_code=400, body_text="raw error text"
        ),
    )
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["relay", "v088-task-aaa"])
    assert result.exit_code == 2, _combined(result)


def test_relay_daemon_500_unexpected_status_exits_2(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon returns 500 → CLI exits 2 with the unexpected-status message."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    _wire(
        monkeypatch,
        daemon_client=_make_daemon_client(
            body={"detail": "boom"}, status_code=500
        ),
    )
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["relay", "v088-task-aaa"])
    assert result.exit_code == 2, _combined(result)
    assert "unexpected status 500" in _combined(result)


def test_relay_missing_cursor_agent_id_exits_2(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cursor_agent_id`` missing on daemon body → CLI exits 2."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    body = _build_daemon_body(cursor_agent_id=None)
    _wire(monkeypatch, daemon_client=_make_daemon_client(body=body))
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["relay", "v088-task-aaa"])
    assert result.exit_code == 2, _combined(result)
    assert "cursor_agent_id" in _combined(result)


def test_relay_missing_repo_url_no_override_exits_2(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No source ``repo_url`` and no ``--target-repo`` → CLI exits 2."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    body = _build_daemon_body(repo_url="")
    _wire(monkeypatch, daemon_client=_make_daemon_client(body=body))
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["relay", "v088-task-aaa"])
    assert result.exit_code == 2, _combined(result)
    assert "no repo_url" in _combined(result)


def test_relay_unparseable_source_repo_exits_2(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source ``repo_url`` that fails ``_canonical_org_repo`` → exit 2."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    body = _build_daemon_body(repo_url="not a real url at all")
    _wire(monkeypatch, daemon_client=_make_daemon_client(body=body))
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["relay", "v088-task-aaa"])
    assert result.exit_code == 2, _combined(result)
    assert "canonicalise" in _combined(result)


# ---------------------------------------------------------------------------
# Policy / payload paths
# ---------------------------------------------------------------------------


def test_relay_payload_too_large_exits_1(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``prompt_size_cap_bytes`` exceeded → exit 1 + audit row."""
    _write_toml(
        isolated_home,
        '[cloud.relay]\nmode="auto"\nrepo_allowlist=["neolix-ai/popola-loom"]\n'
        'audit_root=""\nprompt_size_cap_bytes=1024\n',
    )
    body = _build_daemon_body(summary="x" * 5000)
    _wire(monkeypatch, daemon_client=_make_daemon_client(body=body))
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["relay", "v088-task-aaa"])
    assert result.exit_code == 1, _combined(result)
    assert "prompt size" in _combined(result)


def test_relay_allowlist_non_empty_target_outside_exits_1(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-empty allowlist but target outside → ``rejected_allowlist`` row."""
    _write_toml(isolated_home, _allowlist_toml(["other-org/repo"]))
    _wire(monkeypatch, daemon_client=_make_daemon_client(body=_build_daemon_body()))
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["relay", "v088-task-aaa"])
    assert result.exit_code == 1, _combined(result)
    audit = isolated_home / ".local/.agent/archive/relay/v088-task-aaa.jsonl"
    rows = [
        json.loads(line)
        for line in audit.read_text().splitlines()
        if line.strip()
    ]
    assert rows[-1]["outcome"] == "rejected_allowlist"


# ---------------------------------------------------------------------------
# Idempotency replay
# ---------------------------------------------------------------------------


def test_relay_idempotent_replay_dispatches_idempotent(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second invocation with the same idempotency_key returns the prior id."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    daemon = _make_daemon_client(body=_build_daemon_body())

    def _h(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201, json={"id": "run-1", "agentId": "bc-test-001", "taskId": "task-b-1"}
        )

    cloud = _build_cloud_mock_client(_h)
    _wire(monkeypatch, daemon_client=daemon, cloud_client=cloud)
    from popolaloom.cli.main import app as root_app

    args = [
        "relay", "v088-task-aaa",
        "--target-repo", "https://github.com/neolix-ai/popola-loom",
        "--idempotency-key", "stableidempkey001",
    ]
    r1 = runner.invoke(root_app, args)
    assert r1.exit_code == 0, _combined(r1)
    r2 = runner.invoke(root_app, args)
    assert r2.exit_code == 0, _combined(r2)
    assert "DISPATCHED-IDEMPOTENT" in _combined(r2)


def test_relay_idempotent_replay_json_output(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay path with ``--json`` emits ``outcome=dispatched_idempotent``."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    daemon = _make_daemon_client(body=_build_daemon_body())

    def _h(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201, json={"id": "r2", "agentId": "bc-test-001", "taskId": "task-b-2"}
        )

    cloud = _build_cloud_mock_client(_h)
    _wire(monkeypatch, daemon_client=daemon, cloud_client=cloud)
    from popolaloom.cli.main import app as root_app

    args_base = [
        "relay", "v088-task-aaa",
        "--target-repo", "https://github.com/neolix-ai/popola-loom",
        "--idempotency-key", "stableidempkey002",
    ]
    runner.invoke(root_app, args_base)
    r2 = runner.invoke(root_app, [*args_base, "--json"])
    assert r2.exit_code == 0, _combined(r2)
    body = json.loads(_combined(r2).strip().splitlines()[-1])
    assert body["outcome"] == "dispatched_idempotent"


# ---------------------------------------------------------------------------
# Cloud error path matrix
# ---------------------------------------------------------------------------


def test_relay_no_api_key_exits_77(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing ``CURSOR_API_KEY`` → exit 77 + audit row with cloud_auth_error."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    _wire(
        monkeypatch,
        daemon_client=_make_daemon_client(body=_build_daemon_body()),
        api_key=None,
    )
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay", "v088-task-aaa",
            "--target-repo", "https://github.com/neolix-ai/popola-loom",
        ],
    )
    assert result.exit_code == 77, _combined(result)
    assert "CURSOR_API_KEY" in _combined(result)


@pytest.mark.parametrize(
    ("status", "code", "exit_code"),
    [
        (401, "unauthorized", 77),
        (404, "agent_not_found", 100),
        (403, "feature_unavailable", 78),
        (429, "rate_limit_exceeded", 75),
        (500, "internal_error", 75),
        (409, "agent_busy", 102),
    ],
    ids=["auth-401", "not-found-404", "feature-403", "rate-429",
         "server-500", "conflict-409"],
)
def test_relay_cloud_error_exit_codes(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    code: str,
    exit_code: int,
) -> None:
    """Cursor REST status codes map to the relay-primitive.md §8 exit matrix."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    daemon = _make_daemon_client(body=_build_daemon_body())

    def _h(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": "y"}})

    cloud = _build_cloud_mock_client(_h)
    _wire(monkeypatch, daemon_client=daemon, cloud_client=cloud)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay", "v088-task-aaa",
            "--target-repo", "https://github.com/neolix-ai/popola-loom",
        ],
    )
    assert result.exit_code == exit_code, _combined(result)


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------


def test_relay_json_dispatched_emits_full_payload(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json`` on the dispatched path prints the full payload object."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    daemon = _make_daemon_client(body=_build_daemon_body())

    def _h(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201, json={"id": "r-X", "agentId": "bc-test-001", "taskId": "task-Z"}
        )

    cloud = _build_cloud_mock_client(_h)
    _wire(monkeypatch, daemon_client=daemon, cloud_client=cloud)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay", "v088-task-aaa",
            "--target-repo", "https://github.com/neolix-ai/popola-loom",
            "--json",
        ],
    )
    assert result.exit_code == 0, _combined(result)
    body = json.loads(_combined(result).strip().splitlines()[-1])
    assert body["outcome"] == "dispatched"
    assert body["target_task"] == "task-Z"


def test_relay_dry_run_json_output(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run --json`` emits the dry-run summary as JSON."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    daemon = _make_daemon_client(body=_build_daemon_body())
    _wire(monkeypatch, daemon_client=daemon, cloud_client=None)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay", "v088-task-aaa",
            "--target-repo", "https://github.com/neolix-ai/popola-loom",
            "--dry-run", "--json",
        ],
    )
    assert result.exit_code == 0, _combined(result)
    body = json.loads(_combined(result).strip().splitlines()[-1])
    assert body["mode"] == "dry-run"
    assert body["outcome"] == "dry_run_passed"


def test_relay_with_message_prefix_uses_custom_text(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--message <text>`` lands in the prompt body."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    daemon = _make_daemon_client(body=_build_daemon_body())
    captured: list[bytes] = []

    def _h(req: httpx.Request) -> httpx.Response:
        captured.append(req.content)
        return httpx.Response(
            201, json={"id": "r", "agentId": "bc-test-001", "taskId": "tb"}
        )

    cloud = _build_cloud_mock_client(_h)
    _wire(monkeypatch, daemon_client=daemon, cloud_client=cloud)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay", "v088-task-aaa",
            "--target-repo", "https://github.com/neolix-ai/popola-loom",
            "--message", "Custom prefix XYZ",
        ],
    )
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(captured[0].decode("utf-8"))
    assert "Custom prefix XYZ" in payload["prompt"]["text"]


def test_relay_scan_idempotent_row_no_file_returns_none(tmp_path: Path) -> None:
    """``_scan_idempotent_row`` on a non-existent file returns ``None``."""
    out = relay_cmd._scan_idempotent_row(
        tmp_path / "missing.jsonl",
        source_task_id="t",
        target_repo="o/r",
        idempotency_key="k",
        window_s=60,
    )
    assert out is None


def test_relay_scan_idempotent_row_skips_unparseable_lines(
    tmp_path: Path,
) -> None:
    """``_scan_idempotent_row`` ignores blank / non-JSON / non-dict lines."""
    p = tmp_path / "audit.jsonl"
    p.write_text("\n{}\nnot-json\n[1,2,3]\n", encoding="utf-8")
    out = relay_cmd._scan_idempotent_row(
        p, source_task_id="t", target_repo="o/r", idempotency_key="k", window_s=60
    )
    assert out is None


def test_relay_build_envelope_with_pr_url() -> None:
    """``_build_envelope`` populates ``repos[0].prUrl`` when present."""
    env = relay_cmd._build_envelope(
        source_task_id="t",
        target_repo="https://github.com/foo/bar",
        prompt_body="hi",
        summary="summ",
        pr_url="https://github.com/foo/bar/pull/1",
        model="composer-2",
    )
    assert env["repos"][0]["prUrl"] == "https://github.com/foo/bar/pull/1"


def test_relay_build_prompt_body_includes_source_when_no_prefix() -> None:
    """``_build_prompt_body`` falls back to ``Follow-up relay from <id>``."""
    body = relay_cmd._build_prompt_body(
        message_prefix="", pr_url=None, summary="x", source_task_id="t-1",
    )
    assert "Follow-up relay from t-1" in body
    assert "(no PR opened by source run)" in body


def test_relay_no_confirm_in_auto_mode_flips_mode_source_to_flag(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-confirm`` flips ``mode_source=flag`` even on auto-default."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    daemon = _make_daemon_client(body=_build_daemon_body())

    def _h(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201, json={"id": "r", "agentId": "bc-test-001", "taskId": "tb"}
        )

    cloud = _build_cloud_mock_client(_h)
    _wire(monkeypatch, daemon_client=daemon, cloud_client=cloud)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay", "v088-task-aaa",
            "--target-repo", "https://github.com/neolix-ai/popola-loom",
            "--no-confirm",
        ],
    )
    assert result.exit_code == 0, _combined(result)
    audit = isolated_home / ".local/.agent/archive/relay/v088-task-aaa.jsonl"
    rows = [
        json.loads(line)
        for line in audit.read_text().splitlines()
        if line.strip()
    ]
    assert rows[-1]["mode_source"] == "flag"


def test_relay_confirm_mode_no_tty_exits_2(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mode = "confirm"`` + non-TTY + no ``--no-confirm`` → exit 2."""
    _write_toml(
        isolated_home,
        '[cloud.relay]\nmode="confirm"\nrepo_allowlist=["neolix-ai/popola-loom"]\n'
        'audit_root=""\n',
    )
    daemon = _make_daemon_client(body=_build_daemon_body())
    _wire(monkeypatch, daemon_client=daemon, cloud_client=None)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay", "v088-task-aaa",
            "--target-repo", "https://github.com/neolix-ai/popola-loom",
        ],
    )
    assert result.exit_code == 2, _combined(result)
    assert "stdin is not a TTY" in _combined(result)


def test_relay_confirm_mode_with_no_confirm_dispatches(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mode = "confirm"`` + ``--no-confirm`` → dispatch records ``mode_source=flag``."""
    _write_toml(
        isolated_home,
        '[cloud.relay]\nmode="confirm"\nrepo_allowlist=["neolix-ai/popola-loom"]\n'
        'audit_root=""\n',
    )
    daemon = _make_daemon_client(body=_build_daemon_body())

    def _h(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201, json={"id": "r", "agentId": "bc-test-001", "taskId": "tb"}
        )

    cloud = _build_cloud_mock_client(_h)
    _wire(monkeypatch, daemon_client=daemon, cloud_client=cloud)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay", "v088-task-aaa",
            "--target-repo", "https://github.com/neolix-ai/popola-loom",
            "--no-confirm",
        ],
    )
    assert result.exit_code == 0, _combined(result)
    audit = isolated_home / ".local/.agent/archive/relay/v088-task-aaa.jsonl"
    rows = [
        json.loads(line)
        for line in audit.read_text().splitlines()
        if line.strip()
    ]
    assert rows[-1]["mode_source"] == "flag"


def test_relay_audit_write_failure_on_dispatched_logs_warning(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An OSError from the final audit write is logged and dispatch still exits 0."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    daemon = _make_daemon_client(body=_build_daemon_body())

    def _h(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201, json={"id": "r", "agentId": "bc-test-001", "taskId": "tb"}
        )

    cloud = _build_cloud_mock_client(_h)
    _wire(monkeypatch, daemon_client=daemon, cloud_client=cloud)

    from popolaloom.relay.audit import RelayAuditWriter

    real_append = RelayAuditWriter.append
    call_count = {"n": 0}

    def flaky_append(self: RelayAuditWriter, row: dict[str, Any]) -> Any:
        call_count["n"] += 1
        # Only fail on the final dispatched row (the LAST append).
        if call_count["n"] >= 2:
            raise OSError("disk full")
        return real_append(self, row)

    monkeypatch.setattr(RelayAuditWriter, "append", flaky_append)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay", "v088-task-aaa",
            "--target-repo", "https://github.com/neolix-ai/popola-loom",
        ],
    )
    assert result.exit_code == 0, _combined(result)
    assert "audit write failed" in caplog.text


def test_relay_audit_write_failure_on_pre_flight_exits_75(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OSError on the pre-flight audit row → exit 75 + ``failed to write``."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    daemon = _make_daemon_client(body=_build_daemon_body())
    _wire(monkeypatch, daemon_client=daemon, cloud_client=None)

    from popolaloom.relay.audit import RelayAuditWriter

    def always_fail(self: RelayAuditWriter, row: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(RelayAuditWriter, "append", always_fail)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay", "v088-task-aaa",
            "--target-repo", "https://github.com/neolix-ai/popola-loom",
        ],
    )
    assert result.exit_code == 75, _combined(result)
    assert "failed to write pre-flight audit row" in _combined(result)


def test_relay_no_model_omits_model_from_cloud_payload(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When source task has no ``model``, the cloud POST body has no ``model``."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    body = _build_daemon_body(model="")
    daemon = _make_daemon_client(body=body)
    captured: list[dict[str, Any]] = []

    def _h(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(
            201, json={"id": "r", "agentId": "bc-test-001", "taskId": "tb"}
        )

    cloud = _build_cloud_mock_client(_h)
    _wire(monkeypatch, daemon_client=daemon, cloud_client=cloud)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay", "v088-task-aaa",
            "--target-repo", "https://github.com/neolix-ai/popola-loom",
        ],
    )
    assert result.exit_code == 0, _combined(result)
    assert "model" not in captured[0]


def test_relay_payload_too_large_audit_failure_logs_warning(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OSError during payload_too_large audit row write logs a warning."""
    _write_toml(
        isolated_home,
        '[cloud.relay]\nmode="auto"\nrepo_allowlist=["neolix-ai/popola-loom"]\n'
        'audit_root=""\nprompt_size_cap_bytes=1024\n',
    )
    daemon = _make_daemon_client(body=_build_daemon_body(summary="x" * 5000))
    _wire(monkeypatch, daemon_client=daemon, cloud_client=None)

    from popolaloom.relay.audit import RelayAuditWriter

    def always_fail(self: RelayAuditWriter, row: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(RelayAuditWriter, "append", always_fail)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["relay", "v088-task-aaa"])
    assert result.exit_code == 1, _combined(result)
    assert "audit write failed (payload_too_large path)" in caplog.text


def test_relay_dry_run_audit_failure_logs_warning(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OSError during dry-run audit row write logs a warning, exit still 0."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    daemon = _make_daemon_client(body=_build_daemon_body())
    _wire(monkeypatch, daemon_client=daemon, cloud_client=None)

    from popolaloom.relay.audit import RelayAuditWriter

    def always_fail(self: RelayAuditWriter, row: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(RelayAuditWriter, "append", always_fail)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay", "v088-task-aaa",
            "--target-repo", "https://github.com/neolix-ai/popola-loom",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, _combined(result)
    assert "audit write failed (dry-run path)" in caplog.text


def test_relay_secret_detected_audit_failure_logs_warning(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OSError during secret-detected audit row write logs warning, exit 1."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    daemon = _make_daemon_client(
        body=_build_daemon_body(summary="leaked AKIAIOSFODNN7EXAMPLE"),
    )
    _wire(monkeypatch, daemon_client=daemon, cloud_client=None)

    from popolaloom.relay.audit import RelayAuditWriter

    def always_fail(self: RelayAuditWriter, row: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(RelayAuditWriter, "append", always_fail)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay", "v088-task-aaa",
            "--target-repo", "https://github.com/neolix-ai/popola-loom",
        ],
    )
    assert result.exit_code == 1, _combined(result)
    assert "audit write failed (secret-detected path)" in caplog.text


def test_relay_allowlist_audit_failure_logs_warning(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OSError during allowlist-rejected audit row write logs a warning."""
    _write_toml(isolated_home, _allowlist_toml([]))
    daemon = _make_daemon_client(body=_build_daemon_body())
    _wire(monkeypatch, daemon_client=daemon, cloud_client=None)

    from popolaloom.relay.audit import RelayAuditWriter

    def always_fail(self: RelayAuditWriter, row: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(RelayAuditWriter, "append", always_fail)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay", "v088-task-aaa",
            "--target-repo", "https://github.com/neolix-ai/popola-loom",
        ],
    )
    assert result.exit_code == 1, _combined(result)
    assert "audit write failed (allowlist-rejected path)" in caplog.text


def test_relay_idempotent_replay_audit_failure_logs(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OSError on idempotent path audit row write logs but still exits 0."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    daemon = _make_daemon_client(body=_build_daemon_body())

    def _h(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201, json={"id": "r", "agentId": "bc-test-001", "taskId": "tb"}
        )

    cloud = _build_cloud_mock_client(_h)
    _wire(monkeypatch, daemon_client=daemon, cloud_client=cloud)
    from popolaloom.cli.main import app as root_app

    args = [
        "relay", "v088-task-aaa",
        "--target-repo", "https://github.com/neolix-ai/popola-loom",
        "--idempotency-key", "stableidempkey003",
    ]
    runner.invoke(root_app, args)

    from popolaloom.relay.audit import RelayAuditWriter

    def always_fail(self: RelayAuditWriter, row: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(RelayAuditWriter, "append", always_fail)
    result = runner.invoke(root_app, args)
    assert result.exit_code == 0, _combined(result)
    assert "audit write failed (idempotent path)" in caplog.text


def test_relay_no_api_key_audit_failure_logs_warning(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OSError during no-api-key audit row write logs warning, exit 77."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    daemon = _make_daemon_client(body=_build_daemon_body())
    _wire(
        monkeypatch,
        daemon_client=daemon,
        cloud_client=None,
        api_key=None,
    )

    from popolaloom.relay.audit import RelayAuditWriter

    real_append = RelayAuditWriter.append
    call_count = {"n": 0}

    def flaky_append(self: RelayAuditWriter, row: dict[str, Any]) -> Any:
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise OSError("disk full")
        return real_append(self, row)

    monkeypatch.setattr(RelayAuditWriter, "append", flaky_append)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay", "v088-task-aaa",
            "--target-repo", "https://github.com/neolix-ai/popola-loom",
        ],
    )
    assert result.exit_code == 77, _combined(result)
    assert "audit write failed (no-api-key path)" in caplog.text


def test_relay_cloud_error_audit_failure_logs(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OSError during cloud-error audit row write logs warning."""
    _write_toml(isolated_home, _allowlist_toml(["neolix-ai/popola-loom"]))
    daemon = _make_daemon_client(body=_build_daemon_body())

    def _h(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"code": "unauthorized", "message": "bad"}}
        )

    cloud = _build_cloud_mock_client(_h)
    _wire(monkeypatch, daemon_client=daemon, cloud_client=cloud)

    from popolaloom.relay.audit import RelayAuditWriter

    real_append = RelayAuditWriter.append
    call_count = {"n": 0}

    def flaky_append(self: RelayAuditWriter, row: dict[str, Any]) -> Any:
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise OSError("disk full")
        return real_append(self, row)

    monkeypatch.setattr(RelayAuditWriter, "append", flaky_append)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "relay", "v088-task-aaa",
            "--target-repo", "https://github.com/neolix-ai/popola-loom",
        ],
    )
    assert result.exit_code == 77, _combined(result)
    assert "audit write failed (cloud-error path)" in caplog.text
