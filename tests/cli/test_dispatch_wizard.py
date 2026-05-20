"""Dispatch option-group wizard tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from popolaloom.cli.init_cmd import write_user_preferences_for_cli
from popolaloom.cli.main import app as main_app
from popolaloom.daemon.main import (
    UserPreferencesConfig,
    UserPrefsCursorCloud,
    UserPrefsRouting,
)


@pytest.fixture(autouse=True)
def _stub_jwt_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic JWT-loader stub for v1.6.0 self-hosted single-path contract.

    v1.6.0 hard-forces ``--auth-mode=session-jwt`` whenever the resolved
    ``cloud_target=self-hosted`` (feedback_for_v1.5.2 constraint #5), and
    :func:`_apply_path_b_flags` then eagerly calls
    :func:`popolaloom.cloud.internal.jwt_auth.load_jwt_bundle` so the
    operator sees the ``agent login`` hint at dispatch time instead of
    inside the daemon's RPC failure path.

    CI runners (and any hermetic test env) have neither ``CURSOR_SESSION_JWT``
    nor ``~/.config/cursor/auth.json``, so the load would exit 1 before
    the dispatch ever reaches the mocked popolad client. Stubbing the
    loader to a sentinel preserves the wizard end-to-end test intent
    (verify the wizard produces the correct dispatch payload) without
    requiring a real JWT — the dispatch wire shape is the unit under
    test, not the JWT auth machinery (covered by
    :mod:`tests.cloud.internal.test_jwt_auth`).
    """
    monkeypatch.setattr(
        "popolaloom.cloud.internal.jwt_auth.load_jwt_bundle",
        lambda: object(),
    )


@pytest.fixture
def isolated_popola_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    popola_home = tmp_path / "popola"
    project = tmp_path / "project"
    popola_home.mkdir()
    project.mkdir()
    monkeypatch.setenv("POPOLA_HOME", str(popola_home))
    monkeypatch.chdir(project)
    yield popola_home


def _mock_dispatch_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"task_id": "wizard-task-123"}
    mock_client.__enter__.return_value.post.return_value = mock_response
    monkeypatch.setattr("popolaloom.cli.main.make_sync_client", lambda: mock_client)
    return mock_client


def _posted_body(mock_client: MagicMock) -> dict[str, Any]:
    return mock_client.__enter__.return_value.post.call_args.kwargs["json"]


def test_dispatch_wizard_local_cursor_shape(
    isolated_popola_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_user_preferences_for_cli(UserPreferencesConfig())
    mock_client = _mock_dispatch_client(monkeypatch)
    runner = CliRunner()

    result = runner.invoke(
        main_app,
        ["dispatch", "refactor X", "--wizard"],
        input="1\n1\ny\n",
    )

    assert result.exit_code == 0, result.output
    body = _posted_body(mock_client)
    assert body["cli"] == "cursor"
    assert body["extra"]["output_format"] == "text"


def test_dispatch_wizard_cursor_cloud_self_hosted_shape(
    isolated_popola_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_user_preferences_for_cli(
        UserPreferencesConfig(
            routing=UserPrefsRouting(default_runtime="cloud"),
            cursor_cloud=UserPrefsCursorCloud(
                model="gpt-5.5",
                default_cloud_target="self-hosted",
                worker_name="worker-a",
            ),
        )
    )
    mock_client = _mock_dispatch_client(monkeypatch)
    runner = CliRunner()

    # v1.3.0 P6: wizard now also prompts for preset/effort/thinking_level/max_mode
    # AFTER the worker_name prompt — accept defaults via blank lines + "n" for max_mode.
    result = runner.invoke(
        main_app,
        ["dispatch", "feature X", "--wizard"],
        input="5\n5\n\n\n\n\nn\n1,2\ny\n",
    )

    assert result.exit_code == 0, result.output
    body = _posted_body(mock_client)
    assert body["cli"] == "cursor-cloud"
    assert body["extra"]["model"] == "gpt-5.5"
    assert body["extra"]["env"] == {"type": "machine", "name": "worker-a"}
    assert body["extra"]["auto_create_pr"] is True
    assert body["extra"]["work_on_current_branch"] is True
