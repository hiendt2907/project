# AOIP Provider Portal — Production Vertical Slices (Blueprint + Ledger)

> Repo artifact = source of truth (INV: engineering docs sống trong source control, không phải
> conversation/Claude memory). Cập nhật ledger sau MỖI sub-slice. Ref khảo sát nguồn:
> `docs/plans/aoip-portals-identity-foundation.md` (Slice 0 identity foundation đã xong).

## ⚠️ GOVERNING CORRECTION (2026-07-01, ưu tiên cao nhất — override roadmap B→G product bên dưới)

Dự án backend/runtime là sản phẩm chính. Portal = **operational projection** của capability
backend đã có, KHÔNG phải product portal. **NGỪNG mở rộng product scope.**

- Mỗi UI element PHẢI map: UI → API/read-projection → runtime object/store → evidence/audit.
  Không có nguồn backend thật ⇒ KHÔNG hiển thị (không placeholder metric, không fixture, không
  frontend-invented state). Chỉ optionally show "backend not supported" khi hữu ích vận hành.
- KHÔNG frontend-led domain (license/billing/CRM/deployment/policy-editor/onboarding-wizard/
  config screen) trừ khi backend tương ứng đã production-ready.
- **Nav chỉ vùng runtime-backed:** Overview · Customers · Pipeline · KPI · Agents · Understanding ·
  Missions · Incidents · Operations · Human Inbox · Settings · Audit · Account · Gói dịch vụ.
  Thêm vùng mới CHỈ khi backend capability tồn tại + enforced + có data thật. Gói dịch vụ được
  thêm sau khi `tenant_plan` có API/RBAC/audit và enrollment/autonomy runtime enforcement.
- **Backend-first:** tiếp tục Living Operations Runtime roadmap (Gateway/systemd long-running loop,
  durable command delivery+ack, explicit execution phases, crash reconciliation, lease renewal/
  fencing, continuous observation, mission resume sau restart, E2E audit correlation). Mỗi khi
  backend thêm state/capability → expose ngay qua read model + hai portal.
- UI DoD: (1) data từ nguồn backend thật; (2) khớp canonical backend state; (3) evidence+timestamp
  hiển thị; (4) error/stale/unavailable tường minh; (5) trace ngược về runtime/audit; (6) không mock/
  fixture/invented business state.

**Hệ quả với ledger bên dưới:** roadmap product B→G (Tenant Lifecycle/License/Enrollment wizard…)
**KHÔNG còn là hướng đi**; chỉ triển khai phần map tới capability runtime đã có. Nav product 15-mục ở §3 đã
thay bằng các vùng runtime-backed. Các file product-domain đã tạo (route stubs customers/licenses/… + overview.py
metric license/version-drift đã gỡ) **giữ trên đĩa để tái dùng sau**, không liệt kê trong nav.

## 0. Nguyên tắc thực thi (đã chốt với người dùng 2026-07-01)

- KHÔNG backend-only phase dài rồi UI sau. **Mỗi sub-slice là một vertical slice production**:
  migration + backend API + backend RBAC + tenant isolation + idempotency (khi có mutation) +
  audit + Provider Portal UI + loading/empty/error/forbidden states + API & browser E2E test +
  Kubernetes deployment proof + cập nhật roadmap/ledger/changelog.
- KHÔNG mock/fixture product data. Metric thiếu nguồn → render **unavailable + lý do khe hở**.
- Ẩn menu KHÔNG phải authz. Mọi route enforce ở backend; từ chối = audit.
- Danh tính từ SESSION server-side; tenant từ membership server-side. Không tin client.
- Dừng checkpoint sau mỗi sub-slice (verify), không dừng để bàn kế hoạch lại. Không hỏi lại
  sequencing trừ khi gặp mâu thuẫn kiến trúc thật hoặc migration phá huỷ.

## 1. Kiến trúc hiện có (khảo sát 2026-07-01, 4 subagent) — nguồn tái dùng

| Miền | Nguồn thật đã có | File |
|---|---|---|
| Identity/RBAC/session | PG `omni_admin` (portal_user, provider_role_assignment, tenant_membership, support_access_grant, portal_auth_audit) + authz matrix + session opaque Redis | `src/aoip/console/{identity,identity_store,authz}.py`, `migrations/omni_admin/0004_portal_identity.sql` |
| Console API | `/api/provider/v1/*`, `/api/tenant/v1/*` (read-only) | `src/aoip/console/app.py` |
| Tenant record | PG `omni_admin.tenant(tenant_id, display_name, status, telegram_chat_id)` — **chưa có API lifecycle** | `src/services/admin_config/repo.py:456` list_tenants; `:575-628` status |
| Autonomy | `autonomy_tier_state` (shadow\|assist\|auto) + history + set_tier (CRAT outbox) | `src/workers/tier_gate.py`, `src/services/admin_config/repo.py` |
| Trace Spine | Redis read-model, 12 event type, tenant-isolated (nguồn "AOIP activity" DUY NHẤT) | `src/aoip/agent/trace.py`, `console/projections.py` |
| Agent fleet | register `POST /webhook/agent/register`→Redis `omni:remote_agent:registry:*` TTL 120s; heartbeat=re-register; static API key (chưa scoped) | `src/gateway/routes/{agent_webhook,agents}.py`, `src/remote_agent/` |
| Mission/onboarding | mission runtime derived (chưa persist); curriculum thủ tục 11 stage; FileKnowledgeStore facts; Communication questions | `src/aoip/mission.py`, `capabilities/missions.py`, `knowledge/store.py` |
| Frontend | Next.js 16.2 monorepo `ui/apps/{provider,tenant}-portal` + packages `@aoip/{ui-kit,api-client,auth-client,shared-types}`; ingress `/`→Next, `/auth`+`/api`→FastAPI | `ui/apps/`, `ui/packages/`, `k8s/ingress/aoip-portals.yaml` |

