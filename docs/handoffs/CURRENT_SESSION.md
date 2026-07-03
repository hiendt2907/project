# Current Session Handoff

## Deliverable hiện tại
**Iteration 22 — Mermaid System Diagram trên `/understanding`: DONE (VERIFIED_RUNTIME).**

## Definition of Done
Trang `/understanding` render diagram Mermaid từ `GET /onboarding/diagram` (backend có sẵn),
với E2E assert SVG render từ dữ liệu Redis thật trên pod `omni-ui`. **DONE.**

## Đã hoàn thành
- `ui/app/api/onboarding/understanding/route.ts`: thêm section `diagram` vào aggregate proxy
  (fetch song song, honest-error, không mock fallback).
- `ui/components/mermaid-diagram.tsx` (mới): `MermaidBlock` (client-side render, dynamic import
  `mermaid` — dep mới, `securityLevel: "strict"`, theme dark) + `splitDiagramText()` tách blob
  3-diagram theo dòng `%% <title>` (format `render_all_diagrams` trong
  `src/pkg/onboarding/discovery_doc.py`).
- `ui/app/understanding/page.tsx`: card "System Diagram" full-width — badge version, grid 3 diagram,
  skeleton / honest-error / empty-state.
- `ui/e2e/understanding.spec.ts`: test mới "renders the system diagram as Mermaid SVG" — assert card
  + badge `v{N}` + đúng 3 `data-testid="mermaid-svg"`.
- KHÔNG đổi Python.

## Verification đã chạy
- Rebuild image `omni-ui:latest` (digest `e20e6a9c1cdc`) + rollout; `imageID` xác minh trên pod.
- `E2E_ALLOW_WRITE=1 npx playwright test` → **8 passed** trên pod thật; không flag →
  **7 passed, 1 skipped** (CI-safe).
- `cd ui && npm run build` xanh.
- Pytest: xem checkpoint cuối phiên (không đổi Python — baseline như iter 21: 5967 passed / 1 fail
  env-dependent `test_register_then_real_system_metrics_emitted_through_real_pipeline`).

## Quyết định đã chốt (KHÔNG re-litigate)
- Mermaid render **client-side only** — gateway giữ contract "raw text, never rendered to image".
- Diagram đi qua aggregate route `understanding` (1 fetch), KHÔNG tạo proxy route riêng.
- `splitDiagramText` tách theo dòng bắt đầu `%%` — nếu backend đổi format phải sửa cả hai đầu.
- Assertion CardTitle phải dùng `.first()` (strict-mode violation vì text trong 2 span lồng nhau).
- Mọi quyết định iter 19/20/21 giữ nguyên (honest-error, không mock, write gate `E2E_ALLOW_WRITE=1`,
  browser-fetch thay APIRequestContext, redirect chỉ assert path `/login`).

## Branch và commit
`main`, HEAD `2f48e44`. Commit của iteration: `4645216` (feat portal Mermaid diagram) +
`2f48e44` (docs governance) — đã commit trong phiên, chưa push. Working tree sạch.

## Blockers
None.

## Next step chính xác
Phase 2 slice kế tiếp (chọn 1): (a) **diagram history/diff view** — `GET /onboarding/diagram/history`
backend đã có; hoặc (b) wire `npm run e2e` vào quy trình release (cần pod + port-forward, không chạy
trong CI thuần). Không mở action/billing song song (PRODUCT_CONTRACT §9).

## Không được làm lại
- Không thêm mock fallback vào proxy route.
- Không render diagram server-side / rasterize ở gateway.
- Không mở remediation/billing/multi-region (PRODUCT_CONTRACT §9).
- Không "sửa" test env-dependent `test_register_then_real...`.

## Lệnh cần chạy lại
- `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` trước slice mới.
- E2E: `kubectl -n multi-agent port-forward svc/omni-ui 18081:80 &` rồi
  `cd ui && E2E_USERNAME=… E2E_PASSWORD=… [E2E_ALLOW_WRITE=1] npm run e2e`
  (credentials: `kubectl -n multi-agent get secret omni-ui-secrets`).
- Deploy UI: `docker build -t omni-ui:latest ui/ && kubectl -n multi-agent rollout restart deploy/omni-ui`.

## Tài liệu liên quan
- `docs/product/PRODUCT_PROOF.md` (Iteration 22) · `docs/product/PRODUCT_CONTRACT.md`
- `docs/operations/AUTONOMOUS_LOOP_LEDGER.md` / `AUTONOMOUS_LOOP_STATE.json`
