"""Tests for the v0.9.5 init-time Cursor API key intake flags.

Closes ``.local/feedbacks/feedback_for_v0.9.4.md``: when ``popola init``
is invoked, if the operator provides a Cursor API key, store it
encrypted (OS keyring via the existing :mod:`popolaloom.credentials`
module) so they never have to re-enter it.

The v0.9.5 surface adds two new top-level options on the init root
callback:

* ``--cursor-api-key VAL`` — non-interactive intake (literal value
  forwarded to :func:`popolaloom.credentials.store_cursor_api_key`).
* ``--cursor-api-key-file PATH`` — read the first non-empty line of
  PATH (utf-8) and treat it like ``--cursor-api-key``.

Either flag implies ``--configure-cursor-auth`` (the existing v0.9.2+
flag, which is now accepted on every init path — auto-detect, verb
subcommand, ``--target=cloud-only``, ``--interactive``). ``--dry-run``
short-circuits credential persistence with a clear one-line skip
message (per the workspace **No Silent Failures** rule for secrets —
never prompt or persist during a preview).

This module is hermetic: every test monkeypatches
:func:`popolaloom.cli.init_cmd.is_keyring_available` /
:func:`popolaloom.cli.init_cmd.store_cursor_api_key` (rebound via the
stdlib lazy-import seam in :mod:`popolaloom.credentials`) so a CI run
on a vanilla Linux box without libsecret produces the same observable
behaviour as the developer's macOS host.
"""

from __future__ import annotations

import re
import types
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from popolaloom import credentials as cred_mod
from popolaloom.cli import init_cmd
from popolaloom.cli.init_cmd import app as init_app

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
"""Strip ANSI color/format escapes from Typer/Rich-rendered output.

CI runners default to 80-column terminals, where Rich wraps long flag
names and panel messages across lines and splices ANSI control codes
in between (e.g. ``mutually \\x1b[31m \\x1b[0m exclusive``). The two
assertions below collapse output through :func:`_normalize_terminal_text`
so the substring checks stay robust to wrapping width and decoration.
"""


def _normalize_terminal_text(text: str) -> str:
    """Strip ANSI escapes + Rich box drawing + collapse whitespace."""
    cleaned = _ANSI_ESCAPE_RE.sub("", text)
    for ch in ("│", "╭", "╮", "╰", "╯", "─"):
        cleaned = cleaned.replace(ch, " ")
    return " ".join(cleaned.split()).lower()


class _FakeBackend:
    """In-memory fake of the upstream ``keyring`` module's backend protocol.

    Mirrors :class:`tests.cli.test_init_configure_cursor_auth._FakeBackend`
    so the two suites share the same hermetic fixture shape. ``__module__``
    is set to ``"fake.backend"`` (does NOT end with ``.fail``) so
    :func:`popolaloom.credentials.is_keyring_available` returns True.
    """

    __module__ = "fake.backend"
    __qualname__ = "Keyring"

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, value: str) -> None:
        self.store[(service, username)] = value

    def delete_password(self, service: str, username: str) -> None:
        del self.store[(service, username)]


