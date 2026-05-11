-- 005_popolaloom_extensions.sql
-- PopolaLoom-specific extensions to the ArkTower SQLite schema.
--
-- Purpose
-- -------
-- Track popolaloom dispatch metadata that doesn't fit ArkTower's core
-- ``tasks`` table: how the subprocess was supervised, sandbox profile,
-- and the IDE's native session id (so ``cursor agent --resume <id>`` /
-- ``claude --session <id>`` / ``codex resume <id>`` can stitch context
-- across popolad restarts in v0.3.0).
--
-- Dependencies
-- ------------
-- ArkTower migrations 001-004 must be applied first (the alphabetical
-- ordering of ``MigrationRunner`` already guarantees this when this file
-- is added to a directory that is sorted after ArkTower's, OR when the
-- runner is invoked twice — once on each migrations dir — as
-- ``daemon/repository.py:make_persistence`` does).
--
-- ArkTower upstream collision risk
-- --------------------------------
-- If ArkTower upstream adds 005 (low probability — same org / coordinated
-- work), rename this file to ``005a_popolaloom_extensions.sql``.  The
-- ``MigrationRunner`` parses the leading integer for the ``schema_version``
-- table key and applies in version-then-name order, so a tie is OK iff
-- both files do strictly disjoint things (we own ``popola_dispatch``;
-- ArkTower owns ``tasks`` / ``task_history`` / ``tags`` / ``dependencies``
-- / ``archives``).
--
-- Population status
-- -----------------
-- For v0.2.0 mvp, ``popola_dispatch`` is "occupied schema" — the table
-- exists at startup but ``Popolad.dispatch_task`` does NOT actively
-- INSERT rows here.  Populating it is a v0.3.0 concern (R-010 runtime
-- supervision: tmux / systemd-run / popen) covered by the
-- ``runtime`` / ``supervisor`` / ``sandbox`` columns.

CREATE TABLE IF NOT EXISTS popola_dispatch (
    dispatch_id         TEXT    PRIMARY KEY,
    -- task_id references arktower ``tasks(id)`` but we deliberately do
    -- NOT add a FOREIGN KEY because:
    -- 1. ArkTower may run on a separate DB file in v0.3.0 multi-pool
    --    deployments (cross-DB FK not supported by SQLite anyway);
    -- 2. Allows v0.3.0 ``relay`` table to point at popola_dispatch even
    --    when the underlying ArkTower task has been archived / deleted.
    task_id             TEXT    NOT NULL,
    -- v0.3.0 R-010 (runtime supervision): "systemd-run" / "popen" / "tmux".
    -- v0.2.0 hard-codes "popen" via subprocess.Popen + setsid; this column
    -- exists so the schema is forward-compatible without another migration.
    runtime             TEXT,
    -- v0.2.0 daemon is in-process; v0.3.0 may detach into a subprocess
    -- supervisor.  Allowed: "in-process" / "subprocess".
    supervisor          TEXT,
    -- Sandbox profile passed to the CLI (e.g. cursor: workspace-write /
    -- danger-full-access; claude: read-only / read-write).  Nullable for
    -- adapters that don't support sandboxing.
    sandbox             TEXT,
    -- Native CLI session id for resume:
    --   - cursor: chat session UUID surfaced via ``--print --output-format=stream-json``
    --   - claude: session id from ``claude --session-id``
    --   - codex:  session token from ``codex resume``
    -- Nullable until the adapter parses + back-fills it from CLI output.
    native_session_id   TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_popola_dispatch_task        ON popola_dispatch(task_id);
CREATE INDEX IF NOT EXISTS idx_popola_dispatch_session     ON popola_dispatch(native_session_id);
CREATE INDEX IF NOT EXISTS idx_popola_dispatch_created_at  ON popola_dispatch(created_at);
