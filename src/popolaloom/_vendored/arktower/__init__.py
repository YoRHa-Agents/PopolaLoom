"""Vendored copy of ArkTower (subset).

Source: https://github.com/YoRHa-Agents/ArkTower @ commit 467a087
Vendored at: 2026-05-05 for PopolaLoom v0.5.0 Stage S1 (D5.7 Path B).

Only the modules PopolaLoom imports at runtime are vendored:
``core/{event_bus,models,state_machine,task_service}``,
``store/{connection,migration,repository,sqlite_repository}``,
``cli/deps`` (only :func:`migrations_dir`), plus the four SQL migrations
under ``migrations/``.

To refresh from upstream see :doc:`/VENDORING.md` at the repo root.
"""

__vendored_from__ = "https://github.com/YoRHa-Agents/ArkTower@467a087"
__vendored_version__ = "0.1.0"
