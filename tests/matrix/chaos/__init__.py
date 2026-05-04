"""Chaos / fault-injection suite (testing-matrix.md §10).

The 12 failure modes listed below are *unit-level* checks: each test
mocks the failure point at the deepest sensible interface (e.g.
``arktower.core.task_service.TaskService.create_task``) and asserts
that PopolaLoom **emits a clear error** instead of silently swallowing
it (workspace rule: "No Silent Failures").

Failure modes (per testing-matrix.md §10):

1. C1  — TaskService.create_task raises (IntegrityError-equiv)
2. C2  — SQLite OperationalError "database is locked"
3. C3  — SqliteSaver write fails
4. C4  — EventLog fd suddenly closed mid-write
5. C5  — Supervisor.spawn raises OSError
6. C6  — UDS socket bind permission denied
7. C7  — UDS socket path > 100 chars (AF_UNIX limit)
8. C8  — migration runner fails
9. C9  — asyncio loop blocked by sync call (back-pressure)
10. C10 — event-bus handler raises (other subscribers unaffected)
11. C11 — disk full during NDJSON write (ENOSPC)
12. C12 — concurrent dispatch race (10 dispatches; all distinct ids)

Most cases are Tier 2 speed (< 1 s) because they mock the failure
point — no real daemon subprocess required.  A small number that need
the real RPC layer wrap a :func:`real_popolad` fixture and inherit the
``slow`` marker via module-level ``pytestmark``.
"""

from __future__ import annotations
