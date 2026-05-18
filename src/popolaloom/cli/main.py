"""popola CLI — Typer entry point for the popolaloom meta-orchestrator.

v0.2.0 Stage A: switched from in-process ``Popolad`` calls to a real RPC
client over Unix Domain Socket (closes R-001 + R-005). Each subcommand
opens an :class:`httpx.AsyncClient` with
``transport=httpx.AsyncHTTPTransport(uds=str(socket_path))`` and talks
to the popolad daemon process at ``http://popolad/<endpoint>``.

User-facing subcommands:

- ``popola version`` — print package version + exit (no daemon needed).
- ``popola list-cli`` — show registered CLI adapter names + availability
  (no daemon needed; only the local registry).
- ``popola dispatch`` — POST /dispatch (daemon required).
- ``popola status``  — GET  /status/{id}.
- ``popola list``    — GET  /list (default non-terminal only).
- ``popola attach``  — GET  /attach_stream/{id} (default ``--follow=True``,
  R-005 fix; ``--no-follow`` reverts to one-shot dump).
- ``popola cancel``  — POST /cancel/{id}.
- ``popola probe``   — GET  /probe (lightweight daemon health).

All daemon-bound commands handle ``httpx.ConnectError`` with a friendly
"popolad not running" message + ``exit 1`` (No Silent Failures).

New v0.2.0 options:

- ``--cli-flag KEY=VAL`` repeated → ``extra`` dict passed to adapter
  (R-012 fix — enables ``cursor`` ``--yolo`` etc.).
- ``--events-dir PATH`` on dispatch (R-014 part) — currently advisory only;
  daemon process owns the actual events directory; this passes the option
  to the daemon for future per-task override (Stage E).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import click
import httpx
import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from popolaloom import __version__
from popolaloom.adapters import get_adapter, list_registered
from popolaloom.adapters.cursor_cloud import (
    CloudCursorClient,
    CursorCloudError,
    CursorCloudStreamInvalidLastEventIdError,
    SSEReader,
)

if TYPE_CHECKING:
    from popolaloom.daemon.event_log import EventLog

__all__ = ["app", "make_async_client", "make_sync_client"]

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="popola",
    help="PopolaLoom meta-orchestrator CLI — dispatch tasks across local agent CLIs.",
    no_args_is_help=True,
    add_completion=False,
    invoke_without_command=True,
)


def _register_subcommand_groups() -> None:
    """Attach the popolad + eval + init + skill subcommand groups + doctor verb.

    Registered here (not in :mod:`popolaloom.cli.__init__`) so that
    ``python -m popolaloom.cli.main`` invocations get the same surface
    as ``popola`` (the console_script entry).  Splitting the imports
    into a helper avoids module-level circular imports during the
    Typer app construction.

    v0.5.0 Stage S4: registers the new ``popola skill`` subcommand
    group (``install`` / ``doctor`` / ``upgrade``) and the standalone
    ``popola doctor`` aggregate-health verb (single command, not a
    subcommand group, per plan §S4.E).

    v0.8.8 T2.4.1 (Q-C-1 偏离默认): registers the new ``popola cloud``
    subcommand group whose first verb is ``runs`` — list cloud-agent
    run history per ``runs-subcommand-spec.md`` §2.2.
    """
    from popolaloom.cli.auth_cmd import app as auth_app
    from popolaloom.cli.cloud_cmd import app as cloud_app
    from popolaloom.cli.doctor_cmd import doctor_command
    from popolaloom.cli.eval import app as eval_app
    from popolaloom.cli.handoff_cmd import app as handoff_app
    from popolaloom.cli.init_cmd import app as init_app
    from popolaloom.cli.popolad import app as popolad_app
    from popolaloom.cli.relay_cmd import relay_command
    from popolaloom.cli.skill_cmd import app as skill_app
    from popolaloom.cli.update_cmd import app as update_app

    app.add_typer(popolad_app, name="popolad", help="Manage popolad daemon process")
    app.add_typer(
        eval_app,
        name="eval",
        help="PopolaLoom self-evaluation (8-dim nines runner)",
    )
    app.add_typer(init_app, name="init")
    app.add_typer(
        skill_app,
        name="skill",
        help="Install / audit / upgrade the PopolaLoom Skill",
    )
    app.add_typer(
        handoff_app,
        name="handoff",
        help="Inspect / archive on-disk handoff envelopes (v0.7.2+)",
    )
    app.add_typer(
        cloud_app,
        name="cloud",
        help="Cloud-agent (cursor-cloud runtime) introspection verbs.",
    )
    app.add_typer(
        auth_app,
        name="auth",
        help="Manage credentials (Cursor API key keyring storage, v0.9.2+).",
    )
    # v1.4.0 — `popola update` Python equivalent of `install.sh update`
    # (closes the v1.3.0-feedback gap where operators had no Python-side
    # one-shot path for pip-upgrade + skill refresh + doctor).
    app.add_typer(
        update_app,
        name="update",
        help=(
            "Upgrade popolaloom + refresh installed Skills (Python equivalent "
            "of `install.sh update`)."
        ),
    )
    app.command(name="relay")(relay_command)
    app.command(name="doctor")(doctor_command)


_register_subcommand_groups()

_console_out = Console()

_TERMINAL_STATES: frozenset[str] = frozenset({"completed", "failed", "canceled"})

_POLL_INTERVAL_S: float = 0.5
_DEFAULT_WAIT_TIMEOUT_S: float = 60.0

_EXIT_INVALID_ARGS: int = 2
"""Exit code emitted when ``popola dispatch`` rejects an invalid flag combination.

Mirrors the Typer convention (``code=2``) used by other CLI command groups
(``cli/cloud_cmd.py``, ``cli/relay_cmd.py``, ``cli/auth_cmd.py``) for
argument-validation failures so script-level branching stays consistent.
"""

_VALID_CLOUD_TARGETS_DISPATCH: frozenset[str] = frozenset(
    {"self-hosted", "cursor-managed"}
)
"""Cloud targets accepted by ``--cloud-target`` at dispatch time.

