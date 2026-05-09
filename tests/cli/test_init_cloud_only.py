"""Tests for ``popola init --target=cloud-only`` (v0.9.0 W2.4 — Q-D-4 偏离默认: 必做).

The cloud-only mode scaffolds a *minimal* Cursor-Cloud-Agent-only project
skeleton at the project root. Per the v0.9.0 plan §W2.4 the scaffold drops
exactly three files (``popolad.toml`` / ``.env.example`` / ``Makefile``)
and **never** creates local-CLI shims, IDE skill installs, or the
``.local/`` workspace. The default ``--target=full`` mode keeps the
historical 14-row verb + 8-modifier matrix byte-for-byte.

This file pins six contract surfaces (one per acceptance bullet from the
W2.4 task brief, plus a few mutation-targeting branches):

1. **File set** — ``--target=cloud-only`` writes exactly the three
   expected files at ``tmp_path``; nothing else.
2. **Local-HITL excluded** — the rendered ``popolad.toml`` contains
   ``[hitl.cloud]`` but **not** the bare ``[hitl]`` section emitted by
   ``--target=full``.
3. **CURSOR_API_KEY surfaced** — the rendered ``.env.example`` mentions
   ``CURSOR_API_KEY`` and the ``popola dispatch --cli=cursor-cloud``
   entrypoint, so a fresh operator does not have to grep the source.
4. **Makefile shortcuts** — the rendered ``Makefile`` exposes
   ``make dispatch`` / ``make status`` / ``make relay`` (and
   ``make attach`` for symmetry).
5. **Default mode regression** — ``popola init`` with no ``--target`` (or
   ``--target=full``) still scaffolds via the original ``_install_target``
   / ``_install_local`` paths; this test guards against accidental
   sub-mode bleed (the new branch must not steal the default lane).
6. **Idempotency + --force** — second invocation prints SKIP per file
   (operator edits preserved); ``--force`` overwrites the scaffold's
   default content. Mirrors the existing init verb's idempotency
   contract.

The suite also covers the negative paths (``--target=invalid`` exits 2
with an explicit error; ``--target=cloud-only --list`` rejects the
combination; the dry-run flag emits DRY lines without touching disk).

Per the workspace **No Silent Failures** rule, every assertion is
explicit (``assert ... in output, output``) so failures point at the
exact contract bullet that broke.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from popolaloom.cli.init_cmd import (
    _CLOUD_ONLY_FILES,
    InitTarget,
    _install_cloud_only,
)
from popolaloom.cli.init_cmd import app as init_app


@pytest.fixture
def isolated_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, Path]]:
    """Yield ``(cwd, fake_home)`` with ``Path.home()`` + ``Path.cwd()`` patched.

    The cloud-only scaffold writes everything under ``cwd`` (no
    ``Path.home()`` lookups — that's deliberately the ``--target=full``
    surface), but the fixture still patches ``Path.home()`` because the
    regression tests in this file invoke the legacy verbs and those
    *do* hit the home directory.

    The fixture also unsets ``CURSOR_API_KEY`` so the cloud-only env
    template renders the placeholder value cleanly without leaking the
    developer's real shell env into the scaffold.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)

    yield cwd, fake_home


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _combined_output(result: object) -> str:
    """Return ``result.stdout`` + best-effort ``result.stderr``.

    Mirrors the helper in ``test_init_cmd.py`` so failure messages
    surface the same way regardless of which click 8.x line is in
    effect (some versions populate ``result.stdout`` only when
    ``mix_stderr`` is supported).
    """
    stdout = getattr(result, "stdout", "") or ""
    try:
        stderr = getattr(result, "stderr", "") or ""
    except (ValueError, AttributeError):
        stderr = ""
    output = getattr(result, "output", "") or ""
    return stdout + stderr + output


# ── happy path: file set ────────────────────────────────────────────────


