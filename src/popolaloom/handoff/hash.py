"""Slug-hash addressing for handoff envelopes (v0.7.1, design Q2 = B4).

Per the v0.8.0 plan (user decision 2026-05-06):
- Q2 = B4 ``slug-hash`` addressing scheme.
- ``handoff_id = "<target_cli_slug>-<prompt_slug>-<8hex>"``,
  e.g. ``"cursor-fix-the-bug-in-foo-py-3a7f9c1d"``.
- The 8-hex tail is SHA-256 (first 4 bytes) of a canonical-JSON dump of the
  *content-determining* dispatch tuple (target_cli, prompt, parent_task_id,
  adapter_extra, constraints).  This gives:

  * **Determinism** — same payload → same id (idempotent re-issue, free
    deduplication on the file system layer).
  * **Sensitivity** — flipping a single char in ``prompt`` flips the hash.
  * **Key-order independence** — ``adapter_extra={"a":1,"b":2}`` and
    ``{"b":2,"a":1}`` collapse to the same canonical bytes.

Workspace rule "No Silent Failures": ``slugify_prompt`` deterministically
falls back to ``"task"`` for empty/whitespace/non-ASCII-only inputs (with
no warning swallowed) and ``content_hash`` uses ``default=str`` so
non-JSON-native types (datetime, Path) hash deterministically rather than
silently raising.

This module is **pure** — no IO, no clock, no env reads — so the layer is
trivially unit-testable and safe to call from validation paths.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Final

_FALLBACK_SLUG: Final[str] = "task"
_NON_SLUG_CHAR = re.compile(r"[^a-z0-9]+")
_TARGET_CLI_MAX_CHARS: Final[int] = 12
_PROMPT_SLUG_MAX_CHARS: Final[int] = 30
_HASH_HEX_LEN: Final[int] = 8


def slugify_prompt(prompt: str, max_chars: int = _PROMPT_SLUG_MAX_CHARS) -> str:
    """Reduce ``prompt`` (or any string) to a URL/path-safe slug.

    Pipeline (deterministic, ASCII-only):

    1. Take the first line (``splitlines()[0]`` if any, else empty).
    2. Lowercase.
    3. Collapse every run of non-``[a-z0-9]`` to a single ``-``.
    4. Strip leading/trailing ``-``.
    5. Truncate to ``max_chars``; re-strip trailing ``-`` if the cut landed
       on a separator.
    6. If the result is empty, fall back to :data:`_FALLBACK_SLUG`
       (``"task"``).

    Args:
        prompt:    Input string (typically the first line of a dispatch
                   prompt).  Multi-line input takes only the first line.
        max_chars: Hard cap on output length **after** sanitization
                   (default 30).  Must be >= 1.

    Returns:
        A non-empty ``[a-z0-9-]+``-shaped slug, length 1..``max_chars``.

    Raises:
        ValueError: ``max_chars`` < 1.
    """
    if max_chars < 1:
        raise ValueError(f"slugify_prompt: max_chars must be >= 1, got {max_chars!r}")

    first_line = prompt.splitlines()[0] if prompt.splitlines() else ""
    lowered = first_line.lower()
    replaced = _NON_SLUG_CHAR.sub("-", lowered)
    stripped = replaced.strip("-")
    if len(stripped) > max_chars:
        stripped = stripped[:max_chars].rstrip("-")
    if not stripped:
        return _FALLBACK_SLUG
    return stripped


def content_hash(payload: dict[str, Any]) -> str:
    """Return the first 8 lowercase hex chars of SHA-256(canonical JSON).

    "Canonical JSON" here means:

    - ``sort_keys=True``         — key-order independence
    - ``separators=(",", ":")``  — compact, no whitespace
    - ``ensure_ascii=False``     — preserve multi-byte chars (Chinese OK)
    - ``default=str``            — best-effort coercion for types like
                                   :class:`datetime.datetime` and
                                   :class:`pathlib.Path` so callers don't
                                   have to pre-massage them

    8 hex chars = 32 bits = ~4 billion buckets; for the dispatch-id use
    case (≤ thousands of in-flight handoffs per host) the collision
    probability is negligible (birthday bound ~64k for 1% collision).
    """
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest[:_HASH_HEX_LEN]


def generate_handoff_id(
    target_cli: str,
    prompt: str,
    *,
    parent_task_id: str | None = None,
    adapter_extra: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
) -> str:
    """Build a handoff_id of the form ``<cli>-<slug>-<8hex>`` (Q2 = B4).

    The hash tail is the SHA-256 prefix of a canonical-JSON dump of the
    *content-determining* tuple — see :func:`content_hash`.  This makes
    the id idempotent (same call → same id), so the file-system writer
    in v0.7.1 next slice can dedupe by name alone.

    Args:
        target_cli:     Adapter name (``cursor`` / ``claude`` / ...).
                        Slugified with a tighter ``max_chars=12`` cap so
                        novel adapter names don't blow up the prefix.
        prompt:         Main dispatch prompt body.  Only the first line
                        is reflected in the slug; the full text feeds
                        the hash.
        parent_task_id: Optional relay parent (carried into the hash so
                        relay siblings get distinct ids).
        adapter_extra:  ``--cli-flag`` passthrough payload (``None`` and
                        ``{}`` hash identically — by design).
        constraints:    Execution constraints (timeout, max_tokens,
                        ``allowed_paths``); same ``None`` ↔ ``{}``
                        equivalence.

    Returns:
        e.g. ``"cursor-fix-the-bug-in-foo-py-3a7f9c1d"``.
    """
    cli_slug = slugify_prompt(target_cli, max_chars=_TARGET_CLI_MAX_CHARS)
    prompt_slug = slugify_prompt(prompt, max_chars=_PROMPT_SLUG_MAX_CHARS)
    payload: dict[str, Any] = {
        "target_cli": target_cli,
        "prompt": prompt,
        "parent_task_id": parent_task_id,
        "adapter_extra": adapter_extra or {},
        "constraints": constraints or {},
    }
    tail = content_hash(payload)
    return f"{cli_slug}-{prompt_slug}-{tail}"
