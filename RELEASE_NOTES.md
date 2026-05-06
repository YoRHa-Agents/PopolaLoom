> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom Unreleased — Skill identifier rename

> Status: pending release on the `feat/skill-rename-popola-loom` branch
> Theme: align the user-facing Skill identifier with the PopolaLoom
> brand orthography. **No source-code logic changes; no Python API
> changes.**

## Summary

The user-facing Skill identifier is renamed from **`popolaloom`** to
**`popola-loom`**. The Python package name `popolaloom` is unchanged
(`pip install popolaloom`, `import popolaloom`,
`popolaloom._vendored.arktower`, etc. all keep working). Concretely:

- Wheel-bundled Skill directory:
  `src/popolaloom/skills/popolaloom/` → `src/popolaloom/skills/popola-loom/`
- SKILL.md frontmatter: `name: popolaloom` → `name: popola-loom`
- Version marker filename: `.popolaloom-version` → `.popola-loom-version`
- Every `popola init` / `popola skill install` install path now lands
  at `~/.cursor/skills/popola-loom/`, `~/.claude/skills/popola-loom/`,
  `$CODEX_HOME/skills/popola-loom/` (Copilot stays at the single-file
  `<cwd>/.github/copilot-instructions.md`).
- The `install-popola` Skill keeps its existing legacy trigger
  phrases (`install popolaloom`, `安装 popolaloom`, etc.) so existing
  muscle memory keeps working, and adds new `install popola-loom` /
  `set up popola-loom` / `安装 popola-loom` triggers for the new
  orthography.

Operators upgrading should re-run `popola init` (or
`popola skill upgrade --target=all`) once after upgrading the wheel so
the on-disk SKILL.md lands at the new path. The previous
`~/.cursor/skills/popolaloom/SKILL.md` files can be removed after the
new install lands; `popola doctor` will report the new path's status
under the new directory name.

---

# PopolaLoom v0.7.0 — Docs + install-popola Skill consolidation

> Released: 2026-05-06
> Theme: closes the 4 user-feedback items from v0.6.1
> (`.local/feedbacks/feedback_for_v0.6.1.md`) into a single
> docs + skill polish release. No breaking changes; no new daemon
> primitives, no schema changes, no new dependencies.

## Summary

PopolaLoom v0.7.0 is a documentation + skill-surface consolidation
minor. It deliberately ships zero source-code logic changes (no daemon
primitives, no public Python APIs, no schema migrations) so the entire
release is reviewable as a docs + Skill polish round. Four threads
land in lockstep on `feat/v0.7.0-docs-skill-cleanup`:

1. **`.local/` is now a strictly local-only workspace surface.** The
   directory is gitignored (one-time `git rm --cached -r .local/`
   un-tracks 34 files); on-disk files are preserved by intent so local
   agent workflows that read `.local/feedbacks/` and
   `.local/memory/specs/` keep working unchanged.
2. **Release notes consolidate to a single floating file.** All ten
   per-version `release-notes-v*.md` files (v0.4.0 → v0.6.1) are
   removed; their content is preserved verbatim in `CHANGELOG.md`. The
   new `RELEASE_NOTES.md` is overwritten on every release going
   forward — operators looking for "what changed last release" read
   this file; operators looking for the history archive read
   `CHANGELOG.md`.
3. **Comprehensive docs refresh.** `README.md` is rewritten as a
   polished landing page; new `docs/QUICKSTART.md` (5-minute
   onboarding) + `docs/USER_GUIDE.md` (full reference) land; a
   GitHub Pages-ready Jekyll site under `docs/index.md` +
   `docs/_config.yml` ships scaffolded; `docs/DEMO.md` refreshes to
   the v0.7.0 era. (Wave 3 owns the docs files; this release-prep
   wave owns the version markers + smoke test + this notes file.)
4. **NEW `install-popola` Skill.** A standalone installer-only Skill
   at `src/popolaloom/skills/install-popola/SKILL.md` walks an LLM
   through `pip install popolaloom` → `popola init <ide> --global` →
   `popola popolad start` → `popola doctor`, mirroring the
   conventional `/install-devola-flow` slash-command workflow used to
   install DevolaFlow globally. The Skill ships in the wheel via the
   existing `[tool.hatch.build.targets.wheel] packages =
   ["src/popolaloom"]` recursion (no pyproject change required).

## Closures (4/4)