def test_cloud_only_creates_expected_three_files(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``popola init --target=cloud-only`` writes exactly three files.

    AC (a): ``popolad.toml`` + ``.env.example`` + ``Makefile`` at the
    project root. No ``.local/`` scaffold, no IDE shim, no
    ``.cursor/skills/`` install.
    """
    cwd, _fake_home = isolated_home
    result = runner.invoke(init_app, ["--target=cloud-only"])
    assert result.exit_code == 0, _combined_output(result)

    expected = (
        cwd / "popolad.toml",
        cwd / ".env.example",
        cwd / "Makefile",
    )
    for path in expected:
        assert path.is_file(), f"missing expected cloud-only file: {path}"

    assert not (cwd / ".local").exists(), (
        "cloud-only must NOT scaffold .local/ — that's the --target=full surface"
    )
    assert not (cwd / ".cursor").exists(), (
        "cloud-only must NOT install IDE shims (no .cursor/ either)"
    )
    assert not (cwd / ".claude").exists()
    assert not (cwd / ".github").exists()


# ── popolad.toml content shape ──────────────────────────────────────────


def test_cloud_only_popolad_toml_excludes_local_hitl_section(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """Cloud-only ``popolad.toml`` carries ``[hitl.cloud]`` but **NOT** ``[hitl]``.

    AC (a) §"NO local-only [hitl] or local CLI config": the scaffold's
    daemon config must register the cloud HITL bridge only — no local
    Lark listener / MCP stdio worker (those land via ``--target=full``
    and live in a separate checkout per the disjoint-layout note in
    the scaffold's own docstring).
    """
    cwd, _fake_home = isolated_home
    result = runner.invoke(init_app, ["--target=cloud-only"])
    assert result.exit_code == 0, _combined_output(result)

    body = (cwd / "popolad.toml").read_text(encoding="utf-8")

    assert "[hitl.cloud]" in body, "expected [hitl.cloud] section in cloud-only popolad.toml"
    assert "[cloud.backoff]" in body
    assert "[cloud.busy_strategy]" in body
    assert "[cloud.relay]" in body

    for line in body.splitlines():
        stripped = line.strip()
        assert stripped != "[hitl]", (
            "cloud-only popolad.toml must NOT contain a bare [hitl] section "
            "(local-tier HITL is forbidden in cloud-only init); "
            f"offending line: {line!r}"
        )


def test_cloud_only_env_example_mentions_cursor_api_key(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """Cloud-only ``.env.example`` surfaces the ``CURSOR_API_KEY`` placeholder.

    AC (a) §".env.example with CURSOR_API_KEY placeholder + comment
    pointing to popola dispatch --cli=cursor-cloud".
    """
    cwd, _fake_home = isolated_home
    result = runner.invoke(init_app, ["--target=cloud-only"])
    assert result.exit_code == 0, _combined_output(result)

    body = (cwd / ".env.example").read_text(encoding="utf-8")
    assert "CURSOR_API_KEY" in body, (
        "cloud-only .env.example must mention CURSOR_API_KEY (operator "
        "needs to know which env var to fill in)"
    )
    assert "cursor-cloud" in body, (
        "cloud-only .env.example must reference the cursor-cloud entrypoint "
        "(the only meaningful dispatch path in this layout)"
    )
    assert "popola dispatch" in body


def test_cloud_only_makefile_exposes_cloud_flow_targets(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """Cloud-only ``Makefile`` declares ``dispatch`` / ``status`` / ``relay`` / ``attach`` targets.

    AC (a) §"Makefile (or tasks.toml) with cloud-flow shortcuts (make
    dispatch, make status, make relay)".
    """
    cwd, _fake_home = isolated_home
    result = runner.invoke(init_app, ["--target=cloud-only"])
    assert result.exit_code == 0, _combined_output(result)

    body = (cwd / "Makefile").read_text(encoding="utf-8")
    for target in ("dispatch:", "status:", "relay:", "attach:"):
        assert target in body, f"cloud-only Makefile missing target: {target}"

    assert "popola dispatch" in body and "--cli=cursor-cloud" in body, (
        "Makefile dispatch target must wrap `popola dispatch --cli=cursor-cloud`"
    )


# ── default-mode regression ─────────────────────────────────────────────


def test_target_full_default_preserves_auto_detect_branch(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``popola init`` (no ``--target``) keeps the historical scaffold.

    AC (b): the v0.5.0 14-row verb + 8-modifier matrix is intact;
    cloud-only's three-file drop must not bleed into the default lane.
    Pre-create ``.cursor/`` and ``.claude/`` so auto-detect dispatches
    both IDE installs (mirrors
    ``test_init_cmd.py::test_init_no_args_auto_detects_targets``); this
    is the canonical "auto-detect lane" — the regression we're guarding
    is "cloud-only branch silently steals the IDE install" not "the
    fallback-to-cursor branch loses .local/".
    """
    cwd, _fake_home = isolated_home
    (cwd / ".cursor").mkdir()
    (cwd / ".claude").mkdir()

    result = runner.invoke(init_app, [])
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)

    assert "target: full" in out or "auto-detected" in out, (
        f"default mode should announce target=full or auto-detected; output:\n{out}"
    )
    assert (cwd / ".cursor" / "skills" / "popola-loom" / "SKILL.md").is_file(), (
        "default --target=full should still install the cursor SKILL.md via auto-detect"
    )
    assert (cwd / ".claude" / "skills" / "popola-loom" / "SKILL.md").is_file()

    assert not (cwd / "popolad.toml").exists(), (
        "default --target=full must NOT drop the cloud-only popolad.toml "
        "(that's the cloud-only-mode surface)"
    )
    assert not (cwd / ".env.example").exists()
    assert not (cwd / "Makefile").exists()


