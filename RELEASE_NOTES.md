> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.9.7 — Drop `pip install` from credential WARN paths; add `install.sh --with-credentials`

<!-- updated: 2026-05-10 -->

> Released: 2026-05-10

> **How to install v0.9.7** (Q-D-5 偏离默认 carries forward; PyPI promotion remains tracked as `BL-v0.9.x-PyPI`):
>
> ```bash
> ./install.sh install                                                    # canonical (default --from=git, tracks main)
> ./install.sh install --ref=v0.9.7                                       # canonical tag-pinned (recommended for v0.9.7)
> ./install.sh install --with-credentials                                 # NEW v0.9.7 — also installs the OS-keyring extra
> ./install.sh install --ref=v0.9.7 --with-credentials                    # tag-pinned + keyring extra in one shot
> pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.7       # manual fallback
> pip install 'popolaloom[credentials] @ git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.7'   # manual fallback w/ extra
> ```

## Theme

v0.9.7 is a strictly additive patch on top of v0.9.6 that closes [`./.local/feedbacks/feedback_for_v0.9.4.md`](.local/feedbacks/feedback_for_v0.9.4.md) line 1 ("popola 不使用 pip 修正安装方式" + "init 阶段给出，本地需要能存储并加密"). Four production WARN / error paths used to recommend `pip install popolaloom[credentials]` whenever the OS keyring extra was missing — conflicting with the workspace rule that says PopolaLoom should not surface raw `pip install` commands to operators. v0.9.7 introduces `./install.sh install --with-credentials` (rolls the optional `keyring>=25` extra into the same install) and rewrites every production WARN / error path to point at it. Headless containers without a SecretService backend get an explicit fallback hint to set `CURSOR_API_KEY` in a `0o600` `.env` file (`credentials.py` precedence #2).

## Highlights

- **`./install.sh install --with-credentials`** (NEW; v0.9.7) — opt-in flag that appends the optional `[credentials]` extra (Python `keyring>=25`) to the resolved install spec. Composes with all three `--from` modes: PyPI emits `popolaloom[credentials]` / `popolaloom[credentials]==X.Y.Z`; git emits `popolaloom[credentials] @ git+https://github.com/YoRHa-Agents/PopolaLoom.git[@<ref>]` (PEP 508); local path emits `popolaloom[credentials] @ <PATH>` (PEP 508). `--with-credentials` is rejected on `uninstall` (loud error per **No Silent Failures**, mirrors the existing `--ref` / `--version` semantics). Also accepted by `update`. New `WITH_CREDENTIALS=0` global, new `--with-credentials` arm in `parse_flag`, new validator clause in `validate_args`, refreshed `usage()` block + Examples lines, install / update banner now reports `with_credentials=${WITH_CREDENTIALS}`.
- **`POPOLA_INSTALL_SCRIPT_VERSION` 0.9.6 → 0.9.7** — bash bootstrap surface change advertised explicitly so operators know which behavior they're getting from `./install.sh version`.
- **WARN / error text in four production paths now drops `pip install popolaloom[credentials]`** — `popolaloom.credentials._keyring_set` (`CredentialBackendError` raised from `popola auth cursor set` / init-time persistence), `popolaloom.cli.init_cmd._persist_cursor_api_key_noninteractive` (the WARN operators hit when running `popola init --cursor-api-key-file <path>` on a host without a keyring backend), `popolaloom.cli.init_cmd._offer_cursor_credential_setup` (the interactive `popola init --target=cloud-only --configure-cursor-auth` walkthrough), and `popolaloom.cli.auth_cmd._fail_no_keyring` (called from `popola auth cursor {set,clear,status --json}` when the extra is missing). Every replacement points operators at `./install.sh install --with-credentials` AND surfaces the `CURSOR_API_KEY` env / 0o600 `.env` fallback (precedence #2 per `credentials.py`). Headless Linux containers without a SecretService backend get an explicit "the install path succeeds but the keyring lookup still misses" sentence.
- **Five test files updated** to assert the new invariants — every changed WARN test now requires `./install.sh install --with-credentials` in the message AND asserts both `pip install` and `popolaloom[credentials]` are absent from user-facing error / WARN text. Six new `tests/cli/test_install_script.py` cases pin the new flag's behavior across all `--from` modes (PyPI / PyPI+`--version` / git default / git+`--ref` / `update` / `uninstall` loud-fail) plus a regression test that the **default install MUST omit the extras** so the surface stays additive.

## Test surface

Local verification before PR:

```bash
python -m pytest tests/cli/test_install_script.py tests/cli/test_init_credential_intake.py \
    tests/cli/test_init_configure_cursor_auth.py tests/cli/test_auth_cmd.py \
    tests/test_credentials.py tests/test_smoke.py tests/docs/test_docs_contract.py \
    tests/cli/test_skill_md_canonical.py tests/docs/test_release_notes_callout.py
ruff check src/popolaloom tests/
mypy src/popolaloom/credentials.py src/popolaloom/cli/init_cmd.py src/popolaloom/cli/auth_cmd.py
git diff --check
bash -n install.sh
./install.sh --help | grep -E '\-\-(with-credentials|ref|from|version)'
./install.sh install --dry-run --no-daemon --no-skills --with-credentials
./install.sh install --dry-run --no-daemon --no-skills --with-credentials --ref=v0.9.7
./install.sh install --dry-run --no-daemon --no-skills --with-credentials --from=pypi --version=0.9.7
./install.sh uninstall --with-credentials --dry-run --yes  # loud-fail
pytest -m "not slow and not nightly and not real_cli and not real_lark and not real_cursor_cloud" -q --no-cov
```

Default-lane: 2835 passed, 21 skipped, 86 deselected, 0 failures. `tests/cli/test_install_script.py` lifts from 16 → 22 cases (6 new + 1 modified help-text smoke). `ruff check src/popolaloom tests/` clean, `mypy` clean on the three changed modules, `git diff --check` clean.

## Companion docs

- [`CHANGELOG.md`](CHANGELOG.md) §[0.9.7]
- [`README.md`](README.md) current release banner + verb-table row for `popola auth cursor` updated to advertise both install paths
- [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) §"Cloud Agent dispatch" prerequisites + §"Credentials & secure storage (v0.9.2+)" setup walkthrough + cloud-only `--configure-cursor-auth` description + v0.9.5 init-time intake fallback paragraph all lead with `./install.sh install --with-credentials` (manual `pip install 'popolaloom[credentials]'` retained as labelled fallback)
- [`docs/QUICKSTART.md`](docs/QUICKSTART.md) + [`docs/zh/QUICKSTART.md`](docs/zh/QUICKSTART.md) — Cloud bootstrap bullet recommends the installer flag

## Known limitations

- **Headless Linux containers still cannot persist to a real OS keyring** — `--with-credentials` installs the `keyring` Python package, but on a host without `dbus-launch` / `/run/user/$UID/bus` / `secret-tool` the registered backend is `keyring.backends.fail.Keyring` and `is_keyring_available()` returns `False`. The new WARN text now calls this out explicitly: operators on dev containers / CI should rely on `CURSOR_API_KEY` (env or `0o600` `.env`) which is the documented `credentials.py` precedence #2 slot. Installing a cryptfile-backed keyring (`keyrings.cryptfile`) is intentionally not bundled because its master-passphrase prompt does not compose with the `popolad` long-running daemon model.
- **PyPI publish remains deferred** (`BL-v0.9.x-PyPI`); use the GitHub tag-pinned install commands above. The default install no longer needs PyPI. Operators who specifically need PyPI can opt in via `--from=pypi --version=0.9.x` once the promotion patch lands.
- **`--with-credentials` requires `--from=pypi` or `--from=git` for clean PEP 508 spec emission** — local-path mode (e.g., `--from=./dist/popolaloom-0.9.7-py3-none-any.whl`) also emits the PEP 508 `popolaloom[credentials] @ <PATH>` form, but pip's tolerance for relative paths in PEP 508 is version-dependent; pass an absolute path or `file://` URL for predictable behavior on local-path installs with extras.
- v0.9.5's init-time Cursor API key intake (`popola init --cursor-api-key VAL` / `--cursor-api-key-file PATH`) carries over byte-for-byte; the single-tenant keyring slot still applies (one Cursor API key per machine, service `popolaloom.cursor` / username `default`); use the `CURSOR_API_KEY` env-var override to switch personal vs service-account contexts (unchanged from v0.9.2).
