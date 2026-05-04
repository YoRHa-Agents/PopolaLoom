"""CLI renderer — Rich pending table + popola feedback command (v0.3.0 F4.B).

Per spec §3.4 + roadmap §12.5 + v0.3.0-plan §4 Stage F4.8.

Functions:

- :func:`render_pending_table` — Rich :class:`rich.table.Table` listing
  every pending HITL prompt (id / trigger / why / deadline / options).
- :func:`render_pending_text` — plain-text fallback (when Rich is
  unavailable or piped to a file).
- :func:`parse_reply` — convert ``(hitl_id, option_id, reason)`` into
  a uniform :class:`HITLReply`.

The actual Typer commands (``popola pending`` / ``popola feedback``)
are wired up in :mod:`popolaloom.cli.feedback` (separate module to
avoid bloating :mod:`popolaloom.cli.main`).

Workspace rule "No Silent Failures": empty inputs raise
:class:`ValueError` at the rendered level.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from popolaloom.hitl import HITLChannel, HITLPrompt, HITLReply

logger = logging.getLogger(__name__)

__all__ = [
    "deadline_remaining_human",
    "parse_reply",
    "render_pending_table",
    "render_pending_text",
]


def _coerce_to_prompt(entry: HITLPrompt | dict[str, Any]) -> tuple[HITLPrompt, dict[str, Any]]:
    """Normalise a CLI list entry to ``(HITLPrompt, row_dict)``.

    Accepts either a :class:`HITLPrompt` (use as-is, empty row dict) or a
    SQLite row dict from :class:`popolaloom.hitl.sync.HITLStore.list_pending`
    (parses ``prompt_json`` lazily).  Raises :class:`ValueError` for any
    other shape (workspace rule "No Silent Failures").
    """
    if isinstance(entry, HITLPrompt):
        return entry, {}
    if isinstance(entry, dict):
        prompt_raw = entry.get("prompt_json")
        if not isinstance(prompt_raw, str):
            raise ValueError(
                f"render_pending_*: dict entry missing prompt_json string; got keys "
                f"{sorted(entry.keys())}"
            )
        prompt = HITLPrompt.model_validate(json.loads(prompt_raw))
        return prompt, entry
    raise ValueError(
        f"render_pending_*: entry must be HITLPrompt or dict; got {type(entry).__name__}"
    )


def render_pending_table(prompts: Iterable[HITLPrompt | dict[str, Any]]) -> Any:
    """Render a Rich :class:`Table` of pending prompts.

    Caller may pass either fully-loaded :class:`HITLPrompt` instances or
    SQLite row dicts (from :meth:`HITLStore.list_pending`).  Returns the
    Rich Table object so callers may print to any console.

    Workspace rule "No Silent Failures": raises :class:`ImportError`
    when ``rich`` is not installed (test fallback should call
    :func:`render_pending_text` instead).
    """
    from rich.table import Table  # imported here so non-Rich tests don't fail

    table = Table(title="PopolaLoom · Pending HITL prompts", show_header=True)
    table.add_column("hitl_id", overflow="fold", no_wrap=False)
    table.add_column("trigger", style="cyan")
    table.add_column("why", overflow="fold", max_width=40)
    table.add_column("options", style="green")
    table.add_column("deadline", style="yellow")
    for entry in prompts:
        prompt, row = _coerce_to_prompt(entry)
        deadline_text = "—"
        deadline_at = row.get("deadline_at")
        if isinstance(deadline_at, str) and deadline_at:
            deadline_text = deadline_remaining_human(deadline_at)
        elif prompt.deadline_seconds:
            hrs = prompt.deadline_seconds // 3600
            mins = (prompt.deadline_seconds % 3600) // 60
            deadline_text = f"{hrs}h{mins}m"
        opts = ",".join(opt.id for opt in prompt.options)
        row_pid = row.get("hitl_id")
        pid = row_pid if isinstance(row_pid, str) and row_pid else prompt.ensure_prompt_id()
        table.add_row(
            pid,
            prompt.trigger,
            prompt.why[:120],
            opts,
            deadline_text,
        )
    return table


def render_pending_text(prompts: Iterable[HITLPrompt | dict[str, Any]]) -> str:
    """Plain-text fallback for environments without Rich.

    Format::

        hitl-abc123  approval  Confirm destructive merge  options: yes,no  deadline: 5h30m
        hitl-def456  ...
    """
    rows: list[str] = []
    for entry in prompts:
        prompt, row = _coerce_to_prompt(entry)
        deadline_text = "—"
        deadline_at = row.get("deadline_at")
        if isinstance(deadline_at, str) and deadline_at:
            deadline_text = deadline_remaining_human(deadline_at)
        elif prompt.deadline_seconds:
            hrs = prompt.deadline_seconds // 3600
            mins = (prompt.deadline_seconds % 3600) // 60
            deadline_text = f"{hrs}h{mins}m"
        opts = ",".join(opt.id for opt in prompt.options)
        row_pid = row.get("hitl_id")
        pid = row_pid if isinstance(row_pid, str) and row_pid else prompt.ensure_prompt_id()
        rows.append(
            f"{pid}  {prompt.trigger:18}  {prompt.why[:60]:60}  "
            f"options: {opts}  deadline: {deadline_text}"
        )
    if not rows:
        return "(no pending HITL prompts)"
    header = "hitl_id  trigger  why  options  deadline"
    return header + "\n" + "\n".join(rows)


def deadline_remaining_human(deadline_iso: str) -> str:
    """Render an ISO-8601 deadline as a human-friendly remaining duration.

    Returns ``"overdue"`` when the deadline is in the past;
    ``"<H>h<M>m"`` for future; ``"<S>s"`` for sub-minute.
    """
    try:
        dl = datetime.fromisoformat(deadline_iso.replace("Z", "+00:00"))
    except ValueError:
        return deadline_iso
    if dl.tzinfo is None:
        dl = dl.replace(tzinfo=UTC)
    delta = dl - datetime.now(UTC)
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "overdue"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60}m"


def parse_reply(
    hitl_id: str,
    option_id: str,
    reason: str | None = None,
    *,
    via: HITLChannel = "cli",
    responder: str | None = None,
) -> HITLReply:
    """Construct a :class:`HITLReply` from CLI invocation arguments.

    Args:
        hitl_id: prompt id (must be non-empty).
        option_id: chosen option id (must be non-empty).
        reason: optional rationale.
        via: channel id (defaults to ``"cli"``; the IDE notify
            renderer reuses this with ``via="ide"``).
        responder: optional opaque responder id (e.g. ``$USER``).

    Raises:
        ValueError: when ``hitl_id`` or ``option_id`` is blank.
    """
    if not hitl_id or not hitl_id.strip():
        raise ValueError("parse_reply: hitl_id must be non-empty")
    if not option_id or not option_id.strip():
        raise ValueError("parse_reply: option_id must be non-empty")
    return HITLReply(
        hitl_id=hitl_id.strip(),
        option_id=option_id.strip(),
        via=via,
        reason=reason.strip() if reason else None,
        responder=responder,
    )
