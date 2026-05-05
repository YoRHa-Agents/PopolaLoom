"""Self-bootstrap test scenarios — v0.2.0 Stage E E1 + E2.

This package houses the **self-validation** tests that exercise
PopolaLoom in real subprocess form (real ``python -m popolaloom.daemon``,
real httpx UDS client, real ArkTower SQLite) so the dispatch / attach /
recovery contracts hold across actual OS process boundaries.

Tests here are slow (≥ 1s wall clock — daemon spawn + socket wait +
real subprocess work) and gated by the ``slow`` pytest marker; run
with ``pytest tests/self_bootstrap/ -m slow -v``.

Spec coverage (.local/memory/specs/popolaloom/spec.md §3.4.1):

- **S1** — popolad被 SIGKILL → 重启 → 从 ArkTower SQLite + LangGraph
  SqliteSaver rehydrate in-flight task (R-002 closure, see
  :file:`test_s1_crash_recovery.py`).
- **S3** — popola dispatch一个会自己 popola dispatch子任务的脚本 →
  parent_task_id 链 + thread_id 隔离 (see
  :file:`test_s3_recursive_dispatch.py`).

Out of v0.2.0 scope (planned for v0.3.0):

- S2 — interrupt + resume across HITL.
- S4 — 8-hour offline → re-attach.
- S5 — 5 concurrent CLIs → cross-CLI handoff.
"""
