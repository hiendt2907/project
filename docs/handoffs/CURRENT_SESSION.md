# Current Session Handoff

## Deliverable hiện tại
**Iteration 26 — Parity slice 2 (ADR-003): Onboarding readiness ⚠️→✅. DONE (VERIFIED_RUNTIME).**

## Definition of Done
Capability "Onboarding readiness" đạt ✅ trên thang Operator-visible: người không hiểu hệ thống
đọc được checklist readiness (không raw key), có mục tiêu (target) và hành động kế tiếp. **DONE.**

## Đã hoàn thành
- `src/gateway/routes/onboarding.py`: `GET /onboarding/readiness` trả thêm `thresholds`
  (`dd.resolve_readiness_thresholds` — per-tenant override giữ nguyên semantics, chỉ import `pkg/`).
- `tests/test_gateway_onboarding_readiness.py` (mới): 2 test TDD (default thresholds + tenant
  override, readiness=None vẫn có thresholds).
- `ui/app/understanding/page.tsx`: card Readiness redesign — badge "Ready/Not ready yet",
  3 `ReadinessCheck` (Endpoints mapped / Business flows confirmed / Stale open questions) với
  verdict Done/Needs work, progress bar + vạch target, "Last evaluated Xs ago"; typed
  `ReadinessRecord`/`ReadinessThresholds`; empty-state có hướng dẫn.
- `ui/e2e/understanding.spec.ts`: test mới assert badge + 3 nhãn + "target N%" + đúng 3 verdict
  + assert KHÔNG còn raw key `endpoint_mapped_pct`.

## Verification đã chạy
- Pytest full: **5977 passed** (kể cả test env-dependent lần này pass).
- Curl gateway thật: `?tenant_id=staging-sim` → readiness + thresholds đầy đủ;
  `readiness_flag=false` vì `open_questions_over_threshold=10` (dữ liệu thật, actionable).
- Rebuild + rollout `omni-gateway` + `omni-ui`. `npm run build` xanh.
- `E2E_ALLOW_WRITE=1 make e2e-portal` → **11 passed** pod thật (lần chạy đầu fail 2: strict-mode
  nhãn trùng detail text → fix `{ exact: true }`; diagram-history chỉ flake, tự xanh lại).

## Quyết định đã chốt (KHÔNG re-litigate)
- `thresholds` trả trong cùng response `GET /onboarding/readiness` (không endpoint riêng).
- Readiness=None vẫn trả thresholds — UI cần target để vẽ empty-state có nghĩa.
- Gotcha strict-mode (lần 3): nhãn card xuất hiện cả trong detail text → dùng `{ exact: true }`.
- Mọi quyết định iter 19-25 giữ nguyên (ADR-003 canonical, không mock fallback, portal tiếng Anh).

## Branch và commit
`main`. Xem git log cho commit iteration 26 (feat + docs).

## Blockers
None.

## Next step chính xác
Slice parity kế tiếp theo ADR-003 — chọn MỘT capability ❌/⚠️: ứng viên (1) Tenant creation
(⚠️ API only — write-flow, cần cân nhắc scope), (2) Continuous discovery ❌, (3) System Twin
persisted ❌ (card Entities đã cover một phần — đối chiếu matrix trước, có thể chỉ cần nâng nhãn).
Không mở action/billing song song (PRODUCT_CONTRACT §9).

## Không được làm lại
- Không thêm mock fallback vào proxy route.
- Không hiển thị raw key/JSON thô làm UI chính (ADR-003).
- Không mở nhiều capability parity trong một iteration.
- Không "sửa" test env-dependent `test_register_then_real...`.

## Lệnh cần chạy lại
- `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` trước slice mới.
- Portal E2E: `E2E_ALLOW_WRITE=1 make e2e-portal`.
- Deploy UI: `docker build -t omni-ui:latest ui/ && kubectl -n multi-agent rollout restart deploy/omni-ui`.
- Deploy gateway (nếu đổi Python): `make docker-gateway deploy-gateway`.
- Curl gateway: port-forward svc/omni-gateway; key = secret `omni-gateway-secret` field
  `OMNI_ADMIN_API_KEYS` (Bearer) — KHÔNG phải `omni-gateway-secrets`.

## Tài liệu liên quan
- `docs/product/PRODUCT_PROOF.md` (Iteration 26 + matrix) · `docs/architecture/ADR-003-backend-frontend-parity.md`
- `docs/operations/AUTONOMOUS_LOOP_LEDGER.md` (checkpoint iter26)
