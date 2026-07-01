# AOIP Portals — Slice 0 (Identity & Tenancy Foundation) · K8s / OrbStack

Hai portal **production** loginable qua OIDC, deploy lên K8s namespace `multi-agent`
(OrbStack). Backend AOIP là **auth authority duy nhất** (không NextAuth thứ hai).
Danh tính/role/tenant suy ra **server-side** từ membership; client không cung cấp
tenant ID hay suy quyền từ token.

```
aoip-dex (OIDC issuer, 1 issuer / 2 client)  — dex.ai-agent.local
   │  Authorization Code + PKCE, id_token RS256 (verify iss/aud/exp/nonce)
   ▼
Next.js apps (UI, CSP nonce riêng):  aoip-provider-web (:3001)  ·  aoip-tenant-web (:3002)
   │  ingress same-origin: "/" → Next;  "/auth" + "/api/*/v1" → FastAPI backend (đặt cookie)
   ▼
FastAPI auth authority:  aoip-provider-portal (:8081)  ·  aoip-tenant-portal (:8082)
   │  session opaque HttpOnly cookie RIÊNG (aoip_provider_session / aoip_tenant_session, svc `redis`)
   │  Next đọc /me SERVER-SIDE qua AOIP_BACKEND_URL — không NextAuth, không session thứ hai
   ▼
Postgres omni_admin (svc `omni-postgres`, secret omni-pg-secret, DB omnidb)
  portal_user / provider_role / tenant_membership / support_grant / auth_audit  (migration 0004)

Frontend monorepo: ui/ (npm workspace)
  apps/provider-portal · apps/tenant-portal   (entry/route/nav/CSP/deploy RIÊNG)
  packages/ ui-kit · api-client · auth-client · observability · shared-types  (dùng chung, KHÔNG chứa authz/disclosure)
```

Tái dùng hạ tầng cluster sẵn có: image `multi-agent-system:latest`, svc `redis`,
svc `omni-postgres`. Không dựng Postgres/Redis riêng.

## Deploy

```bash
# 1) Backend (FastAPI auth authority) + Dex + seed
make docker-worker                                   # build multi-agent-system:latest
kubectl apply -f k8s/deployments/aoip-dex.yaml
kubectl apply -f k8s/deployments/aoip-portals.yaml   # lifespan tự migrate omni_admin + hydrate
kubectl -n multi-agent rollout status deploy/aoip-provider-portal deploy/aoip-tenant-portal
kubectl apply -f k8s/jobs/aoip-seed-identity.yaml    # user/role/membership THẬT trong PG (CHỈ-DEV)
kubectl -n multi-agent wait --for=condition=complete job/aoip-seed-identity --timeout=120s

# 2) Next.js portals (UI) — build từ workspace ui/
cd ui
docker build -t aoip-provider-web:latest -f apps/provider-portal/Dockerfile .
docker build -t aoip-tenant-web:latest   -f apps/tenant-portal/Dockerfile .
cd ..
kubectl apply -f k8s/deployments/aoip-portals-web.yaml
kubectl apply -f k8s/ingress/aoip-portals.yaml       # "/"→Next, "/auth"+"/api"→FastAPI
kubectl -n multi-agent rollout status deploy/aoip-provider-web deploy/aoip-tenant-web
```

Thêm hosts (một lần) vào `/etc/hosts` → `127.0.0.1`:

```
127.0.0.1 provider.ai-agent.local tenant.ai-agent.local dex.ai-agent.local
```

Kiểm tra:

```bash
curl -s http://provider.ai-agent.local/api/provider/v1/me      # 401 (chưa login) → đúng
curl -s http://dex.ai-agent.local/dex/.well-known/openid-configuration | head -c 80
```

## Đăng nhập để kiểm tra

Trình duyệt (ẩn danh riêng mỗi portal để cookie không lẫn) · mật khẩu chung `Password123!`:

### Provider Operations Portal — http://provider.ai-agent.local
| Email | Vai trò | Thấy gì |
|---|---|---|
| `owner@aoip.dev` | `platform_owner` | Toàn quyền provider (mọi permission) |
| `support@aoip.dev` | `support_engineer` | Raw-evidence **chỉ khi** có support-grant còn hiệu lực |

