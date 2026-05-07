"""Unit tests for :mod:`popolaloom.handoff.writer`.

Coverage targets (per T1.2 task spec):

- Basic write: file lands at ``<base_dir>/<handoff_id>.md`` with content
  exactly equal to ``env.to_markdown()``.
- Default base_dir: when ``base_dir`` is omitted the writer uses
  :data:`popolaloom.handoff.writer.DEFAULT_HANDOFF_ROOT` (verified via
  ``monkeypatch.chdir(tmp_path)``).
- Auto ``mkdir -p``: nested missing directories are created.
- Atomicity: simulated write failure leaves no half-written target.
- Idempotency: writing the same envelope twice yields identical content
  and the same path.
- Overwrite semantics: a second write to the same path replaces the
  prior file content (used to repair corrupted envelopes).
- UTF-8 encoding for envelopes containing Chinese / emoji.
- :func:`envelope_path` is side-effect-free and matches what
  :func:`write_envelope` actually writes.
- :exc:`TypeError` for non-:class:`HandoffEnvelope` inputs (No Silent
  Failures invariant).
- :exc:`ValueError` for malformed handoff_id values that would let a
  caller escape the active root (path traversal guard).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from popolaloom.handoff import (
    DEFAULT_HANDOFF_ROOT,
    HandoffEnvelope,
    envelope_path,
    write_envelope,
)

NOW = datetime(2026, 5, 6, 22, 0, tzinfo=UTC)


def _envelope(
    *,
    handoff_id: str = "cursor-fix-bug-3a7f9c1d",
    target_cli: str = "cursor",
    prompt: str = "fix the bug in foo.py",
    **overrides: Any,
) -> HandoffEnvelope:
    """Build a HandoffEnvelope with task-spec defaults + per-test overrides."""
    base = {
        "handoff_id": handoff_id,
        "created_at": NOW,
        "target_cli": target_cli,
        "prompt": prompt,
    }
    base.update(overrides)
    return HandoffEnvelope(**base)


# ─────────────────── basic write semantics ───────────────────


def test_write_envelope_creates_file_at_expected_path(tmp_path: Path) -> None:
    env = _envelope()
    written = write_envelope(env, base_dir=tmp_path)
    assert written == tmp_path / f"{env.handoff_id}.md"
    assert written.exists()
    assert written.is_file()


def test_write_envelope_content_matches_to_markdown(tmp_path: Path) -> None:
    env = _envelope()
    written = write_envelope(env, base_dir=tmp_path)
    assert written.read_text(encoding="utf-8") == env.to_markdown()


def test_write_envelope_returns_path_pointing_to_actual_file(tmp_path: Path) -> None:
    env = _envelope()
    written = write_envelope(env, base_dir=tmp_path)
    assert written.read_text(encoding="utf-8") == env.to_markdown()
    assert written.suffix == ".md"


# ─────────────────── default base_dir ───────────────────


def test_write_envelope_default_base_dir_is_default_handoff_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``base_dir`` the writer must land under ``DEFAULT_HANDOFF_ROOT``,
    relative to CWD.  We chdir into ``tmp_path`` so the root materializes
    inside the per-test sandbox.
    """
    monkeypatch.chdir(tmp_path)
    env = _envelope()
    written = write_envelope(env)
    expected = tmp_path / DEFAULT_HANDOFF_ROOT / f"{env.handoff_id}.md"
    assert written.resolve() == expected.resolve()
    assert expected.exists()


def test_default_handoff_root_is_under_local_agent(tmp_path: Path) -> None:
    """Sanity: the default root should live under ``.local/.agent/handoff``
    (which v0.7.0's ``.gitignore`` already excludes)."""
    parts = DEFAULT_HANDOFF_ROOT.parts
    assert parts == (".local", ".agent", "handoff"), parts


# ─────────────────── auto-mkdir ───────────────────


def test_write_envelope_creates_missing_base_dir(tmp_path: Path) -> None:
    base = tmp_path / "does" / "not" / "exist" / "yet"
    assert not base.exists()
    env = _envelope()
    written = write_envelope(env, base_dir=base)
    assert base.is_dir()
    assert written.parent == base
    assert written.exists()


def test_write_envelope_accepts_string_base_dir(tmp_path: Path) -> None:
    """``base_dir`` annotated as ``Path | str | None`` — must accept str."""
    env = _envelope()
    written = write_envelope(env, base_dir=str(tmp_path))
    assert written == tmp_path / f"{env.handoff_id}.md"
    assert written.exists()


# ─────────────────── atomicity ───────────────────


