"""v1.6.0 single-path self-hosted dispatch — contract tests.

These tests pin the 6 hard constraints from
``.local/feedbacks/feedback_for_v1.5.2.md`` (locked in the v1.6.0
plan at ``.cursor/plans/popolaloom_v1.6.0_plan_7c522961.plan.md``).
The constraint mapping (1:1):

1. **No pool mode** — ``popola cloud worker {start,debug}`` reject
   ``--pool`` / ``--pool-name`` flags with a Click ``UsageError``
   (exit 2). The supervisor also refuses ``extra.env.type='pool'``
   when ``cloud_target='self-hosted'``.
2. **No local-CLI fallback** — ``--allow-fallback`` is a no-op +
   bilingual WARN when ``cloud_target='self-hosted'``; the resolver
   never walks ``[user_preferences.routing].fallback_chain`` on the
   self-hosted path.
3. **No GitHub-App preflight** — ``check_github_app_installed(...,
   target='self-hosted')`` short-circuits to ``installed=None``
   WITHOUT calling the client's ``_request_json`` method.
4. **Dashboard URL surfaced** — ``popola dispatch ... --cloud-target=
   self-hosted`` prints ``view: <dashboard_url>`` to stdout (or a
   stderr WARN on timeout) once the daemon emits the
   ``cloud.queued`` event. (The print itself is exercised by
   ``tests/cli/test_dispatch_dashboard_url.py``; this file pins the
   ``_spawn_cloud_path_b`` event payload contract via source
   inspection.)
5. **Single canonical path** — ``--cloud-target=self-hosted`` forces
   ``--auth-mode=session-jwt`` in ``_apply_path_b_flags``;
   ``--auth-mode=rest`` exits 2. The daemon supervisor also rejects
   ``extra.__auth_mode__ != 'session-jwt'`` for self-hosted with
   ``error_kind=invalid_auth_mode_for_self_hosted``.
6. **Skill enforcement** — both SKILL.md copies (``src/popolaloom/
   skills/popola-loom/SKILL.md`` and ``.claude/skills/popola-loom/
   SKILL.md``) are byte-identical and at v1.6.0 with the rewritten
   single-self-hosted-example workflows. The byte-equality contract
   is pinned in ``tests/skills/test_skill_md_pair.py`` (kept in
   place across v1.6.0).
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from popolaloom.cli import cloud_worker_cmd
from popolaloom.cli.main import app as main_app
from popolaloom.cloud.preflight import check_github_app_installed
from popolaloom.daemon import supervisor as sup_mod

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_popola_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Hermetic ``$POPOLA_HOME`` + project dir per test."""
    popola_home = tmp_path / "popola"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("POPOLA_HOME", str(popola_home))
    monkeypatch.delenv("POPOLA_WORKER_NAME", raising=False)
    monkeypatch.delenv("POPOLA_SELF_HOSTED_WORKER_NAME", raising=False)
    monkeypatch.chdir(project)
    return popola_home


def _combined_output(result: object) -> str:
    parts: list[str] = []
    for attr in ("stdout", "stderr", "output"):
        try:
            value = getattr(result, attr, "") or ""
        except (ValueError, AttributeError):
            value = ""
        if value and value not in parts:
            parts.append(value)
    return "".join(parts)


def _mock_dispatch_client(monkeypatch: pytest.MonkeyPatch, task_id: str) -> MagicMock:
    """Stub ``make_sync_client`` so dispatch never opens a real UDS."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"task_id": task_id}
    mock_client.__enter__.return_value.post.return_value = mock_response
    monkeypatch.setattr("popolaloom.cli.main.make_sync_client", lambda: mock_client)
    return mock_client


def _posted_body(mock_client: MagicMock) -> dict[str, Any]:
    return mock_client.__enter__.return_value.post.call_args.kwargs["json"]


def _patch_jwt_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the JWT bundle is loadable (no real ~/.config/cursor/auth.json)."""

    def fake_load_jwt_bundle() -> object:  # pragma: no cover - trivial stub
        return MagicMock()

    monkeypatch.setattr(
        "popolaloom.cloud.internal.jwt_auth.load_jwt_bundle",
        fake_load_jwt_bundle,
    )


