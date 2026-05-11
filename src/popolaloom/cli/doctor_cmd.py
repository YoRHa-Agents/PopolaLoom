"""``popola doctor`` — aggregated health probe (v0.5.0 Stage S4).

Per [v0.5.0-plan.md §4 Stage S4.E](../../../.local/memory/specs/popolaloom/v0.5.0-plan.md):

A single Typer verb that aggregates four subsystem audits:

1. **Skill audit** — runs
   :func:`popolaloom.evolution.skill_doctor.check_skill_health` for every
   target in :data:`SKILL_TARGETS`; PASS when all detected installs have
   the right frontmatter version, WARN when one drifts, FAIL when the
   default-detected install is missing entirely.
2. **Daemon audit** — pokes ``/probe`` over the ``popolad`` UDS socket
   (the same code path as ``popola probe`` in
   :mod:`popolaloom.cli.main`); PASS when the daemon is reachable.
3. **lark-cli audit** — checks
   :func:`popolaloom.lark.is_lark_runtime_available` plus the
   ``LARK_HITL_TARGET_OPEN_ID`` env var; PASS when both are present,
   WARN when the binary exists but the env is unset, OFF when the
   binary is missing entirely (informational, not a fail).
4. **ArkTower audit** — verifies that the vendored
   :mod:`popolaloom._vendored.arktower` package imports cleanly and
   that the two PopolaLoom migration files
   ``005_popolaloom_extensions.sql`` /  ``006_popola_hitl.sql`` are on
   disk; PASS when all three checks pass, WARN when migrations are
   missing (the daemon falls back to a no-op runner per
   :mod:`popolaloom.daemon.repository`).

Exit-code contract (per plan §S4.E):

* ``0`` when no subsystem reports FAIL, OR when ``--strict`` is unset.
* ``1`` when at least one subsystem reports FAIL **and** ``--strict``
  is set.

Output formats:

* Default (terminal): one section per subsystem, fixed-width columns
  matching the plan §S4.E example layout.
* ``--json``: a 4-key envelope ``{"skill": [...], "daemon": {...},
  "lark": {...}, "arktower": [...]}`` for programmatic consumers.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.text import Text

from popolaloom import __version__
from popolaloom.evolution.skill_doctor import DoctorReport, check_skill_health

__all__ = ["doctor_command"]


_console_out = Console()


# ── result tally + status enum ──────────────────────────────────────────


@dataclass(frozen=True)
class _AuditCheck:
    """One row of an audit section's table output.

    Attributes:
        name:    Short row identifier (e.g. ``socket``, ``005 mig``).
        target:  Path / module / env-key the check refers to.
        status:  ``OK`` / ``WARN`` / ``DRIFT`` / ``MISS`` / ``OFF`` /
                 ``FAIL`` — six-state tally aligned with plan §S4.E.
        detail:  Free-form extra context (e.g. ``pid=12345``,
                 ``v0.4.1 (expected v0.5.0)``).
    """

    name: str
    target: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class _AuditSection:
    """Aggregate of an audit subsystem's checks + worst-case verdict."""

    name: str
    checks: list[_AuditCheck]
    verdict: str
    summary: str = ""


@dataclass(frozen=True)
class DoctorAggregate:
    """Top-level doctor output: subsystems + their roll-up tally."""

    skill: _AuditSection
    daemon: _AuditSection
    lark: _AuditSection
    arktower: _AuditSection
    preferences: _AuditSection
    fail_count: int = 0
    warn_count: int = 0
    drift_count: int = 0
    raw_skill_reports: list[DoctorReport] = field(default_factory=list)


# ── verdict roll-up ─────────────────────────────────────────────────────


_VERDICT_ORDER: dict[str, int] = {
    "OK": 0,
    "OFF": 0,
    "WARN": 1,
    "DRIFT": 1,
    "MISS": 2,
    "FAIL": 2,
}


def _roll_up(checks: list[_AuditCheck]) -> str:
    """Roll up a list of check statuses into a single section verdict."""
    worst = "OK"
    worst_score = _VERDICT_ORDER["OK"]
    for check in checks:
        score = _VERDICT_ORDER.get(check.status, 2)
        if score > worst_score:
            worst = check.status
            worst_score = score
    return "OK" if worst in {"OFF"} else worst


# ── 1. skill audit ──────────────────────────────────────────────────────


