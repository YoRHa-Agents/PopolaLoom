"""Interactive option-group resolver for ``popola dispatch --wizard``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

CURSOR_CLOUD_MODELS: tuple[str, ...] = (
    "default",
    "composer-2.5",
    "composer-2-5",
    "sonnet",
    "gpt-5.5",
    "opus",
    "gemini",
)
"""Known Cursor cloud model ids surfaced by the dispatch wizard.

v1.6.1: ``composer-2`` and ``composer-2-fast`` were removed from
``GET https://api.cursor.com/v1/models`` upstream and now return
``HTTP 400 invalid_model``. The wizard offers the current
``composer-2.5`` (canonical id) and its ``composer-2-5`` alias (the
``fast`` variant), plus the still-current ``sonnet`` / ``gpt-5.5``
aliases that Cursor keeps as ``-latest`` pointers. New mainline
models ``opus`` and ``gemini`` (aliases for ``claude-opus-4-7`` and
``gemini-3.1-pro`` per ``GET /v1/models``) are added so the wizard
matches the inventory the gateway actually accepts."""


def _choose(prompt: str, options: list[tuple[str, str]], *, default: str) -> str:
    if not options:
        raise ValueError("dispatch wizard option group cannot be empty")
    default_idx = next(
        (idx for idx, (value, _label) in enumerate(options) if value == default),
        0,
    )
    typer.echo(prompt)
    for idx, (value, label) in enumerate(options, start=1):
        marker = "*" if idx - 1 == default_idx else " "
        typer.echo(f"  {idx}. [{marker}] {label} ({value})")
    raw = typer.prompt("Choose number/value", default=str(default_idx + 1))
    token = str(raw).strip()
    if not token:
        return options[default_idx][0]
    if token.isdigit():
        selected = int(token)
        if selected < 1 or selected > len(options):
            raise typer.BadParameter(f"choice must be 1-{len(options)}")
        return options[selected - 1][0]
    valid = {value for value, _label in options}
    if token not in valid:
        raise typer.BadParameter(f"choice must be one of {sorted(valid)}")
    return token


def _multi(
    prompt: str,
    options: list[tuple[str, str]],
    *,
    defaults: tuple[str, ...] = (),
) -> tuple[str, ...]:
    typer.echo(prompt)
    for idx, (value, label) in enumerate(options, start=1):
        marker = "*" if value in defaults else " "
        typer.echo(f"  {idx}. [{marker}] {label} ({value})")
    raw = typer.prompt("Choose comma-separated numbers/values", default=",".join(defaults))
    token = str(raw).strip()
    if not token:
        return defaults
    by_idx = {str(idx): value for idx, (value, _label) in enumerate(options, start=1)}
    valid = {value for value, _label in options}
    selected: list[str] = []
    for part in [item.strip() for item in token.split(",") if item.strip()]:
        value = by_idx.get(part, part)
        if value not in valid:
            raise typer.BadParameter(f"choice must be one of {sorted(valid)}")
        if value not in selected:
            selected.append(value)
    return tuple(selected)


def _target_default(prefs: Any) -> str:
    if prefs.default_runtime == "cloud":
        target = getattr(prefs.cursor_cloud, "default_cloud_target", "ask-each-time")
        if target == "self-hosted":
            return "cursor-cloud-self-hosted"
        return "cursor-cloud-managed"
    if prefs.default_runtime == "ask-each-time":
        return "local-cursor"
    return f"local-{prefs.default_local_cli}"


def run_dispatch_wizard(
    prefs: Any,
    *,
    prompt: str,
    cwd: Path | None,
    extra: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Prompt for target/model/thinking/special modes and return dispatch args."""
    _ = prompt
    selected_extra = dict(extra)
    target = _choose(
        "Dispatch target",
        [
            ("local-cursor", "Local Cursor"),
            ("local-claude", "Local Claude Code"),
            ("local-codex", "Local Codex"),
            ("cursor-cloud-managed", "Cursor Cloud (managed)"),
            ("cursor-cloud-self-hosted", "Cursor Cloud (self-hosted worker)"),
        ],
        default=_target_default(prefs),
    )

    if target.startswith("local-"):
        cli = target.removeprefix("local-")
        if cli == "cursor":
            selected_extra["output_format"] = _choose(
                "Cursor output format",
                [("text", "Text"), ("stream-json", "Streaming JSON")],
                default=getattr(prefs.cursor, "output_format", "text"),
            )
        elif cli == "claude":
            raw_turns = typer.prompt(
                "Claude max turns (0 = no limit)",
                default=str(getattr(prefs.claude, "max_turns", 0)),
            )
            selected_extra["max_turns"] = int(str(raw_turns).strip() or "0")
        elif cli == "codex":
            selected_extra["sandbox"] = _choose(
                "Codex sandbox",
                [
                    ("read-only", "Read-only"),
                    ("workspace-write", "Workspace write"),
                    ("danger-full-access", "Danger full access"),
                ],
                default=getattr(prefs.codex, "sandbox", "workspace-write"),
            )
        typer.echo(f"Dispatch summary: cli={cli}, cwd={cwd or Path.cwd()}")
        if not typer.confirm("Submit dispatch?", default=True):
            raise typer.Exit(code=1)
        return cli, selected_extra

    cli = "cursor-cloud"
    selected_extra.setdefault("model", getattr(prefs.cursor_cloud, "model", "default"))
    selected_extra["model"] = _choose(
        "Cursor Cloud model",
        [(model, model) for model in CURSOR_CLOUD_MODELS],
        default=str(selected_extra["model"]),
    )
    typer.echo("Cursor Cloud effort is available with --auth-mode=session-jwt.")
    if target == "cursor-cloud-managed":
        selected_extra["env"] = {"type": "cloud"}
        selected_extra["cloud_target"] = "cursor-managed"
    else:
        worker_default = getattr(prefs.cursor_cloud, "worker_name", "")
        worker_name = typer.prompt("Self-hosted worker name", default=worker_default)
        worker_name_text = str(worker_name).strip()
        if not worker_name_text:
            raise typer.BadParameter("self-hosted target requires a worker name")
        selected_extra["env"] = {"type": "machine", "name": worker_name_text}
        # v1.6.0 (feedback_for_v1.5.2 constraint #5): self-hosted choice
        # ALWAYS sets cloud_target=self-hosted + auth_mode=session-jwt
        # at the extras level so the daemon supervisor routes via the
        # single canonical Path-B JWT path. The wizard never offers a
        # pool option or a REST fallback for self-hosted.
        selected_extra["cloud_target"] = "self-hosted"
        selected_extra["worker_name"] = worker_name_text
        selected_extra["__auth_mode__"] = "session-jwt"
    selected_extra.setdefault(
        "starting_ref", getattr(prefs.cursor_cloud, "starting_ref", "main")
    )

    preset_default = getattr(prefs.cursor_cloud, "default_preset", "")
    selected_extra["preset"] = _choose(
        "Preset (Path-B grind/long-running/quick-fix/...)",
        [
            ("", "(none)"),
            ("quick-fix", "Quick fix"),
            ("long-running-plan", "Long-running plan"),
            ("exploration", "Exploration"),
            ("review", "Review"),
            ("grind", "Grind"),
        ],
        default=preset_default,
    )
    effort_default = getattr(prefs.cursor_cloud, "default_effort", "")
    selected_extra["effort"] = _choose(
        "Effort (Path-B)",
        [("", "(none)"), ("low", "Low"), ("medium", "Medium"), ("high", "High")],
        default=effort_default,
    )
    thinking_default = getattr(prefs.cursor_cloud, "default_thinking_level", "")
    selected_extra["thinking_level"] = _choose(
        "Thinking level (Path-B)",
        [("", "(none)"), ("low", "Low"), ("medium", "Medium"), ("high", "High")],
        default=thinking_default,
    )
    max_mode_default = getattr(prefs.cursor_cloud, "default_max_mode", False)
    selected_extra["max_mode"] = typer.confirm(
        "Enable max-mode (Path-B)?", default=bool(max_mode_default)
    )

    special = _multi(
        "Special modes",
        [
            ("auto_create_pr", "Auto-create PR"),
            ("work_on_current_branch", "Work on current branch"),
            ("skip_reviewer_request", "Skip reviewer request"),
            ("popola_handoff_flag", "Preserve Popola handoff flag"),
            ("cwd_flag", "Send cwd in extras"),
        ],
        defaults=tuple(
            name
            for name in (
                "auto_create_pr",
                "work_on_current_branch",
                "skip_reviewer_request",
            )
            if bool(getattr(prefs.cursor_cloud, name, False))
        ),
    )
    for name in special:
        if name == "cwd_flag":
            if cwd is not None:
                selected_extra["cwd"] = str(cwd)
        elif name == "popola_handoff_flag":
            selected_extra["popola_handoff"] = True
        else:
            selected_extra[name] = True
    typer.echo(f"Dispatch summary: cli={cli}, target={target}, model={selected_extra['model']}")
    if not typer.confirm("Submit dispatch?", default=True):
        raise typer.Exit(code=1)
    return cli, selected_extra
