-- migrations/omni_admin/0002_playbook.sql
-- L4 playbook-first: playbook catalog (record-of-config) + graduation state.
-- Hot-path đọc Redis (PlaybookStore + PlaybookGovernor write-through); Postgres =
-- source-of-truth cho Admin UI + audit đối chiếu. Idempotent.

CREATE TABLE IF NOT EXISTS omni_admin.playbook (
    playbook_id   TEXT NOT NULL,
    version       BIGINT NOT NULL DEFAULT 1,
    domain        TEXT NOT NULL CHECK (domain IN ('k8s','os','network','service','application','api','hardware')),
    name          TEXT NOT NULL,
    spec          JSONB NOT NULL,           -- PlaybookSpec serialized
    approved_by   TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (playbook_id, version)
);
CREATE INDEX IF NOT EXISTS ix_playbook_domain ON omni_admin.playbook(domain);

-- Graduation state per tenant×domain×playbook (DRAFT/CANDIDATE/GRADUATED/FROZEN).
CREATE TABLE IF NOT EXISTS omni_admin.playbook_graduation (
    tenant_id     TEXT NOT NULL,
    domain        TEXT NOT NULL,
    playbook_id   TEXT NOT NULL,
    state         TEXT NOT NULL CHECK (state IN ('DRAFT','CANDIDATE','GRADUATED','FROZEN')),
    success_count INTEGER NOT NULL DEFAULT 0,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    last_outcome  TEXT,
    updated_by    TEXT NOT NULL DEFAULT 'system',
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    crat_ref      TEXT,
    PRIMARY KEY (tenant_id, domain, playbook_id)
);

-- Append-only history (đối chiếu CRAT PLAYBOOK_GRADUATED/DEMOTED).
CREATE TABLE IF NOT EXISTS omni_admin.playbook_graduation_history (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    domain        TEXT NOT NULL,
    playbook_id   TEXT NOT NULL,
    from_state    TEXT,
    to_state      TEXT NOT NULL,
    reason        TEXT,
    actor         TEXT NOT NULL DEFAULT 'system',
    crat_ref      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_pb_grad_hist
    ON omni_admin.playbook_graduation_history(tenant_id, playbook_id, created_at DESC);
