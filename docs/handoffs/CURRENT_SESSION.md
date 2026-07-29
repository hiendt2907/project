# Current Session Handoff

## Deliverable hiện tại
Đưa Omni ra Internet qua `omnisre.xyz` bằng Cloudflare Free + core giữ nguyên trên
MacBook. Phạm vi chốt: `www` (landing) + `app` (console có Access). **Không** public
`api.` / `agent.`.
**Phase 1–5 XONG. ĐANG CHẠY THẬT TRÊN INTERNET, login đã verify bằng người thật.**
`verify.sh` → **17 PASS / 0 FAIL / 0 SKIP**. Còn lại duy nhất: quyết định commit/push
(landing page lên Pages phụ thuộc bước này).

## Đang sống trên Internet (2026-07-29)
- `https://app.omnisre.xyz` — Access `holy-wave-800c.cloudflareaccess.com` chặn ẩn danh
  ở **cả 4 path** (`/`, `/auth/login`, `/dex/...`, `/api/provider/v1/me` → 302).
- Tunnel `omnisre` (`26e56eb8-...`), 4 kết nối QUIC, chạy qua LaunchAgent
  `com.omnisre.cloudflared` — tự bật khi máy khởi động.
- Public plane: `aoip-dex-public` + `aoip-provider-portal-public` +
  `aoip-provider-web-public`, đều Ready.
- Login console: `danghien2907@gmail.com` (đã seed `platform_owner` trong Redis).
  Mật khẩu ở scratchpad `ADMIN_PASSWORD.txt` — user cần cất vào password manager.

## Definition of Done
- Public plane **tách hoàn toàn** khỏi lab, không đổi một biến nào của `.local`. → ✅
- Manifest/script/tài liệu đầy đủ, không secret trong git. → ✅
- Verification local pass, gate ngoài đánh dấu Blocked trung thực. → ✅
- Deploy thật lên cluster + Cloudflare. → ⏸ chờ DNS + thao tác Dashboard của user

## Trạng thái hiện tại
`main` @ `7ea24e7`. **CHƯA commit, CHƯA push, CHƯA apply lên cluster** (đúng chỉ thị).
Test: **6737 passed / 0 fail**. Build provider-portal: OK.

## Quyết định kiến trúc đã chốt (KHÔNG thiết kế lại)
1. **Console đi qua Tunnel, không lên Pages** — Next.js `output: "standalone"` +
   4 BFF route giữ `OMNI_GATEWAY_API_KEY` server-side + cookie same-origin.
2. **Tunnel trỏ Traefik `192.168.139.2:80`**, không trỏ từng Service. Tái dùng ingress.
3. **Public auth plane TÁCH RIÊNG** — user bác bỏ phương án đổi issuer Dex lab.
   Isolation thay vì rollback.
4. **Không rewrite Host** — Ingress public tự khai `app.omnisre.xyz`.
5. **HTTP tới origin Traefik** — cert Traefik là self-signed sai SAN; HTTPS sẽ buộc
   `noTLSVerify: true` = TLS giả. Chặng này nằm trọn trong máy.
6. **Không tạo Worker.** Chưa có requirement nào Tunnel không đáp ứng được.

## Phát hiện quan trọng khi inspect
- **Không có `CORSMiddleware` nào trong `src/`** (grep rỗng). Kiến trúc dựa hoàn toàn
  vào same-origin — càng phải giữ `/auth` + `/api` + `/dex` + `/` cùng một host.
- **Frontend KHÔNG dùng chung được.** `aoip-provider-web` có `AOIP_BACKEND_URL` cứng
  trỏ backend lab; server component gọi qua đó kèm cookie (`api-client/src/index.ts:20`).
  Dùng chung ⇒ traffic public chui qua backend lab và **chạy im lặng** vì
  `portal:session:{sid}` chung Redis. Đã tách `aoip-provider-web-public`, cùng image.
- Dex `storage: {type: memory}` ⇒ tách được sạch, không state chung.
- Redis keys: `portal:user:/proles:/membership:` keyed theo email → **cố ý dùng chung**;
  `session:`/`oidc:flow:` keyed ngẫu nhiên → không đụng nhau.
