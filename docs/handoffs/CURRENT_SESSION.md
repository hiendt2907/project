# Current Session Handoff

## Deliverable hiện tại
**Iteration 23 — Diagram History/Diff view trên `/understanding` + fix history endpoint: DONE (VERIFIED_RUNTIME).**

## Definition of Done
Operator xem được lịch sử version diagram và line-diff giữa các version trong card System Diagram,
với endpoint backend thực sự trả được version GẦN ĐÂY (không kẹt cap 200 khi latest=10058). **DONE.**

## Đã hoàn thành
- `src/gateway/routes/onboarding.py`: redesign `GET /onboarding/diagram/history` — contract mới
  `?before=&limit=` (limit 1-50), neo tại `latest`, đi lùi, trả `versions` newest-first + `latest`
  + `next_before`. Contract cũ (`from_version/to_version` cap 200) không có consumer nào (đã grep)
  → không breaking.
- `tests/test_gateway_onboarding_diagram_history.py` (mới, 5 test) — empty/newest-first/pagination/
  skip-missing/limit-bounds.
- `ui/app/api/onboarding/diagram-history/route.ts` (mới): passthrough proxy, validate limit/before
  trước forward (400), honest 502, không mock.
- `ui/lib/diagram-diff.ts` (mới): `diffLines()` LCS line-level + `hasChanges()`.
- `ui/components/diagram-history.tsx` (mới): `DiagramHistoryPanel` — chip version, "Older…"
  pagination qua `next_before`, diff +/− hoặc identical-notice.
- `ui/app/understanding/page.tsx`: nút toggle "History" trên header card System Diagram.
- `ui/e2e/understanding.spec.ts`: test mới — panel neo tại latest (version đầu > 20), ≥2 chip,
  chọn version → diff/identical.

## Verification đã chạy
- Pytest full: **5972 passed / 1 fail env-dependent đã biết** (`test_register_then_real...`).
- Rebuild + rollout **cả `omni-gateway`** (`make docker-gateway deploy-gateway`) **lẫn `omni-ui`**.
- Curl gateway thật (port-forward svc/omni-gateway 18090:**80** — svc port 80/targetPort 8000,
  Bearer key = env `OMNI_GATEWAY_API_KEY` trên pod omni-ui):
  `limit=3` → `latest:10058, [10058,10057,10056]`; `before=10057&limit=2` → `[10056,10055]`.
- Playwright lên pod thật: **8 passed, 1 skipped** (write-flow gate `E2E_ALLOW_WRITE` giữ nguyên).
- `cd ui && npm run build` xanh.

## Quyết định đã chốt (KHÔNG re-litigate)
- History endpoint neo tại latest, phân trang lùi bằng `before`/`next_before`; probe cap 200.
- Diff tính client-side bằng LCS thuần TS (`ui/lib/diagram-diff.ts`) — KHÔNG thêm dependency diff,
  KHÔNG diff phía server.
- History fetch on-demand qua proxy route riêng (pattern competency/answer), KHÔNG nhét vào
  aggregate `understanding` (payload lớn, chỉ cần khi mở panel).
- Mọi quyết định iter 19-22 giữ nguyên (honest-error, không mock, Mermaid client-side only,
  write gate `E2E_ALLOW_WRITE=1`).

## Branch và commit
`main`. Commit của iteration: xem `git log` (feat + docs governance, commit trong phiên, chưa push).
Working tree sạch sau commit.

## Blockers
None.

## Next step chính xác
Phase 2 slice kế tiếp (chọn 1): (b) wire `npm run e2e` vào quy trình release (cần pod +
port-forward, không chạy trong CI thuần); hoặc slice Golden Journey đọc tiếp theo. Không mở
action/billing song song (PRODUCT_CONTRACT §9).

## Không được làm lại
- Không thêm mock fallback vào proxy route.
- Không render diagram server-side / rasterize ở gateway.
- Không khôi phục contract history cũ `from_version/to_version`.
- Không mở remediation/billing/multi-region (PRODUCT_CONTRACT §9).
- Không "sửa" test env-dependent `test_register_then_real...`.

## Lệnh cần chạy lại
- `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` trước slice mới.
- E2E: `kubectl -n multi-agent port-forward svc/omni-ui 18081:80 &` rồi
  `cd ui && E2E_USERNAME=… E2E_PASSWORD=… [E2E_ALLOW_WRITE=1] npm run e2e`
  (credentials: `kubectl -n multi-agent get secret omni-ui-secrets`).
- Deploy UI: `docker build -t omni-ui:latest ui/ && kubectl -n multi-agent rollout restart deploy/omni-ui`.
- Deploy gateway (nếu đổi Python): `make docker-gateway deploy-gateway`.

## Tài liệu liên quan
- `docs/product/PRODUCT_PROOF.md` (Iteration 23) · `docs/product/PRODUCT_CONTRACT.md`
- `docs/operations/AUTONOMOUS_LOOP_LEDGER.md` / `AUTONOMOUS_LOOP_STATE.json`
