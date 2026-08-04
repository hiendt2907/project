# ADR 0001 — Cloudflare Pages + Tunnel, core giữ nguyên trên MacBook

- **Trạng thái**: Superseded bởi [ADR 0002](0002-gcp-k3s-full-migration.md) (2026-08-04)
  — core đã di dời sang GCP k3s, đúng lộ trình "Đường ra khi rời MacBook" ở cuối
  ADR này. Nội dung bên dưới giữ nguyên làm ghi chép lịch sử, không phản ánh kiến
  trúc đang chạy.
- **Ngày**: 2026-07-29
- **Người quyết định**: danghien2907@gmail.com
- **Phạm vi**: mặt public của Omni. Không đụng kiến trúc core.

> ADR đầu tiên của repo. Trước đây quyết định kiến trúc nằm rải ở `docs/architecture/`
> và post-mortem. Đánh số từ `0001`.

## Bối cảnh

Cần đưa Omni ra Internet với một domain riêng (`omnisre.xyz`) trong ràng buộc:
không VPS, không hosting truyền thống, không mở port 80/443 trên router, không IP
tĩnh, không lộ IP mạng nhà. Phải có HTTPS và có xác thực cho console.

Sự thật đã khảo sát, không suy diễn:

- Core chạy trên **Kubernetes (OrbStack)**, namespace `multi-agent` — không phải
  docker-compose. Điểm vào mạng duy nhất là **Traefik LoadBalancer `192.168.139.2:80`**.
- Console là **Next.js `output: "standalone"`** (`ui/apps/provider-portal/next.config.ts:9`)
  kèm 4 BFF route handler giữ `OMNI_GATEWAY_API_KEY` phía server. **Không export tĩnh được.**
- Xác thực là **cookie phiên host-scoped** do FastAPI đặt qua `/auth` **same-origin**;
  ingress cố ý gộp `/auth` + `/api/provider/v1` + `/` trên cùng một host.
- **Không có `CORSMiddleware` nào** trong `src/` (grep rỗng). Kiến trúc dựa hoàn toàn
  vào same-origin.
- PostgreSQL, Redis, Kafka đều là ClusterIP — chưa từng public.

## Quyết định

```
www.omnisre.xyz  → Cloudflare Pages (HTML tĩnh, không build)
app.omnisre.xyz  → Cloudflare Access → Tunnel → Traefik → public plane trong K8s
```

Bốn quyết định con:

**1. Console đi qua Tunnel, không lên Pages.** Vì standalone + BFF + cookie
same-origin. Đây là fallback đã nêu trong yêu cầu, có lý do kỹ thuật cụ thể.

**2. Tunnel trỏ Traefik, không trỏ từng Service.** Tái dùng toàn bộ routing và
middleware đã có. Không dựng thêm một lớp reverse proxy thứ hai.

**3. Mặt public có auth plane RIÊNG.** `aoip-dex-public` + `aoip-provider-portal-public`
+ `aoip-provider-web-public`. Lab `provider.ai-agent.local` không đổi một biến nào.

**4. Chưa public `api.` và `agent.`.** Chưa có remote agent nào thật sự nằm ngoài
mạng local. Không mở surface chỉ để chứng minh kiến trúc chạy được.

## Vì sao phải tách auth plane (quyết định quan trọng nhất)

Phương án rẻ hơn là đổi `issuer` của Dex hiện có sang `https://app.omnisre.xyz/dex`.
Đã bị bác bỏ.

`verify_id_token` so `claims["iss"]` với `cfg.issuer` bằng **so sánh chuỗi tuyệt đối**
(`src/aoip/console/oidc.py:104`). Đổi issuer là thay đổi breaking: `provider.ai-agent.local`
sẽ nhận token có `iss` mới, mọi phiên đang có chết, và lab phụ thuộc vào việc rollback
diễn ra đúng. **Rollback không phải là kiểm soát rủi ro — isolation mới là.**

Tách được vì Dex chạy `storage: {type: memory}` (`k8s/deployments/aoip-dex.yaml:11-12`) —
hai instance không chia sẻ state nào.

Điều bất ngờ: **frontend cũng phải tách.** `aoip-provider-web` có
`AOIP_BACKEND_URL=http://aoip-provider-portal:8081` cứng, và server component gọi
backend qua biến đó kèm cookie chuyển tiếp (`ui/packages/api-client/src/index.ts:20`).
Dùng chung shell ⇒ traffic công khai chui qua backend lab, và vì `portal:session:{sid}`
nằm chung Redis nên nó **hoạt động im lặng** — hợp nhất hai plane mà không có lỗi nào
báo ra. Nên public có shell riêng, **cùng image**, chỉ khác một biến env.

