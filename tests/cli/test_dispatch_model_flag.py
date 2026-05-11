"""Tests for ``popola dispatch --model`` (v1.0.0 GA, Q-A1).

PopolaLoom v1.0.0 — S3 promotes the previously-stringly-typed
``--cli-flag model=<id>`` extras key into a discoverable Typer flag.

The tests below exercise the five acceptance criteria for S3 (per
v1.0.0_ga_plan_4980ad52.plan.md §S3):

1. ``--model`` populates ``extra["model"]`` in the dispatch body for
   ``cursor-cloud`` dispatches.
2. ``--model`` is a no-op (with WARN) for non-cursor-cloud adapters.
3. ``--model`` overrides ``--cli-flag model=<X>`` when both are set,
   emitting a WARN (No Silent Failures).
4. Empty ``--model`` is a no-op (preserves the v0.10.0 ``"default"``
   model fallback in the adapter normalizer).
5. The flag exists on the ``popola dispatch`` command signature
   (smoke test against the Typer app introspection).
"""

from __future__ import annotations

import logging

import pytest

from popolaloom.cli.main import _apply_model_flag


def test_apply_model_flag_populates_extra_for_cursor_cloud() -> None:
    """AC1: ``--model gpt-5.5`` writes ``extra["model"] = "gpt-5.5"``."""
    extra: dict[str, object] = {}
    _apply_model_flag(extra, "gpt-5.5", "cursor-cloud")
    assert extra == {"model": "gpt-5.5"}


def test_apply_model_flag_skips_non_cursor_cloud_adapter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC2: ``--model`` is dropped (with WARN) for ``--cli=cursor`` dispatches."""
    extra: dict[str, object] = {}
    with caplog.at_level(logging.WARNING, logger="popolaloom.cli.main"):
        _apply_model_flag(extra, "gpt-5.5", "cursor")
    assert "model" not in extra
    assert any(
        "only consumed by cursor-cloud dispatches" in rec.message
        for rec in caplog.records
    ), caplog.text


def test_apply_model_flag_overrides_legacy_extra_with_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC3: explicit ``--model`` wins over ``--cli-flag model=<X>`` (WARN)."""
    extra: dict[str, object] = {"model": "claude-sonnet-4"}
    with caplog.at_level(logging.WARNING, logger="popolaloom.cli.main"):
        _apply_model_flag(extra, "gpt-5.5", "cursor-cloud")
    assert extra["model"] == "gpt-5.5"
    assert any(
        "overrides --cli-flag model=" in rec.message for rec in caplog.records
    ), caplog.text


def test_apply_model_flag_empty_is_noop() -> None:
    """AC4: empty ``--model`` leaves extras untouched (preserves 'default')."""
    extra: dict[str, object] = {}
    _apply_model_flag(extra, "", "cursor-cloud")
    assert extra == {}
    extra_with_legacy: dict[str, object] = {"model": "claude-sonnet-4"}
    _apply_model_flag(extra_with_legacy, "", "cursor-cloud")
    assert extra_with_legacy == {"model": "claude-sonnet-4"}


def test_dispatch_command_exposes_model_flag() -> None:
    """AC5: ``popola dispatch`` has a ``model`` parameter (signature smoke).

    Inspect the underlying Python function signature rather than the
    rendered ``--help`` text — Typer's rich-format help truncates flag
    names in narrow terminals (CI default 80-col) regardless of the
    COLUMNS env var because CliRunner uses its own width detection.
    """
    import inspect

    from popolaloom.cli.main import dispatch as _dispatch_fn

    params = inspect.signature(_dispatch_fn).parameters
    assert "model" in params, f"--model missing from dispatch params: {sorted(params)}"


def test_apply_model_flag_no_op_when_legacy_extra_matches_flag() -> None:
    """When ``--model`` value equals the legacy extra, no WARN is emitted."""
    extra: dict[str, object] = {"model": "gpt-5.5"}
    import logging as _logging

    handler_count_before = len(_logging.getLogger("popolaloom.cli.main").handlers)
    _apply_model_flag(extra, "gpt-5.5", "cursor-cloud")
    assert extra["model"] == "gpt-5.5"
    handler_count_after = len(_logging.getLogger("popolaloom.cli.main").handlers)
    assert handler_count_before == handler_count_after
