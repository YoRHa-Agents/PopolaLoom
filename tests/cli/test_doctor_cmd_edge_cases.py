"""Edge-case tests for ``popola doctor`` (v0.5.4 Loop 4 — L4.C).

Per release-notes-v0.5.4.md L4.C: the round-4 mutation-surface expansion
adds ``cli/doctor_cmd.py`` to ``[tool.mutmut].paths_to_mutate``. This
test file targets branches the live mutmut run would prod first:

1. ``_probe_daemon`` success path (line 254 — currently uncovered: the
   existing tests stub ``_probe_daemon`` directly via monkeypatch
   instead of letting it run end-to-end).
2. ``--strict`` with all subsystems FAIL → exit 1 + summary line shows
   the FAIL count (red render path, line 475-476).
3. ``--strict`` with WARN ONLY → exit 0 (already covered, but pin this
   contract again from a different angle for the JSON schema view).
4. ``--json`` envelope schema STABILITY: the 5 top-level keys (skill /
   daemon / lark / arktower / summary) and the ``summary.verdicts``
   sub-keys (4 keys) MUST remain stable so external consumers can
   parse the output reliably.
5. Daemon RUNNING but body returns missing optional fields →
   ``_probe_daemon`` still returns ``ok=True`` with ``daemon_pid=None``
   (defensive default).
6. ``_render_terminal`` with summary_style transitions (FAIL → red,
   WARN → yellow, OK → green).
7. ``_roll_up`` on mixed statuses returns the WORST level (verdict
   monotonicity contract).
8. ``_audit_arktower`` reports OK when both module imports + both
   migration files exist (positive control for the negative
   ``test_doctor_arktower_audit_warns_when_migrations_missing``).
9. ``_audit_lark`` notify OFF detail string is exactly ``"off"`` when
   target_open_id is set but notify_on_completed != "1" (locks in the
   off/on enum literal — mutating it would survive a less-strict
   match).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

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


# ── _probe_daemon happy path (line 254) ─────────────────────────────────


def test_probe_daemon_success_returns_ok_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_probe_daemon`` end-to-end success path (line 254-260).

    Pre-existing tests stub ``_probe_daemon`` itself via monkeypatch;
    none of them exercise the real return-statement when status_code is
    200 + body is valid JSON. Mutating the assignment of any of the 4
    body keys (``daemon_pid`` / ``uptime_seconds`` / ``active_tasks`` /
    ``version``) would otherwise survive.
    """
    from popolaloom.cli.doctor_cmd import _probe_daemon

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "daemon_pid": 4242,
        "uptime_seconds": 7200,
        "active_tasks": 3,
        "version": "0.5.4",
    }

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
    assert out["ok"] is True
    assert out["daemon_pid"] == 4242
    assert out["uptime_seconds"] == 7200
    assert out["active_tasks"] == 3
    assert out["version"] == "0.5.4"


def test_probe_daemon_success_with_missing_optional_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When ``/probe`` returns 200 + an empty body, ``_probe_daemon`` still
    returns ``ok=True`` with all four fields defaulted to ``None`` (line
    256-260 ``.get`` defensive lookups).
    """
    from popolaloom.cli.doctor_cmd import _probe_daemon

    fake_response = MagicMock()
    fake_response.status_code = 200
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
    assert out["ok"] is True
    assert out["daemon_pid"] is None
    assert out["uptime_seconds"] is None
    assert out["active_tasks"] is None
    assert out["version"] is None


# ── --strict + all-FAIL summary path ────────────────────────────────────


def test_doctor_strict_summary_red_when_fail(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola doctor --strict`` exits 1 AND prints the summary table
    when at least one subsystem reports FAIL (lines 475-476 red branch
    + line 558 strict gate).

    Daemon FAIL is the canonical case (no popolad running on a fresh
    dev box).
    """
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda _: None,
    )
    result = runner.invoke(app, ["doctor", "--strict"])
    assert result.exit_code == 1
    out = _combined(result)
    assert "Summary" in out
    assert "FAIL" in out


# ── --json envelope schema stability ────────────────────────────────────


