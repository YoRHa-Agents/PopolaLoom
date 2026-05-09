> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.9.3 — Workspace worker routing

<!-- updated: 2026-05-10 -->

> Released: 2026-05-10

> **How to install v0.9.3** (Q-D-5 偏离默认 carries forward; PyPI promotion remains tracked as `BL-v0.9.x-PyPI`):
>
> ```bash
> # Option A — canonical, tag-pinned (always works for v0.9.3):
> pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.3
>
> # Option A+ — include OS-keyring credential support for cloud users:
> pip install 'popolaloom[credentials] @ git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.3'
>
> # Option B — repo-root unified installer, from-git (auto-tracks main; post-tag = v0.9.3):
> ./install.sh install --from=git
> ```

## Theme

v0.9.3 is a strictly additive patch on the v0.9.x line. It closes the worker feedback recorded in `.local/feedbacks/feedback_for_v0.9.1.md`: a workspace should have one easy-to-recognise self-hosted worker, repeated starts should reuse it by default, and a task should be able to dispatch directly to that workspace worker without a browser handoff.

The release keeps the existing local CLI lane, generic `--cli=cursor-cloud` REST lane, v0.9.1 handoff lane, and v0.9.2 credential resolver intact. The new behavior is opt-in or reuse-first: `popola cloud worker start` only starts a duplicate when `--allow-duplicate` is passed, and `popola cloud worker dispatch --print-only` previews the equivalent daemon dispatch without creating a task.

## Highlights

### Workspace-aware worker singleton

`popola cloud worker start` now derives a deterministic worker name when `--name` is omitted, using the resolved workspace / worker directory in the form `popolaloom-<repo>-<hash>`. If the current workspace already has a matching running worker, the command reuses it instead of spawning another `agent worker start` process.

Use `--allow-duplicate` when an operator intentionally wants a second worker for the same directory. This keeps the default path aligned with the feedback request: one workspace, one recognisable worker.

### Direct worker dispatch

`popola cloud worker dispatch "<prompt>"` sends a PopolaLoom-tracked task through `popolad` with `cli=cursor-cloud` and the workspace worker routing extras pre-filled. The command is the direct path for "dispatch to this worker" when a popola task id, `attach`, `status`, and `cancel` are desired.

Preview mode stays non-mutating:

```bash
popola cloud worker dispatch "review this branch" \
  --worker-dir "$(pwd)" \
  --repo-url https://github.com/acme/repo \
  --print-only
```

### Cursor Cloud private-worker routing extras

The generic REST lane now accepts the same routing extras directly via `--cli-flag`:

```bash
popola dispatch "fix failing tests" --cli=cursor-cloud \
  --cli-flag repo_url=https://github.com/acme/repo \
  --cli-flag worker_name=popolaloom-repo-abc123 \
  --cli-flag pool_name=eng
```

Stable extras for v0.9.x are `use_private_worker`, `labels`, `worker_name`, `machine_name`, and `pool_name`. Convenience keys merge into `labels` and automatically request `use_private_worker=true`; contradictory inputs such as `use_private_worker=false` plus a routing label fail loudly.

## Test surface

Focused/local verification for this release-prep task:

```bash
python -m pytest tests/test_smoke.py tests/docs/test_docs_contract.py tests/cli/test_skill_md_canonical.py tests/docs/test_release_notes_callout.py
git diff --check
```

Result: 14 passed, 2 skipped for the focused pytest command; `git diff --check` passed.

Full default-lane release verification has not been claimed here and should be completed by the parent release run before tagging.

Feature coverage added by the current changes includes cursor-cloud routing-extra tests, worker singleton / reuse tests, direct worker dispatch tests, and `--print-only` preview tests.

## Companion docs

- [`CHANGELOG.md`](CHANGELOG.md) §[0.9.3] — release-grade archive entry with Added / Changed / Tests / Files / Known limitations.
- [`docs/API_STABILITY.md`](docs/API_STABILITY.md) — minimal v0.9.3 stable-surface note for cursor-cloud private-worker routing extras and `popola cloud worker dispatch`.
- [`README.md`](README.md) — current release banner, install snippets, and v0.9.3 highlights.
- [`src/popolaloom/skills/popola-loom/SKILL.md`](src/popolaloom/skills/popola-loom/SKILL.md) — canonical Skill version and Workflow 10 routing summary.

## Known limitations

- **PyPI publish still deferred** — v0.9.3 remains GitHub-Release-only until `BL-v0.9.x-PyPI` lands. Use the tag-pinned Git URL or `./install.sh install --from=git`.
- **Worker selection is ultimately Cursor-owned** — PopolaLoom passes `usePrivateWorker` and routing labels to Cursor REST; Cursor Cloud Agents remains the authority on matching a request to a concrete worker.
- **No duplicate worker cleanup automation** — `--allow-duplicate` intentionally permits multiple workers for experiments; operators still stop unneeded workers through the upstream worker process lifecycle.
