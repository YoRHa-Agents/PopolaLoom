> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.9.4 — Actions validation hotfix

<!-- updated: 2026-05-10 -->

> Released: 2026-05-10

> **How to install v0.9.4** (Q-D-5 偏离默认 carries forward; PyPI promotion remains tracked as `BL-v0.9.x-PyPI`):
>
> ```bash
> pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.4
> pip install 'popolaloom[credentials] @ git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.4'
> ./install.sh install --from=git
> ```

## Theme

v0.9.4 is a CI hotfix for the v0.9.3 workspace-worker routing release. It preserves the user-facing v0.9.3 features and fixes the optional cloud workflows so GitHub Actions no longer reports workflow-file failures when `CURSOR_API_KEY` is absent.

## Highlights

- `cloud-smoke` now checks `CURSOR_API_KEY` inside the smoke step and exits 0 with a clear skip message when the repo secret is not configured.
- `cloud-fixtures-drift-check` uses the same bash-level skip pattern and records `pytest_rc=0` for the no-secret path.
- Package, docs, Skill markers, and release contracts are bumped to `0.9.4`.

## Test surface

Local verification before PR:

```bash
python -m pytest tests/cli/test_cloud_worker_cmd.py tests/test_smoke.py tests/docs/test_docs_contract.py tests/cli/test_skill_md_canonical.py tests/docs/test_release_notes_callout.py
pytest -m "not slow and not nightly and not real_cli and not real_lark" --cov=popolaloom --cov-report=term-missing --cov-report=xml:coverage-local.xml
ruff check src/popolaloom tests/
mypy src/popolaloom
git diff --check
```

Results: focused pytest 81 passed / 2 skipped; default lane 2790 passed / 25 skipped / 82 deselected with coverage 94.08%; ruff clean; mypy clean; diff check clean.

## Companion docs

- [`CHANGELOG.md`](CHANGELOG.md) §[0.9.4]
- [`README.md`](README.md) current release banner
- [`docs/API_STABILITY.md`](docs/API_STABILITY.md) v0.9.x stable surface

## Known limitations

- PyPI publish remains deferred (`BL-v0.9.x-PyPI`); use the GitHub tag-pinned install commands above.
- Real Cursor Cloud smoke remains opt-in and requires the repository `CURSOR_API_KEY` secret; without it, the workflow intentionally skips without a red X.