def test_doctor_json_summary_has_stable_top_level_keys(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json`` envelope MUST always include the 5 top-level keys
    (``skill``, ``daemon``, ``lark``, ``arktower``, ``summary``); the
    summary MUST always include the 3 counts (``fail`` / ``warn`` /
    ``drift``) and ``verdicts`` MUST have all 4 subsystem keys.

    This is the contract external consumers (devops scripts, CI gate
    parsers) rely on; mutating any key string in ``_render_json``
    would otherwise survive.
    """
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda _: None,
    )
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(_combined(result).strip().splitlines()[-1])

    assert set(payload.keys()) >= {
        "skill",
        "daemon",
        "lark",
        "arktower",
        "summary",
    }, f"missing top-level key: payload keys = {list(payload.keys())}"

    summary = payload["summary"]
    assert "fail" in summary
    assert "warn" in summary
    assert "drift" in summary
    assert "verdicts" in summary

    verdicts = summary["verdicts"]
    assert set(verdicts.keys()) == {
        "skill",
        "daemon",
        "lark",
        "arktower",
        "preferences",
    }, (
        f"verdicts must have exactly the 5 subsystem keys; got {list(verdicts.keys())}"
    )


def test_doctor_json_check_row_schema(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every row in ``skill`` / ``lark`` / ``arktower`` lists has the 4
    canonical keys (name / target / status / detail).

    The ``daemon`` slot is a single dict (not a list) but follows the
    same schema. Locks in ``_section_to_jsonable`` against mutations
    that drop one of the 4 keys.
    """
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda _: None,
    )
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(_combined(result).strip().splitlines()[-1])

    canonical_keys = {"name", "target", "status", "detail"}
    for section_key in ("skill", "lark", "arktower"):
        rows = payload[section_key]
        assert isinstance(rows, list)
        for row in rows:
            assert set(row.keys()) == canonical_keys, (
                f"{section_key} row missing keys: {set(row.keys())}"
            )

    daemon_row = payload["daemon"]
    assert set(daemon_row.keys()) == canonical_keys


# ── _roll_up monotonicity ───────────────────────────────────────────────


def test_roll_up_returns_worst_status() -> None:
    """``_roll_up`` returns the WORST severity in the input list.

    Pins the ``_VERDICT_ORDER`` ranking; mutating any score in the
    dict (e.g. flipping FAIL to ``0``) would surface immediately.
    """
    from popolaloom.cli.doctor_cmd import _AuditCheck, _roll_up

    def _mk(status: str) -> _AuditCheck:
        return _AuditCheck(name="t", target="t", status=status)

    assert _roll_up([_mk("OK"), _mk("OK")]) == "OK"
    assert _roll_up([_mk("OK"), _mk("WARN")]) == "WARN"
    assert _roll_up([_mk("OK"), _mk("DRIFT")]) == "DRIFT"
    assert _roll_up([_mk("WARN"), _mk("FAIL")]) == "FAIL"
    assert _roll_up([_mk("DRIFT"), _mk("MISS")]) == "MISS"
    assert _roll_up([_mk("OK"), _mk("FAIL"), _mk("WARN")]) == "FAIL"


def test_roll_up_off_demotes_to_ok() -> None:
    """``_roll_up`` flattens an OFF-only section to OK (line 134).

    OFF is informational ("not installed; not an error"); the doctor
    convention is that an OFF-only section reports OK overall so the
    aggregate-strict contract isn't tripped by an absent dependency.
    """
    from popolaloom.cli.doctor_cmd import _AuditCheck, _roll_up

    assert _roll_up([_AuditCheck(name="x", target="x", status="OFF")]) == "OK"


# ── doctor render path coloring ─────────────────────────────────────────


