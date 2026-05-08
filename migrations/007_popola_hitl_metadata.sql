-- 007_popola_hitl_metadata.sql
-- v0.8.7 T2.1.3 — Cloud HITL idempotency-key metadata column.
--
-- Per .local/.agent/active/v0.8.7-cloud-hitl-prod/PLAN.md §4.1 T2.1.3 AC (a)+(b)
-- and .local/research/v0.8.7_hitl/mcp-tool-contract.md §5 idempotency design:
-- the daemon's `submit_request` handler must persist the
-- `idempotency_key` (sha256(task_id|agent_id|run_id|question_text)[:32], or
-- caller-supplied) so that replays inside a 1-hour window short-circuit to
-- the existing row instead of creating a new HITL prompt + new Lark card.
--
-- Per SECURITY_CHECKLIST §5 R3 the dedup lookup MUST be SQL-only (no
-- in-memory cache that would not survive `popolad` restarts). This column
-- is the single source of truth for the dedup state; ``json_extract`` (or
-- the equivalent ``->>`` operator) is queried directly against the
-- `popola_hitl` table.
--
-- The column also carries the structured `(cursor_agent_id, cursor_run_id,
-- task_id)` tuple used for mis-route defense in `submit_answer`: a Lark
-- webhook MUST NOT be able to answer a row that belongs to a different
-- `cursor_run_id` (lateral-movement guard L1).
--
-- Schema choice
-- -------------
-- TEXT NOT NULL DEFAULT '{}' so existing rows backfill to a valid empty
-- JSON object. Application code reads with `json_extract(metadata, ...)`
-- and writes via parameterized UPDATE; both compose with SQLite's JSON1
-- extension (compiled in by default since SQLite 3.38; verified at module
-- import time in src/popolaloom/hitl/cloud_bridge.py per R2 mitigation).

ALTER TABLE popola_hitl ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}';

-- Functional index over `idempotency_key` for the rolling 1-hour dedup
-- lookup. Combined with the existing `idx_hitl_created_at` (006), the
-- planner can scan the most-recent rows quickly. Partial-index would be
-- ideal but SQLite functional indexes already only emit entries for rows
-- where the expression is non-null, so this gives us the same effect.
CREATE INDEX IF NOT EXISTS idx_hitl_idempotency_key
    ON popola_hitl(json_extract(metadata, '$.idempotency_key'));