- Cookie không set `domain=` ⇒ host-scoped ⇒ hai jar tách biệt.

## Files thêm (13)
`cloudflare/README.md` · `cloudflare/pages/{index.html,_headers,_redirects,.nojekyll,README.md}` ·
`cloudflare/tunnel/{config.example.yml,com.omnisre.cloudflared.plist.template,install-macos.sh,uninstall-macos.sh,verify.sh}` ·
`cloudflare/k8s/aoip-dex-public-config.example.yaml` ·
`k8s/deployments/{aoip-dex-public.yaml,aoip-provider-portal-public.yaml}` ·
`k8s/ingress/omnisre-public.yaml` ·
`docs/adr/0001-cloudflare-pages-tunnel-local-core.md` ·
`docs/deployment/cloudflare-macbook.md` · `docs/runbooks/cloudflare-public-access.md`

## Đồng bộ code local → public (thêm sau khi user hỏi "muốn đổi UI thì sao")
`scripts/sync_public_plane.sh` + 4 target Makefile: `sync-public`, `sync-public-ui`,
`sync-public-backend`, `sync-public-all`. Đã chạy thật, exit 0.

Lý do tồn tại: `imagePullPolicy: IfNotPresent` + tag `:latest` ⇒ `rollout restart`
KHÔNG build lại gì. Script build trước rồi **so `imageID` mọi pod Running với image
local** — lệch là fail.

Blast radius: lab và public dùng chung tag image. Mặc định script CHỈ restart public;
`--with-lab` mới đụng lab. Đã kiểm chứng: sau `make sync-public-ui`, public chạy
`3b7f335b…`, lab vẫn `c8ccfd4f…`.

**2 bug trong chính script này, bắt được nhờ chạy thật:**
- `.items[0]` chọn nhầm pod **Terminating** ngay sau `rollout status` → báo động giả
  ngay lần chạy đầu. Fix: lọc `--field-selector=status.phase=Running`, đòi MỌI pod
  Running khớp, kèm retry 10×2s.
- Smoke test `curl` timeout vì Traefik chưa kịp cập nhật endpoint; `set -e` giết
  script bằng exit 28 khó hiểu. Fix: retry 10×3s + `|| true`.

## Files sửa (6)
`.env.example` (biến public, không secret) · `.gitignore` (chặn credential) ·
`docs/CODEBASE.md` (mục `cloudflare/`) · `docs/handoffs/CURRENT_SESSION.md` ·
`Makefile` (4 target `sync-public*` + `.PHONY`) · `CLAUDE.md` (mục PUBLIC PLANE +
`INV_PUBLIC_PLANE_ISOLATED`)

## Memory đã ghi (ngoài repo)
`project_cloudflare_public_plane_2026_07_29` (kiến trúc + isolation) ·
`project_oidc_client_secret_base64_gotcha` (bug hex vs base64 + cách chẩn đoán).
Cả hai đã có pointer trong `MEMORY.md`.

Sửa thêm trong chính phiên này (sau khi đã tạo lần đầu):
`cloudflare/tunnel/verify.sh` (3 lần: ExternalName false-positive, semantics nhóm B,
DNS hỏi resolver công cộng, redact JWT Access) · `cloudflare/tunnel/install-macos.sh`
(vị trí flag `--config`) · `cloudflare/k8s/aoip-dex-public-config.example.yaml` +
`docs/deployment/cloudflare-macbook.md` (cảnh báo hex vs base64).

## Trạng thái cluster (KHÔNG nằm trong git — nếu mất phải tạo lại)
2 Secret do operator tạo, không có trong repo:
`aoip-dex-public-config` (config.yaml Dex public) ·
`aoip-provider-portal-public-secret` (client secret **hex**).
Redis: `portal:user:danghien2907@gmail.com` + `portal:proles:...` = `platform_owner`
(đã seed thủ công; không có trong seed script).
`~/.cloudflared/config.yml` + credentials tunnel `26e56eb8-...` (chmod 600).
LaunchAgent `~/Library/LaunchAgents/com.omnisre.cloudflared.plist`.

**Không sửa** `k8s/deployments/aoip-portals.yaml` và `aoip-dex.yaml` — đúng chỉ thị.

