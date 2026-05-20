"""Tests for ``popola dispatch --replay`` (v0.7.3+).

Replay reads an existing handoff envelope from local disk and uses its
stored dispatch payload (target_cli / prompt / cwd / adapter_extra) to
re-run a previous dispatch — without the user retyping the prompt / flags.

These tests stub the daemon HTTP transport via httpx Mock so they don't
need a running popolad.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from popolaloom.cli.main import _resolve_replay
from popolaloom.cli.main import app as main_app
from popolaloom.handoff import HandoffEnvelope, generate_handoff_id, write_envelope


def _build_envelope(
    target_cli: str = "cursor",
    prompt: str = "fix the bug",
    *,
    cwd: str | None = None,
    adapter_extra: dict[str, str] | None = None,
) -> HandoffEnvelope:
    return HandoffEnvelope(
        handoff_id=generate_handoff_id(target_cli, prompt, adapter_extra=adapter_extra),
        created_at=datetime.now(UTC),
        target_cli=target_cli,
        prompt=prompt,
        cwd=cwd,
        adapter_extra=adapter_extra or {},
    )


# ── _resolve_replay unit tests ──────────────────────────────────────────


def test_resolve_replay_loads_envelope_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_resolve_replay`` returns the envelope's fields in a payload."""
    monkeypatch.setenv("POPOLA_HANDOFF_DIR", str(tmp_path))
    env = _build_envelope(
        "claude",
        "refactor module X",
        cwd="/some/where",
        adapter_extra={"max_turns": "10"},
    )
    write_envelope(env, base_dir=tmp_path)

    payload = _resolve_replay(env.handoff_id, "", "", None, [])

    assert payload.cli == "claude"
    assert payload.prompt == "refactor module X"
    assert payload.cwd == Path("/some/where")
    assert payload.adapter_extra == {"max_turns": "10"}


def test_resolve_replay_warns_on_inline_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Passing inline prompt / --cli / --cwd while replaying → stderr warning."""
    monkeypatch.setenv("POPOLA_HANDOFF_DIR", str(tmp_path))
    env = _build_envelope("cursor", "primary prompt")
    write_envelope(env, base_dir=tmp_path)

    _resolve_replay(env.handoff_id, "ignored", "claude", Path("/x"), ["foo=bar"])

    err = capsys.readouterr().err
    assert "warning: --replay overrides inline" in err
    assert "prompt=" in err
    assert "--cli=" in err
    assert "--cwd=" in err
    assert "--cli-flag" in err


def test_resolve_replay_missing_handoff_id_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing handoff_id → typer.Exit(1)."""
    monkeypatch.setenv("POPOLA_HANDOFF_DIR", str(tmp_path))

    with pytest.raises(typer.Exit) as exc_info:
        _resolve_replay("ghost-missing-12345678", "", "", None, [])

    assert exc_info.value.exit_code == 1


def test_resolve_replay_invalid_handoff_id_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path-traversal handoff_id → typer.Exit(2)."""
    monkeypatch.setenv("POPOLA_HANDOFF_DIR", str(tmp_path))

    with pytest.raises(typer.Exit) as exc_info:
        _resolve_replay("../escape-id", "", "", None, [])

    assert exc_info.value.exit_code == 2


# ── Full CLI integration via mocked daemon ──────────────────────────────


def test_dispatch_replay_full_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mocker: pytest.FixtureRequest,
) -> None:
    """``popola dispatch --replay <id>`` posts envelope's payload to /dispatch."""
    monkeypatch.setenv("POPOLA_HANDOFF_DIR", str(tmp_path))
    env = _build_envelope(
        "cursor",
        "replay me end-to-end",
        adapter_extra={"output_format": "stream-json"},
    )
    write_envelope(env, base_dir=tmp_path)

    # Mock the make_sync_client to return a client whose post returns 200 with task_id
    mock_client = mocker.MagicMock()
    mock_response = mocker.MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"task_id": "cursor-fake0123abcd"}
    mock_client.__enter__.return_value.post.return_value = mock_response
    mocker.patch("popolaloom.cli.main.make_sync_client", return_value=mock_client)

    runner = CliRunner()
    result = runner.invoke(main_app, ["dispatch", "--replay", env.handoff_id])

    assert result.exit_code == 0, result.output

    # Verify the POST body matched the envelope's stored payload
    posted = mock_client.__enter__.return_value.post.call_args
    body = posted.kwargs["json"]
    assert body["cli"] == "cursor"
    assert body["prompt"] == "replay me end-to-end"
    assert body["extra"] == {"output_format": "stream-json"}


def test_dispatch_replay_unknown_id_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--replay unknown-id`` → exit 1 with not-found message."""
    monkeypatch.setenv("POPOLA_HANDOFF_DIR", str(tmp_path))

    runner = CliRunner()
    result = runner.invoke(main_app, ["dispatch", "--replay", "nope-nada-deadbeef"])

    assert result.exit_code == 1
    assert "not found" in result.output


# ── Existing dispatch behaviour preserved (no --replay) ─────────────────


def test_dispatch_without_replay_still_requires_prompt(tmp_path: Path) -> None:
    """``popola dispatch --cli=cursor`` (no prompt, no --replay) → exit 2."""
    runner = CliRunner()
    result = runner.invoke(main_app, ["dispatch", "--cli", "cursor"])

    assert result.exit_code == 2, result.output
    assert "missing prompt" in result.output


def test_dispatch_without_replay_still_requires_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola dispatch <prompt>`` (no --cli, no --replay) → exit 2.

    Pins ``POPOLA_HOME`` at a fresh ``tmp_path`` so the operator's real
    ``~/.popola/popolad.toml`` ``[user_preferences]`` (which may pin a
    default ``--cloud-target`` after v1.6.0's wizard rewrite) does not
    answer for the test — without that, ``_select_cli_from_preferences``
    would resolve a CLI from the persisted preferences instead of exiting
    with "--cli is required". The contract under test (no preferences →
    explicit exit 2 + hint) is what guards the v0.7.3+ behaviour for
    operators who haven't yet run ``popola init``.
    """
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(main_app, ["dispatch", "some prompt"])

    assert result.exit_code == 2, result.output
    assert "--cli is required" in result.output