def _audit_skill() -> tuple[_AuditSection, list[DoctorReport]]:
    """Run :func:`check_skill_health` and translate to :class:`_AuditCheck` rows."""
    reports = check_skill_health()
    checks: list[_AuditCheck] = []
    for report in reports:
        if not report.exists:
            status = "MISS"
            detail = f"expected v{__version__}"
        elif report.drift:
            status = "DRIFT"
            detail = f"v{report.version} (expected v{__version__})"
        else:
            status = "OK"
            detail = f"v{report.version or __version__}"
        label = f"{report.target} {report.scope}"
        checks.append(
            _AuditCheck(
                name=label,
                target=str(report.expected_path),
                status=status,
                detail=detail,
            )
        )
    verdict = _roll_up(checks)
    return (
        _AuditSection(
            name="Skill audit",
            checks=checks,
            verdict=verdict,
            summary=f"{len(reports)} (target × scope) slots checked",
        ),
        reports,
    )


# ── 2. daemon audit ─────────────────────────────────────────────────────


def _audit_daemon() -> _AuditSection:
    """Probe the popolad UDS socket; PASS if ``/probe`` returns 200."""
    socket_path = _resolve_socket_path()
    detail = ""
    status = "OK"

    if not socket_path.exists():
        status = "FAIL"
        detail = "popolad not running"
    else:
        probe = _probe_daemon(socket_path)
        if probe.get("ok"):
            pid = probe.get("daemon_pid")
            uptime = probe.get("uptime_seconds")
            detail = f"pid={pid} uptime={uptime}s" if pid is not None else "reachable"
            status = "OK"
        else:
            status = "FAIL"
            detail = probe.get("error", "probe failed")

    check = _AuditCheck(
        name="socket",
        target=str(socket_path),
        status=status,
        detail=detail,
    )
    return _AuditSection(
        name="Daemon audit",
        checks=[check],
        verdict=status,
        summary="popolad UDS reachability",
    )


def _resolve_socket_path() -> Path:
    """Resolve ``$POPOLA_HOME/popolad.sock`` (default ``~/.popola/popolad.sock``).

    Mirrors :func:`popolaloom.cli.main._socket_path` so the doctor verb
    talks to the same daemon as ``popola probe``.  Inlined (rather than
    imported) to avoid pulling :mod:`httpx` / :mod:`typer` setup cost
    of ``cli.main`` onto the doctor verb's startup path.
    """
    home = os.environ.get("POPOLA_HOME")
    base = Path(home).expanduser().resolve() if home else Path.home() / ".popola"
    return base / "popolad.sock"


