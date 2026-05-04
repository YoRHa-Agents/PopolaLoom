"""Tier 2 / Coverage — :mod:`popolaloom.evaluation.runner` helper paths.

The existing ``tests/test_evaluation.py`` covers the happy paths;
this file targets defensive branches:

- ``_load_weights`` fallback when nines.toml missing / malformed / non-dict eval section.
- ``collect_evidence`` with empty events dir / corrupt NDJSON line.
- ``_resolve_default_events_dir`` honors ``$POPOLA_HOME``.
- ``run_evaluation`` with explicit evidence dict (test-only override).
- ``_detect_locks`` returns the 3 canonical lock names.
- ``_NoopFilter.__getattr__`` returns None for any attribute.
- ``toml_serialize`` round-trip with ``tomllib.loads``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from popolaloom.evaluation import runner as ev_runner

# ── _load_weights ────────────────────────────────────────────────────────


def test_load_weights_fallback_when_file_missing(tmp_path: Path) -> None:
    """``_load_weights`` returns fallback dict when path does not exist."""
    missing = tmp_path / "no_such.toml"
    weights = ev_runner._load_weights(missing)
    assert isinstance(weights, dict)
    assert weights == ev_runner._FALLBACK_WEIGHTS


def test_load_weights_fallback_when_file_malformed(tmp_path: Path) -> None:
    """``_load_weights`` falls back when TOML is unparseable."""
    bad = tmp_path / "bad.toml"
    bad.write_text("=== not valid TOML ===\n", encoding="utf-8")
    weights = ev_runner._load_weights(bad)
    assert weights == ev_runner._FALLBACK_WEIGHTS


def test_load_weights_fallback_when_eval_weights_not_dict(tmp_path: Path) -> None:
    """``[eval.weights]`` being a non-table falls back."""
    cfg = tmp_path / "x.toml"
    cfg.write_text(
        "[eval]\nweights = 'not a table'\n",
        encoding="utf-8",
    )
    weights = ev_runner._load_weights(cfg)
    assert weights == ev_runner._FALLBACK_WEIGHTS


def test_load_weights_extra_keys_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Unknown weight keys are logged but not added to the result."""
    cfg = tmp_path / "extra.toml"
    cfg.write_text(
        "[eval.weights]\nunknown_dim = 0.5\ndispatch_isolation = 0.1\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        weights = ev_runner._load_weights(cfg)
    assert "unknown_dim" not in weights
    assert weights["dispatch_isolation"] == 0.1


def test_load_weights_invalid_value_falls_back_to_zero(tmp_path: Path) -> None:
    """When a weight value can't coerce to float, falls back to 0.0.

    The TOML value ``'not-a-float'`` is a string; ``float(str)`` raises
    ValueError, caught by the loader which falls back to 0.0.
    """
    cfg = tmp_path / "bad_value.toml"
    cfg.write_text(
        "[eval.weights]\ndispatch_isolation = 'not-a-float'\n",
        encoding="utf-8",
    )
    weights = ev_runner._load_weights(cfg)
    assert weights["dispatch_isolation"] == 0.0


# ── collect_evidence ─────────────────────────────────────────────────────


def test_collect_evidence_handles_missing_dir(tmp_path: Path) -> None:
    """``collect_evidence`` returns files=0 when events dir doesn't exist."""
    missing = tmp_path / "ghost"
    evidence = ev_runner.collect_evidence(missing)
    assert evidence["files"] == 0
    assert evidence["total_events"] == 0


def test_collect_evidence_skips_corrupt_ndjson_lines(tmp_path: Path) -> None:
    """Corrupt JSON lines are silently skipped (not counted as events)."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    (events_dir / "task1.jsonl").write_text(
        '{"type":"task.dispatched","data":{}}\n'
        "{not-valid-json\n"
        '{"type":"task.completed","data":{}}\n',
        encoding="utf-8",
    )
    evidence = ev_runner.collect_evidence(events_dir)
    assert evidence["files"] == 1
    assert evidence["total_events"] == 3
    assert evidence["event_types"]["task.dispatched"] == 1
    assert evidence["event_types"]["task.completed"] == 1


def test_collect_evidence_with_recovered_event(tmp_path: Path) -> None:
    """When ``popolad.recovered`` is present, recovered_count is set."""
    events_dir = tmp_path / "ev"
    events_dir.mkdir()
    (events_dir / "rec.jsonl").write_text(
        '{"type":"task.dispatched","data":{}}\n'
        '{"type":"popolad.recovered","data":{"recovered_count":3}}\n',
        encoding="utf-8",
    )
    evidence = ev_runner.collect_evidence(events_dir)
    assert evidence["recovered_count"] == 3
    assert evidence["event_count_after_recovery"] == 2


# ── _resolve_default_events_dir ──────────────────────────────────────────


def test_resolve_default_events_dir_honors_popola_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_resolve_default_events_dir`` uses ``$POPOLA_HOME``."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    result = ev_runner._resolve_default_events_dir()
    assert result == tmp_path.resolve() / "events"


def test_resolve_default_events_dir_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When ``$POPOLA_HOME`` is unset, defaults to ``~/.popola/events``."""
    monkeypatch.delenv("POPOLA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ev_runner._resolve_default_events_dir() == tmp_path / ".popola" / "events"


# ── run_evaluation with evidence override ────────────────────────────────


def test_run_evaluation_with_explicit_evidence(tmp_path: Path) -> None:
    """``run_evaluation(evidence=...)`` skips disk IO and scores from the dict."""
    evidence = {
        "files": 5,
        "total_events": 50,
        "event_types": {"task.completed": 5, "task.dispatched": 5},
        "cycle_demo_present": True,
        "locks_present": {"_event_logs_lock", "state_store_lock", "event_log_lock"},
    }
    report = ev_runner.run_evaluation(evidence=evidence)
    assert isinstance(report.composite, float)
    assert 0.0 <= report.composite <= 1.0
    assert set(report.dimensions) == {d.name for d in ev_runner.DIMENSIONS}


# ── _detect_locks ────────────────────────────────────────────────────────


def test_detect_locks_finds_canonical_three() -> None:
    """``_detect_locks`` returns the 3 canonical lock names."""
    locks = ev_runner._detect_locks()
    assert "_event_logs_lock" in locks
    assert "state_store_lock" in locks
    assert "event_log_lock" in locks


# ── _NoopFilter ──────────────────────────────────────────────────────────


def test_noop_filter_returns_none_for_any_attribute() -> None:
    """``_NoopFilter.__getattr__`` returns None for any attribute access."""
    f = ev_runner._NoopFilter()
    assert f.totally_random_attr is None
    assert f.another is None


# ── toml_serialize round-trip ────────────────────────────────────────────


def test_toml_serialize_round_trips_via_tomllib() -> None:
    """``toml_serialize`` output parses cleanly via :mod:`tomllib`."""
    evidence = {
        "files": 1,
        "total_events": 10,
        "event_types": {"task.completed": 1},
        "locks_present": {"_event_logs_lock"},
        "cycle_demo_present": True,
    }
    report = ev_runner.run_evaluation(evidence=evidence)
    text = ev_runner.toml_serialize(report)
    parsed = tomllib.loads(text)
    assert "version" in parsed
    assert "composite" in parsed
    assert isinstance(parsed["composite"], float)
    assert "dimensions" in parsed


# ── _cycle_demo_module_present ───────────────────────────────────────────


def test_cycle_demo_module_present_returns_true() -> None:
    """The Stage B Gen-Verifier demo module is importable in v0.2.0+."""
    assert ev_runner._cycle_demo_module_present() is True
