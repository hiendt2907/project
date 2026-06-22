-- migrations/omni_admin/0003_tenant_readiness.sql
-- Onboarding readiness checklist (step-3 của plan agent/plans/PLAN_onboarding_ops_agent.md).
-- Postgres = source-of-truth cho readiness_flag (step-4 đọc qua is_tenant_ready());
-- Redis = write-through cache (TTL ngắn), theo đúng pattern resolve_tier hiện có.

CREATE TABLE IF NOT EXISTS omni_admin.tenant_readiness_state (
    tenant_id                       TEXT PRIMARY KEY REFERENCES omni_admin.tenant(tenant_id),
    endpoint_mapped_pct             NUMERIC,
    business_flow_confirmed_pct     NUMERIC,
    open_questions_over_threshold   INT NOT NULL DEFAULT 0,
    readiness_flag                  BOOLEAN NOT NULL DEFAULT false,
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE omni_admin.tenant ADD COLUMN IF NOT EXISTS telegram_chat_id BIGINT;

-- Ngưỡng readiness (endpoint_mapped_pct >= X%, business_flow_confirmed_pct >= Y%,
-- open_questions_over_threshold <= Z) đọc từ omni_admin.runtime_flag, key
-- 'readiness_threshold:{tenant_id}' (per-tenant override) hoặc
-- 'readiness_threshold:default' (global) — KHÔNG hardcode trong code ứng dụng.