def test_target_full_explicit_falls_back_to_cursor_on_empty_repo(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``--target=full`` explicit on a fresh repo hits the cursor fallback.

    AC (c) §"--target accepts cloud-only | full (default: full)": the
    explicit-full path must reach the same callback branch as no-flag.
    Pre-create ``.local/`` so ``_auto_detect`` returns ``[]`` and the
    "No AI tools detected" fallback fires (installs cursor by default).
    Mirrors ``test_init_cmd_edge_cases.py::test_init_auto_detect_no_ides_falls_back_to_cursor``
    for the ``--target=full`` lane specifically.
    """
    cwd, _fake_home = isolated_home
    (cwd / ".local").mkdir()

    result = runner.invoke(init_app, ["--target=full"])
    assert result.exit_code == 0, _combined_output(result)

    out = _combined_output(result)
    assert "No AI tools detected" in out, (
        f"--target=full on empty repo should hit the cursor fallback; output:\n{out}"
    )
    assert (cwd / ".cursor" / "skills" / "popola-loom" / "SKILL.md").is_file()

    assert not (cwd / "popolad.toml").exists()
    assert not (cwd / "Makefile").exists()


# ── error / negative paths ──────────────────────────────────────────────


def test_target_invalid_value_exits_with_clear_error(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``--target=bogus`` exits non-zero (Typer Enum auto-validation).

    AC (c) §"explicit error on unknown value": Typer surfaces a
    UsageError with exit code 2; the error message names the rejected
    value so operators see exactly what was wrong.
    """
    result = runner.invoke(init_app, ["--target=bogus"])
    assert result.exit_code != 0
    assert result.exit_code == 2, (
        f"--target=invalid should exit 2 (UsageError); got {result.exit_code}; "
        f"output:\n{_combined_output(result)}"
    )

    out = _combined_output(result).lower()
    assert "bogus" in out or "invalid" in out or "is not one of" in out or "must be" in out, (
        f"error must surface the rejected value or list the valid choices; output:\n"
        f"{_combined_output(result)}"
    )


def test_cloud_only_with_list_flag_errors(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``--target=cloud-only --list`` is a usage error.

    The two flags are mutually exclusive: ``--list`` is a fresh-detect
    info dump for the legacy verb matrix; cloud-only init owns its own
    output surface. Mixing them would silently strip the cloud-only
    scaffold (No Silent Failures).
    """
    result = runner.invoke(init_app, ["--target=cloud-only", "--list"])
    assert result.exit_code != 0, _combined_output(result)
    assert "cannot be combined" in _combined_output(result).lower()


def test_cloud_only_with_subcommand_errors(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``popola init cursor --target=cloud-only`` is a usage error.

    The cloud-only target replaces the entire verb matrix; mixing the
    two surfaces would scaffold a half-cloud-half-cursor layout that
    nothing supports.
    """
    result = runner.invoke(init_app, ["--target=cloud-only", "cursor"])
    assert result.exit_code != 0, _combined_output(result)
    assert "cannot be combined" in _combined_output(result).lower()


# ── idempotency + --force ───────────────────────────────────────────────


def test_cloud_only_idempotent_second_run_skips(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """Second ``popola init --target=cloud-only`` run prints SKIP per file.

    AC (e): the scaffold preserves the existing init's idempotency
    contract — operator edits to any of the three scaffold files
    survive a re-run as long as ``--force`` is absent.
    """
    cwd, _fake_home = isolated_home

    first = runner.invoke(init_app, ["--target=cloud-only"])
    assert first.exit_code == 0

    user_marker = cwd / "popolad.toml"
    user_marker.write_text("# operator-edited content\n", encoding="utf-8")
    mtime_before = user_marker.stat().st_mtime

    second = runner.invoke(init_app, ["--target=cloud-only"])
    assert second.exit_code == 0, _combined_output(second)

    out = _combined_output(second)
    assert "SKIP" in out, f"second cloud-only run should print SKIP per file; output:\n{out}"
    assert "use --force" in out or "--force to overwrite" in out, (
        f"second cloud-only run should advertise --force escape hatch; output:\n{out}"
    )

    assert user_marker.read_text(encoding="utf-8") == "# operator-edited content\n", (
        "operator-edited popolad.toml must be preserved on re-run without --force"
    )
    assert user_marker.stat().st_mtime == mtime_before


def test_cloud_only_force_overwrites_existing_files(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``--target=cloud-only --force`` overwrites operator edits.

    AC (f): ``--force`` matches the existing init flag's semantics —
    when the operator wants to reset the scaffold to the canonical
    template (e.g. after a bad first edit), ``--force`` writes all three
    files unconditionally.
    """
    cwd, _fake_home = isolated_home

    first = runner.invoke(init_app, ["--target=cloud-only"])
    assert first.exit_code == 0

    user_marker = cwd / "popolad.toml"
    user_marker.write_text("# OLD CONTENT TO BE OVERWRITTEN\n", encoding="utf-8")

    second = runner.invoke(init_app, ["--target=cloud-only", "--force"])
    assert second.exit_code == 0, _combined_output(second)

    out = _combined_output(second)
    assert "OK" in out, f"--force second run should print OK (not SKIP); output:\n{out}"

    new_body = user_marker.read_text(encoding="utf-8")
    assert "# OLD CONTENT TO BE OVERWRITTEN" not in new_body, (
        "--force must overwrite the operator's edits"
    )
    assert "[hitl.cloud]" in new_body, "--force must write the canonical cloud-only template"


# ── --dry-run ──────────────────────────────────────────────────────────


def test_cloud_only_dry_run_writes_nothing(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """``--target=cloud-only --dry-run`` prints DRY but writes no files.

    Mirrors the existing ``init <verb> --dry-run`` contract; lets CI
    smoke a freshly-checked-out repo without spilling any file
    artefacts on disk.
    """
    cwd, _fake_home = isolated_home
    result = runner.invoke(init_app, ["--target=cloud-only", "--dry-run"])
    assert result.exit_code == 0, _combined_output(result)

    out = _combined_output(result)
    assert "DRY" in out, f"--dry-run should print DRY lines; output:\n{out}"

    assert not (cwd / "popolad.toml").exists()
    assert not (cwd / ".env.example").exists()
    assert not (cwd / "Makefile").exists()


# ── output surface ──────────────────────────────────────────────────────


def test_cloud_only_output_announces_target_selection(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
) -> None:
    """Cloud-only output stream clearly announces ``cloud-only``.

    AC (d): "Output to stdout/stderr clearly indicates which target is
    selected" — operators reading the log stream after the fact must
    be able to grep for the target literal without reading the source.
    """
    cwd, _fake_home = isolated_home
    result = runner.invoke(init_app, ["--target=cloud-only"])
    assert result.exit_code == 0, _combined_output(result)

    out = _combined_output(result)
    assert "cloud-only" in out, (
        f"output must include the literal token 'cloud-only'; output:\n{out}"
    )


# ── direct helper coverage (mutmut-friendly) ────────────────────────────


def test_install_cloud_only_helper_creates_files(tmp_path: Path) -> None:
    """``_install_cloud_only(cwd, dry_run=False, force=False)`` writes 3 files.

    Direct unit test for the helper so mutmut mutations on the per-file
    loop (e.g. ``for relative_path, content in _CLOUD_ONLY_FILES`` →
    early-break) get caught. The Typer wrapper exercises the same code
    path, but a direct call is faster and more diagnostic.
    """
    _install_cloud_only(tmp_path, dry_run=False, force=False)
    for relative_path, _content in _CLOUD_ONLY_FILES:
        assert (tmp_path / relative_path).is_file(), (
            f"_install_cloud_only failed to create {relative_path}"
        )


def test_init_target_enum_has_two_members() -> None:
    """``InitTarget`` declares exactly ``FULL`` + ``CLOUD_ONLY`` (no extras).

    AC (c): the ``--target`` flag accepts ``cloud-only | full`` and
    nothing else. This pins the enum membership so a future contributor
    cannot silently add a third value (which would widen the public
    surface without a corresponding spec update).
    """
    assert {member.value for member in InitTarget} == {"full", "cloud-only"}
    assert InitTarget.FULL.value == "full"
    assert InitTarget.CLOUD_ONLY.value == "cloud-only"


# ── env var mock for CURSOR_API_KEY independence ────────────────────────


def test_cloud_only_template_independent_of_environment_variable(
    isolated_home: tuple[Path, Path],
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloud-only ``.env.example`` template body does not interpolate ``CURSOR_API_KEY``.

    AC (h) hint: "mock os.environ for CURSOR_API_KEY checks if needed".
    The scaffold template is a static string with an empty placeholder
    (``CURSOR_API_KEY=``) regardless of whether the developer's shell
    has the var set; this test enforces that contract by setting a
    bogus value and confirming it does NOT leak into the rendered
    template (No Silent Failures: copying the user's secret into a
    committed file would be a credential-leak hazard).
    """
    cwd, _fake_home = isolated_home
    monkeypatch.setenv("CURSOR_API_KEY", "cr_some_secret_token_must_not_appear_in_scaffold")

    result = runner.invoke(init_app, ["--target=cloud-only"])
    assert result.exit_code == 0, _combined_output(result)

    body = (cwd / ".env.example").read_text(encoding="utf-8")
    assert "cr_some_secret_token_must_not_appear_in_scaffold" not in body, (
        "scaffolded .env.example must NEVER interpolate the developer's "
        "CURSOR_API_KEY (would leak the secret into a committed file)"
    )
    for line in body.splitlines():
        if line.startswith("CURSOR_API_KEY="):
            value = line.split("=", 1)[1]
            assert value == "", f"CURSOR_API_KEY placeholder must be empty; got: {line!r}"
            break
    else:
        pytest.fail("scaffolded .env.example missing the CURSOR_API_KEY=<placeholder> line")