## 2. Thứ tự phụ thuộc (A→G)

- **A. Provider Control Tower foundation** — nav 15 mục, Account/Profile, Overview số thật. *(đang làm)*
- **B. Tenant Lifecycle + Plan/License** — create/view/update/activate/suspend/reactivate/onboarding; assign/change plan, agent limit, entitlements, autonomy ceiling, retention, support tier, usage. UI Customers + Licenses & Plans.
- **C. Scoped Agent Enrollment** — enrollment token (tenant-bound, expiry, max-uses, one-time, revoke), attempt log, install command per-platform, first register+heartbeat, revoke/quarantine. Browser KHÔNG nhận API key tenant vô hạn.
- **D. Onboarding Workspace** — phơi 11 stage curriculum; mỗi stage: status/assigned agent/DoD/progress/verified facts/unknowns/contradictions/questions/blockers/last activity/next action; start/pause/resume/cancel.
- **E. Human learning loop** — provider thấy onboarding blocked → tenant nhận câu hỏi → user trả lời → Fact bền vững → graph cập nhật → Mission resume → completion đổi → hai portal cùng timeline tương quan.
- **F. Enforcement** — license/plan enforce ở backend/runtime (agent limit, entitlements, autonomy ceiling, retention, support access, recovery capability). UI hiding KHÔNG phải enforcement.
- **G. Fleet/Platform Health/Policy/Deployment/Audit expansion.**

## 3. Provider navigation (khung production, 15 mục)

Overview · Customers · Onboarding · Agent Fleet · Systems · Missions · Incidents · Human Inbox ·
Policies & Autonomy · Licenses & Plans · Deployments & Versions · Platform Health ·
Audit & Security · Users & Access · Settings.

Route chưa triển khai → đánh dấu **unavailable** rõ ràng, KHÔNG hiển thị dữ liệu giả.

---

## LEDGER (cập nhật sau mỗi sub-slice)

### Sub-slice A — Provider Control Tower foundation — ✅ **DONE (2026-07-01)**

**Verification (live):** BE `pytest tests/test_aoip_provider_overview.py` 5/5 + `test_aoip_console.py` 11/11;
E2E `provider_overview.spec.ts` 4/4 + `portals.spec.ts` 8/8 trên cluster thật (Chromium→Traefik→Next→FastAPI).
K8s proof: `curl /api/provider/v1/overview` no-auth → **401** (fail-closed RBAC); authed E2E thấy Overview
grid + `health-redis=ok`; **nav 7 vùng runtime-backed** (Overview + 6 read-projection soon), `/agents`
→ section stub; identity ở `/account`. (Overview đã gỡ metric license/version-drift theo governing rule.)
Image rebuilt: `multi-agent-system:latest` (backend console) + `aoip-provider-web:latest` (Next), rollout OK.



**Mục tiêu:** provider thấy control-tower số thật + khung nav production + Account/Profile.

**Overview metric ↔ nguồn (thật vs unavailable):**

| Metric | Nguồn thật | Trạng thái slice A |
|---|---|---|
| tenants total/active/suspended | PG `omni_admin.tenant` GROUP BY status | ✅ thật |
| tenants onboarding | (PG readiness state; aggregate projection chưa tách riêng) | ⛔ unavailable → follow-up |
| agents online/offline | Redis `omni:remote_agent:registry:*`, online nếu now-last_seen ≤ 120s | ✅ thật |
| agent version drift | (chưa có expected-version baseline) | ⛔ unavailable → Sub-slice C/G |
| missions running/blocked/failed | Redis `omni:mission:*` MissionStore projection | ✅ thật khi có mission |
| active incidents | Trace Spine: correlation chưa terminal | ✅ thật |
| pending approvals | Trace `pending_approvals` mọi tenant | ✅ thật |
| pending human questions | (chưa có question store queryable) | ⛔ unavailable → Sub-slice E |
| reconciliation-required ops | Trace `provider_incident.reconcile_required` | ✅ thật |
| license warnings | (chưa có license store) | ⛔ unavailable → Sub-slice B/F |
| AOIP component health | ping Redis + Postgres (liveness phụ thuộc) | ✅ thật |
| recent activity | Trace events mới nhất mọi tenant | ✅ thật |

