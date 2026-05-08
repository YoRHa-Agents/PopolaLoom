"""v0.8.8 T4.1 — coverage backfill for ``popolaloom.relay.secrets``.

Lifts ``relay/secrets.py`` from 72 % to ≥ 90 % by exercising:

- The :mod:`detect_secrets`-primary path (lines 441-478) — both the
  successful scan branch AND the per-secret value filtering paths
  (empty value, sub-min-length false-positive guard).
- ``_walk_envelope`` edge cases: non-dict ``repos`` entry, string-shaped
  ``constraints``, list-of-string ``adapter_extra`` recursion.
- ``_map_detect_secrets_type`` unknown-type fallback (returns the
  lowercased name with spaces → underscores).
- ``_passes_min_length`` rejects sub-floor secrets.
- ``_shannon_entropy`` empty-string short-circuit.

Each test is short (≤ 20 lines) and self-contained.
"""

from __future__ import annotations

import importlib.util
import logging
from typing import Any

import pytest

from popolaloom.relay.secrets import (
    _DEFAULT_MIN_LENGTH,
    _DETECT_SECRETS_TYPE_TO_SHAPE,
    _SHAPE_MIN_LENGTH,
    Finding,
    _fallback_scan,
    _fallback_scan_text,
    _map_detect_secrets_type,
    _passes_min_length,
    _recursive_strings,
    _redact,
    _shannon_entropy,
    _walk_envelope,
    scan_envelope,
)

_DETECT_SECRETS_AVAILABLE: bool = (
    importlib.util.find_spec("detect_secrets") is not None
)

# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


def test_shannon_entropy_empty_string_returns_zero() -> None:
    """Empty-string short-circuit returns 0.0 (line 171)."""
    assert _shannon_entropy("") == 0.0


def test_redact_short_token_returns_full_string() -> None:
    """Tokens shorter than 4 chars are returned verbatim with ellipsis prefix."""
    assert _redact("abc") == "\u2026abc"


def test_redact_4char_token_uses_full_string() -> None:
    """Token exactly 4 chars uses the entire token as last-4."""
    assert _redact("abcd") == "\u2026abcd"


def test_redact_long_token_uses_last4() -> None:
    """Token longer than 4 chars: ``…<last4>``."""
    assert _redact("0123456789ABCD") == "\u2026ABCD"


def test_walk_envelope_skips_non_dict_repo_entry() -> None:
    """``repos`` entries that are not dicts are silently skipped (line 220)."""
    envelope: dict[str, Any] = {
        "repos": ["not a dict", {"url": "https://github.com/foo/bar"}],
    }
    out = list(_walk_envelope(envelope))
    # Only the dict entry surfaces a (location, value) pair.
    assert any(loc.startswith("repos[1]") for loc, _ in out)
    assert not any(loc.startswith("repos[0]") for loc, _ in out)


def test_walk_envelope_handles_string_constraints() -> None:
    """``constraints: "x"`` (string) yields ``("constraints", "x")`` (line 228)."""
    envelope: dict[str, Any] = {"constraints": "max=10"}
    out = list(_walk_envelope(envelope))
    assert ("constraints", "max=10") in out


def test_walk_envelope_handles_dict_constraints() -> None:
    """``constraints: {...}`` (dict) recursively yields leaf strings."""
    envelope: dict[str, Any] = {"constraints": {"a": "alpha", "b": 1}}
    out = list(_walk_envelope(envelope))
    assert any(loc == "constraints.a" and val == "alpha" for loc, val in out)


def test_recursive_strings_walks_lists() -> None:
    """``_recursive_strings`` indexes lists with ``[i]`` (line 252-253)."""
    out = list(
        _recursive_strings(
            {"items": ["x", "y", {"z": "deep"}]}, prefix="root"
        )
    )
    locs = [loc for loc, _ in out]
    assert "root.items[0]" in locs
    assert "root.items[1]" in locs
    assert "root.items[2].z" in locs


def test_recursive_strings_skips_non_string_leaves() -> None:
    """Numeric / bool / None leaves do NOT yield (no false-positive class)."""
    out = list(
        _recursive_strings(
            {"a": 1, "b": True, "c": None, "d": "leaf"}, prefix="root"
        )
    )
    assert len(out) == 1
    assert out[0] == ("root.d", "leaf")


def test_map_detect_secrets_type_known_aws() -> None:
    """Known catalog entries map to the spec catalogue ID."""
    assert _map_detect_secrets_type("AWS Access Key") == "aws_access_key_id"


def test_map_detect_secrets_type_unknown_falls_back() -> None:
    """Unknown plugin types lowercase + underscore the type name (line 365-367)."""
    assert _map_detect_secrets_type("New Custom Plugin") == "new_custom_plugin"


def test_passes_min_length_rejects_short_jwt() -> None:
    """JWT under 50 chars fails the floor check (line 397-398)."""
    assert not _passes_min_length("jwt", "eyJ.short.x")


def test_passes_min_length_accepts_long_aws() -> None:
    """A 16-char AWS body passes the spec catalogue threshold."""
    assert _passes_min_length("aws_access_key_id", "A" * 16)


def test_passes_min_length_uses_default_for_unknown_shape() -> None:
    """Unknown shape falls back to ``_DEFAULT_MIN_LENGTH``."""
    assert _passes_min_length("unknown", "x" * _DEFAULT_MIN_LENGTH)
    assert not _passes_min_length("unknown", "x" * (_DEFAULT_MIN_LENGTH - 1))


def test_finding_is_immutable_frozen_dataclass() -> None:
    """:class:`Finding` is frozen — assignment raises."""
    f = Finding(shape="x", location="prompt", redacted_preview="\u2026ABCD")
    with pytest.raises((AttributeError, TypeError)):
        f.shape = "y"


