> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.9.6 — Install.sh default no longer requires PyPI

<!-- updated: 2026-05-10 -->

> Released: 2026-05-10

> **How to install v0.9.6** (Q-D-5 偏离默认 carries forward; PyPI promotion remains tracked as `BL-v0.9.x-PyPI`):
>
> ```bash
> ./install.sh install                                                # canonical (default --from=git, tracks main)
> ./install.sh install --ref=v0.9.6                                   # canonical tag-pinned (recommended for v0.9.6)
> pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.6   # manual fallback
> ```

## Theme

v0.9.6 is a strictly additive patch on top of v0.9.5 that closes [`./.local/feedbacks/feedback_for_v0.9.4.md`](.local/feedbacks/feedback_for_v0.9.4.md) lines 2-5: the official installer (`./install.sh`) used to default to `pip install popolaloom`, but PyPI publish remains intentionally deferred for the v0.9.x line (Q-D-5 偏离默认 / `BL-v0.9.x-PyPI`), so operators on Chinese pip mirrors hit `404 popolaloom` and the canonical install path silently failed. v0.9.6 fixes that with a default flip and adds a new tag-pin flag so a fresh `./install.sh install` works without PyPI.

## Highlights

- **Default install source flipped from PyPI to GitHub** — `./install.sh install` now defaults to `--from=git` (tracks `main`) instead of `--from=pypi`. A fresh bootstrap on a Chinese pip mirror that doesn't carry `popolaloom` yet succeeds end-to-end. The 404 surface that broke the v0.9.x install path is exercised in the default lane (`tests/cli/test_install_script.py::test_install_script_install_default_uses_git_source` pins the new default so a future regression that flips it back to `pypi` fails fast).
- **New `--ref=<tag|branch|sha>` flag** — append `@<ref>` to `git+https://github.com/YoRHa-Agents/PopolaLoom.git` so `./install.sh install --ref=v0.9.6` is the canonical tag-pinned recipe. Mirror of `--version=X.Y.Z` for the `--from=pypi` path; `--ref` requires `--from=git` and is forbidden for the `uninstall` verb (matches the existing `--version` semantics). Per the workspace No-Silent-Failures rule, contradictory inputs (`--ref` with `--from=pypi`, `--ref` with a local path source, `--ref` on `uninstall`) fail loudly with a clear error.
- **`POPOLA_INSTALL_SCRIPT_VERSION` 0.8.4 → 0.9.6** — bash bootstrap surface change advertised explicitly so operators know which behavior they're getting from `install.sh version`. `./install.sh --help` documents the new default for `--from`, the new `--ref` flag, and the `install --ref=v0.9.6` plus `install --from=pypi --version=0.9.6` examples.
- **PyPI fallback preserved** — `./install.sh install --from=pypi --version=0.9.6` keeps working for operators who specifically need PyPI; the path will become live once `BL-v0.9.x-PyPI` lands. Until then `--from=pypi` (with or without `--version=`) resolves to the prior v0.8.x stable line.
- **`verb_install` log line transparency** — the install banner now prints `from=${FROM} ref=${REF:-(none)}` so the resolved install spec is visible (debug parity with how `--version` is already surfaced).

## Test surface

Local verification before PR:

```bash
python -m pytest tests/cli/test_install_script.py tests/test_smoke.py tests/docs/test_docs_contract.py tests/cli/test_skill_md_canonical.py tests/docs/test_release_notes_callout.py
ruff check src/popolaloom tests/
mypy src/popolaloom
git diff --check
bash -n install.sh
./install.sh --help | head -40
./install.sh install --dry-run --no-daemon --no-skills
./install.sh install --dry-run --no-daemon --no-skills --ref=v0.9.6
./install.sh install --dry-run --no-daemon --no-skills --from=pypi --version=0.9.6
pytest -m "not slow and not nightly and not real_cli and not real_lark" --cov=popolaloom --cov-report=term-missing --cov-fail-under=94 -q
rm -f coverage-local.xml coverage.json .coverage*
```

`tests/cli/test_install_script.py` lifts from 13 → 16 cases (3 new + 2 modified to assert the new git default; the `--help` smoke also asserts `--ref` appears in the rendered usage matrix). Default-lane coverage holds the v0.9.5 floor at ≥94%; `ruff check src/popolaloom tests/` clean, `mypy src/popolaloom` clean, `git diff --check` clean.

## Companion docs

- [`CHANGELOG.md`](CHANGELOG.md) §[0.9.6]
- [`README.md`](README.md) current release banner + v0.9.6 highlights section + install commands re-ordered (canonical `./install.sh install` first, tag-pinned `./install.sh install --ref=v0.9.6` second, manual `pip install git+...@v0.9.6` third)
- [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) §"`install.sh` — bash bootstrap installer" — flag matrix + `--from=` source resolution table updated for the new git default and the new `--ref` flag
- [`docs/API_STABILITY.md`](docs/API_STABILITY.md) §2 stable surfaces — v0.9.6 install.sh default flip + `--ref` flag join the v0.9.x stable surface

## Known limitations

- PyPI publish remains deferred (`BL-v0.9.x-PyPI`); use the GitHub tag-pinned install commands above. The default install no longer needs PyPI, so the impact is minimal — operators who explicitly need PyPI can opt in via `--from=pypi --version=0.9.x` once the promotion patch lands.
- `--ref` accepts arbitrary git refs (branches, SHAs, and tags all work because `pip install git+...@<ref>` resolves them the same way). Operators MUST verify they used the intended ref. The install banner now prints `from=git ref=<value>` so the resolved spec is visible. v0.9.6 does not gate `--ref` to the tag namespace because that would prevent the `--ref=main` workflow some operators use during pre-release verification.
- v0.9.5's init-time Cursor API key intake (`popola init --cursor-api-key VAL` / `--cursor-api-key-file PATH`) carries over byte-for-byte; the single-tenant keyring slot still applies (one Cursor API key per machine, service `popolaloom.cursor` / username `default`); use the `CURSOR_API_KEY` env-var override to switch personal vs service-account contexts (unchanged from v0.9.2).
