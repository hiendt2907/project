-- Playbook Engine schema migration
-- Run against the Omni RAG Postgres (same cluster as pgvector store).
-- Idempotent: safe to run multiple times.
--
-- Usage:
--   PGPASSWORD=$OMNI_DB_PASSWORD psql -h pgpool-gateway -U appuser -d ragdb -f scripts/migrate_playbook_schema.sql

BEGIN;

-- ---------------------------------------------------------------
-- Table: playbooks
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS playbooks (
    playbook_id     TEXT PRIMARY KEY,
    version         TEXT NOT NULL DEFAULT '1',
    name            TEXT NOT NULL,
    -- severity_filter: 'critical' | 'warning' | 'info' | '' (matches all)
    severity_filter TEXT NOT NULL DEFAULT '',
    approved_by     TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------
-- Table: playbook_steps
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS playbook_steps (
    playbook_id     TEXT NOT NULL REFERENCES playbooks(playbook_id) ON DELETE CASCADE,
    step_order      INT  NOT NULL,
    -- action_type maps to Omni executor tool names (K8S_SDK_MUTATING_TOOL_NAMES)
    action_type     TEXT NOT NULL,
    -- target: namespace/deployment or other scoping info
    target          TEXT NOT NULL DEFAULT '',
    -- params: arbitrary JSON args for the action
    params          JSONB NOT NULL DEFAULT '{}',
    timeout_sec     INT  NOT NULL DEFAULT 60,
    requires_hitl   BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (playbook_id, step_order)
);

-- ---------------------------------------------------------------
-- Table: playbook_category_map
-- Maps SIEM incident categories (from siem_bridge.py) to playbook IDs.
-- Multiple categories can map to the same playbook.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS playbook_category_map (
    siem_category   TEXT NOT NULL,
    playbook_id     TEXT NOT NULL REFERENCES playbooks(playbook_id) ON DELETE CASCADE,
    PRIMARY KEY (siem_category, playbook_id)
);

-- ---------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_playbook_category ON playbook_category_map(siem_category);
CREATE INDEX IF NOT EXISTS idx_playbook_steps_order ON playbook_steps(playbook_id, step_order);

COMMIT;

-- Verification
SELECT 'playbooks' AS tbl, count(*) FROM playbooks
UNION ALL
SELECT 'playbook_steps', count(*) FROM playbook_steps
UNION ALL
SELECT 'playbook_category_map', count(*) FROM playbook_category_map;
