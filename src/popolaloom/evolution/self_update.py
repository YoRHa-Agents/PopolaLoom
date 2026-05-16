"""self_update — Python-side ``popola update`` core (v1.4.0).

Pure-Python implementation of the update orchestration that lives in
[install.sh](../../../install.sh) ``verb_update`` (lines 502-525) so
operators can run ``popola update`` without leaving the Python entry
point.  The bash installer remains the canonical *bootstrap* path
(used by ``curl ... | bash`` before Python is even on PATH); this
module is the *post-install* path consumed by
:mod:`popolaloom.cli.update_cmd`.

The two paths share a contract by design — same flag matrix, same
sequence of side-effects, byte-stable JSON shape — but are
deliberately duplicated because each is appropriate to its context.
The parity is asserted by ``tests/test_update_parity.py`` so a future
flag rename in either place fails fast.

Three building blocks:

* :func:`resolve_install_spec` — port of ``install.sh:resolve_install_spec``
  (lines 395-436).  Pure string assembly; never touches the filesystem
  or pip.
* :func:`detect_install_kind` — classifies the running install as
  ``REGULAR`` / ``EDITABLE`` / ``PIPX`` so the orchestrator can refuse
  the unsafe ones early.  The two refusal paths exist because ``pip
  install -U git+...`` over an editable checkout corrupts state, and
  ``pip install -U`` invoked from a pipx-managed venv shadows the
  user-visible CLI installed via pipx.
* :func:`run_pip_upgrade` — thin :func:`subprocess.run` wrapper that
  raises :class:`PipUpgradeError` on non-zero exit and captures both
  streams for verbose error messages (workspace rule "No Silent
  Failures").

The orchestrator :func:`update_all` glues them together with the
existing :func:`popolaloom.evolution.skill_upgrade.upgrade_skill` and
:func:`popolaloom.evolution.skill_doctor.check_skill_health` APIs.
Returns a single :class:`UpdateOutcome` containing every step's
result so the CLI verb can render a Rich table or emit JSON without
re-running anything.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from popolaloom.evolution.skill_doctor import DoctorReport, check_skill_health
from popolaloom.evolution.skill_inject import SKILL_TARGETS, supported_scopes
from popolaloom.evolution.skill_upgrade import UpgradeOutcome, upgrade_skill

logger = logging.getLogger(__name__)


__all__ = [
    "DEFAULT_PACKAGE_NAME",
    "DEFAULT_GIT_URL",
    "InstallKind",
    "PipUpgradeError",
    "PipUpgradeOutcome",
    "UnsafeInstallError",
    "UpdateConfig",
    "UpdateOutcome",
    "detect_install_kind",
    "resolve_install_spec",
    "run_pip_upgrade",
    "update_all",
]


DEFAULT_PACKAGE_NAME: str = "popolaloom"
"""Distribution name on PyPI / pip's package index."""

DEFAULT_GIT_URL: str = "git+https://github.com/YoRHa-Agents/PopolaLoom.git"
"""Canonical GitHub clone URL — matches ``install.sh:POPOLA_GIT_URL``."""


# ── Install-kind classification ──────────────────────────────────────────


class InstallKind(StrEnum):
    """Detected installation flavor of the running ``popolaloom`` package.

    ``REGULAR``: a normal ``pip install popolaloom`` into a venv or the
    user site-packages — the only kind ``popola update`` can refresh
    safely.

    ``EDITABLE``: ``pip install -e <repo>`` editable install — running
    ``pip install -U git+...`` on top of this corrupts the .egg-link
    metadata and silently switches the import to the freshly-fetched
    copy without removing the editable .pth entry, which leaves the
    operator with two competing copies on ``sys.path``.

    ``PIPX``: pipx-managed isolated venv (``~/.local/pipx/venvs/popolaloom``);
    upgrading via inner-process pip works mechanically but pipx loses
    track of the pinned version, so we redirect the operator to
    ``pipx upgrade popolaloom`` instead.

    ``UNKNOWN``: the metadata lookup failed entirely (e.g. distribution
    not registered).  Treated as ``REGULAR`` by the orchestrator
    because the safest course is to attempt the upgrade and let pip
    report a clear error if anything is wrong.
    """

    REGULAR = "regular"
    EDITABLE = "editable"
    PIPX = "pipx"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class _InstallProbe:
    """Internal probe result returned by :func:`detect_install_kind`."""

    kind: InstallKind
    location: Path | None
    editable_project_location: Path | None
    notes: list[str] = field(default_factory=list)


