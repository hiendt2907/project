# Runbook — mặt public Cloudflare (`app.omnisre.xyz`)

Vận hành, kiểm tra, chẩn đoán, rollback. Triển khai lần đầu:
`docs/deployment/cloudflare-macbook.md`. Lý do kiến trúc:
`docs/adr/0001-cloudflare-pages-tunnel-local-core.md`.

---

## Kiểm tra nhanh

```bash
bash cloudflare/tunnel/verify.sh
```

**SKIP không phải PASS.** SKIP nghĩa là gate chưa chạy được (chưa deploy, chưa có
DNS, chưa có credential), không phải đã đạt.

---

## Trust boundary

| Vùng | Ai vào được | Cái gì ở đây |
|---|---|---|
| **Public** | Toàn Internet | `www.omnisre.xyz` (Pages). Không auth, không dữ liệu. |
| **Protected** | 1 email qua Access + OIDC + role | `app.omnisre.xyz` — console, `/auth`, `/api/provider/v1`, `/dex` |
| **Private** | Chỉ trong mạng OrbStack | Redis, PostgreSQL, Kafka, omni-gateway, toàn bộ lab `.local` |

Secret nằm ở đâu:

| Secret | Nơi lưu |
|---|---|
| Dex public config (client secret + password hash) | K8s Secret `aoip-dex-public-config` |
| OIDC client secret của backend | K8s Secret `aoip-provider-portal-public-secret` |
| Credentials tunnel | `~/.cloudflared/<ID>.json` (chmod 600) |
| Cert tài khoản Cloudflare | `~/.cloudflared/cert.pem` |
| `OMNI_GATEWAY_API_KEY` | K8s Secret `omni-gateway-secret` (dùng chung với lab) |

**Không cái nào ở trong git.**

---

## Kiểm tra từng mặt

### Landing page sống?
```bash
curl -sI https://www.omnisre.xyz/ | head -1        # HTTP/2 200
```

### Console bị Access chặn?
```bash
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' https://app.omnisre.xyz/
```
Kỳ vọng `302 https://<team>.cloudflareaccess.com/...`.
**Trả `200` là sự cố bảo mật** — Access chưa bật hoặc bị gỡ. Dừng tunnel ngay.

### Tunnel kết nối?
```bash
launchctl list | grep com.omnisre.cloudflared
cloudflared tunnel info omnisre
tail -50 ~/Library/Logs/omnisre-cloudflared.err.log
```

### Public plane khoẻ?
```bash
kubectl -n multi-agent get pods -l omni.io/plane=public
curl -s -H 'Host: app.omnisre.xyz' \
  http://192.168.139.2/dex/.well-known/openid-configuration | head -3
```

### Lab còn nguyên vẹn? *(gate quan trọng nhất)*
```bash
curl -sI -H 'Host: provider.ai-agent.local' http://192.168.139.2/ | head -1

kubectl -n multi-agent get deploy aoip-provider-portal \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="AOIP_OIDC_PROVIDER_ISSUER")].value}'
# BẮT BUỘC là http://dex.ai-agent.local/dex
```

### Database và Redis không public?
```bash
kubectl -n multi-agent get svc -o wide | grep -v ClusterIP   # chỉ dòng tiêu đề
grep -E 'hostname:' ~/.cloudflared/config.yml                 # chỉ app.omnisre.xyz
```

### Catch-all hoạt động?
```bash
grep -A1 'http_status:404' ~/.cloudflared/config.yml
```

---

## Chẩn đoán sự cố

### `https://app.omnisre.xyz` → 502 / 530

Tunnel không tới được Traefik.

```bash
tail -50 ~/Library/Logs/omnisre-cloudflared.err.log
curl -sI -H 'Host: app.omnisre.xyz' http://192.168.139.2/     # Traefik trực tiếp
kubectl -n traefik get svc traefik                            # IP còn đúng?
```

OrbStack cấp lại IP LB là nguyên nhân phổ biến nhất. Sửa `service:` trong
`~/.cloudflared/config.yml` rồi `launchctl kickstart -k gui/$(id -u)/com.omnisre.cloudflared`.

### Traefik trả 404

`Host` header không khớp Ingress nào.

```bash
kubectl -n multi-agent get ingress omnisre-public-console -o yaml | grep -A2 'host:'
```

Config tunnel **không được** có `httpHostHeader` — Ingress khai báo đúng
`app.omnisre.xyz` nên Host gốc phải giữ nguyên.

### Login OIDC redirect sai chỗ

Nếu bị đưa tới `dex.ai-agent.local` ⇒ đang chạm backend **lab**, không phải public.

```bash
kubectl -n multi-agent get deploy aoip-provider-portal-public \
  -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' | grep OIDC
```

Cũng kiểm tra Ingress `/` trỏ `aoip-provider-web-public`, **không phải**
`aoip-provider-web`.

### Callback lỗi `issuer mismatch`

`AOIP_OIDC_PROVIDER_ISSUER` và `issuer` trong Secret Dex public phải **giống hệt**
từng ký tự (`src/aoip/console/oidc.py:104` so sánh chuỗi tuyệt đối).

```bash
kubectl -n multi-agent get secret aoip-dex-public-config \
  -o jsonpath='{.data.config\.yaml}' | base64 -d | grep '^issuer:'
```

### Đăng nhập được nhưng 403 `no authorized role`

OIDC thành công nhưng email không có role provider. Xem Bước 1.4 của deployment guide.

### Mọi POST trả 403 `cross-origin mutation blocked`

`AOIP_PROVIDER_ORIGINS` thiếu `https://app.omnisre.xyz` (`app.py:171-190`).

### Pod Dex public `Pending` / `CreateContainerConfigError`