## 2 bug thật phát hiện khi chạy thử (không phải suy đoán)
1. **`cloudflared tunnel ingress validate --config X` in help và exit 0** — `--config`
   là flag toàn cục, phải đứng TRƯỚC `tunnel`. Đặt sai ⇒ bỏ qua validation im lặng.
   Đã sửa trong `install-macos.sh`.
2. **`verify.sh` báo động giả** vì coi `ExternalName` (`ollama-service`) là public.
   Chỉ NodePort/LoadBalancer mới mở listener. Đã sửa. Cũng sửa semantics nhóm B:
   "chưa deploy" giờ là SKIP, không phải FAIL.

## Verification đã chạy
- `pytest -q --ignore=tests/integration` → **6737 passed**, 164s
- `npm run build -w @aoip/provider-portal` → OK (mọi route `ƒ` dynamic — xác nhận
  không export tĩnh được)
- `kubectl apply --dry-run=server` × 3 manifest → created (server thật)
- `cloudflared --config … tunnel ingress validate` → **OK**; `ingress rule` xác nhận
  `app.omnisre.xyz` khớp rule #0, `api.omnisre.xyz` rơi vào catch-all 404
- `bash -n` × 3 script; XML plist parse; YAML parse × 5
- `verify.sh` → **4 PASS / 0 FAIL / 10 SKIP**, lab invariance 3/3 PASS
- Secret scan: 0 secret thật. `.gitignore` xác minh bằng `git add --dry-run`

## Blockers
**Landing page chưa connect Cloudflare Pages** — code đã push xong, chỉ còn thao tác
Dashboard của user (connect repo, output dir `cloudflare/pages`, custom domain `www`).

## ĐÃ COMMIT + PUSH (4 commit, 2026-07-29)
`7ea24e7 → a496833`
| Commit | Nội dung |
|---|---|
| `2038de1` | ci: gỡ toàn bộ 4 GitHub Actions — hết quota |
| `1ad0f50` | feat(public): mặt public app.omnisre.xyz (19 file) |
| `db395d6` | docs: ADR 0001 + deployment guide + runbook |
| `a496833` | feat(pages): landing page song ngữ VI/EN, CSP siết tối đa |

**⚠️ Hệ quả gỡ CI: không còn quét secret và chạy test TỰ ĐỘNG trước push.**
Phải chạy tay từ nay:
```bash
docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:v8.18.2 detect \
  --no-git --source=/repo --config=/repo/.gitleaks.toml
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
```
Cả hai đã chạy sạch trước lần push này (no leaks / 6737 passed).
Cloudflare Pages build trên hạ tầng Cloudflare, KHÔNG tốn phút Actions.

## Landing page — song ngữ, không JS
`cloudflare/pages/`: `index.html` (EN, `/`) · `vi/index.html` (VI, `/vi/`) ·
`style.css` dùng chung · `_headers` · `_redirects`.
Hai trang riêng chứ không toggle: CSP chặn JS, và mỗi trang giữ `lang` đúng +
`hreflang` en/vi/x-default + link chia sẻ được. CSS tách file để hai bản không trôi
lệch giao diện. **Sửa nội dung phải sửa CẢ HAI file** — không có cơ chế nào ép.

CSP siết được xuống `default-src 'none'; style-src 'self'` (bỏ `'unsafe-inline'` nhờ
CSS ra file cùng origin). Điều kiện đã verify bằng grep: 0 `<script>`, 0 `style=`
nội tuyến, 0 tài nguyên host ngoài. **Thêm script/font/CDN sẽ bị chặn IM LẶNG** —
lệnh kiểm tra ở `cloudflare/pages/README.md`.

