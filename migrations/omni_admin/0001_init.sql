-- migrations/omni_admin/0001_init.sql
-- Admin Config Store — source-of-truth cho mọi cấu hình Admin UI (autonomy tier,
-- risk-class override, runtime flag, tenant/api-key, HITL ledger, config audit,
-- CRAT outbox). Idempotent: chạy nhiều lần an toàn (CREATE ... IF NOT EXISTS).
-- Ref: docs/MASTER_PLAN_autonomy_tiers.md §6.5.

CREATE SCHEMA IF NOT EXISTS omni_admin;

-- 1. Tier hiện tại (1 hàng / tenant). Source-of-truth cho OMNI_AUTONOMY_TIER.
CREATE TABLE IF NOT EXISTS omni_admin.autonomy_tier_state (
    tenant_id     TEXT PRIMARY KEY,
    tier          TEXT NOT NULL CHECK (tier IN ('shadow','assist','auto')),
    updated_by    TEXT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    version       BIGINT NOT NULL DEFAULT 1,
    crat_ref      TEXT
);

-- 2. Lịch sử đổi tier (append-only, đối chiếu CRAT chain).
CREATE TABLE IF NOT EXISTS omni_admin.autonomy_tier_history (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    from_tier     TEXT,
    to_tier       TEXT NOT NULL,
    actor         TEXT NOT NULL,
    wilson_lb     DOUBLE PRECISION,
    accepted      INTEGER,
    total         INTEGER,
    elapsed_days  INTEGER,
    forced        BOOLEAN NOT NULL DEFAULT false,
    crat_ref      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_tier_hist_tenant
    ON omni_admin.autonomy_tier_history(tenant_id, created_at DESC);

-- 3. Override risk-class theo tool (mặc định = bảng tĩnh §2; bảng này chỉ chứa override).
CREATE TABLE IF NOT EXISTS omni_admin.risk_class_override (
    tenant_id     TEXT NOT NULL,
    tool_name     TEXT NOT NULL,
    risk_class    TEXT NOT NULL CHECK (risk_class IN ('READONLY','LOW','MEDIUM','HIGH')),
    reason        TEXT,
    updated_by    TEXT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    version       BIGINT NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, tool_name)
);
-- BẤT BIẾN: không cho hạ dangerous_tools xuống dưới HIGH (enforce ở app layer).

-- 4. Runtime flags (auto_execute, siem_suggest_only, HITL timeout, num_ctx, model...).
CREATE TABLE IF NOT EXISTS omni_admin.runtime_flag (
    tenant_id     TEXT NOT NULL,
    flag_key      TEXT NOT NULL,
    flag_value    JSONB NOT NULL,
    value_type    TEXT NOT NULL,
    updated_by    TEXT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    version       BIGINT NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, flag_key)
);

-- 5. Tenant + API keys (thay/bổ sung cho OMNI_TENANT_APIKEYS env tĩnh).
CREATE TABLE IF NOT EXISTS omni_admin.tenant (
    tenant_id     TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS omni_admin.tenant_api_key (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL REFERENCES omni_admin.tenant(tenant_id),
    key_hash      TEXT NOT NULL,
    key_prefix    TEXT NOT NULL,
    label         TEXT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
    created_by    TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_api_key_hash ON omni_admin.tenant_api_key(key_hash);
CREATE INDEX IF NOT EXISTS ix_api_key_tenant
    ON omni_admin.tenant_api_key(tenant_id) WHERE status='active';

-- 6. HITL decision ledger (bổ sung CRAT, để query/UI nhanh).
CREATE TABLE IF NOT EXISTS omni_admin.hitl_decision (
    pending_id    TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    tool_name     TEXT NOT NULL,
    risk_class    TEXT NOT NULL,
    tier_at_time  TEXT NOT NULL,
    decision      TEXT NOT NULL CHECK (decision IN ('PENDING','APPROVED','REJECTED','TIMEOUT')),
    channel       TEXT NOT NULL DEFAULT 'telegram' CHECK (channel IN ('telegram','ui')),
    actor         TEXT,
    crat_ref      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_hitl_tenant_state
    ON omni_admin.hitl_decision(tenant_id, decision, created_at DESC);

-- 7. Audit phổ quát: MỌI write config UI ghi 1 dòng (cùng TX với bảng đích).
CREATE TABLE IF NOT EXISTS omni_admin.config_change_log (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    entity        TEXT NOT NULL,
    entity_key    TEXT,
    action        TEXT NOT NULL,
    old_value     JSONB,
    new_value     JSONB,
    actor         TEXT NOT NULL,
    crat_ref      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_cfg_log_tenant
    ON omni_admin.config_change_log(tenant_id, created_at DESC);

-- 8. CRAT outbox (Transactional Outbox Pattern — atomic tuyệt đối config↔CRAT).
CREATE TABLE IF NOT EXISTS omni_admin.crat_outbox (
    id            BIGSERIAL PRIMARY KEY,
    dedup_key     TEXT NOT NULL UNIQUE,
    event_type    TEXT NOT NULL,
    payload       JSONB NOT NULL,
    status        TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','SENT','FAILED')),
    attempts      INT  NOT NULL DEFAULT 0,
    last_error    TEXT,
    crat_ref      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_crat_outbox_pending
    ON omni_admin.crat_outbox(status, created_at) WHERE status <> 'SENT';

-- Seed: tenant 'default' + tier='shadow' (fail-closed default).
INSERT INTO omni_admin.tenant (tenant_id, display_name)
    VALUES ('default', 'Default')
    ON CONFLICT (tenant_id) DO NOTHING;
INSERT INTO omni_admin.autonomy_tier_state (tenant_id, tier, updated_by)
    VALUES ('default', 'shadow', 'migration:0001')
    ON CONFLICT (tenant_id) DO NOTHING;
