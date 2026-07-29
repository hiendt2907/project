# Triển khai mặt public: Cloudflare + core trên MacBook

Đưa Omni Console ra `https://app.omnisre.xyz` và landing page ra
`https://www.omnisre.xyz` mà không thuê VPS, không mở port router, không cần IP tĩnh.

Quyết định kiến trúc và lý do: `docs/adr/0001-cloudflare-pages-tunnel-local-core.md`.
Vận hành hằng ngày và rollback: `docs/runbooks/cloudflare-public-access.md`.

---

## Kiến trúc

```
                    ┌─────────────── Cloudflare ───────────────┐
Trình duyệt ──TLS──►│  www.omnisre.xyz  → Pages (HTML tĩnh)     │
                    │  app.omnisre.xyz  → Access → Tunnel       │
                    └──────────────────┬───────────────────────┘
                                       │ cloudflared (LaunchAgent)
                                       ▼
                            MacBook · OrbStack K8s · ns multi-agent
                            Traefik 192.168.139.2:80
                                       │  Host: app.omnisre.xyz
                    ┌──────────────────┼──────────────────┐
                    ▼                  ▼                  ▼
            /auth, /api/…          /dex                  /
   aoip-provider-portal-public  aoip-dex-public   aoip-provider-web-public
                    └──────────────────┴──────────────────┘
                                       │ dùng chung
                             Redis · PostgreSQL · omni-gateway
                                  (không cái nào public)
```

Lab `provider.ai-agent.local` chạy song song, **không đổi gì**.

---

## Điều kiện tiên quyết

| | Kiểm tra |
|---|---|
| Domain trỏ Cloudflare | `dig +trace NS omnisre.xyz \| tail -3` → phải thấy `*.ns.cloudflare.com` |
| Tài khoản Cloudflare | Free tier đủ |
| cloudflared | `cloudflared --version` (chưa có: `brew install cloudflared`) |
| Cluster sống | `kubectl -n multi-agent get pods` |
| Traefik | `kubectl -n traefik get svc traefik` → EXTERNAL-IP `192.168.139.2` |
| Image đã build | `multi-agent-system:latest`, `aoip-provider-web:latest` |

> **Trạng thái lúc viết tài liệu (2026-07-29):** registrar đã đổi NS sang Cloudflare
> (`ROCKY`/`RAFE.NS.CLOUDFLARE.COM`) nhưng zone `.xyz` vẫn trả `ns*.matbao.com`.
> Domain tạo cùng ngày. **Chờ propagation** trước khi làm Bước 3 trở đi.
> Bước 1–2 làm được ngay, không cần DNS.

---

## Bước 1 — Deploy public plane vào Kubernetes

### 1.1 Tạo Secret cho Dex public

Không tái dùng credential lab: mật khẩu `Password123!` và secret
`provider-portal-secret` đã nằm công khai trong git history. Dùng lại cho một
endpoint ra Internet đồng nghĩa với không có mật khẩu.

> ⚠️ **Client secret phải là hex.** RFC 6749 §2.3.1 buộc form-urlencode credential
> trong HTTP Basic; Dex URL-decode nên `+` trong secret base64 thành dấu cách, trong
> khi `httpx` gửi Basic thô. Kết quả: `invalid client_secret on token request` dù hai
> Secret trong K8s **giống hệt nhau** — rất khó chẩn đoán. Hex không có ký tự nào bị
> biến dạng.

```bash
# Sinh client secret — HEX, không phải base64 (xem cảnh báo trên)
DEX_CLIENT_SECRET="$(openssl rand -hex 32)"

# Sinh bcrypt hash cho mật khẩu đăng nhập console
htpasswd -bnBC 10 "" 'MẬT_KHẨU_MẠNH_CỦA_BẠN' | tr -d ':\n' | sed 's/\$2y/\$2a/'

cp cloudflare/k8s/aoip-dex-public-config.example.yaml /tmp/dex-public.yaml
# Điền: __DEX_PUBLIC_CLIENT_SECRET__, __DEX_PUBLIC_ADMIN_EMAIL__,
#       __DEX_PUBLIC_ADMIN_BCRYPT_HASH__, __DEX_PUBLIC_ADMIN_USERNAME__,
#       __DEX_PUBLIC_ADMIN_UUID__  (uuidgen)

kubectl -n multi-agent create secret generic aoip-dex-public-config \
    --from-file=config.yaml=/tmp/dex-public.yaml

rm -P /tmp/dex-public.yaml     # macOS; Linux: shred -u
```