| # | User feedback (v0.6.1) | Closure |
|---|---|---|
| 1 | `.local/` should be gitignored locally (NOT deleted) | `.gitignore` adds explicit `.local/` ignore + drops the directory from the "DO NOT IGNORE" comment block. `git rm --cached -r .local/` un-tracks 34 files; on-disk files are preserved by intent. |
| 2 | Single floating release notes per version (not one new file each release) | All 10 `release-notes-v*.md` files (v0.4.0 → v0.6.1) deleted. NEW `RELEASE_NOTES.md` at repo root (this file). Historical archive stays in `CHANGELOG.md`. |
| 3 | Comprehensive Readme / UserGuide / Quickstart / GitHub Pages / DEMO refresh | `README.md` rewritten as a polished landing page. NEW `docs/QUICKSTART.md` (5-min onboarding). NEW `docs/USER_GUIDE.md` (full reference). NEW Jekyll site under `docs/index.md` + `docs/_config.yml` (GitHub Pages-ready). `docs/DEMO.md` refreshed to v0.7.0 era. |
| 4 | `install-popola` Skill (mirroring `/install-devola-flow`) | NEW `src/popolaloom/skills/install-popola/` Skill (`SKILL.md` + `.popolaloom-version` + `__init__.py`). Wheel-bundled. Triggers on `install popola` / `/install-popola` / `安装 popolaloom`. Walks pip install + `popola init <ide> --global` + daemon boot + `popola doctor` smoke. |

## Behaviour deltas

- **Operators** — no runtime behaviour change. The `popola` CLI verbs,
  the `popolad` daemon, the MCP tool surface, the LangGraph dispatch
  graph, the HITL renderers, and the Lark bridge are all byte-for-byte
  identical to v0.6.1. The only operator-visible delta is that the
  v0.7.0 `popola version` reports `0.7.0` and the SKILL.md's
  frontmatter `version` field bumps in lockstep.
- **`.local/` policy is local-only** — the directory is gitignored on
  the v0.7.0+ branch but the on-disk files are preserved by intent
  (`.local/feedbacks/`, `.local/memory/specs/`, `.local/eval_reports/`,
  `.local/.agent/` etc. all survive the W1A `git rm --cached`). Local
  agent workflows that read these files keep working; CI / hosted
  runners no longer see them in the git tree.
- **`RELEASE_NOTES.md` is a floating document** — overwritten on every
  release going forward. Operators looking for the v0.6.1 release
  notes (or any earlier version) read the equivalent block in
  `CHANGELOG.md`; the per-version `release-notes-v*.md` files are
  intentionally not coming back.
- **The new `install-popola` Skill is opt-in / additive** — it does
  NOT replace the canonical `popolaloom` Skill (which assumes `popola`
  is already on PATH and the daemon can be started). The host agent
  loads the canonical Skill for runtime task-dispatch flows; the
  installer Skill only triggers on phrases like `install popola` /
  `/install-popola`. Both are tier-1 and discoverable side-by-side via
  `importlib.resources.files('popolaloom') / 'skills' / '<name>' /
  'SKILL.md'`.
- **The canonical `popolaloom` Skill body is unchanged** — only the
  frontmatter `version` (and `last_updated`) bumps. The body content
  is the v0.5.0 baseline that v0.5.x and v0.6.x preserved unchanged;
  v0.7.0 continues that contract. The skill-md canonical regression
  test (`tests/cli/test_skill_md_canonical.py`) keeps the body within
  the documented `[8 000, 16 000]` chars budget.
- **Version is in lockstep across 6 files** — `pyproject.toml`,
  `src/popolaloom/__init__.py`, `src/popolaloom/skills/popolaloom/{SKILL.md,.popolaloom-version}`,
  `src/popolaloom/skills/install-popola/{SKILL.md,.popolaloom-version}`,
  and `tests/test_smoke.py` all assert / declare `0.7.0`.

## Verification commands