def test_doctor_render_terminal_summary_red_path_coverage(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The terminal renderer prints the summary in red style when
    ``aggregate.fail_count > 0``. We can't easily intercept Rich style
    objects from the CliRunner stdout, but we CAN assert the FAIL +
    summary text appear together on the same render pass.

    Pins the ``summary_style = "red"`` branch (line 476).
    """
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda _: None,
    )
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    out = _combined(result)
    assert "FAIL" in out
    summary_idx = out.find("Summary")
    assert summary_idx > 0
    summary_text = out[summary_idx:]
    assert "FAIL" in summary_text


# ── arktower OK happy path ──────────────────────────────────────────────


def test_doctor_arktower_audit_ok_when_migrations_present(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ArkTower migration row reports OK when both 005 + 006 SQL files
    exist on disk (positive control for
    ``test_doctor_arktower_audit_warns_when_migrations_missing``).

    Asserts:
    - module row OK
    - 005 mig row OK
    - 006 mig row OK
    """
    fake_mig_dir = isolated_env / "fake_migrations"
    fake_mig_dir.mkdir()
    (fake_mig_dir / "005_popolaloom_extensions.sql").write_text(
        "-- placeholder\n", encoding="utf-8"
    )
    (fake_mig_dir / "006_popola_hitl.sql").write_text(
        "-- placeholder\n", encoding="utf-8"
    )

    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda _: None,
    )
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd._popolaloom_migrations_dir",
        lambda: fake_mig_dir,
    )

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(_combined(result).strip().splitlines()[-1])

    arktower_rows = payload["arktower"]
    module_row = next(r for r in arktower_rows if r["name"] == "module")
    assert module_row["status"] == "OK"
    mig_rows = [r for r in arktower_rows if r["name"].endswith("mig")]
    assert all(r["status"] == "OK" for r in mig_rows), (
        f"all mig rows should be OK; got {mig_rows}"
    )
    assert payload["summary"]["verdicts"]["arktower"] == "OK"


# ── lark notify off literal pinning ─────────────────────────────────────


def test_doctor_lark_notify_off_literal_when_not_one(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``LARK_NOTIFY_ON_COMPLETED`` set to anything other than exactly
    ``"1"`` (e.g. ``"true"``, ``"yes"``, ``"0"``) reports OFF.

    Locks in the strict-equality check on line 319-320.
    """
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda name: "/usr/local/bin/lark-cli" if name == "lark-cli" else None,
    )
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_test")
    monkeypatch.setenv("LARK_NOTIFY_ON_COMPLETED", "true")

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    notify_row = next(r for r in payload["lark"] if r["name"] == "notify")
    assert notify_row["status"] == "OFF"
    assert notify_row["detail"] == "off"


def test_doctor_lark_notify_on_literal_exact_match(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``LARK_NOTIFY_ON_COMPLETED=1`` (exact) reports OK + detail ``"on"``.

    Twin to ``test_doctor_lark_notify_off_literal_when_not_one``;
    together they pin the ``"1" → "on"`` / ``else → "off"`` branch.
    """
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda name: "/usr/local/bin/lark-cli" if name == "lark-cli" else None,
    )
    monkeypatch.setenv("LARK_HITL_TARGET_OPEN_ID", "ou_test")
    monkeypatch.setenv("LARK_NOTIFY_ON_COMPLETED", "1")

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    notify_row = next(r for r in payload["lark"] if r["name"] == "notify")
    assert notify_row["status"] == "OK"
    assert notify_row["detail"] == "on"


# ── DoctorAggregate field invariants ────────────────────────────────────


def test_doctor_aggregate_counts_summed_correctly(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``collect_doctor_aggregate`` counts FAIL / WARN / DRIFT correctly
    across all 4 sections (line 426-428 sum-comprehensions).

    Force a specific outcome: FAIL daemon + WARN lark + missing skills
    (which give MISS not WARN per check_skill_health).
    """
    from popolaloom.cli.doctor_cmd import collect_doctor_aggregate

    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda name: "/usr/local/bin/lark-cli" if name == "lark-cli" else None,
    )
    monkeypatch.delenv("LARK_HITL_TARGET_OPEN_ID", raising=False)

    aggregate = collect_doctor_aggregate()
    assert aggregate.fail_count >= 1
    assert aggregate.warn_count >= 1
    assert aggregate.fail_count + aggregate.warn_count + aggregate.drift_count >= 2
    assert aggregate.daemon.verdict == "FAIL"
    assert aggregate.lark.verdict == "WARN"


# ── verdict roll-up sanity for daemon section ───────────────────────────


def test_doctor_daemon_verdict_fail_when_socket_missing(
    isolated_env: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the popolad UDS socket is absent, daemon verdict is FAIL.

    This is the canonical "no daemon running on dev box" path; the
    detail line should mention "popolad not running" (line 184-186).
    """
    monkeypatch.setattr(
        "popolaloom.cli.doctor_cmd.shutil.which",
        lambda _: None,
    )
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    assert payload["daemon"]["status"] == "FAIL"
    assert "not running" in payload["daemon"]["detail"]