**Files (slice A):**
- BE: `src/aoip/console/overview.py` (aggregation thuần), `console/app.py` (+GET /overview)
- BE test: `tests/test_aoip_provider_overview.py`
- FE: `ui/apps/provider-portal/app/{layout,page}.tsx`, `app/account/page.tsx`, nav + section stubs, `ui/packages/{shared-types,ui-kit}`
- E2E: `tests/e2e_portals/provider_overview.spec.ts`
- Deploy: rebuild image `multi-agent-system` + `aoip-provider-web`, redeploy pods

### Sub-slice B0 — Environment lifecycle + scoped enrollment — ✅ **DONE (2026-07-14)**

Provider tạo/quản lý tenant và environment theo tenant (onboarding/active/suspended/archived), có
API/UI thật và audit/outbox transaction. Enrollment token mới có thể bind vào environment;
credential, registry và tenant context giữ `environment_id`. Legacy tenant-wide credential
vẫn được đọc để backward compatibility và phải được migrate dần.

Verification: targeted backend tests xanh; tenant/environment boundary tests; provider portal
build xanh. Nguồn: `migrations/omni_admin/0007_environment.sql`,
`0008_agent_environment_binding.sql`, `AdminConfigRepo`, provider `/tenants/{tenant}/environments`.

### Sub-slice C1 — Tenant operational projection — ✅ **DONE (2026-07-14)**

Tenant API/UI có fleet agent, System Twin, incident list và approval queue; mọi projection
được lọc server-side theo session membership, không nhận tenant identity từ browser.
Provider fleet projection vẫn across-tenant; tenant projection chỉ trả đúng tenant.

Verification: tenant isolation tests + tenant portal production build. Còn lại: command
mutation/approval UI đầy đủ và agent lifecycle controls phải nối tiếp từ durable runtime.

### Sub-slice C2 — Durable mission/onboarding projection — ✅ **DONE (2026-07-14)**

`MissionStore` lưu lifecycle, completion, last activity và next action vào Redis theo tenant;
onboarding discovery dual-write progress mà không thay thế discovery document hay PG readiness.
Provider/tenant `/missions`, portal screens và Overview mission counts đều đọc read-model thật.

Verification: mission store isolation/update tests + provider/tenant portal production builds.

### Sub-slice D1 — Tenant-scoped autonomy graduation — ✅ **IMPLEMENTED (2026-07-14)**

Executor resolves tier Redis → Postgres → env using the action's tenant identity; remote-host
confidence is a ceiling (`effective_tier = min(tenant_tier, confidence_ceiling)`), and missing
confidence is fail-closed to `shadow`. Evidence/action contracts now carry `tenant_id` through
the worker context. Legacy unscoped lab envelopes remain compatibility-only; production action
envelopes must be tenant-scoped.

Verification: tier matrix, confidence ceiling, action-contract and worker regression tests.

### Sub-slice B1/F1 — Tenant plan and entitlement enforcement — ✅ **IMPLEMENTED (2026-07-14)**

`omni_admin.tenant_plan` is the provider-managed source for plan code, agent limit, retention,
support tier, enabled state and autonomy ceiling. Provider API/UI exposes the plan; executor
enforces the ceiling and enrollment rejects disabled/over-limit agents transactionally.
Missing entitlement fails closed rather than granting unlimited access.

Verification: migration applied in cluster (`tenant_plan` exists with 3 rows), enrollment limit
regression, plan RBAC route test, autonomy-ceiling test, release gate.

### Sub-slice B2 — Provider plan operations surface — ✅ **IMPLEMENTED (2026-07-14)**

`/licenses` is now a real provider operational surface, backed by the tenant-plan API. It
shows and updates plan code, active-agent limit, autonomy ceiling, retention, support tier,
and enabled state. The write remains protected by `P_CHANGE_POLICY`, audited by
`AdminConfigRepo`, and enforced by enrollment/tier runtime paths; it is now included in
provider navigation because the backend capability is production-ready.

Verification: provider portal production build passes; backend plan RBAC/enrollment tests and
the full product release gate remain green.

### Verification refresh — ✅ 2026-07-14

The complete frontend/backend/business-logic release gate is green: backend `6150` passed,
boundary/safety `61` passed, portal E2E `18/18`, pre-deploy `17/17`, both portal
builds/typechecks passed, and production npm audit found zero vulnerabilities. Detailed
evidence: `docs/reports/frontend-backend-logic-verification-2026-07-14.md`.

**DoD A:** provider login → thấy Overview số thật (không giả), nav 15 mục (mục chưa làm = unavailable),
trang identity chuyển sang /account; backend enforce provider RBAC cho /overview; BE+E2E test pass;
K8s proof (curl /api/provider/v1/overview 200 + số khớp nguồn).
</content>
</invoke>
