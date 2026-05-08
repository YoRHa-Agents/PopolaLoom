"""Secret pre-flight scanner for ``popola relay`` payloads (v0.8.8 T2.3.3).

Implements **mitigation M3** of ``relay-auto-safety.md`` §5 — every
``popola relay`` invocation runs the resolved relay envelope through a
secret scanner BEFORE the allowlist gate (per §3.3 step 3 of the
safety spec). A hit aborts the dispatch with exit code 1 and writes an
``outcome="rejected_secret_detected"`` audit row carrying only a
``…<last4>`` redacted preview — the full token NEVER lands in stderr,
audit log, or event log.

Design contract (per task brief AC (c)–(e) + spec §5):

1. **Two-tier engine** — primary :mod:`detect_secrets` (Yelp, v1.5.0+)
   when the ``relay-secrets`` optional-dependency extra is installed,
   falling back to a built-in regex + Shannon-entropy catalogue (S1..S6
   in this module) when the optional dep is unavailable. The fallback
   is the **floor** — coverage never drops below S1..S6 even on a
   minimal install.
2. **No silent ImportError** — when the optional dep is unavailable
   the scanner emits an explicit ``WARNING`` log per the spec §5.1
   wording ("WARNING: detect-secrets not installed; using built-in
   regex catalogue only…"). Per the workspace No-Silent-Failures rule,
   the fallback path is the load-bearing route, NOT a try/except that
   silently swallows the import error.
3. **Lazy import** — :mod:`detect_secrets` is imported INSIDE
   :func:`_scan_with_detect_secrets` rather than at module top-level
   so (a) tests can patch it out via ``sys.modules[...] = None`` to
   exercise the fallback path, and (b) the module is importable even
   on installs that don't pull the optional extra.
4. **Scan target surface** — only the §5.3 whitelisted envelope
   segments are scanned: ``prompt`` (full body), ``summary``,
   ``repos[].url`` / ``ref`` / ``prUrl``, ``constraints`` (string-
   valued or recursive dict), and ``adapter_extra`` (recursive leaf
   strings). New envelope keys default DENY (silently scanning a new
   field would let "scanner chases ghosts" reputation undermine the
   gate; T1.1.3 spec §5.3).
5. **Allow-shape escape hatch** — :func:`scan_envelope` returns
   findings AFTER applying the ``allow_shapes`` filter; callers wanting
   the unfiltered surface can re-derive it. Per-shape granularity
   matches ``--allow-secret-shape <name>`` on the CLI; per-finding
   allow-listing is a v0.8.9 backlog item (``BL-v0.8.9-2``).
6. **Redacted preview** — :class:`Finding`'s ``redacted_preview``
   field is constant-shape ``…<last4>`` (the leading char is U+2026
   ``HORIZONTAL ELLIPSIS``); the full token NEVER appears anywhere in
   the :class:`Finding` object so a downstream auditor cannot recover
   the secret from the log even with full :class:`Finding` access.

Token shapes catalogued (S1..S6 of ``relay-auto-safety.md`` §5.2):

| Shape ID | Name                  | Detector                                                |
| -------- | --------------------- | ------------------------------------------------------- |
| S1       | ``aws_access_key_id`` | ``(AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}``                   |
| S2       | ``github_pat``        | ``ghp_/ghs_/gho_/ghu_/ghr_/github_pat_…``               |
| S3       | ``stripe_key``        | ``sk_(live|test)_… / rk_(live|test)_…``                 |
| S4       | ``jwt``               | ``eyJ…\\.eyJ…\\.…`` (3-segment, sig ≥ 20 bytes)         |
| S5       | ``slack_token``       | ``xox[baprs]-…``                                        |
| S6       | ``generic_high_entropy`` | ``[A-Za-z0-9_-]{40,128}`` filtered by Shannon ≥ 4.0  |

Future (v0.8.9 ``BL-v0.8.9-1``): Cursor API key + Lark webhook secret
custom plugins once their canonical regex ranges are publicly published.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """A single secret-scan hit.

    Attributes:
        shape: Catalog identifier of the matched shape (``aws_access_key_id``,
            ``github_pat``, ``stripe_key``, ``jwt``, ``slack_token``,
            ``generic_high_entropy`` for the built-in catalogue;
            additional shapes when the primary :mod:`detect_secrets`
            engine is used). The same ID is what callers pass to
            ``--allow-secret-shape`` to whitelist the shape (per
            ``relay-auto-safety.md`` §5.4).
        location: Path to where the secret was found inside the relay
            envelope, e.g. ``"prompt"``, ``"repos[0].url"``,
            ``"adapter_extra.foo.bar"``. Useful for forensic
            reconstruction without storing the secret itself.
        redacted_preview: ``…<last4>`` — the literal U+2026 ellipsis
            followed by the **last four** characters of the token. By
            spec §5.4 the redaction is fixed-length so an attacker
            cannot infer the original length from the preview, and the
            full token is NEVER stored on the :class:`Finding` instance.
    """

    shape: str
    location: str
    redacted_preview: str


# ---------------------------------------------------------------------------
# Built-in regex catalogue (fallback path; S1..S6 of safety spec §5.2)
# ---------------------------------------------------------------------------


# S1 — AWS Access Key ID. The look-around guards prevent matching
# substrings of longer all-uppercase identifiers (matches the
# ``detect_secrets.plugins.aws.AWSKeyDetector`` invariants).
_S1_AWS_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Z0-9])(AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}(?![A-Z0-9])"
)

# S2 — GitHub Personal Access Tokens (classic + fine-grained + OAuth +
# user-server + installation + refresh). All share the prefix-discriminated
# shape (``ghp_``, ``ghs_``, ``gho_``, ``ghu_``, ``ghr_``, ``github_pat_``).
_S2_GITHUB_RES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bghs_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bgho_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bghu_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bghr_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b"),
)

# S3 — Stripe API keys (live + test, regular + restricted).
_S3_STRIPE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{24,}\b"
)

# S4 — JSON Web Token. The 20-byte signature minimum reduces false
# positives on prose that happens to contain the ``eyJ`` Base64 prefix
# of an unrelated JSON-encoded value.
_S4_JWT_RE: Final[re.Pattern[str]] = re.compile(
    r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{20,}\b"
)

# S5 — Slack tokens (bot, user, app, refresh, …).
_S5_SLACK_RE: Final[re.Pattern[str]] = re.compile(
    r"\bxox[baprs]-[A-Za-z0-9-]{10,48}\b"
)

# S6 — Generic high-entropy candidate. The regex matches any contiguous
# 40..128-char run of base64-url-friendly characters; the Shannon-entropy
# filter (≥ 4.0 bits/char per the task brief hint) is what makes the
# match cross the bar from "URL slug" to "likely secret".
_S6_HIGH_ENTROPY_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{40,128}")
_S6_ENTROPY_THRESHOLD_BITS_PER_CHAR: Final[float] = 4.0


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _shannon_entropy(text: str) -> float:
    """Return Shannon entropy (bits/char) of ``text`` over its own alphabet.

    Pure helper — no IO, no external deps. The classic
    ``-Σ p_i log2(p_i)`` formula. An empty string yields 0.0 (the
    information-theoretic floor); a single repeated character also
    yields 0.0 (no ambiguity).
    """
    if not text:
        return 0.0
    freq: dict[str, int] = {}
    for ch in text:
        freq[ch] = freq.get(ch, 0) + 1
    n = float(len(text))
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


def _redact(token: str) -> str:
    """Return the constant-shape redacted preview ``…<last4>``.

    Per ``relay-auto-safety.md`` §5.4 the redaction is fixed-length so
    an attacker reading the audit log cannot infer the secret's
    original length from the preview. Tokens shorter than 4 characters
    fall back to the verbatim string preceded by the ellipsis (this
    branch is theoretically reachable for malformed inputs but the
    catalogue regexes all require ≥ 4 characters in practice).
    """
    last4 = token[-4:] if len(token) >= 4 else token
    return f"\u2026{last4}"


def _walk_envelope(envelope: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """Yield ``(location, leaf_string)`` for each whitelisted segment.

    Per ``relay-auto-safety.md`` §5.3, the surface is exactly:

    - ``prompt`` (full body)
    - ``summary``
    - each entry in ``repos[]``, fields ``url`` / ``ref`` / ``prUrl``
    - ``constraints`` (string-valued OR recursive dict)
    - ``adapter_extra`` (recursive leaf strings)

    Anything else (handoff_id, timestamps, schema_version, …) is OUT
    OF scope — the spec §5.3 says "any new envelope key added by
    T1.1.3 must be added to this union with a written justification".
    """
    prompt = envelope.get("prompt")
    if isinstance(prompt, str):
        yield "prompt", prompt

    summary = envelope.get("summary")
    if isinstance(summary, str):
        yield "summary", summary

    repos = envelope.get("repos")
    if isinstance(repos, list):
        for idx, repo in enumerate(repos):
            if not isinstance(repo, dict):
                continue
            for sub_key in ("url", "ref", "prUrl"):
                sub_val = repo.get(sub_key)
                if isinstance(sub_val, str):
                    yield f"repos[{idx}].{sub_key}", sub_val

    constraints = envelope.get("constraints")
    if isinstance(constraints, str):
        yield "constraints", constraints
    elif isinstance(constraints, dict):
        yield from _recursive_strings(constraints, prefix="constraints")

    adapter_extra = envelope.get("adapter_extra")
    if isinstance(adapter_extra, dict):
        yield from _recursive_strings(adapter_extra, prefix="adapter_extra")


def _recursive_strings(
    obj: Any, *, prefix: str
) -> Iterator[tuple[str, str]]:
    """Walk an arbitrary dict/list tree, yielding leaf string values.

    Non-string leaves (int, bool, None, float) are skipped — the
    catalogue is shaped against text-encoded secrets, and a numeric
    field is unlikely to carry a credential.
    """
    if isinstance(obj, str):
        yield prefix, obj
    elif isinstance(obj, dict):
        for key, val in obj.items():
            yield from _recursive_strings(val, prefix=f"{prefix}.{key}")
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            yield from _recursive_strings(item, prefix=f"{prefix}[{idx}]")


# ---------------------------------------------------------------------------
# Fallback regex scanner (S1..S6) — always available
# ---------------------------------------------------------------------------


def _fallback_scan_text(text: str, *, location: str) -> list[Finding]:
    """Run the built-in S1..S6 catalogue against a single text block.

    Returns a list rather than yielding so the caller can take a
    snapshot for assertions; per-shape duplicates within the same
    text are reported (the audit row caller decides how to render
    them).
    """
    findings: list[Finding] = []

    for match in _S1_AWS_RE.finditer(text):
        findings.append(
            Finding(
                shape="aws_access_key_id",
                location=location,
                redacted_preview=_redact(match.group(0)),
            )
        )

    for github_re in _S2_GITHUB_RES:
        for match in github_re.finditer(text):
            findings.append(
                Finding(
                    shape="github_pat",
                    location=location,
                    redacted_preview=_redact(match.group(0)),
                )
            )

    for match in _S3_STRIPE_RE.finditer(text):
        findings.append(
            Finding(
                shape="stripe_key",
                location=location,
                redacted_preview=_redact(match.group(0)),
            )
        )

    for match in _S4_JWT_RE.finditer(text):
        findings.append(
            Finding(
                shape="jwt",
                location=location,
                redacted_preview=_redact(match.group(0)),
            )
        )

    for match in _S5_SLACK_RE.finditer(text):
        findings.append(
            Finding(
                shape="slack_token",
                location=location,
                redacted_preview=_redact(match.group(0)),
            )
        )

    # S6 high-entropy: gate the regex match on Shannon entropy so URL
    # slugs ("github.com/neolix-ai/popola/pull/12345-some-thing-here")
    # don't drown the signal.
    for match in _S6_HIGH_ENTROPY_RE.finditer(text):
        candidate = match.group(0)
        if _shannon_entropy(candidate) >= _S6_ENTROPY_THRESHOLD_BITS_PER_CHAR:
            findings.append(
                Finding(
                    shape="generic_high_entropy",
                    location=location,
                    redacted_preview=_redact(candidate),
                )
            )

    return findings


def _fallback_scan(envelope: dict[str, Any]) -> list[Finding]:
    """Run the built-in S1..S6 catalogue across the §5.3 envelope surface."""
    findings: list[Finding] = []
    for location, text in _walk_envelope(envelope):
        findings.extend(_fallback_scan_text(text, location=location))
    return findings


# ---------------------------------------------------------------------------
# Primary detect-secrets scanner (lazy import)
# ---------------------------------------------------------------------------


# Mapping of detect-secrets plugin type names to our catalogue shape
# identifiers. Anything not in the map falls through to its lower-cased
# detect-secrets type name so v0.8.9 custom plugins surface as new
# distinct shapes (callers can then pick them up via
# ``--allow-secret-shape <new-shape>``).
_DETECT_SECRETS_TYPE_TO_SHAPE: Final[dict[str, str]] = {
    "AWS Access Key": "aws_access_key_id",
    "GitHub Token": "github_pat",
    "Stripe Access Key": "stripe_key",
    "JSON Web Token": "jwt",
    "Slack Token": "slack_token",
    "Base64 High Entropy String": "generic_high_entropy",
    "Hex High Entropy String": "generic_high_entropy",
}


def _map_detect_secrets_type(secret_type: str) -> str:
    """Map a :mod:`detect_secrets` plugin type to a catalogue shape ID."""
    if secret_type in _DETECT_SECRETS_TYPE_TO_SHAPE:
        return _DETECT_SECRETS_TYPE_TO_SHAPE[secret_type]
    return secret_type.lower().replace(" ", "_")


# Minimum length per spec catalogue shape. The :mod:`detect_secrets`
# library's ``scan_line`` runs in ``enable_eager_search=True`` mode which
# bypasses the per-plugin entropy filter for short matches; without this
# table a high-entropy plugin will report English words like "Use" /
# "for" as findings. The thresholds align with the catalogue regexes in
# §5.2 (e.g. AWS keys are exactly 20 chars, GitHub PATs are 40 chars,
# the generic high-entropy floor is 40 chars per the brief hint).
_SHAPE_MIN_LENGTH: Final[dict[str, int]] = {
    "aws_access_key_id": 16,
    "github_pat": 16,
    "stripe_key": 24,
    "jwt": 50,
    "slack_token": 15,
    "generic_high_entropy": 40,
}
_DEFAULT_MIN_LENGTH: Final[int] = 16


def _passes_min_length(shape: str, secret_value: str) -> bool:
    """Reject obviously-short detect-secrets findings (eager-search false-positive guard).

    The library's plugin set has a known ``enable_eager_search``
    quirk where high-entropy plugins return short English words; the
    minimum-length floor here mirrors the spec catalogue thresholds
    so ``scan_envelope`` cannot return a ``Finding`` with
    ``redacted_preview = "…<3 chars>"`` on natural-language input.
    """
    threshold = _SHAPE_MIN_LENGTH.get(shape, _DEFAULT_MIN_LENGTH)
    return len(secret_value) >= threshold


def _scan_with_detect_secrets(envelope: dict[str, Any]) -> list[Finding]:
    """Scan via :mod:`detect_secrets`; raise :class:`ImportError` if absent.

    Strategy: run BOTH the :mod:`detect_secrets` ``scan_line`` engine
    AND the spec-aligned fallback regex catalogue, then dedupe by
    ``(shape, location, redacted_preview)`` so a token caught by both
    engines surfaces exactly once. The two engines have different
    strengths:

    - :mod:`detect_secrets` covers a broader set of shapes (e.g. NPM
      tokens, Mailgun, custom plugins added by v0.8.9 backlog) and
      reports a ``secret_type`` we can map onto the catalogue.
    - The fallback regex always extracts the **full** matched token
      (some :mod:`detect_secrets` plugins return only the matched
      ``re.Match.group(1)`` prefix — e.g. ``GitHubTokenDetector``
      returns ``"ghp"`` rather than the full PAT — which is unhelpful
      for the ``…<last4>`` redaction since the last 4 chars of "ghp"
      give the operator no identification handle).

    Combining the two and deduping yields the cleanest findings list:
    the fallback catalogue's full-token extraction wins for the 6
    spec shapes, and :mod:`detect_secrets`-only shapes still surface
    as bonus coverage.

    Returns:
        list[Finding]: zero or more findings; same shape as the
        fallback path so callers don't need to branch on engine choice.

    Raises:
        ImportError: when :mod:`detect_secrets` is not installed (or
            has been patched out via ``sys.modules``).
    """
    # ruff: noqa: I001 — keep the lazy import block in spec-cited order
    # (core.scan first, then settings) regardless of alphabetical order.
    # The ``import-untyped`` / ``import-not-found`` ignores are duplicated
    # so the module is type-checkable in BOTH "extra installed" and
    # "extra missing" environments without spurious mypy errors.
    from detect_secrets.core.scan import (  # type: ignore[import-not-found,import-untyped,unused-ignore]
        scan_line,
    )
    from detect_secrets.settings import (  # type: ignore[import-not-found,import-untyped,unused-ignore]
        default_settings,
    )

    detect_findings: list[Finding] = []
    with default_settings():
        for location, text in _walk_envelope(envelope):
            for line in text.splitlines() or [text]:
                if not line:
                    continue
                for secret in scan_line(line):
                    secret_value = getattr(secret, "secret_value", None)
                    if not isinstance(secret_value, str) or not secret_value:
                        continue
                    shape = _map_detect_secrets_type(
                        getattr(secret, "type", "unknown")
                    )
                    if not _passes_min_length(shape, secret_value):
                        continue
                    detect_findings.append(
                        Finding(
                            shape=shape,
                            location=location,
                            redacted_preview=_redact(secret_value),
                        )
                    )

    fallback_findings = _fallback_scan(envelope)
    seen: set[tuple[str, str, str]] = {
        (f.shape, f.location, f.redacted_preview) for f in fallback_findings
    }
    merged: list[Finding] = list(fallback_findings)
    for finding in detect_findings:
        key = (finding.shape, finding.location, finding.redacted_preview)
        if key not in seen:
            seen.add(key)
            merged.append(finding)
    return merged


# ---------------------------------------------------------------------------
# Public scan API
# ---------------------------------------------------------------------------


_FALLBACK_WARNING_MESSAGE: Final[str] = (
    "detect-secrets not installed; using built-in regex catalogue only "
    "(v0.8.8 mandates a secret pre-flight, see relay-auto-safety.md §5). "
    "Run `pip install 'popolaloom[relay-secrets]'` "
    "(or `pip install detect-secrets>=1.5.0`) for full coverage."
)


def scan_envelope(
    envelope: dict[str, Any],
    allow_shapes: list[str] | None = None,
) -> list[Finding]:
    """Scan ``envelope`` for embedded secrets and return findings.

    Runs the primary :mod:`detect_secrets` engine when available;
    otherwise falls back to the built-in S1..S6 regex + Shannon-entropy
    catalogue. The fallback path emits an explicit WARNING log
    (No-Silent-Failures) and is exercised in tests via
    ``sys.modules["detect_secrets"] = None``.

    Args:
        envelope: Relay envelope dict (T1.1.3 schema). Only the §5.3
            whitelisted segments (``prompt``, ``summary``, ``repos[]``
            url/ref/prUrl, ``constraints``, ``adapter_extra``) are
            scanned; out-of-scope keys are silently ignored.
        allow_shapes: List of shape IDs to filter out of the result
            (the ``--allow-secret-shape`` escape hatch). ``None``
            (default) and ``[]`` are equivalent — no shapes whitelisted.

    Returns:
        list[Finding]: zero or more findings, **after** the
        ``allow_shapes`` filter has been applied. Findings preserve the
        order the scanner produced them (segment-walk order); callers
        wanting deterministic ordering should sort by
        ``(location, shape)``.
    """
    allowed: set[str] = set(allow_shapes or [])

    try:
        raw_findings = _scan_with_detect_secrets(envelope)
    except ImportError:
        logger.warning(_FALLBACK_WARNING_MESSAGE)
        raw_findings = _fallback_scan(envelope)

    if not allowed:
        return raw_findings
    return [f for f in raw_findings if f.shape not in allowed]