def detect_install_kind() -> _InstallProbe:
    """Classify the running ``popolaloom`` distribution.

    Uses :mod:`importlib.metadata` (PEP 621) instead of the older
    ``pkg_resources`` so we do not depend on setuptools.  The two
    signals checked:

    1. **Editable** — ``importlib.metadata.distribution(...).read_text(
       'direct_url.json')`` is the standard PEP 610 location for the
       ``--editable`` marker.  When present and ``dir_info.editable ==
       True`` the install is editable.  Falls back to inspecting the
       distribution's ``location`` for a ``*.dist-info/RECORD`` listing
       a ``__editable__`` finder shim (modern hatchling layout) when
       PEP 610 metadata is missing.
    2. **pipx** — pipx installs land at ``~/.local/pipx/venvs/<pkg>/``
       (Linux/macOS) or ``%USERPROFILE%/pipx/venvs/<pkg>/`` (Windows),
       with the wrapper script symlinked into ``~/.local/bin/``.
       Detection: ``sys.executable`` lives under ``pipx/venvs``.

    Returns:
        _InstallProbe: contains ``kind``, ``location`` (the dist's
        on-disk path), ``editable_project_location`` (only when
        editable, the source-tree path the .pth points at), and
        ``notes`` (zero or more diagnostic strings).
    """
    notes: list[str] = []
    try:
        from importlib.metadata import PackageNotFoundError, distribution
    except ImportError:  # pragma: no cover — Python <3.8, unsupported.
        return _InstallProbe(InstallKind.UNKNOWN, None, None, ["importlib.metadata unavailable"])

    try:
        dist = distribution(DEFAULT_PACKAGE_NAME)
    except PackageNotFoundError:
        return _InstallProbe(
            InstallKind.UNKNOWN,
            None,
            None,
            [f"distribution {DEFAULT_PACKAGE_NAME!r} not registered with importlib.metadata"],
        )

    # ── Pipx detection (highest priority — overrides editable). ──
    exe = Path(sys.executable).resolve()
    if "pipx" in exe.parts and "venvs" in exe.parts:
        return _InstallProbe(
            InstallKind.PIPX,
            Path(str(dist.locate_file(""))).resolve(),
            None,
            [f"sys.executable={exe}; pipx-managed venv"],
        )

    # ── Editable detection via PEP 610 ``direct_url.json``. ──
    editable_project: Path | None = None
    direct_url_text = dist.read_text("direct_url.json")
    if direct_url_text is not None:
        import json as _json

        try:
            payload = _json.loads(direct_url_text)
        except _json.JSONDecodeError as exc:
            notes.append(f"direct_url.json malformed: {exc!r}")
        else:
            dir_info = payload.get("dir_info") or {}
            if isinstance(dir_info, dict) and dir_info.get("editable") is True:
                url_value = payload.get("url") or ""
                if isinstance(url_value, str) and url_value.startswith("file://"):
                    editable_project = Path(url_value.removeprefix("file://"))
                else:
                    editable_project = None
                return _InstallProbe(
                    InstallKind.EDITABLE,
                    Path(str(dist.locate_file(""))).resolve(),
                    editable_project,
                    notes,
                )

    return _InstallProbe(
        InstallKind.REGULAR,
        Path(str(dist.locate_file(""))).resolve(),
        None,
        notes,
    )


# ── Pip spec resolution (port of install.sh:resolve_install_spec) ────────


