-- migrations/omni_admin/0006_agent_command_ledger.sql
-- IT-6 sprint "Nhân viên SRE": command outcome durability — PG là bản ghi bền vững
-- (survive Redis flush/gateway restart) cho mutating recovery command (ADR-002).
--
-- KHÔNG FK tới omni_admin.tenant (khác 0005): đây là ledger sự kiện đã-xảy-ra, ghi
-- outcome phải THÀNH CÔNG kể cả khi tenant registry drift — mất outcome vì FK
-- violation tệ hơn một dòng orphan (xem post-mortem drift-correction-2026-07-02).
-- Bất biến: mỗi (tenant_id, command_id) có ĐÚNG MỘT terminal outcome — enforce bằng
-- PK + UPDATE điều kiện terminal_at IS NULL (first-writer-wins, xem ledger.py).
-- Idempotent (CREATE ... IF NOT EXISTS).

CREATE SCHEMA IF NOT EXISTS omni_admin;

CREATE TABLE IF NOT EXISTS omni_admin.agent_command_outcome (
    tenant_id        TEXT NOT NULL,
    command_id       TEXT NOT NULL,
    agent_id         TEXT NOT NULL,
    mission_id       TEXT NOT NULL DEFAULT '',
    incident_id      TEXT NOT NULL DEFAULT '',
    decision_id      TEXT NOT NULL DEFAULT '',
    action_id        TEXT NOT NULL DEFAULT '',
    canonical_scope  TEXT NOT NULL DEFAULT '',
    payload_hash     TEXT NOT NULL DEFAULT '',
    state            TEXT NOT NULL,               -- QUEUED | ... | COMPLETED/FAILED/ESCALATED/EXPIRED
    outcome          JSONB NOT NULL DEFAULT '{}'::jsonb,
    delivery_attempt INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),  -- lúc enqueue (theo record Redis)
    terminal_at      TIMESTAMPTZ,                 -- NULL = chưa terminal
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),  -- lần ghi PG gần nhất
    source           TEXT NOT NULL DEFAULT 'gateway',     -- gateway | reconcile
    PRIMARY KEY (tenant_id, command_id)
);

CREATE INDEX IF NOT EXISTS ix_cmd_outcome_agent
    ON omni_admin.agent_command_outcome(tenant_id, agent_id);
CREATE INDEX IF NOT EXISTS ix_cmd_outcome_open
    ON omni_admin.agent_command_outcome(tenant_id) WHERE terminal_at IS NULL;
