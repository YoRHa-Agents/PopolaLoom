"""Default-lane tests for ``popola doctor`` (Stage S4 of v0.5.0).

Six test cases per the v0.5.0-plan §S4.G test contract:

1. ``popola doctor`` prints all four subsystem section headers.
2. ``popola doctor --json`` envelope has the four documented top-level
   keys (acceptance criterion #4).
3. ``popola doctor`` exits 0 even when a subsystem reports FAIL
   (informational mode is the default).
4. ``popola doctor --strict`` exits 1 when any subsystem reports FAIL.
5. lark subsystem flags WARN when ``lark-cli`` is on PATH but the
   ``LARK_HITL_TARGET_OPEN_ID`` env is unset.
6. lark subsystem flags OFF (informational) when ``lark-cli`` is not
   installed at all — exit code stays 0.

All tests run with the popolad daemon socket pointed at a tmp_path
that does NOT contain a live socket, so the daemon subsystem reliably
reports FAIL — that's the canonical "developer machine without a
running daemon" case the doctor was designed for.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from popolaloom.cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Yield a tmp dir + isolated POPOLA_HOME / HOME / lark env."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.chdir(cwd)

    popola_home = tmp_path / "popola_home"
    popola_home.mkdir()
    monkeypatch.setenv("POPOLA_HOME", str(popola_home))

    monkeypatch.delenv("LARK_HITL_TARGET_OPEN_ID", raising=False)
    monkeypatch.delenv("LARK_HITL_ALLOWED_RESPONDERS", raising=False)
    monkeypatch.delenv("LARK_NOTIFY_ON_COMPLETED", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)

    yield tmp_path


def _combined(result: object) -> str:
    """Return ``result.stdout`` + best-effort ``result.stderr`` (click 8.x compat)."""
    stdout = getattr(result, "stdout", "") or ""
    try:
        stderr = getattr(result, "stderr", "") or ""
    except (ValueError, AttributeError):
        stderr = ""
    output = getattr(result, "output", "") or ""
    return stdout + stderr + output


def test_doctor_prints_all_four_subsystem_sections(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola doctor`` lists Skill / Daemon / Lark / ArkTower sections (acceptance #2)."""
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda _: None,
    )
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "Skill audit" in out
    assert "Daemon audit" in out
    assert "Lark audit" in out
    assert "ArkTower audit" in out
    assert "Summary" in out


