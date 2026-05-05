-- 003_add_agent_fields.sql
-- Add agent capability matching fields to tasks table.

ALTER TABLE tasks ADD COLUMN capabilities TEXT NOT NULL DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN required_tools TEXT NOT NULL DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN estimated_complexity TEXT DEFAULT NULL;
