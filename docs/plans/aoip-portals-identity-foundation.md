# AOIP Portals — Identity & Tenancy Foundation (Slice 0)

> Repo artifact (INV: engineering docs live in source control, không phải Claude memory).
> Bắt buộc đọc trước khi code Slice 0. Cập nhật khi contract đổi.

## 0. Reviewer correction đã chốt (2026-07-01)

Hai đường danh tính **TÁCH BIỆT**, không dùng chung authority:

```
# HUMAN (portal users)                    # MACHINE (remote agents / svc-to-svc)
OIDC subject                              Agent credential / API key
  → internal user                           → agent identity
  → provider roles + tenant memberships     → server-side tenant binding
  → scoped permissions                      → (giữ nguyên gateway/tenant_context.py)
  → secure server-side session
```

- Tenant context của portal API đến từ **membership server-side của user đã xác thực**,
  KHÔNG từ tenant_id do browser gửi, KHÔNG từ agent API key.
- `OMNI_TENANT_APIKEYS` / `omni:agent:tenant` = **machine authority**, KHÔNG dùng cho portal user.
- Standards-based (Authorization Code + PKCE), KHÔNG phụ thuộc claim riêng của Keycloak.
  Keycloak/Dex chỉ là OIDC provider dev/self-host thật.
- Cookie: `HttpOnly`, `Secure`, `SameSite`; KHÔNG để access token nhạy cảm trong localStorage.

## 1. Khảo sát cấu trúc auth hiện có (đã inspect)

| Thành phần | File | Trạng thái | Quyết định |
|---|---|---|---|
| NextAuth v4 shell | `ui/lib/auth.ts` | CredentialsProvider, 1 admin hardcode, session JWT, cookie HttpOnly/Secure | **Tái dùng toolchain**; THAY provider bằng OIDC (Authorization Code+PKCE). Bỏ admin hardcode. |
| Session type | `ui/types/next-auth.d.ts` | chỉ `{id,name,email,role}` | **Mở rộng**: thêm `kind`, `providerRoles`, `memberships[]`, `activeTenant`. |
| Realm theo host | `ui/middleware.ts`, `ui/lib/omni-ui-realm.ts` | portal/ops/local redirect theo prefix | Mầm 2 portal; **nâng** thành 2 app-shell + route-space riêng. |
| Agent tenant binding | `src/gateway/tenant_context.py` | `TenantContext(tenant_id,is_admin)`, `require_agent_tenant` | **GIỮ NGUYÊN** — machine path, không đụng. |
| AOIP authz (placeholder) | `src/aoip/console/authz.py` | `PrincipalRegistry` token→Principal in-memory | **THAY** bằng session-verified identity + membership resolver. |
| AOIP console API | `src/aoip/console/app.py` | `/api/{provider,tenant}/*`, chưa versioned | **Version /v1**; nguồn Principal từ session, không từ registry token. |
| Product persistence (human user/membership) | — | CHƯA CÓ | Thêm migration source-controlled (PG omni_admin). |

## 2. Persistence mới (chỉ phần thiếu)

Thêm vào PG `omni_admin` (migration source-controlled):

- `portal_user(id, oidc_subject UNIQUE, email, display_name, created_at, disabled)`
- `provider_role(user_id, role)` — provider-side roles (platform_owner…)
- `tenant_membership(user_id, tenant_id, role)` — tenant-side membership (sre_lead…)
- `auth_audit(id, ts, subject, event, ip, ua, tenant_id, detail)` — login/logout/denied/support-access.

Resolver server-side: `oidc_subject → portal_user → {provider_roles} ∪ {memberships}` →
`Principal(kind, roles, tenant?)`. Tenant portal API: tenant = membership của session, KHÔNG nhận từ client.

## 3. Delivery cadence (đã chốt) — vertical slices, không backend-only phase dài

### Slice 0 — Identity & tenancy foundation *(đang làm)*
Backend: OIDC login/callback; secure server-side session; logout+revocation; provider roles;
tenant memberships; backend RBAC; `/api/provider/v1/*`; `/api/tenant/v1/*`; tenant isolation cho
REST/SSE/audit/export; auth+authz audit.
Frontend: Provider Portal + Tenant Portal login/logout + app shell thật; hiển thị identity/roles/
active-org; trạng thái session-expired/unauthorized/forbidden; KHÔNG fixture, KHÔNG hardcode.
**DoD:** provider-user thật + tenant-user thật đăng nhập được vào đúng portal, nhận session scoped
đúng, backend chứng minh RBAC + tenant isolation (test tự động). Không phải "backend xong là xong".

### Slice 1 — Fleet visibility
Provider: tenant list/detail + global agent fleet + heartbeat/offline/version.
Tenant: chỉ agent của tenant mình + heartbeat/host-metadata + **test tự động chứng minh không đọc/
subscribe được dữ liệu tenant khác**.

### Slice 2 — Incident operations
Provider: incident summaries across authorized tenants + command delivery + execution phase +
lease/reconcile state + Gateway/report status.
Tenant: reasoning + evidence + blast radius + Decision + bounded Approval + execution + verification
+ final correlated timeline (cùng correlation_id với provider view).

## 4. Bất biến kéo dài (an ninh)

- fail-closed; mutation chỉ qua executor; analyst read-only; CRAT audit MUST succeed trước emit.
- tenant identity server-side ONLY; chặn cross-tenant qua REST/WS/cache/export/audit/approval/object-id.
- authz backend-enforced (ẩn menu KHÔNG phải authz); không hardcode user / trusted browser header.
- RuntimeTrace = read model, không nguồn sự thật thứ hai, không drive mutation.
- OrbStack = LAB ONLY; không commit trừ khi được chỉ thị.
</content>
</invoke>