def test_doctor_json_has_four_top_level_keys(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola doctor --json`` envelope exposes skill/daemon/lark/arktower (acceptance #4)."""
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda _: None,
    )
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    for key in ("skill", "daemon", "lark", "arktower"):
        assert key in payload, f"missing top-level key {key!r}"
    assert isinstance(payload["skill"], list)
    assert isinstance(payload["daemon"], dict)
    assert isinstance(payload["lark"], list)
    assert isinstance(payload["arktower"], list)


def test_doctor_exits_0_without_strict_even_when_daemon_fails(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola doctor`` (no --strict) exits 0 even when daemon subsystem is FAIL.

    Acceptance criterion #2 + plan §S4.E exit-code contract: WARN /
    DRIFT / FAIL are informational by default.
    """
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda _: None,
    )
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, _combined(result)


def test_doctor_strict_exits_1_when_daemon_fails(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola doctor --strict`` exits 1 when any subsystem reports FAIL."""
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda _: None,
    )
    result = runner.invoke(app, ["doctor", "--strict"])
    assert result.exit_code == 1, _combined(result)


def test_doctor_lark_warns_when_binary_present_but_env_unset(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lark subsystem reports WARN when ``lark-cli`` is on PATH but the env is unset."""
    fake_lark_path = "/usr/local/bin/lark-cli"
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda name: fake_lark_path if name == "lark-cli" else None,
    )
    monkeypatch.delenv("LARK_HITL_TARGET_OPEN_ID", raising=False)
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    lark_rows = payload["lark"]
    binary_row = next(r for r in lark_rows if r["name"] == "lark-cli")
    assert binary_row["status"] == "WARN"
    assert "TARGET_OPEN_ID" in binary_row["detail"]


def test_doctor_lark_off_when_binary_missing(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lark subsystem reports OFF (informational) when ``lark-cli`` is not in PATH."""
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda _: None,
    )
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    lark_rows = payload["lark"]
    binary_row = next(r for r in lark_rows if r["name"] == "lark-cli")
    assert binary_row["status"] == "OFF"


def test_doctor_lark_ok_when_binary_present_and_env_set(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lark subsystem reports OK when ``lark-cli`` and ``LARK_HITL_TARGET_OPEN_ID`` are set."""
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda name: "/usr/local/bin/lark-cli" if name == "lark-cli" else None,
    )
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_test_target_id")
    monkeypatch.setenv("LARK_NOTIFY_ON_COMPLETED", "1")

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    lark_rows = payload["lark"]
    binary_row = next(r for r in lark_rows if r["name"] == "lark-cli")
    notify_row = next(r for r in lark_rows if r["name"] == "notify")
    assert binary_row["status"] == "OK"
    assert "ou_test_target_id" in binary_row["detail"]
    assert notify_row["status"] == "OK"
    assert notify_row["detail"] == "on"


def test_doctor_lark_ok_target_set_but_notify_off(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lark notify row reports OFF when ``LARK_NOTIFY_ON_COMPLETED`` is unset."""
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda name: "/usr/local/bin/lark-cli" if name == "lark-cli" else None,
    )
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_test_target_id")
    monkeypatch.delenv("LARK_NOTIFY_ON_COMPLETED", raising=False)

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    notify_row = next(r for r in payload["lark"] if r["name"] == "notify")
    assert notify_row["status"] == "OFF"
    assert notify_row["detail"] == "off"


def test_doctor_skill_ok_when_skill_md_matches_version(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skill subsystem reports OK rows for SKILL.md whose frontmatter matches the wheel."""
    from popolaloom import __version__

    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda _: None,
    )
    cwd = isolated_env / "project"
    target = cwd / ".github" / "copilot-instructions.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"---\nname: popola-loom\nversion: {__version__}\n---\nbody\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    copilot_row = next(
        row for row in payload["skill"] if row["name"] == "copilot project"
    )
    assert copilot_row["status"] == "OK"


def test_doctor_daemon_ok_when_probe_succeeds(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon subsystem reports OK when the probe returns 200 + JSON body.

    Patches both the socket-existence check and the ``_probe_daemon``
    helper so the test doesn't need a real popolad UDS.
    """
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda _: None,
    )

    fake_socket_path = isolated_env / "fake-popolad.sock"
    fake_socket_path.write_text("")
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd._resolve_socket_path",
        lambda: fake_socket_path,
    )
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd._probe_daemon",
        lambda _: {
            "ok": True,
            "daemon_pid": 12345,
            "uptime_seconds": 42,
            "active_tasks": 0,
            "version": "0.4.1",
        },
    )

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    assert payload["daemon"]["status"] == "OK"
    assert "pid=12345" in payload["daemon"]["detail"]


def test_doctor_aggregate_summary_counts(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The terminal report's Summary line reports the WARN / DRIFT / FAIL counts."""
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda name: "/usr/local/bin/lark-cli" if name == "lark-cli" else None,
    )
    monkeypatch.delenv("LARK_HITL_TARGET_OPEN_ID", raising=False)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "Summary" in out
    assert "WARN" in out
    assert "FAIL" in out


def test_doctor_strict_passes_when_no_failures(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola doctor --strict`` exits 0 when no subsystem reports FAIL.

    Stubs the daemon probe to return success, lark binary missing
    (informational OFF), and pre-installs a matching SKILL.md so every
    audit returns OK / OFF only.  Drift-free + fail-free.
    """
    from popolaloom import __version__

    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda _: None,
    )

    fake_socket_path = isolated_env / "fake-popolad.sock"
    fake_socket_path.write_text("")
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd._resolve_socket_path",
        lambda: fake_socket_path,
    )
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd._probe_daemon",
        lambda _: {"ok": True, "daemon_pid": 1, "uptime_seconds": 1},
    )

    cwd = isolated_env / "project"
    fake_home = isolated_env / "home"
    targets_to_install = [
        cwd / ".cursor" / "skills" / "popola-loom" / "SKILL.md",
        cwd / ".claude" / "skills" / "popola-loom" / "SKILL.md",
        fake_home / ".cursor" / "skills" / "popola-loom" / "SKILL.md",
        fake_home / ".claude" / "skills" / "popola-loom" / "SKILL.md",
        fake_home / ".codex" / "skills" / "popola-loom" / "SKILL.md",
        cwd / ".github" / "copilot-instructions.md",
    ]
    body = (
        f"---\nname: popola-loom\nversion: {__version__}\n"
        'description: "ok fixture"\n---\nbody\n'
    )
    for target in targets_to_install:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

    result = runner.invoke(app, ["doctor", "--strict"])
    assert result.exit_code == 0, _combined(result)


# ── v0.5.1 coverage gap-fillers (per release-notes-v0.5.1.md L1.B) ──────


def test_doctor_skill_drift_detected_when_frontmatter_lags(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skill subsystem reports DRIFT when frontmatter version != installed __version__.

    Covers ``cli/doctor_cmd.py`` lines 149-150 (the ``elif report.drift``
    branch) — previously only the OK + MISS branches were exercised.
    """
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda _: None,
    )
    cwd = isolated_env / "project"
    target = cwd / ".github" / "copilot-instructions.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\nname: popola-loom\nversion: 0.0.0-stale\n---\nbody\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    drift_rows = [row for row in payload["skill"] if row["status"] == "DRIFT"]
    assert drift_rows, f"expected at least one DRIFT row in {payload['skill']}"
    drift_row = next(r for r in drift_rows if r["name"] == "copilot project")
    assert "0.0.0-stale" in drift_row["detail"]


