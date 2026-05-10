> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.9.9 — Worker dispatch + observability + init secret caching

<!-- updated: 2026-05-10 -->

> Released: 2026-05-10

> **How to install v0.9.9** (Q-D-5 偏离默认 carries forward; PyPI promotion remains tracked as `BL-v0.9.x-PyPI`):
>
> ```bash
> ./install.sh install                                                    # canonical (default --from=git, tracks main)
> ./install.sh install --ref=v0.9.9                                       # canonical tag-pinned (recommended for v0.9.9)
> ./install.sh install --with-credentials                                 # also installs the OS-keyring extra
> ./install.sh install --ref=v0.9.9 --with-credentials                    # tag-pinned + keyring extra in one shot
> pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.9       # manual fallback
> pip install 'popolaloom[credentials] @ git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.9'   # manual fallback w/ extra
> ```

## Theme

v0.9.9 closes the **8 outstanding items** in [`./.local/feedbacks/feedback_for_v0.9.7.md`](.local/feedbacks/feedback_for_v0.9.7.md) (the six original observability / dispatch / orphan-process pain points 1a / 1b / 1c / 2 / 3 / 5, plus the user's verbatim follow-up at lines 114-116 about worker-targeted dispatch and init-time secret caching). Six source-code patches across supervisor / daemon / adapter / CLI plus one canonical 0o600 fallback file land **without breaking a single v0.9.0 GA stable surface** (per [`docs/API_STABILITY.md`](docs/API_STABILITY.md)). The release organises those changes into 4 implementation waves (A / B / C / D) plus 1 schema-investigation spike (Spike-0); the `pid_alive` probe, the worker stop verb, and the `account_class` pre-flight gate are the operator-visible new surfaces.

The lockstep version-bump (`__version__`, `pyproject.toml`, two `SKILL.md` frontmatters, two `.popola-loom-version` markers, `docs/_config.yml`) is enforced by `tests/cli/test_skill_md_canonical.py::test_skill_md_version_matches_package`; the feedback-resolution stamp lives in `.local/feedbacks/TRACKER.md` (`FB-v0.9.7-1` Closed row).

## Highlights

- **F1 — stdout silence-timer + branched `process.note` event** (Q-V099-5 + Q-V099-14). The supervisor (`src/popolaloom/daemon/supervisor.py`) now arms a 30-second stdout-silence timer in `Supervisor.spawn` (t0 = the `process.started` thread fan-out moment); when neither stdout nor stderr emits a line within the window, it fires a single `process.note` event with `kind=stdout_silence` plus a branched operator-facing hint. The branching matches the cursor-agent stdout-buffering shape verbatim from `feedback_for_v0.9.7.md:33-34`: `cursor` + `output_format=text` (or unknown) gets the "pass `--cli-flag output_format=stream-json` for live progress" hint, `cursor` + `output_format=stream-json` gets the "first frame not yet emitted" hint per Q-V099-14, and any other CLI gets a generic stdout-silence note. The timer is a fire-once `threading.Timer` cancelled by the first non-empty drain line (`_drain_stream`) AND by the wait-thread (`_wait_and_finalize`) so a fast-exiting task does not leak a delayed note after termination.

- **F2 — `popola status` `pid_alive` field** (Q-V099-4). `Popolad.get_status` now performs a WARN-only `os.kill(pid, 0)` probe for every local-runtime + RUNNING handle with a known pid; on `ProcessLookupError` it surfaces `pid_alive=false` in the JSON envelope and logs a daemon-side WARN (`status drift: task=… state=running but pid=… already reaped; supervisor sync pending`); on `PermissionError` it surfaces `pid_alive=true`. The field is intentionally **absent** for cloud-runtime tasks, terminal-state tasks, and running tasks without a known pid — additive-only contract, every existing JSON consumer keeps working unchanged. The follow-up "force-finalize once `pid_alive=false`" change is deferred to `BL-v0.10.0-supervisor-force-finalize` per Q-V099-4 (we keep status read-only in v0.9.9 to avoid widening the supervisor write contract on a patch release).

- **F3 — dispatch-time CLI footer + worker idle hint**. `popola dispatch --cli=cursor` (`src/popolaloom/cli/main.py`) now prints a follow-up line after the `task_id` so operators don't spend 10 minutes refreshing the Cursor dashboard waiting for a local subprocess to appear: `view: popola attach <id> --follow (note: Cursor dashboard does not show local subprocess tasks)`. The footer is gated on `cli == "cursor"` so `cursor-cloud` and other adapters keep their existing single-line output. In parallel, `popola cloud worker status` (`cloud_worker_cmd.py`) appends a worker idle hint (`note: 0 sessions claimed since worker started`) when `metrics.last_activity` is zero AND `readyz.claimed` is false — the hint is suppressed in JSON mode and as soon as a claim signal is observed.

- **F4 — `_ERROR_CATALOG` GitHub-App-missing 400 entry (Q-V099-7)**. `_ERROR_CATALOG` in `src/popolaloom/adapters/cursor_cloud.py` grows from 16 → 17 entries. The new `integration_github_app_branch_not_found` entry matches the regex `(?i)failed\s+to\s+verify\s+existence\s+of\s+branch.+in\s+repository` against HTTP 400 `validation_error` responses and reuses the existing `GithubAppMissingError` subclass (no new exception subclass — Q-V099-7 lock). The bilingual hint surfaces both `https://cursor.com/integrations/github` and the `auto_create_pr=false` workaround. The entry sits BEFORE the generic `validation_request_body` hit so the regex match wins on the +5 score per `_score_entry`.

- **F5 + U1 — `account_class` pre-flight gate** (Q-V099-1 + Wave Spike-0 BRANCH_B). New `account_class` metadata field on `$POPOLA_HOME/credentials.toml` (default `unknown` for backward compat); new `AccountClass` enum + `store_account_class` / `get_account_class` helpers in `src/popolaloom/credentials.py`; new `popola auth cursor set --account-class={personal|service-account|unknown}` Typer option (case-insensitive) plus `--no-prompt` for CI. The pre-flight gate in `worker_dispatch_cmd` refuses with a bilingual loud-fail when class ∈ {`personal`, `unknown`} (Exit 78), citing the Wave Spike-0 verdict and offering the three documented workarounds (`popola cloud worker handoff`, `popola dispatch --cli=cursor`, the `@Cursor worker=<name>` chat trigger in Slack/GitHub/Linear). The persistence is metadata-only — the literal API key value never travels alongside the class label.

- **F6 — `_run_subprocess` Popen + setsid + signal forwarder + NEW `popola cloud worker stop` verb** (Q-V099-6). The `_run_subprocess` helper underneath `popola cloud worker start` is rewritten to `subprocess.Popen(start_new_session=True)` so the spawned `agent worker start` Node child is the leader of its own process group; the Python wrapper installs `signal.signal(SIGTERM, …)` / `signal.signal(SIGINT, …)` handlers that re-broadcast to `os.killpg(getpgid(self.pid), SIGTERM)` so killing the wrapper now cascades cleanly. NEW `popola cloud worker stop --name X | --worker-dir Y` Typer verb with `--grace N` SIGTERM-then-SIGKILL escalation; the `--help` documents the no-idle-gate caveat verbatim per Q-V099-6: *"Stops the worker even if a Cloud Agent session is currently claimed; compose with `popola cloud worker status --busy` to gate."*

- **U2 — `popola init` 0o600 fallback file** (Q-V099-11 + Q-V099-12; closes the user's verbatim follow-up at `feedback_for_v0.9.7.md:114-116`). When the keyring is unavailable on the host, `popola init --cursor-api-key VAL` (and `--cursor-api-key-file`) now writes a 0o600-protected fallback file at `~/.popola/cursor_api_key.env` containing `CURSOR_API_KEY=<value>\n`. A fresh shell can then `source ~/.popola/cursor_api_key.env` before `popola dispatch`, OR rely on the daemon's new auto-source: `popolad` startup now calls `credentials.load_env_fallback_into_environ` so `popola popolad start` after `popola init --cursor-api-key VAL` works end-to-end without any manual export. The pre-existing env-var precedence (slot #2 in [`API_STABILITY.md` §2.5](docs/API_STABILITY.md#25-cursor-api-key-credential-resolver-v092)) keeps winning if the operator has `CURSOR_API_KEY` in their shell already (No-Silent-Failures: never overwrite a live env value).

### Wave Spike-0 outcome

The Wave Spike-0 desk-research spike (`.local/.agent/active/v0.9.9-worker-observability/SCHEMA_INVESTIGATION.md`) concluded BRANCH_B: as of 2026-05-10 Cursor REST has **no documented schema** for personal-key + self-hosted-worker dispatch with Dashboard visibility — the `usePrivateWorker` + `labels` keys are accepted only by Self-Hosted Pool (Enterprise + service-account API key), and the `My Machines` lane visible in the dashboard is a chat-surface trigger (`@Cursor worker=<name>` in Slack / GitHub / Linear) rather than a REST endpoint. The original Q-V099-2 lock (Option-A loud-fail) is therefore the correct shape; v0.9.9 ships exactly that with hint text that references `SCHEMA_INVESTIGATION.md` and the My Machines chat-trigger workaround. Drafted upstream Cursor issue text is embedded in the SCHEMA_INVESTIGATION.md for filing to `https://github.com/getcursor/cursor/issues` once the v0.9.9 release is cut.

## Test surface

Local verification before PR:

```bash
python -m pytest tests/cli/test_skill_md_canonical.py tests/docs/test_docs_contract.py \
    tests/docs/test_release_notes_callout.py tests/test_smoke.py -q
python -m pytest tests/cli/ tests/cloud/ tests/adapters/ tests/daemon/ tests/docs/ -q --maxfail=10
ruff check src/popolaloom tests/
```

Default-lane CI: 1289+ passed across `tests/cli/` + `tests/cloud/` + `tests/adapters/` + `tests/daemon/` + `tests/docs/`. New tests: roughly 91 (Wave A + B1 — F1 + F2 + F3 + F4 + U2) + 51 (Wave B2 — F5 + U1 account_class pre-flight gate) + 16 (Wave C — F6 Popen+setsid + worker stop verb) ≈ 158 new tests across the four lanes. The lockstep test (`tests/cli/test_skill_md_canonical.py::test_skill_md_version_matches_package`) passes after the v0.9.8 → v0.9.9 bump on six version-marker files.

## Companion docs

- [`CHANGELOG.md`](CHANGELOG.md) §[0.9.9] — full diff matrix + cross-reference back to `feedback_for_v0.9.7.md`
- [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) — 7 add-only sub-sections (F1 silence-timer, F2 `pid_alive`, F3 dispatch footer + worker idle hint, F4 GitHub-App-missing 400, F5 + U1 account_class, F6 `popola cloud worker stop`, U2 fallback file)
- [`docs/QUICKSTART.md`](docs/QUICKSTART.md) — Step 1.5 callout for the `~/.popola/cursor_api_key.env` headless-container fallback
- [`README.md`](README.md) — version-pointer lockstep (`--ref=v0.9.9` install commands)
- `.local/.agent/active/v0.9.9-worker-observability/SCHEMA_INVESTIGATION.md` — Wave Spike-0 BRANCH_B verdict + drafted upstream Cursor issue text (local-only)
- `.local/feedbacks/TRACKER.md` — `FB-v0.9.7-1` Closed row + `Releases 总览` v0.9.9 line

## Known limitations

- **PyPI publish remains deferred** (`BL-v0.9.x-PyPI`); use the GitHub tag-pinned install commands above. The default install no longer needs PyPI. Operators who specifically need PyPI can opt in via `--from=pypi --version=0.9.x` once the promotion patch lands.
- **Three v0.10.0 backlog rows carry forward from Q-V099-1 + Q-V099-2 + Q-V099-9**: `BL-v0.10.0-cursor-personal-key-worker-schema` (track upstream Cursor REST schema once it lands), `BL-v0.10.0-supervisor-force-finalize` (auto-reap a status-vs-pid drift after T seconds rather than just WARN-and-surface), `BL-v0.10.0-cursor-cloud-rest-smoke` (gated live REST smoke for personal vs service-account combinations).
- **Two cleanup rows carry forward**: `BL-v0.10.0-init-no-cursor-key-flag` (explicit opt-out flag for `popola init` so CI can skip the v0.9.5 intake without setting an empty `--cursor-api-key`) and `BL-v0.10.0-init-validate-cursor-key` (round-trip the key through `GET /v1/me` at init time, mirroring `popola auth cursor set --validate`).
- **Personal API key + self-hosted worker still cannot land a popola-tracked task on the Cursor dashboard.** Per Wave Spike-0, the only Dashboard-visible path under a personal key remains the chat-surface trigger or `popola dispatch --cli=cursor-cloud auto_create_pr=false` (without worker routing). v0.9.9 surfaces this verbatim in the F5/U1 pre-flight gate hint and the `SCHEMA_INVESTIGATION.md` upstream-issue draft tracks the asks PopolaLoom needs from Cursor REST to lift this constraint.
- v0.9.7's `--with-credentials` install flag and v0.9.8's design-ideas / demo-page chapters carry over byte-for-byte; the single-tenant keyring slot still applies (one Cursor API key per machine, service `popolaloom.cursor` / username `default`); use the `CURSOR_API_KEY` env-var override or the new `~/.popola/cursor_api_key.env` 0o600 fallback to switch personal vs service-account contexts.
