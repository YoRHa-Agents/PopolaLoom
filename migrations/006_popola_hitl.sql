-- 006_popola_hitl.sql
-- v0.3.0 F4.C — HITL prompt persistence for cross-channel sync.
--
-- Per spec §3.4 + roadmap §12.8.3 + v0.3.0-plan §4 Stage F4.12.
--
-- Purpose
-- -------
-- Track every HITLPrompt issued by popolad: the prompt JSON, current
-- status, the channel that recorded the answer (if any), and the
-- timeline (created/deadline/answered).
--
-- Status FSM
-- ----------
-- pending  → answered  (any channel atomically UPDATE WHERE status='pending')
-- pending  → timeout   (the deadline passed and process_timeout fired)
-- pending  → cancelled (the parent task was cancelled)
--
-- Anti-race
-- ---------
-- Cross-channel sync (F4.C) relies on the atomic UPDATE-WHERE-status='pending'
-- pattern in :class:`popolaloom.hitl.sync.HITLStore.mark_answered` — the
-- second write loses the race and surfaces "already answered" to the
-- responder.
--
-- ArkTower upstream collision
-- ---------------------------
-- 005 owns popola_dispatch; 006 is the next migration step. If ArkTower
-- upstream adds 006, rename to 006a_popola_hitl.sql.

CREATE TABLE IF NOT EXISTS popola_hitl (
    -- The unique HITL prompt id (mirrors HITLPrompt.prompt_id when set).
    hitl_id           TEXT    PRIMARY KEY,

    -- HITLTrigger enum (5 values per spec §12.6); CHECK enforced.
    trigger           TEXT    NOT NULL CHECK(
        trigger IN ('round_floor', 'ambiguous_input', 'destructive_op',
                    'approval', 'info_request')
    ),

    -- Status FSM (4-value enum); CHECK enforced.
    status            TEXT    NOT NULL CHECK(
        status IN ('pending', 'answered', 'timeout', 'cancelled')
    ),

    -- The full HITLPrompt model (Pydantic .model_dump_json()) so renderers
    -- can re-render the prompt from this table without re-fetching from
    -- LangGraph state.
    prompt_json       TEXT    NOT NULL,

    -- Timeline (ISO 8601 strings).
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deadline_at       TEXT,
    answered_at       TEXT,

    -- Reply metadata (set when status transitions to 'answered').
    answered_via      TEXT CHECK(
        answered_via IS NULL
        OR answered_via IN ('lark', 'ide', 'cli', 'mcp', 'web', 'email', 'signal', 'cloud')
    ),
    answer_option_id  TEXT,
    answer_reason     TEXT,
    answer_responder_id TEXT,

    -- Lark-specific tracking (per roadmap §12.8.3 R-LARK extensions).
    lark_message_id    TEXT,
    -- JSON array of de-duped Lark event_ids (uniqueness enforced in app code).
    lark_event_ids     TEXT,
    lark_send_attempts INTEGER NOT NULL DEFAULT 0,
    lark_last_send_error TEXT,

    -- Optional link to the dispatching ArkTower task (per D3.6).
    -- ON DELETE SET NULL keeps HITL audit row even when the task is
    -- archived; FK is soft-referenced because ArkTower tables live in
    -- the same DB but per ADR-0001 we avoid hard FKs across owners.
    task_id           TEXT
);

CREATE INDEX IF NOT EXISTS idx_hitl_status         ON popola_hitl(status);
CREATE INDEX IF NOT EXISTS idx_hitl_lark_message   ON popola_hitl(lark_message_id);
CREATE INDEX IF NOT EXISTS idx_hitl_task           ON popola_hitl(task_id);
CREATE INDEX IF NOT EXISTS idx_hitl_created_at     ON popola_hitl(created_at);
CREATE INDEX IF NOT EXISTS idx_hitl_deadline_at    ON popola_hitl(deadline_at);
