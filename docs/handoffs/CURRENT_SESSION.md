# Current Session Handoff

## Deliverable hiện tại
**Iteration 20 — Answer-question trên portal (Phase-2, write-action đầu tiên): DONE (VERIFIED_RUNTIME).**

## Definition of Done
Operator đóng được loop Unknown→Question→Claim ngay trên trang `/understanding` (không curl Bearer
key), có test + runtime proof. **DONE.**

## Đã hoàn thành
- `ui/app/api/onboarding/answer/route.ts` (mới) — proxy POST duy nhất tới gateway
  `/onboarding/questions/{id}/answer`; validate question_id pattern + answered_by (≤120) +
  value (≤500); `source_channel="portal"`; honest error, forward status/detail từ gateway,
  KHÔNG mock fallback.
- `ui/app/understanding/page.tsx` — nút "Answer" trên mỗi PENDING question → form inline
  (answered_by + value) → submit → refresh; ANSWERED badge sau 200; lỗi qua `SectionError`;
  reset state khi đổi tenant.
- KHÔNG đổi Python — backend answer endpoint đã runtime-verified iter 15.

## Verification đã chạy
- Full suite: **5968 passed, 0 failed** (cả flake đã biết cũng pass).
- `cd ui && npm run build` xanh; route `/api/onboarding/answer` có trong build output.
- Runtime: rebuild+rollout `omni-ui:latest` (`4cdb63f6e68a…`, imageID pod khớp). NextAuth login
  thật (port-forward + Host `omni.ai-agent.local`, `--resolve`) → POST answer question
  `a96324a653fe6491b3be9fec` (svc:systemd-udevd/sla, tenant staging-sim) → 200 answer_id
  `5abc1da3499876efd4bb` → re-fetch **status=ANSWERED** (state thật trong Redis). Unauth POST →
  401; invalid question_id → 400 tại proxy. Page 200. `OMNI_AUTO_EXECUTE_ENABLED=false` reconfirmed.

## Quyết định đã chốt (KHÔNG re-litigate)
- Claim KHÔNG tự thành VERIFIED sau answer — đúng thiết kế (chỉ competency_matrix promote khi có
  Fact máy khớp). Đừng "sửa" cho answer thành VERIFIED.
- Proxy validate input trước khi forward (400 tại proxy cho input sai) — giữ nguyên.
- Các quyết định iter 19 giữ nguyên: `/understanding` dùng được cả 2 realm, honest-error, tsconfig
  exclude apps/packages.

## Branch và commit
`main`, HEAD sau chốt handoff nằm ngay trên `26fa08d`. Commit của iteration: `3898ae5` (feat portal
answer) + `26fa08d` (docs governance) — đã commit trong phiên, chưa push. Working tree sạch.

## Blockers
None.

## Next step chính xác
Phase 2 slice kế tiếp (chọn 1): (a) **Mermaid diagram render** trên trang Understanding
(`GET /onboarding/diagram` đã có backend); (b) **Playwright E2E cho `/understanding`** — giờ có cả
read + write flow đáng test. Khuyến nghị (b) nếu ưu tiên production readiness, (a) nếu ưu tiên
Golden Journey visibility.

## Không được làm lại
- Không thêm mock fallback vào proxy route.
- Không mở remediation/billing/multi-region (PRODUCT_CONTRACT §9).
- Không chép state constants tay — import `aoip.protocol` (ADR-002).

## Lệnh cần chạy lại
`.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` trước khi bắt đầu slice mới.

## Tài liệu liên quan
- `docs/product/PRODUCT_PROOF.md` (Iteration 20) · `docs/product/PRODUCT_CONTRACT.md`
- `docs/operations/AUTONOMOUS_LOOP_LEDGER.md` / `AUTONOMOUS_LOOP_STATE.json`
