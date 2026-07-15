-- Provider-managed environment lifecycle.
-- PostgreSQL is the source of truth; Redis is not used for lifecycle state.

CREATE TABLE IF NOT EXISTS omni_admin.environment (
    environment_id TEXT NOT NULL,
    tenant_id      TEXT NOT NULL REFERENCES omni_admin.tenant(tenant_id),
    display_name   TEXT NOT NULL,
    environment_type TEXT NOT NULL CHECK (environment_type IN ('production','staging','development')),
    status         TEXT NOT NULL DEFAULT 'onboarding'
                   CHECK (status IN ('onboarding','active','suspended','archived')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, environment_id)
);

CREATE INDEX IF NOT EXISTS ix_environment_tenant_status
    ON omni_admin.environment(tenant_id, status, created_at DESC);
