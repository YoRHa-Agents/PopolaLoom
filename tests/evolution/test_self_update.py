"""Default-lane tests for :mod:`popolaloom.evolution.self_update` (v1.4.0).

Covers the four building blocks added by the v1.4.0 ``popola update``
verb:

* :func:`resolve_install_spec` — pip-spec assembly parity with
  ``install.sh:resolve_install_spec`` (lines 395-436).
* :func:`detect_install_kind` — distinguishes regular / editable / pipx
  installs without spawning subprocesses; uses
  :func:`importlib.metadata.distribution` so we can monkey-patch the
  classifier in tests.
* :func:`run_pip_upgrade` — thin :func:`subprocess.run` wrapper with
  dry-run support.
* :func:`update_all` — orchestrator combining the above with the
  existing ``upgrade_skill`` + ``check_skill_health`` library APIs.

Per the workspace rule "No Silent Failures": every refusal / failure
path is asserted to raise an explicit exception with a useful message,
not a silent ``return None``.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest

from popolaloom import __version__
from popolaloom.evolution import self_update
from popolaloom.evolution.self_update import (
    DEFAULT_GIT_URL,
    DEFAULT_PACKAGE_NAME,
    InstallKind,
    PipUpgradeError,
    UnsafeInstallError,
    UpdateConfig,
    detect_install_kind,
    outcome_to_json,
    resolve_install_spec,
    run_pip_upgrade,
    update_all,
)

# ── resolve_install_spec ─────────────────────────────────────────────────


class TestResolveInstallSpec:
    """Parity tests against install.sh:resolve_install_spec (lines 395-436)."""

    def test_default_git_no_ref_returns_bare_url(self) -> None:
        spec = resolve_install_spec(from_="git")
        assert spec == DEFAULT_GIT_URL

    def test_git_with_ref_appends_at_ref(self) -> None:
        spec = resolve_install_spec(from_="git", ref="v1.4.0")
        assert spec == f"{DEFAULT_GIT_URL}@v1.4.0"

    def test_git_with_credentials_uses_pep_508_form(self) -> None:
        spec = resolve_install_spec(from_="git", with_credentials=True)
        assert spec == f"{DEFAULT_PACKAGE_NAME}[credentials] @ {DEFAULT_GIT_URL}"

    def test_git_with_ref_and_credentials_combines_both(self) -> None:
        spec = resolve_install_spec(from_="git", ref="v1.4.0", with_credentials=True)
        assert spec == (
            f"{DEFAULT_PACKAGE_NAME}[credentials] @ {DEFAULT_GIT_URL}@v1.4.0"
        )

    def test_pypi_no_pin_returns_bare_name(self) -> None:
        spec = resolve_install_spec(from_="pypi")
        assert spec == DEFAULT_PACKAGE_NAME

    def test_pypi_with_version_returns_pinned(self) -> None:
        spec = resolve_install_spec(from_="pypi", version="1.4.0")
        assert spec == f"{DEFAULT_PACKAGE_NAME}==1.4.0"

    def test_pypi_with_credentials_inline_extras(self) -> None:
        spec = resolve_install_spec(from_="pypi", with_credentials=True)
        assert spec == f"{DEFAULT_PACKAGE_NAME}[credentials]"

    def test_pypi_with_credentials_and_version(self) -> None:
        spec = resolve_install_spec(from_="pypi", version="1.4.0", with_credentials=True)
        assert spec == f"{DEFAULT_PACKAGE_NAME}[credentials]==1.4.0"

    def test_local_path_passes_through_unchanged(self) -> None:
        local = "/abs/path/to/popolaloom-1.4.0-py3-none-any.whl"
        spec = resolve_install_spec(from_=local)
        assert spec == local

    def test_local_path_with_credentials_uses_pep_508(self) -> None:
        local = "/abs/path/to/dist"
        spec = resolve_install_spec(from_=local, with_credentials=True)
        assert spec == f"{DEFAULT_PACKAGE_NAME}[credentials] @ {local}"

    def test_ref_with_pypi_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"--ref=.* requires --from=git"):
            resolve_install_spec(from_="pypi", ref="v1.4.0")

    def test_version_with_git_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"--version=.* requires --from=pypi"):
            resolve_install_spec(from_="git", version="1.4.0")

    def test_version_with_local_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"--version=.* requires --from=pypi"):
            resolve_install_spec(from_="/local/path", version="1.4.0")

    def test_empty_from_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"from_ must be non-empty"):
            resolve_install_spec(from_="")


# ── detect_install_kind ──────────────────────────────────────────────────


class TestDetectInstallKind:
    """Cover the four classification branches with deterministic mocks."""

    def test_regular_install_when_no_pep610_no_pipx(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No direct_url.json + sys.executable not under pipx/venvs → REGULAR."""
        fake_dist = mock.MagicMock()
        fake_dist.read_text.return_value = None
        fake_dist.locate_file.return_value = "/usr/lib/python/site-packages"

        monkeypatch.setattr(
            self_update,
            "sys",
            mock.MagicMock(executable="/usr/bin/python3"),
        )
        with mock.patch(
            "importlib.metadata.distribution", return_value=fake_dist
        ):
            probe = detect_install_kind()
        assert probe.kind is InstallKind.REGULAR
        assert probe.editable_project_location is None

    def test_editable_install_via_pep610_direct_url(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """direct_url.json with dir_info.editable=True → EDITABLE."""
        fake_dist = mock.MagicMock()
        fake_dist.read_text.return_value = json.dumps(
            {"url": f"file://{tmp_path}", "dir_info": {"editable": True}}
        )
        fake_dist.locate_file.return_value = str(tmp_path)

        monkeypatch.setattr(
            self_update,
            "sys",
            mock.MagicMock(executable="/usr/bin/python3"),
        )
        with mock.patch(
            "importlib.metadata.distribution", return_value=fake_dist
        ):
            probe = detect_install_kind()
        assert probe.kind is InstallKind.EDITABLE
        assert probe.editable_project_location == tmp_path

    def test_pipx_install_via_sys_executable_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sys.executable under pipx/venvs/ → PIPX (overrides editable)."""
        fake_dist = mock.MagicMock()
        fake_dist.read_text.return_value = None
        fake_dist.locate_file.return_value = (
            "/root/.local/pipx/venvs/popolaloom/lib/python3.12/site-packages"
        )

        monkeypatch.setattr(
            self_update,
            "sys",
            mock.MagicMock(executable="/root/.local/pipx/venvs/popolaloom/bin/python"),
        )
        with mock.patch(
            "importlib.metadata.distribution", return_value=fake_dist
        ):
            probe = detect_install_kind()
        assert probe.kind is InstallKind.PIPX

    def test_unknown_when_distribution_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """importlib.metadata.PackageNotFoundError → UNKNOWN with note."""
        from importlib.metadata import PackageNotFoundError

        with mock.patch(
            "importlib.metadata.distribution",
            side_effect=PackageNotFoundError(DEFAULT_PACKAGE_NAME),
        ):
            probe = detect_install_kind()
        assert probe.kind is InstallKind.UNKNOWN
        assert any("not registered" in n for n in probe.notes)


# ── run_pip_upgrade ──────────────────────────────────────────────────────


class TestRunPipUpgrade:
    """Cover the dry-run / success / failure paths via subprocess mocks."""

    def test_empty_spec_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"spec must be non-empty"):
            run_pip_upgrade("")

    def test_dry_run_returns_argv_without_subprocess(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = mock.MagicMock()
        monkeypatch.setattr("subprocess.run", called)

        outcome = run_pip_upgrade("popolaloom==1.4.0", dry_run=True)
        assert outcome.dry_run is True
        assert outcome.spec == "popolaloom==1.4.0"
        assert outcome.argv[-1] == "popolaloom==1.4.0"
        assert outcome.returncode is None
        called.assert_not_called()

    def test_success_returns_zero_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=b"installed popolaloom-1.4.0\n",
            stderr=b"",
        )
        monkeypatch.setattr("subprocess.run", mock.MagicMock(return_value=proc))

        outcome = run_pip_upgrade("popolaloom==1.4.0")
        assert outcome.returncode == 0
        assert "installed" in outcome.stdout

    def test_failure_raises_pip_upgrade_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=b"",
            stderr=b"ERROR: Could not find a version that satisfies the requirement\n",
        )
        monkeypatch.setattr("subprocess.run", mock.MagicMock(return_value=proc))

        with pytest.raises(PipUpgradeError) as ctx:
            run_pip_upgrade("popolaloom==99.99.99")
        assert ctx.value.outcome.returncode == 1
        assert "Could not find" in ctx.value.outcome.stderr


# ── update_all ───────────────────────────────────────────────────────────


@pytest.fixture
def isolated_skill_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, Path]]:
    """Patch ``Path.home()`` + ``Path.cwd()`` so skill writes hit a tmp tree."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("POPOLA_HOME", raising=False)
    yield cwd, fake_home


class TestUpdateAll:
    """End-to-end orchestrator tests using mocked pip + real skill writes."""

    def test_dry_run_does_not_spawn_pip_or_write_skills(
        self,
        isolated_skill_home: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--dry-run`` plans every step without side effects."""
        cwd, fake_home = isolated_skill_home

        called = mock.MagicMock()
        monkeypatch.setattr("subprocess.run", called)

        # Force REGULAR install kind so the orchestrator does not refuse.
        monkeypatch.setattr(
            self_update,
            "detect_install_kind",
            lambda: self_update._InstallProbe(
                kind=InstallKind.REGULAR,
                location=Path("/fake/site-packages"),
                editable_project_location=None,
                notes=[],
            ),
        )

        config = UpdateConfig(dry_run=True, no_doctor=True)
        outcome = update_all(config)

        called.assert_not_called()
        # No skill files materialised on disk.
        assert not list((cwd / ".cursor").rglob("*")) or all(
            not p.is_file()
            for p in (cwd / ".cursor").rglob("*")
        )
        # Outcome reports the planned argv.
        assert outcome.pip is not None
        assert outcome.pip.dry_run is True

    def test_editable_install_refuses_without_force(
        self,
        isolated_skill_home: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """EDITABLE install must raise UnsafeInstallError unless force=True."""
        monkeypatch.setattr(
            self_update,
            "detect_install_kind",
            lambda: self_update._InstallProbe(
                kind=InstallKind.EDITABLE,
                location=Path("/repo/src/popolaloom"),
                editable_project_location=Path("/repo"),
                notes=[],
            ),
        )

        with pytest.raises(UnsafeInstallError) as ctx:
            update_all(UpdateConfig())
        assert ctx.value.probe.kind is InstallKind.EDITABLE
        assert "git pull" in ctx.value.hint

    def test_pipx_install_refuses_without_force(
        self,
        isolated_skill_home: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PIPX install must raise UnsafeInstallError unless force=True."""
        monkeypatch.setattr(
            self_update,
            "detect_install_kind",
            lambda: self_update._InstallProbe(
                kind=InstallKind.PIPX,
                location=Path("/root/.local/pipx/venvs/popolaloom/lib/python3.12/site-packages"),
                editable_project_location=None,
                notes=[],
            ),
        )

        with pytest.raises(UnsafeInstallError) as ctx:
            update_all(UpdateConfig())
        assert ctx.value.probe.kind is InstallKind.PIPX
        assert "pipx upgrade" in ctx.value.hint

    def test_force_overrides_editable_refusal(
        self,
        isolated_skill_home: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--force`` lets the orchestrator proceed on an editable install."""
        monkeypatch.setattr(
            self_update,
            "detect_install_kind",
            lambda: self_update._InstallProbe(
                kind=InstallKind.EDITABLE,
                location=Path("/repo/src/popolaloom"),
                editable_project_location=Path("/repo"),
                notes=[],
            ),
        )

        # Run dry-run to keep the test hermetic (no pip, no real writes).
        config = UpdateConfig(dry_run=True, force=True, no_doctor=True)
        outcome = update_all(config)
        assert outcome.install_kind is InstallKind.EDITABLE

    def test_no_skills_skips_skill_phase(
        self,
        isolated_skill_home: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--no-skills`` leaves the skills list empty."""
        monkeypatch.setattr(
            self_update,
            "detect_install_kind",
            lambda: self_update._InstallProbe(
                kind=InstallKind.REGULAR,
                location=Path("/fake"),
                editable_project_location=None,
                notes=[],
            ),
        )

        config = UpdateConfig(dry_run=True, no_skills=True, no_doctor=True)
        outcome = update_all(config)
        assert outcome.skills == []

    def test_outcome_to_json_returns_serialisable_dict(
        self,
        isolated_skill_home: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``outcome_to_json`` produces a JSON-dumpable dict."""
        monkeypatch.setattr(
            self_update,
            "detect_install_kind",
            lambda: self_update._InstallProbe(
                kind=InstallKind.REGULAR,
                location=Path("/fake"),
                editable_project_location=None,
                notes=[],
            ),
        )

        config = UpdateConfig(dry_run=True, no_doctor=True)
        outcome = update_all(config)
        payload = outcome_to_json(outcome)
        encoded = json.dumps(payload)  # raises if non-serialisable.
        assert __version__ in encoded or outcome.install_kind.value in encoded
        assert payload["install_kind"] == "regular"

    def test_daemon_running_warning_appended(
        self,
        isolated_skill_home: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """When the daemon socket is detected post-upgrade, a restart warning is appended."""
        # Force REGULAR + skip dry-run so the daemon-detection path runs.
        monkeypatch.setattr(
            self_update,
            "detect_install_kind",
            lambda: self_update._InstallProbe(
                kind=InstallKind.REGULAR,
                location=Path("/fake"),
                editable_project_location=None,
                notes=[],
            ),
        )

        # Plant a fake popolad.sock under POPOLA_HOME.
        popola_home = tmp_path / "popola_home"
        popola_home.mkdir()
        (popola_home / "popolad.sock").touch()
        monkeypatch.setenv("POPOLA_HOME", str(popola_home))

        # Stub out the real subprocess.run so pip is not invoked.
        monkeypatch.setattr(
            "subprocess.run",
            mock.MagicMock(
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=b"", stderr=b""
                )
            ),
        )

        config = UpdateConfig(no_skills=True, no_doctor=True)
        outcome = update_all(config)
        assert any("popolad daemon socket detected" in w for w in outcome.warnings), (
            outcome.warnings
        )
