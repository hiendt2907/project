-- Tenant service plan / entitlement source-of-truth.
-- Runtime enforcement reads this table; the portal is only an operator view.
CREATE TABLE IF NOT EXISTS omni_admin.tenant_plan (
    tenant_id          TEXT PRIMARY KEY REFERENCES omni_admin.tenant(tenant_id),
    plan_code          TEXT NOT NULL DEFAULT 'standard',
    agent_limit        INTEGER NOT NULL DEFAULT 10 CHECK (agent_limit >= 0),
    autonomy_ceiling   TEXT NOT NULL DEFAULT 'assist'
                       CHECK (autonomy_ceiling IN ('shadow','assist','auto')),
    retention_days     INTEGER NOT NULL DEFAULT 30 CHECK (retention_days > 0),
    support_tier       TEXT NOT NULL DEFAULT 'standard',
    enabled            BOOLEAN NOT NULL DEFAULT true,
    updated_by         TEXT NOT NULL DEFAULT 'migration:0009',
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    version            BIGINT NOT NULL DEFAULT 1
);

INSERT INTO omni_admin.tenant_plan (tenant_id)
SELECT tenant_id FROM omni_admin.tenant
ON CONFLICT (tenant_id) DO NOTHING;