def _probe_daemon(socket_path: Path) -> dict[str, Any]:
    """Issue ``GET /probe`` over the daemon's UDS socket.

    Returns a small status dict; never raises — failures are flattened
    into ``{"ok": False, "error": "<reason>"}`` so the audit can render
    a uniform FAIL row instead of crashing.
    """
    transport = httpx.HTTPTransport(uds=str(socket_path))
    try:
        with httpx.Client(
            transport=transport,
            base_url="http://popolad",
            timeout=httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0),
        ) as client:
            response = client.get("/probe")
    except httpx.ConnectError as exc:
        return {"ok": False, "error": f"connect failed: {exc!r}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"http error: {exc!r}"}
    except OSError as exc:
        return {"ok": False, "error": f"os error: {exc!r}"}

    if response.status_code != 200:
        return {"ok": False, "error": f"status {response.status_code}"}
    body: Mapping[str, Any]
    try:
        body = response.json()
    except ValueError:
        return {"ok": False, "error": "non-JSON response"}
    return {
        "ok": True,
        "daemon_pid": body.get("daemon_pid"),
        "uptime_seconds": body.get("uptime_seconds"),
        "active_tasks": body.get("active_tasks"),
        "version": body.get("version"),
    }


# ── 3. lark audit ──────────────────────────────────────────────────────


def _audit_lark() -> _AuditSection:
    """Detect ``lark-cli`` binary + the ``LARK_HITL_TARGET_OPEN_ID`` env."""
    binary = shutil.which("lark-cli")
    target_open_id = os.environ.get("LARK_HITL_TARGET_OPEN_ID", "").strip()
    notify_on_completed = os.environ.get("LARK_NOTIFY_ON_COMPLETED", "").strip()

    checks: list[_AuditCheck] = []
    if binary is None:
        checks.append(
            _AuditCheck(
                name="lark-cli",
                target="(not in PATH)",
                status="OFF",
                detail="binary not installed",
            )
        )
        checks.append(
            _AuditCheck(
                name="notify",
                target="LARK_NOTIFY_ON_COMPLETED",
                status="OFF",
                detail="lark binary missing",
            )
        )
        return _AuditSection(
            name="Lark audit",
            checks=checks,
            verdict="OK",
            summary="lark-cli not installed (informational)",
        )

    if not target_open_id:
        checks.append(
            _AuditCheck(
                name="lark-cli",
                target=binary,
                status="WARN",
                detail="LARK_HITL_TARGET_OPEN_ID unset",
            )
        )
    else:
        checks.append(
            _AuditCheck(
                name="lark-cli",
                target=binary,
                status="OK",
                detail=f"target={target_open_id}",
            )
        )

    if not target_open_id:
        notify_status = "OFF"
        notify_detail = "depends on LARK_HITL_TARGET_OPEN_ID"
    elif notify_on_completed == "1":
        notify_status = "OK"
        notify_detail = "on"
    else:
        notify_status = "OFF"
        notify_detail = "off"
    checks.append(
        _AuditCheck(
            name="notify",
            target="LARK_NOTIFY_ON_COMPLETED",
            status=notify_status,
            detail=notify_detail,
        )
    )

    summary = (
        "lark-cli present, configured"
        if target_open_id
        else "lark-cli present, not configured"
    )
    return _AuditSection(
        name="Lark audit",
        checks=checks,
        verdict=_roll_up(checks),
        summary=summary,
    )


# ── 4. arktower audit ───────────────────────────────────────────────────


def _audit_arktower() -> _AuditSection:
    """Verify the vendored ArkTower module imports + 005/006 migrations exist."""
    checks: list[_AuditCheck] = []

    module_status = "OK"
    module_detail = "importable"
    try:
        from popolaloom._vendored import arktower  # noqa: F401
    except ImportError as exc:
        module_status = "FAIL"
        module_detail = f"import failed: {exc!r}"
    checks.append(
        _AuditCheck(
            name="module",
            target="popolaloom._vendored.arktower",
            status=module_status,
            detail=module_detail,
        )
    )

    popola_dir = _popolaloom_migrations_dir()
    for filename in ("005_popolaloom_extensions.sql", "006_popola_hitl.sql"):
        path = popola_dir / filename
        if path.is_file():
            row = _AuditCheck(
                name=f"{filename[:3]} mig",
                target=str(path),
                status="OK",
                detail="present",
            )
        else:
            row = _AuditCheck(
                name=f"{filename[:3]} mig",
                target=str(path),
                status="WARN",
                detail="missing (daemon falls back to no-op migration runner)",
            )
        checks.append(row)

    return _AuditSection(
        name="ArkTower audit",
        checks=checks,
        verdict=_roll_up(checks),
        summary="vendored module + PopolaLoom migrations",
    )


def _popolaloom_migrations_dir() -> Path:
    """Locate the PopolaLoom migrations directory.

    Mirrors :func:`popolaloom.daemon.repository._popolaloom_migrations_dir`
    so ``popola doctor`` reports drift identically to what the daemon's
    runtime would observe.  When the package is installed via wheel
    (which doesn't ship ``migrations/``), this points at a non-existent
    path and the audit downgrades to WARN — not FAIL — to match the
    daemon's "no-op when missing" behaviour.
    """
    return Path(__file__).resolve().parents[3] / "migrations"


# ── 5. user preferences audit ──────────────────────────────────────────


def _audit_user_preferences() -> _AuditSection:
    """Validate the optional ``[user_preferences]`` schema."""
    home = os.environ.get("POPOLA_HOME")
    config_path = (
        Path(home).expanduser().resolve() if home else Path.home() / ".popola"
    ) / "popolad.toml"
    if not config_path.exists():
        check = _AuditCheck(
            name="schema",
            target=str(config_path),
            status="OFF",
            detail="popolad.toml absent",
        )
        return _AuditSection(
            name="User preferences schema",
            checks=[check],
            verdict="OK",
            summary="no preferences file configured",
        )

    try:
        import tomllib

        from popolaloom.daemon.main import _load_user_preferences

        with config_path.open("rb") as fp:
            raw = tomllib.load(fp)
        prefs = _load_user_preferences(raw, source=config_path)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        check = _AuditCheck(
            name="schema",
            target=str(config_path),
            status="FAIL",
            detail=f"invalid: {exc}",
        )
        return _AuditSection(
            name="User preferences schema",
            checks=[check],
            verdict="FAIL",
            summary="invalid user_preferences",
        )

    if prefs is None:
        detail = "not configured"
        status = "OFF"
    elif getattr(prefs, "schema_version", 1) >= 2:
        detail = "v2 nested"
        status = "OK"
    else:
        detail = "v1 flat (will migrate on write)"
        status = "WARN"
    check = _AuditCheck(
        name="schema",
        target=str(config_path),
        status=status,
        detail=detail,
    )
    return _AuditSection(
        name="User preferences schema",
        checks=[check],
        verdict=_roll_up([check]),
        summary=detail,
    )


# ── aggregator ─────────────────────────────────────────────────────────


def collect_doctor_aggregate() -> DoctorAggregate:
    """Run all four audits and return a :class:`DoctorAggregate`.

    Pulled out of the Typer verb so library callers (tests, the ``popola
    skill doctor`` JSON path, future automation) can re-use the
    aggregator without reaching for the Typer ``CliRunner``.
    """
    skill_section, raw_reports = _audit_skill()
    daemon_section = _audit_daemon()
    lark_section = _audit_lark()
    arktower_section = _audit_arktower()
    preferences_section = _audit_user_preferences()

    sections = (
        skill_section,
        daemon_section,
        lark_section,
        arktower_section,
        preferences_section,
    )
    fail = sum(1 for s in sections for c in s.checks if c.status == "FAIL")
    warn = sum(1 for s in sections for c in s.checks if c.status == "WARN")
    drift = sum(1 for s in sections for c in s.checks if c.status == "DRIFT")

    return DoctorAggregate(
        skill=skill_section,
        daemon=daemon_section,
        lark=lark_section,
        arktower=arktower_section,
        preferences=preferences_section,
        fail_count=fail,
        warn_count=warn,
        drift_count=drift,
        raw_skill_reports=raw_reports,
    )


# ── rendering helpers ──────────────────────────────────────────────────


_STATUS_STYLES: dict[str, str] = {
    "OK": "green",
    "WARN": "yellow",
    "DRIFT": "yellow",
    "MISS": "red",
    "FAIL": "red",
    "OFF": "dim",
}


def _render_terminal(aggregate: DoctorAggregate) -> None:
    """Render the doctor aggregate as a multi-section terminal report."""
    _console_out.print(Text("PopolaLoom Doctor Report", style="bold"))

    for section in (
        aggregate.skill,
        aggregate.daemon,
        aggregate.lark,
        aggregate.arktower,
        aggregate.preferences,
    ):
        _console_out.print(Text(f"\n{section.name}", style="bold"))
        for check in section.checks:
            style = _STATUS_STYLES.get(check.status, "")
            line = (
                f"  {check.name:<8} {check.target:<60} "
                f"{check.status:<6} {check.detail}"
            )
            _console_out.print(Text(line, style=style))

    summary_style = "green"
    if aggregate.fail_count:
        summary_style = "red"
    elif aggregate.warn_count or aggregate.drift_count:
        summary_style = "yellow"

    _console_out.print(
        Text(
            "\nSummary: 5/5 subsystems checked. "
            f"{aggregate.warn_count} WARN, "
            f"{aggregate.drift_count} DRIFT, "
            f"{aggregate.fail_count} FAIL.",
            style=summary_style,
        )
    )


def _render_json(aggregate: DoctorAggregate) -> str:
    """Render the doctor aggregate as a 4-key JSON envelope.

    Per acceptance criterion (4): the four top-level keys are
    ``skill`` / ``daemon`` / ``lark`` / ``arktower``.
    """
    payload: dict[str, Any] = {
        "skill": [_section_to_jsonable(c) for c in aggregate.skill.checks],
        "daemon": _section_to_jsonable(
            aggregate.daemon.checks[0]
        )
        if aggregate.daemon.checks
        else {},
        "lark": [_section_to_jsonable(c) for c in aggregate.lark.checks],
        "arktower": [_section_to_jsonable(c) for c in aggregate.arktower.checks],
        "preferences": [
            _section_to_jsonable(c) for c in aggregate.preferences.checks
        ],
        "summary": {
            "fail": aggregate.fail_count,
            "warn": aggregate.warn_count,
            "drift": aggregate.drift_count,
            "verdicts": {
                "skill": aggregate.skill.verdict,
                "daemon": aggregate.daemon.verdict,
                "lark": aggregate.lark.verdict,
                "arktower": aggregate.arktower.verdict,
                "preferences": aggregate.preferences.verdict,
            },
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def _section_to_jsonable(check: _AuditCheck) -> dict[str, Any]:
    """Convert a single :class:`_AuditCheck` to a JSON-friendly dict."""
    payload = asdict(check)
    return {
        "name": payload["name"],
        "target": payload["target"],
        "status": payload["status"],
        "detail": payload["detail"],
    }


# ── Typer-registered command ───────────────────────────────────────────


def doctor_command(
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON instead of the terminal table.",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help=(
            "Exit non-zero (1) when any subsystem reports FAIL. "
            "WARN and DRIFT are informational regardless of this flag."
        ),
    ),
) -> None:
    """Aggregated PopolaLoom health probe (skill + daemon + lark + ArkTower)."""
    aggregate = collect_doctor_aggregate()

    if json_out:
        typer.echo(_render_json(aggregate))
    else:
        _render_terminal(aggregate)

    if strict and aggregate.fail_count > 0:
        raise typer.Exit(code=1)


