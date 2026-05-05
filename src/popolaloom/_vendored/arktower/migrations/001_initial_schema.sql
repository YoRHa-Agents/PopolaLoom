-- 001_initial_schema.sql
-- Core tables for the ArkTower task pool.

-- ============================================================
-- Task templates (referenced by tasks.template_id FK)
-- ============================================================
CREATE TABLE IF NOT EXISTS task_templates (
    id                  TEXT    PRIMARY KEY,
    name                TEXT    NOT NULL UNIQUE,
    description         TEXT    NOT NULL DEFAULT '',
    default_priority    TEXT    NOT NULL DEFAULT 'medium'
                               CHECK (default_priority IN ('low','medium','high','critical')),
    default_tags        TEXT    NOT NULL DEFAULT '[]',
    default_labels      TEXT    NOT NULL DEFAULT '{}',
    parameter_schema    TEXT    NOT NULL DEFAULT '{}',
    checklist           TEXT    NOT NULL DEFAULT '[]',
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ============================================================
-- Tasks (primary table)
-- ============================================================
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT    PRIMARY KEY,
    title           TEXT    NOT NULL,
    description     TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'submitted'
                           CHECK (status IN (
                               'submitted','queued','in_progress','review',
                               'input_required','blocked',
                               'completed','failed','canceled','timed_out'
                           )),
    priority        TEXT    NOT NULL DEFAULT 'medium'
                           CHECK (priority IN ('low','medium','high','critical')),
    parent_id       TEXT    REFERENCES tasks(id) ON DELETE SET NULL,
    context_id      TEXT,
    owner_id        TEXT    NOT NULL DEFAULT 'system',
    assigned_to     TEXT,
    assigned_type   TEXT,
    parameters      TEXT    NOT NULL DEFAULT '{}',
    output          TEXT,
    error           TEXT,
    labels          TEXT    NOT NULL DEFAULT '{}',
    template_id     TEXT    REFERENCES task_templates(id) ON DELETE SET NULL,
    max_steps       INTEGER,
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    started_at      TEXT,
    completed_at    TEXT
);

-- ============================================================
-- Tags (many-to-many)
-- ============================================================
CREATE TABLE IF NOT EXISTS tags (
    task_id     TEXT    NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    tag         TEXT    NOT NULL,
    PRIMARY KEY (task_id, tag)
);

-- ============================================================
-- Dependencies
-- ============================================================
CREATE TABLE IF NOT EXISTS dependencies (
    from_task_id    TEXT    NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    to_task_id      TEXT    NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    dep_type        TEXT    NOT NULL DEFAULT 'blocks'
                           CHECK (dep_type IN ('blocks','relates_to')),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (from_task_id, to_task_id),
    CHECK (from_task_id != to_task_id)
);

-- ============================================================
-- Task history (immutable audit log)
-- ============================================================
CREATE TABLE IF NOT EXISTS task_history (
    event_id        TEXT    PRIMARY KEY,
    task_id         TEXT    NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    trigger         TEXT    NOT NULL,
    from_status     TEXT    NOT NULL,
    to_status       TEXT    NOT NULL,
    actor           TEXT    NOT NULL DEFAULT 'system',
    notes           TEXT,
    timestamp       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ============================================================
-- Archives
-- ============================================================
CREATE TABLE IF NOT EXISTS archives (
    id              TEXT    PRIMARY KEY,
    task_snapshot   TEXT    NOT NULL,
    history         TEXT    NOT NULL,
    tags            TEXT    NOT NULL DEFAULT '[]',
    archived_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    original_status TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    context_id      TEXT
);

-- ============================================================
-- Indexes
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_tasks_status          ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority        ON tasks(priority);
CREATE INDEX IF NOT EXISTS idx_tasks_parent_id       ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_context_id      ON tasks(context_id);
CREATE INDEX IF NOT EXISTS idx_tasks_assigned_to     ON tasks(assigned_to);
CREATE INDEX IF NOT EXISTS idx_tasks_owner_id        ON tasks(owner_id);
CREATE INDEX IF NOT EXISTS idx_tasks_template_id     ON tasks(template_id);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at      ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority DESC);

CREATE INDEX IF NOT EXISTS idx_tasks_queue_pick ON tasks(status, priority DESC, created_at ASC)
    WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

CREATE INDEX IF NOT EXISTS idx_deps_to ON dependencies(to_task_id);

CREATE INDEX IF NOT EXISTS idx_history_task_id   ON task_history(task_id);
CREATE INDEX IF NOT EXISTS idx_history_timestamp ON task_history(timestamp);

CREATE INDEX IF NOT EXISTS idx_archives_context  ON archives(context_id);
CREATE INDEX IF NOT EXISTS idx_archives_archived ON archives(archived_at);
