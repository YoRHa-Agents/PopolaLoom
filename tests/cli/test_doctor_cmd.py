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
        f"---\nname: popolaloom\nversion: {__version__}\n---\nbody\n",
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
        cwd / ".cursor" / "skills" / "popolaloom" / "SKILL.md",
        cwd / ".claude" / "skills" / "popolaloom" / "SKILL.md",
        fake_home / ".cursor" / "skills" / "popolaloom" / "SKILL.md",
        fake_home / ".claude" / "skills" / "popolaloom" / "SKILL.md",
        fake_home / ".codex" / "skills" / "popolaloom" / "SKILL.md",
        cwd / ".github" / "copilot-instructions.md",
    ]
    body = (
        f"---\nname: popolaloom\nversion: {__version__}\n"
        'description: "ok fixture"\n---\nbody\n'
    )
    for target in targets_to_install:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

    result = runner.invoke(app, ["doctor", "--strict"])
    assert result.exit_code == 0, _combined(result)
