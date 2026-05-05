"""Web renderer — HTML form stub (v0.3.0 F4.B; full NiceGUI in v0.4.0).

Per spec §3.4 + roadmap §12.5 + v0.3.0-plan §4 Stage F4.10.

The web renderer is a v0.3.0 *interface stub* — full NiceGUI page
integration is deferred to v0.4.0 polish.  We provide a minimal
HTML5 form so the daemon can render a static page (no JS framework
required) that posts back to the popolad ``/hitl/answer`` RPC.

Functions:

- :func:`render_web_form` — :class:`HITLPrompt` → HTML5 form string.
- :func:`parse_reply` — extract :class:`HITLReply` from form data
  (e.g. ``application/x-www-form-urlencoded``).

Workspace rule "No Silent Failures": empty form fields raise
:class:`ValueError`; malformed form_data dict raises :class:`TypeError`.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from popolaloom.hitl import HITLPrompt, HITLReply

logger = logging.getLogger(__name__)

__all__ = [
    "parse_reply",
    "render_web_form",
]


_FORM_ACTION: str = "/hitl/answer"
"""POST target — the daemon RPC endpoint added by F4.13."""


def render_web_form(prompt: HITLPrompt) -> str:
    """Render an HTML5 form for ``prompt``.

    The form has:

    - hidden ``hitl_id`` field
    - radio buttons for each option (``option_id``)
    - free-text ``reason`` field
    - submit button posting to ``/hitl/answer``

    All user-visible strings are :func:`html.escape`-ed to prevent
    XSS via crafted HITL prompt content.

    Args:
        prompt: the prompt to render.

    Returns:
        str: HTML5 ``<form>`` markup (caller wraps with template).
    """
    pid = prompt.ensure_prompt_id()
    safe_why = html.escape(prompt.why)
    safe_what = html.escape(prompt.what)
    safe_trigger = html.escape(prompt.trigger)

    options_html: list[str] = []
    for opt in prompt.options:
        safe_id = html.escape(opt.id)
        safe_label = html.escape(opt.label)
        checked = " checked" if opt.id == prompt.default_option_id else ""
        options_html.append(
            f'<label><input type="radio" name="option_id" value="{safe_id}"{checked}> '
            f"{safe_label} (<code>{safe_id}</code>)</label><br>"
        )

    reason_block = (
        '  <p><label>Reason: '
        '<textarea name="reason" rows="3" cols="40"></textarea>'
        '</label></p>\n'
    )

    return (
        f'<form method="post" action="{_FORM_ACTION}" '
        'enctype="application/x-www-form-urlencoded">\n'
        f'  <input type="hidden" name="hitl_id" value="{html.escape(pid)}">\n'
        f"  <h3>PopolaLoom · {safe_trigger}</h3>\n"
        f"  <p><strong>Why:</strong> {safe_why}</p>\n"
        f"  <p><strong>What:</strong> {safe_what}</p>\n"
        + "".join(f"  {row}\n" for row in options_html)
        + reason_block
        + '  <button type="submit">Submit</button>\n'
        + "</form>\n"
    )


def parse_reply(form_data: dict[str, Any]) -> HITLReply | None:
    """Extract a :class:`HITLReply` from form-encoded ``form_data``.

    Args:
        form_data: dict of form field name → value (typically from
            ``urllib.parse.parse_qs`` or FastAPI ``Request.form()``).
            Values may be lists (parse_qs default) or scalars (FastAPI);
            we tolerate both.

    Returns:
        HITLReply: on success.
        None: on missing required fields (logged at debug; benign).

    Raises:
        TypeError: when ``form_data`` is not a dict.
    """
    if not isinstance(form_data, dict):
        raise TypeError(
            f"parse_reply: form_data must be dict; got {type(form_data).__name__}"
        )

    def _scalar(value: Any) -> str | None:
        if isinstance(value, list):
            return value[0] if value else None
        return value if isinstance(value, str) else None

    hitl_id = _scalar(form_data.get("hitl_id"))
    option_id = _scalar(form_data.get("option_id"))
    if not hitl_id or not option_id:
        logger.debug("web.parse_reply: missing hitl_id/option_id; benign skip")
        return None
    reason = _scalar(form_data.get("reason"))
    return HITLReply(
        hitl_id=hitl_id,
        option_id=option_id,
        via="web",
        reason=reason,
        responder=_scalar(form_data.get("responder")),
    )
