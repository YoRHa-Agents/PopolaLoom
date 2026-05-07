> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.8.2 — docs UX overhaul (content rot + UX polish)

> Released: 2026-05-07
> Theme: clears the v0.7.0 content rot left in `QUICKSTART.md` / `USER_GUIDE.md` / `DEMO.md` after the v0.7.x → v0.8.0 → v0.8.1 release chain, adds a full v0.8.0 hands-off envelope walkthrough to `DEMO.md`, and ships 4 UX polish features that v0.8.1 deferred (copy buttons, anchor links, EN-only honest disclosure, refined Popola SVG mark). **No source-code changes** — entire patch only touches `docs/` static assets + version metadata.

## Why v0.8.2 right after v0.8.1?

Direct user feedback: **"网页问题也依旧是不合格的"** (the web is still substandard). Investigation revealed two distinct legs:

1. **Content rot**: deployed GitHub Pages site showed `popolaloom v0.7.0 ready` + `User Guide (v0.7.0)` + a `DEMO.md` frozen at v0.3.5 → v0.7.0. Every visitor doubted whether the v0.8.x release chain actually shipped. Worse, `DEMO.md` had **zero mention of the hands-off envelope** — the project's biggest v0.8.x feature.

2. **UX polish gaps**: v0.8.1's bilingual + day-night surface had silent failures on doc pages (lang-toggle did nothing on QUICKSTART/USER_GUIDE/DEMO since they had no `data-i18n` hooks); no copy buttons on code blocks; no permalink ¶ on headings; the Popola SVG mark was overly minimal (just 2 concentric rings + a vertical hairline).

v0.8.2 fixes both legs as a single docs-only patch.

## What's NEW · `DEMO.md` v0.8.0 hands-off envelope walkthrough

Visit <https://yorha-agents.github.io/PopolaLoom/DEMO.html> for the full walkthrough. Highlights:

```bash
$ popola dispatch "fix the bug in foo.py — there's a NoneType error around line 42" --cli=cursor
# → cursor-1f0a2b8d4e5c

# behind the scenes: a Markdown front-matter envelope is written
$ ls .local/.agent/handoff/
cursor-fix-the-bug-in-foo-py-3a7f9c1d.md

$ cat .local/.agent/handoff/cursor-fix-the-bug-in-foo-py-3a7f9c1d.md
---
schema_version: '1'
handoff_id: cursor-fix-the-bug-in-foo-py-3a7f9c1d
created_at: '2026-05-07T10:30:00+00:00'
target_cli: cursor
...
---
fix the bug in foo.py — there's a NoneType error around line 42

# replay the same dispatch verbatim later
$ popola dispatch --replay cursor-fix-the-bug-in-foo-py-3a7f9c1d
# → cursor-2a8e3f4c5d6e (new task_id, same payload)

# inspect / list / archive (no daemon required)
$ popola handoff list
$ popola handoff show cursor-fix-the-bug-in-foo-py-3a7f9c1d --json | jq .prompt
$ popola handoff archive cursor-fix-the-bug-in-foo-py-3a7f9c1d cursor-1f0a2b8d4e5c
```

DEMO.md also now has a `## v0.8.1 web design (NEW)` section documenting the NieR-Popola visual system, bilingual switcher, and day/night toggle, so first-time visitors land on a current-state snapshot instead of v0.7.0.

## What's NEW · 4 UX polish features

### 1. Copy-to-clipboard buttons on every `<pre>`

Hover any code block on the site → top-right `⎘` button fades in. Click → copies via `navigator.clipboard.writeText`, shows `✓` for 500 ms then reverts. Failure (clipboard denied / no permission) shows `✗` + `console.error` (No Silent Failures).

### 2. Anchor permalinks on every `h2[id]` / `h3[id]`

