"""Unit tests for :mod:`popolaloom.relay.secrets` (v0.8.8 T2.3.3).

Covers the AC items from the task brief §"Acceptance criteria":

- (c) :func:`scan_envelope` runs the primary detect-secrets engine when
  available, falls back to S1..S6 regex catalogue with a WARN log when
  not (No-Silent-Failures rule).
- (d) ``allow_shapes`` whitelists ALL findings of those shapes.
- (e) Findings are :class:`Finding` tuples with ``(shape, location,
  redacted_preview)``; the preview is constant-shape ``…<last4>`` and
  the full token NEVER appears on the dataclass.
- (f) Test count: 6 parametrized shape happy-path + allow_shapes
  whitelist + fallback-WARN + false-positive (≥ 6, in addition to the
  audit tests in ``test_audit_writer.py``).

The 6 token shape samples are chosen to be **synthetic** (generated
character runs that match the regex shapes) — they are NOT real
credentials. Real tokens MUST never land in the test fixture per the
spec §5.4 redaction policy. The "AKIA" prefix that AWS uses is
pre-encoded in the sample, but the trailing 16-char body is just
``"X" * 16`` style filler.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import pytest

from popolaloom.relay.secrets import (
    Finding,
    _shannon_entropy,
    scan_envelope,
)

# ---------------------------------------------------------------------------
# Token shape samples (S1..S6) — synthetic, NOT real credentials
# ---------------------------------------------------------------------------
#
# Naming: ``<SHAPE_ID>__SAMPLE`` so the parametrised test ID surfaces the
# shape under inspection in pytest output.
#
# Sources (per ``relay-auto-safety.md`` §5.2):
#   S1 — AWS:        ``(AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}``
#   S2 — GitHub PAT: ``ghp_[A-Za-z0-9]{36}`` (and 5 sibling shapes)
#   S3 — Stripe:     ``sk_live_[A-Za-z0-9]{24,}`` (and ``rk_…``)
#   S4 — JWT:        ``eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{20,}``
#   S5 — Slack:      ``xox[baprs]-[A-Za-z0-9-]{10,48}``
#   S6 — Generic:    ``[A-Za-z0-9_-]{40,128}`` filtered by Shannon ≥ 4.0

S1_AWS_SAMPLE = "AKIAIOSFODNN7EXAMPLE"
S2_GITHUB_PAT_SAMPLE = "ghp_" + "A" * 36
S3_STRIPE_SAMPLE = "sk_live_" + "B" * 24
S4_JWT_SAMPLE = (
    "eyJhbGciOiJIUzI1NiJ9."
    "eyJzdWIiOiIxMjMifQ."
    + "C" * 30
)
S5_SLACK_SAMPLE = "xoxb-" + "1" * 12 + "abc"

# A 44-char synthetic high-entropy token. Shannon entropy verified to
# exceed 4.0 bits/char (~5.4 with all-unique chars). Constructed by
# concatenating small case-mixed alphanumeric runs so the regex matches
# AND the entropy filter passes.
S6_HIGH_ENTROPY_SAMPLE = "AbCdEf01GhIjKl23MnOpQr45StUvWx67YzAb89EfGh01"

# Map shape → (id, sample) for parametrize. Repeat in tests at row level.
_SHAPE_SAMPLES: list[tuple[str, str]] = [
    ("aws_access_key_id", S1_AWS_SAMPLE),
    ("github_pat", S2_GITHUB_PAT_SAMPLE),
    ("stripe_key", S3_STRIPE_SAMPLE),
    ("jwt", S4_JWT_SAMPLE),
    ("slack_token", S5_SLACK_SAMPLE),
    ("generic_high_entropy", S6_HIGH_ENTROPY_SAMPLE),
]


def _envelope_with(prompt: str, **extra: Any) -> dict[str, Any]:
    """Build a relay envelope with ``prompt`` set + arbitrary other keys."""
    base: dict[str, Any] = {"prompt": prompt}
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Fallback engine pin (sys.modules patching)
# ---------------------------------------------------------------------------
#
# All shape happy-path tests exercise the fallback regex catalogue rather
# than the primary detect-secrets engine. Two reasons:
#
# 1. Determinism: detect-secrets's plugin set + thresholds drift across
#    library versions; pinning to the in-tree fallback guarantees the
#    test is hermetic regardless of whether the optional extra is
#    installed.
# 2. Coverage: the fallback path is the spec-mandated **floor** — the
#    primary path adds shapes on top, so a shape test passing on the
#    fallback also passes on the primary engine (in the worst case the
#    primary engine reports the SAME shape PLUS extras).
#
# A separate test (``test_fallback_path_emits_warning``) confirms the
# WARN log fires when the primary engine is unavailable.


@pytest.fixture(autouse=True)
def _force_fallback_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force :func:`scan_envelope` to use the regex fallback in tests.

    The autouse fixture sets ``sys.modules["detect_secrets"] = None``
    so the lazy ``from detect_secrets import …`` inside
    :func:`_scan_with_detect_secrets` raises ``ImportError`` immediately,
    triggering the WARN-then-fallback branch.

    Tests that want to specifically assert WARN log emission opt back in
    by clearing ``caplog.records`` before the call (see
    :func:`test_fallback_path_emits_warning`).
    """
    monkeypatch.setitem(sys.modules, "detect_secrets", None)
    monkeypatch.setitem(sys.modules, "detect_secrets.core", None)
    monkeypatch.setitem(sys.modules, "detect_secrets.core.scan", None)
    monkeypatch.setitem(sys.modules, "detect_secrets.settings", None)


