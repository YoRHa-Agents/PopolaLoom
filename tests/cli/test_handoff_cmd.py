"""Tests for ``popola handoff`` CLI subcommand group (v0.7.2 patch 2).

Exercises the typer-defined commands directly via ``CliRunner`` to avoid
spinning up a daemon (the handoff CLI is filesystem-only by design).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from popolaloom.cli.handoff_cmd import app as handoff_app
from popolaloom.handoff import HandoffEnvelope, generate_handoff_id, write_envelope


def _build_envelope(prompt: str = "demo prompt", target_cli: str = "cursor") -> HandoffEnvelope:
    return HandoffEnvelope(
        handoff_id=generate_handoff_id(target_cli, prompt),
        created_at=datetime.now(UTC),
        target_cli=target_cli,
        prompt=prompt,
    )


# ── popola handoff list ──────────────────────────────────────────────────


def test_handoff_list_empty_dir(tmp_path: Path) -> None:
    """Empty / missing dir → friendly message + exit 0."""
    runner = CliRunner()
    missing = tmp_path / "nope"
    result = runner.invoke(handoff_app, ["list", "--handoff-dir", str(missing)])

    assert result.exit_code == 0, result.output
    assert "No active envelopes" in result.output


def test_handoff_list_renders_table(tmp_path: Path) -> None:
    """Single envelope shows up in the rendered table."""
    env = _build_envelope("first dispatch")
    write_envelope(env, base_dir=tmp_path)

    runner = CliRunner()
    result = runner.invoke(handoff_app, ["list", "--handoff-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert env.handoff_id in result.output
    assert "Active handoff envelopes" in result.output


def test_handoff_list_json_output(tmp_path: Path) -> None:
    """``--json`` returns parseable JSON array."""
    env_a = _build_envelope("a-prompt")
    env_b = _build_envelope("b-prompt")
    write_envelope(env_a, base_dir=tmp_path)
    write_envelope(env_b, base_dir=tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        handoff_app, ["list", "--handoff-dir", str(tmp_path), "--json"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 2
    ids = {item["handoff_id"] for item in payload}
    assert env_a.handoff_id in ids
    assert env_b.handoff_id in ids
    for item in payload:
        assert "path" in item
        assert "size_bytes" in item
        assert "mtime" in item


# ── popola handoff show ──────────────────────────────────────────────────


def test_handoff_show_default_prints_markdown(tmp_path: Path) -> None:
    """Default mode prints the raw Markdown front-matter file."""
    env = _build_envelope("show me raw")
    write_envelope(env, base_dir=tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        handoff_app, ["show", env.handoff_id, "--handoff-dir", str(tmp_path)]
    )

    assert result.exit_code == 0
    assert result.output.startswith("---\n")
    assert env.prompt in result.output
    assert env.handoff_id in result.output


def test_handoff_show_json_re_serialises(tmp_path: Path) -> None:
    """``--json`` parses the file and re-serialises the validated model."""
    env = _build_envelope("json me")
    write_envelope(env, base_dir=tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        handoff_app,
        ["show", env.handoff_id, "--handoff-dir", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["handoff_id"] == env.handoff_id
    assert payload["prompt"] == env.prompt
    assert payload["target_cli"] == env.target_cli
    assert payload["schema_version"] == "1"


def test_handoff_show_missing_id_exit_1(tmp_path: Path) -> None:
    """Missing handoff_id → exit 1 + helpful stderr."""
    runner = CliRunner()
    result = runner.invoke(
        handoff_app,
        ["show", "nope-nada-12345678", "--handoff-dir", str(tmp_path)],
    )
    assert result.exit_code == 1
    assert "not found" in result.output


def test_handoff_show_traversal_exit_2(tmp_path: Path) -> None:
    """Path-traversal id → exit 2 + invalid-handoff_id message."""
    runner = CliRunner()
    result = runner.invoke(
        handoff_app,
        ["show", "../escape", "--handoff-dir", str(tmp_path)],
    )
    assert result.exit_code == 2
    assert "invalid handoff_id" in result.output


# ── popola handoff archive ───────────────────────────────────────────────


def test_handoff_archive_copies_to_archive_root(tmp_path: Path) -> None:
    """Archive copies envelope to ``<archive_root>/<task_id>/<id>.md``."""
    env = _build_envelope("archive me")
    handoff_dir = tmp_path / "active"
    archive_dir = tmp_path / "archive"
    write_envelope(env, base_dir=handoff_dir)

    runner = CliRunner()
    result = runner.invoke(
        handoff_app,
        [
            "archive",
            env.handoff_id,
            "test-task-001",
            "--handoff-dir",
            str(handoff_dir),
            "--archive-root",
            str(archive_dir),
        ],
    )

    assert result.exit_code == 0
    expected = archive_dir / "test-task-001" / f"{env.handoff_id}.md"
    assert expected.is_file()
    assert str(expected) in result.output

    # Source still exists (audit snapshot)
    assert (handoff_dir / f"{env.handoff_id}.md").is_file()


def test_handoff_archive_missing_source_exit_1(tmp_path: Path) -> None:
    """Missing source envelope → exit 1."""
    runner = CliRunner()
    result = runner.invoke(
        handoff_app,
        [
            "archive",
            "missing-id-12345678",
            "task-x",
            "--handoff-dir",
            str(tmp_path / "active"),
            "--archive-root",
            str(tmp_path / "archive"),
        ],
    )
    assert result.exit_code == 1
    assert "source envelope missing" in result.output


def test_handoff_archive_traversal_id_exit_2(tmp_path: Path) -> None:
    """Path-traversal in handoff_id → exit 2."""
    runner = CliRunner()
    result = runner.invoke(
        handoff_app,
        [
            "archive",
            "../escape",
            "task-x",
            "--handoff-dir",
            str(tmp_path),
            "--archive-root",
            str(tmp_path / "archive"),
        ],
    )
    assert result.exit_code == 2
    assert "invalid handoff_id" in result.output


def test_handoff_archive_traversal_task_id_exit_2(tmp_path: Path) -> None:
    """Path-traversal in task_id → exit 2 (delegated to archive_envelope)."""
    env = _build_envelope("archive bad task id")
    write_envelope(env, base_dir=tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        handoff_app,
        [
            "archive",
            env.handoff_id,
            "../etc",
            "--handoff-dir",
            str(tmp_path),
            "--archive-root",
            str(tmp_path / "archive"),
        ],
    )
    assert result.exit_code == 2
    assert "archive rejected" in result.output


def test_handoff_archive_idempotent(tmp_path: Path) -> None:
    """Re-archiving the same envelope to the same destination is a no-op overwrite."""
    env = _build_envelope("twice archive")
    handoff_dir = tmp_path / "active"
    archive_dir = tmp_path / "archive"
    write_envelope(env, base_dir=handoff_dir)

    runner = CliRunner()
    args = [
        "archive",
        env.handoff_id,
        "task-twice",
        "--handoff-dir",
        str(handoff_dir),
        "--archive-root",
        str(archive_dir),
    ]
    r1 = runner.invoke(handoff_app, args)
    r2 = runner.invoke(handoff_app, args)

    assert r1.exit_code == 0
    assert r2.exit_code == 0
    expected = archive_dir / "task-twice" / f"{env.handoff_id}.md"
    assert expected.is_file()
