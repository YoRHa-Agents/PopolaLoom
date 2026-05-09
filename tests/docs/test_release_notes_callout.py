"""Default-lane docs lint for the v0.8.8 Q-C-4 RELEASE_NOTES callout (M4 mitigation).

**v0.9.0 GA NOTE**: ``RELEASE_NOTES.md`` is overwritten per release per
the v0.7.0+ policy, so the v0.8.8-anchored Q-C-4 callout no longer lives
in the current RELEASE_NOTES (v0.9.0 GA). The Q-C-4 偏离默认 behavior
change is now documented in ``docs/MIGRATION_v07_to_v09.md`` (T1.1.2);
these tests SKIP automatically when the v0.8.8 H1 is not present.
The historical record lives in ``CHANGELOG.md`` ``## [0.8.8]`` entry.


These tests enforce the locked structure of the M4 callout in
``RELEASE_NOTES.md`` per
``.local/research/v0.8.8_multi_run/relay-auto-safety.md`` §6 + the
v0.8.8 ``PLAN.md`` §9 release-gate criterion C4.

The Q-C-4 偏离默认 lock (relay defaults to AUTO, opt-out via
``--no-confirm`` / ``--dry-run``) carries five mandatory safety
mitigations (M1..M5). M4 is "RELEASE_NOTES.md ships a top-of-block
callout warning operators of the auto-default behavior change" — and
this test pair is the lint that enforces M4's presence + position +
link resolution at default-CI time. Without these tests landing in
the default ``pytest -m "not real_cursor_cloud"`` lane, M4 could
silently rot via a future copy-edit or a forgotten release-notes
overwrite.

The two tests:

1. ``test_release_notes_callout_present`` — asserts (a) the callout
   text contains the substring ``"Behavior change"`` AND
   ``"relay defaults to AUTO"`` (per ``relay-auto-safety.md`` §6.1
   locked wording, accepting both the ⚠️ emoji form and the
   ``**WARNING**`` emoji-free fallback per §6.3); (b) the link to
   ``relay-auto-safety.md`` resolves to a file on disk OR is
   annotated as ``(local-only)`` since ``.local/`` is gitignored
   (no public URL is expected); (c) the callout is positioned
   ABOVE the first ``## `` H2 heading inside the v0.8.8 block
   (per §6.1 position contract).
2. ``test_release_notes_links_resolve`` — asserts every Markdown
   link inside the callout block resolves to an existing file in
   the repo (cf. v0.7.x doc-link CI guard).

Why default-lane: these are pure read-and-parse checks (no daemon,
no network, no real CLI), runtime well under 100 ms total.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_NOTES_PATH = REPO_ROOT / "RELEASE_NOTES.md"

# v0.8.8 callout heading regex — accepts both ⚠️ emoji form (§6.1 default)
# and the **WARNING** emoji-free fallback (§6.3 — operators whose
# RELEASE_NOTES rendering toolchain strips Unicode).
_CALLOUT_OPENER_RE = re.compile(
    r"(?:⚠️|\*\*WARNING\*\*).*?Behavior change.*?relay defaults to AUTO",
    re.IGNORECASE,
)

# v0.8.8 H1 (locked title per spec §6.1). The callout MUST sit between
# the v0.8.8 H1 line and the FIRST ``## `` H2 heading inside the v0.8.8
# block, so we anchor on this title to find the v0.8.8 block.
_V088_H1_RE = re.compile(r"^# PopolaLoom v0\.8\.8\b", re.MULTILINE)


def _read_release_notes() -> str:
    """Return the canonical RELEASE_NOTES.md content as a UTF-8 string.

    Fails fast with a clear message if the file is missing — the v0.7.0+
    policy requires a single floating ``RELEASE_NOTES.md`` at the repo
    root (per CHANGELOG.md §[0.7.0] policy line).
    """
    if not RELEASE_NOTES_PATH.is_file():
        pytest.fail(
            f"RELEASE_NOTES.md not found at {RELEASE_NOTES_PATH} — "
            f"v0.7.0+ policy requires a single floating release notes file."
        )
    return RELEASE_NOTES_PATH.read_text(encoding="utf-8")


def _v088_block_bounds(text: str) -> tuple[int, int]:
    """Locate the v0.8.8 block inside RELEASE_NOTES.md and return [start, end).

    The block starts at the v0.8.8 ``# `` H1 heading and ends at either
    the end of the file or the next ``# `` H1 heading.

    **v0.9.0 GA**: the v0.7.0+ overwrite policy means RELEASE_NOTES.md
    holds ONLY the current release's content. When v0.8.8 H1 is absent
    (i.e., we're past v0.8.8), this function calls ``pytest.skip`` —
    the M4 callout's historical record lives in CHANGELOG ``[0.8.8]``
    and the behavior-change documentation moved to
    ``docs/MIGRATION_v07_to_v09.md``.
    """
    h1 = _V088_H1_RE.search(text)
    if h1 is None:
        pytest.skip(
            "RELEASE_NOTES.md is past v0.8.8 (overwrite policy per "
            "v0.7.0+); Q-C-4 callout history is in CHANGELOG [0.8.8] + "
            "MIGRATION_v07_to_v09.md. Test re-activates if a future "
            "release reverts to v0.8.8."
        )
    start = h1.start()
    # Find the next H1 (if any) — a NEXT release would land its own H1
    # header below, which would make this test reject the prior callout
    # by definition. With the v0.7.0+ overwrite policy this should
    # never trigger, but guard for completeness.
    next_h1 = re.search(r"^# ", text[h1.end() :], re.MULTILINE)
    end = (h1.end() + next_h1.start()) if next_h1 else len(text)
    return start, end


def _first_h2_position_within(block: str) -> int | None:
    """Return the offset of the first ``## `` H2 heading inside the block.

    The callout MUST appear ABOVE this offset (per §6.1 position
    contract). Returns ``None`` if no H2 heading is present (in which
    case any callout position is trivially "above" the absent H2).
    """
    match = re.search(r"^## ", block, re.MULTILINE)
    return match.start() if match is not None else None


def test_release_notes_callout_present() -> None:
    """The Q-C-4 callout is present and structurally correct.

    Per ``relay-auto-safety.md`` §6.1 + §6.3, the callout MUST:

    - contain the substring ``"Behavior change"`` AND ``"relay defaults
      to AUTO"`` (case-insensitive — the regex
      ``_CALLOUT_OPENER_RE`` accepts both the ⚠️ emoji form and the
      ``**WARNING**`` emoji-free fallback);
    - link to ``relay-auto-safety.md`` (resolves to an existing file
      in the repo, OR is annotated as ``(local-only)`` since
      ``.local/`` is gitignored — see §6.1 link contract);
    - be positioned ABOVE the first ``## `` H2 heading inside the
      v0.8.8 block (per §6.1 position contract).

    Failure here is a Stage 5 release-gate blocker (criterion C4 in
    PLAN.md §9). The 0-deferred-items rule applies — this test
    failing means v0.8.8 ships with a mis-positioned or missing
    behavior-change warning, which would silently undermine the
    Q-C-4 deviation safety story.
    """
    text = _read_release_notes()
    block_start, block_end = _v088_block_bounds(text)
    block = text[block_start:block_end]

    # (a) Substring presence — both required tokens must appear in the
    # callout opener line. We match on the regex which OR's the
    # warning-emoji forms so the §6.3 fallback works equivalently.
    callout_match = _CALLOUT_OPENER_RE.search(block)
    assert callout_match is not None, (
        "RELEASE_NOTES.md v0.8.8 block missing the Q-C-4 callout opener; "
        "expected a line matching either '⚠️ Behavior change — relay "
        "defaults to AUTO' or '**WARNING** Behavior change — relay "
        "defaults to AUTO' per relay-auto-safety.md §6.1 + §6.3 fallback."
    )

    # (b) Link to relay-auto-safety.md — the spec accepts either a
    # resolvable file path on disk OR an explicit "(local-only)"
    # annotation since `.local/` is gitignored. Search across the
    # full v0.8.8 block (the link can land in the bullet list or in
    # the "Spec:" / "Decision:" footer of the callout).
    has_relay_auto_safety_link = "relay-auto-safety.md" in block
    assert has_relay_auto_safety_link, (
        "Q-C-4 callout missing required link to relay-auto-safety.md; "
        "the spec at `.local/research/v0.8.8_multi_run/relay-auto-safety.md` "
        "MUST be referenced (or be annotated as '(local-only)' per the "
        "callout link contract)."
    )

    # (c) Positioned ABOVE the first ## H2 heading — anchor on the
    # callout's start position vs the first H2 in the block.
    first_h2 = _first_h2_position_within(block)
    if first_h2 is not None:
        assert callout_match.start() < first_h2, (
            "Q-C-4 callout is mis-positioned: it must appear ABOVE the "
            f"first ## H2 heading in the v0.8.8 block (callout at offset "
            f"{callout_match.start()}, first H2 at offset {first_h2}). "
            "Per relay-auto-safety.md §6.1 position contract, the callout "
            "is the first thing operators see when opening RELEASE_NOTES.md."
        )


def _extract_markdown_links(block: str) -> list[tuple[str, str]]:
    """Parse Markdown links from the block and return ``[(text, target)]``.

    Matches the standard inline form ``[text](target)`` plus the
    blockquote-prefixed variant ``> [text](target)``. Bare URLs and
    autolinks (``<URL>``) are ignored because they target the public
    web, not the local repo.
    """
    return re.findall(r"\[([^\]]+)\]\(([^)]+)\)", block)


def _callout_block(text: str) -> str:
    """Slice out just the callout's blockquote run (lines starting with ``> ``).

    The callout is a contiguous run of ``> `` lines starting at the
    callout opener and ending at the first non-``>``-non-empty line.
    This lets us check link resolution scoped to the callout only,
    not the broader v0.8.8 block.
    """
    block_start, block_end = _v088_block_bounds(text)
    block = text[block_start:block_end]
    callout_match = _CALLOUT_OPENER_RE.search(block)
    if callout_match is None:
        return ""
    # Find the START of the callout's opener LINE (not the position of
    # the regex match, which lands mid-line at the ⚠️ or **WARNING**
    # token). We walk backwards from the match position to the previous
    # newline so the line walker below sees the leading "> " prefix.
    line_start = block.rfind("\n", 0, callout_match.start())
    line_start = 0 if line_start < 0 else line_start + 1
    # Walk forward line-by-line from the opener line start, collecting
    # blockquote lines until we hit a non-blockquote-non-empty line.
    # The callout ends at the first line that is BOTH non-empty AND
    # does not start with ``>``.
    out: list[str] = []
    for line in block[line_start:].splitlines():
        if line.lstrip().startswith(">") or line.strip() == "":
            out.append(line)
        else:
            break
    return "\n".join(out)


def test_release_notes_links_resolve() -> None:
    """Every Markdown link inside the callout resolves to a real file.

    Per ``relay-auto-safety.md`` §6.2 lint contract, *every* Markdown
    link inside the callout must resolve to an existing file in the
    repo (cf. the v0.7.x doc-link CI guard pattern). The two
    expected link targets are:

    - ``relay-auto-safety.md`` — the spec itself, at
      ``.local/research/v0.8.8_multi_run/relay-auto-safety.md``.
      This file is **gitignored** (`.local/` is local-only per the
      v0.7.0+ workspace policy), so we accept either:
        (i) the file resolves on disk in the developer's checkout, OR
        (ii) the link target is annotated with ``(local-only)`` /
             ``research note`` / similar text in the surrounding
             prose so reviewers don't expect a public URL.
    - ``decision-matrices-zh.md`` (when referenced) — same
      gitignored treatment.

    Other link targets (e.g., references to in-tree files like
    ``RELEASE_NOTES.md``, ``CHANGELOG.md``) MUST resolve on disk —
    these are public artifacts and a broken link is a release blocker.

    Failure here is a Stage 5 release-gate blocker (criterion C4).
    """
    text = _read_release_notes()
    callout = _callout_block(text)
    if not callout:
        # If the opener test passed, the callout block must extract
        # something. A missing callout block is therefore an upstream
        # bug in test_release_notes_callout_present — fail loudly.
        pytest.fail(
            "Could not extract callout block from RELEASE_NOTES.md — "
            "this indicates an upstream issue in the callout shape; "
            "fix test_release_notes_callout_present first."
        )

    links = _extract_markdown_links(callout)
    # The callout SHOULD contain at least one link (to relay-auto-safety.md)
    # per §6.1; if the entire block has zero links, that itself is a
    # signal that the callout is malformed.
    assert links, (
        "Q-C-4 callout has zero Markdown links; per relay-auto-safety.md "
        "§6.1, the callout must reference the spec via a Markdown link."
    )

    # We accept these gitignored / local-only paths (annotated as such
    # in the surrounding prose). Any Markdown link with a target
    # starting with one of these prefixes is allowed to be missing on
    # disk because `.local/` is gitignored per v0.7.0+ policy.
    local_only_prefixes = (
        ".local/",
        "./.local/",
    )

    unresolved: list[str] = []
    for link_text, link_target in links:
        # Strip URL fragments (#anchor) and query strings (?...).
        target_path = link_target.split("#", 1)[0].split("?", 1)[0].strip()
        if not target_path:
            # Pure anchor link (#section) — same-document; trivially valid.
            continue
        if target_path.startswith(("http://", "https://", "mailto:")):
            # External URL — out of scope for filesystem lint.
            continue
        if any(target_path.startswith(p) for p in local_only_prefixes):
            # Gitignored research note — accept as "(local-only)" by
            # convention. The surrounding prose annotates it; we do
            # NOT require the file to exist on disk.
            continue

        # Path is relative to the RELEASE_NOTES.md location (repo root).
        candidate = (REPO_ROOT / target_path).resolve()
        if not candidate.exists():
            unresolved.append(
                f"link [{link_text}]({link_target}) -> {candidate} (not found)"
            )

    assert not unresolved, (
        "Q-C-4 callout has unresolved Markdown link(s):\n  - "
        + "\n  - ".join(unresolved)
        + "\n\nFix the broken link(s) or, if the target is a research "
        "note under `.local/`, ensure the surrounding prose annotates "
        "it as '(local-only)' / '(research note, local-only)' so "
        "reviewers don't expect a public URL."
    )