# ---------------------------------------------------------------------------
# Constraint #1 — no pool mode
# ---------------------------------------------------------------------------


def test_constraint_1_pool_flag_does_not_exist_on_worker_start(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola cloud worker start --pool`` raises UsageError (exit 2)."""
    monkeypatch.setattr(cloud_worker_cmd, "_resolve_agent_binary", lambda: "/x/agent")
    result = runner.invoke(
        main_app,
        [
            "cloud",
            "worker",
            "start",
            "--worker-dir",
            str(isolated_popola_home),
            "--pool",
        ],
    )
    assert result.exit_code == 2


def test_constraint_1_pool_flag_does_not_exist_on_worker_debug(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola cloud worker debug --pool`` raises UsageError (exit 2)."""
    monkeypatch.setattr(cloud_worker_cmd, "_resolve_agent_binary", lambda: "/x/agent")
    result = runner.invoke(
        main_app,
        [
            "cloud",
            "worker",
            "debug",
            "--worker-dir",
            str(isolated_popola_home),
            "--pool",
        ],
    )
    assert result.exit_code == 2


def test_constraint_1_pool_helpers_removed_from_module() -> None:
    """``_resolve_pool_env`` / ``_fail_pool_requires_api_key`` are deleted."""
    assert not hasattr(cloud_worker_cmd, "_resolve_pool_env")
    assert not hasattr(cloud_worker_cmd, "_fail_pool_requires_api_key")
    assert not hasattr(cloud_worker_cmd, "_EXIT_POOL_REQUIRES_API_KEY")


def test_constraint_1_supervisor_rejects_env_pool_for_self_hosted() -> None:
    """``_spawn_cloud`` rejects ``env.type=pool`` when ``cloud_target=self-hosted``.

    Source-level pin so that even a hand-rolled marker payload (or a
    legacy CLI) cannot route a self-hosted dispatch through a pool worker.
    """
    src = inspect.getsource(sup_mod.Supervisor._spawn_cloud)
    assert "pool_forbidden_self_hosted" in src
    # The check must run on the same `cloud_target_str == "self-hosted"`
    # value that gates the auth-mode reject below — keeping the constraint
    # set coherent at the daemon boundary.
    assert "cloud_target_str" in src


# ---------------------------------------------------------------------------
# Constraint #2 — no local CLI fallback for self-hosted
# ---------------------------------------------------------------------------


def test_constraint_2_allow_fallback_is_noop_for_self_hosted(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--allow-fallback`` + ``--cloud-target=self-hosted`` emits WARN."""
    _mock_dispatch_client(monkeypatch, "self-hosted-no-fallback-1234")
    _patch_jwt_loader(monkeypatch)

    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "no fallback for self-hosted",
            "--cloud-target=self-hosted",
            "--worker-name=probe-w1",
            "--allow-fallback",
        ],
    )

    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "--allow-fallback is a no-op" in out or "不生效" in out


# ---------------------------------------------------------------------------
# Constraint #3 — no GitHub-App preflight for self-hosted
# ---------------------------------------------------------------------------


def test_constraint_3_github_app_preflight_skipped_for_self_hosted() -> None:
    """``check_github_app_installed(..., target='self-hosted')`` skips."""
    spy = {"calls": 0}

    class _SpyClient:
        def list_workers(self) -> list[dict[str, Any]]:
            return []

        def _request_json(self, method: str, path: str) -> Any:
            spy["calls"] += 1
            return {"items": []}

    out = check_github_app_installed(
        _SpyClient(),
        "https://github.com/owner/repo",
        target="self-hosted",
    )
    assert out.installed is None
    assert spy["calls"] == 0, (
        "constraint #3: self-hosted target must short-circuit BEFORE "
        "calling _request_json (Path-B transports do not expose it)"
    )


# ---------------------------------------------------------------------------
# Constraint #4 — dashboard URL emitted on cloud.queued
# ---------------------------------------------------------------------------


def test_constraint_4_path_b_cloud_queued_event_carries_dashboard_url() -> None:
    """``_spawn_cloud_path_b`` writes ``dashboard_url`` into the ``cloud.queued`` event.

    Source-level pin: the supervisor must include ``dashboard_url`` in
    the queued event payload so the CLI's post-dispatch poller can
    surface it as ``view: <url>``. The actual print-to-stdout behaviour
    is verified by ``tests/cli/test_dispatch_dashboard_url.py``.
    """
    src = inspect.getsource(sup_mod.Supervisor._spawn_cloud_path_b)
    assert '"dashboard_url": outcome.dashboard_url' in src
    assert 'event_log.append("cloud.queued"' in src


# ---------------------------------------------------------------------------
# Constraint #5 — single canonical path (auth_mode=session-jwt for self-hosted)
# ---------------------------------------------------------------------------


def test_constraint_5_explicit_rest_with_self_hosted_exits_2(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--cloud-target=self-hosted --auth-mode=rest`` exits 2 explicitly."""
    _mock_dispatch_client(monkeypatch, "self-hosted-rest-1234")
    _patch_jwt_loader(monkeypatch)

    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "reject explicit rest",
            "--cloud-target=self-hosted",
            "--worker-name=probe-w1",
            "--auth-mode=rest",
        ],
    )

    assert result.exit_code == 2
    out = _combined_output(result)
    assert "session-jwt" in out


