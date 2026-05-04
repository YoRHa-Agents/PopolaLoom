"""popolad daemon — v0.2.0 Stages A + B + C + D public surface.

Public surface:

- :class:`Popolad` — top-level facade (preferred entry; explicit instance).
  No longer a module-level singleton (R-013 fixed in v0.2.0).
- :func:`create_app` — FastAPI app factory exposing 7 RPC endpoints over UDS.
- :class:`EventLog` — append-only NDJSON + CloudEvents 1.0 envelope writer.
- :class:`StateStore` / :class:`TaskHandle` / :class:`TaskState` — in-memory
  task registry (now with ``rehydrate`` hook + ``persisted`` field).
- :class:`Supervisor` — subprocess.Popen + threading worker pool
  (with ``stream.truncated`` event for R-007 large-output scenarios).
- **Stage C** :class:`TaskPersistence` + :func:`make_persistence` — ArkTower
  SQLite injection (TaskService + repository + connection + EventBus).
- **Stage C** :class:`PopolaEventBusBridge` — translates ArkTower
  TASK_TRANSITION_EVENT into ``task.transition`` NDJSON entries.

Process model (v0.2.0):

- ``python -m popolaloom.daemon`` (see :mod:`popolaloom.daemon.main`) starts
  a real OS daemon process: asyncio + uvicorn + Unix Domain Socket at
  ``~/.popola/popolad.sock``.
- CLI (``popola dispatch / status / list / attach / cancel``) connects via
  ``httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=...))``.
- Cross-process status visibility: closes R-001 (no real daemon) + R-005
  (attach cross-process invisible).

参见 ``.local/memory/specs/popolaloom/spec.md`` §3.1-3.2 + v0.2.0-plan §4。
"""

from popolaloom.daemon.checkpoint import CheckpointerHandle, make_checkpointer
from popolaloom.daemon.event_bus import PopolaEventBusBridge
from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.graph import GraphCallbacks, build_main_graph
from popolaloom.daemon.graph import TaskState as GraphTaskState
from popolaloom.daemon.interrupt import apply_resume, human_input_required
from popolaloom.daemon.repository import TaskPersistence, make_persistence
from popolaloom.daemon.rpc import create_app
from popolaloom.daemon.server import (
    AdapterCallback,
    Popolad,
)
from popolaloom.daemon.state import StateStore, TaskHandle, TaskState
from popolaloom.daemon.subgraph_dev_test import DevTestState, build_dev_test_subgraph
from popolaloom.daemon.supervisor import Supervisor

__all__ = [
    "AdapterCallback",
    "CheckpointerHandle",
    "DevTestState",
    "EventLog",
    "GraphCallbacks",
    "GraphTaskState",
    "PopolaEventBusBridge",
    "Popolad",
    "StateStore",
    "Supervisor",
    "TaskHandle",
    "TaskPersistence",
    "TaskState",
    "apply_resume",
    "build_dev_test_subgraph",
    "build_main_graph",
    "create_app",
    "human_input_required",
    "make_checkpointer",
    "make_persistence",
]