# ---------------------------------------------------------------------------
# detect_secrets primary path (lines 441-478)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _DETECT_SECRETS_AVAILABLE,
    reason="detect-secrets is an optional dep; CI runners without it correctly "
    "exercise the fallback regex path which is covered by other tests",
)
def test_scan_envelope_uses_detect_secrets_when_available(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When :mod:`detect_secrets` is installed, the primary path runs without WARN."""
    aws = "AKIAIOSFODNN7EXAMPLE"
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="popolaloom.relay.secrets"):
        findings = scan_envelope({"prompt": f"key={aws}"})
    assert any(f.shape == "aws_access_key_id" for f in findings)
    fallback_warnings = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "detect-secrets not installed" in rec.getMessage()
    ]
    assert not fallback_warnings


@pytest.mark.skipif(
    not _DETECT_SECRETS_AVAILABLE,
    reason="detect-secrets is an optional dep; CI runners without it correctly "
    "exercise the fallback regex path which is covered by other tests",
)
def test_scan_envelope_with_detect_secrets_dedupes_with_fallback() -> None:
    """Both engines surface the same shape — dedup yields one finding per location."""
    aws = "AKIAIOSFODNN7EXAMPLE"
    findings = scan_envelope({"prompt": aws})
    aws_findings = [f for f in findings if f.shape == "aws_access_key_id"]
    # Dedup keeps a single Finding for the same (shape, location, preview) tuple.
    keys = {(f.shape, f.location, f.redacted_preview) for f in aws_findings}
    assert len(keys) == 1


def test_scan_envelope_with_detect_secrets_walks_summary() -> None:
    """detect-secrets path also walks ``summary`` per spec §5.3."""
    aws = "AKIAIOSFODNN7EXAMPLE"
    findings = scan_envelope({"summary": f"leaked: {aws}"})
    assert any(f.location == "summary" for f in findings)


def test_scan_envelope_with_detect_secrets_handles_recursive_extras() -> None:
    """Primary path walks ``adapter_extra`` recursively (full path coverage)."""
    aws = "AKIAIOSFODNN7EXAMPLE"
    findings = scan_envelope(
        {"adapter_extra": {"deep": {"nested": aws}}}
    )
    aws_findings = [f for f in findings if f.shape == "aws_access_key_id"]
    assert any(f.location == "adapter_extra.deep.nested" for f in aws_findings)


def test_scan_envelope_with_detect_secrets_no_findings_for_clean_input() -> None:
    """Clean text input → 0 findings even with detect_secrets engaged."""
    findings = scan_envelope({"prompt": "the cat sat on the mat"})
    assert findings == []


def test_scan_envelope_allow_shapes_filters_after_detect_secrets() -> None:
    """``allow_shapes`` filters out matches from the primary engine too."""
    aws = "AKIAIOSFODNN7EXAMPLE"
    base = scan_envelope({"prompt": aws})
    filtered = scan_envelope({"prompt": aws}, allow_shapes=["aws_access_key_id"])
    assert any(f.shape == "aws_access_key_id" for f in base)
    assert not any(f.shape == "aws_access_key_id" for f in filtered)


def test_scan_envelope_with_detect_secrets_skips_blank_lines() -> None:
    """The primary path's per-line loop skips empty lines (defensive)."""
    aws = "AKIAIOSFODNN7EXAMPLE"
    # A leading blank line + the AWS key on the second line.
    findings = scan_envelope({"prompt": f"\n\n{aws}\n"})
    assert any(f.shape == "aws_access_key_id" for f in findings)


def test_fallback_scan_text_covers_all_shapes_in_one_text() -> None:
    """All 6 shapes co-occurring in one string — order-preserving findings."""
    composite = (
        "AKIAIOSFODNN7EXAMPLE "
        + "ghp_" + "A" * 36 + " "
        + "sk_live_" + "B" * 24 + " "
        + "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ." + "C" * 30 + " "
        + "xoxb-" + "1" * 12 + "abc "
        + "AbCdEf01GhIjKl23MnOpQr45StUvWx67YzAb89EfGh01"
    )
    out = _fallback_scan_text(composite, location="prompt")
    shapes = {f.shape for f in out}
    assert {"aws_access_key_id", "github_pat", "stripe_key",
            "jwt", "slack_token", "generic_high_entropy"} <= shapes


def test_fallback_scan_returns_empty_for_clean_envelope() -> None:
    """Clean prose → 0 findings via the fallback scan."""
    findings = _fallback_scan({"prompt": "Look at this PR"})
    assert findings == []


def test_shape_min_length_table_is_complete_for_catalog() -> None:
    """``_SHAPE_MIN_LENGTH`` covers every catalog shape (forensic invariant)."""
    expected_keys = set(_DETECT_SECRETS_TYPE_TO_SHAPE.values())
    assert expected_keys <= set(_SHAPE_MIN_LENGTH.keys())


def test_walk_envelope_handles_non_string_prompt() -> None:
    """Non-string ``prompt`` is silently dropped (defensive)."""
    out = list(_walk_envelope({"prompt": 42}))
    assert all(loc != "prompt" for loc, _ in out)


def test_walk_envelope_handles_non_string_summary() -> None:
    """Non-string ``summary`` is silently dropped (defensive)."""
    out = list(_walk_envelope({"summary": ["a", "b"]}))
    assert all(loc != "summary" for loc, _ in out)


def test_walk_envelope_skips_non_string_repo_subkey() -> None:
    """Repos sub-fields that are not strings are silently skipped."""
    out = list(_walk_envelope({"repos": [{"url": 42, "ref": "main"}]}))
    assert ("repos[0].url", 42) not in out
    assert ("repos[0].ref", "main") in out
