-- 002_add_fts5.sql
-- Full-text search with FTS5 + synchronization triggers.

-- ============================================================
-- FTS5 virtual table for tasks
-- ============================================================
CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
    title,
    description,
    content='tasks',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

-- Populate the FTS index from any existing rows
INSERT INTO tasks_fts(rowid, title, description)
    SELECT rowid, title, description FROM tasks;

-- ============================================================
-- Triggers to keep FTS5 index synchronized
-- ============================================================
CREATE TRIGGER IF NOT EXISTS tasks_fts_insert AFTER INSERT ON tasks BEGIN
    INSERT INTO tasks_fts(rowid, title, description)
    VALUES (new.rowid, new.title, new.description);
END;

CREATE TRIGGER IF NOT EXISTS tasks_fts_update AFTER UPDATE OF title, description ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, description)
    VALUES ('delete', old.rowid, old.title, old.description);
    INSERT INTO tasks_fts(rowid, title, description)
    VALUES (new.rowid, new.title, new.description);
END;

CREATE TRIGGER IF NOT EXISTS tasks_fts_delete AFTER DELETE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, description)
    VALUES ('delete', old.rowid, old.title, old.description);
END;

-- ============================================================
-- FTS5 virtual table for archives
-- ============================================================
CREATE VIRTUAL TABLE IF NOT EXISTS archives_fts USING fts5(
    title,
    content='archives',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS archives_fts_insert AFTER INSERT ON archives BEGIN
    INSERT INTO archives_fts(rowid, title)
    VALUES (new.rowid, new.title);
END;

CREATE TRIGGER IF NOT EXISTS archives_fts_delete AFTER DELETE ON archives BEGIN
    INSERT INTO archives_fts(archives_fts, rowid, title)
    VALUES ('delete', old.rowid, old.title);
END;
