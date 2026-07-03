# Current Session Handoff

## Deliverable hiện tại
**Iteration 21 — Playwright E2E cho `/understanding` (read + write + auth boundary): DONE (VERIFIED_RUNTIME).**

## Definition of Done
Bộ E2E browser-level chạy lên pod `omni-ui` thật, cover login NextAuth + read flow + write flow
answer-question + auth boundary, CI-safe mặc định (không mutate nếu thiếu flag). **DONE.**

## Đã hoàn thành
- `ui/playwright.config.ts` (mới) — baseURL `http://omni.ai-agent.local:18081` (override qua
  `E2E_BASE_URL`), Chromium `--host-resolver-rules` map hostname → 127.0.0.1; setup project
  (login) → chromium project dùng `storageState` (`ui/e2e/.auth/`, đã gitignore).
- `ui/e2e/auth.setup.ts` (mới) — login qua form NextAuth thật; credentials từ env
  `E2E_USERNAME`/`E2E_PASSWORD` (secret `omni-ui-secrets`), không hardcode.
- `ui/e2e/understanding.spec.ts` (mới) — 7 test: read (sections + competency matrix), write
  answer-question (gate `E2E_ALLOW_WRITE=1`, không flag → skip), 400 tại proxy, redirect
  `/login`, POST unauth 401.
- `ui/package.json`: script `e2e`; `@playwright/test` 1.61.1 devDep. `.gitignore`: `/e2e/.auth`,
  `/test-results`, `/playwright-report`.
- KHÔNG đổi Python, KHÔNG đổi app code.

## Verification đã chạy
- `E2E_ALLOW_WRITE=1 npx playwright test` → **7 passed (3.7s)** trên pod thật (port-forward
  `svc/omni-ui 18081:80`, tenant staging-sim, mutation answer thật vào Redis lab).
- Không flag → **6 passed, 1 skipped** (write flow skip đúng — CI-safe).
- `cd ui && npm run build` xanh.
- Pytest: **5967 passed / 1 fail** = `test_register_then_real_system_metrics_emitted_through_real_pipeline`
  — known env-dependent (psutil thật trên máy dev, routing phụ thuộc tải máy; đã ghi
  resume_checks từ trước, fail cả trên working tree sạch, KHÔNG liên quan iteration này).

## Quyết định đã chốt (KHÔNG re-litigate)
- Mọi assertion HTTP trong E2E đi qua browser (`page.evaluate(fetch)`) — Playwright
  `APIRequestContext` chạy trong Node, KHÔNG ăn `--host-resolver-rules`. Đừng chuyển sang
  request fixture.
- Write flow gate bằng `E2E_ALLOW_WRITE=1`, mặc định skip. Giữ nguyên.
- Test redirect chỉ assert URL path `/login` — ops-realm middleware redirect về public host
  KHÔNG port (Ingress :80), qua port-forward không tải được trang sau redirect. Không phải bug app.
- Write-flow assert bằng count badge ANSWERED trước/sau (`expect.poll`) — row cũ bị re-render
  sau `load()`, locator bám row sẽ chết.
- Các quyết định iter 19/20 giữ nguyên (honest-error, không mock fallback, Claim không tự VERIFIED).

## Branch và commit
`main`, HEAD `14e51a9`. Commit của iteration: `e5f4b95` (test portal E2E) + `14e51a9` (docs
governance) — đã commit trong phiên, chưa push. Working tree sạch.

## Blockers
None.

## Next step chính xác
Phase 2 slice kế tiếp: **Mermaid diagram render trên `/understanding`** (`GET /onboarding/diagram`
backend đã có) — Golden Journey visibility. Sau đó cân nhắc wire `npm run e2e` vào quy trình
release (cần pod + port-forward, không chạy được trong CI thuần).

## Không được làm lại
- Không thêm mock fallback vào proxy route.
- Không mở remediation/billing/multi-region (PRODUCT_CONTRACT §9).
- Không "sửa" test env-dependent `test_register_then_real...` như thể do UI slice gây ra.

## Lệnh cần chạy lại
- `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` trước slice mới.
- E2E: `kubectl -n multi-agent port-forward svc/omni-ui 18081:80 &` rồi
  `cd ui && E2E_USERNAME=… E2E_PASSWORD=… [E2E_ALLOW_WRITE=1] npm run e2e`
  (credentials: `kubectl -n multi-agent get secret omni-ui-secrets`).

## Tài liệu liên quan
- `docs/product/PRODUCT_PROOF.md` (Iteration 21) · `docs/product/PRODUCT_CONTRACT.md`
- `docs/operations/AUTONOMOUS_LOOP_LEDGER.md` / `AUTONOMOUS_LOOP_STATE.json`