### 1.2 Secret cho backend public

```bash
kubectl -n multi-agent create secret generic aoip-provider-portal-public-secret \
    --from-literal=AOIP_OIDC_PROVIDER_CLIENT_SECRET="$DEX_CLIENT_SECRET"
unset DEX_CLIENT_SECRET
```

> Giá trị này phải **khớp tuyệt đối** `staticClients[0].secret` trong config Dex
> public. Lệch ⇒ callback fail ở bước đổi code lấy token.

### 1.3 Apply

```bash
kubectl apply -f k8s/deployments/aoip-dex-public.yaml
kubectl apply -f k8s/deployments/aoip-provider-portal-public.yaml
kubectl apply -f k8s/ingress/omnisre-public.yaml

kubectl -n multi-agent rollout status deploy/aoip-dex-public
kubectl -n multi-agent rollout status deploy/aoip-provider-portal-public
kubectl -n multi-agent rollout status deploy/aoip-provider-web-public
```

### 1.4 Email đăng nhập phải có role provider

Qua được OIDC nhưng không có role sẽ bị 403 `no authorized role for this portal`
(`src/aoip/console/app.py:160-162`).

```bash
kubectl -n multi-agent exec deploy/aoip-provider-portal-public -- \
  python -c "
import asyncio, os, redis.asyncio as r
async def m():
    c = r.from_url(os.environ['OMNI_REDIS_URL'], decode_responses=True)
    print('roles:', await c.smembers('portal:proles:EMAIL_CỦA_BẠN'))
asyncio.run(m())"
```

Rỗng ⇒ seed bằng `src/aoip/console/seed_identity.py`.

### 1.5 Kiểm tra tại chỗ, chưa cần Internet

```bash
curl -s -H 'Host: app.omnisre.xyz' \
  http://192.168.139.2/dex/.well-known/openid-configuration | head -3
# phải thấy "issuer":"https://app.omnisre.xyz/dex"

curl -sI -H 'Host: app.omnisre.xyz' http://192.168.139.2/ | head -1     # 200
curl -sI -H 'Host: provider.ai-agent.local' http://192.168.139.2/ | head -1  # 200 — lab còn sống
```

---

## Bước 2 — Landing page lên Cloudflare Pages (Direct Upload)

**Không nối repo vào Cloudflare.** Repo là private và chứa manifest RBAC, tên topic
Kafka, mẫu DSN cùng lịch sử commit liên quan pentest; cấp cho Cloudflare quyền đọc
nó chỉ để phục vụ 5 file tĩnh là mở một đường truy cập thừa. Direct Upload chỉ đẩy
đúng thư mục `cloudflare/pages/`.

```bash
npx --yes wrangler@latest login    # một lần duy nhất, mở browser
make deploy-landing
```

Sau lần deploy đầu: Dashboard → Workers & Pages → `omnisre` → Custom domains →
`www.omnisre.xyz`.

`make deploy-landing` chặn trước nếu có file `.md` hay `.DS_Store` lọt vào thư mục
được phục vụ — **mọi file trong `cloudflare/pages/` đều truy cập được từ Internet**.

Cũng không dùng GitHub Actions: repo đã gỡ toàn bộ workflow vì hết quota
(commit `2038de1`). Pages build trên hạ tầng Cloudflare nên không tiêu phút Actions.

---

## Bước 3 — Tạo Tunnel *(cần DNS đã sang Cloudflare)*

```bash
cloudflared tunnel login          # mở browser, chọn zone omnisre.xyz
cloudflared tunnel create omnisre # in ra <TUNNEL_ID>, sinh ~/.cloudflared/<ID>.json

cp cloudflare/tunnel/config.example.yml ~/.cloudflared/config.yml
# thay __CLOUDFLARE_TUNNEL_ID__ và __HOME__

cloudflared tunnel ingress validate --config ~/.cloudflared/config.yml
cloudflared tunnel route dns omnisre app.omnisre.xyz
```

> `credentials-file` JSON là **khoá của tunnel**. `chmod 600`. Không commit.
> `.gitignore` đã chặn nhưng đừng dựa vào một lớp phòng thủ.

