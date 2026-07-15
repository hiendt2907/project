-- Bind newly enrolled agents to a provider-managed environment.
-- NULL is retained for pre-0008 credentials and is treated as legacy scope.

ALTER TABLE omni_admin.agent_enroll_token
    ADD COLUMN IF NOT EXISTS environment_id TEXT;

ALTER TABLE omni_admin.agent_credential
    ADD COLUMN IF NOT EXISTS environment_id TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_enroll_token_environment'
    ) THEN
        ALTER TABLE omni_admin.agent_enroll_token
            ADD CONSTRAINT fk_enroll_token_environment
            FOREIGN KEY (tenant_id, environment_id)
            REFERENCES omni_admin.environment(tenant_id, environment_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_agent_credential_environment'
    ) THEN
        ALTER TABLE omni_admin.agent_credential
            ADD CONSTRAINT fk_agent_credential_environment
            FOREIGN KEY (tenant_id, environment_id)
            REFERENCES omni_admin.environment(tenant_id, environment_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_agent_credential_environment
    ON omni_admin.agent_credential(tenant_id, environment_id) WHERE status = 'active';