## Bug thật phát hiện thêm ở Phase 5
5. **`client_secret` base64 phá HTTP Basic của Dex** — nghiêm trọng nhất, mất một
   vòng chẩn đoán sai hướng. Triệu chứng hiển thị là `{"detail":"invalid or expired
   state"}` nhưng đó chỉ là **hệ quả**: callback lần đầu trả **500**, `consume_flow`
   đã xoá state (one-time), mọi lần F5 sau chỉ còn 400 → che mất lỗi gốc.
   Log Dex: `invalid client_secret on token request`. Nhưng so hai Secret trong K8s
   thì **len và sha256 giống hệt nhau** — đây là chỗ dễ kết luận nhầm là "khớp rồi,
   lỗi chỗ khác".
   Nguyên nhân: RFC 6749 §2.3.1 buộc form-urlencode credential trong HTTP Basic; Dex
   tuân thủ nên URL-decode, biến `+` trong secret base64 thành **dấu cách**. `httpx`
   (`auth=(id, secret)`, `app.py:134`) gửi Basic thô không encode ⇒ hai bên so hai
   chuỗi khác nhau.
   **Fix**: xoay secret sang `openssl rand -hex 32` (chỉ `[0-9a-f]`). KHÔNG sửa code
   dùng chung với lab. Đã ghi cảnh báo vào template + deployment guide vì bản cũ
   khuyên dùng `-base64 48`, tức đẩy người sau vào đúng bẫy này.
   **Bài học chẩn đoán**: khi callback OIDC lỗi, ĐỌC LOG lần gọi ĐẦU TIÊN — các lần
   sau luôn là 400 "invalid or expired state" do state one-time, không phải nguyên nhân.
3. **`verify.sh` dùng resolver hệ thống** cho gate DNS → âm tính giả: `1.1.1.1` và
   `8.8.8.8` đã trả Cloudflare trong khi DNS ISP còn cache registrar cũ. Đã đổi sang
   hỏi resolver công cộng.
4. **Access ban đầu chưa có ⇒ console mở ra Internet** (`curl` ẩn danh nhận 200).
   Đã dừng tunnel ngay và KHÔNG cài LaunchAgent cho tới khi user tạo xong Access —
   cài trước sẽ tự mở lại cửa đó mỗi lần máy boot.

## Login end-to-end — ĐÃ XÁC MINH BẰNG NGƯỜI THẬT (2026-07-29)
User đăng nhập thành công qua đủ chuỗi: Cloudflare Access (OTP) → console →
OIDC → `app.omnisre.xyz/dex/auth` → callback → phiên portal. Gate cuối cùng đã đóng.
Toàn bộ hệ thống public hoạt động đúng thiết kế.

## Next step chính xác
1. **User connect Cloudflare Pages** (thao tác Dashboard, không tự động hoá được):
   Workers & Pages → Create → Pages → Connect to Git → repo `hiendt2907/project`.
   Framework preset **None**, build command **để trống**, output directory
   `cloudflare/pages`, production branch `main`. Rồi Custom domains → `www.omnisre.xyz`.
   Verify sau đó: `curl -sI https://www.omnisre.xyz/` và `.../vi/`.
2. **User cất mật khẩu** ở scratchpad `ADMIN_PASSWORD.txt` rồi xoá file.
3. Cân nhắc `brew upgrade cloudflared` (2026.5.0 → 2026.7.3).
4. Việc cũ còn treo (mục dưới) — không thuộc deliverable này.

## Trạng thái sau lần sửa cuối
`verify.sh` → **17 PASS / 0 FAIL / 0 SKIP** (chạy lại sau khi restart Dex + portal).
Discovery vẫn trả issuer public đúng. Lab invariance vẫn 3/3 PASS.

## Không được làm lại
Inspect topology/frontend/auth (xong), thiết kế kiến trúc (chốt rồi), sửa issuer lab
(đã bị bác bỏ dứt khoát), tạo Worker (không cần).

## Việc cũ còn treo (từ phiên trước, không thuộc deliverable này)
1. `/autonomy/hitl/pending` và `/decide` chưa scope tenant — vá TRƯỚC khi nối tenant portal.
2. Tenant portal đi qua `src/aoip/console/app.py`, chưa có endpoint reports/HITL.
3. `/approvals` khai `string[]` nhưng backend trả `list[dict]` → React crash.
4. 4 file untracked ở root (3 file mang tên khách hàng `fpt-loyalty`) — chờ quyết định.

## Tài liệu liên quan
`docs/adr/0001-cloudflare-pages-tunnel-local-core.md` ·
`docs/deployment/cloudflare-macbook.md` · `docs/runbooks/cloudflare-public-access.md` ·
`cloudflare/README.md` · `docs/CODEBASE.md`
