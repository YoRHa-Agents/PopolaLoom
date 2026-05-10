> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.9.8 — Documentation refresh + interactive `/demo-page` + canonical "Core Design Ideas" chapter

<!-- updated: 2026-05-10 -->

> Released: 2026-05-10

> **How to install v0.9.8** (Q-D-5 偏离默认 carries forward; PyPI promotion remains tracked as `BL-v0.9.x-PyPI`):
>
> ```bash
> ./install.sh install                                                    # canonical (default --from=git, tracks main)
> ./install.sh install --ref=v0.9.8                                       # canonical tag-pinned (recommended for v0.9.8)
> ./install.sh install --with-credentials                                 # also installs the OS-keyring extra (v0.9.7+)
> ./install.sh install --ref=v0.9.8 --with-credentials                    # tag-pinned + keyring extra in one shot
> pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.8       # manual fallback
> pip install 'popolaloom[credentials] @ git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.8'   # manual fallback w/ extra
> ```

## Theme

v0.9.8 is a **strictly additive docs patch** on top of v0.9.7 that aligns the public-facing surface with the actual v0.9.7 feature set, ships a brand-new interactive `/demo-page`, and lands a canonical seven-section `/design-ideas` chapter that an outside reader can land on first when they ask "why does this project exist + what is it not". Closes the documentation half of [`./.local/feedbacks/feedback_for_v0.9.7.md`](.local/feedbacks/feedback_for_v0.9.7.md) §1c (the `runtime=local` / Cursor-Dashboard visibility gap is now cross-referenced from the new `/design-ideas` Sidecar Daemon section + the README + USER_GUIDE local-dispatch sub-section). The other five feedback items (stdout-buffering observability gap, `popola status` ↔ supervisor state-machine drift, Cursor REST GitHub-App misclassification, `popola cloud worker dispatch` schema reject under personal API key, orphan Node.js worker on stop) require source-code surgery and are tracked for v0.9.9 / v0.10.0.

No `src/popolaloom/**` logic changed beyond the canonical version-bump lockstep (`__version__`, `pyproject.toml`, two `SKILL.md` frontmatters, two `.popola-loom-version` markers) enforced by `tests/cli/test_skill_md_canonical.py::test_skill_md_version_matches_package`.

## Highlights

- **`docs/design-ideas.md`** (NEW; `/design-ideas.html` route) — exactly **7 H2 sections** walking PopolaLoom's seven architectural choices: (1) The Loom Metaphor (织机), (2) Daemon-as-Sidecar (`popolad`), (3) File-Backed Handoff, (4) 5-Channel HITL Fanout, (5) Vendoring Philosophy, (6) Skill = Auto-Discovery Contract, (7) GA Stability Boundary (v0.9.0+). Each section closes with a `> See: <code reference> + <docs reference>` blockquote so a reviewer can drill in. Idiomatic Chinese mirror at `docs/zh/design-ideas.md`.
- **`docs/demo-page.md`** (NEW; `/demo-page.html` route) — interactive scenario-picker with 6 cards (Local single-CLI / Cross-CLI handoff / HITL pause / Cloud Agent dispatch / Self-hosted worker handoff / Cross-PR relay), each linking to a body `<section>` with a `.terminal-block`-styled `<pre>` containing the verbatim v0.9.8 popola CLI commands a user would type to reproduce the scenario. Idiomatic Chinese mirror, identical CLI commands.
- **Jekyll site facelift** — new `.scenario-grid` + `.scenario-card` + `.terminal-block` + `@keyframes caret-blink` CSS rulesets in `docs/assets/css/nier-popola.css`; new "Design" primary-nav entry in `docs/_includes/header.html`; `docs/index.md` `feature-grid` lifted from 6 → 7 cards (replaced `Hands-off envelope` with `Cloud + Self-hosted worker (v0.8.5–v0.9.3)` and added `Secure credential storage (v0.9.2+)`); footer fallback version bumped from `v0.8.4` → `v0.9.8`; new `nav.design` + `feature.cloud.*` + `feature.credentials.*` keys mirrored across `docs/assets/i18n/{en,zh}.json` + `docs/assets/js/i18n.js`.
- **README + Quickstart + User Guide + Demo refreshed to the v0.9.7 surface** — README's 5-minute Quickstart code block now shows `popola auth cursor set --validate` (v0.9.2+) and `popola cloud worker start --worker-dir "$(pwd)"` (v0.9.1+) as next-step bullets; QUICKSTART gets a brand-new **Step 1.5 — (optional) configure your Cursor API key** sub-section pointing at `popola auth cursor set --validate` as the recommended path and the `export CURSOR_API_KEY=...` shell-export as the headless-container fallback; USER_GUIDE adds a new **`popola init` Interactive Intake (v0.9.5+)** TOC entry + body section between Credentials & secure storage and Self-hosted worker handoff. The existing Cloud HITL / Multi-run cloud agents / Cross-PR relay sections are untouched (stable v0.9.0 GA).
- **Both Chinese mirrors are real rewrites**, not machine translations — uses 设计哲学 / 织机 / 旁路 / 信封 / 五通道 idiomatically, mirrors the EN front-matter `lang: zh` + `translation_url: /<en-path>.html` contract, every modified zh file has the `<!-- updated: 2026-05-10 -->` HTML comment near the top.

