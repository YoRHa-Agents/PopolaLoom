"""Hands-off envelope schema (v0.7.1, design Q1 = A4 Markdown front-matter).

Per the v0.8.0 plan (user decision 2026-05-06):
- Q1 = A4 — wire format is Markdown with YAML front-matter.  Metadata
  goes in the front-matter, the prompt body lives **outside** the fence
  so it round-trips byte-faithfully (no YAML escaping of newlines, no
  block scalar surprises).
- Q5 = E3 — this slice intentionally exposes only the schema +
  serializer.  ``writer`` (atomic file landing) and the
  ``dispatch_with_envelope`` unification ride in subsequent slices.

File layout::

    ---
    schema_version: '1'
    handoff_id: cursor-fix-the-bug-in-foo-py-3a7f9c1d
    created_at: '2026-05-06T22:00:00+00:00'
    ...other front-matter fields, no `prompt`...
    ---
    <prompt body, byte-faithful, exactly one trailing newline>

Workspace rule "No Silent Failures": :meth:`HandoffEnvelope.from_markdown`
wraps every parse / validation failure into a :class:`ValueError` with a
diagnostic message; nothing is swallowed.  Pydantic's strict
``extra="forbid"`` rejects unknown fields (drift detection).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

HANDOFF_SCHEMA_VERSION: Final[str] = "1"
"""Current envelope schema version; bumped on breaking changes."""

_FENCE: Final[str] = "---"
_FENCE_LINE: Final[str] = f"{_FENCE}\n"


class HandoffEnvelope(BaseModel):
    """File-landed dispatch payload (v0.7.1 schema, version "1").

    Field order is the **canonical** front-matter order — Pydantic v2
    preserves declaration order in :meth:`model_dump`, and we feed that
    straight into ``yaml.safe_dump(..., sort_keys=False)`` so the
    on-disk layout is stable across releases.

    Workspace rule "No Silent Failures": ``extra="forbid"`` — unknown
    keys raise :class:`pydantic.ValidationError` rather than silently
    being dropped, so adapter-specific drift can't sneak past the
    schema gate.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    handoff_id: str = Field(..., min_length=1)
    created_at: datetime = Field(...)
    source_cli: str | None = Field(default=None)
    target_cli: str = Field(..., min_length=1)
    parent_task_id: str | None = Field(default=None)
    prompt: str = Field(..., min_length=1)
    cwd: str | None = Field(default=None)
    adapter_extra: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = Field(default=None)
    tags: list[str] = Field(default_factory=list)

    def to_markdown(self) -> str:
        """Serialize to the canonical Markdown front-matter envelope.

        Invariants:

        - Output starts with ``"---\\n"``; the second ``"\\n---\\n"``
          terminates the front-matter.
        - ``created_at`` is emitted as an ISO-8601 string (so YAML can't
          turn it into a typed timestamp, which would break round-trip
          byte equality across pyyaml versions).
        - ``prompt`` is **not** in the front-matter; it lives in the
          body, byte-faithful, with exactly one trailing ``"\\n"``
          regardless of how many trailing newlines the source had
          (canonicalization is part of the file format contract).
        - ``None`` values are written as ``null`` (preserved, not
          omitted) so round-trip equality holds for explicitly-null
          fields.
        """
        data = self.model_dump()
        data["created_at"] = self.created_at.isoformat()
        prompt_body: str = data.pop("prompt")

        front_matter = yaml.safe_dump(
            data,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )
        body = prompt_body.rstrip("\n") + "\n"
        return f"{_FENCE_LINE}{front_matter}{_FENCE_LINE}{body}"

    @classmethod
    def from_markdown(cls, text: str) -> HandoffEnvelope:
        """Inverse of :meth:`to_markdown`.

        Recognized format:

        - Must start with ``"---\\n"`` (opening fence).
        - The closing fence is the **first** ``"\\n---\\n"`` after the
          opening; everything before is YAML, everything after is the
          prompt body.  This means a body containing literal ``"---"``
          is fine — only the *first* fence boundary matters.
        - Trailing newlines on the body are stripped (the writer
          canonicalizes to exactly one newline; the parser tolerates
          0 or more for robustness, then re-asserts non-empty).

        Raises:
            ValueError: opening fence missing, closing fence missing,
                YAML parse error, YAML root not a dict, body trims to
                empty, or schema validation fails (Pydantic
                :class:`ValidationError` is wrapped per workspace rule
                "No Silent Failures").
        """
        if not text.startswith(_FENCE_LINE):
            raise ValueError(
                "HandoffEnvelope.from_markdown: missing opening fence "
                f"(expected {_FENCE_LINE!r} at offset 0)"
            )

        rest = text[len(_FENCE_LINE):]
        sep = f"\n{_FENCE_LINE}"
        sep_idx = rest.find(sep)
        if sep_idx < 0:
            raise ValueError(
                "HandoffEnvelope.from_markdown: missing closing fence "
                f"(expected {sep!r} after front-matter)"
            )

        front_matter_text = rest[:sep_idx]
        body = rest[sep_idx + len(sep):]

        try:
            data = yaml.safe_load(front_matter_text)
        except yaml.YAMLError as exc:
            raise ValueError(
                f"HandoffEnvelope.from_markdown: YAML parse error in front-matter: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise ValueError(
                "HandoffEnvelope.from_markdown: front-matter must be a YAML mapping, "
                f"got {type(data).__name__}"
            )

        prompt_body = body.rstrip("\n")
        if not prompt_body:
            raise ValueError(
                "HandoffEnvelope.from_markdown: prompt body is empty after stripping "
                "trailing newlines (prompt is required, min_length=1)"
            )
        data["prompt"] = prompt_body

        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise ValueError(
                f"HandoffEnvelope.from_markdown: schema validation failed: {exc}"
            ) from exc
