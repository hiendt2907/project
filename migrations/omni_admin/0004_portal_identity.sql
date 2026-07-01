-- migrations/omni_admin/0004_portal_identity.sql
-- Human portal identity — source-of-truth cho user/role/membership của HAI portal
-- (Provider Operations + Customer/Tenant Operations). TÁCH BIỆT machine path
-- (omni_admin.tenant_api_key / OMNI_TENANT_APIKEYS) vốn dành cho Remote Agent + S2S.
--
-- Đường human: OIDC subject → portal_user → provider_role ∪ tenant_membership → Principal.
-- Redis chỉ giữ session opaque + revocation cache; PG là nguồn sự thật bền vững.
-- Idempotent (CREATE ... IF NOT EXISTS). Ref: docs/plans/aoip-portals-identity-foundation.md.

CREATE SCHEMA IF NOT EXISTS omni_admin;

-- 1. Portal user (con người). disabled = khoá đăng nhập tức thời (re-resolve mỗi request).
CREATE TABLE IF NOT EXISTS omni_admin.portal_user (
    subject       TEXT PRIMARY KEY,            -- OIDC `sub` (chuẩn, không phụ thuộc Keycloak)
    email         TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    disabled      BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. Liên kết OIDC (một user có thể đăng nhập từ >1 issuer/provider-neutral).
CREATE TABLE IF NOT EXISTS omni_admin.oidc_identity_link (
    issuer        TEXT NOT NULL,
    oidc_sub      TEXT NOT NULL,               -- `sub` do issuer cấp
    subject       TEXT NOT NULL REFERENCES omni_admin.portal_user(subject) ON DELETE CASCADE,
    email         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (issuer, oidc_sub)
);

-- 3. Provider role assignment (platform_owner/operator/support_engineer/security_auditor/viewer).
CREATE TABLE IF NOT EXISTS omni_admin.provider_role_assignment (
    subject       TEXT NOT NULL REFERENCES omni_admin.portal_user(subject) ON DELETE CASCADE,
    role          TEXT NOT NULL CHECK (role IN
                    ('platform_owner','platform_operator','support_engineer',
                     'security_auditor','provider_viewer')),
    granted_by    TEXT NOT NULL,
    granted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (subject, role)
);

-- 4. Tenant membership — nguồn DUY NHẤT xác định tenant của portal user (server-side).
--    Client KHÔNG BAO GIỜ tự cung cấp tenant_id.
CREATE TABLE IF NOT EXISTS omni_admin.tenant_membership (
    subject       TEXT NOT NULL REFERENCES omni_admin.portal_user(subject) ON DELETE CASCADE,
    tenant_id     TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN
                    ('tenant_owner','sre_lead','operator','approver','auditor','viewer')),
    granted_by    TEXT NOT NULL,
    granted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (subject, tenant_id)
);
CREATE INDEX IF NOT EXISTS ix_membership_tenant
    ON omni_admin.tenant_membership(tenant_id);

-- 5. Support access grant — provider truy cập raw evidence của 1 tenant phải có grant + audit.
CREATE TABLE IF NOT EXISTS omni_admin.support_access_grant (
    id            BIGSERIAL PRIMARY KEY,
    subject       TEXT NOT NULL REFERENCES omni_admin.portal_user(subject) ON DELETE CASCADE,
    tenant_id     TEXT NOT NULL,
    reason        TEXT NOT NULL,
    granted_by    TEXT NOT NULL,
    granted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    revoked_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_support_grant_active
    ON omni_admin.support_access_grant(subject, tenant_id)
    WHERE revoked_at IS NULL;

-- 6. Auth/authz audit bền vững (mirror của Redis edge-buffer portal:auth_audit).
--    Events: LOGIN, LOGOUT, DENIED, SUPPORT_ACCESS, ROLE_GRANTED, ROLE_REVOKED.
CREATE TABLE IF NOT EXISTS omni_admin.portal_auth_audit (
    id            BIGSERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    event         TEXT NOT NULL,
    subject       TEXT NOT NULL,
    tenant_id     TEXT,
    detail        TEXT NOT NULL DEFAULT '',
    ip            TEXT NOT NULL DEFAULT '',
    ua            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_auth_audit_subject
    ON omni_admin.portal_auth_audit(subject, ts DESC);
CREATE INDEX IF NOT EXISTS ix_auth_audit_tenant
    ON omni_admin.portal_auth_audit(tenant_id, ts DESC);
