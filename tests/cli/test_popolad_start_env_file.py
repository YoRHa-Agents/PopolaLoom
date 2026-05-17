"""v1.5.0 — ``popola popolad start --env-file`` + 4-tier auto env injection chain.

Covers PLAN.md Phase G (feedback_for_v1.4.0 G8 + G9):

1. Explicit ``--env-file <path>`` injects KEY=VALUE pairs into the daemon
   child env.
2. ``~/.popola/cursor_api_key.env`` (existing boot-time fallback) is also
   honored CLI-side.
3. Workspace ``.local/.secrets/cursor_user_api_key.secret`` is read as a
   bare-key file (G8).
4. Workspace ``.env`` (mode 0o600) is the legacy dotenv fallback.

The helpers are exercised directly (no subprocess spawn) so the suite
runs in the default lane without depending on the actual ``popolad``
binary.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from popolaloom.cli.popolad import (
    _check_secure_mode,
    _load_bare_secret_file,
    _load_kv_env_file,
    _parse_env_file_contents,
    _resolve_child_env,
)


@pytest.fixture
def workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Build an isolated workspace + POPOLA_HOME pair."""
    popola_home = tmp_path / "popola"
    popola_home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("POPOLA_HOME", str(popola_home))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    yield project


def _write_0600(path: Path, contents: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_parse_env_file_contents_basic() -> None:
    out = _parse_env_file_contents(
        "FOO=bar\n# comment\n\nBAZ=qux\n", source=Path("/x")
    )
    assert out == {"FOO": "bar", "BAZ": "qux"}


def test_parse_env_file_contents_strips_quotes() -> None:
    out = _parse_env_file_contents(
        'A="value-with-spaces"\nB=\'single\'\nC=raw\n', source=Path("/x")
    )
    assert out == {"A": "value-with-spaces", "B": "single", "C": "raw"}


def test_parse_env_file_contents_malformed_lines_logged_and_skipped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _parse_env_file_contents(
        "GOOD=ok\nMALFORMED_NO_EQUALS\n=no_key\n",
        source=Path("/test/env"),
    )
    assert out == {"GOOD": "ok"}
    captured = capsys.readouterr()
    assert "malformed env line" in captured.err
    assert "missing key" in captured.err


def test_check_secure_mode_rejects_non_0600(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    f = tmp_path / "leaky.env"
    f.write_text("X=1")
    f.chmod(0o644)
    assert _check_secure_mode(f) is False
    assert "skipping" in capsys.readouterr().err


def test_check_secure_mode_accepts_0600(tmp_path: Path) -> None:
    f = _write_0600(tmp_path / "good.env", "X=1")
    assert _check_secure_mode(f) is True


def test_load_kv_env_file_returns_empty_on_missing(tmp_path: Path) -> None:
    assert _load_kv_env_file(tmp_path / "absent.env") == {}


def test_load_bare_secret_file_single_line_to_cursor_api_key(
    tmp_path: Path,
) -> None:
    secret = _write_0600(tmp_path / "secret", "sk-test-1234567890")
    out = _load_bare_secret_file(secret)
    assert out == {"CURSOR_API_KEY": "sk-test-1234567890"}


def test_load_bare_secret_file_kv_format_accepted(tmp_path: Path) -> None:
    """Defensive: if operator wrote KEY=VALUE format, treat as KV file."""
    secret = _write_0600(tmp_path / "secret", "CURSOR_API_KEY=sk-test-kv")
    out = _load_bare_secret_file(secret)
    assert out == {"CURSOR_API_KEY": "sk-test-kv"}


def test_load_bare_secret_file_skips_non_0600(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = tmp_path / "leaky_secret"
    secret.write_text("sk-leaky")
    secret.chmod(0o644)
    assert _load_bare_secret_file(secret) == {}


def test_resolve_child_env_env_file_wins_over_workspace_secret(
    workspace: Path,
    tmp_path: Path,
) -> None:
    """--env-file value wins over workspace secret (precedence #2 > #4)."""
    env_file = _write_0600(tmp_path / "env_file", "CURSOR_API_KEY=via-env-file")
    _write_0600(
        workspace / ".local" / ".secrets" / "cursor_user_api_key.secret",
        "via-workspace-secret",
    )
    env, sources = _resolve_child_env(cwd=workspace, env_file=env_file)
    assert env["CURSOR_API_KEY"] == "via-env-file"
    assert str(env_file) in sources


def test_resolve_child_env_workspace_secret_used_when_env_file_absent(
    workspace: Path,
) -> None:
    """G8 — workspace secret picks up CURSOR_API_KEY when no env-file."""
    _write_0600(
        workspace / ".local" / ".secrets" / "cursor_user_api_key.secret",
        "via-workspace-secret",
    )
    env, sources = _resolve_child_env(cwd=workspace, env_file=None)
    assert env["CURSOR_API_KEY"] == "via-workspace-secret"
    assert any("cursor_user_api_key.secret" in s for s in sources)


def test_resolve_child_env_popola_home_fallback(
    workspace: Path,
    tmp_path: Path,
) -> None:
    """~/.popola/cursor_api_key.env still works as a fallback source."""
    popola_home = Path(os.environ["POPOLA_HOME"])
    _write_0600(
        popola_home / "cursor_api_key.env",
        "CURSOR_API_KEY=via-popola-home\n",
    )
    env, sources = _resolve_child_env(cwd=workspace, env_file=None)
    assert env["CURSOR_API_KEY"] == "via-popola-home"
    assert any("cursor_api_key.env" in s for s in sources)


def test_resolve_child_env_existing_os_environ_wins(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator's already-exported CURSOR_API_KEY beats every file source."""
    monkeypatch.setenv("CURSOR_API_KEY", "operator-shell-export")
    _write_0600(
        workspace / ".local" / ".secrets" / "cursor_user_api_key.secret",
        "would-be-overwritten",
    )
    env, _sources = _resolve_child_env(cwd=workspace, env_file=None)
    assert env["CURSOR_API_KEY"] == "operator-shell-export"


def test_resolve_child_env_returns_empty_sources_when_no_files(
    workspace: Path,
) -> None:
    """No file sources → no diagnostic entries; CURSOR_API_KEY remains unset."""
    env, sources = _resolve_child_env(cwd=workspace, env_file=None)
    assert sources == []
    assert env.get("CURSOR_API_KEY") in (None, "")


def test_resolve_child_env_dotenv_lowest_precedence(
    workspace: Path,
    tmp_path: Path,
) -> None:
    """Workspace .env (mode 0600) is consulted last."""
    _write_0600(workspace / ".env", "CURSOR_API_KEY=via-dotenv\n")
    env, sources = _resolve_child_env(cwd=workspace, env_file=None)
    assert env["CURSOR_API_KEY"] == "via-dotenv"
    assert any(".env" in s for s in sources)


def test_resolve_child_env_workspace_secret_beats_dotenv(
    workspace: Path,
) -> None:
    """G8 secret is precedence #3; ``.env`` is #4 → secret wins."""
    _write_0600(
        workspace / ".local" / ".secrets" / "cursor_user_api_key.secret",
        "secret-key-3",
    )
    _write_0600(workspace / ".env", "CURSOR_API_KEY=dotenv-key-4\n")
    env, _sources = _resolve_child_env(cwd=workspace, env_file=None)
    assert env["CURSOR_API_KEY"] == "secret-key-3"


def test_popolad_start_signature_exposes_v1_5_0_env_flags() -> None:
    """v1.5.0 — ``popolad start`` Typer signature exposes --env-file + --reload-env."""
    import inspect

    from popolaloom.cli.popolad import start as _start_fn

    params = inspect.signature(_start_fn).parameters
    assert "env_file" in params
    assert "reload_env" in params