def test_probe_daemon_handles_connect_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_probe_daemon`` surfaces ``httpx.ConnectError`` as ``{"ok": False, error: ...}``.

    Covers ``cli/doctor_cmd.py`` lines 240-241.
    """
    from popolaloom.cli.doctor_cmd import _probe_daemon

    class _StubClient:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def __enter__(self) -> _StubClient:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def get(self, _url: str) -> object:
            raise httpx.ConnectError("conn refused")

    monkeypatch.setattr("popolaloom.cli.doctor_cmd.httpx.Client", _StubClient)
    out = _probe_daemon(tmp_path / "fake.sock")
    assert out["ok"] is False
    assert "connect failed" in out["error"]


def test_probe_daemon_handles_http_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_probe_daemon`` surfaces ``httpx.HTTPError`` (non-connect) as ``ok: False``.

    Covers ``cli/doctor_cmd.py`` lines 242-243.
    """
    from popolaloom.cli.doctor_cmd import _probe_daemon

    class _StubClient:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def __enter__(self) -> _StubClient:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def get(self, _url: str) -> object:
            raise httpx.ReadTimeout("read timeout")

    monkeypatch.setattr("popolaloom.cli.doctor_cmd.httpx.Client", _StubClient)
    out = _probe_daemon(tmp_path / "fake.sock")
    assert out["ok"] is False
    assert "http error" in out["error"]


def test_probe_daemon_handles_os_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_probe_daemon`` surfaces ``OSError`` as ``ok: False``.

    Covers ``cli/doctor_cmd.py`` lines 244-245.
    """
    from popolaloom.cli.doctor_cmd import _probe_daemon

    class _StubClient:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def __enter__(self) -> _StubClient:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def get(self, _url: str) -> object:
            raise OSError("socket dead")

    monkeypatch.setattr("popolaloom.cli.doctor_cmd.httpx.Client", _StubClient)
    out = _probe_daemon(tmp_path / "fake.sock")
    assert out["ok"] is False
    assert "os error" in out["error"]


def test_probe_daemon_handles_non_200_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_probe_daemon`` reports ``ok: False`` when ``/probe`` returns non-200.

    Covers ``cli/doctor_cmd.py`` lines 247-248.
    """
    from unittest.mock import MagicMock

    from popolaloom.cli.doctor_cmd import _probe_daemon

    fake_response = MagicMock()
    fake_response.status_code = 503
    fake_response.json.return_value = {}

    class _StubClient:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def __enter__(self) -> _StubClient:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def get(self, _url: str) -> object:
            return fake_response

    monkeypatch.setattr("popolaloom.cli.doctor_cmd.httpx.Client", _StubClient)
    out = _probe_daemon(tmp_path / "fake.sock")
    assert out["ok"] is False
    assert "status 503" in out["error"]


def test_probe_daemon_handles_non_json_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_probe_daemon`` reports ``ok: False`` when ``/probe`` body is non-JSON.

    Covers ``cli/doctor_cmd.py`` lines 250-253.
    """
    from unittest.mock import MagicMock

    from popolaloom.cli.doctor_cmd import _probe_daemon

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.side_effect = ValueError("not JSON")

    class _StubClient:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def __enter__(self) -> _StubClient:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def get(self, _url: str) -> object:
            return fake_response

    monkeypatch.setattr("popolaloom.cli.doctor_cmd.httpx.Client", _StubClient)
    out = _probe_daemon(tmp_path / "fake.sock")
    assert out["ok"] is False
    assert "non-JSON" in out["error"]


def test_doctor_daemon_section_renders_probe_failure_detail(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon FAIL row carries ``probe.error`` text in ``detail`` (line 196)."""
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda _: None,
    )
    fake_socket_path = isolated_env / "fake-popolad.sock"
    fake_socket_path.write_text("")
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd._resolve_socket_path",
        lambda: fake_socket_path,
    )
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd._probe_daemon",
        lambda _: {"ok": False, "error": "synthetic probe failure"},
    )

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    assert payload["daemon"]["status"] == "FAIL"
    assert "synthetic probe failure" in payload["daemon"]["detail"]


