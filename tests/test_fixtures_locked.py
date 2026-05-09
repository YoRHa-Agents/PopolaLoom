"""Drift-detection lint test for ``tests/fixtures/``.

Walks the fixture tree, computes the SHA-256 of every locked file,
and asserts the digest matches the value committed in
``tests/fixtures/checksums.json``.

If this test fails, the cause is almost always a hand-edited fixture
under ``tests/fixtures/`` whose author forgot to run::

    python scripts/regen_fixture_checksums.py

Spec: ``.local/research/v0.9.0_ga/fixtures-strategy.md`` §4.2.

This test is default-lane (no ``CURSOR_API_KEY`` required) and adds
<50 ms to the suite. The complementary live-API drift comparison runs
monthly via ``.github/workflows/cloud-fixtures-drift-check.yml``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT: Path = Path(__file__).parent / "fixtures"
LOCK_PATH: Path = ROOT / "checksums.json"

WALKED_GLOBS: tuple[str, ...] = ("**/*.json", "**/*.txt")
EXCLUDE_NAMES: frozenset[str] = frozenset(
    {
        "checksums.json",
        "README.md",
        "__init__.py",
        "real_popolad.py",
    }
)
EXCLUDE_PREFIXES: tuple[str, ...] = ("mock_cli/",)


def _is_excluded(rel: str) -> bool:
    if rel in EXCLUDE_NAMES:
        return True
    return any(rel.startswith(prefix) for prefix in EXCLUDE_PREFIXES)


def _walked_relpaths() -> list[str]:
    rels: set[str] = set()
    for pat in WALKED_GLOBS:
        for path in ROOT.glob(pat):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT).as_posix()
            if _is_excluded(rel):
                continue
            rels.add(rel)
    return sorted(rels)


def test_fixtures_match_checksums() -> None:
    """Every locked fixture's on-disk SHA-256 matches checksums.json."""
    assert LOCK_PATH.exists(), (
        f"missing manifest: {LOCK_PATH}; run "
        f"`python scripts/regen_fixture_checksums.py` to create it"
    )

    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    rows = lock.get("fixtures")
    assert isinstance(rows, dict), (
        f"checksums.json must contain a top-level `fixtures` dict; got {type(rows).__name__}"
    )

    drifted: list[str] = []
    walked = _walked_relpaths()
    for rel in walked:
        path = ROOT / rel
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        expected_row = rows.get(rel)
        if expected_row is None:
            drifted.append(f"  orphan fixture (missing from checksums.json): {rel}")
            continue
        expected_sha = expected_row.get("sha256") if isinstance(expected_row, dict) else None
        if expected_sha != digest:
            drifted.append(
                f"  SHA-256 drift: {rel}\n"
                f"    expected: {expected_sha}\n"
                f"    actual:   {digest}"
            )

    walked_set = set(walked)
    extra_rows = sorted(set(rows) - walked_set)
    drifted.extend(
        f"  manifest references missing file: {rel}" for rel in extra_rows
    )

    assert not drifted, (
        "tests/fixtures/checksums.json is out of sync. Re-run "
        "`python scripts/regen_fixture_checksums.py` after a deliberate "
        "fixture update.\nDrift:\n" + "\n".join(drifted)
    )
