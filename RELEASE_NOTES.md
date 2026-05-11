> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md).

# PopolaLoom v1.1.0 — Preferences Wizard + Dispatch Q&A + Path-B Wiring

<!-- updated: 2026-05-11 -->

> **Upgrade note:** v1.1.0 auto-migrates flat `[user_preferences]` keys to the
> nested `schema_version = 2` layout, writing `popolad.toml.v1.bak` before the
> live file is updated. `popola doctor` now includes a "User preferences schema"
> row to verify absent / v2 nested / invalid states.

## Highlights

- Expanded `popola init prefs --wizard` into sectioned option groups for
  routing, adapter defaults, Lark notifications, and dispatch behavior.
- Added dotted preference writes, for example
  `popola init prefs --set cursor-cloud.model=composer-2`.
- Added `popola dispatch --wizard` plus implicit ambiguity Q&A when
  `[user_preferences.dispatch].ambiguity_resolution = "prompt"`.
- Documented IDE AskQuestion guidance in the canonical `popola-loom` Skill.
- Wired Path-B session-JWT dispatch through `popolad` and registered
  `--preset=grind`.
- Documented the current experimental Path-B HTTP 404 risk in
  `docs/known-issues.md`; stable REST remains the production lane.

## Verification

Run:

```bash
pytest tests/daemon/test_user_preferences_nested.py \
  tests/cli/test_init_prefs_wizard.py \
  tests/cli/test_dispatch_wizard.py \
  tests/cli/test_dispatch_ambiguity_auto.py \
  tests/cli/test_skill_md_canonical.py \
  tests/cli/test_path_b_e2e_wiring.py \
  tests/cloud/internal/test_rpc_mock.py \
  tests/daemon/test_supervisor_path_b_branch.py -q
```

For the live experimental Path-B endpoint smoke, opt in explicitly:

```bash
POPOLA_REAL_CURSOR_REPO_URL=https://github.com/org/repo \
pytest -m real_cursor_cloud_jwt tests/cloud/internal/test_rpc_mock.py
```