``ask-each-time`` is INTENTIONALLY excluded — it is a valid value only for
``[user_preferences].default_cloud_target`` (Q-5); the dispatch resolver
must collapse it to ``self-hosted`` or ``cursor-managed`` BEFORE leaving the
CLI process (DECISIONS Q-6, PLAN B3 AC 3).
"""


def _option_was_set_on_command_line(param_name: str) -> bool:
    """Return whether Click saw ``param_name`` from the current command line."""
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        return False
    try:
        source = ctx.get_parameter_source(param_name)
    except (AttributeError, RuntimeError, KeyError):
        logger.debug(
            "could not inspect Click parameter source for %s; treating as default",
            param_name,
            exc_info=True,
        )
        return False
    return source is click.core.ParameterSource.COMMANDLINE


@app.callback()
def _root_callback(
    version_flag: bool = typer.Option(
        False,
        "--version",
        help="Print package version and exit.",
    ),
) -> None:
    """Root options shared by all commands."""
    if version_flag:
        typer.echo(f"popolaloom {__version__}")
        raise typer.Exit(code=0)


# ── transport helpers ────────────────────────────────────────────────────


def _socket_path() -> Path:
    """Resolve the popolad UDS path: ``$POPOLA_HOME/popolad.sock`` or ``~/.popola/popolad.sock``.

    Tests can override by setting ``$POPOLA_HOME`` to a tmp_path.
    """
    home = os.environ.get("POPOLA_HOME")
    base = Path(home).expanduser().resolve() if home else Path.home() / ".popola"
    return base / "popolad.sock"


def make_async_client(socket_path: Path | None = None) -> httpx.AsyncClient:
    """Construct an :class:`httpx.AsyncClient` bound to the popolad UDS.

    Args:
        socket_path: override (tests use this); default resolves via
            :func:`_socket_path`.

    Returns:
        httpx.AsyncClient: caller is responsible for ``async with`` /
        ``aclose()``.

    Raises:
        Nothing here — connection failures surface on first request as
        :class:`httpx.ConnectError`, handled by :func:`_render_connect_error`.
    """
    sock = socket_path or _socket_path()
    transport = httpx.AsyncHTTPTransport(uds=str(sock))
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://popolad",
        timeout=httpx.Timeout(connect=5.0, read=None, write=10.0, pool=10.0),
    )


def make_sync_client(socket_path: Path | None = None) -> httpx.Client:
    """Construct a synchronous :class:`httpx.Client` bound to the popolad UDS.

    Used for fast non-streaming subcommands (``status``, ``list``) where
    asyncio overhead is not warranted; ``attach`` uses the async variant
    for proper SSE streaming.
    """
    sock = socket_path or _socket_path()
    transport = httpx.HTTPTransport(uds=str(sock))
    return httpx.Client(
        transport=transport,
        base_url="http://popolad",
        timeout=httpx.Timeout(connect=5.0, read=None, write=10.0, pool=10.0),
    )


def _render_connect_error(exc: httpx.HTTPError) -> None:
    """Print friendly daemon-down message + exit 1 (No Silent Failures)."""
    typer.echo(
        "error: popolad not running, run `popola popolad start` to start it",
        err=True,
    )
    logger.debug("daemon connect error: %r", exc)
    raise typer.Exit(code=1)


# ── version (no daemon) ───────────────────────────────────────────────────


@app.command()
def version() -> None:
    """Print package version and exit (e.g. ``popolaloom 0.0.1``)."""
    typer.echo(f"popolaloom {__version__}")


# ── list-cli (no daemon) ──────────────────────────────────────────────────


@app.command(name="list-cli")
def list_cli() -> None:
    """Show all registered CLI adapter names + availability on ``$PATH``.

    R-014 fix: status column is rendered via :class:`rich.text.Text` so
    Rich markup like ``[available]`` doesn't get interpreted (previously
    rendered as empty in some terminals).
    """
    names = list_registered()
    if not names:
        typer.echo(
            "error: no adapters registered (Phase 1 expects cursor/claude/codex)",
            err=True,
        )
        raise typer.Exit(code=1)

    table = Table(title="Registered CLI adapters", show_header=True, header_style="bold")
    table.add_column("name", style="bold")
    table.add_column("binary")
    table.add_column("status")

    for name in names:
        adapter = get_adapter(name)
        if adapter.is_available():
            status_text = Text("available", style="green")
        else:
            status_text = Text("missing", style="yellow")
        table.add_row(name, adapter.binary, status_text)

    _console_out.print(table)


# ── dispatch ──────────────────────────────────────────────────────────────


@app.command()
def dispatch(
    prompt: str = typer.Argument(
        "",
        help="Prompt string forwarded to the chosen CLI. May be empty when --replay is set.",
    ),
    cli: str = typer.Option(
        "",
        "--cli",
        help="CLI adapter name (cursor/claude/codex/...). May be empty when --replay is set.",
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Working directory for the spawned subprocess (defaults to popolad's CWD).",
    ),
    cli_flag: list[str] = typer.Option(  # noqa: B008
        [],
        "--cli-flag",
        help=(
            "Repeatable adapter extras KEY=VAL (e.g. repo_url=...; "
            "cursor-cloud routing: worker_name/pool_name/labels). R-012."
        ),
    ),
    cloud_target: str = typer.Option(
        "",
        "--cloud-target",
        help=(
            "Cloud target for this dispatch: 'self-hosted' (route to your own "
            "registered worker via env={type:'machine',name:<X>}) or "
            "'cursor-managed' (Cursor's hosted cloud). When set and --cli is "
            "empty, --cli is auto-set to 'cursor-cloud' (Q-6). Per-task value "
            "wins over [user_preferences].default_cloud_target (Q-5). "
            "本次派发的云端目标:'self-hosted'(派发到自己注册的 Worker)或 "
            "'cursor-managed'(Cursor 托管云)。设定后若 --cli 为空将自动设为 "
            "'cursor-cloud'。本次任务值优先于 [user_preferences].default_cloud_target。"
        ),
    ),
    worker_name: str = typer.Option(
        "",
        "--worker-name",
        help=(
            "Self-hosted worker name; REQUIRED iff --cloud-target=self-hosted; "
            "REJECTED when --cloud-target=cursor-managed (mutual exclusion "
            "per Q-6). When --cloud-target=self-hosted and the named worker "
            "is missing, dispatch hard-exits per Q-7's no-fallback contract. "
            "自托管 Worker 名称;仅在 --cloud-target=self-hosted 时必填,与 "
            "--cloud-target=cursor-managed 互斥。当 Worker 不存在时,直接退出,"
            "不会回退到本地执行。"
        ),
    ),
    model: str = typer.Option(
        "",
        "--model",
        help=(
            "Model id forwarded to the Cursor cloud agent (Q-A1, v1.0.0). "
            "Equivalent to --cli-flag model=<id>; this flag is the "
            "discoverable / self-documenting form. Accepted ids are listed "
            "by GET /v1/models (e.g. 'default', 'gpt-5.5', 'claude-sonnet-4'). "
            "When empty, Cursor picks the recommended model for the user's "
            "plan (the v0.10.0 'default' marker). Only consumed by "
            "cursor-cloud dispatches; ignored for other --cli adapters. "
            "派发到 Cursor 云端时使用的 model id;留空交由 Cursor 选择默认 model。"
        ),
    ),
    auth_mode: str = typer.Option(
        "rest",
        "--auth-mode",
        help=(
            "EXPERIMENTAL (v1.0.0 Q-13/Q-22) — auth transport for cursor-cloud "
            "dispatches: 'rest' (default; uses CURSOR_API_KEY against the "
            "stable POST /v1/agents schema) or 'session-jwt' (opt-in; uses "
            "the JWT at ~/.config/cursor/auth.json against the experimental "
            "Connect-RPC StartBackgroundComposerFromSnapshot endpoint to "
            "unlock --mode/--max-mode/--effort/--time-budget/--long-running/"
            "--auto-proceed-after-plan/--preset). Path-B (session-jwt) is "
            "NOT part of the v1.x SemVer stability surface; Cursor may "
            "change the wire format without notice. "
            "(实验性) cursor-cloud 派发使用的鉴权通道;'rest' 为默认稳定接口,"
            "'session-jwt' 启用实验性 RPC 路径以支持 --mode 等高级控制项。"
        ),
    ),
    mode: str = typer.Option(
        "",
        "--mode",
        help=(
            "EXPERIMENTAL — agent mode (path-B only): "
            "agent|ask|plan|debug|triage|project|multitask. "
            "Requires --auth-mode=session-jwt. "
            "Agent 工作模式;需要 --auth-mode=session-jwt。"
        ),
    ),
    max_mode: bool = typer.Option(
        False,
        "--max-mode/--no-max-mode",
        help=(
            "EXPERIMENTAL — enable max-context mode on the chosen model "
            "(path-B only). Requires --auth-mode=session-jwt. "
            "启用 max-context 模式;需要 --auth-mode=session-jwt。"
        ),
    ),
    effort: str = typer.Option(
        "",
        "--effort",
        help=(
            "EXPERIMENTAL — effort_mode (path-B only): low|medium|high. "
            "Requires --auth-mode=session-jwt. "
            "Agent 投入深度;需要 --auth-mode=session-jwt。"
        ),
    ),
    thinking_level: str = typer.Option(
        "",
        "--thinking-level",
        help=(
            "EXPERIMENTAL (v1.3.0 P2) — model_details.thinking_level "
            "(path-B only): low|medium|high. Requires --auth-mode=session-jwt. "
            "(实验性) 思考深度;需要 --auth-mode=session-jwt。"
        ),
    ),
    time_budget: str = typer.Option(
        "",
        "--time-budget",
        help=(
            "EXPERIMENTAL — time budget (path-B only). Accepted forms: "
            "bare integer (= seconds), or suffixed '60s' / '30m' / '1h'. "
            "Requires --auth-mode=session-jwt. "
            "时间预算;需要 --auth-mode=session-jwt。"
        ),
    ),
    long_running: bool = typer.Option(
        False,
        "--long-running/--no-long-running",
        help=(
            "EXPERIMENTAL — enable long_running_agent_mode (path-B only). "
            "Requires --auth-mode=session-jwt. "
            "启用长任务模式;需要 --auth-mode=session-jwt。"
        ),
    ),
    auto_proceed_after_plan: bool = typer.Option(
        False,
        "--auto-proceed-after-plan/--no-auto-proceed-after-plan",
        help=(
            "EXPERIMENTAL — auto_proceed_after_planning (path-B only); "
            "typically paired with --mode=plan. "
            "Requires --auth-mode=session-jwt. "
            "规划完成后自动执行;需要 --auth-mode=session-jwt。"
        ),
    ),
    preset: str = typer.Option(
        "",
        "--preset",
        help=(
            "EXPERIMENTAL — flag preset (path-B only). Built-in: "
            "'quick-fix' / 'long-running-plan' / 'exploration' / 'review' "
            "(Q-17). Custom presets via ~/.config/popola/presets.toml. "
            "Requires --auth-mode=session-jwt. "
            "标志预设;需要 --auth-mode=session-jwt。"
        ),
    ),
    # v1.5.0 — Path-B "skip branch / PR" knobs (feedback_for_v1.4.0 G4).
    # These mirror the equivalent Cursor web-UI toggles. Defaults match
    # the historical Path-B behaviour so existing dispatches see no
    # change; opt-in via the negated flag (e.g. --no-auto-branch).
    auto_branch: bool = typer.Option(
        True,
        "--auto-branch/--no-auto-branch",
        help=(
            "EXPERIMENTAL (v1.5.0) — toggle Cursor's auto-branch creation "
            "on the agent worker (path-B only). Default ON matches the "
            "Cursor web-UI default. Use --no-auto-branch to dispatch onto "
            "the worker's current ref without creating a feature branch. "
            "Requires --auth-mode=session-jwt + --cli=cursor-cloud. "
            "(实验性) 是否在 Worker 上自动创建分支;默认开启,与 Cursor "
            "网页端一致。--no-auto-branch 跳过分支创建,直接在当前 ref 上派发。"
        ),
    ),
    auto_create_pr: bool = typer.Option(
        False,
        "--auto-create-pr/--no-auto-create-pr",
        help=(
            "EXPERIMENTAL (v1.5.0) — toggle the auto-create-PR step after "
            "the agent finishes (path-B only). Default OFF so a JWT-direct "
            "dispatch does NOT spawn a PR unless the operator opts in. "
            "Requires --auth-mode=session-jwt + --cli=cursor-cloud. "
            "(实验性) Agent 完成后是否自动创建 PR;默认关闭。"
        ),
    ),
    work_on_current_branch: bool = typer.Option(
        False,
        "--work-on-current-branch",
        help=(
            "EXPERIMENTAL (v1.5.0) — instruct the worker to operate on "
            "the cwd's current ref rather than checking out a new branch "
            "(path-B only). Satisfies G4 of feedback_for_v1.4.0 ('跳过 "
            "git 分支 / PR 相关操作'). Requires --auth-mode=session-jwt + "
            "--cli=cursor-cloud. "
            "(实验性) Worker 在当前 ref 上工作,不切分支。"
        ),
    ),
    skip_reviewer_request: bool = typer.Option(
        False,
        "--skip-reviewer-request",
        help=(
            "EXPERIMENTAL (v1.5.0) — suppress the auto reviewer-request "
            "on the resulting PR (path-B only). Pairs with --auto-create-pr "
            "for the 'create PR but don't ping reviewers' workflow. "
            "Requires --auth-mode=session-jwt + --cli=cursor-cloud. "
            "(实验性) PR 创建后不发起 Reviewer 请求。"
        ),
    ),
    allow_fallback: bool = typer.Option(
        False,
        "--allow-fallback",
        help=(
            "v1.5.0 No-Silent-Fallback opt-in (managed cloud / local CLI "
            "only): when --cli=<X> is unavailable, allow the resolver to "
            "walk [user_preferences.routing].fallback_chain. Default OFF — "
            "popola hard-fails when the requested CLI adapter is missing. "
            "v1.6.0 (feedback_for_v1.5.2 constraint #2): this flag is a "
            "NO-OP + WARN when --cloud-target=self-hosted; popola NEVER "
            "swaps to a local CLI on the self-hosted single-path dispatch. "
            "v1.6.0 不静默回退默认约束:除非显式 --allow-fallback,"
            "否则当请求的 --cli 不可用时直接退出。--cloud-target=self-hosted "
            "时此标志强制为 no-op,popola 绝不回退到本地 CLI。"
        ),
    ),
    events_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--events-dir",
        help="Override events_dir (advisory; daemon owns actual write path). R-014 part.",
    ),
    replay: str = typer.Option(
        "",
        "--replay",
        help=(
            "Replay a previously written handoff envelope by id "
            "(e.g. cursor-fix-bug-foo-py-3a7f9c1d). When set, overrides "
            "prompt / --cli / --cwd / --cli-flag from the envelope's "
            "stored values (v0.7.3+)."
        ),
    ),
    wait: bool = typer.Option(
        False,
        "--wait",
        help="Block until the task reaches a terminal state (completed/failed/canceled).",
    ),
    timeout_s: float = typer.Option(
        _DEFAULT_WAIT_TIMEOUT_S,
        "--timeout",
        help="--wait timeout in seconds (default 60).",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON instead of a plain task_id line.",
    ),
    wizard: bool = typer.Option(
        False,
        "--wizard",
        "-W",
        help="Walk through dispatch option groups before submitting.",
    ),
    no_wizard: bool = typer.Option(
        False,
        "--no-wizard",
        help="Disable implicit dispatch wizard even when preferences request prompting.",
    ),
) -> None:
    """Dispatch a new task to popolad and (optionally) wait for completion.

    v0.7.3+ ``--replay HANDOFF_ID`` reads a previously written envelope from
    ``$POPOLA_HANDOFF_DIR`` (or ``.local/.agent/handoff/``) and uses its
    ``target_cli`` / ``prompt`` / ``cwd`` / ``adapter_extra`` as the
    dispatch payload — exact replay of a prior dispatch (or a relay'd /
    HITL'd one) without re-typing the prompt or its flags.
    """
    if wizard and json_out:
        typer.echo("error: --wizard cannot be combined with --json", err=True)
        raise typer.Exit(code=2)
    if wizard and no_wizard:
        typer.echo("error: --wizard and --no-wizard are mutually exclusive", err=True)
        raise typer.Exit(code=2)

    if replay:
        _resolved = _resolve_replay(replay, prompt, cli, cwd, cli_flag)
        prompt = _resolved.prompt
        cli = _resolved.cli
        cwd = _resolved.cwd
        extra = _resolved.adapter_extra
    else:
        if not prompt:
            typer.echo("error: missing prompt (or use --replay HANDOFF_ID)", err=True)
            raise typer.Exit(code=2)
        extra = _parse_cli_flags(cli_flag)

        if cloud_target or worker_name:
            _validate_cloud_target_flags(cloud_target, worker_name)

        if cloud_target and not cli:
            logger.info(
                "auto-setting --cli=cursor-cloud due to --cloud-target=%s",
                cloud_target,
            )
            cli = "cursor-cloud"

        # v1.6.0 (feedback_for_v1.5.2 constraint #2 / locked DECISIONS Q-4):
        # the `--allow-fallback` flag is preserved for managed cloud and
        # local CLIs (cursor / claude / codex / ...), but is a no-op +
        # bilingual WARN when ``cloud_target=self-hosted`` is in play.
        # Per No-Silent-Failures: never silently swap to a local CLI on
        # the self-hosted single-path dispatch.
        if cloud_target == "self-hosted" and allow_fallback:
            typer.echo(
                "warn: --allow-fallback is a no-op when "
                "--cloud-target=self-hosted (v1.6.0 single-path contract; "
                "feedback_for_v1.5.2.md constraint #2). popola will NEVER "
                "swap to a local CLI when the self-hosted worker dispatch "
                "fails. Fix the worker registration or re-dispatch with "
                "--cloud-target=cursor-managed if you need the managed "
                "cloud. "
                "(warn: --cloud-target=self-hosted 时 --allow-fallback 不生效; "
                "popola 不会自动回退到本地 CLI。请修复 Worker 注册或重新派发)",
                err=True,
            )
            allow_fallback = False

        if model:
            _apply_model_flag(extra, model, cli)

        prefs_for_dispatch: Any = None
        if not cli:
            prefs_for_dispatch = _load_dispatch_preferences_or_exit()
            if prefs_for_dispatch is None:
                typer.echo("error: --cli is required (or use --replay HANDOFF_ID)", err=True)
                raise typer.Exit(code=2)
            cli, extra = _select_cli_from_preferences(
                prefs_for_dispatch,
                extra=extra,
                cwd=cwd,
                cloud_target_flag=cloud_target,
                worker_name_flag=worker_name,
                prompt=prompt,
                wizard=wizard,
                no_wizard=no_wizard,
                allow_fallback=allow_fallback,
            )
        elif cli == "cursor-cloud":
            prefs_for_dispatch = _load_dispatch_preferences_or_exit()
            extra = _apply_cloud_preferences(
                prefs_for_dispatch,
                extra,
                cwd=cwd,
                cloud_target_flag=cloud_target,
                worker_name_flag=worker_name,
            )

        if cli == "cursor":
            # v1.5.0 (feedback_for_v1.4.0 §7 issue #2) — propagate the
            # persisted `[user_preferences.cursor].cli_args` to the
            # adapter's `extra["cli_args"]` so a user who sets a
            # standing flag set (e.g. `--trust --no-color`) once via
            # `popola init prefs --set cursor.cli_args=...` doesn't have
            # to re-pass them per dispatch. v1.3.0 silently dropped this
            # because dispatch only consulted `default_model`; v1.5.0
            # consults BOTH `default_model` and `cli_args` for the local
            # cursor adapter path. An explicit `--cli-flag cli_args=...`
            # always wins (we only fill when the key is absent).
            prefs_for_local_default = (
                prefs_for_dispatch
                if prefs_for_dispatch is not None
                else _try_load_dispatch_preferences()
            )
            cursor_prefs = (
                getattr(prefs_for_local_default, "cursor", None)
                if prefs_for_local_default is not None
                else None
            )
            local_default_model = (
                str(getattr(cursor_prefs, "default_model", "") or "")
                if cursor_prefs is not None
                else ""
            )
            if not model and local_default_model:
                _apply_model_flag(extra, local_default_model, cli)
            pref_cli_args = tuple(
                getattr(cursor_prefs, "cli_args", ()) or ()
            ) if cursor_prefs is not None else ()
            if pref_cli_args and "cli_args" not in extra:
                extra["cli_args"] = list(pref_cli_args)
                logger.debug(
                    "cursor: propagated [user_preferences.cursor].cli_args "
                    "%r into extra (v1.5.0 feedback_for_v1.4.0 §7 issue #2)",
                    pref_cli_args,
                )

        if prefs_for_dispatch is None:
            prefs_for_dispatch = _try_load_dispatch_preferences()

        _apply_path_b_flags(
            extra,
            cli=cli,
            auth_mode=auth_mode,
            auth_mode_explicit=_option_was_set_on_command_line("auth_mode"),
            mode=mode,
            max_mode=max_mode,
            effort=effort,
            time_budget=time_budget,
            long_running=long_running,
            auto_proceed_after_plan=auto_proceed_after_plan,
            preset=preset,
            thinking_level=thinking_level,
            auto_branch=auto_branch,
            auto_create_pr=auto_create_pr,
            work_on_current_branch=work_on_current_branch,
            skip_reviewer_request=skip_reviewer_request,
            prefs=prefs_for_dispatch,
        )

    if events_dir is not None:
        extra.setdefault("__events_dir", str(events_dir))

    body: dict[str, Any] = {"cli": cli, "prompt": prompt}
    if cwd is not None:
        body["cwd"] = str(cwd)
    if extra:
        body["extra"] = extra

    try:
        with make_sync_client() as client:
            r = client.post("/dispatch", json=body)
    except httpx.ConnectError as exc:
        _render_connect_error(exc)
        return

    if r.status_code == 404:
        typer.echo(f"error: unknown cli={cli!r}: {r.json().get('detail', '')}", err=True)
        raise typer.Exit(code=1)
    if r.status_code == 400:
        typer.echo(f"error: dispatch failed: {r.json().get('detail', '')}", err=True)
        raise typer.Exit(code=1)
    if r.status_code != 200:
        typer.echo(f"error: dispatch unexpected status {r.status_code}: {r.text}", err=True)
        raise typer.Exit(code=1)

    payload = r.json()
    task_id = payload["task_id"]

    if wait:
        _wait_for_terminal(task_id, timeout_s=timeout_s)

    if json_out:
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        typer.echo(task_id)
        # v0.9.9 F3 (feedback_for_v0.9.7.md:51): Cursor's web dashboard
        # only lists Cloud Agent runs (cursor.com/agents); local
        # subprocess tasks dispatched via --cli=cursor are invisible
        # there. Surface the right local-observability path right at
        # dispatch time so operators don't waste 10 minutes refreshing
        # the dashboard. Gated on cli == "cursor": cursor-cloud and
        # other adapters keep their existing single-line output.
        if cli == "cursor":
            typer.echo(
                f"view: popola attach {task_id} --follow "
                "(note: Cursor dashboard does not show local "
                "subprocess tasks)"
            )


# ── status ────────────────────────────────────────────────────────────────


@app.command()
def status(
    task_id: str = typer.Argument(..., help="Task identifier returned by `popola dispatch`."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help=(
            "v0.8.8 (Q-C-2): show the cost / model / wall-clock surface "
            "per cost-fields.md §3.1. Default off — the surface is opt-in "
            "to avoid promoting fabricated cost numbers (cost is always "
            "rendered as 'cost: n/a' since Cursor's public REST/SSE API "
            "does not expose per-run cost in v0.8.8). With --json, adds "
            "a 'verbose' block per spec §3.2 (10 keys)."
        ),
    ),
) -> None:
    """Query a task's runtime status (state / pid / exit_code / timestamps).

    v0.8.8 T2.1.2 (Q-C-2 ``cost-fields.md``):

    - Without ``--verbose`` the response shape is unchanged from v0.8.7
      — no cost block in the table, no ``verbose`` key in the JSON.
    - With ``--verbose`` the table appends a one-liner
      ``cost: n/a  model: <id|->  [mode: max]  wall: NN.Ns  link: <url>``
      and the JSON gains a ``verbose`` object per spec §3.2.

    The ``cost: n/a`` literal is locked: PopolaLoom does NOT fabricate
    per-run cost numbers (no authoritative source in the public Cloud
    Agents v1 API). Future Admin-API correlation is opt-in and will
    surface as ``cost: ~$X.XX (admin-est)`` behind a separate config
    knob.
    """
    try:
        with make_sync_client() as client:
            params: dict[str, Any] = {"verbose": "true"} if verbose else {}
            r = client.get(f"/status/{task_id}", params=params)
    except httpx.ConnectError as exc:
        _render_connect_error(exc)
        return

    if r.status_code == 404:
        typer.echo(f"error: task not found: {task_id}", err=True)
        raise typer.Exit(code=1)
    if r.status_code != 200:
        typer.echo(f"error: status unexpected {r.status_code}: {r.text}", err=True)
        raise typer.Exit(code=1)

    info = r.json()

    busy_line = _build_status_busy_line(task_id)

    if json_out:
        typer.echo(json.dumps(info, ensure_ascii=False))
        if busy_line is not None:
            # Q-C-7 default-visibility: surface the WAITING marker even
            # in --json mode so machine consumers (status pollers) see
            # the same signal humans do; written to stderr so JSON
            # parsers reading stdout are unaffected.
            typer.echo(busy_line, err=True)
        return

    table = Table(title=f"Task {task_id}", show_header=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    fields = (
        "task_id",
        "cli",
        "state",
        "pid",
        "exit_code",
        "started_at",
        "completed_at",
        "latest_event_index",
        "arktower_task_id",
        "persisted",
    )
    for key in fields:
        value = info.get(key)
        table.add_row(key, "" if value is None else str(value))
    # v0.9.9 F2 (Q-V099-4): surface ``pid_alive`` only when the daemon
    # included it in the response (additive-only contract — the field is
    # absent for cloud-runtime tasks, terminal-state tasks, or running
    # tasks without a known pid). Defensive ``in`` check keeps older
    # daemons (pre-v0.9.9) from rendering a confusing empty row.
    if "pid_alive" in info:
        table.add_row("pid_alive", str(info["pid_alive"]))
    _console_out.print(table)

    # v0.8.8 T2.2.2 (Q-C-7): default-visible WAITING line surfaced
    # below the table whenever the latest unmatched ``cloud.queued_quota_exceeded``
    # / ``cloud.busy_queued`` event indicates the daemon is parked on a
    # 429 backoff or 409 agent_busy queue. This is disjoint from
    # T2.1.2's --verbose path (the cost line); both can render in the
    # same invocation.
    if busy_line is not None:
        typer.echo(busy_line)

    if verbose:
        cost_line = _format_verbose_cost_line(info.get("verbose"))
        typer.echo(cost_line)


def _build_status_busy_line(task_id: str) -> str | None:
    """Compose the ``WAITING:`` line for ``popola status`` (Q-C-7).

    Per ``quota-config.md`` §5.2, ``popola status`` at default verbosity
    must surface a single line summarising the latest
    ``cloud.queued_quota_exceeded`` (or ``cloud.busy_queued``) event
    until a matching exit event arrives. Returns ``None`` when the task
    is not in any waiting state — the caller suppresses the line entirely
    in that case so non-throttled tasks render as before.

    The events log lives at ``$POPOLA_HOME/events/<task_id>.jsonl``;
    we read it directly rather than adding a new RPC method (the daemon
    already owns the file's authoritative writes; the CLI is purely a
    reader here, so concurrent writes are safe via ``EventLog``'s
    ``O_APPEND`` semantics).
    """
    events = _read_events_for_task(task_id)
    if not events:
        return None
    return _busy_line_from_events(events)


def _events_path_for_task(task_id: str) -> Path:
    """Resolve ``$POPOLA_HOME/events/<task_id>.jsonl`` (mirrors daemon).

    Centralised here so the WAITING surface and any future per-task
    event peek share one path resolver — keeps the CLI's view of
    ``$POPOLA_HOME`` consistent with :func:`_socket_path`.
    """
    home = os.environ.get("POPOLA_HOME")
    base = Path(home).expanduser().resolve() if home else Path.home() / ".popola"
    return base / "events" / f"{task_id}.jsonl"


def _read_events_for_task(task_id: str) -> list[dict[str, Any]]:
    """Tolerantly read every NDJSON event for ``task_id``.

    Non-existent file → ``[]`` (the task may be local, or events may not
    have been written yet). Per workspace rule "No Silent Failures" we
    log decode errors at DEBUG so operators grepping the daemon log can
    still see them — but the CLI's status surface is non-fatal, so a
    bad line skips rather than aborts.
    """
    path = _events_path_for_task(task_id)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("status busy-line: cannot read %s: %s", path, exc)
        return []

    events: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            logger.debug(
                "status busy-line: skipping corrupt line %d in %s: %s",
                line_no,
                path,
                exc,
            )
    return events


def _busy_line_from_events(events: list[dict[str, Any]]) -> str | None:
    """Pick a WAITING summary from a per-task event stream (or ``None``).

    Decision tree (per ``quota-config.md`` §5.2 + §4.3):

    1. Walk events in chronological order tracking the most recent
       ``cloud.queued_quota_exceeded`` (rate-limit backoff) and
       ``cloud.busy_queued`` (409 agent_busy queue).
    2. Each is "cleared" by a matching exit event:
       - ``cloud.queued_quota_exceeded`` cleared by ``cloud.queue_exit``
         with ``outcome="success"`` (other outcomes leave the line up
         briefly so the eventual ``task.failed`` rendering takes over).
       - ``cloud.busy_queued`` cleared by ``cloud.busy_dispatched`` or
         ``cloud.busy_timeout``.
    3. The most-recent uncleared event of either kind wins; if both are
       uncleared we prefer the agent_busy line because it carries the
       longer wait window (per spec §5.2 priority).
    4. ``task.completed`` / ``task.failed`` / ``task.canceled`` clears
       both kinds (the task is over).
    """
    pending_quota: dict[str, Any] | None = None
    pending_busy: dict[str, Any] | None = None
    quota_ts: str | None = None
    busy_ts: str | None = None

    for ev in events:
        ev_type = ev.get("type") if isinstance(ev, dict) else None
        if not isinstance(ev_type, str):
            continue
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        if ev_type == "cloud.queued_quota_exceeded":
            pending_quota = data
            quota_ts = ev.get("time") if isinstance(ev.get("time"), str) else None
        elif ev_type == "cloud.queue_exit":
            outcome = data.get("outcome") if isinstance(data, dict) else None
            if outcome == "success" or outcome is None:
                pending_quota = None
                quota_ts = None
        elif ev_type == "cloud.busy_queued":
            pending_busy = data
            busy_ts = ev.get("time") if isinstance(ev.get("time"), str) else None
        elif ev_type in {"cloud.busy_dispatched", "cloud.busy_timeout"}:
            pending_busy = None
            busy_ts = None
        elif ev_type in {"task.completed", "task.failed", "task.canceled"}:
            pending_quota = None
            pending_busy = None
            quota_ts = None
            busy_ts = None

    if pending_busy is None and pending_quota is None:
        return None

    # Priority: busy (longer wait window) > quota (short backoff) when
    # both are uncleared simultaneously — see spec §5.2.
    if pending_busy is not None:
        return _format_busy_queue_line(pending_busy)
    assert pending_quota is not None
    _ = quota_ts  # ts retained for future enrichment (latency display)
    _ = busy_ts
    return _format_quota_waiting_line(pending_quota)


def _format_quota_waiting_line(payload: dict[str, Any]) -> str:
    """Format the rate-limit waiting line (``WAITING: rate_limit ...``).

    Spec example (§5.2): ``WAITING: rate_limit retry 2/5 next=~2.5s``.

    The current ``_retrying_request`` helper emits
    ``cloud.queued_quota_exceeded`` once per backoff sequence (NOT per
    attempt), so we have ``max_retries`` and (optionally)
    ``retry_after_ms`` but not the exact attempt counter. We render
    ``retry 1/<max>`` to match the spec example shape — operators
    reading the line know the daemon is parked on the *first* observed
    backoff in the current sequence; subsequent retries are silent
    until exit (the once-per-sequence emit policy is the design intent
    per spec §3.3).
    """
    max_retries = payload.get("max_retries")
    retry_after_ms = payload.get("retry_after_ms")
    max_str = (
        str(max_retries)
        if isinstance(max_retries, int) and max_retries > 0
        else "?"
    )
    if isinstance(retry_after_ms, (int, float)) and retry_after_ms >= 0:
        next_segment = f" next=~{float(retry_after_ms) / 1000.0:.1f}s"
    else:
        next_segment = ""
    return f"WAITING: rate_limit retry 1/{max_str}{next_segment}"


def _format_busy_queue_line(payload: dict[str, Any]) -> str:
    """Format the 409 agent_busy waiting line (``WAITING: agent_busy ...``).

    Per spec §5.2 the queue path is symmetric with the rate-limit path
    (both default-visible). We surface the queue position and the
    deadline so operators can decide whether to ``popola cancel <task>``
    or wait it out — the deadline is the most actionable bit.
    """
    agent_id = payload.get("agent_id")
    position = payload.get("queue_position")
    deadline_ts = payload.get("deadline_ts")

    parts: list[str] = ["WAITING: agent_busy"]
    if isinstance(agent_id, str) and agent_id:
        parts.append(f"agent={agent_id}")
    if isinstance(position, int) and position > 0:
        parts.append(f"position={position}")
    if isinstance(deadline_ts, str) and deadline_ts:
        parts.append(f"deadline={deadline_ts}")
    elif deadline_ts is None:
        # ``None`` is the "wait forever" sentinel (queue_max_wait_s=0).
        parts.append("deadline=never")
    return " ".join(parts)


def _format_verbose_cost_line(verbose_block: Any) -> str:
    """Render the §3.1 one-liner from the daemon's verbose block.

    Format (per ``cost-fields.md`` §3.1):

    ``cost: n/a  model: <id|->  [mode: max]  wall: NN.Ns  link: <url|->``

    Rules:

    - ``cost: n/a`` is always literal (Q-C-2: no fabricated numbers).
    - ``model: -`` when ``model_id`` is ``None`` (default substituted).
    - ``mode:`` segment omitted entirely when mode is ``"std"`` (keeps
      the line short in the common case per §3.1 example).
    - ``wall:`` shows 1-decimal seconds; ``-`` when missing.
    - ``link:`` from ``agent_url``; ``-`` when missing (local task).
    """
    if not isinstance(verbose_block, dict):
        return "cost: n/a  model: -  wall: -  link: -"

    model_id = verbose_block.get("model_id")
    model_str = model_id if isinstance(model_id, str) and model_id else "-"

    model_mode = verbose_block.get("model_mode")
    mode_segment = ""
    if isinstance(model_mode, str) and model_mode and model_mode != "std":
        mode_segment = f"mode: {model_mode}  "

    wall = verbose_block.get("wall_clock_s")
    wall_str = (
        f"{float(wall):.1f}s"
        if isinstance(wall, (int, float)) and wall >= 0
        else "-"
    )

    link = verbose_block.get("agent_url")
    link_str = link if isinstance(link, str) and link else "-"

    return (
        f"cost: n/a  model: {model_str}  {mode_segment}wall: {wall_str}  link: {link_str}"
    )


# ── attach ────────────────────────────────────────────────────────────────


@app.command()
def attach(
    task_id: str = typer.Argument(..., help="Task identifier whose events to tail."),
    from_index: int = typer.Option(
        0,
        "--from",
        help="Skip first N events (set to previous len() for incremental polling).",
    ),
    follow: bool = typer.Option(
        True,
        "--follow/--no-follow",
        help=(
            "Stream new events until terminal (default). "
            "Use --no-follow for a one-shot dump (R-005 fix in v0.2.0)."
        ),
    ),
    no_stream: bool = typer.Option(
        False,
        "--no-stream",
        help=(
            "v0.8.6 (T2.2.1): force the legacy poll-only path even for "
            "runtime=cloud tasks (escape hatch). By default --follow on a "
            "cloud task additionally pumps Cursor's SSE events into the "
            "renderer alongside the daemon's /attach_stream feed."
        ),
    ),
) -> None:
    """Print task events as ``<time>  <type>  <data_summary>`` lines.

    R-005 fix: ``--follow`` defaults to **True** so cross-process attach
    sees new events live. Use ``--no-follow`` for the legacy one-shot dump.

    v0.8.6 (T2.2.1): for ``runtime=cloud`` tasks the default ``--follow``
    additionally launches a Cursor SSE pump on a background thread that
    feeds ``cloud.sse.*`` events into the same renderer as the daemon's
    ``/attach_stream`` feed. On disconnect / ``410 stream_expired`` /
    network error the SSE thread bows out cleanly with a
    ``cloud.sse.fallback_to_poll`` boundary marker and a stderr notice;
    the existing poll-driven view continues. Use ``--no-stream`` to force
    the legacy poll-only path (escape hatch).
    """
    if follow:
        _attach_streaming(task_id, from_index=from_index, no_stream=no_stream)
    else:
        _attach_one_shot(task_id, from_index=from_index)


def _attach_one_shot(task_id: str, *, from_index: int) -> None:
    """Issue a one-shot tail request via ``GET /status`` + log file read.

    For non-follow mode we just hit ``/status`` (to verify the task exists)
    then ``/attach_stream`` with a short timeout: producer terminates as
    soon as the event log is drained when ``handle.is_terminal()``. For
    in-flight tasks we still produce the events available so far.
    """
    try:
        with make_sync_client() as client:
            r_status = client.get(f"/status/{task_id}")
            if r_status.status_code == 404:
                typer.echo(f"error: task not found: {task_id}", err=True)
                raise typer.Exit(code=1)
            if r_status.status_code != 200:
                typer.echo(f"error: status {r_status.status_code}: {r_status.text}", err=True)
                raise typer.Exit(code=1)

            with client.stream(
                "GET",
                f"/attach_stream/{task_id}",
                params={"since": from_index},
                timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=10.0),
            ) as stream:
                if stream.status_code != 200:
                    typer.echo(
                        f"error: attach_stream {stream.status_code}",
                        err=True,
                    )
                    raise typer.Exit(code=1)
                _consume_sse(stream, terminate_on_terminal=True)
    except httpx.ConnectError as exc:
        _render_connect_error(exc)


def _attach_streaming(
    task_id: str,
    *,
    from_index: int,
    no_stream: bool = False,
) -> None:
    """Long-poll the SSE attach_stream endpoint until terminal or Ctrl-C.

    v0.8.6 (T2.2.1): when the status snapshot reports ``runtime=cloud``
    and ``--no-stream`` was *not* passed, additionally spawn a daemon
    thread that pumps Cursor's SSE events into the local renderer. The
    thread exits cleanly on ``CursorCloudStreamExpiredError``,
    ``httpx.ReadError`` / ``httpx.ConnectError`` /
    ``httpx.TimeoutException``, missing API key, or main-thread teardown
    (``stop_event.set()``). On any error path it surfaces a
    ``cloud.sse.fallback_to_poll`` boundary event via the sink + a
    ``[cloud sse] ...`` one-liner on stderr (No-Silent-Failures).
    """
    sse_thread: threading.Thread | None = None
    sse_stop_event: threading.Event | None = None
    try:
        with make_sync_client() as client:
            r_status = client.get(f"/status/{task_id}")
            if r_status.status_code == 404:
                typer.echo(f"error: task not found: {task_id}", err=True)
                raise typer.Exit(code=1)
            if r_status.status_code != 200:
                typer.echo(f"error: status {r_status.status_code}: {r_status.text}", err=True)
                raise typer.Exit(code=1)

            status_info = _safe_status_payload(r_status)

            if not no_stream and status_info.get("runtime") == "cloud":
                sse_stop_event = threading.Event()
                sse_thread = _maybe_spawn_cloud_sse_thread(
                    task_id=task_id,
                    status_info=status_info,
                    stop_event=sse_stop_event,
                )

            try:
                with client.stream(
                    "GET",
                    f"/attach_stream/{task_id}",
                    params={"since": from_index},
                    timeout=httpx.Timeout(connect=5.0, read=None, write=10.0, pool=10.0),
                ) as stream:
                    if stream.status_code != 200:
                        typer.echo(
                            f"error: attach_stream {stream.status_code}",
                            err=True,
                        )
                        raise typer.Exit(code=1)
                    _consume_sse(stream, terminate_on_terminal=True)
            finally:
                if sse_stop_event is not None:
                    sse_stop_event.set()
                if sse_thread is not None:
                    sse_thread.join(timeout=2.0)
    except httpx.ConnectError as exc:
        _render_connect_error(exc)
    except KeyboardInterrupt:
        return


def _consume_sse(response: httpx.Response, *, terminate_on_terminal: bool) -> bool:
    """Iterate raw SSE response, parse each ``data:`` frame, render one line.

    Args:
        response: an open streaming :class:`httpx.Response`.
        terminate_on_terminal: when True, stop iterating once a terminal
            event is seen (we still let the server close naturally too).

    Returns:
        bool: ``True`` if a terminal event (``task.completed`` /
        ``task.failed`` / ``task.canceled``) or a server-side
        ``event: end-of-stream`` marker was observed. Callers can use
        this to distinguish "stream ended cleanly" from "stream broke
        mid-flight" (BUG-C in v0.7.0 feedback).

    BUG-C (v0.7.1): when the server's ``StreamingResponse`` returns
    after writing many SSE frames, ``httpx`` occasionally misclassifies
    the resulting EOF as a :class:`httpx.ReadTimeout` instead of a
    clean stream close. We catch ReadTimeout here and, when at least
    one terminal event has already been rendered, treat it as a normal
    stream-end (the producer is done; nothing more is coming). When the
    timeout fires *before* any terminal event we re-raise so the caller
    can decide (server hung mid-stream is still an error).
    """
    terminal_types = {"task.completed", "task.failed", "task.canceled"}
    saw_terminal = False
    try:
        for raw_line in response.iter_lines():
            if not raw_line:
                continue
            if raw_line.startswith("data: "):
                payload = raw_line[len("data: "):]
            elif raw_line.startswith("event:"):
                event_kind = raw_line[len("event:"):].strip()
                if event_kind == "end-of-stream":
                    saw_terminal = True
                    break
                continue
            elif raw_line.startswith(":"):
                continue
            else:
                continue
            try:
                envelope = json.loads(payload)
            except json.JSONDecodeError:
                logger.warning("Skipping un-parsable SSE frame: %r", payload[:200])
                continue
            typer.echo(_format_event(envelope))
            if envelope.get("type") in terminal_types:
                saw_terminal = True
                if terminate_on_terminal:
                    break
    except httpx.ReadTimeout:
        if not saw_terminal:
            logger.warning(
                "_consume_sse: ReadTimeout before any terminal event was seen; "
                "re-raising to caller"
            )
            raise
        logger.debug(
            "_consume_sse: ReadTimeout after terminal event observed — "
            "treating as clean stream-end (BUG-C)"
        )
    return saw_terminal


# ── cloud SSE pump (T2.2.1) ──────────────────────────────────────────────


def _utc_now_iso_ms() -> str:
    """ISO-8601 UTC timestamp with millisecond precision and ``Z`` suffix.

    Mirrors the format produced by :func:`popolaloom.daemon.event_log._utc_now_iso`
    so cloud SSE envelopes rendered by :class:`_CloudSSEEventSink` look the
    same as those streamed from the daemon's ``/attach_stream`` feed.
    """
    return (
        datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class _CloudSSEEventSink:
    """Duck-typed :class:`EventLog` for the CLI cloud SSE worker (T2.2.1).

    Implements the same ``append(event_type, data) -> dict`` contract as
    :class:`popolaloom.daemon.event_log.EventLog`, but instead of writing to
    disk it renders each envelope with :func:`_format_event` and emits it via
    :func:`typer.echo` so ``cloud.sse.*`` events appear inline in
    ``popola attach`` output alongside the daemon's ``/attach_stream`` feed.

    Per the v0.8.6 writer contract (``state-source-of-truth.md`` §1.2 rule 1),
    the SSE reader must NOT receive a :class:`StateStore` reference — it is
    structurally barred from ``cloud_phase`` mutation. This sink mirrors that
    property: it has no ``state_store`` attribute, never imports the daemon
    state module, and never makes a daemon RPC call.

    The collected ``events`` list is exposed for tests so they can introspect
    every envelope appended without having to capture stdout.
    """

    closed: bool = False

    def __init__(self, *, source: str = "popola/cli-cloud-sse") -> None:
        self._source = source
        self.events: list[dict[str, Any]] = []

    def append(self, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        envelope: dict[str, Any] = {
            "specversion": "1.0",
            "id": f"evt-cli-{uuid.uuid4().hex}",
            "source": self._source,
            "type": event_type,
            "time": _utc_now_iso_ms(),
            "data": data,
        }
        self.events.append(envelope)
        try:
            typer.echo(_format_event(envelope))
        except Exception as exc:  # noqa: BLE001 — render failures must not crash worker
            logger.warning(
                "cloud SSE sink failed to render event %s: %s", event_type, exc
            )
        return envelope


def _safe_status_payload(response: httpx.Response) -> dict[str, Any]:
    """Tolerantly parse a ``GET /status/{task_id}`` JSON body.

    Returns an empty dict on any decode failure or non-dict shape so the
    caller's ``runtime``/``cursor_agent_id`` lookups simply fall through to
    the legacy poll-only path (No-Silent-Failures: a warning is logged at
    DEBUG level).
    """
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        logger.debug("attach: status payload not JSON-decodable: %s", exc)
        return {}
    if not isinstance(body, dict):
        return {}
    return cast(dict[str, Any], body)


def _maybe_spawn_cloud_sse_thread(
    *,
    task_id: str,
    status_info: dict[str, Any],
    stop_event: threading.Event,
) -> threading.Thread | None:
    """Construct and start a daemon thread pumping Cursor SSE.

    Returns ``None`` (no thread spawned) when the prerequisites for the
    cloud SSE channel are not met:

    - ``cursor_agent_id`` or ``cursor_run_id`` missing on the status
      snapshot (the dispatch may have failed before the cloud
      ``POST /v1/agents`` returned, or runtime hydration is incomplete);
    - ``CURSOR_API_KEY`` env var is unset/empty (no cloud auth possible).

    Each fall-back path emits a ``[cloud sse] ...`` notice on stderr per
    the workspace **No-Silent-Failures** rule so the user understands why
    the SSE channel is silent and why we're using the existing poll-driven
    view alone.
    """
    agent_id = status_info.get("cursor_agent_id")
    run_id = status_info.get("cursor_run_id")
    if not agent_id or not isinstance(agent_id, str):
        logger.warning(
            "cloud SSE skipped for task_id=%s: cursor_agent_id missing/invalid (%r)",
            task_id,
            agent_id,
        )
        typer.echo(
            f"[cloud sse] cursor_agent_id missing for {task_id}; using poll-only view",
            err=True,
        )
        return None
    if not run_id or not isinstance(run_id, str):
        logger.warning(
            "cloud SSE skipped for task_id=%s: cursor_run_id missing/invalid (%r)",
            task_id,
            run_id,
        )
        typer.echo(
            f"[cloud sse] cursor_run_id missing for {task_id}; using poll-only view",
            err=True,
        )
        return None

    # v0.9.2: route through the resolver so SSE attach honours OS
    # keyring storage in addition to the historical env-var path. The
    # downgrade to poll-only when no slot answers preserves backward
    # compatibility with the v0.8.6+ stderr one-liner contract.
    from popolaloom.credentials import resolve_cursor_api_key

    api_key = resolve_cursor_api_key()
    if not api_key:
        logger.warning(
            "cloud SSE skipped for task_id=%s: no Cursor API key configured",
            task_id,
        )
        typer.echo(
            "[cloud sse] no Cursor API key configured "
            "(set CURSOR_API_KEY env or run `popola auth cursor set`); "
            "using poll-only view",
            err=True,
        )
        return None

    sink = _CloudSSEEventSink()
    thread = threading.Thread(
        target=_run_cloud_sse_pump,
        kwargs={
            "api_key": api_key,
            "task_id": task_id,
            "agent_id": agent_id,
            "run_id": run_id,
            "sink": sink,
            "stop_event": stop_event,
        },
        name=f"popola-cloud-sse-{task_id}",
        daemon=True,
    )
    thread.start()
    return thread


def _run_cloud_sse_pump(
    *,
    api_key: str,
    task_id: str,
    agent_id: str,
    run_id: str,
    sink: _CloudSSEEventSink,
    stop_event: threading.Event,
) -> None:
    """Pump Cursor SSE into ``sink`` until done, error, or ``stop_event``.

    Constructs a fresh :class:`CloudCursorClient` (one per attach session,
    closed in the ``finally`` block), wires it to a :class:`SSEReader`
    pointed at our duck-typed sink, and drives ``pump()``. ``pump()``
    already absorbs :class:`CursorCloudStreamExpiredError` and
    ``httpx.RemoteProtocolError`` / ``httpx.ReadError`` internally —
    emitting ``cloud.sse.stream_expired`` / ``cloud.sse.parse_error``
    envelopes — so the worker only has to catch the broader
    ``httpx.ConnectError`` / ``httpx.TimeoutException`` /
    :class:`CursorCloudStreamInvalidLastEventIdError` / generic
    :class:`CursorCloudError` cases.

    On any non-graceful exit (``stop_event`` not set), emits a single
    ``cloud.sse.fallback_to_poll`` envelope via the sink + a stderr
    one-liner so users see the transition to poll-only mode (AC e + g).
    """
    fallback_reason: str | None = None
    fallback_error: str | None = None
    client: CloudCursorClient | None = None
    try:
        try:
            client = CloudCursorClient(api_key)
        except (ValueError, CursorCloudError) as exc:
            logger.warning("cloud SSE client init failed: %s", exc)
            fallback_reason = "client_init_failed"
            fallback_error = f"{type(exc).__name__}: {exc}"
            return

        try:
            reader = SSEReader(
                client,
                cast("EventLog", sink),
                task_id,
                run_id,
                agent_id=agent_id,
            )
        except (TypeError, AssertionError) as exc:
            logger.warning("cloud SSE reader init rejected: %s", exc)
            fallback_reason = "reader_init_failed"
            fallback_error = f"{type(exc).__name__}: {exc}"
            return

        try:
            reader.pump(stop_event=stop_event)
        except CursorCloudStreamInvalidLastEventIdError as exc:
            logger.warning(
                "cloud SSE invalid Last-Event-ID for task_id=%s; aborting: %s",
                task_id,
                exc,
            )
            fallback_reason = "invalid_last_event_id"
            fallback_error = f"{type(exc).__name__}: {exc}"
        except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError) as exc:
            logger.warning(
                "cloud SSE network error for task_id=%s: %s", task_id, exc
            )
            fallback_reason = "network_error"
            fallback_error = f"{type(exc).__name__}: {exc}"
        except CursorCloudError as exc:
            logger.warning(
                "cloud SSE Cursor API error for task_id=%s: %s", task_id, exc
            )
            fallback_reason = "cursor_error"
            fallback_error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 — surface unexpected, then fallback
            logger.exception("cloud SSE unexpected error for task_id=%s", task_id)
            fallback_reason = "unexpected_error"
            fallback_error = f"{type(exc).__name__}: {exc}"
        else:
            fallback_reason = "stream_ended"
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001 — close failure is non-fatal
                logger.debug("cloud SSE client.close() failed: %s", exc)
        if not stop_event.is_set() and fallback_reason is not None:
            data: dict[str, Any] = {
                "reason": fallback_reason,
                "task_id": task_id,
            }
            if fallback_error is not None:
                data["error"] = fallback_error
            try:
                sink.append("cloud.sse.fallback_to_poll", data)
            except Exception:  # noqa: BLE001 — sink errors must never crash worker
                logger.debug("cloud SSE failed to append fallback envelope", exc_info=True)
            try:
                typer.echo(
                    f"[cloud sse] stream ended ({fallback_reason}); switching to poll",
                    err=True,
                )
            except Exception:  # noqa: BLE001 — stderr write must never crash worker
                logger.debug("cloud SSE failed to write stderr notice", exc_info=True)


# ── list ──────────────────────────────────────────────────────────────────


@app.command(name="list")
def list_active(
    state: str | None = typer.Option(
        None,
        "--state",
        help="Filter by state (running/pending). Default: all non-terminal.",
    ),
    include_terminal: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Include terminal tasks (completed/failed/canceled).",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
    no_runtime: bool = typer.Option(
        False,
        "--no-runtime",
        help=(
            "Hide the runtime column (escape hatch). v0.8.6: column is shown by "
            "default; --json output always includes the field."
        ),
    ),
) -> None:
    """List currently-running (non-terminal) tasks, optionally filtered by state.

    v0.8.6 (T2.1.2): the table gains a default-on ``runtime`` column showing
    ``local`` (subprocess) vs ``cloud`` (Cursor Cloud Agent) per row, sourced
    from ``TaskHandle.runtime`` via the daemon ``/list`` summary builder.
    The ``--no-runtime`` flag hides the column without affecting JSON output
    (``--json`` always emits the field, no schema change).
    Legacy rows missing the field render ``"-"`` (No Silent Failures).
    Column order: ``task_id, runtime, cli, state, pid, started_at``.
    """
    try:
        with make_sync_client() as client:
            r = client.get("/list", params={"include_terminal": include_terminal})
    except httpx.ConnectError as exc:
        _render_connect_error(exc)
        return

    if r.status_code != 200:
        typer.echo(f"error: list unexpected {r.status_code}: {r.text}", err=True)
        raise typer.Exit(code=1)

    items = r.json()
    if state is not None:
        items = [item for item in items if item.get("state") == state]

    if json_out:
        typer.echo(json.dumps(items, ensure_ascii=False))
        return

    if not items:
        _console_out.print(Text("No active tasks.", style="dim"))
        return

    table = Table(
        title="Active tasks" if not include_terminal else "All tasks",
        show_header=True,
        header_style="bold",
    )
    columns: tuple[str, ...] = (
        ("task_id", "cli", "state", "pid", "started_at")
        if no_runtime
        else ("task_id", "runtime", "cli", "state", "pid", "started_at")
    )
    for col in columns:
        table.add_column(col)
    for item in items:
        row: list[str] = [item.get("task_id", "")]
        if not no_runtime:
            row.append(item.get("runtime") or "-")
        row.extend(
            [
                item.get("cli", ""),
                item.get("state", ""),
                "" if item.get("pid") is None else str(item["pid"]),
                item.get("started_at", ""),
            ]
        )
        table.add_row(*row)
    _console_out.print(table)


# ── cancel ────────────────────────────────────────────────────────────────


@app.command()
def cancel(
    task_id: str = typer.Argument(
        ..., help="Task identifier to cancel (SIGTERM, SIGKILL after 5s)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a plain line."),
) -> None:
    """Send SIGTERM to a task subprocess; SIGKILL after 5s grace."""
    try:
        with make_sync_client() as client:
            r = client.post(f"/cancel/{task_id}")
    except httpx.ConnectError as exc:
        _render_connect_error(exc)
        return

    if r.status_code == 404:
        typer.echo(f"error: task not found: {task_id}", err=True)
        raise typer.Exit(code=1)
    if r.status_code == 409:
        typer.echo(f"error: cannot cancel: {r.json().get('detail', '')}", err=True)
        raise typer.Exit(code=1)
    if r.status_code != 200:
        typer.echo(f"error: cancel unexpected {r.status_code}: {r.text}", err=True)
        raise typer.Exit(code=1)

    info = r.json()
    if json_out:
        typer.echo(json.dumps(info, ensure_ascii=False))
    else:
        sig = info["requested_signal"]
        esc = " (escalated to SIGKILL)" if info["escalated_to_sigkill"] else ""
        typer.echo(f"cancel requested for {task_id}: {sig}{esc}")


# ── probe ─────────────────────────────────────────────────────────────────


@app.command()
def probe(
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Lightweight health probe — ``daemon_pid``, ``uptime``, ``active_tasks``."""
    try:
        with make_sync_client() as client:
            r = client.get("/probe")
    except httpx.ConnectError as exc:
        _render_connect_error(exc)
        return

    if r.status_code != 200:
        typer.echo(f"error: probe unexpected {r.status_code}: {r.text}", err=True)
        raise typer.Exit(code=1)

    info = r.json()
    if json_out:
        typer.echo(json.dumps(info, ensure_ascii=False))
        return

    table = Table(title="popolad probe", show_header=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    for key in ("daemon_pid", "started_at", "uptime_seconds", "active_tasks", "version"):
        table.add_row(key, str(info.get(key, "")))
    _console_out.print(table)


# ── helpers ───────────────────────────────────────────────────────────────


def _wait_for_terminal(task_id: str, *, timeout_s: float) -> None:
    """Poll ``/status/{task_id}`` until terminal or timeout."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    last_state = "<unknown>"
    try:
        with make_sync_client() as client:
            while True:
                r = client.get(f"/status/{task_id}")
                if r.status_code != 200:
                    typer.echo(
                        f"warning: --wait status {r.status_code}: {r.text}",
                        err=True,
                    )
                    return
                info = r.json()
                last_state = info["state"]
                if last_state in _TERMINAL_STATES:
                    return
                if time.monotonic() >= deadline:
                    typer.echo(
                        f"warning: --wait timed out after {timeout_s}s; "
                        f"task {task_id} still in state={last_state}",
                        err=True,
                    )
                    return
                time.sleep(_POLL_INTERVAL_S)
    except httpx.ConnectError as exc:
        _render_connect_error(exc)


def _parse_cli_flags(flags: list[str]) -> dict[str, Any]:
    """Parse repeatable ``--cli-flag KEY=VAL`` into a dict (R-012).

    Values are parsed as JSON when possible (e.g. ``yolo=true`` →
    ``{"yolo": True}``); falls back to string when JSON decoding fails
    (e.g. ``output_format=text`` → ``{"output_format": "text"}``).

    Raises:
        typer.BadParameter: when a flag is missing ``=``.
    """
    result: dict[str, Any] = {}
    for raw in flags:
        if "=" not in raw:
            raise typer.BadParameter(
                f"--cli-flag must be KEY=VAL form, got: {raw!r}"
            )
        key, _, value = raw.partition("=")
        key = key.strip()
        if not key:
            raise typer.BadParameter(f"--cli-flag missing key: {raw!r}")
        try:
            parsed: Any = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        result[key] = parsed
    return result


def _load_dispatch_preferences_or_exit() -> Any | None:
    """Load ``[user_preferences]`` for dispatch or exit 1 on invalid config."""
    from popolaloom.cli.init_cmd import load_user_preferences_for_cli

    try:
        return load_user_preferences_for_cli()
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        typer.echo(f"error: invalid popolad.toml user_preferences: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _try_load_dispatch_preferences() -> Any | None:
    """v1.3.0 P6 — best-effort variant of ``_load_dispatch_preferences_or_exit``.

    Used by Path-B / cursor.default_model fall-back code paths that want
    to consult ``[user_preferences]`` if it exists but must NOT exit when
    the TOML file is absent or malformed (the caller already validates
    elsewhere; here we only enrich missing knobs).

    Returns ``None`` for both "missing file" and "malformed TOML";
    malformed TOML is still surfaced via the regular load path
    (the dispatch flow always also calls
    :func:`_load_dispatch_preferences_or_exit` in the cursor-cloud branch
    BEFORE this helper runs, so the loud failure is preserved upstream).
    """
    try:
        from popolaloom.cli.init_cmd import load_user_preferences_for_cli

        return load_user_preferences_for_cli()
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return None


def _select_cli_from_preferences(
    prefs: Any,
    *,
    extra: dict[str, Any],
    cwd: Path | None,
    cloud_target_flag: str = "",
    worker_name_flag: str = "",
    prompt: str = "",
    wizard: bool = False,
    no_wizard: bool = False,
    allow_fallback: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Resolve an omitted ``--cli`` using the present preferences block.

    The per-task ``cloud_target_flag`` / ``worker_name_flag`` are forwarded to
    :func:`_apply_cloud_preferences` so the precedence resolver (B3 AC 4)
    sees them whenever the runtime resolves to ``cursor-cloud``.
    """
    if wizard or (
        not no_wizard
        and getattr(getattr(prefs, "dispatch", None), "ambiguity_resolution", "")
        == "prompt"
    ):
        from popolaloom.cli.dispatch_wizard import run_dispatch_wizard

        return run_dispatch_wizard(
            prefs,
            prompt=prompt,
            extra=extra,
            cwd=cwd,
        )

    if (
        not no_wizard
        and getattr(getattr(prefs, "dispatch", None), "ambiguity_resolution", "")
        == "fail"
    ):
        typer.echo(
            "error: dispatch preferences require explicit dimensions; "
            "pass --cli or use --wizard. "
            "(dispatch 配置要求明确派发维度;请传 --cli 或使用 --wizard)",
            err=True,
        )
        raise typer.Exit(code=2)

    runtime = str(prefs.default_runtime)
    if bool(prefs.prompt_each_dispatch) or runtime == "ask-each-time":
        cli = _prompt_cli_from_preferences(prefs)
    elif runtime == "cloud":
        cli = "cursor-cloud"
    else:
        cli = str(prefs.default_local_cli)

    selected_extra = dict(extra)
    if bool(prefs.follow_devola_flow):
        selected_extra.setdefault("follow_devola_flow", True)

    if cli in {"cloud", "cursor-cloud"}:
        return "cursor-cloud", _apply_cloud_preferences(
            prefs,
            selected_extra,
            cwd=cwd,
            cloud_target_flag=cloud_target_flag,
            worker_name_flag=worker_name_flag,
        )

    if cli not in {"cursor", "claude", "codex", "copilot"}:
        typer.echo(
            "error: preference prompt must select one of "
            "cursor, claude, codex, copilot, cursor-cloud",
            err=True,
        )
        raise typer.Exit(code=2)
    # v1.6.0 (feedback_for_v1.5.2 constraint #2): when the operator
    # explicitly requested ``--cloud-target=self-hosted`` we MUST NOT
    # consult ``fallback_chain`` — even if the resolver lands here via
    # a misconfigured pref (the auto-set ``--cli=cursor-cloud`` branch
    # in ``dispatch()`` already covers the common case; this defends
    # the rare path where prefs say ``default_runtime="local"`` but
    # the per-task flag says ``self-hosted``). Hard-fail with the
    # existing self-hosted hint instead of walking the chain.
    effective_allow_fallback = (
        allow_fallback and cloud_target_flag != "self-hosted"
    )
    return _select_available_local_cli(
        cli, prefs, extra=selected_extra, allow_fallback=effective_allow_fallback
    )


def _prompt_cli_from_preferences(prefs: Any) -> str:
    """Prompt once for the runtime/CLI when preferences request it."""
    default = "cursor-cloud" if prefs.default_runtime == "cloud" else prefs.default_local_cli
    raw = typer.prompt(
        "[prefs] CLI for this dispatch (cursor/claude/codex/copilot/cursor-cloud)",
        default=default,
    )
    return str(raw).strip()


def _select_available_local_cli(
    requested: str,
    prefs: Any,
    *,
    extra: dict[str, Any],
    allow_fallback: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Return the requested local CLI, or walk the fallback chain (opt-in only).

    v1.5.0 No-Silent-Fallback contract (per
    ``feedback_for_v1.4.0.md`` operator-added hard constraint, surfaced
    in PLAN §"硬约束 — 禁止 silent fallback"):

    * **Default behaviour (allow_fallback=False)** — when the
      ``requested`` adapter is unavailable, this function hard-fails
      with exit code 1 and renders an actionable error pointing at the
      ``--allow-fallback`` opt-in flag. The persisted
      ``[user_preferences.routing].fallback_chain`` is NOT consulted.
      This matches the No-Silent-Failures workspace rule: popola will
      never silently switch the dispatched CLI adapter without explicit
      operator consent.
    * **allow_fallback=True** — the caller has explicitly passed
      ``--allow-fallback`` on the dispatch CLI; the resolver walks
      ``prefs.fallback_chain`` as it did pre-v1.5.0 and emits a stderr
      ``[prefs] (fallback consent acknowledged) ...`` line per
      switch (No Silent Failures: the switch is visible).

    Failure mode: when every candidate is unavailable, exit 1 with a
    list of every checked adapter so the operator can diagnose. SSE
    observability fallbacks (``cloud.sse.fallback_to_poll``) are
    explicitly OUT OF SCOPE of this invariant per PLAN §硬约束 — they
    sit at the observability layer, not the dispatch-routing layer.
    """
    if _local_cli_available(requested):
        return requested, extra

    fallback_chain = list(getattr(prefs, "fallback_chain", []) or [])

    if not allow_fallback:
        typer.echo(
            f"error: --cli={requested!r} is not available; "
            f"fallback_chain={fallback_chain!r} ignored. "
            f"Pass --allow-fallback to opt into auto-switching, OR "
            f"re-dispatch with an explicit --cli=<X> that is installed. "
            f"(v1.5.0 no-silent-fallback invariant: popola will NOT "
            f"switch the dispatched CLI adapter without explicit "
            f"operator consent.) "
            f"(--cli={requested!r} 不可用;默认不自动回退到 "
            f"fallback_chain={fallback_chain!r}。需要显式传 "
            f"--allow-fallback 才会启用回退,或重新指定 --cli=<X>。)",
            err=True,
        )
        raise typer.Exit(code=1)

    # allow_fallback=True — operator explicitly opted in.
    candidates: list[str] = []
    for name in [requested, *fallback_chain]:
        if name not in candidates:
            candidates.append(name)

    unavailable: list[str] = []
    for candidate in candidates:
        if _local_cli_available(candidate):
            if candidate != requested:
                typer.echo(
                    f"[prefs] (fallback consent acknowledged) "
                    f"--cli={requested} unavailable; switched to {candidate} "
                    f"per fallback_chain",
                    err=True,
                )
            return candidate, extra
        unavailable.append(candidate)

    typer.echo(
        "error: no preferred local CLI adapter is available "
        f"(checked: {', '.join(unavailable)}). "
        "Even with --allow-fallback the fallback_chain is exhausted; "
        "install one of the listed adapters or pick a different --cli.",
        err=True,
    )
    raise typer.Exit(code=1)


def _local_cli_available(name: str) -> bool:
    """Return adapter availability, treating unregistered local names as missing."""
    try:
        adapter = get_adapter(name)
    except KeyError:
        return False
    try:
        return bool(adapter.is_available())
    except Exception as exc:  # noqa: BLE001 - availability failures must be visible
        typer.echo(f"[prefs] {name} availability check failed: {exc}", err=True)
        return False


def _validate_cloud_target_flags(cloud_target: str, worker_name: str) -> None:
    """Validate the per-task ``--cloud-target`` / ``--worker-name`` pair.

    Per DECISIONS Q-6 + Q-7 and PLAN B3 AC 3:

    * ``--cloud-target=ask-each-time`` is rejected at dispatch time — the
      value is only valid as ``[user_preferences].default_cloud_target``;
      the resolver MUST collapse it before dispatch.
    * Any other ``--cloud-target`` value outside
      :data:`_VALID_CLOUD_TARGETS_DISPATCH` (i.e. not in
      ``{"self-hosted", "cursor-managed"}``) is rejected.
    * ``--cloud-target=self-hosted`` REQUIRES ``--worker-name``; the
      resolver has no pref-level fallback for the worker name (the
      ``[user_preferences]`` schema records a default target only, not a
      default worker name) — and Q-7 forbids any local-CLI fallback.
    * ``--cloud-target=cursor-managed`` REJECTS ``--worker-name``: the
      cursor-managed cloud has no notion of a per-worker route.
    * ``--worker-name`` outside ``--cloud-target=self-hosted`` is rejected
      (the ``iff`` semantics in AC 3).

    All invalid combinations exit with :data:`_EXIT_INVALID_ARGS` (2) and a
    bilingual (English + Chinese) error message — No Silent Failures.
    """
    if cloud_target == "ask-each-time":
        typer.echo(
            "error: --cloud-target=ask-each-time is invalid at dispatch time; "
            "use it only as [user_preferences].default_cloud_target. "
            "Choose --cloud-target=self-hosted or --cloud-target=cursor-managed. "
            "(--cloud-target=ask-each-time 仅可作为 [user_preferences].default_cloud_target "
            "的默认值,派发时请明确指定 --cloud-target=self-hosted "
            "或 --cloud-target=cursor-managed)",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)
    if cloud_target and cloud_target not in _VALID_CLOUD_TARGETS_DISPATCH:
        typer.echo(
            f"error: --cloud-target={cloud_target!r} is not one of "
            "{'self-hosted', 'cursor-managed', 'ask-each-time'}. "
            f"(--cloud-target={cloud_target!r} 取值非法,必须是 "
            "'self-hosted'、'cursor-managed' 或 'ask-each-time' 之一)",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)
    if cloud_target == "self-hosted" and not worker_name:
        typer.echo(
            "error: --cloud-target=self-hosted requires --worker-name=<X>. "
            "Hint: register the worker first with "
            "`popola cloud worker start --name <X> --worker-dir <repo-root>`; "
            "no local fallback is taken (Q-7). "
            "(--cloud-target=self-hosted 必须同时提供 --worker-name=<名称>;"
            "请先运行 `popola cloud worker start --name <X> --worker-dir <repo-root>` "
            "注册 Worker;此处不会回退到本地执行)",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)
    if cloud_target == "cursor-managed" and worker_name:
        typer.echo(
            "error: --cloud-target=cursor-managed cannot be combined with "
            "--worker-name (mutual exclusion per Q-6); cursor-managed "
            "dispatches do not route to a named self-hosted worker. "
            "(--cloud-target=cursor-managed 与 --worker-name 互斥,"
            "cursor-managed 云端派发不会路由到指定的自托管 Worker)",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)
    if not cloud_target and worker_name:
        typer.echo(
            "error: --worker-name requires --cloud-target=self-hosted "
            "(per Q-6 the iff semantics: --worker-name is meaningful only "
            "alongside --cloud-target=self-hosted). "
            "(--worker-name 必须搭配 --cloud-target=self-hosted 使用,"
            "其它场景下 --worker-name 无意义)",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)


# Q-17 LOCKED — built-in preset catalog. Each entry is a partial dict
# of (mode, max_mode, effort, time_budget, long_running,
# auto_proceed_after_plan); the resolver expands it into the equivalent
# explicit flags (later flags override preset values).
_BUILTIN_PRESETS: dict[str, dict[str, Any]] = {
    "quick-fix": {
        "mode": "agent",
        "effort": "medium",
        "time_budget": "600s",
    },
    "long-running-plan": {
        "mode": "plan",
        "effort": "high",
        "time_budget": "3600s",
        "long_running": True,
        "auto_proceed_after_plan": True,
    },
    "exploration": {
        "mode": "ask",
        "effort": "medium",
    },
    "review": {
        "mode": "ask",
        "effort": "high",
        "max_mode": True,
    },
    # v1.1.0 (Track 6) — user-facing "Grind mode" entry point per Cursor's
    # UI feature naming. Bundles plan + high effort + 4h budget +
    # long_running + auto-proceed-after-plan so the operator gets the
    # closest single-flag equivalent of Cursor's "Grind" toggle.
    "grind": {
        "mode": "plan",
        "effort": "high",
        "long_running": True,
        "time_budget": "14400s",
        "auto_proceed_after_plan": True,
    },
}


def _parse_time_budget(value: str) -> int:
    """Parse a ``--time-budget`` value (Q-18) → seconds (int).

    Accepted forms: ``"60"`` / ``"60s"`` / ``"30m"`` / ``"1h"``. Empty
    string returns 0; negative values are rejected.

    Raises:
        typer.BadParameter: with a bilingual hint when the value is
            unparseable. The CLI surface translates this to exit 2.
    """
    import re

    if not value:
        return 0
    match = re.fullmatch(r"(\d+)([smh]?)", value.strip())
    if not match:
        raise typer.BadParameter(
            f"--time-budget={value!r} not in accepted forms "
            f"(integer-seconds | <int>s | <int>m | <int>h); "
            f"(--time-budget={value!r} 取值非法,可使用 60 / 60s / 30m / 1h)"
        )
    n = int(match.group(1))
    suffix = match.group(2) or "s"
    multiplier = {"s": 1, "m": 60, "h": 3600}[suffix]
    return n * multiplier


def _apply_preset(
    extra: dict[str, Any],
    preset: str,
    *,
    explicit: dict[str, Any],
) -> dict[str, Any]:
    """Expand ``--preset <name>`` into the path-B extras (Q-17).

    Built-in catalog: see :data:`_BUILTIN_PRESETS`. Custom catalog: read
    from ``~/.config/popola/presets.toml`` (TOML overlay; v1.0.0 ships
    the loader but does not require the file to exist). Explicit per-task
    flags override preset values when both are set (preset is sugar for
    a flag combination; explicit flags WIN).

    Returns the merged dict (preset defaults + explicit overrides),
    suitable for direct merge into the ``extra`` dict the dispatcher
    consumes.
    """
    if not preset:
        return explicit
    catalog: dict[str, dict[str, Any]] = dict(_BUILTIN_PRESETS)
    overlay_path = Path.home() / ".config" / "popola" / "presets.toml"
    if overlay_path.exists():
        try:
            import tomllib

            with overlay_path.open("rb") as fp:
                overlay_data = tomllib.load(fp)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            logger.warning(
                "failed to load custom presets from %s: %s; using built-ins only",
                overlay_path,
                exc,
            )
        else:
            for k, v in overlay_data.items():
                if isinstance(v, dict):
                    catalog[k] = v
    if preset not in catalog:
        valid = sorted(catalog.keys())
        typer.echo(
            f"error: --preset={preset!r} not in {valid}; "
            f"(--preset={preset!r} 必须是 {valid} 之一)",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)
    base = dict(catalog[preset])
    base.update(explicit)
    return base


def _apply_path_b_flags(
    extra: dict[str, Any],
    *,
    cli: str,
    auth_mode: str,
    auth_mode_explicit: bool | None = None,
    mode: str,
    max_mode: bool,
    effort: str,
    time_budget: str,
    long_running: bool,
    auto_proceed_after_plan: bool,
    preset: str,
    thinking_level: str = "",
    auto_branch: bool = True,
    auto_create_pr: bool = False,
    work_on_current_branch: bool = False,
    skip_reviewer_request: bool = False,
    prefs: Any = None,
) -> None:
    """Validate Path-B Typer knobs before POST /dispatch (Q-13 / Q-19 / Q-22).

    ``--auth-mode=rest`` (default) rejects Path-B-exclusive flags because the
    stable REST gateway does not accept those fields.

    ``--auth-mode=session-jwt`` (v1.1.0+ wired): when ``cli=cursor-cloud``
    the JWT bundle is loaded eagerly via
    :func:`popolaloom.cloud.internal.jwt_auth.load_jwt_bundle` so the
    operator gets a friendly ``cursor login`` hint at dispatch time
    rather than at the supervisor's first RPC. On success we inject
    ``extra["__auth_mode__"] = "session-jwt"`` plus the resolved Path-B
    knobs (``mode`` / ``max_mode`` / ``effort`` / ``time_budget`` /
    ``long_running`` / ``auto_proceed_after_plan`` / ``thinking_level``)
    into ``extra`` so the
    daemon supervisor (:meth:`popolaloom.daemon.supervisor.Supervisor._spawn_cloud`)
    can branch on ``__auth_mode__`` and call
    :class:`popolaloom.cloud.internal.cursor_cloud_internal.CursorCloudInternalClient`
    instead of the REST :class:`CloudCursorClient`.

    v1.3.0 P2 surfaces ``thinking_level`` as a top-level Typer flag (it
    was already accepted by ``build_start_composer_request`` via
    ``--cli-flag thinking_level=`` but undiscoverable).

    v1.3.0 P6 (feedback §6) — when ``prefs`` is supplied, this function
    falls back to ``prefs.cursor_cloud.default_*`` for any Path-B knob
    NOT explicitly set by the per-task flags. Per-task flag wins;
    ``--preset`` wins over individual knob defaults; pref ``default_preset``
    wins over pref ``default_{mode,effort,...}``. The supervisor then sees
    a ``merged`` dict that already includes the persisted defaults, so a
    user who sets ``cursor-cloud.default_preset=grind`` once does not have
    to re-pass ``--preset=grind`` per dispatch.
    """
    raw_auth = auth_mode.strip().replace("_", "-").lower()
    if raw_auth == "jwt":
        raw_auth = "session-jwt"
    # v1.5.0 Phase H — consult `[user_preferences.cursor-cloud].default_auth_mode`
    # only when the operator left the CLI flag at its default ``"rest"``.
    # Pref==session-jwt + CLI==rest (default) → upgrade to session-jwt
    # (no Silent Failure: a stderr `[prefs] ...` line announces the
    # override so the operator sees what's happening). The dispatch CLI
    # ALWAYS wins when the operator explicitly passed `--auth-mode=...`;
    # the command path provides that bit via Click's parameter-source API.
    # Direct unit calls may leave auth_mode_explicit as None, preserving the
    # historical "raw==rest + non-empty pref override" behavior.
    if raw_auth == "rest" and prefs is not None and not auth_mode_explicit:
        cursor_cloud_prefs_node = getattr(prefs, "cursor_cloud", None)
        pref_auth = (
            str(getattr(cursor_cloud_prefs_node, "default_auth_mode", "") or "")
            if cursor_cloud_prefs_node is not None
            else ""
        )
        if pref_auth in {"rest", "session-jwt"} and pref_auth != "rest":
            typer.echo(
                f"[prefs] applying [user_preferences.cursor-cloud]."
                f"default_auth_mode={pref_auth!r} "
                f"(pass --auth-mode=rest explicitly to override). "
                f"(已应用 default_auth_mode={pref_auth!r})",
                err=True,
            )
            raw_auth = pref_auth
    normalized_auth_modes = frozenset({"rest", "session-jwt"})
    if raw_auth not in normalized_auth_modes:
        typer.echo(
            "error: --auth-mode="
            f"{auth_mode!r} must be one of {sorted(normalized_auth_modes)} "
            "(or the shorthand jwt). "
            f"( --auth-mode 只能是 rest / session-jwt / jwt; "
            f"当前为 {auth_mode!r} )",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)

    auth_mode_normalized = raw_auth

    explicit: dict[str, Any] = {}
    if mode:
        explicit["mode"] = mode
    if max_mode:
        explicit["max_mode"] = True
    if effort:
        explicit["effort"] = effort
    if time_budget:
        explicit["time_budget"] = time_budget
    if long_running:
        explicit["long_running"] = True
    if auto_proceed_after_plan:
        explicit["auto_proceed_after_plan"] = True
    if thinking_level:
        explicit["thinking_level"] = thinking_level

    if prefs is not None and getattr(prefs, "cursor_cloud", None) is not None:
        cc: Any = prefs.cursor_cloud
        default_mode_val: Any = getattr(cc, "default_mode", "")
        if "mode" not in explicit and default_mode_val:
            explicit["mode"] = default_mode_val
        default_effort_val: Any = getattr(cc, "default_effort", "")
        if "effort" not in explicit and default_effort_val:
            explicit["effort"] = default_effort_val
        default_max_mode_val: Any = getattr(cc, "default_max_mode", False)
        if "max_mode" not in explicit and default_max_mode_val:
            explicit["max_mode"] = True
        default_long_running_val: Any = getattr(cc, "default_long_running", False)
        if "long_running" not in explicit and default_long_running_val:
            explicit["long_running"] = True
        default_auto_proceed_val: Any = getattr(
            cc, "default_auto_proceed_after_plan", False
        )
        if (
            "auto_proceed_after_plan" not in explicit
            and default_auto_proceed_val
        ):
            explicit["auto_proceed_after_plan"] = True
        default_time_budget_val: Any = getattr(cc, "default_time_budget", "")
        if "time_budget" not in explicit and default_time_budget_val:
            explicit["time_budget"] = default_time_budget_val
        default_thinking_level_val: Any = getattr(cc, "default_thinking_level", "")
        if "thinking_level" not in explicit and default_thinking_level_val:
            explicit["thinking_level"] = default_thinking_level_val
        default_preset_val: Any = getattr(cc, "default_preset", "")
        if not preset and default_preset_val:
            preset = default_preset_val

    merged = _apply_preset(extra, preset, explicit=explicit)
    if not merged and auth_mode_normalized == "rest":
        return

    if cli != "cursor-cloud":
        if merged or auth_mode_normalized == "session-jwt":
            logger.warning(
                "path-B flags (--auth-mode=session-jwt or Path-B knobs) apply only "
                "to cursor-cloud dispatches; ignoring for --cli=%r "
                "(Path-B/session-jwt 仅作用于 cursor-cloud,已在 --cli=%s 忽略)",
                cli,
                cli,
            )
        return

    if merged and auth_mode_normalized == "rest":
        flag_list = sorted(
            {
                "--mode" if k == "mode" else (
                    "--max-mode" if k == "max_mode" else (
                        "--effort" if k == "effort" else (
                            "--time-budget" if k == "time_budget" else (
                                "--long-running" if k == "long_running" else (
                                    "--auto-proceed-after-plan"
                                    if k == "auto_proceed_after_plan"
                                    else (
                                        "--thinking-level"
                                        if k == "thinking_level"
                                        else f"--{k.replace('_', '-')}"
                                    )
                                )
                            )
                        )
                    )
                )
                for k in merged
            }
        )
        typer.echo(
            f"error: path-B flags {flag_list} require --auth-mode=session-jwt "
            f"(currently --auth-mode=rest). The Cursor REST schema does NOT "
            f"accept these fields (the gateway returns 'Unrecognized key'). "
            f"(path-B 标志 {flag_list} 需要 --auth-mode=session-jwt;"
            f"当前 --auth-mode=rest 不支持这些字段)",
            err=True,
        )
        raise typer.Exit(code=_EXIT_INVALID_ARGS)

    if auth_mode_normalized == "session-jwt":
        # v1.1.0 (Track 6) — Path-B is now wired end-to-end. Eagerly verify
        # the JWT is loadable so the operator sees the friendly
        # `cursor login` hint at dispatch time instead of inside the
        # daemon's RPC failure path. No Silent Failures: any
        # JWTAuthError propagates to a non-zero exit with hint surfaced.
        from popolaloom.cloud.internal.jwt_auth import (
            JWTAuthError,
            load_jwt_bundle,
        )

        try:
            load_jwt_bundle()
        except JWTAuthError as exc:
            typer.echo(
                f"error: --auth-mode=session-jwt could not load a JWT: {exc}",
                err=True,
            )
            if exc.hint:
                typer.echo(f"hint: {exc.hint}", err=True)
            raise typer.Exit(code=1) from exc

        # v1.5.0 PLAN Phase L empirical finding (post-PR-#36 verification
        # round, 2026-05-17): Cursor's path-B Connect-RPC
        # ``StartBackgroundComposerFromSnapshot`` SILENTLY downgrades the
        # ``env={type:"machine",name:X}`` field to ``env={type:"pool"}``
        # server-side. Pool routing does NOT pin to the specific named
        # worker — Cursor's server picks any matching worker from the
        # user's pool. Direct routing to a NAMED self-hosted worker
        # (G3 of feedback_for_v1.4.0) requires the REST path-A flow
        # via ``--auth-mode=rest`` + ``CURSOR_API_KEY``.
        #
        # Surfacing this empirically-discovered limitation per the
        # No-Silent-Fallback invariant: we WARN strongly but do NOT
        # auto-switch transports. The operator either:
        #   (a) accepts pool-level routing on path-B (any free worker
        #       in their pool matching the repo claims the task), OR
        #   (b) re-dispatches with --auth-mode=rest for guaranteed
        #       named-worker routing.
        cloud_target_val = str(extra.get("cloud_target", ""))
        worker_name_val = str(extra.get("worker_name", ""))
        if (
            cloud_target_val == "self-hosted"
            and worker_name_val
            and merged.get("env_emit_mode") != "explicit_pool_ack"
        ):
            typer.echo(
                "warn: path-B (--auth-mode=session-jwt) + "
                f"--cloud-target=self-hosted --worker-name={worker_name_val!r} "
                "has a known Cursor server-side limitation (v1.5.0 PLAN "
                "Phase L empirical finding, 2026-05-17): the upstream "
                "Connect-RPC silently downgrades env={type:machine,name:X} "
                "to env={type:pool}. The dispatch will still reach a "
                "worker in your private pool that matches the repo, but "
                "NOT necessarily the named worker. For guaranteed "
                "named-worker routing re-dispatch with --auth-mode=rest "
                "(requires CURSOR_API_KEY). popola does NOT auto-switch "
                "transports per v1.5.0 no-silent-fallback invariant. "
                "(path-B + 自托管 + worker-name 组合时 Cursor 服务端会把 "
                "env 降级到 pool;若需精确路由到指定 Worker,请改用 "
                "--auth-mode=rest;popola 不会自动切换)",
                err=True,
            )

        # Inject the Path-B routing marker the supervisor branches on.
        extra["__auth_mode__"] = "session-jwt"
        # Forward every Path-B knob the user / preset resolved into
        # `merged`. Only set keys that are present so downstream code
        # can distinguish "unset" (use Cursor default) from "explicit
        # value" (overrides default). The supervisor reads these back
        # to build the StartBackgroundComposerFromSnapshot RPC body.
        for key in (
            "mode",
            "max_mode",
            "effort",
            "time_budget",
            "long_running",
            "auto_proceed_after_plan",
            "thinking_level",
        ):
            if key in merged:
                extra[key] = merged[key]
        # v1.5.0 — write the new "skip branch / PR" bool knobs into
        # extras for the supervisor + builder to pick up. Only write
        # when the value diverges from the default so we don't add
        # noise to dispatches that didn't opt in.
        if not auto_branch:
            extra["auto_branch"] = False
        if auto_create_pr:
            extra["auto_create_pr"] = True
        if work_on_current_branch:
            extra["work_on_current_branch"] = True
        if skip_reviewer_request:
            extra["skip_reviewer_request"] = True
        logger.debug(
            "path-B enabled: mode=%r effort=%r long_running=%r "
            "max_mode=%r time_budget=%r auto_proceed_after_plan=%r "
            "thinking_level=%r model=%r auto_branch=%r "
            "auto_create_pr=%r work_on_current_branch=%r "
            "skip_reviewer_request=%r",
            extra.get("mode"),
            extra.get("effort"),
            extra.get("long_running"),
            extra.get("max_mode"),
            extra.get("time_budget"),
            extra.get("auto_proceed_after_plan"),
            extra.get("thinking_level"),
            extra.get("model"),
            extra.get("auto_branch", True),
            extra.get("auto_create_pr", False),
            extra.get("work_on_current_branch", False),
            extra.get("skip_reviewer_request", False),
        )


def _apply_model_flag(extra: dict[str, Any], model: str, cli: str) -> None:
    """Translate the ``--model`` first-class flag into ``extra["model"]``.

    v1.0.0 (Q-A1) — promotes the previously-stringly-typed
    ``--cli-flag model=<id>`` extras key into a discoverable Typer flag.
    The flag is only meaningful for cursor-cloud dispatches; non-cloud
    adapters (cursor / claude / codex / ...) get a soft WARN and the
    flag is dropped (the adapter would have ignored it anyway).

    Conflict: when both ``--model`` and ``--cli-flag model=<X>`` are
    supplied, the explicit ``--model`` flag wins, matching the precedent
    set by ``--cloud-target`` over ``--cli-flag cloud_target=`` in v0.10.0.
    A bilingual WARN is emitted in that case (No Silent Failures).

    Empty-string ``model`` is a no-op (callers gate on truthiness before
    calling), preserving the v0.10.0 ``"default"`` model fallback in
    :func:`popolaloom.adapters.cursor_cloud._normalize_cloud_extra`.
    """
    if not model:
        return
    if cli and cli != "cursor-cloud":
        logger.warning(
            "--model=%r is only consumed by cursor-cloud dispatches; "
            "ignored for --cli=%r (--model 仅对 cursor-cloud 有效, "
            "已忽略当前 --cli=%s 的 --model 值)",
            model,
            cli,
            cli,
        )
        return
    existing = extra.get("model")
    if existing is not None and existing != model:
        logger.warning(
            "--model=%r overrides --cli-flag model=%r "
            "(--model 与 --cli-flag model= 冲突,以 --model 为准)",
            model,
            existing,
        )
    extra["model"] = model


def _apply_cloud_preferences(
    prefs: Any | None,
    extra: dict[str, Any],
    *,
    cwd: Path | None,
    cloud_target_flag: str = "",
    worker_name_flag: str = "",
) -> dict[str, Any]:
    """Resolve cloud target precedence: per-task flag > pref > default.

    DECISIONS Q-6 + Q-7 / PLAN B3 AC 4-5 — the precedence is, highest first:

    1. The per-task ``--cloud-target`` / ``--worker-name`` flags (already
       validated upstream by :func:`_validate_cloud_target_flags`).
    2. ``[user_preferences].default_cloud_target`` (B1 — single-value
       field; replaces v0.9.10's ``cloud_target_priority`` list-of-targets).
    3. The hard-coded ``"ask-each-time"`` default — collapses to a no-op
       (no ``cloud_target`` / ``worker_name`` written to extras), so the
       cursor-cloud adapter routes to Cursor's managed cloud without an
       ``env`` field on the request body.

    The resolver writes the resolved pair into ``extra``:
    ``extra["cloud_target"] = "self-hosted"|"cursor-managed"`` and (for
    self-hosted only) ``extra["worker_name"] = <X>``. ``ask-each-time``
    omits both.

    The legacy ``cloud_target_priority`` list-of-targets path is REMOVED
    (B1's loader still parses it for back-compat with a one-time
    deprecation WARN; this resolver no longer consults it).

    Q-7 no-fallback contract: when the resolved target is ``self-hosted``
    and no worker name is recoverable from (a) the per-task flag, (b) the
    legacy ``--cli-flag worker_name=`` extra, (c) ``POPOLA_WORKER_NAME`` /
    ``POPOLA_SELF_HOSTED_WORKER_NAME`` env, or (d) a ``.popola-worker`` /
    ``.popola/worker_name`` file marker, the resolver hard-fails with
    :data:`_EXIT_INVALID_ARGS` (2) and a bilingual hint pointing at
    ``popola cloud worker start --name <X> --worker-dir <repo-root>`` —
    NOT at any ``--cli=cursor`` local path (No Silent Failures + the
    user's explicit "no Fall Back" constraint in feedback_for_v0.10.0
    L5+L11).

    The legacy ``--cli-flag worker_name=`` / ``--cli-flag use_private_worker=``
    escape hatches still work (per AC 6): they flow through ``extra``
    unchanged and are translated by A1's ``_normalize_cloud_extra`` inside
    ``cursor_cloud.py`` (with a ``DeprecationWarning``).
    """
    out = dict(extra)
    if prefs is not None:
        cursor_cloud_prefs = getattr(prefs, "cursor_cloud", None)
        if cursor_cloud_prefs is not None:
            pref_model = getattr(cursor_cloud_prefs, "model", "default")
            if pref_model != "default":
                out.setdefault("model", pref_model)
            pref_starting_ref = getattr(cursor_cloud_prefs, "starting_ref", "main")
            if pref_starting_ref != "main":
                out.setdefault("starting_ref", pref_starting_ref)
            for pref_key in (
                "auto_create_pr",
                "work_on_current_branch",
                "skip_reviewer_request",
            ):
                pref_value = getattr(cursor_cloud_prefs, pref_key, False)
                if pref_value:
                    out.setdefault(pref_key, pref_value)

    resolved_target: str
    resolved_worker_name: str
    if cloud_target_flag:
        resolved_target = cloud_target_flag
        resolved_worker_name = worker_name_flag
    elif prefs is not None:
        pref_target = str(getattr(prefs, "default_cloud_target", "ask-each-time"))
        if pref_target in _VALID_CLOUD_TARGETS_DISPATCH:
            resolved_target = pref_target
        else:
            resolved_target = "ask-each-time"
        resolved_worker_name = ""
    else:
        resolved_target = "ask-each-time"
        resolved_worker_name = ""

    if resolved_target == "self-hosted" and not resolved_worker_name:
        detected = _detect_self_hosted_worker_name(cwd, out)
        if detected:
            resolved_worker_name = detected
        else:
            typer.echo(
                "error: cloud_target=self-hosted requires a worker name "
                "(--worker-name=<X>, POPOLA_WORKER_NAME env, or "
                ".popola-worker / .popola/worker_name file marker). "
                "Hint: register the worker first with "
                "`popola cloud worker start --name <X> --worker-dir <repo-root>`; "
                "no local fallback is taken (Q-7). "
                "(cloud_target=self-hosted 需要 Worker 名称("
                "--worker-name=<名称>、POPOLA_WORKER_NAME 环境变量或 "
                ".popola-worker / .popola/worker_name 文件)。"
                "请先运行 `popola cloud worker start --name <X> --worker-dir <repo-root>` "
                "注册 Worker;此处不会回退到本地执行)",
                err=True,
            )
            raise typer.Exit(code=_EXIT_INVALID_ARGS)

    if resolved_target in _VALID_CLOUD_TARGETS_DISPATCH:
        out["cloud_target"] = resolved_target
    elif resolved_target == "ask-each-time":
        typer.echo(
            "[prefs] no explicit cloud target (default_cloud_target=ask-each-time); "
            "falling back to cursor-managed cloud",
            err=True,
        )
    if resolved_worker_name:
        out["worker_name"] = resolved_worker_name

    # v1.5.0 (PLAN Phase K hotfix; feedback_for_v1.4.0 G4) — auto-derive
    # ``repo_url`` from the workspace's git origin when the operator
    # dispatches to a self-hosted worker without providing one. Cursor's
    # path-B body (and the REST adapter's _normalize_cloud_extra) both
    # REQUIRE ``repos[0].url`` even when the worker checks out from
    # its own local clone (i.e. with --work-on-current-branch). G4's
    # acceptance is "argv doesn't contain --repo-url=..." — auto-deriving
    # satisfies that without dropping the field from the wire.
    if (
        resolved_target == "self-hosted"
        and "repo_url" not in out
        and "pr_url" not in out
    ):
        derived_repo_url = _derive_workspace_repo_url(cwd)
        if derived_repo_url:
            out["repo_url"] = derived_repo_url
            typer.echo(
                f"[prefs] auto-derived repo_url={derived_repo_url!r} from "
                f"workspace git remote (v1.5.0; pass --cli-flag repo_url=<X> "
                f"to override). "
                f"(已从 workspace git remote 自动派生 repo_url)",
                err=True,
            )
    return out


def _derive_workspace_repo_url(cwd: Path | None) -> str | None:
    """Return ``git remote get-url origin`` for ``cwd`` (or :data:`None`).

    Used by :func:`_apply_cloud_preferences` to satisfy the v1.5.0
    feedback G4 contract: the operator shouldn't have to pass
    ``--cli-flag repo_url=<X>`` for a self-hosted-worker dispatch.
    Best-effort; returns :data:`None` on any failure (not a git repo,
    no origin remote, git binary missing, etc.) so the caller can fall
    through to the existing error path with full diagnostic context.
    """
    import subprocess

    repo_root = (cwd or Path.cwd()).expanduser()
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo_root),
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        return None
    # Normalize the common ssh form ``git@github.com:owner/repo[.git]`` to
    # ``https://github.com/owner/repo`` so the value lands in the same
    # shape Cursor's BackgroundComposerService expects on path-B (and
    # the REST adapter's snapshotNameOrId derivation strips ``.git``
    # downstream regardless).
    if raw.startswith("git@") and ":" in raw:
        host, _, path = raw.partition(":")
        host = host[len("git@"):]
        raw = f"https://{host}/{path}"
    return raw


def _detect_self_hosted_worker_name(
    cwd: Path | None,
    extra: dict[str, Any],
) -> str | None:
    """Detect a self-hosted worker route from explicit extras, env, or markers."""
    raw_extra_worker = extra.get("worker_name")
    if isinstance(raw_extra_worker, str) and raw_extra_worker.strip():
        return raw_extra_worker.strip()
    for env_name in ("POPOLA_SELF_HOSTED_WORKER_NAME", "POPOLA_WORKER_NAME"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    root = (cwd or Path.cwd()).expanduser()
    for marker in (root / ".popola-worker", root / ".popola" / "worker_name"):
        try:
            value = marker.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return None


@dataclass(frozen=True, slots=True)
class _ReplayPayload:
    """Resolved dispatch parameters from a `--replay HANDOFF_ID` lookup."""

    cli: str
    prompt: str
    cwd: Path | None
    adapter_extra: dict[str, Any]


def _resolve_replay(
    handoff_id: str,
    user_prompt: str,
    user_cli: str,
    user_cwd: Path | None,
    user_cli_flag: list[str],
) -> _ReplayPayload:
    """Load envelope by id and produce a dispatch payload.

    v0.7.3+ ``popola dispatch --replay <handoff_id>`` reads the local
    envelope file and uses its stored fields (``target_cli`` / ``prompt`` /
    ``cwd`` / ``adapter_extra``) as the dispatch payload — an exact replay
    of a prior dispatch.

    If the user also passed ``prompt`` / ``--cli`` / ``--cwd`` /
    ``--cli-flag`` on the same invocation we WARN to stderr that those are
    overridden by the envelope (No Silent Failures — the user gets
    explicit feedback that their inline args were ignored).
    """
    from popolaloom.handoff import load_envelope

    try:
        env = load_envelope(handoff_id)
    except FileNotFoundError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        typer.echo(f"error: invalid handoff_id: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    overrides: list[str] = []
    if user_prompt:
        overrides.append(f"prompt={user_prompt!r}")
    if user_cli:
        overrides.append(f"--cli={user_cli!r}")
    if user_cwd is not None:
        overrides.append(f"--cwd={user_cwd!s}")
    if user_cli_flag:
        overrides.append(f"--cli-flag (×{len(user_cli_flag)})")

    if overrides:
        typer.echo(
            f"warning: --replay overrides inline {', '.join(overrides)} with envelope values",
            err=True,
        )

    return _ReplayPayload(
        cli=env.target_cli,
        prompt=env.prompt,
        cwd=Path(env.cwd) if env.cwd else None,
        adapter_extra=dict(env.adapter_extra) if env.adapter_extra else {},
    )


def _format_event(ev: dict[str, Any]) -> str:
    """Render a CloudEvents envelope as ``<time>  <type>  <data_summary>``."""
    time_s = ev.get("time", "")
    type_s = ev.get("type", "")
    data = ev.get("data", {})
    summary = _summarize_data(type_s, data)
    return f"{time_s}  {type_s}  {summary}"


def _summarize_data(event_type: str, data: Any) -> str:
    """Pick a short, human-friendly summary based on event type."""
    if not isinstance(data, dict):
        return repr(data)

    if event_type in {"process.stdout", "process.stderr"}:
        return str(data.get("line", ""))
    if event_type == "task.dispatched":
        return f"cli={data.get('cli')!r} prompt={data.get('prompt')!r}"
    if event_type in {"task.completed", "task.failed"}:
        parts: list[str] = [f"exit_code={data.get('exit_code')}"]
        runtime = data.get("runtime")
        if runtime and runtime != "local":
            parts.append(f"runtime={runtime}")
        error_kind = data.get("error_kind")
        if error_kind:
            parts.append(f"error_kind={error_kind}")
        err = data.get("error")
        if isinstance(err, dict):
            et = err.get("error_type")
            if et:
                parts.append(f"error_type={et}")
        return " ".join(parts)
    if event_type == "cloud.queued" and data:
        return (
            f"agent_id={data.get('agent_id')!r} "
            f"run_id={data.get('run_id')!r} "
            f"initial_phase={data.get('initial_phase')!r}"
        )
    # v0.8.8 T2.2.2 (Q-C-7 default-visibility): 429 / 409 queue events
    # render with a stable, grep-friendly prefix so attach UIs and tests
    # can match on the literal "WAITING:" / "DISPATCHED:" / "TIMEOUT:"
    # tokens. None of these events are debug-filtered: they ship inline
    # alongside the existing cloud.run_status / cloud.sse.* feed.
    if event_type == "cloud.queued_quota_exceeded":
        return _format_quota_waiting_line(data)
    if event_type == "cloud.queue_exit":
        outcome = data.get("outcome") if isinstance(data, dict) else None
        attempts = data.get("attempts") if isinstance(data, dict) else None
        wait_ms = data.get("total_wait_ms") if isinstance(data, dict) else None
        exit_parts: list[str] = ["QUEUE_EXIT"]
        if isinstance(outcome, str) and outcome:
            exit_parts.append(f"outcome={outcome}")
        if isinstance(attempts, int):
            exit_parts.append(f"attempts={attempts}")
        if isinstance(wait_ms, (int, float)) and wait_ms >= 0:
            exit_parts.append(f"total_wait={float(wait_ms) / 1000.0:.1f}s")
        return " ".join(exit_parts)
    if event_type == "cloud.busy_queued":
        return _format_busy_queue_line(data)
    if event_type == "cloud.busy_dispatched":
        agent = data.get("agent_id") if isinstance(data, dict) else None
        prev_run = data.get("prev_run_id") if isinstance(data, dict) else None
        new_run = data.get("new_run_id") if isinstance(data, dict) else None
        waited = data.get("waited_ms") if isinstance(data, dict) else None
        bits: list[str] = ["DISPATCHED:"]
        if isinstance(agent, str) and agent:
            bits.append(f"agent={agent}")
        if isinstance(prev_run, str) and prev_run:
            bits.append(f"prev_run={prev_run}")
        if isinstance(new_run, str) and new_run:
            bits.append(f"new_run={new_run}")
        if isinstance(waited, (int, float)) and waited >= 0:
            bits.append(f"waited={float(waited) / 1000.0:.1f}s")
        return " ".join(bits)
    if event_type == "cloud.busy_timeout":
        agent = data.get("agent_id") if isinstance(data, dict) else None
        run_id = (
            data.get("current_run_id_at_timeout")
            if isinstance(data, dict)
            else None
        )
        waited = data.get("waited_ms") if isinstance(data, dict) else None
        bits = ["TIMEOUT: agent_busy"]
        if isinstance(agent, str) and agent:
            bits.append(f"agent={agent}")
        if isinstance(run_id, str) and run_id:
            bits.append(f"current_run={run_id}")
        if isinstance(waited, (int, float)) and waited >= 0:
            bits.append(f"waited={float(waited) / 1000.0:.1f}s")
        return " ".join(bits)
    if event_type == "process.started":
        return f"pid={data.get('pid')} session_id={data.get('session_id')}"
    if event_type == "stream.truncated":
        return (
            f"stream={data.get('stream')} actual_lines={data.get('actual_lines')} "
            f"reason={data.get('reason')}"
        )
    if event_type == "state.ghost_exit":
        return f"reason={data.get('reason')!r} exit_code={data.get('exit_code')}"

    serialized = json.dumps(data, ensure_ascii=False, default=str)
    if len(serialized) > 120:
        serialized = serialized[:117] + "..."
    return serialized


def main() -> None:
    """Entry point for ``python -m popolaloom.cli.main`` debug invocations."""
    app()


if __name__ == "__main__":
    main()
