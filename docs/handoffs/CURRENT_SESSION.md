# Current Session Handoff

## Deliverable hiện tại
**Iteration 19 — Operator Understanding surface (Phase-2 Golden Journey Read-only, slice 1): DONE (VERIFIED_RUNTIME).**

## Definition of Done
Bước "Understanding Ready" của Golden Journey phải operator-visible qua official portal (không
Redis/curl thủ công), có test + runtime proof. **DONE.**

## Đã hoàn thành
- Gateway `GET /onboarding/entities` (`src/gateway/routes/onboarding.py`) — entity index của System
  Twin (hosts/services + revision); đóng Known Broken Link #4 (operator không còn phải đoán
  `entity_id` format `host:xxx`). 3 test TDD mới trong
  `tests/test_gateway_onboarding_competency_routes.py` (7 passed).
- omni-ui trang `/understanding` (`ui/app/understanding/page.tsx`): readiness card, entity list →
  Competency Matrix facet table (state badges, confidence, evidence_refs), Open Unknowns,
  Questions; TenantSelector; per-section honest error, KHÔNG mock fallback. Proxy routes mới:
  `ui/app/api/onboarding/understanding/route.ts` (aggregate 4 endpoint song song) +
  `ui/app/api/onboarding/competency/route.ts`. Sidebar link ở navOps/navFull/navPortal;
  không đụng middleware prefix (page hoạt động cả 2 realm như /pipeline).
- Fix phụ: `ui/tsconfig.json` exclude `apps`/`packages` — root `next build` trước đó fail
  type-check vì `apps/provider-portal` (pre-existing latent break; ghi TECH_DEBT_BACKLOG #14).

## Verification đã chạy
- Full suite: 5967 passed, 1 failed = flake đã biết (`test_register_then_real_system_metrics…`).
- `cd ui && npm run build` xanh, route `/understanding` trong manifest.
- Runtime: rebuild+rollout `omni-gateway:latest` (`aa24b92ad3bf…`) + `omni-ui:latest`
  (`b0c85bbdd6d7…`). Gateway entities API trả 3/3 host + 7 svc thật (rev 2793→2814 realtime).
  UI proof CÓ AUTH THẬT: NextAuth credentials login qua port-forward + Host `omni.ai-agent.local`
  (cookie domain `.ai-agent.local` — curl phải dùng `--resolve`, không dùng 127.0.0.1 trần);
  aggregate → 352 unknowns/336 questions/readiness_flag=true; competency `host:cust-app` facet thật;
  page HTML 200; unauth → 401. `/readyz` 200; `OMNI_AUTO_EXECUTE_ENABLED=false` reconfirmed.

## Quyết định đã chốt (KHÔNG re-litigate)
- `/understanding` không nằm trong `OPS_PAGE_PREFIXES`/`PORTAL_ADMIN_PREFIXES` — cố ý, để dùng được
  cả 2 realm; đừng "sửa" thành redirect.
- Proxy route trả `{data,error}` per-section, không mock fallback — giữ nguyên chuẩn honest-error.
- `apps/`/`packages/` bị exclude khỏi root ui type-check là fix đúng tầng (chúng có tsconfig/
  Dockerfile riêng); lỗi thật bên trong provider-portal là TECH_DEBT #14, slice riêng.

## Blockers
None.

## Next step chính xác
Phase 2 slice kế tiếp (chọn 1): (a) **Answer-question button trên trang Understanding** — write
action đầu tiên của portal, backend `POST /onboarding/questions/{id}/answer` đã runtime-verified
iter 15, chỉ cần proxy POST + form UI; (b) render Mermaid diagram (`GET /onboarding/diagram`);
(c) Playwright E2E cho `/understanding`. Khuyến nghị (a) — giá trị Golden Journey cao nhất
(đóng loop Unknown→Question→Claim ngay trên portal).

## Không được làm lại
- Không thêm mock fallback vào proxy route.
- Không mở remediation/billing/multi-region (PRODUCT_CONTRACT §9).
- Không chép state constants tay — import `aoip.protocol` (ADR-002).

## Lệnh cần chạy lại
`.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` trước khi bắt đầu slice mới.

## Tài liệu liên quan
- `docs/product/PRODUCT_PROOF.md` (Iteration 19) · `docs/product/PRODUCT_CONTRACT.md`
- `docs/product/PRODUCTION_MISSON.md` (mission gốc, commit lần này)
- `docs/operations/AUTONOMOUS_LOOP_LEDGER.md` / `AUTONOMOUS_LOOP_STATE.json`