# ---------------------------------------------------------------------------
# AC (c) — 6 parametrized shape happy-path tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("shape_id", "sample"), _SHAPE_SAMPLES)
def test_scan_envelope_detects_each_shape(
    shape_id: str, sample: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Each S1..S6 shape yields ≥1 finding when present in ``prompt``."""
    envelope = _envelope_with(prompt=f"please use {sample} for the api call")
    with caplog.at_level(logging.WARNING, logger="popolaloom.relay.secrets"):
        findings = scan_envelope(envelope)
    matching = [f for f in findings if f.shape == shape_id]
    assert matching, (
        f"shape {shape_id!r} not detected; got shapes "
        f"{sorted({f.shape for f in findings})}"
    )


@pytest.mark.parametrize(("shape_id", "sample"), _SHAPE_SAMPLES)
def test_finding_redacted_preview_hides_full_token(
    shape_id: str, sample: str
) -> None:
    """The :class:`Finding`'s preview is ``…<last4>`` — never the full token."""
    envelope = _envelope_with(prompt=sample)
    findings = scan_envelope(envelope)
    matching = [f for f in findings if f.shape == shape_id]
    assert matching
    finding = matching[0]
    last4 = sample[-4:]
    assert finding.redacted_preview == f"\u2026{last4}"
    assert sample not in finding.redacted_preview


# ---------------------------------------------------------------------------
# AC (e) — finding location surfaces the §5.3 segment paths
# ---------------------------------------------------------------------------


def test_findings_locate_in_repos_url() -> None:
    """A token in ``repos[0].url`` shows up as ``location='repos[0].url'``."""
    envelope: dict[str, Any] = {
        "repos": [
            {
                "url": (
                    "https://x-access-token:" + S2_GITHUB_PAT_SAMPLE
                    + "@github.com/foo/bar"
                ),
                "ref": "main",
            },
        ],
    }
    findings = scan_envelope(envelope)
    assert any(
        f.location == "repos[0].url" and f.shape == "github_pat"
        for f in findings
    ), [f.location for f in findings]


def test_findings_locate_in_adapter_extra_recursive() -> None:
    """Recursive walk through ``adapter_extra`` hits nested leaf strings."""
    envelope: dict[str, Any] = {
        "adapter_extra": {
            "outer": {
                "inner": {
                    "secret": S1_AWS_SAMPLE,
                },
            },
            "harmless_int": 42,
        },
    }
    findings = scan_envelope(envelope)
    aws_findings = [f for f in findings if f.shape == "aws_access_key_id"]
    assert len(aws_findings) == 1
    assert aws_findings[0].location == "adapter_extra.outer.inner.secret"


def test_findings_skip_non_string_leaves() -> None:
    """Numeric / bool / None leaves are not scanned (no false-positive class)."""
    envelope: dict[str, Any] = {
        "adapter_extra": {
            "n": 12345678901234567890,
            "b": True,
            "x": None,
        },
    }
    assert scan_envelope(envelope) == []


# ---------------------------------------------------------------------------
# AC (d) — allow_shapes whitelist filters findings
# ---------------------------------------------------------------------------


def test_allow_shapes_whitelists_all_findings_of_a_shape() -> None:
    """``allow_shapes=["jwt"]`` removes ALL JWT findings, leaves others."""
    envelope = _envelope_with(
        prompt=f"jwt={S4_JWT_SAMPLE} and aws={S1_AWS_SAMPLE}"
    )
    findings_unfiltered = scan_envelope(envelope)
    shapes_unfiltered = {f.shape for f in findings_unfiltered}
    assert {"jwt", "aws_access_key_id"} <= shapes_unfiltered

    findings_filtered = scan_envelope(envelope, allow_shapes=["jwt"])
    shapes_filtered = {f.shape for f in findings_filtered}
    assert "jwt" not in shapes_filtered
    assert "aws_access_key_id" in shapes_filtered


def test_allow_shapes_with_no_matching_findings_is_a_noop() -> None:
    """Whitelisting a shape that doesn't appear changes nothing."""
    envelope = _envelope_with(prompt=f"aws={S1_AWS_SAMPLE}")
    base = scan_envelope(envelope)
    filtered = scan_envelope(envelope, allow_shapes=["jwt", "slack_token"])
    assert {f.shape for f in base} == {f.shape for f in filtered}


def test_allow_shapes_can_whitelist_multiple_shapes() -> None:
    """Multiple shapes whitelisted simultaneously are all suppressed."""
    envelope = _envelope_with(
        prompt=(
            f"a={S1_AWS_SAMPLE} g={S2_GITHUB_PAT_SAMPLE} "
            f"s={S3_STRIPE_SAMPLE}"
        ),
    )
    findings = scan_envelope(
        envelope,
        allow_shapes=["aws_access_key_id", "github_pat"],
    )
    shapes = {f.shape for f in findings}
    assert "aws_access_key_id" not in shapes
    assert "github_pat" not in shapes
    assert "stripe_key" in shapes


# ---------------------------------------------------------------------------
# AC (c) — fallback path WARN log (No Silent Failures)
# ---------------------------------------------------------------------------


def test_fallback_path_emits_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When :mod:`detect_secrets` is missing, a WARNING log MUST fire.

    The autouse ``_force_fallback_engine`` fixture has already patched
    :mod:`detect_secrets` to ``None`` in :data:`sys.modules`, so this
    test only needs to call ``scan_envelope`` and assert the log
    fingerprint. Per ``relay-auto-safety.md`` §5.1, the message MUST
    name the missing dep so an operator immediately knows what to
    install.
    """
    caplog.clear()
    envelope = _envelope_with(prompt=f"token={S2_GITHUB_PAT_SAMPLE}")
    with caplog.at_level(logging.WARNING, logger="popolaloom.relay.secrets"):
        findings = scan_envelope(envelope)
    assert findings, "fallback path returned no findings on a clear hit"
    fingerprints = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING
    ]
    assert any(
        "detect-secrets" in msg and "regex catalogue" in msg
        for msg in fingerprints
    ), fingerprints


def test_fallback_path_returns_findings_even_without_dep() -> None:
    """The fallback is the floor: secrets still surface without the extra."""
    envelope = _envelope_with(prompt=S1_AWS_SAMPLE)
    findings = scan_envelope(envelope)
    assert any(
        f.shape == "aws_access_key_id" for f in findings
    ), "fallback failed to detect AWS key"


# ---------------------------------------------------------------------------
# AC (c) — false-positive resistance on natural language
# ---------------------------------------------------------------------------


def test_natural_language_input_yields_no_findings() -> None:
    """A clean prose prompt (no embedded secrets) MUST scan to 0 findings.

    Catches the failure mode where a regex over-matches everyday
    English / repository slugs / pull-request URLs (the anti-pattern
    the §5.2 entropy filter was designed to prevent for S6).
    """
    envelope = _envelope_with(
        prompt=(
            "Please review the latest PR at "
            "https://github.com/neolix-ai/popola-loom/pull/42 "
            "and let me know if the multi-run support looks reasonable. "
            "We changed about 12 files across the daemon, the cloud "
            "adapter, and the CLI; the test suite is green and "
            "coverage is unchanged."
        ),
        summary="Reviewer wants follow-up on the cloud-poller changes.",
        constraints={"timeout_s": 600, "max_tokens": 4096},
    )
    findings = scan_envelope(envelope)
    assert findings == [], [
        (f.shape, f.location, f.redacted_preview) for f in findings
    ]


def test_short_alphanumeric_runs_dont_trigger_high_entropy_shape() -> None:
    """A 30-char run is below the 40-char minimum for S6 → no finding."""
    envelope = _envelope_with(prompt="A" * 30 + " not a token")
    findings = scan_envelope(envelope)
    assert all(f.shape != "generic_high_entropy" for f in findings)


def test_low_entropy_long_run_doesnt_trigger_high_entropy_shape() -> None:
    """A 50-char run of identical chars has entropy 0 → no S6 finding.

    Pins the entropy gate in :func:`_shannon_entropy` so the regex
    alone cannot push a long URL slug into the finding stream.
    """
    envelope = _envelope_with(prompt="Z" * 60)
    findings = scan_envelope(envelope)
    assert all(f.shape != "generic_high_entropy" for f in findings)


# ---------------------------------------------------------------------------
# Internal helpers — pin the entropy formula
# ---------------------------------------------------------------------------


def test_shannon_entropy_zero_for_homogeneous_string() -> None:
    """All-same-char string has 0 bits/char of information."""
    assert _shannon_entropy("A" * 50) == 0.0


def test_shannon_entropy_above_threshold_for_high_entropy_sample() -> None:
    """The S6 sample's entropy clears the 4.0 bits/char threshold."""
    assert _shannon_entropy(S6_HIGH_ENTROPY_SAMPLE) >= 4.0


def test_finding_dataclass_is_immutable() -> None:
    """:class:`Finding` is frozen — accidental mutation is a programming error."""
    finding = Finding(
        shape="jwt",
        location="prompt",
        redacted_preview="\u2026XXXX",
    )
    with pytest.raises((AttributeError, TypeError)):
        finding.shape = "other"  # type: ignore[misc]