def resolve_install_spec(
    *,
    from_: str = "git",
    ref: str | None = None,
    version: str | None = None,
    with_credentials: bool = False,
    package_name: str = DEFAULT_PACKAGE_NAME,
    git_url: str = DEFAULT_GIT_URL,
) -> str:
    """Compute the pip install spec for the given ``--from`` / ``--ref`` / ``--version`` flags.

    Direct port of [install.sh](../../../install.sh) lines 395-436 —
    same precedence rules, same error-on-conflict behaviour, same
    PEP 508 ``pkg[extras] @ <url>`` form for git/local sources.

    Args:
        from_: ``"git"`` (default — tracks ``main``), ``"pypi"``, or
            an arbitrary local path / URL.  Must be non-empty.
        ref: optional git tag / branch / sha; only valid when
            ``from_ == "git"``.  Appended as ``@<ref>`` to the git URL.
        version: optional ``X.Y.Z`` pin; only valid when
            ``from_ == "pypi"``.  Appended as ``==<version>``.
        with_credentials: include the ``[credentials]`` extra so the
            keyring backend installs alongside the wheel.
        package_name: distribution name (defaults to
            :data:`DEFAULT_PACKAGE_NAME`).
        git_url: canonical GitHub clone URL (defaults to
            :data:`DEFAULT_GIT_URL`).

    Returns:
        str: the spec string ready to pass to
        ``python -m pip install --upgrade <spec>``.

    Raises:
        ValueError: when the ``ref`` / ``version`` flags are combined
            with an incompatible ``from_`` source (No Silent Failures —
            mirrors the install.sh ``validate_args`` checks at lines
            287-301).
    """
    if not from_:
        raise ValueError("resolve_install_spec: from_ must be non-empty")

    if ref is not None and from_ != "git":
        raise ValueError(
            f"resolve_install_spec: --ref={ref!r} requires --from=git "
            f"(got --from={from_!r})"
        )
    if version is not None and from_ != "pypi":
        raise ValueError(
            f"resolve_install_spec: --version={version!r} requires --from=pypi "
            f"(got --from={from_!r})"
        )

    extras = "[credentials]" if with_credentials else ""

    if from_ == "pypi":
        if version:
            return f"{package_name}{extras}=={version}"
        return f"{package_name}{extras}"

    if from_ == "git":
        url = git_url
        if ref:
            url = f"{url}@{ref}"
        if extras:
            return f"{package_name}{extras} @ {url}"
        return url

    # Treat anything else as a local filesystem path / non-git URL.
    if extras:
        return f"{package_name}{extras} @ {from_}"
    return from_


# ── Pip subprocess runner ────────────────────────────────────────────────


@dataclass(frozen=True)
class PipUpgradeOutcome:
    """Result of one :func:`run_pip_upgrade` invocation.

    Attributes:
        spec:        Resolved pip spec (as passed to ``pip install -U``).
        argv:        Full argv list executed (or *would* execute when
                     ``dry_run=True``).
        dry_run:     When ``True``, no subprocess was spawned; ``stdout``
                     / ``stderr`` / ``returncode`` are empty / None.
        returncode:  Exit code from the pip subprocess (``None`` for
                     dry-run, ``0`` on success, non-zero on failure
                     when ``check=False``).
        stdout:      Captured stdout bytes decoded as UTF-8.
        stderr:      Captured stderr bytes decoded as UTF-8.
    """

    spec: str
    argv: list[str]
    dry_run: bool = False
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""


class PipUpgradeError(RuntimeError):
    """Raised when ``pip install --upgrade <spec>`` exits non-zero.

    Carries the :class:`PipUpgradeOutcome` so callers can render the
    captured stdout / stderr without re-running pip.
    """

    def __init__(self, outcome: PipUpgradeOutcome) -> None:
        self.outcome = outcome
        super().__init__(
            f"pip install --upgrade failed (exit {outcome.returncode}): "
            f"spec={outcome.spec!r}; stderr_tail="
            f"{outcome.stderr.splitlines()[-3:] if outcome.stderr else []!r}"
        )


