> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.9.5 — Init-time Cursor API key intake

<!-- updated: 2026-05-10 -->

> Released: 2026-05-10

> **How to install v0.9.5** (Q-D-5 偏离默认 carries forward; PyPI promotion remains tracked as `BL-v0.9.x-PyPI`):
>
> ```bash
> pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.5
> pip install 'popolaloom[credentials] @ git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.5'
> ./install.sh install --from=git
> ```

## Theme

v0.9.5 is a strictly additive patch on top of v0.9.4 that closes [`./.local/feedbacks/feedback_for_v0.9.4.md`](.local/feedbacks/feedback_for_v0.9.4.md): if the operator hands `popola init` a Cursor API key on the way in, PopolaLoom forwards the value to the existing v0.9.2 OS-keyring resolver so they never have to re-enter it. The flag composes with every init path so no second invocation is needed.

## Highlights

- **`popola init --cursor-api-key VAL`** — non-interactive intake. The literal value is forwarded to [`popolaloom.credentials.store_cursor_api_key`](src/popolaloom/credentials.py); the API key never appears in stdout / stderr (only the SHA-256 fingerprint).
- **`popola init --cursor-api-key-file PATH`** — read the first non-empty line of `PATH` (utf-8) and persist it the same way. Mutually exclusive with `--cursor-api-key`. Missing or empty files are rejected (No Silent Failures).
- **`--configure-cursor-auth` works on every init path** — auto-detect, verb subcommand (`cursor` / `claude` / `copilot` / `codex` / `local` / `all`), `--target=cloud-only`, `--interactive`. Passing `--cursor-api-key` / `--cursor-api-key-file` implies `--configure-cursor-auth`. The verb subcommand path runs the helper AFTER the verb body returns via a click `ctx.call_on_close` hook so install + credential persistence stay in lockstep.
- **`--dry-run` short-circuits credential persistence** with a clear one-line skip message. Per the workspace No-Silent-Failures rule for secrets, the helper never prompts and never persists during a preview. Operators see exactly why the credential step was elided.
- **Best-effort when `popolaloom[credentials]` is missing** — the helper prints an actionable hint pointing at the extra and the `CURSOR_API_KEY` env-var fallback, then returns without exiting non-zero. The install path itself still succeeds; only credential persistence is degraded.

## Test surface

Local verification before PR:

```bash
python -m pytest tests/cli/test_init_credential_intake.py tests/cli/test_init_configure_cursor_auth.py tests/cli/test_init_cmd.py tests/cli/test_init_cmd_edge_cases.py tests/cli/test_init_paths.py tests/cli/test_init_interactive.py tests/cli/test_init_cloud_only.py tests/test_smoke.py tests/docs/test_docs_contract.py tests/cli/test_skill_md_canonical.py tests/docs/test_release_notes_callout.py
ruff check src/popolaloom tests/
mypy src/popolaloom
git diff --check
pytest -m "not slow and not nightly and not real_cli and not real_lark" --cov=popolaloom --cov-report=term-missing --cov-report=xml:coverage-local.xml
rm coverage-local.xml
```

Results (focused subset): 118 passed, 2 skipped (the 2 skips are the v0.8.8 Q-C-4 callout lints that auto-skip post-overwrite per `tests/docs/test_release_notes_callout.py` — see CHANGELOG `[0.8.8]` for the historical record). Default-lane coverage holds the v0.9.4 floor at ≥94%; `ruff check src/popolaloom tests/` clean, `mypy src/popolaloom` clean, `git diff --check` clean.

## Companion docs

- [`CHANGELOG.md`](CHANGELOG.md) §[0.9.5]
- [`README.md`](README.md) current release banner
- [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) §"Credentials & secure storage" — new v0.9.5 init-time intake subsection
- [`docs/API_STABILITY.md`](docs/API_STABILITY.md) v0.9.x stable surface — v0.9.5 init-time credential intake flags

## Known limitations

- PyPI publish remains deferred (`BL-v0.9.x-PyPI`); use the GitHub tag-pinned install commands above.
- Single-tenant keyring slot still applies (one Cursor API key per machine, service `popolaloom.cursor` / username `default`); use the `CURSOR_API_KEY` env-var override to switch personal vs service-account contexts (unchanged from v0.9.2).
- Best-effort credential persistence when `popolaloom[credentials]` is missing: the install path still succeeds, but the helper prints a hint pointing at the extra and the env-var fallback rather than persisting the key (intentional — v0.9.5 keeps the install path additive).
