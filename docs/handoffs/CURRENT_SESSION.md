# Current Session Handoff

## Deliverable hiện tại
**Iteration 25 — Backend-Frontend Parity (ADR-003) + card Remote Agents: DONE (VERIFIED_RUNTIME).**

## Definition of Done
(1) Nguyên tắc "backend làm được gì thì frontend phải hiển thị được cho người không hiểu hệ
thống" trở thành governance chính thức; (2) capability đầu tiên (agent enrollment/heartbeat)
nâng từ ❌ lên ✅ theo nguyên tắc đó. **DONE cả hai.**

## Đã hoàn thành
- `docs/architecture/ADR-003-backend-frontend-parity.md` (mới) + `PRODUCT_CONTRACT.md` §10:
  parity rule, tiêu chuẩn hiển thị (nhãn đời thường + badge ngữ nghĩa, không raw key/JSON,
  empty-state có hướng dẫn), thang Operator-visible ❌/⚠️/✅ (persona test), nhịp 1 capability/
  iteration. Capability matrix trong PRODUCT_PROOF có legend mới.
- `src/gateway/routes/agent_commands.py`: `GET /webhook/agent/versions` nhận `?tenant_id=`
  (semantics `resolve_scope` — admin override, non-admin scope về tenant của key); item trả thêm
  `tenant_id`. TDD: 2 test mới trong `tests/test_agent_update.py`.
- `ui/app/api/onboarding/understanding/route.ts`: aggregate thêm section `agents` (honest-error).
- `ui/app/understanding/page.tsx`: card "Remote Agents" — subtitle giải thích cho người ngoài,
  badge `N/N online`, mỗi agent dot + Online/Offline + hostname + "Last report Xs ago" + version,
  empty-state hướng dẫn cài agent. Helper `formatAge()`.
- `ui/e2e/understanding.spec.ts`: test mới assert nội dung hiển thị (badge, "Last report … ago").

## Verification đã chạy
- TDD RED→GREEN; `tests/test_agent_update.py` 20 passed. Pytest full: xem ledger checkpoint
  (chạy nền cuối phiên — nếu chưa ghi, chạy lại lệnh bên dưới).
- Rebuild + rollout cả `omni-gateway` (`make docker-gateway deploy-gateway`) lẫn `omni-ui`.
- Curl gateway thật: `?tenant_id=staging-sim` → 3 agent online (v1.1.3, age vài giây);
  `?tenant_id=tenant-replay-01` → đúng 2 agent (cách ly tenant giữ nguyên).
- `E2E_ALLOW_WRITE=1 make e2e-portal` → **10 passed** trên pod thật; `npm run build` xanh.

## Quyết định đã chốt (KHÔNG re-litigate)
- ADR-003 là canonical cho parity; sửa nguyên tắc = ADR mới, không sửa ngầm.
- `/webhook/agent/versions` dùng `resolve_scope` giống onboarding routes; lab mode (ctx=None)
  bỏ qua override — ĐÚNG semantics, không "sửa".
- Portal giữ tiếng Anh (ngôn ngữ portal hiện hành); "đời thường" ≠ bắt buộc tiếng Việt.
- Gotcha lặp: CardTitle lồng span → Playwright strict mode, getByText cần `.first()`.
- Mọi quyết định iter 19-24 giữ nguyên.

## Branch và commit
`main`. Iteration 25 commit trong phiên (feat + docs governance), chưa push.

## Blockers
None.

## Next step chính xác
Slice parity kế tiếp theo ADR-003 — chọn MỘT capability ❌/⚠️ giá trị Golden Journey cao nhất.
Ứng viên: Tenant creation (⚠️ API only), Onboarding readiness (⚠️ đọc DB trực tiếp), System Twin
persisted (⚠️ chỉ redis-cli — card Entities đã cover một phần, cần đối chiếu matrix). Không mở
action/billing song song (PRODUCT_CONTRACT §9).

## Không được làm lại
- Không thêm mock fallback vào proxy route.
- Không hiển thị raw Redis key/state code/JSON thô làm UI chính (ADR-003).
- Không mở nhiều capability parity trong một iteration.
- Không mở remediation/billing/multi-region (PRODUCT_CONTRACT §9).
- Không "sửa" test env-dependent `test_register_then_real...`.

## Lệnh cần chạy lại
- `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` trước slice mới.
- Portal E2E: `make e2e-portal` (write-flow: `E2E_ALLOW_WRITE=1 make e2e-portal`).
- Deploy UI: `docker build -t omni-ui:latest ui/ && kubectl -n multi-agent rollout restart deploy/omni-ui`.
- Deploy gateway (nếu đổi Python): `make docker-gateway deploy-gateway`.

## Tài liệu liên quan
- `docs/architecture/ADR-003-backend-frontend-parity.md` · `docs/product/PRODUCT_CONTRACT.md` §10
- `docs/product/PRODUCT_PROOF.md` (Iteration 25) · `docs/operations/AUTONOMOUS_LOOP_LEDGER.md`