## Cái gì dùng chung, và vì sao an toàn

| Redis key | Khoá theo | Kết luận |
|---|---|---|
| `portal:session:{sid}` | sid ngẫu nhiên | Hai plane không đụng nhau |
| `portal:oidc:flow:{state}` | state ngẫu nhiên | Không đụng; replay chéo bị `iss` chặn |
| `portal:user:` `proles:` `membership:` | email | **Cố ý dùng chung** — cùng người, cùng quyền |

Cookie `aoip_provider_session` không set `domain=` (`app.py:165-167`) ⇒ host-scoped ⇒
hai jar tách biệt trên trình duyệt.

PostgreSQL, omni-gateway dùng chung — read-only từ phía console, đã scope theo tenant.

## Phương án đã cân nhắc và loại

| Phương án | Vì sao loại |
|---|---|
| VPS + reverse proxy | Tốn tiền hàng tháng; vẫn phải chuyển core hoặc dựng tunnel |
| Port forwarding trên router | Lộ IP nhà, cần IP tĩnh/DDNS, tự lo TLS, không có WAF |
| Migrate sang Cloudflare Workers/D1/KV | Viết lại toàn bộ core. Bị cấm rõ ràng và không đáng |
| Ngrok / Quick Tunnel | URL không ổn định, không dùng được với Access |
| Đổi issuer Dex lab | Breaking cho lab; isolation tốt hơn rollback |
| Dùng chung frontend shell | Traffic public chui qua backend lab một cách im lặng |
| Cloudflare Worker làm proxy | Chưa có requirement nào Tunnel không đáp ứng được |

## Hệ quả

**Tích cực** — chi phí 0đ; HTTPS + WAF + DDoS ở edge; IP nhà không lộ; không mở port;
core không đổi một dòng; lab chạy song song; rollback là thao tác cấu hình thuần.

**Tiêu cực** — **MacBook là single point of failure**: máy ngủ/reboot/mất mạng là
console chết. Cloudflare thấy plaintext sau khi terminate TLS. Chặng
cloudflared → Traefik là HTTP (xem dưới). Thêm 3 Deployment cần vận hành. Băng thông
phụ thuộc đường lên của mạng nhà.

**Vì sao HTTP tới origin Traefik** — cert của Traefik là self-signed lab CA, sai SAN
cho host portal; bật HTTPS buộc phải `noTLSVerify: true`, tức TLS giả, tệ hơn HTTP
thẳng. Chặng này nằm **trọn trong một máy** (`192.168.139.2` là LB nội bộ OrbStack).
TLS thật vẫn có ở đoạn duy nhất quan trọng: browser ↔ Cloudflare edge.

## Ảnh hưởng bảo mật

Ba lớp xác thực chồng nhau, không lớp nào bị gỡ:

1. **Cloudflare Access** (edge) — chặn toàn Internet trừ một email.
2. **Dex OIDC + cookie phiên** — chặn người chưa đăng nhập portal.
3. **Principal/role/membership** — chặn người đăng nhập được nhưng không có quyền.

Backend **không đọc** `CF-Access-*`, `CF-Connecting-IP`, `X-Forwarded-For` (grep rỗng)
và ADR này **không** thêm code nào tin chúng. Không có header-spoofing surface mới.

Bypass Access đòi hỏi đã ở trong mạng nội bộ OrbStack — không routable từ Internet.

## Giới hạn đã biết

- MacBook là SPOF. Không khắc phục được trong kiến trúc này.
- `/auth` **không có rate limit ở tầng ứng dụng** (grep rỗng). Hiện Access chặn trước.
  Bắt buộc phải giải quyết trước khi public `api.`.
- Dex `storage: memory` — restart Dex là mất mọi luồng OIDC đang dở.
- Access session và phiên Omni hết hạn độc lập (24h vs 8h).

## Đường ra khi rời MacBook

Kiến trúc này không khoá bạn lại. Khi chuyển sang VPS/K8s cloud:

1. Deploy core ở đích mới, giữ nguyên manifest (chỉ đổi ingress class + storage).
2. Trỏ DNS thẳng tới ingress mới, **giữ nguyên hostname**.
3. Gỡ LaunchAgent cloudflared. Giữ hoặc bỏ Access tuỳ mô hình auth mới.
4. Bật HTTPS đầu-cuối với cert thật; bỏ ngoại lệ HTTP-tới-origin ở trên.

Không có lock-in Cloudflare-specific nào trong code — chỉ ở tầng manifest và DNS.

## Tham chiếu

`docs/deployment/cloudflare-macbook.md` · `docs/runbooks/cloudflare-public-access.md` ·
`cloudflare/README.md`
