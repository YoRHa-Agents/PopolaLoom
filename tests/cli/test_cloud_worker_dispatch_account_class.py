"""``popola cloud worker dispatch`` v0.9.9 F5 + U1 pre-flight gate tests.

Acceptance-criteria coverage from the L0 brief (numbered (g) → (l)):

* (g) Dispatch refuses with ``Exit(78)`` when class == ``personal``.
* (h) Dispatch refuses with ``Exit(78)`` when class == ``unknown`` (the
  env-var-only operator path that pre-dates v0.9.9).
* (i) Dispatch proceeds normally when class == ``service_account`` —
  body shape unchanged from v0.9.8.
* (j) Hint text contains all five required substrings: ``popola cloud
  worker handoff``, ``popola dispatch --cli=cursor``,
  ``SCHEMA_INVESTIGATION.md``,
  ``https://cursor.com/docs/cloud-agent/self-hosted-pool#authenticate-workers``,
  AND the Chinese fragment ``当前 Cursor API key 类别不支持``.
* (k) ``caplog`` captures the
  ``worker_dispatch refused: account_class=`` WARN entry (No Silent
  Failures).
* (l) Pre-flight gate runs BEFORE the popolad RPC — the
  ``_post_popolad_dispatch_request`` stub is NOT called when the gate
  refuses.

The Spike-0 verdict (BRANCH_B) sourced from
``.local/.agent/active/v0.9.9-worker-observability/SCHEMA_INVESTIGATION.md``
locks the gate behaviour for v0.9.x.

Hermetic via ``tmp_path`` + ``monkeypatch.setenv("POPOLA_HOME", ...)``;
no real subprocess / network IO.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from popolaloom.cli import cloud_worker_cmd

# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Hermetic ``$POPOLA_HOME`` + ``$HOME`` so worker paths can't bleed."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def _write_credentials_toml(home: Path, account_class: str | None) -> None:
    """Write a minimal ``credentials.toml`` under ``home``.

    When ``account_class`` is :data:`None` no ``account_class`` key is
    emitted — that's the AC (h) ``unknown`` path (the env-var-only
    operator who never ran ``popola auth cursor set``).
    """
    home.mkdir(parents=True, exist_ok=True)
    lines = [
        "# popola credentials metadata — non-secret. Do not commit.",
        "[cursor]",
        'backend = "keyring"',
        'fingerprint = "aabbccddeeff"',
        'last_set_at = "2026-05-09T08:00:00Z"',
    ]
    if account_class is not None:
        lines.append(f'account_class = "{account_class}"')
    (home / "credentials.toml").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _combined_output(result: Any) -> str:
    parts: list[str] = []
    for attr in ("stdout", "stderr", "output"):
        try:
            value = getattr(result, attr, "") or ""
        except (ValueError, AttributeError):
            value = ""
        if value and value not in parts:
            parts.append(value)
    return "".join(parts)


# ── (g) personal class refused with Exit(78) ──────────────────────────


def test_personal_account_class_refuses_with_exit_78(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC (g): explicit ``personal`` blocks dispatch with exit 78."""
    _write_credentials_toml(isolated_home, "personal")
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda worker_dir: [],
    )

    def must_not_be_called(_body: dict[str, Any]) -> httpx.Response:
        raise AssertionError("popolad RPC must NOT be called when gate refuses")

    monkeypatch.setattr(
        cloud_worker_cmd,
        "_post_popolad_dispatch_request",
        must_not_be_called,
    )
    from popolaloom.cli.main import app as root_app

    with caplog.at_level("WARNING", logger="popolaloom.cli.cloud_worker_cmd"):
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
    assert result.exit_code == cloud_worker_cmd._EXIT_ACCOUNT_CLASS_GATE
    out = _combined_output(result)
    assert "popola cloud worker dispatch is unavailable" in out


# ── (h) unknown class (legacy or env-only operator) refused ─────────


