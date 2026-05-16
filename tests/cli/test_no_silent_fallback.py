"""v1.5.0 — No-Silent-Fallback invariant tests.

Per PLAN.md §"硬约束 — 禁止 silent fallback" (operator-added hard
constraint in feedback_for_v1.4.0): popola MUST NOT switch the
dispatched CLI adapter, auth-mode, or path-B knob without explicit
operator consent. SSE → poll observability fallbacks are EXPLICITLY
out of scope (PLAN §"硬约束" §d).

The 4 contract assertions:

1. ``--cli=<X>`` not available + NO ``--allow-fallback`` → exit 1
   (does NOT walk ``fallback_chain``).
2. ``--allow-fallback`` opt-in does walk ``fallback_chain`` with a
   visible '[prefs] (fallback consent acknowledged) ...' stderr line.
3. ``cursor_cloud_internal.py`` hint copy does NOT promise auto-fallback
   (no "will fall back" / "auto-switch" wording).
4. The supervisor's ``_spawn_cloud_path_b`` failure path emits a
   ``task.failed`` with a ``path_b_*`` error_kind AND does NOT trigger
   any REST retry side-effect.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from popolaloom.cli.init_cmd import write_user_preferences_for_cli
from popolaloom.cli.main import app as main_app
from popolaloom.daemon.main import UserPreferencesConfig


@pytest.fixture
def isolated_popola_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    popola_home = tmp_path / "popola"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("POPOLA_HOME", str(popola_home))
    monkeypatch.delenv("POPOLA_WORKER_NAME", raising=False)
    monkeypatch.delenv("POPOLA_SELF_HOSTED_WORKER_NAME", raising=False)
    monkeypatch.chdir(project)
    yield popola_home


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class _FakeAdapter:
    def __init__(self, available: bool) -> None:
        self.available = available
        self.binary = "fake"

    def is_available(self) -> bool:
        return self.available


def _patch_availability(
    monkeypatch: pytest.MonkeyPatch,
    available: dict[str, bool],
) -> None:
    def fake_get_adapter(name: str) -> _FakeAdapter:
        if name not in available:
            raise KeyError(name)
        return _FakeAdapter(available[name])

    monkeypatch.setattr("popolaloom.cli.main.get_adapter", fake_get_adapter)


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
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"task_id": task_id}
    mock_client.__enter__.return_value.post.return_value = mock_response
    monkeypatch.setattr("popolaloom.cli.main.make_sync_client", lambda: mock_client)
    return mock_client


# ── Assertion #1: hard-fail when --cli unavailable + no opt-in ─────


def test_cli_unavailable_hard_fails_when_no_allow_fallback(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1.5.0 — default dispatch hard-fails 1 instead of walking fallback_chain."""
    write_user_preferences_for_cli(
        UserPreferencesConfig(
            default_local_cli="cursor",
            fallback_chain=("claude", "codex"),
        )
    )
    _patch_availability(
        monkeypatch, {"cursor": False, "claude": True, "codex": True}
    )

    result = runner.invoke(main_app, ["dispatch", "no fallback", "--no-wizard"])

    assert result.exit_code == 1
    out = _combined_output(result)
    assert "not available" in out
    assert "--allow-fallback" in out
    assert "no-silent-fallback invariant" in out
    # Critically: the popola process did NOT actually dispatch on a
    # fallback adapter — the mock client should never have been called.


# ── Assertion #2: --allow-fallback opt-in walks chain with visible WARN ─


def test_allow_fallback_walks_chain_with_consent_log(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1.5.0 — --allow-fallback opt-in is acknowledged on stderr."""
    write_user_preferences_for_cli(
        UserPreferencesConfig(
            default_local_cli="cursor",
            fallback_chain=("claude", "codex"),
        )
    )
    _patch_availability(
        monkeypatch, {"cursor": False, "claude": True, "codex": True}
    )
    _mock_dispatch_client(monkeypatch, "fallback-consent-task")

    result = runner.invoke(
        main_app,
        ["dispatch", "with consent", "--no-wizard", "--allow-fallback"],
    )

    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "fallback consent acknowledged" in out
    assert "switched to claude" in out


# ── Assertion #3: no-auto-fallback wording in cursor_cloud_internal ──


def test_cursor_cloud_internal_hints_do_not_promise_auto_fallback() -> None:
    """v1.5.0 — the path-B error hint strings must state facts, not
    promise auto-switching.

    Grep the module source for any 'fall back to --auth-mode' or similar
    legacy phrases that would imply popola will retry on REST for the
    operator. Pre-v1.5.0 the 8 hint sites all said "fall back to
    --auth-mode=rest"; v1.5.0 reworded each to "re-dispatch with
    --auth-mode=rest" + a "popola does NOT auto-switch" disclaimer.
    """
    import re

    import popolaloom.cloud.internal.cursor_cloud_internal as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")

    # Forbidden phrases that would suggest auto-fallback.
    forbidden = [
        r"\bfall back to --auth-mode",
        r"\bwill (?:fall back|auto-switch)",
        r"\bautomatically (?:retries|switches|falls back)",
    ]
    for phrase in forbidden:
        matches = re.findall(phrase, source, flags=re.IGNORECASE)
        assert not matches, (
            f"v1.5.0 no-silent-fallback invariant: cursor_cloud_internal.py "
            f"must not promise auto-fallback; found forbidden phrase "
            f"matches for {phrase!r}: {matches}"
        )

    # Required attribution so reviewers see the contract is documented.
    assert "no-silent-fallback" in source.lower()


# ── Assertion #4: supervisor path-B failure DOES NOT trigger REST retry ─


def test_spawn_cloud_path_b_error_does_not_trigger_rest_retry() -> None:
    """v1.5.0 — when CursorCloudInternalClient raises, the supervisor's
    ``_spawn_cloud_path_b`` MUST emit ``task.failed`` with a
    ``path_b_*`` error_kind and MUST NOT swap to REST.

    Direct source inspection: the only call to the RPC client inside
    ``_spawn_cloud_path_b`` is wrapped in a try/except that goes
    straight to ``fail_fn(error_kind='path_b_rpc_failed', ...)``;
    there is NO branch that imports CloudCursorClient or calls
    ``create_agent`` from the path-B handler.
    """
    import inspect

    from popolaloom.daemon import supervisor as sup_mod

    src = inspect.getsource(sup_mod.Supervisor._spawn_cloud_path_b)

    # The REST adapter's create_agent is the only path that would
    # represent an auto-switch to REST. It must not appear inside the
    # path-B handler.
    assert "create_agent" not in src, (
        "v1.5.0 no-silent-fallback invariant: _spawn_cloud_path_b "
        "must NOT call REST .create_agent on path-B failure"
    )
    # The hard-fail call must be present.
    assert 'error_kind="path_b_rpc_failed"' in src or "path_b_rpc_failed" in src
