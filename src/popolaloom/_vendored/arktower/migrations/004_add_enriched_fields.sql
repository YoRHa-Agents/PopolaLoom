-- 004_add_enriched_fields.sql
-- Add enriched task fields derived from DevolaFlow dispatch/context schemas
-- and agent-oriented best practices.

-- Task Typing & Classification
ALTER TABLE tasks ADD COLUMN task_type TEXT DEFAULT NULL;
ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'task';

-- Execution Constraints
ALTER TABLE tasks ADD COLUMN timeout_seconds INTEGER DEFAULT NULL;
ALTER TABLE tasks ADD COLUMN max_retries INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN deadline TEXT DEFAULT NULL;
ALTER TABLE tasks ADD COLUMN budget_tokens INTEGER DEFAULT NULL;

-- Input/Output Contracts (JSON columns)
ALTER TABLE tasks ADD COLUMN input_schema TEXT NOT NULL DEFAULT '{}';
ALTER TABLE tasks ADD COLUMN output_schema TEXT NOT NULL DEFAULT '{}';
ALTER TABLE tasks ADD COLUMN acceptance_criteria TEXT NOT NULL DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN constraints TEXT NOT NULL DEFAULT '[]';

-- Context References (JSON columns)
ALTER TABLE tasks ADD COLUMN context_refs TEXT NOT NULL DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN subtask_ids TEXT NOT NULL DEFAULT '[]';

-- Quality & Metrics
ALTER TABLE tasks ADD COLUMN quality_thresholds TEXT NOT NULL DEFAULT '{}';
ALTER TABLE tasks ADD COLUMN estimated_effort_minutes INTEGER DEFAULT NULL;

-- Agent Interaction
ALTER TABLE tasks ADD COLUMN agent_instructions TEXT DEFAULT NULL;
ALTER TABLE tasks ADD COLUMN preferred_agent_type TEXT DEFAULT NULL;
ALTER TABLE tasks ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;
