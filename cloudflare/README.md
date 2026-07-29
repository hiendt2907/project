# Cloudflare public surface

Mọi thứ liên quan tới việc đưa Omni ra Internet mà **không** thuê VPS, **không** mở
port trên router, **không** cần IP tĩnh. Core vẫn chạy trên MacBook (OrbStack K8s).

```
cloudflare/
├── PAGES.md   tài liệu cho landing page (CỐ Ý nằm ngoài pages/)
├── pages/     landing page tĩnh → www.omnisre.xyz — MỌI file trong đây là public
├── tunnel/    cấu hình + LaunchAgent + script cho cloudflared
└── k8s/       template config chứa secret (KHÔNG commit bản đã điền)
```

Landing page deploy bằng **Direct Upload**, không nối repo vào Cloudflare:

```bash
npx --yes wrangler@latest login    # một lần
make deploy-landing
```

Manifest Kubernetes của mặt public **không** nằm ở đây — chúng theo convention sẵn có
của repo:

- `k8s/deployments/aoip-dex-public.yaml`
- `k8s/deployments/aoip-provider-portal-public.yaml`
- `k8s/ingress/omnisre-public.yaml`

## Nguyên tắc

| | |
|---|---|
| **Public plane tách hoàn toàn khỏi lab** | `app.omnisre.xyz` dùng Dex riêng, backend riêng, shell riêng. `provider.ai-agent.local` không đổi một biến nào. |
| **Tunnel trỏ Traefik, không trỏ Service** | Tái dùng toàn bộ routing/middleware đã có. |
| **Cloudflare không thay thế auth của Omni** | Access ở edge; OIDC + phiên + role vẫn chạy đủ bên trong. |
| **Chưa public `api.` và `agent.`** | Không mở surface chỉ để chứng minh kiến trúc chạy được. |

## Đọc theo thứ tự

1. `docs/adr/0001-cloudflare-pages-tunnel-local-core.md` — vì sao chọn kiến trúc này
2. `docs/deployment/cloudflare-macbook.md` — triển khai từ đầu
3. `docs/runbooks/cloudflare-public-access.md` — vận hành, kiểm tra, rollback

## Không bao giờ commit

`~/.cloudflared/*.json` · `~/.cloudflared/cert.pem` · tunnel token · API token ·
bản đã điền của `k8s/aoip-dex-public-config.example.yaml`.

Đã chặn ở `.gitignore`. Kiểm tra trước mỗi commit:

```bash
git diff --cached | grep -iE 'BEGIN .*PRIVATE|TunnelSecret|AccountTag|client_secret'
```
