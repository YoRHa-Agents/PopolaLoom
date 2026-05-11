"""v1.1.1 Cloud HITL migration packaging and FAIL-loud startup tests."""

from __future__ import annotations

import subprocess
import sys
import zipfile
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from popolaloom._vendored.arktower.cli.deps import migrations_dir as arktower_migrations_dir
from popolaloom._vendored.arktower.core.event_bus import EventBus
from popolaloom.cli.main import app
from popolaloom.daemon import repository as repository_mod
from popolaloom.daemon.repository import MigrationsMissingError, make_persistence

REQUIRED_MIGRATIONS = {
    "005_popolaloom_extensions.sql",
    "006_popola_hitl.sql",
    "007_popola_hitl_metadata.sql",
}


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_package_resources_list_cloud_hitl_migrations() -> None:
    """``popolaloom.migrations`` exposes all Cloud HITL SQL files."""
    migration_root = repository_mod._popolaloom_migrations_dir()
    found = {path.name for path in migration_root.iterdir() if path.is_file()}

    assert REQUIRED_MIGRATIONS <= found


def test_built_wheel_contains_cloud_hitl_migrations(repo_root: Path, tmp_path: Path) -> None:
    """The wheel payload contains all SQL migrations under ``popolaloom/migrations``."""
    dist_dir = tmp_path / "dist"
    result = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(dist_dir), "."],
        cwd=repo_root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    wheels = sorted(dist_dir.glob("popolaloom-*.whl"))
    assert wheels, f"no wheel produced in {dist_dir}"

    with zipfile.ZipFile(wheels[-1]) as wheel:
        names = set(wheel.namelist())
    for filename in REQUIRED_MIGRATIONS:
        assert f"popolaloom/migrations/{filename}" in names


def test_daemon_startup_missing_packaged_migrations_fails_loud(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing packaged SQL raises a typed error and emits the diagnostic event."""
    missing_dir = tmp_path / "missing_popola_migrations"
    missing_dir.mkdir()

    original_files = repository_mod.resources.files
    monkeypatch.setattr(
        repository_mod.resources,
        "files",
        lambda package: missing_dir
        if package == "popolaloom.migrations"
        else original_files(package),
    )
    bus = EventBus()
    events: list[dict[str, object]] = []
    bus.subscribe("popolad.migrations_missing", events.append)

    with pytest.raises(MigrationsMissingError) as exc_info:
        make_persistence(
            db_path=tmp_path / "popolad.db",
            event_bus=bus,
            arktower_migrations_dir=arktower_migrations_dir(),
        )

    assert exc_info.value.missing == tuple(sorted(REQUIRED_MIGRATIONS))
    assert len(events) == 1
    assert events[0]["missing"] == list(exc_info.value.missing)


def test_doctor_reports_ok_for_packaged_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Normal installs report OK for the packaged migrations audit rows."""
    monkeypatch.setattr("popolaloom.cli.doctor_cmd.shutil.which", lambda _: None)

    result = CliRunner().invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout.strip().splitlines()[-1])

    mig_rows = [row for row in payload["arktower"] if row["name"].endswith("mig")]
    assert len(mig_rows) == len(REQUIRED_MIGRATIONS)
    assert all(row["status"] == "OK" for row in mig_rows)
    assert payload["summary"]["verdicts"]["arktower"] == "OK"


def test_migrations_missing_error_hints_are_structural(tmp_path: Path) -> None:
    """The typed startup error carries both Chinese and English operator hints."""
    error = MigrationsMissingError(("006_popola_hitl.sql",), tmp_path)

    assert error.hint_zh
    assert error.hint_en
