# Current Session Handoff

## Deliverable hiện tại
Đưa Omni ra Internet qua `omnisre.xyz` + đóng toàn bộ việc treo + lộ trình production.
**XONG.** `main` @ `007f7e4`, đã push. Test **6750 passed**. `verify.sh` 17/17.

## Đang sống trên Internet
- `https://www.omnisre.xyz` + `/vi/` — landing song ngữ, Cloudflare Pages (Direct Upload)
- `https://app.omnisre.xyz` — console sau Cloudflare Access, login đã verify bằng người thật
- Tunnel `omnisre` qua LaunchAgent, cloudflared **2026.7.3**

## Việc đã đóng trong phiên này
| | |
|---|---|
| Lỗ hổng HITL cross-tenant | `_hitl_tenant()` + resolve_scope; 5 test; đã deploy + smoke test cluster |
| `/approvals` sai kiểu | `string[]` → `PendingApproval[]`; React hết crash |
| Tenant portal thiếu endpoint | +3 endpoint reports/HITL, tenant TỪ PRINCIPAL; 8 test |
| GitHub Actions hết quota | Gỡ cả 4 workflow |
| Landing page | Xây lại, song ngữ, CSP `default-src 'none'` |
| cloudflared | 2026.5.0 → 2026.7.3, tunnel 12 kết nối |
| Lộ trình production | `docs/plans/production-roadmap.md` |

## ⚠️ Hệ quả gỡ CI — phải chạy TAY trước mỗi push
```bash
docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:v8.18.2 detect \
  --no-git --source=/repo --config=/repo/.gitleaks.toml
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
```

## Phát hiện quan trọng: 2 finding CRITICAL của audit 2026-07-22 ĐÃ ĐÓNG
Kiểm chứng lại runtime, không tin audit cũ:
- ClusterRoleBinding `cluster-admin` cho SA `omni-*` — **đã dọn**, chỉ còn CRB mặc định
- `idx:itops_sop_ledger` "0 docs" — nay **1093 docs** (G1 RAG backfill, `a50e2ca`)

Bài học: audit hết hạn nhanh. Luôn verify lại trước khi mang một "CRITICAL" cũ đi tiếp.

## Lệnh vận hành
```bash
bash cloudflare/tunnel/verify.sh          # mặt public, 17 gate
make sync-public-ui | sync-public-backend | sync-public
make deploy-landing                        # landing → Cloudflare Pages
make deploy-gateway                        # gateway (đã tự build image)
```

## Next step — không có gì bắt buộc
1. **Đọc `docs/plans/production-roadmap.md`.** Giai đoạn 1 (tìm design partner) là
   toàn bộ việc cần làm 1–3 tháng tới. KHÔNG nâng cấp hạ tầng trước cổng đó.
2. Việc nhỏ rẻ: bật `omni-hitl-dispatcher` (replicas=0), nạp `idx:k8s_expert`,
   apex redirect (runbook), xoá `nginx-test`.
3. Quyết định 4 file untracked ở gốc repo mang tên khách hàng thật.
4. Mật khẩu console ở scratchpad `ADMIN_PASSWORD.txt` — cất rồi xoá.

## Không được làm lại
Toàn bộ hạ tầng Cloudflare (xong, verified), 3 bản vá phân quyền/kiểu (xong, có test),
gỡ CI (xong), landing page (xong, deployed).

## Trạng thái cluster KHÔNG nằm trong git (mất là phải tạo lại tay)
Secret `aoip-dex-public-config` + `aoip-provider-portal-public-secret` (client secret
**hex** — không dùng base64, xem bên dưới) · Redis `portal:proles:danghien2907@gmail.com`
= `platform_owner` (seed thủ công) · `~/.cloudflared/config.yml` + credentials
`26e56eb8-...` · LaunchAgent plist · `~/.config/cloudflare/omnisre-pages.token`.

## Bẫy đã trả giá (đừng lặp lại)
1. **`client_secret` base64 phá HTTP Basic của Dex** — `+` bị URL-decode thành dấu
   cách (RFC 6749 §2.3.1). Dùng `openssl rand -hex`. Triệu chứng hiển thị là
   "invalid or expired state" — **đọc log lần callback ĐẦU TIÊN**, các lần sau luôn
   400 do state one-time.
2. **`wrangler login` thất bại nếu authorize ở thiết bị khác** — callback về
   `localhost:8976` của máy chạy lệnh. Dùng API token.
3. **Token Pages·Edit không liệt kê được account** → wrangler báo "Failed to retrieve
   account IDs". Không phải lỗi token; script tự trích từ `~/.cloudflared/cert.pem`.
4. **Thiếu `404.html` → Pages trả 200 cho MỌI path lạ.**
5. **Cache edge trễ ~1 phút** sau deploy Pages — đo ngay sẽ thấy nội dung cũ.
6. **`cloudflared --config` là flag TOÀN CỤC**, phải đứng trước `tunnel`.
7. **`rollout restart` KHÔNG build image** (`IfNotPresent` + `:latest`) — dùng
   `make sync-public*`, nó so `imageID` pod với image local.
8. **`.items[0]` chọn nhầm pod Terminating** ngay sau `rollout status`.

## Tài liệu
`docs/plans/production-roadmap.md` · `docs/adr/0001-cloudflare-pages-tunnel-local-core.md` ·
`docs/deployment/cloudflare-macbook.md` · `docs/runbooks/cloudflare-public-access.md` ·
`cloudflare/PAGES.md` · `CLAUDE.md` (mục PUBLIC PLANE)
