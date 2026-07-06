# Current Session Handoff

## Deliverable hiện tại
**Iteration 24 — Portal E2E release gate `make e2e-portal`: DONE (VERIFIED_RUNTIME).**

## Definition of Done
Bộ Playwright E2E của portal chạy được bằng MỘT lệnh lặp lại được (không cần port-forward tay,
lấy secret tay, export env tay), exit code phản ánh trung thực kết quả. **DONE.**

## Đã hoàn thành
- `scripts/e2e_portal_release_gate.sh` (mới): preflight kubectl + `rollout status deploy/omni-ui`;
  credentials từ secret `omni-ui-secrets` (`ADMIN_USERNAME`/`ADMIN_PASSWORD`); check port bận;
  port-forward `svc/omni-ui $LOCAL_PORT:80` (default 18081) với trap cleanup; chờ `/login` qua
  đúng `Host: omni.ai-agent.local`; `npm run e2e` exit-code passthrough; write-flow opt-in
  `E2E_ALLOW_WRITE=1`.
- `Makefile`: target `e2e-portal` (+ .PHONY).
- KHÔNG đổi Python/app code trong iteration này.

## Verification đã chạy
- `make e2e-portal` trên cluster lab → **8 passed, 1 skipped**, exit 0, port-forward tự dọn.
- `E2E_ALLOW_WRITE=1 make e2e-portal` → **9 passed** (answer-question mutation thật).
- Negative: chiếm port 18081 → gate **fail exit 2** (không silent-pass).
- Pytest baseline: **5972 passed / 1 fail env-dependent đã biết** (`test_register_then_real...`).
- Kill-switch reconfirmed: `OMNI_AUTO_EXECUTE_ENABLED=false` trên pod omni-fullstack.

## Quyết định đã chốt (KHÔNG re-litigate)
- Gate cần cluster thật + `ui/node_modules` — KHÔNG chạy trong CI thuần (đúng quyết định iter 23).
- Credentials luôn đọc từ secret, không hardcode, không cache ra file.
- Write-flow mặc định skip; chỉ bật qua `E2E_ALLOW_WRITE=1`.
- Gotcha shell: `${VAR:+X=Y}` expansion không được coi là assignment prefix — dùng `export`.
- Mọi quyết định iter 19-23 giữ nguyên (honest-error, không mock, Mermaid client-side,
  history `?before=&limit=`).

## Branch và commit
`main`. Iteration 24 commit trong phiên (feat tooling + docs governance), chưa push.

## Blockers
None. Lưu ý vận hành: OrbStack k8s có thể ở trạng thái Stopped đầu phiên — `orb start` rồi chờ
deploy Ready trước khi chạy gate.

## Next step chính xác
Slice Golden Journey đọc tiếp theo, hoặc cân nhắc wire `e2e-portal` vào `omni-death-loop`.
Không mở action/billing song song (PRODUCT_CONTRACT §9).

## Không được làm lại
- Không thêm mock fallback vào proxy route.
- Không hardcode credentials trong script/Makefile.
- Không biến gate thành CI-thuần (cần pod thật — đã chốt).
- Không mở remediation/billing/multi-region (PRODUCT_CONTRACT §9).
- Không "sửa" test env-dependent `test_register_then_real...`.

## Lệnh cần chạy lại
- `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` trước slice mới.
- Portal E2E: `make e2e-portal` (write-flow: `E2E_ALLOW_WRITE=1 make e2e-portal`).
- Deploy UI: `docker build -t omni-ui:latest ui/ && kubectl -n multi-agent rollout restart deploy/omni-ui`.
- Deploy gateway (nếu đổi Python): `make docker-gateway deploy-gateway`.

## Tài liệu liên quan
- `docs/product/PRODUCT_PROOF.md` (Iteration 24) · `docs/product/PRODUCT_CONTRACT.md`
- `docs/operations/AUTONOMOUS_LOOP_LEDGER.md` / `AUTONOMOUS_LOOP_STATE.json`