```bash
## 1. version (6 files in lockstep)
python -c "import popolaloom; assert popolaloom.__version__ == '0.7.0'"
grep -E '^version = "0.7.0"' pyproject.toml
grep -E '^version: 0.7.0' src/popolaloom/skills/popolaloom/SKILL.md
grep -E '^version: 0.7.0' src/popolaloom/skills/install-popola/SKILL.md
cat src/popolaloom/skills/popolaloom/.popolaloom-version  # 0.7.0
cat src/popolaloom/skills/install-popola/.popolaloom-version  # 0.7.0

## 2. smoke test passes (both tests)
pytest tests/test_smoke.py -v
## ⇒ test_import_and_version PASSED
## ⇒ test_both_skills_resolve_via_importlib PASSED

## 3. .local/ is gitignored
git check-ignore -v .local/feedbacks/

## 4. release-notes consolidated
ls release-notes-v*.md 2>&1 | grep -q "No such file"
wc -l RELEASE_NOTES.md  # >= 120 lines

## 5. install-popola Skill discoverable
python -c "from importlib.resources import files; \
  p = files('popolaloom') / 'skills' / 'install-popola' / 'SKILL.md'; \
  assert p.is_file()"

## 6. default lane stays green at the v0.5.5 floor
pytest -m "not slow and not nightly and not real_cli and not real_lark" \
  --cov=src/popolaloom --cov-fail-under=94

## 7. owned-code lint clean
ruff check src/popolaloom tests/test_smoke.py
```

All seven commands exit 0 on a clean v0.7.0 checkout.

## Owned files (this release)

The release spans three waves on `feat/v0.7.0-docs-skill-cleanup`:

**Wave 1A — `.local/` policy (1 file modified + 34 untracked):**

- `.gitignore` — explicit `.local/` ignore rule + the bottom
  "DO NOT IGNORE" comment block updated to drop `.local/` from the
  tracked-surfaces list.
- `.local/**` (34 files un-tracked via `git rm --cached -r .local/`;
  on-disk files preserved by intent).

**Wave 1B — release-notes consolidation (10 files deleted + 1 created
+ 1 modified):**

- `release-notes-v0.4.0.md` … `release-notes-v0.6.1.md` (10 files,
  ~140 KB total) — DELETED.
- `RELEASE_NOTES.md` — NEW (this file).
- `CHANGELOG.md` — heading paragraph gains the
  `Latest release notes also live at RELEASE_NOTES.md ...` pointer.

**Wave 1C — `install-popola` Skill (2 files NEW):**

The directory name contains a dash (`install-popola`), which is not
a valid Python identifier — by design. The Skill is shipped as wheel
**data** (resolved via
`importlib.resources.files('popolaloom') / 'skills' / 'install-popola'
/ 'SKILL.md'`) rather than as an importable submodule, mirroring how
the canonical Skill's content is consumed (see
`cli/_skill_source.py:canonical_source_path`). No `__init__.py` is
needed for this directory because it is never imported as a package.

- `src/popolaloom/skills/install-popola/SKILL.md` (~165 lines) — the
  Skill body (frontmatter + 7 sections: pre-flight checks → install
  → register per-IDE → boot daemon → verify → upgrade path → common
  errors → after-install).
- `src/popolaloom/skills/install-popola/.popolaloom-version` —
  drift-detection marker (`0.7.0`).

**Wave 2 — version lockstep + CHANGELOG + RELEASE_NOTES + smoke test
(this wave; 9 files):**

- `pyproject.toml` — `[project] version = "0.6.1" → "0.7.0"`.
- `src/popolaloom/__init__.py` — `__version__ = "0.6.1" → "0.7.0"`.
- `src/popolaloom/skills/popolaloom/SKILL.md` — frontmatter
  `version: 0.6.1 → 0.7.0` (body unchanged).
- `src/popolaloom/skills/popolaloom/.popolaloom-version` — `0.7.0`.
- `src/popolaloom/skills/install-popola/SKILL.md` — frontmatter
  `version: 0.6.1 → 0.7.0` + the inline `popola version` example
  (`"popolaloom 0.6.1" → "popolaloom 0.7.0"`) + the verification-
  checklist row (`0.6.1 → 0.7.0`) + the drift-detection paragraph
  bumped.
- `src/popolaloom/skills/install-popola/.popolaloom-version` — `0.7.0`.
- `tests/test_smoke.py` — version assertion bumped to `0.7.0`;
  module docstring gains a v0.7.0 lead paragraph; NEW
  `test_both_skills_resolve_via_importlib` regression guard.
- `CHANGELOG.md` — NEW `[0.7.0]` entry above the existing `[0.6.1]`
  entry (Keep-a-Changelog format with Added / Changed / Removed /
  Released subsections).
- `RELEASE_NOTES.md` — body rewritten (line 1 policy header
  preserved; the rest replaced with this v0.7.0 release note).