Hover any section heading → `¶` glyph fades in next to it. Click → URL hash updates + smooth scroll (Stage A's `scroll-behavior: smooth` on `<html>`). Kramdown auto-generates the IDs; we just decorate them.

### 3. EN-only honest disclosure toast

When a user toggles to **zh** on `QUICKSTART.html` / `USER_GUIDE.html` / `DEMO.html` (which have ≤ 5 `data-i18n` hooks — chrome only), an `aria-live="polite"` toast appears bottom-center:

> **本页面暂仅有英文版 — 仅 header / footer / 着陆页支持中文。**

`sessionStorage["popola.notice.dismissed.en_only"]` flag prevents spam: once dismissed via `✕`, no more toasts in the session. The toast itself carries `data-i18n="notice.en_only"` so it retranslates if the user toggles back to en while it's still up.

This replaces v0.8.1's silent failure (lang-toggle did nothing on doc pages) with explicit disclosure of the bilingual coverage scope. Deeper bilingual coverage of QUICKSTART/USER_GUIDE/DEMO is on the BL-UI follow-up backlog.

### 4. Refined Popola SVG mark + favicon

| Before (v0.8.1) | After (v0.8.2) |
|---|---|
| 2 concentric rings + 1 vertical hairline (3 elements) | Outer ring + inscribed diamond + 4 cardinal ticks + center dot (7 elements, compass / oracle motif) |

The favicon uses the same geometric language stripped to 3 elements (ring + diamond + center dot) for 32×32 legibility. All `currentColor`, all rendered via CSS color, zero NieR-Automata copyright risk (style inspired, not copied).

## Files changed (v0.8.2)

| Slice | Files |
|---|---|
| Stage A — Content rot fix (commit `ddab915`) | `docs/QUICKSTART.md` (-3/+4), `docs/USER_GUIDE.md` (-2/+3), `docs/DEMO.md` (-5/+102) |
| Stage B — UX polish (commit `32ee843`) | `docs/assets/js/extras.js` (NEW, 113 lines), `docs/assets/js/i18n.js` (+70), `docs/assets/css/nier-popola.css` (+112), `docs/_layouts/default.html` (+1), `docs/_includes/popola-mark.svg` (REWRITE, 7 elements), `docs/assets/img/favicon.svg` (REWRITE, 3 elements), `docs/assets/i18n/{en,zh}.json` (+1 key each) |
| Bump | `pyproject.toml`, `src/popolaloom/__init__.py`, SKILL.md (×2), `.popola-loom-version`, `docs/_config.yml`, `tests/test_smoke.py`, `CHANGELOG.md`, `RELEASE_NOTES.md` |

## Stats

- **0 source-code changes** (no `src/popolaloom/**` touched, no test semantics changed beyond version-string assertions)
- **11 docs files** changed across `docs/` (3 content + 8 UX)
- 1597 default-lane tests still green; coverage 94.42% unchanged
- i18n key parity: 38 ≡ 38 (was 37 ≡ 37 in v0.8.1; +1 for `notice.en_only`)
- Vanilla JS, no new dependencies

## Status

| Capability | Status |
|---|---|
| ALL v0.8.1 capabilities | unchanged ✓ |
| Content correctness across QUICKSTART/USER_GUIDE/DEMO | ✓ (v0.8.x cohort, no v0.7.0 cruft) |
| `DEMO.md` v0.8.0 hands-off envelope walkthrough | new ✓ |
| `DEMO.md` v0.8.1 web design walkthrough | new ✓ |
| Copy-to-clipboard on `<pre>` blocks | new ✓ |
| Anchor permalinks on `h2[id]` / `h3[id]` | new ✓ |
| EN-only honest disclosure toast | new ✓ |
| Refined Popola SVG mark + favicon | new ✓ |
| Bilingual coverage of QUICKSTART/USER_GUIDE/DEMO | deferred → BL-UI follow-up (now honestly disclosed) |
| 1597 default-lane tests / 94.42% coverage | ✓ unchanged |

## Upgrade notes

- **No breaking changes**: `pip install -U popolaloom` and the CLI / Python API work exactly as in v0.8.1.
- The GitHub Pages site updates automatically on merge — visit <https://yorha-agents.github.io/PopolaLoom/> after the deploy completes (1–3 min after PR merge). Hard-refresh (`Cmd-Shift-R` / `Ctrl-F5`) to bypass the CDN cache for CSS + JS.
- New localStorage / sessionStorage keys claimed: only `sessionStorage["popola.notice.dismissed.en_only"]`. localStorage `popola.lang` and `popola.theme` from v0.8.1 unchanged.
- All inline code blocks in QUICKSTART/USER_GUIDE/DEMO can now be one-click-copied directly from the rendered page.

## Branch / PR

Branch: `feat/v0.8.2-docs-content-rot-fix` → squash-merged to `main`. Per "Protected Branch Workflow", branch was NOT pushed directly to main; merge happened via PR with squash.

Suggested PR title: `release: v0.8.2 — docs UX overhaul (content rot + UX polish)`.