---

## Bước 4 — Cloudflare Access

Zero Trust → Access → Applications → Add an application → **Self-hosted**.

| Trường | Giá trị |
|---|---|
| Application name | `Omni Console` |
| Session duration | `24 hours` |
| Subdomain / Domain | `app` / `omnisre.xyz` |
| Path | *(trống — bảo vệ cả `/dex` và `/auth`)* |

Policy:

| | |
|---|---|
| Name | `admin-only` |
| Action | `Allow` |
| Include | Emails → `danghien2907@gmail.com` |

Authentication: bật **One-time PIN**. Không cần IdP ngoài.

> Access **không** thay thế đăng nhập của portal. Sau Access bạn vẫn phải đăng nhập
> OIDC. Đó là thiết kế, không phải trùng lặp thừa.

---

## Bước 5 — Chạy tunnel như dịch vụ macOS

```bash
bash cloudflare/tunnel/install-macos.sh
```

Script fail-closed: thiếu config, còn placeholder, thiếu credentials, quyền file sai,
hoặc ingress không validate ⇒ **dừng, không cài**.

```bash
launchctl list | grep com.omnisre.cloudflared
tail -f ~/Library/Logs/omnisre-cloudflared.err.log
```

---

## Bước 6 — Kiểm chứng

```bash
bash cloudflare/tunnel/verify.sh
```

Chạy 6 nhóm gate: lab invariance, resource isolation, public plane, surface
containment, lifecycle, edge. **SKIP ≠ PASS** — script nói rõ.

Kiểm tra thủ công không thể tự động hoá:

1. Ẩn danh mở `https://app.omnisre.xyz/` → phải thấy màn hình Cloudflare Access.
2. Nhập email admin → nhận PIN → vào được.
3. Bấm đăng nhập OIDC → redirect tới `https://app.omnisre.xyz/dex/auth`
   (**không phải** `dex.ai-agent.local`).
4. Đăng nhập → quay về `https://app.omnisre.xyz/` với phiên hợp lệ.
5. Mở `http://provider.ai-agent.local/` → lab vẫn đăng nhập bình thường,
   redirect vẫn tới `dex.ai-agent.local`.

Bước 5 là gate quan trọng nhất: nó chứng minh isolation.

---

## Rollback

Xem mục Rollback trong `docs/runbooks/cloudflare-public-access.md`. Tóm tắt: dừng
tunnel là public tắt ngay; core và lab không bị ảnh hưởng; **không cần** revert
issuer lab vì issuer lab chưa từng bị đổi.

---

## Xoay secret

| Secret | Cách xoay |
|---|---|
| Dex client secret | Tạo lại cả hai Secret (1.1 + 1.2) → `rollout restart` cả hai deploy |
| Mật khẩu console | Sinh hash mới → `kubectl create secret --dry-run=client -o yaml \| kubectl apply -f -` → restart Dex public |
| Credentials tunnel | `cloudflared tunnel delete omnisre` → tạo lại → cập nhật config → chạy lại install |
| `OMNI_GATEWAY_API_KEY` | Ngoài phạm vi — dùng chung với lab |

---

## Gỡ bỏ hoàn toàn

```bash
bash cloudflare/tunnel/uninstall-macos.sh
kubectl delete -f k8s/ingress/omnisre-public.yaml
kubectl delete -f k8s/deployments/aoip-provider-portal-public.yaml
kubectl delete -f k8s/deployments/aoip-dex-public.yaml
kubectl -n multi-agent delete secret aoip-dex-public-config aoip-provider-portal-public-secret
cloudflared tunnel delete omnisre
# Dashboard: xoá Access application, DNS record `app`, Pages project
```

Không đụng schema PostgreSQL, không xoá key Redis, không đổi port binding của core.

---

## Chưa làm ở iteration này

`api.omnisre.xyz` và `agent.omnisre.xyz` **chưa public**. Kiến trúc đã sẵn đường mở
(thêm một entry vào `ingress:` của tunnel + một Ingress rule) nhưng mở chúng cần một
phase riêng: service authentication, rate limiting, replay protection, và verification
đầy đủ. Chặn kỹ thuật đã biết: `/auth` và `_require_api_key` **chưa có rate limit ở
tầng ứng dụng** — phải giải quyết trước.
