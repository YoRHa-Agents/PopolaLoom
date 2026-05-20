> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md).

# PopolaLoom v1.6.2 — git installer build-dependency fallback

Released: 2026-05-21

<!-- updated: 2026-05-21 -->

## Theme

v1.6.2 fixes the `install.sh` git-source bootstrap path on machines whose
configured primary pip mirror does not provide isolated build dependencies
such as `hatchling`. This is a patch release for installer reliability only:
no daemon contract changes, no dispatch behavior changes, and no migration.

## What Changed

- **Git-source installs use a per-command PyPI fallback.** `install.sh`
  now passes `--extra-index-url=https://pypi.org/simple` only when
  `--from=git` is active. The user's configured primary pip index remains
  the first source, but pip can still resolve build-system packages needed
  to build PopolaLoom from GitHub.
- **No global pip or proxy mutation.** The fallback is scoped to the single
  `pip install` / `pip install --upgrade` invocation. `--from=pypi` and
  local path or wheel installs keep their previous index behavior.
- **Installer version bumped to `0.9.8`.** The package, bundled skills,
  tracked project skill copy, and release metadata are bumped to `1.6.2`.

## Tests

- `bash -n install.sh`
- `pytest tests/cli/test_install_script.py -q`
- `pytest -q`

## Upgrade

```bash
popola update

pip install --upgrade git+https://github.com/YoRHa-Agents/PopolaLoom@v1.6.2
popola skill install --target=cursor --global --force
popola skill install --target=claude --global --force

popola version  # -> popolaloom 1.6.2
```