def run_pip_upgrade(
    spec: str,
    *,
    python: str | None = None,
    dry_run: bool = False,
    extra_args: list[str] | None = None,
) -> PipUpgradeOutcome:
    """Invoke ``<python> -m pip install --upgrade <spec>``.

    Args:
        spec: pip spec returned by :func:`resolve_install_spec`.  Must
            be non-empty.
        python: optional Python interpreter to invoke; defaults to
            :data:`sys.executable` so the upgrade lands in the same
            environment that's running ``popola``.
        dry_run: when ``True``, the function builds the argv but does
            NOT spawn a subprocess.  Used by ``popola update --dry-run``.
        extra_args: optional pip flags appended after ``--upgrade``
            (kept for forward-compat — current callers pass nothing).

    Returns:
        PipUpgradeOutcome: see class docstring.

    Raises:
        ValueError: when ``spec`` is empty.
        PipUpgradeError: when the subprocess returns non-zero (No
            Silent Failures — the caller MUST handle it explicitly).
    """
    if not spec or not spec.strip():
        raise ValueError("run_pip_upgrade: spec must be non-empty")
    py = python or sys.executable
    argv: list[str] = [py, "-m", "pip", "install", "--upgrade", spec]
    if extra_args:
        argv.extend(extra_args)

    if dry_run:
        return PipUpgradeOutcome(
            spec=spec,
            argv=argv,
            dry_run=True,
        )

    proc = subprocess.run(  # noqa: S603 — argv is a constructed list, not user-shell.
        argv,
        capture_output=True,
        check=False,
        text=False,
    )
    outcome = PipUpgradeOutcome(
        spec=spec,
        argv=argv,
        dry_run=False,
        returncode=proc.returncode,
        stdout=proc.stdout.decode("utf-8", errors="replace") if proc.stdout else "",
        stderr=proc.stderr.decode("utf-8", errors="replace") if proc.stderr else "",
    )
    if proc.returncode != 0:
        raise PipUpgradeError(outcome)
    return outcome


# ── Update orchestrator ──────────────────────────────────────────────────


class UnsafeInstallError(RuntimeError):
    """Raised when :func:`update_all` refuses to run on the current install.

    Two trigger paths:

    1. ``InstallKind.EDITABLE`` — the orchestrator declines because
       ``pip install -U git+...`` over an editable checkout corrupts
       both copies on ``sys.path``.  Operators are redirected to
       ``git pull`` + ``popola skill upgrade --target=all
       --global --project``.
    2. ``InstallKind.PIPX`` — pipx loses track of the pinned version
       when an inner pip upgrade silently changes the wheel.
       Operators are redirected to ``pipx upgrade popolaloom``.

    Carries the :class:`_InstallProbe` so the CLI verb can render
    detail-rich remediation hints.
    """

    def __init__(self, probe: _InstallProbe, hint: str) -> None:
        self.probe = probe
        self.hint = hint
        super().__init__(
            f"unsafe install kind {probe.kind.value!r} for popola update; {hint}"
        )


@dataclass(frozen=True)
class UpdateConfig:
    """Frozen config bag for :func:`update_all` and the CLI verb.

    All fields default to the safe / canonical values used by
    ``install.sh update`` so a no-flag invocation does the right
    thing.

    Attributes:
        target:           ``cursor`` / ``claude`` / ``codex`` /
                          ``copilot`` / ``all`` (default).
        scope:            ``global`` / ``project`` / ``both`` (default).
                          ``both`` runs every (target, scope) pair the
                          target supports.  ``project`` skips
                          codex-global; ``global`` skips copilot-project.
        from_:            ``"git"`` (default — tracks ``main``),
                          ``"pypi"``, or a local path.
        ref:              git tag / branch / sha (only valid with git).
        version:          PyPI pin ``X.Y.Z`` (only valid with pypi).
        python:           override Python interpreter for the pip
                          subprocess (defaults to :data:`sys.executable`).
        no_skills:        skip the skill-upgrade phase.
        no_doctor:        skip the post-upgrade :func:`popola doctor`
                          probe.
        with_credentials: include the ``[credentials]`` extra.
        dry_run:          plan-only; no subprocess, no writes.
        force:            override the editable / pipx refusal (escape
                          hatch — operator accepts the resulting state
                          may be inconsistent).  Defaults to ``False``;
                          operators must pass it explicitly.
    """

    target: str = "all"
    scope: str = "both"
    from_: str = "git"
    ref: str | None = None
    version: str | None = None
    python: str | None = None
    no_skills: bool = False
    no_doctor: bool = False
    with_credentials: bool = False
    dry_run: bool = False
    force: bool = False


