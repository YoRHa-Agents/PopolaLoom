> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.8.3 — docs/web remediation patch

> Released: 2026-05-07
> Theme: builds on v0.8.2 by fixing the docs i18n flat-key lookup, shipping localized zh routes for the main docs pages, refreshing stale demo and status copy, and adding fast docs contract tests so the same regressions are caught automatically next time. Also tightens CI: adds PyYAML stubs to the dev extras and tightens schema-version typing so strict mypy passes.

## Why v0.8.3 right after v0.8.2?

Direct user feedback in `feedback_for_v0.8.2.md`:

1. Pages still showed placeholder-feeling fields and stale `v0.8.1` copy.
2. Chinese/English switching did not actually translate the main docs pages.
3. The site did not foreground design thinking and the implementation plan.
4. There was no clear demo page.

v0.8.3 closes all four items as a docs-only patch (plus a small CI typing fix that surfaced when CI ran the new lint expectations).

## Highlights

### 1. zh/en route switching that actually works

`docs/zh/QUICKSTART.md`, `docs/zh/USER_GUIDE.md`, and `docs/zh/DEMO.md` ship as Chinese counterparts of the main docs. Each page declares `lang` and `translation_url` in its front matter. The layout exposes those via `<html lang>` + `data-page-lang` + `data-translation-url`, and `i18n.js` now navigates between paired routes when the user toggles, instead of leaving them on an English page with the EN-only toast.

### 2. Flat-key lookup fix

`lookup()` in `i18n.js` now matches the flat dotted keys (`hero.title`, `nav.quickstart`, etc.) directly against the dictionaries, so the existing landing-page translations actually render instead of falling back to raw key text.

### 3. Demo page rewrite + design strip

`docs/DEMO.md` is reshaped into a product walkthrough — what the demo proves, a five-minute path, a design and implementation flow diagram, the hands-off envelope walkthrough, and the HITL walkthrough — with the older release content preserved in a "Historical appendix". `docs/index.md` gains a "Design in one picture" feature grid so the home page foregrounds design rationale.

### 4. Docs contract tests

`tests/docs/test_docs_contract.py` adds fast pytest checks that catch this class of regression in the default lane:

- package `__version__` matches `docs/_config.yml` `popola_version`
- `en.json` and `zh.json` cover every landing-page `data-i18n` key with parity
- `i18n.js` keeps the flat-key lookup and localized-route behavior
- `docs/zh/{QUICKSTART,USER_GUIDE,DEMO}.md` exist with paired front matter
- `docs/index.md` and the header still link to `DEMO.html`
- the primary user-facing docs contain no stale placeholder markers

### 5. CI tightening

The PR that fixed the docs surfaced two strict-mode mypy issues. v0.8.3 ships them as part of the same release:

- `pyproject.toml` adds `types-PyYAML` to the dev extras.
- `FEEDBACK_SCHEMA_VERSION` is typed `Final[Literal["1"]]`.
- An obsolete `# type: ignore[import-untyped]` on the lazy YAML import in `gate/automerge.py` is removed.

## Files changed (v0.8.3)

| Slice | Files |
|---|---|
| i18n + layout | `docs/assets/js/i18n.js`, `docs/_layouts/default.html`, `docs/_includes/header.html`, `docs/_includes/footer.html` |
| Localized zh routes | `docs/zh/QUICKSTART.md`, `docs/zh/USER_GUIDE.md`, `docs/zh/DEMO.md` |
| Content refresh | `README.md`, `docs/index.md`, `docs/QUICKSTART.md`, `docs/USER_GUIDE.md`, `docs/DEMO.md`, `docs/assets/i18n/en.json`, `docs/assets/i18n/zh.json`, `docs/assets/js/theme.js`, `docs/assets/js/extras.js`, `docs/assets/css/nier-popola.css` |
| Tests | `tests/docs/test_docs_contract.py` (NEW) |
| CI lint fix | `pyproject.toml`, `src/popolaloom/handoff/feedback.py`, `src/popolaloom/gate/automerge.py` |
| Bump | `pyproject.toml`, `src/popolaloom/__init__.py`, SKILL.md (×2), `.popola-loom-version`, `docs/_config.yml`, `tests/test_smoke.py`, `CHANGELOG.md`, `RELEASE_NOTES.md` |

## Verification

- `pytest tests/docs/test_docs_contract.py tests/matrix/tier5/test_quickstart_smoke.py -q` → 12 passed
- `ruff check src/popolaloom tests/` → clean
- `mypy src/popolaloom` → clean

## Status

| Capability | Status |
|---|---|
| ALL v0.8.2 capabilities | unchanged |
| zh/en switching across main docs pages | OK live |
| Localized zh docs routes (`/zh/QUICKSTART`, `/zh/USER_GUIDE`, `/zh/DEMO`) | OK live |
| Design strip on landing page | OK live |
| Demo page reshape with design + implementation flow | OK live |
| Docs contract tests in default pytest lane | OK live |
| CI strict mypy passing on the lint job | OK live |

## Upgrade notes

- **No breaking changes**: `pip install -U popolaloom`, the CLI, and the Python API behave exactly as in v0.8.2.
- The GitHub Pages site updates automatically on merge — visit <https://yorha-agents.github.io/PopolaLoom/> after the deploy completes (1–3 min after release PR merge). Hard-refresh to bypass the CDN cache for CSS / JS.
- No new persistent storage keys claimed; existing `popola.lang` and `popola.theme` localStorage keys are unchanged.

## Branch / PR

- PR #9 (`Fix v0.8.2 docs site remediation`) landed the docs/site/test changes and the CI typing fix into `main` via squash-merge.
- The v0.8.3 release PR ships the version bump, refreshed copy, CHANGELOG, and these RELEASE_NOTES.

Suggested release PR title: `release: v0.8.3 — docs/web remediation patch`.