### Tenant Operations Portal — http://tenant.ai-agent.local
| Email | Tenant / Vai trò | Thấy gì |
|---|---|---|
| `sre@acme.dev` | `acme` / `tenant_owner` | Chỉ tổ chức `acme` |
| `approver@acme.dev` | `acme` / `approver` | Chỉ `acme`, quyền phê duyệt (Slice 2) |
| `sre@globex.dev` | `globex` / `sre_lead` | Chỉ `globex` — không thấy `acme` |

Luồng: mở portal → **Đăng nhập bằng OIDC** → Dex (email + `Password123!`) → quay lại
với phiên đúng scope. Shell hiển thị danh tính, vai trò, quyền, tổ chức đang hoạt động;
nút **Đăng xuất** thu hồi phiên máy chủ.

## Chứng minh DoD (thử tay)

1. **Provider↔Tenant** — đăng nhập chéo portal → 403 (không role hợp lệ cho portal đó).
2. **Tenant A ≠ B** — `sre@globex.dev`: `GET /api/tenant/v1/incidents` chỉ trả `globex`;
   đoán `correlation_id` của `acme` → 403 + audit `DENIED`.
3. **Thu hồi role tức thời** — khi `owner@aoip.dev` đang đăng nhập:
   ```bash
   kubectl -n multi-agent exec deploy/omni-postgres -- psql -U omni -d omnidb \
     -c "DELETE FROM omni_admin.provider_role_assignment WHERE subject='owner@aoip.dev';"
   ```
   Reload portal → phiên vô hiệu ngay (re-resolve server-side; PG mirror→Redis, không tin claim frontend).
4. **Logout** — xoá `portal:session:{sid}` (Redis); request sau → 401.
5. **Raw-evidence cần grant** — `support@aoip.dev` mở incident `?raw=true` → 403 tới khi có
   `support_access_grant` còn hiệu lực → khi có: trả raw + ghi `SUPPORT_ACCESS` vào PG audit.
6. **Sống sót restart** — `kubectl -n multi-agent rollout restart deploy/aoip-provider-portal`
   (và/hoặc restart redis) → hydrate lại từ PG, user/role/membership còn nguyên (DoD #7).

## Bảo mật (đã bật)
Cookie `HttpOnly`+`SameSite=Lax` (+`Secure` khi HTTPS) · CSRF Origin allow-list cho mutation ·
CSP/`X-Frame-Options: DENY`/`nosniff`/HSTS/Referrer-Policy · redirect_uri allow-list ở Dex ·
access/id token **không** vào browser (chỉ sid opaque).

## Development vs Production (trung thực)

OrbStack ở đây là **DEV cluster — "production-shaped development", KHÔNG phải production.**
Các mục CHỈ-DEV, phải cô lập khỏi production:

| Chỉ-DEV (hiện tại) | Production BẮT BUỘC |
|---|---|
| Dex staticPasswords (owner@aoip.dev…) | Nối IdP thật (Keycloak/Okta/Entra), **không user mặc định** |
| Mật khẩu chung `Password123!` | **Không mật khẩu chung**; MFA theo policy IdP |
| `/etc/hosts` + HTTP | DNS thật + **HTTPS** (TLS ở Traefik/ingress) |
| Job `aoip-seed-identity` | **Tắt seed job**; user/role/membership cấp qua admin flow |
| secret client trong ConfigMap/env | **Secret injection thật** (Vault/K8s Secret + sealed) |

Chuyển production: đổi `AOIP_OIDC_*_REDIRECT_URI`→`https://…` (cookie tự `Secure`) ·
đặt redirect_uri allow-list đúng ở IdP · `AOIP_*_ORIGINS`=host HTTPS · KHÔNG apply
`k8s/jobs/aoip-seed-identity.yaml` · thay Dex ConfigMap bằng client bí mật tiêm qua Secret.

## Kiểm thử tự động (bằng chứng Slice 0)

Browser E2E (Playwright, Chromium) — `tests/e2e_portals/` — chạy trực tiếp trên K8s,
verify HAI app **Next.js** thật (`provider-portal`/`tenant-portal`) — **8/8 pass**:
css áp dụng + `/me` SSR resolve + identity render (**CSP nonce bật, 0 lỗi CSP**) ·
logout thu hồi phiên máy chủ · 401/403/forbidden state render · cross-portal 403 ·
tenant chỉ thấy tenant mình · **cookie 2 portal không đụng nhau** (không cần ẩn danh) ·
token không vào browser storage · session-expiry → 401 (không tin claim frontend).

```bash
cd tests/e2e_portals && npm install && npx playwright install chromium && npx playwright test
```