def test_write_envelope_atomic_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a failure inside the tmp-file write step.  After the
    failure the *target* path must not exist (it is only ``os.replace``'d
    *after* the tmp file is fully written).
    """
    env = _envelope()
    target = tmp_path / f"{env.handoff_id}.md"
    assert not target.exists()

    real_write_text = Path.write_text

    def boom(self: Path, *args: Any, **kwargs: Any) -> int:
        if self.suffix == ".tmp":
            raise OSError("simulated disk-full during tmp write")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", boom)

    with pytest.raises(OSError, match=r"simulated disk-full"):
        write_envelope(env, base_dir=tmp_path)

    assert not target.exists(), "target file must not exist after a mid-write failure"


def test_write_envelope_atomic_failure_does_not_leave_tmp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a failed write the writer best-effort cleans up the ``.tmp``
    sibling so the next run isn't tripped up by stale debris."""
    env = _envelope()
    target = tmp_path / f"{env.handoff_id}.md"
    tmp_sibling = tmp_path / f"{env.handoff_id}.md.tmp"

    real_replace = __import__("os").replace

    def boom_replace(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated rename failure (EXDEV-ish)")

    monkeypatch.setattr("os.replace", boom_replace)

    with pytest.raises(OSError, match=r"simulated rename failure"):
        write_envelope(env, base_dir=tmp_path)

    # restore (defensive — tmp_path teardown handles it anyway)
    monkeypatch.setattr("os.replace", real_replace)
    assert not target.exists()
    assert not tmp_sibling.exists(), "tmp file should be cleaned up on failure"


# ─────────────────── idempotency / overwrite ───────────────────


def test_write_envelope_idempotent_same_envelope(tmp_path: Path) -> None:
    env = _envelope()
    p1 = write_envelope(env, base_dir=tmp_path)
    p2 = write_envelope(env, base_dir=tmp_path)
    assert p1 == p2
    assert p1.read_text(encoding="utf-8") == p2.read_text(encoding="utf-8")
    assert p2.read_text(encoding="utf-8") == env.to_markdown()


def test_write_envelope_overwrites_existing_file(tmp_path: Path) -> None:
    """Different envelopes that *happen* to share a handoff_id (e.g. when
    the caller uses a hand-picked id outside ``generate_handoff_id``) →
    second write replaces the first.  This is the documented contract:
    ``os.replace`` is replace-if-exists."""
    env_a = _envelope(prompt="alpha prompt")
    env_b = _envelope(prompt="beta prompt different")  # same handoff_id (hand-set)

    p1 = write_envelope(env_a, base_dir=tmp_path)
    assert p1.read_text(encoding="utf-8") == env_a.to_markdown()

    p2 = write_envelope(env_b, base_dir=tmp_path)
    assert p2 == p1
    assert p2.read_text(encoding="utf-8") == env_b.to_markdown()
    assert "beta prompt different" in p2.read_text(encoding="utf-8")
    assert "alpha prompt" not in p2.read_text(encoding="utf-8")


# ─────────────────── encoding ───────────────────


def test_write_envelope_utf8_encoding_chinese_and_emoji(tmp_path: Path) -> None:
    env = _envelope(prompt="修复 bug 🚀 in 中文 emoji land")
    written = write_envelope(env, base_dir=tmp_path)

    raw_bytes = written.read_bytes()
    decoded = raw_bytes.decode("utf-8")
    assert "修复" in decoded
    assert "🚀" in decoded
    assert "中文" in decoded
    assert decoded == env.to_markdown()


# ─────────────────── envelope_path ───────────────────


def test_envelope_path_matches_actual_write_target(tmp_path: Path) -> None:
    env = _envelope()
    predicted = envelope_path(env.handoff_id, base_dir=tmp_path)
    written = write_envelope(env, base_dir=tmp_path)
    assert predicted == written


def test_envelope_path_does_not_create_file_or_dir(tmp_path: Path) -> None:
    nested = tmp_path / "nope" / "deeper"
    p = envelope_path("cursor-x-deadbeef", base_dir=nested)
    assert not p.exists()
    assert not p.parent.exists()


def test_envelope_path_default_base_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    p = envelope_path("cursor-x-deadbeef")
    assert p == DEFAULT_HANDOFF_ROOT / "cursor-x-deadbeef.md"


# ─────────────────── input validation (No Silent Failures) ───────────────────


@pytest.mark.parametrize(
    "bad_input",
    [
        None,
        {"handoff_id": "x", "prompt": "y"},
        "not an envelope",
        42,
        [],
    ],
)
def test_write_envelope_typeerror_for_non_envelope_input(
    tmp_path: Path, bad_input: Any
) -> None:
    with pytest.raises(TypeError, match=r"must be a HandoffEnvelope"):
        write_envelope(bad_input, base_dir=tmp_path)  # type: ignore[arg-type]


def test_envelope_path_rejects_empty_handoff_id() -> None:
    with pytest.raises(ValueError, match=r"non-empty"):
        envelope_path("")


@pytest.mark.parametrize(
    "bad_id",
    [
        "cursor/x-12345678",
        "cursor\\x-12345678",
        "../escape-12345678",
        "..",
    ],
)
def test_envelope_path_rejects_traversal_handoff_id(bad_id: str) -> None:
    with pytest.raises(ValueError):
        envelope_path(bad_id)