Thiếu Secret. **Đây là fail-closed đúng thiết kế**, không phải bug.

```bash
kubectl -n multi-agent get secret aoip-dex-public-config
```

---

## Rollback

Theo thứ tự. Mỗi bước độc lập — dừng ở bất kỳ mức nào.

**Mức 1 — tắt public ngay (10 giây), giữ mọi thứ khác**
```bash
bash cloudflare/tunnel/uninstall-macos.sh
```

**Mức 2 — gỡ khỏi Cloudflare**
Dashboard: xoá DNS record `app`; xoá Access application `Omni Console`.

**Mức 3 — gỡ public plane khỏi cluster**
```bash
kubectl delete -f k8s/ingress/omnisre-public.yaml
kubectl delete -f k8s/deployments/aoip-provider-portal-public.yaml
kubectl delete -f k8s/deployments/aoip-dex-public.yaml
kubectl -n multi-agent delete secret aoip-dex-public-config aoip-provider-portal-public-secret
```

**Mức 4 — huỷ tunnel**
```bash
cloudflared tunnel delete omnisre
rm -f ~/.cloudflared/<TUNNEL_ID>.json ~/.cloudflared/config.yml
```

**Mức 5 — gỡ landing page**
Dashboard → Pages → project → Settings → Delete.

### Không có trong rollback, có chủ đích

- **Không revert issuer lab** — issuer lab chưa từng bị đổi. Đây chính là lý do tách plane.
- Không đụng schema PostgreSQL.
- Không xoá key Redis. `portal:user:` / `proles:` / `membership:` dùng chung với lab —
  xoá là hỏng đăng nhập lab.
- Không đổi port binding của core.

Kiểm chứng sau rollback:
```bash
curl -sI -H 'Host: provider.ai-agent.local' http://192.168.139.2/ | head -1   # 200
kubectl -n multi-agent get pods -l omni.io/plane=public                       # rỗng
```

---

## Đồng bộ code local → public

```bash
make sync-public-ui        # chỉ Next.js shell
make sync-public-backend   # chỉ FastAPI console
make sync-public           # cả hai
make sync-public-all       # cả public LẪN lab .local
```

Vòng lặp nhanh khi sửa giao diện (không đụng cluster):

```bash
kubectl -n multi-agent port-forward svc/aoip-provider-portal-public 8081:8081 &
cd ui && AOIP_BACKEND_URL=http://localhost:8081 npm run dev -w @aoip/provider-portal
```

### Vì sao phải qua script, không `kubectl rollout restart` tay

`imagePullPolicy: IfNotPresent` + tag `:latest` ⇒ **restart không build lại gì**.
"deployment successfully rolled out" là tín hiệu giả — pod vẫn có thể chạy image cũ.
`scripts/sync_public_plane.sh` build trước rồi **so `imageID` của mọi pod Running với
image local**; lệch là fail, không phải cảnh báo.

### Blast radius

Lab và public dùng chung tag `aoip-provider-web:latest` và `multi-agent-system:latest`.
Build là image cũ bị thay ngay, **nhưng pod lab giữ bản cũ tới khi nó restart**. Mặc
định script chỉ restart public — dùng cái này để thử UI mới trên public trước, lab
không đổi. Hệ quả phải biết: nếu lab tự restart vì lý do khác (evict, reboot máy), nó
sẽ nhảy sang UI mới ngoài ý muốn.

Kiểm tra hai bên đang chạy image nào:

```bash
kubectl -n multi-agent get pod -l app=aoip-provider-web        -o jsonpath='{.items[0].status.containerStatuses[0].imageID}{"\n"}'
kubectl -n multi-agent get pod -l app=aoip-provider-web-public -o jsonpath='{.items[0].status.containerStatuses[0].imageID}{"\n"}'
```

---

## Vận hành định kỳ

| Việc | Nhịp | Lệnh |
|---|---|---|
| Kiểm chứng toàn bộ | Hằng tuần | `bash cloudflare/tunnel/verify.sh` |
| Xem log tunnel | Khi có sự cố | `tail -100 ~/Library/Logs/omnisre-cloudflared.err.log` |
| Xoay Dex client secret | 90 ngày | Deployment guide § Xoay secret |
| Xoay mật khẩu console | 90 ngày | như trên |
| Rà Access audit log | Hằng tháng | Zero Trust → Logs → Access |
| Cập nhật cloudflared | Hằng quý | `brew upgrade cloudflared` rồi `launchctl kickstart -k` |

---

## Giới hạn đã biết

- **MacBook là SPOF.** Máy ngủ, reboot, hoặc mất mạng ⇒ console chết. Landing page
  vẫn sống (Pages ở edge). Không khắc phục được trong kiến trúc này.
- **`/auth` không có rate limit ở tầng ứng dụng** (grep rỗng trong
  `src/aoip/console/`). Hiện Access chặn trước nên chưa lộ. **Bắt buộc phải giải
  quyết trước khi public `api.omnisre.xyz`.** Chưa xác minh quota rule miễn phí của
  Cloudflare — phải tra tài liệu hiện hành trước khi dựa vào nó.
- **Chặng cloudflared → Traefik là HTTP.** Nằm trọn trong máy; lý do đầy đủ ở ADR.
- **Cloudflare thấy plaintext** sau khi terminate TLS. Bản chất reverse proxy.
- **Dex public `storage: memory`** — restart là mất luồng OIDC đang dở. Người dùng
  chỉ cần đăng nhập lại.
- **Access session (24h) và phiên Omni (8h) hết hạn độc lập.** Có thể còn Access
  nhưng mất phiên Omni; biểu hiện là bị đá về màn hình đăng nhập OIDC.
- **`api.` và `agent.` chưa public.** Cố ý.