**Wave 3 — docs refresh (6 files; not in this wave's scope but listed
for the entry to stay self-contained):**

- `README.md` — full rewrite as a polished landing page with status
  table, 5-minute Quickstart, architecture TL;DR, and pointers to
  `docs/QUICKSTART.md` + `docs/USER_GUIDE.md`.
- `docs/QUICKSTART.md` — NEW 5-minute onboarding (install →
  `popola init` → `popola popolad start` → first `popola dispatch` →
  `popola doctor`).
- `docs/USER_GUIDE.md` — NEW full-reference manual covering every
  `popola` verb, every adapter, every CLI-flag passthrough KEY, the
  Lark notification env vars, and the `popola doctor` 4-subsystem
  audit.
- `docs/index.md` — NEW Jekyll-ready landing page for the GitHub
  Pages site.
- `docs/_config.yml` — NEW Jekyll config (theme + nav + collections).
- `docs/DEMO.md` — refreshed to the v0.7.0 era (the v0.5.x evolution
  walkthrough preserved; new v0.6.0 + v0.6.1 + v0.7.0 sections).

## Smoke-test additions

`tests/test_smoke.py` carries TWO tests in v0.7.0:

- `test_import_and_version` — bumped from `"0.6.1"` to `"0.7.0"`. The
  v0.7.0 lead paragraph at the top of the module docstring documents
  the 4-fix theme for archaeology.
- `test_both_skills_resolve_via_importlib` (NEW) — the v0.7.0
  regression guard. Asserts BOTH the canonical
  `popolaloom/SKILL.md` AND the new `install-popola/SKILL.md` resolve
  via `importlib.resources.files('popolaloom') / 'skills' / '<name>'
  / 'SKILL.md'` and that each one has the expected frontmatter
  (`name:` + `version: 0.7.0`). This locks the wheel-data layout
  against accidental future drops (e.g. someone removing one of the
  Skill directories or changing
  `[tool.hatch.build.targets.wheel] packages` away from
  `["src/popolaloom"]`).

## CHANGELOG / RELEASE_NOTES policy

Going forward (v0.7.0+):

- **`CHANGELOG.md`** is the single historical archive. Every release
  appends a new `## [<version>] — <date>` block at the top, in
  Keep-a-Changelog format. v0.6.1 + earlier history is preserved
  verbatim from the deleted per-version files.
- **`RELEASE_NOTES.md`** is overwritten on every release. The line-1
  policy paragraph stays unchanged across releases; the body is the
  current release's notes only. Operators looking for an older
  release's notes consult `CHANGELOG.md`.
- **Per-version `release-notes-v*.md` files are not coming back.**
  The 10 deleted files (v0.4.0 → v0.6.1) are tracked at
  `/tmp/v07_broken_release_links.txt` (a Wave 3 input) so any links
  in `README.md` / `docs/` that pointed at them get rewritten in
  Wave 3 to point at the equivalent CHANGELOG anchor.

## Known limitations / deferred to v0.7.1

- **`popola init` doesn't yet auto-install the `install-popola`
  Skill.** The new Skill is opt-in (it must be authored into
  `~/.cursor/skills/install-popola/` manually until `popola init`
  learns to install BOTH skills). v0.7.1 will extend `popola init`
  to register the installer Skill alongside the canonical Skill in
  the same idempotent step (so a user running `popola init cursor
  --global` once gets both Skills installed under
  `~/.cursor/skills/{popolaloom,install-popola}/`).
- **The Jekyll site (`docs/index.md` + `docs/_config.yml`) ships
  scaffolded but the GitHub Pages source must be enabled by the
  repo owner** via `Settings → Pages → Source = docs/`. Adding a
  `.github/workflows/pages.yml` for actions-based deployment is
  deferred to v0.7.1 so the v0.7.0 release stays purely additive on
  the docs side.
- **Live `mutmut run` activation** — same v0.6.x carry-over;
  blocked by the src/ + editable install friction documented in
  `evidence/mutmut-baseline.md`. Pinned for v0.7.x.
- **Real Lark Tier-3 test creds** — same v0.6.x carry-over;
  `tests/lark/test_listener_real.py` requires creds the hosted
  runner lacks. Pinned for v0.7.x.

---

**PopolaLoom v0.7.0 ships 2026-05-06.**
The next branch picks up the v0.7.0 known-limitation backlog
(above) on a fresh feature branch off `main` after this lands.