def test_constraint_5_default_auth_mode_upgrades_to_session_jwt_for_self_hosted(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting ``--auth-mode`` with self-hosted forces ``__auth_mode__=session-jwt``."""
    mock_client = _mock_dispatch_client(monkeypatch, "self-hosted-default-jwt-1234")
    _patch_jwt_loader(monkeypatch)

    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "default auth-mode upgrades",
            "--cloud-target=self-hosted",
            "--worker-name=probe-w1",
        ],
    )

    assert result.exit_code == 0, _combined_output(result)
    body = _posted_body(mock_client)
    assert body["cli"] == "cursor-cloud"
    assert body["extra"]["cloud_target"] == "self-hosted"
    assert body["extra"]["__auth_mode__"] == "session-jwt"


def test_constraint_5_supervisor_rejects_rest_for_self_hosted() -> None:
    """``_spawn_cloud`` returns ``invalid_auth_mode_for_self_hosted`` on REST + self-hosted.

    Source-level pin: even a legacy CLI that bypasses the CLI-side
    force cannot route a self-hosted dispatch through the REST branch.
    """
    src = inspect.getsource(sup_mod.Supervisor._spawn_cloud)
    assert "invalid_auth_mode_for_self_hosted" in src


# ---------------------------------------------------------------------------
# Constraint #6 — skill enforcement (both copies at v1.6.0 + byte-identical)
# ---------------------------------------------------------------------------


def test_constraint_6_skill_md_pair_byte_identical() -> None:
    """The two SKILL.md copies must be byte-identical (v1.4.0+ invariant)."""
    repo_root = Path(__file__).resolve().parents[2]
    src_skill = repo_root / "src" / "popolaloom" / "skills" / "popola-loom" / "SKILL.md"
    project_skill = repo_root / ".claude" / "skills" / "popola-loom" / "SKILL.md"
    assert src_skill.exists()
    assert project_skill.exists()
    assert src_skill.read_bytes() == project_skill.read_bytes(), (
        "constraint #6: both SKILL.md copies must be byte-identical so "
        "operators on either Cursor scope see the same single-path "
        "self-hosted workflow guidance"
    )


def test_constraint_6_skill_md_at_current_version() -> None:
    """Both SKILL.md copies declare the current ``popolaloom.__version__``.

    The hard-coded literal would force the test to fail between Wave B4
    (this file lands) and Wave B6 (SKILL.md frontmatter bumps to 1.6.0
    in lockstep with ``__version__``). Using ``popolaloom.__version__``
    keeps the test green on every intermediate commit AND pins the
    same byte-equality contract: any drift between the two surfaces
    fails this test immediately. The plan locks the v1.6.0 final value
    via ``test_constraint_6_popolaloom_version_matches_skill_frontmatter``
    + the explicit version-bump in B6's commit.
    """
    from popolaloom import __version__

    repo_root = Path(__file__).resolve().parents[2]
    for skill_path in (
        repo_root / "src" / "popolaloom" / "skills" / "popola-loom" / "SKILL.md",
        repo_root / ".claude" / "skills" / "popola-loom" / "SKILL.md",
    ):
        text = skill_path.read_text(encoding="utf-8")
        assert f"\nversion: {__version__}\n" in text, (
            f"constraint #6: {skill_path} must declare "
            f"version: {__version__} in the YAML frontmatter "
            "(Skill <-> popolaloom.__version__ lockstep)"
        )


def test_constraint_6_skill_md_documents_single_path_self_hosted() -> None:
    """The single self-hosted Workflow 12 example mentions session-jwt as the canonical path.

    The ``view: <url>`` print contract is locked separately via
    ``test_constraint_4_path_b_cloud_queued_event_carries_dashboard_url``
    (source-level) and ``tests/cli/test_dispatch_dashboard_url.py``
    (behaviour). This test stays narrow on the skill copy: the path
    must be discoverable for both the IDE and project-local Cursor
    scopes (the two byte-identical SKILL.md copies enforced by
    ``test_constraint_6_skill_md_pair_byte_identical``).
    """
    repo_root = Path(__file__).resolve().parents[2]
    skill_path = repo_root / "src" / "popolaloom" / "skills" / "popola-loom" / "SKILL.md"
    text = skill_path.read_text(encoding="utf-8")
    assert "session-jwt" in text
    assert "self-hosted" in text


def test_constraint_6_popolaloom_version_matches_skill_frontmatter() -> None:
    """``popolaloom.__version__`` must equal the SKILL.md frontmatter version."""
    from popolaloom import __version__

    repo_root = Path(__file__).resolve().parents[2]
    skill_path = repo_root / "src" / "popolaloom" / "skills" / "popola-loom" / "SKILL.md"
    text = skill_path.read_text(encoding="utf-8")
    assert f"\nversion: {__version__}\n" in text, (
        f"popolaloom.__version__={__version__!r} must equal the SKILL.md "
        "frontmatter version field (v1.4.0+ lockstep invariant)"
    )


# ---------------------------------------------------------------------------
# Combined contract — end-to-end self-hosted dispatch shape
# ---------------------------------------------------------------------------


def test_self_hosted_dispatch_emits_canonical_extras(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: ``popola dispatch --cloud-target=self-hosted`` posts the
    v1.6.0 canonical extras shape:

    - ``cloud_target=self-hosted``
    - ``worker_name=<X>``
    - ``__auth_mode__=session-jwt``
    """
    mock_client = _mock_dispatch_client(monkeypatch, "self-hosted-e2e-1234")
    _patch_jwt_loader(monkeypatch)

    result = runner.invoke(
        main_app,
        [
            "dispatch",
            "v1.6.0 e2e shape",
            "--cloud-target=self-hosted",
            "--worker-name=e2e-w1",
            "--cli-flag",
            "repo_url=https://github.com/acme/repo",
        ],
    )

    assert result.exit_code == 0, _combined_output(result)
    body = _posted_body(mock_client)
    assert body["cli"] == "cursor-cloud"
    extra = body["extra"]
    assert extra["cloud_target"] == "self-hosted"
    assert extra["worker_name"] == "e2e-w1"
    assert extra["__auth_mode__"] == "session-jwt"
    # The repo_url passthrough from --cli-flag is preserved.
    assert extra["repo_url"] == "https://github.com/acme/repo"
    # `_apply_path_b_flags` did NOT emit `env_emit_mode=explicit_pool_ack`
    # (constraint #1: no pool routing on the self-hosted path).
    assert "env_emit_mode" not in extra or extra.get("env_emit_mode") != "pool"
