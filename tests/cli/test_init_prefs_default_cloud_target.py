"""v0.10.0 Wave B2 — wizard prompt + ``--set`` parser for ``default_cloud_target``.

Covers PLAN.md Wave B2 acceptance criteria 1-5 verbatim:

1. :func:`apply_user_preference_sets` accepts ``--set default_cloud_target=...``
   per the same validation pattern as ``default_runtime``.
2. The wizard prompt sequence asks ``default_cloud_target`` ONLY when the
   just-answered ``default_runtime`` is ``cloud`` OR ``ask-each-time``; the
   prompt shows three labeled options plus a ``q/ESC`` skip.
3. When ``default_runtime=local``, the wizard SKIPS the
   ``default_cloud_target`` question entirely (no implicit value written —
   the field stays at its default ``"ask-each-time"`` from B1's loader).
4. The wizard's final summary screen shows the chosen value (or
   ``"(skipped)"`` when ``default_runtime=local`` OR when user typed ``q``).
5. Tests cover: prompt-shown branch (runtime=cloud), prompt-skipped branch
   (runtime=local), ``q/ESC`` skip during the prompt, valid-value
   persistence, invalid-value rejection (re-prompts).

References
----------
- ``.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md`` Q-5
- ``.local/.agent/active/v0.10.0-cloud-dispatch-clarity/PLAN.md`` §"Wave B → Task B2"
- B1's loader contract on ``daemon/main.py``'s ``UserPreferencesConfig``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from popolaloom.cli.init_cmd import (
    app as init_app,
)
from popolaloom.cli.init_cmd import (
    apply_user_preference_sets,
    load_user_preferences_for_cli,
)
from popolaloom.daemon.main import USER_PREF_VALID_DEFAULT_CLOUD_TARGET

# ---------------------------------------------------------------------------
# Fixtures (mirror tests/cli/test_init_prefs.py — isolated $POPOLA_HOME +
# fake $HOME so the operator's real config is never touched).
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_popola_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    home = tmp_path / "home"
    home.mkdir()
    popola_home = tmp_path / "popola"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("POPOLA_HOME", str(popola_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(project)
    yield popola_home


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _combined_output(result: object) -> str:
    """Combine stdout / stderr / output (mirrors tests/cli/test_init_prefs.py)."""
    parts: list[str] = []
    for attr in ("stdout", "stderr", "output"):
        try:
            value = getattr(result, attr, "") or ""
        except (ValueError, AttributeError):
            value = ""
        if value and value not in parts:
            parts.append(value)
    return "".join(parts)


_PROMPT_LABEL: str = (
    "Default cloud target "
    "[self-hosted | cursor-managed | ask-each-time, q to skip]"
)
"""Verbatim prompt label per PLAN B2 AC 2 — used both for present/absent
assertions in the wizard tests."""


def _wizard_base_answers() -> list[str]:
    """Return the IDE-install + .local-scaffold + proceed-plan answers.

    Installs Cursor (project scope) so the wizard does NOT short-circuit at
    the "Nothing selected. Wizard exiting without changes." early-return
    branch; we need to reach Step 6 (preferences) to exercise B2.
    """
    return [
        "y",  # Install Cursor?
        "P",  # Cursor scope -> project.
        "n",  # Install Claude?
        "n",  # Install Copilot?
        "n",  # Install Codex?
        "n",  # Scaffold .local/?
        "y",  # Proceed with plan?
        "y",  # Configure dispatch preferences now?
    ]


def _wizard_tail_answers() -> list[str]:
    """Answers after the (runtime + cloud_target) pair, accepting defaults."""
    return [
        "",  # cloud_target_priority keeps default.
        "",  # default_local_cli keeps default.
        "",  # fallback_chain keeps default.
        "",  # follow_devola_flow keeps default.
        "",  # hitl_enabled keeps default.
        "",  # prompt_each_dispatch keeps default.
        "",  # wait_timeout_s keeps default.
        "",  # cursor output_format keeps default.
        "",  # cursor cli_args keeps default.
        "",  # cursor-cloud model keeps default.
        "",  # auto_create_pr keeps default.
        "",  # work_on_current_branch keeps default.
        "",  # claude max_turns keeps default.
        "",  # codex sandbox keeps default.
        "",  # lark completed keeps default.
        "",  # lark failed keeps default.
        "",  # lark canceled keeps default.
        "",  # lark cancel escalated keeps default.
        "",  # lark prompt truncate keeps default.
        "",  # ambiguity_resolution keeps default.
        "",  # ask_dimensions keeps default.
    ]


# ---------------------------------------------------------------------------
# AC 1 — apply_user_preference_sets accepts --set default_cloud_target=...
# (valid-value persistence + invalid-value rejection branches).
# ---------------------------------------------------------------------------


def test_set_default_cloud_target_persists_via_cli(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    """``init prefs --set default_cloud_target=self-hosted`` writes the value
    end-to-end via :func:`apply_user_preference_sets`. Covers PLAN B2 AC 1
    + "valid-value persistence" branch of AC 5."""
    result = runner.invoke(
        init_app,
        ["prefs", "--set", "default_cloud_target=self-hosted"],
    )
    assert result.exit_code == 0, _combined_output(result)
    prefs = load_user_preferences_for_cli()
    assert prefs is not None
    assert prefs.default_cloud_target == "self-hosted"


def test_set_default_cloud_target_accepts_all_three_valid_values(
    isolated_popola_home: Path,
) -> None:
    """The validator accepts every label documented in DECISIONS Q-5."""
    for label in ("self-hosted", "cursor-managed", "ask-each-time"):
        prefs = apply_user_preference_sets([f"default_cloud_target={label}"])
        assert prefs.default_cloud_target == label


def test_set_default_cloud_target_invalid_value_rejected(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    """Invalid ``--set`` value exits non-zero with a clear error message —
    covers the AC 5 "invalid-value rejection" branch on the CLI surface;
    No Silent Failures rule applies (workspace-level)."""
    result = runner.invoke(
        init_app,
        ["prefs", "--set", "default_cloud_target=not-a-real-target"],
    )
    assert result.exit_code != 0
    combined = _combined_output(result)
    assert "default_cloud_target" in combined
    assert "not-a-real-target" in combined
    for valid in USER_PREF_VALID_DEFAULT_CLOUD_TARGET:
        assert valid in combined, (
            f"validator error must enumerate valid value {valid!r} so the "
            f"operator can correct the typo without consulting docs"
        )


# ---------------------------------------------------------------------------
# AC 2 — wizard asks the prompt only when runtime is cloud or ask-each-time.
# AC 5 — prompt-shown branch + valid-value persistence.
# ---------------------------------------------------------------------------


def test_wizard_prompts_default_cloud_target_when_runtime_cloud(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    """Runtime=cloud surfaces the prompt; a typed valid value persists to
    TOML. Covers AC 2 (prompt label shown) + AC 4 (summary shows chosen
    value, not "(skipped)") + AC 5 "prompt-shown branch" + "valid-value
    persistence"."""
    answers = [
        *_wizard_base_answers(),
        "cloud",  # default_runtime -> triggers the NEW prompt.
        "self-hosted",  # default_cloud_target.
        *_wizard_tail_answers(),
    ]
    result = runner.invoke(
        init_app,
        ["--interactive"],
        input="\n".join(answers) + "\n",
    )
    assert result.exit_code == 0, _combined_output(result)
    combined = _combined_output(result)
    assert _PROMPT_LABEL in combined, (
        "expected the verbatim default_cloud_target prompt label per AC 2"
    )
    assert "default_cloud_target = self-hosted" in combined, (
        "expected the wizard summary to show the chosen value per AC 4"
    )
    assert "(skipped)" not in combined.split("Preferences summary:")[-1], (
        "summary line must NOT say '(skipped)' when the operator typed a "
        "valid value"
    )
    prefs = load_user_preferences_for_cli()
    assert prefs is not None
    assert prefs.default_runtime == "cloud"
    assert prefs.default_cloud_target == "self-hosted"


