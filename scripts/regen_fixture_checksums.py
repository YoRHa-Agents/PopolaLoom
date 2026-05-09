"""Regenerate the SHA-256 manifest after a deliberate fixture update.

Usage::

    python scripts/regen_fixture_checksums.py [--dry-run]

This is a manual action. ``tests/test_fixtures_locked.py`` catches
accidental drift on every PR, so this script is invoked only when a
maintainer genuinely intends to refresh ``tests/fixtures/checksums.json``
(after editing a fixture by hand or after a live-API capture refresh).

Spec: ``.local/research/v0.9.0_ga/fixtures-strategy.md`` §4.4.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT: Path = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
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


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_excluded(rel: str) -> bool:
    if rel in EXCLUDE_NAMES:
        return True
    return any(rel.startswith(prefix) for prefix in EXCLUDE_PREFIXES)


def walk_fixtures(root: Path = ROOT) -> list[Path]:
    """Return every locked fixture file, deduped + sorted by relative path."""
    seen: set[Path] = set()
    for pat in WALKED_GLOBS:
        for path in root.glob(pat):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if _is_excluded(rel):
                continue
            seen.add(path)
    return sorted(seen, key=lambda p: p.relative_to(root).as_posix())


def _carry_meta(rel: str, prior: dict[str, Any]) -> dict[str, str]:
    """Carry forward human-curated metadata fields from a prior manifest."""
    fixtures_block = prior.get("fixtures", {}) if isinstance(prior, dict) else {}
    if not isinstance(fixtures_block, dict):
        return {}
    row = fixtures_block.get(rel, {})
    if not isinstance(row, dict):
        return {}
    out: dict[str, str] = {}
    for key in ("captured_at", "endpoint", "scenario"):
        val = row.get(key)
        if isinstance(val, str) and val:
            out[key] = val
    return out


def build_manifest(prior: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compute the manifest dict from disk; pure function (no writes)."""
    prior = prior or {}
    fixtures: dict[str, dict[str, Any]] = {}
    for path in walk_fixtures():
        rel = path.relative_to(ROOT).as_posix()
        row: dict[str, Any] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
        }
        row.update(_carry_meta(rel, prior))
        fixtures[rel] = row
    return {
        "schema_version": 1,
        "generated_at": _utc_now_iso(),
        "fixtures": dict(sorted(fixtures.items())),
    }


def _serialize(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate tests/fixtures/checksums.json (manual deliberate action).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the manifest to stdout without writing checksums.json.",
    )
    args = parser.parse_args(argv)

    prior: dict[str, Any] = {}
    if LOCK_PATH.exists():
        prior = json.loads(LOCK_PATH.read_text(encoding="utf-8"))

    manifest = build_manifest(prior)
    serialized = _serialize(manifest)
    if args.dry_run:
        sys.stdout.write(serialized)
        return 0
    LOCK_PATH.write_text(serialized, encoding="utf-8")
    sys.stdout.write(
        f"wrote {LOCK_PATH} ({len(manifest['fixtures'])} fixtures)\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