def test_doctor_arktower_audit_warns_when_migrations_missing(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ArkTower migration row reports WARN when the SQL files are absent.

    Covers ``cli/doctor_cmd.py`` line 381 (the ``else: WARN`` branch
    when neither 005 nor 006 migration is on disk).
    """
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda _: None,
    )
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd._popolaloom_migrations_dir",
        lambda: isolated_env / "no_migrations_here",
    )

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    arktower_rows = payload["arktower"]
    mig_rows = [row for row in arktower_rows if row["name"].endswith("mig")]
    assert mig_rows, "expected migration rows in arktower audit"
    assert all(row["status"] == "WARN" for row in mig_rows), (
        f"all migration rows should be WARN: {mig_rows}"
    )


def test_doctor_arktower_audit_reports_import_failure(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_audit_arktower`` returns FAIL row when the vendored module won't import.

    Covers ``cli/doctor_cmd.py`` lines 358-360 (the ``except ImportError``
    branch).  We hijack ``builtins.__import__`` for the duration of the
    helper call so the inline ``from popolaloom._vendored import
    arktower`` raises — direct unit invocation avoids re-running the
    Typer app under ``CliRunner``, which is what makes this test
    reproducible across Python 3.11 / 3.12.
    """
    import builtins

    from popolaloom.cli.doctor_cmd import _audit_arktower

    real_import = builtins.__import__

    def _selective_import(name: str, *args: object, **kwargs: object) -> object:
        fromlist = ()
        if "fromlist" in kwargs:
            fromlist = tuple(kwargs.get("fromlist") or ())
        elif len(args) >= 3 and args[2]:
            fromlist = tuple(args[2])
        if name == "popolaloom._vendored" and "arktower" in fromlist:
            raise ImportError("synthetic vendored arktower import failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _selective_import)

    section = _audit_arktower()
    module_row = next(row for row in section.checks if row.name == "module")
    assert module_row.status == "FAIL"
    assert "synthetic vendored arktower import failure" in module_row.detail


def test_doctor_summary_yellow_when_only_warn(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal summary uses yellow style when there's WARN but no FAIL.

    Covers ``cli/doctor_cmd.py`` lines 477-478 (the ``elif
    aggregate.warn_count or aggregate.drift_count`` branch).  Lark
    binary present without env triggers WARN; daemon absent normally
    triggers FAIL — we patch socket success to suppress that so only
    WARN remains.
    """
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda name: "/usr/local/bin/lark-cli" if name == "lark-cli" else None,
    )
    monkeypatch.delenv("LARK_HITL_TARGET_OPEN_ID", raising=False)

    fake_socket_path = isolated_env / "fake-popolad.sock"
    fake_socket_path.write_text("")
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd._resolve_socket_path",
        lambda: fake_socket_path,
    )
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd._probe_daemon",
        lambda _: {"ok": True, "daemon_pid": 99, "uptime_seconds": 1},
    )

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    out = _combined(result)
    assert "WARN" in out
    assert "Summary" in out


def test_doctor_strict_with_only_warn_exits_zero(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola doctor --strict`` only fails on FAIL — WARN is informational.

    Confirms the plan §S4.E exit-code contract: WARN / DRIFT never
    flip the exit code, even with ``--strict``.
    """
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda name: "/usr/local/bin/lark-cli" if name == "lark-cli" else None,
    )
    monkeypatch.delenv("LARK_HITL_TARGET_OPEN_ID", raising=False)

    fake_socket_path = isolated_env / "fake-popolad.sock"
    fake_socket_path.write_text("")
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd._resolve_socket_path",
        lambda: fake_socket_path,
    )
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd._probe_daemon",
        lambda _: {"ok": True, "daemon_pid": 99, "uptime_seconds": 1},
    )

    result = runner.invoke(app, ["doctor", "--strict"])
    assert result.exit_code == 0


def test_collect_doctor_aggregate_returns_dataclass(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``collect_doctor_aggregate()`` returns a fully-populated DoctorAggregate."""
    from popolaloom.cli.doctor_cmd import DoctorAggregate, collect_doctor_aggregate

    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda _: None,
    )

    aggregate = collect_doctor_aggregate()
    assert isinstance(aggregate, DoctorAggregate)
    assert aggregate.skill is not None
    assert aggregate.daemon is not None
    assert aggregate.lark is not None
    assert aggregate.arktower is not None
    assert isinstance(aggregate.fail_count, int)
    assert isinstance(aggregate.warn_count, int)
    assert isinstance(aggregate.drift_count, int)
