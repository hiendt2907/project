-- migrations/omni_admin/0005_agent_enrollment.sql
-- IT-3 sprint "Nhân viên SRE": enrollment + identity per-agent.
-- Machine path (Remote Agent) — TÁCH BIỆT human portal identity (0004).
--
-- Flow: admin phát one-time enroll token (lưu HASH) → agent/installer đổi token
-- lấy credential per-agent (lưu HASH, plaintext trả đúng 1 lần) → tenant binding
-- bền vững ở PG. Thay thế OMNI_TENANT_APIKEYS tĩnh dùng chung cho agent.
-- Idempotent (CREATE ... IF NOT EXISTS). Ref: docs/plans/sprint-agent-sre-employee-production.md IT-3.

CREATE SCHEMA IF NOT EXISTS omni_admin;

-- 1. One-time enroll token. status: issued → used (đúng 1 lần) | revoked.
--    Gotcha FK (post-mortem drift-correction-2026-07-02): tenant PHẢI được
--    provision qua create_tenant() trước khi phát token.
CREATE TABLE IF NOT EXISTS omni_admin.agent_enroll_token (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      TEXT NOT NULL REFERENCES omni_admin.tenant(tenant_id),
    token_hash     TEXT NOT NULL,               -- sha256(plaintext), không bao giờ lưu raw
    token_prefix   TEXT NOT NULL,               -- 8 ký tự đầu để operator nhận diện
    label          TEXT,
    status         TEXT NOT NULL DEFAULT 'issued'
                       CHECK (status IN ('issued','used','revoked')),
    created_by     TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ,                 -- NULL = không hết hạn (lab)
    used_at        TIMESTAMPTZ,
    used_by_agent  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_enroll_token_hash
    ON omni_admin.agent_enroll_token(token_hash);
CREATE INDEX IF NOT EXISTS ix_enroll_token_tenant
    ON omni_admin.agent_enroll_token(tenant_id) WHERE status = 'issued';

-- 2. Per-agent credential — thay OMNI_AGENT_API_KEY tenant-shared.
--    Mỗi (tenant, agent) tối đa 1 credential active; re-enroll revoke bản cũ.
CREATE TABLE IF NOT EXISTS omni_admin.agent_credential (
    id                 BIGSERIAL PRIMARY KEY,
    tenant_id          TEXT NOT NULL REFERENCES omni_admin.tenant(tenant_id),
    agent_id           TEXT NOT NULL,
    hostname           TEXT NOT NULL DEFAULT '',
    key_hash           TEXT NOT NULL,           -- sha256(plaintext)
    key_prefix         TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active','revoked')),
    enrolled_via_token BIGINT REFERENCES omni_admin.agent_enroll_token(id),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at         TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_credential_hash
    ON omni_admin.agent_credential(key_hash);
CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_credential_active
    ON omni_admin.agent_credential(tenant_id, agent_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS ix_agent_credential_tenant
    ON omni_admin.agent_credential(tenant_id) WHERE status = 'active';