def test_unknown_account_class_refuses_with_exit_78(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (h): a credentials.toml WITHOUT account_class also refuses."""
    _write_credentials_toml(isolated_home, account_class=None)
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda worker_dir: [],
    )

    def must_not_be_called(_body: dict[str, Any]) -> httpx.Response:
        raise AssertionError("popolad RPC must NOT be called when gate refuses")

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
    assert result.exit_code == cloud_worker_cmd._EXIT_ACCOUNT_CLASS_GATE


def test_no_credentials_toml_at_all_refuses(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (h) extension: a fresh install (no metadata file) also blocks."""
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda worker_dir: [],
    )

    def must_not_be_called(_body: dict[str, Any]) -> httpx.Response:
        raise AssertionError("popolad RPC must NOT be called when gate refuses")

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
    assert result.exit_code == cloud_worker_cmd._EXIT_ACCOUNT_CLASS_GATE


# ── (i) service_account proceeds; body shape unchanged ─────────────


def test_service_account_class_proceeds_unchanged(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (i): ``service_account`` lets the v0.9.8 body shape through verbatim."""
    _write_credentials_toml(isolated_home, "service_account")
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
        return httpx.Response(200, json={"task_id": "cursor-cloud-sa"})

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
    assert _combined_output(result).strip() == "cursor-cloud-sa"
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


# ── (j) hint text contains all five required substrings ─────────────


def test_pre_flight_hint_contains_all_required_substrings(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (j): every documented workaround / link / bilingual fragment is present."""
    _write_credentials_toml(isolated_home, "personal")
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda worker_dir: [],
    )
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_post_popolad_dispatch_request",
        lambda body: (_ for _ in ()).throw(
            AssertionError("RPC must not be called")
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
    out = _combined_output(result)
    assert result.exit_code == cloud_worker_cmd._EXIT_ACCOUNT_CLASS_GATE
    for required in (
        "popola cloud worker handoff",
        "popola dispatch --cli=cursor",
        "SCHEMA_INVESTIGATION.md",
        "https://cursor.com/docs/cloud-agent/self-hosted-pool#authenticate-workers",
        "当前 Cursor API key 类别不支持",
    ):
        assert required in out, f"missing required hint substring: {required!r}"


# ── (k) WARN log fires with the gate outcome ────────────────────────


def test_pre_flight_emits_warn_log_with_account_class(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC (k): the daemon-style ``worker_dispatch refused`` WARN entry is emitted."""
    _write_credentials_toml(isolated_home, "personal")
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda worker_dir: [],
    )
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_post_popolad_dispatch_request",
        lambda body: (_ for _ in ()).throw(
            AssertionError("RPC must not be called")
        ),
    )
    from popolaloom.cli.main import app as root_app

    with caplog.at_level("WARNING", logger="popolaloom.cli.cloud_worker_cmd"):
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
    assert result.exit_code == cloud_worker_cmd._EXIT_ACCOUNT_CLASS_GATE
    matching = [
        record
        for record in caplog.records
        if "worker_dispatch refused: account_class=" in record.getMessage()
    ]
    assert matching, (
        "expected at least one WARN with "
        "'worker_dispatch refused: account_class=' present in caplog"
    )
    assert any("personal" in record.getMessage() for record in matching)


# ── (l) pre-flight gate runs BEFORE the popolad RPC ─────────────────


def test_pre_flight_runs_before_popolad_rpc(
    runner: CliRunner,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (l): the popolad RPC stub MUST NOT be called when class is personal.

    This is the regression guard for operators who wired
    ``popola dispatch --cli=cursor-cloud`` directly: the gate fires
    locally in the CLI before ever reaching ``popolad`` so the daemon
    never logs a half-completed dispatch.
    """
    _write_credentials_toml(isolated_home, "personal")
    rpc_calls: list[dict[str, Any]] = []

    def fake_post(body: dict[str, Any]) -> httpx.Response:
        rpc_calls.append(body)
        return httpx.Response(200, json={"task_id": "should-not-arrive"})

    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda worker_dir: [],
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
        ],
    )
    assert result.exit_code == cloud_worker_cmd._EXIT_ACCOUNT_CLASS_GATE
    assert rpc_calls == [], (
        "pre-flight gate must short-circuit BEFORE the popolad RPC "
        f"(got {len(rpc_calls)} unexpected calls)"
    )