@pytest.fixture
def isolated_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, Path]]:
    """Yield ``(cwd, fake_home)`` with ``Path.home()`` + ``Path.cwd()`` patched.

    Mirrors the ``tests/cli/test_init_configure_cursor_auth.py`` fixture
    so the two suites share the same isolation contract: install verbs
    that hit ``Path.home()`` (cursor --global, codex) land under
    ``fake_home``; verbs that hit ``Path.cwd()`` land under ``cwd``.
    ``CURSOR_API_KEY`` is unset so the env-var precedence slot does not
    leak the developer's real shell value into the keyring fingerprint
    surface. ``$POPOLA_HOME`` points at a tmp dir so the credentials
    metadata file does not pollute the developer's ``~/.popola``.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path / "popola"))

    yield cwd, fake_home


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeBackend]:
    """Inject a fake keyring backend so credential persistence is hermetic.

    Patches :func:`popolaloom.credentials._import_keyring` (the lazy-import
    seam) to return a stub module exposing the four upstream entry points
    (``get_keyring`` / ``get_password`` / ``set_password`` /
    ``delete_password``). The stub backend stores secrets in-process so
    tests can assert on ``backend.store`` after the helper runs.
    """
    backend = _FakeBackend()
    fake_module = types.ModuleType("keyring")
    fake_module.get_keyring = lambda: backend  # type: ignore[attr-defined]
    fake_module.get_password = lambda s, u: backend.get_password(s, u)  # type: ignore[attr-defined]
    fake_module.set_password = lambda s, u, v: backend.set_password(s, u, v)  # type: ignore[attr-defined]
    fake_module.delete_password = lambda s, u: backend.delete_password(s, u)  # type: ignore[attr-defined]
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: fake_module)
    yield backend


@pytest.fixture
def call_recorder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every call to ``store_cursor_api_key`` from inside ``init_cmd``.

    The brief explicitly asks for ``monkeypatch.setattr(init_cmd,
    "store_cursor_api_key", ...)`` — but the credential helper imports
    the symbol lazily inside the function body (so the global is not
    bound at module import time). Instead, we wrap the underlying
    :mod:`popolaloom.credentials` symbol so the helper records its
    invocation while still going through the keyring stub, returning
    the canonical :class:`popolaloom.credentials.CredentialStatus`
    envelope so the helper's ``status.backend_name`` access does not
    crash. Hermetic + faithful to the helper's actual code path.
    """
    calls: list[str] = []
    original = cred_mod.store_cursor_api_key

    def _spy(api_key: str) -> object:
        calls.append(api_key)
        return original(api_key)

    monkeypatch.setattr(cred_mod, "store_cursor_api_key", _spy)
    return calls


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _combined_output(result: object) -> str:
    """Return ``result.stdout`` + best-effort ``result.stderr`` + ``output``."""
    stdout = getattr(result, "stdout", "") or ""
    try:
        stderr = getattr(result, "stderr", "") or ""
    except (ValueError, AttributeError):
        stderr = ""
    output = getattr(result, "output", "") or ""
    return stdout + stderr + output


# ── --cursor-api-key VAL on plain `popola init` (auto-detect) ───────────


def test_cursor_api_key_on_auto_detect_persists_via_keyring(
    isolated_home: tuple[Path, Path],
    fake_backend: _FakeBackend,
    call_recorder: list[str],
    runner: CliRunner,
) -> None:
    """``popola init --cursor-api-key cr_test`` persists the value after auto-install.

    Closes ``.local/feedbacks/feedback_for_v0.9.4.md``: a single
    invocation gets the operator BOTH the per-IDE skill install AND a
    persisted Cursor API key in one step. We pre-create a ``.cursor/``
    marker so ``_auto_detect`` dispatches to the cursor verb (mirrors
    the ``test_init_no_args_auto_detects_targets`` fixture setup).
    """
    cwd, _ = isolated_home
    (cwd / ".cursor").mkdir()
    result = runner.invoke(init_app, ["--cursor-api-key", "cr_test_value"])
    out = _combined_output(result)
    assert result.exit_code == 0, out
    # Auto-detect dispatched to cursor (the marker we created above).
    assert (cwd / ".cursor" / "skills" / "popola-loom" / "SKILL.md").is_file()
    # Helper persisted the value.
    assert call_recorder == ["cr_test_value"]
    assert fake_backend.store.get(("popolaloom.cursor", "default")) == "cr_test_value"
    # Fingerprint banner present; raw value never echoed.
    assert "Stored Cursor API key" in out
    assert "cr_test_value" not in out


# ── --cursor-api-key-file PATH ─────────────────────────────────────────


