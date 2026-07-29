# Current Session Handoff

## Deliverable hiện tại
Commit + deploy toàn bộ công việc G1–G4 và 3 bản vá phân quyền đã tồn đọng trong working tree.
**ĐÃ XONG** — 5 commit đã push, cluster đã verify bằng smoke test thật.

## Definition of Done
- Commit sạch theo nhóm ngữ nghĩa, push lên `origin/main`. → ✅
- Worker + gateway deploy và **verify code mới có mặt trong pod đang chạy**, không chỉ tin rollout. → ✅
- Endpoint mới trả đúng trên cluster. → ✅

## Trạng thái hiện tại
`main` @ `cc66c4e`, đã push. Working tree chỉ còn 4 file untracked (cố ý, xem dưới).
Test: **6737 passed / 0 fail**.

## Đã hoàn thành

### 5 commit đã push
| Commit | Nội dung |
|---|---|
| `fc796a9` | fix(gateway): vá 3 lỗ hổng phân quyền — `/autonomy` (10 endpoint), `/trace/purge`, CRAT cross-tenant |
| `a50e2ca` | feat(sre): G1–G4 — RAG backfill, advisory promoter, KPI key contract, capacity/report |
| `7f70319` | chore: dọn 20 file dead code/dead docs/route rỗng |
| `529e576` | fix(gateway): `/reports/playbooks` 500 — datetime không serialize |
| `cc66c4e` | fix(build): `deploy-gateway` không build image |

### Deploy + verify trên cluster thật
- `omni-fullstack` redeploy. Verify bằng import thật trong pod: 5 module mới OK,
  `capacity_report_loop` đã đăng ký (`omni_worker.py:1229`) và **đã chạy** —
  log `event=capacity_report_published tenants=2`.
- `omni-gateway` rebuild + redeploy. 3 endpoint `/reports/{sre,capacity,playbooks}`
  trả **200** cho `staging-sim`; `/reports/sre?tenant_id=default` trả 404 **đúng thiết kế**
  (chỉ `staging-sim` và `tenant-replay-01` có dữ liệu).
- Báo cáo SRE sinh nội dung thật: 1490 fact cho `staging-sim`.

### 2 bug phát hiện lúc smoke test (test suite KHÔNG bắt được)
1. **`/reports/playbooks` trả 500** — row `omni_admin.playbook_graduation` có cột
   timestamp, `JSONResponse` không encode `datetime`. Fixture `_Repo` chỉ trả kiểu
   nguyên thuỷ nên test luôn xanh. Đã sửa fixture dùng `datetime` thật + thêm test hồi quy;
   xác nhận test có giá trị bằng cách gỡ fix → 2 test fail.
2. **`make deploy-gateway` không build image** — chỉ `apply` + `rollout restart`, khác
   `deploy-fullstack` vốn phụ thuộc `docker-worker`. Deploy "thành công" 2 lần mà code mới
   không vào pod. Đã thêm dependency `docker-gateway`.

## Branch và commit
`main` @ `cc66c4e`, đã push lên `origin`.

## Working tree
4 file untracked, **cố ý chưa xử lý** — chờ quyết định của user:
`fpt-loyalty-sre-compat-report.md`, `fpt_loyalty_topology.html`, `omni-sre-system-prompt.md`,
`plans/omni-universal-sre-discovery-qwen3.6-27b-2026-07-28.md`.
Ba file đầu ở root và mang tên khách hàng thật (`fpt-loyalty`) — cân nhắc kỹ trước khi
đưa vào git.

## Quyết định đã chốt (KHÔNG thiết kế lại)
- Giữ nguyên mọi quyết định trong handoff trước (advisory_sop_payload luôn
  `auto_execute=False`; không tái tạo `src/pkg/autonomy/tier_readiness.py`; danh sách
  "không xoá" đã xác minh ngược).
- `/reports/*` serialize qua `jsonable_encoder`, không tự viết default encoder.

## Verification đã chạy
- `pytest tests/ -q --ignore=tests/integration` → **6737 passed**, 160s.
- `kubectl exec` import 5 module mới trong `omni-fullstack` → OK.
- `grep -c jsonable_encoder /app/src/gateway/routes/reports.py` trong pod → 3 (code mới đã vào).
- Smoke test 6 tổ hợp tenant × endpoint qua `curl` trong pod gateway.

**Gotcha kiểm chứng**: kiểm tra route bằng cách re-import `gateway.api` trong process phụ
qua `kubectl exec python -c` cho **âm tính giả** (`app.routes` rỗng dù route có thật).
Nguồn sự thật là server đang chạy: `curl localhost:8000/openapi.json`.
Gateway listen cổng **8000**, không phải 8080/8090.

## Deployment hiện tại
`omni-fullstack` và `omni-gateway` đều chạy image chứa toàn bộ thay đổi của phiên này.
Kill-switch `OMNI_AUTO_EXECUTE_ENABLED=false` không đổi.

## Blockers
None.

## Next step chính xác
Không còn việc bắt buộc. Ba hướng mở, theo mức độ rủi ro:

1. **`/autonomy/hitl/pending` và `/decide` chưa scope tenant** — phải vá TRƯỚC khi nối
   tenant portal, nếu không tạo leak cross-tenant mới. Đây là việc rủi ro cao nhất còn lại.
2. Tenant portal đi qua `src/aoip/console/app.py`, KHÔNG qua gateway → endpoint
   reports/HITL phải thêm ở đó, chưa làm.
3. `/approvals` khai báo `string[]` nhưng backend trả `list[dict]` → React crash runtime.

## Lệnh cần chạy lại
```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
make deploy-worker deploy-gateway   # deploy-gateway nay đã tự build image
```

## Không được làm lại
Commit/push (xong), deploy worker+gateway (xong, đã verify trong pod), audit portal/gateway,
khảo sát dead code, pentest `/autonomy`, backfill RAG vector.

## Ngoài phạm vi kỹ thuật (context phiên này)
User hỏi về Google Startup Credit. Kết luận: **không đủ điều kiện** vì chưa có pháp nhân —
chương trình yêu cầu công ty đã đăng ký. Landing page đã dựng ở `/Users/hiendang/omni-site/`
(1 file HTML, chưa deploy) để dùng khi có pháp nhân. Không thuộc repo này.

## Tài liệu liên quan
`plans/omni-evolve-to-senior-sre-2026-07-29.md` · `ui/apps/provider-portal/EXCLUDED_ROUTES.md` ·
`docs/CODEBASE.md` · `docs/architecture/ASSESSMENT_autonomous_sre_v2.md`