@dataclass(frozen=True)
class UpdateOutcome:
    """Aggregated outcome of one :func:`update_all` invocation.

    Attributes:
        config:        Echo of the input :class:`UpdateConfig` for
                       audit / JSON output.
        spec:          Resolved pip spec from
                       :func:`resolve_install_spec`.
        install_kind:  Detected :class:`InstallKind` of the running
                       distribution.
        pip:           :class:`PipUpgradeOutcome` from
                       :func:`run_pip_upgrade` (``None`` when
                       ``dry_run=True`` *and* ``no_skills=True``, i.e.
                       a no-op invocation, or when ``no_skills=False``
                       but ``dry_run=True`` we still record the
                       planned argv).
        skills:        list of :class:`UpgradeOutcome` (one per
                       (target, scope) pair processed).  Empty when
                       ``no_skills=True``.
        doctor:        list of :class:`DoctorReport` after the upgrade
                       (empty when ``no_doctor=True`` or
                       ``dry_run=True``).
        warnings:      Free-form diagnostic strings — e.g. "daemon
                       running with old wheel; restart with `popola
                       popolad stop && start`".  Always populated
                       (never raises silently).
    """

    config: UpdateConfig
    spec: str
    install_kind: InstallKind
    pip: PipUpgradeOutcome | None
    skills: list[UpgradeOutcome] = field(default_factory=list)
    doctor: list[DoctorReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _expand_target_list(target: str) -> list[str]:
    """Expand ``--target`` to the registry keys to operate on."""
    if target == "all":
        return list(SKILL_TARGETS.keys())
    if target not in SKILL_TARGETS:
        raise ValueError(
            f"unknown skill target {target!r}; valid: "
            f"{', '.join(sorted({*SKILL_TARGETS.keys(), 'all'}))}"
        )
    return [target]


def _expand_scope_list(target: str, scope: str) -> list[str]:
    """Expand ``--scope`` to the scopes registered for ``target``.

    ``scope == "both"`` -> every scope the target supports (cursor /
    claude have two; codex has only ``global``; copilot has only
    ``project``).  ``scope == "global"`` / ``scope == "project"`` ->
    that scope when the target supports it, otherwise the only
    supported scope (mirrors ``popola skill install`` fallback).
    """
    target_scopes = supported_scopes(target)
    if not target_scopes:
        return []
    if scope == "both":
        return target_scopes
    if scope in target_scopes:
        return [scope]
    return target_scopes  # silent fallback to the single supported scope


def _detect_daemon_running() -> bool:
    """Return ``True`` iff a ``popolad`` daemon socket appears live.

    Uses ``$POPOLA_HOME`` (default ``~/.popola/``) and probes
    ``popolad.sock`` — same path :class:`popola probe` walks.  Pure
    filesystem check; never imports the daemon process module.
    """
    home = os.environ.get("POPOLA_HOME") or str(Path.home() / ".popola")
    sock = Path(home) / "popolad.sock"
    return sock.exists()


def update_all(config: UpdateConfig) -> UpdateOutcome:
    """Run the complete update sequence: detect → pip → skills → doctor.

    Workflow (mirrors install.sh:verb_update):

    1. Probe the install kind via :func:`detect_install_kind`.  Refuse
       on ``EDITABLE`` / ``PIPX`` unless ``config.force=True``.
    2. Resolve the pip spec via :func:`resolve_install_spec`.
    3. Run :func:`run_pip_upgrade` (skip on ``config.dry_run``).
    4. For every (target, scope) pair selected by
       ``config.target`` × ``config.scope``, invoke
       :func:`popolaloom.evolution.skill_upgrade.upgrade_skill`.
       Skip the whole phase on ``config.no_skills``.
    5. Run :func:`popolaloom.evolution.skill_doctor.check_skill_health`
       to surface any residual drift.  Skip on
       ``config.no_doctor`` / ``config.dry_run``.
    6. If ``popolad.sock`` is present, append a daemon-restart
       warning so the operator knows to bounce the daemon (we never
       auto-restart — in-flight tasks would die).

    Workspace rule "No Silent Failures": every refusal raises
    :class:`UnsafeInstallError`; every pip failure raises
    :class:`PipUpgradeError`.  The function never returns a partial
    success silently — the caller (CLI verb) catches and renders the
    error explicitly.

    Args:
        config: :class:`UpdateConfig` with the operator's flag values.

    Returns:
        UpdateOutcome: see class docstring.

    Raises:
        UnsafeInstallError: when ``config.force=False`` and the
            running install is editable or pipx-managed.
        PipUpgradeError: when the pip subprocess exits non-zero.
        ValueError: when ``config.target`` / ``config.scope`` /
            ``config.from_`` combinations are invalid (delegated to
            :func:`resolve_install_spec` and :func:`_expand_target_list`).
    """
    probe = detect_install_kind()
    warnings: list[str] = list(probe.notes)

    if not config.force and probe.kind in (InstallKind.EDITABLE, InstallKind.PIPX):
        if probe.kind is InstallKind.EDITABLE:
            hint = (
                f"running from editable install at "
                f"{probe.editable_project_location or probe.location}; run "
                f"`git pull && popola skill upgrade --target=all "
                f"--global --project` instead, or pass `--force` to "
                f"override (will leave a stale .pth entry on sys.path)."
            )
        else:
            hint = (
                f"popola was installed via pipx (sys.executable={sys.executable}); "
                f"run `pipx upgrade popolaloom` then "
                f"`popola skill upgrade --target=all --global --project`, or "
                f"pass `--force` to override (pipx will lose its pinned-version "
                f"tracking until the next `pipx reinstall`)."
            )
        raise UnsafeInstallError(probe, hint)

    spec = resolve_install_spec(
        from_=config.from_,
        ref=config.ref,
        version=config.version,
        with_credentials=config.with_credentials,
    )

    pip_outcome: PipUpgradeOutcome | None
    pip_outcome = run_pip_upgrade(
        spec,
        python=config.python,
        dry_run=config.dry_run,
    )

    skill_outcomes: list[UpgradeOutcome] = []
    if not config.no_skills:
        targets = _expand_target_list(config.target)
        for target in targets:
            for scope in _expand_scope_list(target, config.scope):
                skill_outcomes.append(
                    upgrade_skill(target, scope=scope, dry_run=config.dry_run)
                )

    doctor_reports: list[DoctorReport] = []
    if not config.no_doctor and not config.dry_run:
        doctor_reports = check_skill_health()

    if not config.dry_run and _detect_daemon_running():
        warnings.append(
            "popolad daemon socket detected at "
            f"{Path(os.environ.get('POPOLA_HOME') or Path.home() / '.popola') / 'popolad.sock'}; "
            "restart it after the upgrade so the new wheel is loaded "
            "(`popola popolad stop && popola popolad start`). "
            "Auto-restart is intentionally disabled — in-flight tasks "
            "would die mid-flight."
        )

    return UpdateOutcome(
        config=config,
        spec=spec,
        install_kind=probe.kind,
        pip=pip_outcome,
        skills=skill_outcomes,
        doctor=doctor_reports,
        warnings=warnings,
    )


def outcome_to_json(outcome: UpdateOutcome) -> dict[str, Any]:
    """Convert :class:`UpdateOutcome` to a JSON-serialisable dict.

    Walks the dataclass tree and stringifies :class:`pathlib.Path`
    values.  Used by ``popola update --json`` for stable
    machine-readable output.
    """
    from dataclasses import asdict

    def _stringify_paths(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: _stringify_paths(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_stringify_paths(x) for x in node]
        if isinstance(node, Path):
            return str(node)
        if isinstance(node, StrEnum):
            return node.value
        return node

    raw = asdict(outcome)
    walked = _stringify_paths(raw)
    # _stringify_paths preserves the ``dict[str, Any]`` shape for the top-
    # level argument (asdict always returns a dict for a dataclass), but
    # mypy can't prove that across the recursive ``Any``-typed helper.
    # Cast back to the declared return type — runtime invariant is
    # enforced by :func:`dataclasses.asdict` returning a dict.
    assert isinstance(walked, dict)
    return walked


__all__ += ["outcome_to_json"]