## Test surface

Local verification before PR:

```bash
python -m pytest tests/cli/test_skill_md_canonical.py tests/docs/test_docs_contract.py \
    tests/docs/test_release_notes_callout.py tests/test_smoke.py -q
ruff check src/popolaloom tests/
git diff --check
git ls-files docs/*.md docs/zh/*.md | xargs -I{} grep -l '^---$' {} | wc -l   # every doc page has YAML front-matter
grep -rn 'v0\.8\.4' docs/    # zero non-historical matches
grep -c '^## ' docs/design-ideas.md   # exactly 7
```

Default-lane CI (4 matrix jobs: ubuntu-22.04 / py3.11, ubuntu-22.04 / py3.12, ubuntu-24.04 / py3.11, ubuntu-24.04 / py3.12): all SUCCESS on the docs-only commits; re-runs after the version-bump release commit are expected to stay green because no test logic changed.

## Companion docs

- [`CHANGELOG.md`](CHANGELOG.md) §[0.9.8] — full diff matrix
- [`README.md`](README.md) — current-release banner + new "Core design ideas at a glance" subsection
- [`docs/QUICKSTART.md`](docs/QUICKSTART.md) — Step 1 install recipe + Step 1.5 API key intake
- [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) — `popola init` Interactive Intake (v0.9.5+) section
- [`docs/demo-page.md`](docs/demo-page.md) — interactive 6-scenario walkthrough
- [`docs/design-ideas.md`](docs/design-ideas.md) — 7-section design philosophy chapter
- [`docs/zh/demo-page.md`](docs/zh/demo-page.md), [`docs/zh/design-ideas.md`](docs/zh/design-ideas.md) — Chinese mirrors

## Known limitations

- **Five `feedback_for_v0.9.7.md` items remain open** (tracked for v0.9.9 / v0.10.0):
  1. **stdout-buffering observability gap** — `cursor-agent agent --print --output-format text` buffers stdout when stdout is a PIPE, so `popola attach --follow` can show 0 events for 10+ minutes on long tasks. The fix needs supervisor-side change (default to `stream-json`, or surface a `process.note` warning, or attach a pty).
  2. **`popola status` ↔ supervisor state-machine drift** — `popola status` can report `state=running, exit_code=null` for ~10 seconds after the OS has reaped the pid. Needs an active `kill -0` probe in the status path.
  3. **Cursor REST 400 "branch not found" misclassification** — when the Cursor GitHub App is missing, Cursor REST returns a misleading "Failed to verify existence of branch 'main'" error rather than the GitHub-App-missing 422; needs `_ERROR_CATALOG` regex match + dedicated `CursorCloudGithubAppMissingError` subclass.
  4. **`popola cloud worker dispatch` schema reject under personal API key** — the v0.9.x worker dispatcher injects `usePrivateWorker` + `labels` keys that Cursor REST rejects under personal API key + My Machines mode; needs key-classification gate + alternative routing or explicit "use `popola dispatch --cli=cursor` instead" failure mode.
  5. **Orphan Node.js worker on `popola cloud worker start` stop** — `kill <wrapper-pid>` does not cascade to the underlying `agent worker start` Node.js child; needs `os.setsid()` + `killpg()` in the supervisor.
- **PyPI publish remains deferred** (`BL-v0.9.x-PyPI`); use the GitHub tag-pinned install commands above. The default install no longer needs PyPI. Operators who specifically need PyPI can opt in via `--from=pypi --version=0.9.x` once the promotion patch lands.
- v0.9.7's `--with-credentials` install flag carries over byte-for-byte; the single-tenant keyring slot still applies (one Cursor API key per machine, service `popolaloom.cursor` / username `default`); use the `CURSOR_API_KEY` env-var override to switch personal vs service-account contexts (unchanged from v0.9.2).
