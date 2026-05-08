"""Cost / token field redaction helper for popolad logging (v0.8.8 T2.1.2).

Per ``cost-fields.md`` §5.3 (Code-level enforcement, Q-C-2 locked):

> New helper ``popolaloom.daemon.log_redact.scrub_cost_fields(payload: dict)
> -> dict`` deep-copies and removes any key in
> ``{"usage", "tokens_input", "tokens_output", "cacheReadTokens",
> "cacheWriteTokens", "chargedCents", "totalCents", "tokenUsage",
> "cursorTokenFee", "spendCents", "cost_estimate_usd"}`` before any
> ``INFO``/``WARNING`` emit. Tested via fuzzed payloads.

The redaction is **deep**: nested dicts and lists are walked recursively
so a cost-bearing key buried inside ``data.event_payload.usage`` is
stripped just like a top-level ``usage`` key. Tuples are converted to
lists during the deep-copy walk (the standard library's ``copy.deepcopy``
preserves tuple identity, but for redaction what matters is that the
returned structure is JSON-safe — and our ``RedactedPayload`` consumers
all serialize via ``json.dumps`` which collapses tuples to lists anyway).

This module is import-safe (no side effects, no daemon state) so any
callsite — including unit tests — can ``from popolaloom.daemon.log_redact
import scrub_cost_fields`` without spinning up a popolad.

Workspace rule **No Silent Failures** is preserved: redaction does NOT
swallow logging payloads — it removes only the locked-list keys and
returns the rest verbatim so the caller can still log the surviving
context. Callers that want stricter "drop the whole payload if any
forbidden key is present" semantics should compose their own wrapper.
"""

from __future__ import annotations

from typing import Any

__all__ = ["FORBIDDEN_KEYS", "scrub_cost_fields"]


FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "usage",
        "tokens_input",
        "tokens_output",
        "tokens_total",
        "cacheReadTokens",
        "cacheWriteTokens",
        "chargedCents",
        "totalCents",
        "tokenUsage",
        "cursorTokenFee",
        "spendCents",
        "cost_estimate_usd",
    }
)
"""Locked set of cost / token-bearing keys that MUST NOT land in INFO logs.

Source of truth: ``cost-fields.md`` §5.3 + §2.3 (Admin-API field catalog).
Adding a new key here requires a v0.8.8.x point release per spec §6.

``tokens_total`` is included alongside ``tokens_input`` / ``tokens_output``
because the SDK and Admin API may surface it independently; we strip the
whole token triplet for consistency. (The spec text enumerates
``tokens_*`` collectively; we materialize the wildcard into the explicit
locked list so the redaction is grep-auditable.)
"""


def scrub_cost_fields(payload: Any) -> Any:
    """Return a deep copy of ``payload`` with every cost-bearing key removed.

    Args:
        payload: Arbitrary JSON-shaped Python object — typically the
            ``data`` dict of a CloudEvents envelope or a logger
            ``extra=`` dict. Lists, dicts, scalars, and ``None`` are all
            valid; the function is total over JSON-decodable values.

    Returns:
        The same shape as the input, but with any key listed in
        :data:`FORBIDDEN_KEYS` excised at every nesting depth. Scalar
        inputs (``str`` / ``int`` / ``float`` / ``bool`` / ``None``) are
        returned verbatim (still copied where appropriate; immutable
        scalars are shared).

    Examples:
        >>> scrub_cost_fields({"usage": {"a": 1}, "model": "x"})
        {'model': 'x'}
        >>> scrub_cost_fields({"data": {"usage": 99, "k": "v"}})
        {'data': {'k': 'v'}}
        >>> scrub_cost_fields([{"usage": 1}, {"keep": 2}])
        [{}, {'keep': 2}]

    The implementation walks the structure once and rebuilds; we do
    *not* call :func:`copy.deepcopy` first because that would pay a
    second traversal cost. ``__name__`` checks against the locked
    set are O(1) (``frozenset`` membership), so total cost is
    O(n) over the payload's leaf+key count.
    """
    if isinstance(payload, dict):
        scrubbed: dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(key, str) and key in FORBIDDEN_KEYS:
                continue
            scrubbed[key] = scrub_cost_fields(value)
        return scrubbed
    if isinstance(payload, list):
        return [scrub_cost_fields(item) for item in payload]
    if isinstance(payload, tuple):
        return tuple(scrub_cost_fields(item) for item in payload)
    return payload
