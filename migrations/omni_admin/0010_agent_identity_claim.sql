-- migrations/omni_admin/0010_agent_identity_claim.sql
-- Phase 3 (0-6 roadmap): durable agent_id ownership, independent of the
-- ephemeral Redis registry (omni:remote_agent:registry:{agent_id}, TTL=120s).
--
-- Gap this closes: omni_admin.agent_credential (0005) already durably binds
-- (tenant_id, agent_id) for hosts enrolled via IT-3 per-agent credentials —
-- but two of the three real lab hosts (cust-edge, cust-db) still authenticate
-- with a tenant-SHARED key (no per-agent credential row at all). For those,
-- ownership of an agent_id was ENTIRELY determined by "whoever's registry key
-- is currently live in Redis" — a 120s network blip or VM reboot creates a
-- window where a different tenant's shared key could register the SAME
-- agent_id string and become its new "owner" until the original host
-- re-registers, at which point IT gets rejected (403) instead. Confirmed
-- live 2026-07-21 that this affects real fleet hosts today, not a
-- hypothetical.
--
-- This table is a first-claim-wins, durable, no-TTL identity lock — it does
-- NOT replace agent_credential (which remains the stronger, cryptographic
-- binding for enrolled agents); it only closes the TOFU-on-every-TTL-expiry
-- gap for tenant-shared-key deployments. Idempotent (CREATE ... IF NOT EXISTS).

CREATE SCHEMA IF NOT EXISTS omni_admin;

CREATE TABLE IF NOT EXISTS omni_admin.agent_identity_claim (
    agent_id        TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL REFERENCES omni_admin.tenant(tenant_id),
    first_claimed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_agent_identity_claim_tenant
    ON omni_admin.agent_identity_claim(tenant_id);