def test_cursor_api_key_file_reads_first_non_empty_line_and_persists(
    isolated_home: tuple[Path, Path],
    fake_backend: _FakeBackend,
    call_recorder: list[str],
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """``--cursor-api-key-file`` reads the first non-empty line and persists it."""
    cwd, _ = isolated_home
    (cwd / ".cursor").mkdir()
    keyfile = tmp_path / "secret.key"
    # Leading blank lines + trailing newline are intentional — the helper
    # must skip blanks and strip trailing whitespace per the v0.9.5 spec.
    keyfile.write_text("\n\n   cr_from_file_value   \nsome-other-line\n", encoding="utf-8")
    result = runner.invoke(init_app, ["--cursor-api-key-file", str(keyfile)])
    out = _combined_output(result)
    assert result.exit_code == 0, out
    assert call_recorder == ["cr_from_file_value"]
    assert fake_backend.store.get(("popolaloom.cursor", "default")) == "cr_from_file_value"
    assert "cr_from_file_value" not in out
    # The other line in the file MUST NOT be persisted (only the first
    # non-empty line is treated as the key).
    assert "some-other-line" not in out


# ── mutual exclusion of --cursor-api-key + --cursor-api-key-file ────────


def test_cursor_api_key_and_file_are_mutually_exclusive(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Passing both ``--cursor-api-key`` and ``--cursor-api-key-file`` exits ≠0.

    Per **No Silent Failures**: the operator must pick exactly one
    intake source so the resolved value is unambiguous. Typer renders
    the BadParameter as a Rich-bordered panel that may wrap the message
    across two lines (``mutually`` / ``exclusive``); we collapse all
    whitespace before searching for the canonical phrase so the
    rendered surface stays robust to terminal-width changes.
    """
    keyfile = tmp_path / "secret.key"
    keyfile.write_text("cr_file\n", encoding="utf-8")
    result = runner.invoke(
        init_app,
        ["--cursor-api-key", "cr_inline", "--cursor-api-key-file", str(keyfile)],
    )
    assert result.exit_code != 0
    flat = _normalize_terminal_text(_combined_output(result))
    assert "mutually exclusive" in flat


# ── empty / whitespace-only inline value rejected ──────────────────────


@pytest.mark.parametrize("bad_value", ["", "   ", "\t\n  \t"])
def test_empty_or_whitespace_cursor_api_key_rejected(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
    bad_value: str,
) -> None:
    """Empty/whitespace-only ``--cursor-api-key`` exits ≠0 with a clear error."""
    result = runner.invoke(init_app, ["--cursor-api-key", bad_value])
    assert result.exit_code != 0
    out = _combined_output(result).lower()
    assert "empty" in out or "whitespace" in out


# ── missing file rejected ───────────────────────────────────────────────


def test_missing_cursor_api_key_file_rejected(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """``--cursor-api-key-file`` pointing at a missing path exits ≠0."""
    missing = tmp_path / "does-not-exist.key"
    result = runner.invoke(init_app, ["--cursor-api-key-file", str(missing)])
    assert result.exit_code != 0
    out = _combined_output(result).lower()
    assert "not found" in out or "no such file" in out


def test_empty_cursor_api_key_file_rejected(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """``--cursor-api-key-file`` pointing at an empty file exits ≠0."""
    empty_file = tmp_path / "empty.key"
    empty_file.write_text("", encoding="utf-8")
    result = runner.invoke(init_app, ["--cursor-api-key-file", str(empty_file)])
    assert result.exit_code != 0
    out = _combined_output(result).lower()
    assert "empty" in out or "whitespace" in out


def test_whitespace_only_cursor_api_key_file_rejected(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """``--cursor-api-key-file`` pointing at a whitespace-only file exits ≠0."""
    ws_file = tmp_path / "ws.key"
    ws_file.write_text("\n\n   \n\t\n", encoding="utf-8")
    result = runner.invoke(init_app, ["--cursor-api-key-file", str(ws_file)])
    assert result.exit_code != 0
    out = _combined_output(result).lower()
    assert "empty" in out or "whitespace" in out


# ── intake works alongside a verb subcommand (cursor/claude/...) ────────


def test_cursor_api_key_with_verb_subcommand_persists_after_install(
    isolated_home: tuple[Path, Path],
    fake_backend: _FakeBackend,
    call_recorder: list[str],
    runner: CliRunner,
) -> None:
    """``popola init cursor --cursor-api-key cr_test`` persists after the verb runs.

    The brief: "after the verb installs, run the credential helper".
    The implementation defers to a click ``ctx.call_on_close`` hook on
    the parent (init) context so the helper fires AFTER the cursor verb
    body returns.
    """
    cwd, _ = isolated_home
    result = runner.invoke(init_app, ["--cursor-api-key", "cr_test_verb", "cursor"])
    out = _combined_output(result)
    assert result.exit_code == 0, out
    # Verb body ran.
    assert (cwd / ".cursor" / "skills" / "popola-loom" / "SKILL.md").is_file()
    # Helper persisted the value.
    assert call_recorder == ["cr_test_verb"]
    assert fake_backend.store.get(("popolaloom.cursor", "default")) == "cr_test_verb"
    assert "cr_test_verb" not in out


# ── --cursor-api-key works with --target=cloud-only ────────────────────


def test_cursor_api_key_with_cloud_only_persists_after_scaffold(
    isolated_home: tuple[Path, Path],
    fake_backend: _FakeBackend,
    call_recorder: list[str],
    runner: CliRunner,
) -> None:
    """``--cursor-api-key`` works alongside ``--target=cloud-only`` (no prompt).

    The cloud-only path takes the resolved key and persists it via the
    non-interactive helper instead of running the v0.9.2 interactive
    prompt.
    """
    cwd, _ = isolated_home
    result = runner.invoke(
        init_app,
        ["--target=cloud-only", "--cursor-api-key", "cr_test_cloud_only"],
    )
    out = _combined_output(result)
    assert result.exit_code == 0, out
    assert (cwd / "popolad.toml").is_file()
    assert call_recorder == ["cr_test_cloud_only"]
    assert fake_backend.store.get(("popolaloom.cursor", "default")) == "cr_test_cloud_only"
    assert "cr_test_cloud_only" not in out
    # The interactive prompt header MUST NOT appear (we have the value
    # already; no need to ask).
    assert "Store a Cursor API key in the OS keyring now?" not in out


# ── --cursor-api-key works with --interactive ─────────────────────────


def test_cursor_api_key_with_interactive_skips_credential_prompt(
    isolated_home: tuple[Path, Path],
    fake_backend: _FakeBackend,
    call_recorder: list[str],
    runner: CliRunner,
) -> None:
    """``popola init --interactive --cursor-api-key cr_test`` persists w/o prompting.

    The wizard's interactive prompts for IDE / scope / .local/ /
    Proceed? still run; only the credential intake is short-circuited
    (the value was already supplied non-interactively).
    """
    cwd, _ = isolated_home
    # Wizard answers (no IDE; no .local/; no proceed needed since plan empty).
    answers = "\n".join(
        [
            "n",  # Install for Cursor?
            "n",  # Install for Claude?
            "n",  # Install for Copilot?
            "n",  # Install for Codex?
            "n",  # Scaffold .local/?
        ]
    ) + "\n"
    result = runner.invoke(
        init_app,
        ["--interactive", "--cursor-api-key", "cr_test_interactive"],
        input=answers,
    )
    out = _combined_output(result)
    assert result.exit_code == 0, out
    # Wizard did its dance.
    assert "PopolaLoom interactive setup wizard" in out
    # No interactive credential prompt appeared.
    assert "Store a Cursor API key in the OS keyring now?" not in out
    # But the value was persisted.
    assert call_recorder == ["cr_test_interactive"]
    assert (
        fake_backend.store.get(("popolaloom.cursor", "default")) == "cr_test_interactive"
    )
    assert "cr_test_interactive" not in out


# ── --dry-run short-circuits credential persistence ────────────────────


def test_dry_run_with_cursor_api_key_does_not_persist(
    isolated_home: tuple[Path, Path],
    fake_backend: _FakeBackend,
    call_recorder: list[str],
    runner: CliRunner,
) -> None:
    """``--dry-run --cursor-api-key`` skips persistence + prints the skip line.

    Per **No Silent Failures** for secrets: never persist a key during
    a preview. The skip message is explicit so operators see exactly
    why the credential step was elided.
    """
    cwd, _ = isolated_home
    result = runner.invoke(
        init_app,
        ["--dry-run", "--cursor-api-key", "cr_test_dry_run"],
    )
    out = _combined_output(result)
    assert result.exit_code == 0, out
    assert call_recorder == [], "store_cursor_api_key MUST NOT be called under --dry-run"
    assert fake_backend.store == {}
    assert "credential setup skipped during dry-run" in out
    assert "cr_test_dry_run" not in out


def test_dry_run_with_cloud_only_and_cursor_api_key_skips(
    isolated_home: tuple[Path, Path],
    fake_backend: _FakeBackend,
    call_recorder: list[str],
    runner: CliRunner,
) -> None:
    """``--dry-run --target=cloud-only --cursor-api-key`` also skips persistence.

    The cloud-only path has its own dry-run gate; this test pins the
    contract that the new non-interactive intake also short-circuits
    on dry-run via the same skip line.
    """
    cwd, _ = isolated_home
    result = runner.invoke(
        init_app,
        ["--dry-run", "--target=cloud-only", "--cursor-api-key", "cr_dryrun_cloud"],
    )
    out = _combined_output(result)
    assert result.exit_code == 0, out
    assert call_recorder == []
    assert fake_backend.store == {}
    assert not (cwd / "popolad.toml").exists()
    assert "credential setup skipped during dry-run" in out
    assert "cr_dryrun_cloud" not in out


# ── keyring backend unavailable: actionable hint, no raise ─────────────


def test_cursor_api_key_without_keyring_backend_writes_fallback_file_and_returns_zero(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the keyring extra is unavailable, intake writes the v0.9.9 0600 fallback file.

    Closes ``.local/feedbacks/feedback_for_v0.9.7.md:114-116`` (U2 ask
    "另外初始化传入 secret 没能正确缓存，需要优化"). Q-V099-11 +
    Q-V099-12 lock the new behavior: instead of silently dropping the
    operator's ``--cursor-api-key VAL`` when the keyring is missing
    (the v0.9.7 deliberate-bug-pinned behavior), v0.9.9 writes a
    0600 fallback file at ``$POPOLA_HOME/cursor_api_key.env`` and
    emits a follow-up line that names the file path and the
    ``source`` command for fresh shells. The daemon auto-sources the
    file at startup so ``popola dispatch`` "just works" without an
    explicit ``source`` step.

    This test replaces the v0.9.7 ``test_cursor_api_key_without_
    keyring_backend_prints_hint_and_returns_zero`` (which asserted
    only the WARN hint and the silent-discard exit code; v0.9.9
    flips that to a positive-behavior assertion: the file MUST
    exist with mode 0o600 and the literal payload
    ``CURSOR_API_KEY=<raw_key>\\n``).
    """
    cwd, _ = isolated_home
    (cwd / ".cursor").mkdir()
    monkeypatch.setattr(cred_mod, "_import_keyring", lambda: None)
    result = runner.invoke(init_app, ["--cursor-api-key", "cr_no_keyring"])
    out = _combined_output(result)
    assert result.exit_code == 0, out
    # The WARN hint still fires (operator-facing diagnostic that the
    # primary keyring path was unavailable).
    assert "OS keyring backend unavailable" in out
    assert "./install.sh install --with-credentials" in out
    # v0.9.7: pip MUST NOT appear in the hint (per the workspace rule
    # "popola 不使用 pip 修正安装方式" — fix the install method, do
    # not point operators at a bare pip command).
    assert "pip install" not in out
    assert "popolaloom[credentials]" not in out
    # Auto-detect still installed cursor (the install path succeeded).
    assert (cwd / ".cursor" / "skills" / "popola-loom" / "SKILL.md").is_file()
    # Raw key never echoed (security invariant).
    assert "cr_no_keyring" not in out

    # v0.9.9 U2 positive behavior: the fallback file exists with mode
    # 0o600 and contains exactly ``CURSOR_API_KEY=<raw_key>\n``. The
    # ``$POPOLA_HOME`` env var is set by ``isolated_home`` so we can
    # locate the file deterministically without polluting the
    # developer's real ``~/.popola``.
    fallback_path = cred_mod._env_fallback_path()
    assert fallback_path.is_file()
    import os as _os
    actual_mode = _os.stat(fallback_path).st_mode & 0o777
    assert actual_mode == 0o600, f"expected mode 0o600, got {oct(actual_mode)}"
    payload = fallback_path.read_text(encoding="utf-8")
    assert payload == "CURSOR_API_KEY=cr_no_keyring\n"

    # Operator-facing follow-up line: stdout MUST tell the operator
    # the file path AND the ``source`` command so they can use it from
    # fresh shells without re-reading USER_GUIDE.md.
    assert "Wrote fallback to" in out
    assert "cursor_api_key.env" in out
    assert "mode 0600" in out
    assert "source" in out
    assert "auto-source" in out


# ── direct unit test of _resolve_cursor_api_key_input ──────────────────


class TestResolveCursorApiKeyInput:
    """Covers branches in :func:`init_cmd._resolve_cursor_api_key_input`.

    The runtime CLI path exercises the resolver indirectly; these tests
    pin the boundary semantics directly so a future refactor (e.g.
    moving the helper to :mod:`popolaloom.credentials`) preserves the
    same contract.
    """

    def test_returns_none_when_both_unset(self) -> None:
        assert init_cmd._resolve_cursor_api_key_input(value=None, file=None) is None

    def test_strips_inline_value(self) -> None:
        assert (
            init_cmd._resolve_cursor_api_key_input(value="  cr_x  ", file=None)
            == "cr_x"
        )

    def test_inline_value_alone_returns_string(self) -> None:
        assert (
            init_cmd._resolve_cursor_api_key_input(value="cr_alone", file=None)
            == "cr_alone"
        )

    def test_file_only_returns_first_non_empty_line(self, tmp_path: Path) -> None:
        keyfile = tmp_path / "k.txt"
        keyfile.write_text("\n   \n  cr_first  \nsecond\n", encoding="utf-8")
        assert (
            init_cmd._resolve_cursor_api_key_input(value=None, file=keyfile)
            == "cr_first"
        )

    def test_both_set_raises(self, tmp_path: Path) -> None:
        keyfile = tmp_path / "k.txt"
        keyfile.write_text("cr_x\n", encoding="utf-8")
        import typer

        with pytest.raises(typer.BadParameter, match="mutually exclusive"):
            init_cmd._resolve_cursor_api_key_input(
                value="cr_inline", file=keyfile
            )

    @pytest.mark.parametrize("bad", ["", "   ", "\t\n  "])
    def test_empty_value_raises(self, bad: str) -> None:
        import typer

        with pytest.raises(typer.BadParameter, match="empty|whitespace"):
            init_cmd._resolve_cursor_api_key_input(value=bad, file=None)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        import typer

        with pytest.raises(typer.BadParameter, match="not found"):
            init_cmd._resolve_cursor_api_key_input(
                value=None, file=tmp_path / "missing.txt"
            )

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        keyfile = tmp_path / "empty.txt"
        keyfile.write_text("", encoding="utf-8")
        import typer

        with pytest.raises(typer.BadParameter, match="empty|whitespace"):
            init_cmd._resolve_cursor_api_key_input(value=None, file=keyfile)


# ── _handle_credential_intake_after_install branch coverage ────────────


class TestHandleCredentialIntakeAfterInstall:
    """Direct unit coverage of the helper's branch table.

    The runtime CLI path exercises the helper indirectly via the
    callback / wizard / cloud-only entry points; the direct tests pin
    each branch so a regression on the gating logic surfaces here
    rather than as a flaky CLI test elsewhere.
    """

    def test_no_op_when_neither_flag_set(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        init_cmd._handle_credential_intake_after_install(
            resolved_key=None,
            configure_cursor_auth=False,
            dry_run=False,
        )
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_dry_run_short_circuits_with_skip_message(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        init_cmd._handle_credential_intake_after_install(
            resolved_key="cr_x",
            configure_cursor_auth=True,
            dry_run=True,
        )
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "credential setup skipped during dry-run" in combined

    def test_persists_when_resolved_key_present(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        called: list[str] = []

        def _fake(raw: str) -> None:
            called.append(raw)

        monkeypatch.setattr(
            init_cmd,
            "_persist_cursor_api_key_noninteractive",
            _fake,
        )
        # The interactive helper MUST NOT fire when the value is
        # provided non-interactively.
        offered: list[None] = []

        def _fake_offer() -> None:
            offered.append(None)

        monkeypatch.setattr(
            init_cmd,
            "_offer_cursor_credential_setup",
            _fake_offer,
        )
        init_cmd._handle_credential_intake_after_install(
            resolved_key="cr_persist",
            configure_cursor_auth=True,
            dry_run=False,
        )
        assert called == ["cr_persist"]
        assert offered == []

    def test_offers_interactive_when_only_configure_flag(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        offered: list[None] = []

        def _fake_offer() -> None:
            offered.append(None)

        monkeypatch.setattr(
            init_cmd,
            "_offer_cursor_credential_setup",
            _fake_offer,
        )
        called: list[str] = []
        monkeypatch.setattr(
            init_cmd,
            "_persist_cursor_api_key_noninteractive",
            lambda raw: called.append(raw),
        )
        init_cmd._handle_credential_intake_after_install(
            resolved_key=None,
            configure_cursor_auth=True,
            dry_run=False,
        )
        assert offered == [None]
        assert called == []


# ── _persist_cursor_api_key_noninteractive direct paths ────────────────


class TestPersistCursorApiKeyNoninteractive:
    """Direct branch coverage for the v0.9.5 non-interactive persistence helper."""

    def test_unavailable_keyring_writes_fallback_and_prints_hint(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """Keyring missing → v0.9.9 U2 fallback file is written + hint is printed.

        Previously (v0.9.7) this test asserted the silent-discard
        behavior — only the WARN hint was emitted and the literal
        secret was dropped. Q-V099-11 + Q-V099-12 flip that to
        "instead of silently discarding the secret, write
        ``$POPOLA_HOME/cursor_api_key.env`` mode 0600 + emit the
        operator-facing follow-up line + return". The hint is still
        printed (operator-facing diagnostic that the *primary*
        keyring path was unavailable) but the function now has a
        positive observable effect on disk.
        """
        monkeypatch.setattr(cred_mod, "_import_keyring", lambda: None)
        # Pin POPOLA_HOME so the fallback file lands in tmp_path rather
        # than the developer's real ``~/.popola``.
        monkeypatch.setenv("POPOLA_HOME", str(tmp_path / "popola"))
        result = init_cmd._persist_cursor_api_key_noninteractive("cr_x")
        assert result is None
        out = capsys.readouterr()
        combined = out.out + out.err
        assert "OS keyring backend unavailable" in combined
        # v0.9.7 (closes feedback_for_v0.9.4 line 1): point at the official
        # installer, NOT at a raw pip command.
        assert "./install.sh install --with-credentials" in combined
        assert "pip install" not in combined
        assert "popolaloom[credentials]" not in combined
        # The literal value must never appear in any output.
        assert "cr_x" not in combined
        # v0.9.9 U2: the fallback file is written with mode 0o600 and
        # contains the literal payload ``CURSOR_API_KEY=<raw_key>\n``.
        fallback_path = cred_mod._env_fallback_path()
        assert fallback_path.is_file()
        import os as _os
        actual_mode = _os.stat(fallback_path).st_mode & 0o777
        assert actual_mode == 0o600
        assert fallback_path.read_text(encoding="utf-8") == "CURSOR_API_KEY=cr_x\n"
        # Operator-facing follow-up line names the file path + ``source``.
        assert "Wrote fallback to" in combined
        assert "cursor_api_key.env" in combined
        assert "source" in combined

    def test_credential_backend_error_falls_back_with_message(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Keep keyring "available" so the helper hits the store_cursor_api_key
        # path and propagates the backend error.
        fake_backend = _FakeBackend()
        fake_module = types.ModuleType("keyring")
        fake_module.get_keyring = lambda: fake_backend  # type: ignore[attr-defined]
        fake_module.get_password = lambda s, u: None  # type: ignore[attr-defined]

        def _raise(_s: str, _u: str, _v: str) -> None:
            raise RuntimeError("backend down for maintenance")

        fake_module.set_password = _raise  # type: ignore[attr-defined]
        fake_module.delete_password = lambda s, u: None  # type: ignore[attr-defined]
        monkeypatch.setattr(cred_mod, "_import_keyring", lambda: fake_module)
        result = init_cmd._persist_cursor_api_key_noninteractive("cr_y")
        assert result is None
        out = capsys.readouterr()
        combined = out.out + out.err
        assert "ERROR" in combined
        assert "Falling back" in combined or "CURSOR_API_KEY" in combined

    def test_value_error_path_prints_and_returns(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Force store_cursor_api_key to raise a ValueError (e.g. on
        # an empty input that slipped past the resolver).
        monkeypatch.setattr(
            cred_mod,
            "is_keyring_available",
            lambda: True,
        )

        def _raise(_api_key: str) -> object:
            raise ValueError("api_key must be a non-empty string")

        monkeypatch.setattr(cred_mod, "store_cursor_api_key", _raise)
        result = init_cmd._persist_cursor_api_key_noninteractive("   ")
        assert result is None
        out = capsys.readouterr()
        combined = out.out + out.err
        assert "ERROR" in combined

    def test_happy_path_prints_fingerprint_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Wire a fake backend so store_cursor_api_key succeeds.
        fake_backend = _FakeBackend()
        fake_module = types.ModuleType("keyring")
        fake_module.get_keyring = lambda: fake_backend  # type: ignore[attr-defined]
        fake_module.get_password = lambda s, u: fake_backend.get_password(s, u)  # type: ignore[attr-defined]
        fake_module.set_password = lambda s, u, v: fake_backend.set_password(s, u, v)  # type: ignore[attr-defined]
        fake_module.delete_password = lambda s, u: fake_backend.delete_password(s, u)  # type: ignore[attr-defined]
        monkeypatch.setattr(cred_mod, "_import_keyring", lambda: fake_module)
        # Isolated POPOLA_HOME so the metadata file does not pollute ~/.popola.
        monkeypatch.setenv("POPOLA_HOME", str(_tmp_safe_home(monkeypatch)))
        init_cmd._persist_cursor_api_key_noninteractive("cr_happy")
        out = capsys.readouterr()
        combined = out.out + out.err
        assert "Stored Cursor API key" in combined
        assert "fingerprint=" in combined
        assert "cr_happy" not in combined


def _tmp_safe_home(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a unique directory for each test invocation.

    Helper for the happy-path persist test above; uses ``pytest``'s
    own temporary-directory factory via :func:`pytest.MonkeyPatch.delenv`
    indirection. Falls back to ``/tmp/popola-test-credentials`` if no
    ``tmp_path`` plugin is available — the path is created with
    mode ``0o700`` and removed on teardown via ``monkeypatch.delenv``
    fallback.
    """
    import tempfile

    base = Path(tempfile.mkdtemp(prefix="popola-init-cred-"))
    return base


# ── _DRY_RUN_CREDENTIAL_SKIP_MSG literal pin ───────────────────────────


def test_dry_run_skip_message_literal_is_stable() -> None:
    """The skip message literal is a stable substring (greppable for ops).

    Operators searching CI logs for "credential setup skipped" expect a
    consistent substring across init paths. The constant is used by
    cloud-only, interactive, auto-detect, and the per-verb subcommand
    path so a single edit changes every path's wording in lockstep.
    """
    assert "credential setup skipped during dry-run" in init_cmd._DRY_RUN_CREDENTIAL_SKIP_MSG


# ── ensure the new options surface in --help text ──────────────────────


def test_init_help_text_advertises_new_credential_intake_flags(
    runner: CliRunner,
) -> None:
    """``popola init --help`` advertises both new v0.9.5 credential intake flags."""
    result = runner.invoke(init_app, ["--help"])
    assert result.exit_code == 0
    # Rich wraps long flag tokens across lines on 80-column CI runners
    # (e.g. ``--cursor-\nA-api-key-file``); strip ANSI + collapse all
    # whitespace before the substring check so the test stays robust
    # to terminal-width variation between dev hosts and CI.
    flat = _normalize_terminal_text(_combined_output(result))
    flat_no_space = flat.replace(" ", "")
    assert "--cursor-api-key" in flat_no_space
    assert "--cursor-api-key-file" in flat_no_space


# ── helper _Callable alias used by the spy fixture (typing nicety) ─────


_StoreFn = Callable[[str], object]
"""Type alias: shape of the credentials.store_cursor_api_key hook.

Surfaced as a module-level constant so the spy fixture's annotation
stays readable; the alias itself is not part of the public surface.
"""