def test_wizard_prompts_default_cloud_target_when_runtime_ask_each_time(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    """The gate is `default_runtime ∈ {cloud, ask-each-time}` — ask-each-time
    also surfaces the prompt (per PLAN B2 AC 2 exact wording)."""
    answers = [
        *_wizard_base_answers(),
        "ask-each-time",  # default_runtime.
        "cursor-managed",  # default_cloud_target.
        *_wizard_tail_answers(),
    ]
    result = runner.invoke(
        init_app,
        ["--interactive"],
        input="\n".join(answers) + "\n",
    )
    assert result.exit_code == 0, _combined_output(result)
    combined = _combined_output(result)
    assert _PROMPT_LABEL in combined
    prefs = load_user_preferences_for_cli()
    assert prefs is not None
    assert prefs.default_runtime == "ask-each-time"
    assert prefs.default_cloud_target == "cursor-managed"


# ---------------------------------------------------------------------------
# AC 3 — wizard SKIPS the prompt entirely when runtime=local.
# AC 4 — summary shows "(skipped)" in that case.
# ---------------------------------------------------------------------------


def test_wizard_skips_default_cloud_target_prompt_when_runtime_local(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    """Runtime=local short-circuits the gate: the prompt is NEVER shown,
    summary marks "(skipped)", and the persisted value stays at the
    loader's default ``"ask-each-time"``. Covers AC 3 + AC 4 (local
    branch) + AC 5 "prompt-skipped branch"."""
    answers = [
        *_wizard_base_answers(),
        "local",  # default_runtime.
        # NO answer for default_cloud_target — it must not be asked.
        *_wizard_tail_answers(),
    ]
    result = runner.invoke(
        init_app,
        ["--interactive"],
        input="\n".join(answers) + "\n",
    )
    assert result.exit_code == 0, _combined_output(result)
    combined = _combined_output(result)
    assert _PROMPT_LABEL not in combined, (
        "AC 3: default_cloud_target prompt must NOT appear when runtime=local"
    )
    assert "default_cloud_target = (skipped)" in combined, (
        "AC 4: summary must mark the skipped branch with '(skipped)'"
    )
    prefs = load_user_preferences_for_cli()
    assert prefs is not None
    assert prefs.default_runtime == "local"
    assert prefs.default_cloud_target == "ask-each-time", (
        "AC 3 / B1-loader contract: field stays at default 'ask-each-time' "
        "when the wizard skips the question"
    )


# ---------------------------------------------------------------------------
# AC 5 — q/ESC skip during the prompt preserves the existing/default value.
# ---------------------------------------------------------------------------


def test_wizard_q_skip_during_default_cloud_target_prompt(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    """Typing ``q`` at the prompt skips THIS field while letting the wizard
    finish the rest of the preferences. The persisted ``default_cloud_target``
    falls back to the existing default. Covers AC 4 + AC 5 "q/ESC skip"."""
    answers = [
        *_wizard_base_answers(),
        "cloud",  # default_runtime triggers the prompt.
        "q",  # Skip default_cloud_target.
        *_wizard_tail_answers(),
    ]
    result = runner.invoke(
        init_app,
        ["--interactive"],
        input="\n".join(answers) + "\n",
    )
    assert result.exit_code == 0, _combined_output(result)
    combined = _combined_output(result)
    assert _PROMPT_LABEL in combined, (
        "the prompt is still shown — only its answer was 'q'"
    )
    assert "default_cloud_target = (skipped)" in combined, (
        "AC 4: summary must mark 'q'-skipped runs as '(skipped)' too"
    )
    prefs = load_user_preferences_for_cli()
    assert prefs is not None
    assert prefs.default_runtime == "cloud"
    assert prefs.default_cloud_target == "ask-each-time", (
        "skip semantics: keep the existing/default value rather than write "
        "a wizard decision"
    )


# ---------------------------------------------------------------------------
# AC 5 — invalid input rejected, prompt re-asks (No Silent Failures).
# ---------------------------------------------------------------------------


def test_wizard_rejects_invalid_default_cloud_target_and_reprompts(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    """Garbage input triggers a one-line hint + re-prompt; the next valid
    answer is accepted and persisted. Covers AC 5 "invalid-value rejection
    (re-prompts)" — the workspace-level No Silent Failures rule applies."""
    answers = [
        *_wizard_base_answers(),
        "cloud",  # default_runtime triggers the prompt.
        "totally-bogus-target",  # First answer: invalid.
        "self-hosted",  # Second answer: valid.
        *_wizard_tail_answers(),
    ]
    result = runner.invoke(
        init_app,
        ["--interactive"],
        input="\n".join(answers) + "\n",
    )
    assert result.exit_code == 0, _combined_output(result)
    combined = _combined_output(result)
    assert _PROMPT_LABEL in combined
    assert "Please answer one of" in combined, (
        "AC 5: invalid input must surface a re-prompt hint, never silently "
        "accept the garbage value"
    )
    prefs = load_user_preferences_for_cli()
    assert prefs is not None
    assert prefs.default_cloud_target == "self-hosted", (
        "AC 5: the re-prompt's next valid answer must be the persisted value"
    )
