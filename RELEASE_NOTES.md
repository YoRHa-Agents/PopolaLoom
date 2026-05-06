> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.7.1 — v0.7.0 BUG fixes + handoff envelope foundation

> Released: 2026-05-06
> Theme: closes 3 v0.7.0 residual BUGs (cancel orphan, rehydrate spawn-aborted, attach `--no-follow` ReadTimeout) **and** lays the substrate for the v0.8.0 hands-off envelope feature (schema + hash + writer + archive). No daemon-side wiring yet — that lands in v0.7.2.

## Summary

PopolaLoom v0.7.1 is a hybrid bug-fix + foundation patch that wraps two threads in one branch (`fix/v0.7.1-cancel-orphan-and-rehydrate-spawn-aborted`):

1. **Three BUG fixes** (`feedback_for_v0.7.0.md`):
   - **BUG-A** — cancel-orphan path for daemon-restart leftover SUBMITTED tasks
   - **BUG-B** — rehydrate guard against pre-spawn-crash zombies
   - **BUG-C** — `popola attach --no-follow` EOF / ReadTimeout treated as normal completion

2. **Hands-off envelope foundation** (`feedback_for_v0.8.0.md` item #1, user-decided design 2026-05-06):
   - `popolaloom.handoff` module — schema, slug-hash addressing, atomic writer, dual-layer (active + archive) archiver
   - **100% line + branch coverage** on the new module
   - dispatch / adapter / CLI integration deferred to v0.7.2 (user Q5=E3 internal unification)

The two threads cohabit one branch because the BUG fixes were already implemented (working tree of the same branch) and the handoff foundation is a pure-additive new module — no risk of cross-contamination.

## What's NEW · `popolaloom.handoff` module

```python
from datetime import UTC, datetime
from popolaloom.handoff import HandoffEnvelope, generate_handoff_id, write_envelope

handoff_id = generate_handoff_id("cursor", "fix the bug in foo.py")
# → "cursor-fix-the-bug-in-foo-py-e2de7acd"

env = HandoffEnvelope(
    handoff_id=handoff_id,
    created_at=datetime.now(UTC),
    target_cli="cursor",
    prompt="fix the bug in foo.py — there's a NoneType error around line 42",
)
path = write_envelope(env)
# → .local/.agent/handoff/cursor-fix-the-bug-in-foo-py-e2de7acd.md
```

The on-disk envelope is human-readable Markdown:

```
---
schema_version: '1'
handoff_id: cursor-fix-the-bug-in-foo-py-e2de7acd
created_at: '2026-05-06T14:30:00+00:00'
source_cli: null
target_cli: cursor
parent_task_id: null
cwd: null
adapter_extra: {}
constraints: {}
reason: null
tags: []
---
fix the bug in foo.py — there's a NoneType error around line 42
```

`HandoffEnvelope.from_markdown(text)` round-trips back to an identical model — verified by 3 parametrized cases (minimal / full / special-chars) plus 11 other invariant tests. `extra="forbid"` so unknown front-matter keys raise `ValidationError` (No Silent Failures).

### Public surface

```python
from popolaloom.handoff import (
    HandoffEnvelope,           # Pydantic v2 model, schema_version="1"
    HANDOFF_SCHEMA_VERSION,
    generate_handoff_id,       # <cli>-<slug>-<8hex>
    slugify_prompt,            # prompt → safe ASCII slug, max 30 chars
    content_hash,              # canonical-JSON SHA-256 first 8 hex
    write_envelope,            # atomic write to .local/.agent/handoff/
    envelope_path,             # canonical path lookup, no I/O
    DEFAULT_HANDOFF_ROOT,      # Path(".local/.agent/handoff")
    archive_envelope,          # shutil.copy2 to .local/.agent/archive/<task_id>/
    archive_dir_for,           # canonical archive dir, no I/O
    DEFAULT_ARCHIVE_ROOT,      # Path(".local/.agent/archive")
)
```

## 3 BUG fixes (commits)

| # | Commit | Subject | feedback ref |
|---|---|---|---|
| BUG-A | `1549a2c` | cancel orphan: 区分 `pid=null` 两类 (`_soft_cancel_orphan` vs race window) | item #5 BUG-A |
| BUG-B | `1549a2c` | rehydrate 仅复活有 `popola_dispatch` row 的 task；缺行标 `failed` + emit `popolad.spawn_aborted` event | item #5 BUG-B |
| BUG-C | `d20f46a` | `_consume_sse` hybrid (a)+(b)：终止事件即 break + 终止后 ReadTimeout 视作正常 EOF | item #4 |

3 commits, 11 new tests across the BUG fixes (3 in `tests/test_repository.py` + 5 in `tests/cli/test_attach_no_follow_eof.py` + 3 from `tests/test_repository.py` for orphan-reap path).

## Upgrade notes

- **No breaking changes**：`popolaloom.handoff` 是新公共模块；老 `dispatch_task(prompt)` / `RelayHandoffEnvelope` 路径完全不动
- `.local/.agent/handoff/` 已经在 v0.7.0 `.gitignore` 里，envelope 文件不会进 git
- 升级仅需 `pip install -U popolaloom`（local clone：`pip install -e ".[dev]"`）
- `popola doctor` 不需要新检查项（v0.7.2 接 dispatch 时再加）

## What's NOT in v0.7.1（在后续 patch 落地）

- **v0.7.2**：`dispatch_with_envelope` 内部统一 + 各 adapter `POPOLA_HANDOFF_FILE` env / `--popola-handoff-file` flag 注入（Q3=C5 双通道） + `popola handoff list / show / archive` CLI
- **v0.7.3**：`popola dispatch --replay <handoff_id>` 重放 + HITL feedback envelope（Q7=yes） + 老 `RelayHandoffEnvelope` 桥接到新 `HandoffEnvelope` + 文档刷新（README / QUICKSTART / USER_GUIDE / SKILL.md / DEMO）
- **v0.8.0**：final minor bump + 整体回归 + PR 提交（不直 push main）

## Files changed (v0.7.1)

| 改动 | 文件 |
|---|---|
| BUG-A/B 修 | `src/popolaloom/daemon/server.py`, `src/popolaloom/daemon/rpc.py`, `tests/test_repository.py` |
| BUG-C 修 | `src/popolaloom/cli/main.py`, `tests/cli/test_attach_no_follow_eof.py` |
| Handoff foundation src (NEW) | `src/popolaloom/handoff/{__init__,envelope,hash,writer,archive}.py` (5 files) |
| Handoff foundation tests (NEW) | `tests/handoff/{__init__,test_envelope,test_hash,test_writer,test_archive}.py` (5 files) |
| Doc | `.github/copilot-instructions.md` (Workflow 4 `--cli-flag` 展开) |
| Bump | `pyproject.toml`, `src/popolaloom/__init__.py`, `src/popolaloom/skills/popola-loom/SKILL.md`, `src/popolaloom/skills/popola-loom/.popola-loom-version`, `CHANGELOG.md`, `RELEASE_NOTES.md` |

## Status

| Capability | Status |
|---|---|
| ALL v0.7.0 capabilities | unchanged ✓ |
| BUG-A/B/C closed | ✓ |
| `popolaloom.handoff` module | new ✓ (100% line + branch coverage) |
| `dispatch_with_envelope` | deferred → v0.7.2 |
| `popola handoff` CLI subcommands | deferred → v0.7.2 |
| `popola dispatch --replay` | deferred → v0.7.3 |
| HITL feedback envelope | deferred → v0.7.3 |
| 1508 default-lane tests / 94.45% coverage | ✓ |

## Self-eval (v0.7.1)

- `pytest -m "not slow and not nightly and not real_cli and not real_lark" --cov=src/popolaloom --cov-fail-under=94`：1508 passed, 18 skipped, 82 deselected, 0 failed
- `ruff check src/popolaloom tests/`：All checks passed
- `popolaloom.handoff` 模块覆盖率：100% (line + branch)
- 累计 commits 在 `fix/v0.7.1-cancel-orphan-and-rehydrate-spawn-aborted` 分支：5（BUG-A/B + doc + BUG-C + feat-handoff + this release）

## Next steps

1. v0.7.1 PR 不直接 push 到 `main`（按 "Protected Branch Workflow" 工作区规则），等用户 review
2. v0.7.2 在同一分支继续叠加：`dispatch_with_envelope` 内部统一 + adapter env+flag 双通道 + handoff CLI 子命令
3. v0.7.3 跟进：replay + HITL feedback envelope + old-RelayHandoffEnvelope 桥接 + 文档
4. v0.8.0 收口：final bump + PR 提交
