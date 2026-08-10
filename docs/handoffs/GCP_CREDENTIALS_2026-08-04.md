# Credentials — hạ tầng GCP omni-k3s-vm (omnisre.xyz)

> Tạo ngày 2026-08-04. Đây là bản chụp giá trị **thật đang sống** trên cluster
> tại thời điểm ghi — lấy trực tiếp qua `kubectl get secret ... | base64 -d`,
> không phải suy đoán. Xóa/di chuyển file này khỏi git ngay sau khi lưu vào
> password manager — **không commit file này**.

## Hạ tầng / SSH

- **GCP Project ID**: `project-b32bedc2-6395-44a9-b61`
- **VM**: `omni-k3s-vm`, zone `asia-southeast1-c`, IP public `136.85.2.181`
- **SSH**: `gcloud compute ssh omni-k3s-vm --zone=asia-southeast1-c --tunnel-through-iap` (không cần password riêng, xác thực qua gcloud IAM/IAP)
- **kubectl trên VM**: `export KUBECONFIG=~/.kube/config` (không dùng `sudo kubectl` mặc định — file `/etc/rancher/k3s/k3s.yaml` chỉ root đọc được)

## Gitea (self-hosted git, namespace `cicd`)

- **URL nội bộ cluster**: `http://gitea.cicd.svc.cluster.local:3000`
- **URL NodePort (từ ngoài VM)**: `http://100.67.117.19:30300`
- **User**: `hiendang`
- **Token (dùng làm password khi git push/API)**: `9c7d05ba9ad461501a851864`

## Jenkins (CI/CD)

- **URL**: `http://100.67.117.19:30080` (NodePort — Jenkins migrated in-cluster
  2026-08-10, pod in namespace `cicd`; the old `:8080` VM systemd service is
  stopped+disabled, see `k8s/gitops/jenkins-incluster.yaml`)
- **User**: `hiendang`
- **Password**: `D@ngT4phi3n2026`

## Grafana (namespace `monitor`)

- **URL public**: `https://grafana.omnisre.xyz` — chặn bằng Traefik BasicAuth (xem mục "Monitoring BasicAuth" bên dưới), qua lớp đó mới tới màn login Grafana thật.
- **User Grafana**: `admin`
- **Password Grafana**: `fac90f4f22ee5a1bcf7392be15e107bb`
- Lưu trong k8s Secret `grafana-admin` (namespace `monitor`) — Jenkinsfile tự sinh lần đầu, giữ nguyên các lần sau.

## Monitoring BasicAuth (chặn ngoài Grafana/Prometheus/Vault UI)

Lớp bảo vệ "chỉ tôi được vào" trước khi tới các UI monitoring thật — thêm 2026-08-04 theo yêu cầu bảo mật tuyệt đối. Cùng 1 user/pass dùng chung cho cả 3 URL:

- **URL**: `https://grafana.omnisre.xyz`, `https://prometheus.omnisre.xyz`, `https://vault.omnisre.xyz` (Vault UI — xem cảnh báo ở mục HashiCorp Vault bên dưới)
- **User**: `hiendang`
- **Password**: `COEyifX9ypL0cTXoy43eKIhv`
- Lưu trong k8s Secret `monitoring-basicauth` (namespace `monitor`, key `users`, dạng htpasswd bcrypt).
- **Nâng cấp khuyến nghị**: thay bằng Cloudflare Access (chỉ 1 email, OTP) khi có Cloudflare API Token đủ quyền `Account > Access: Apps and Policies > Edit` — cùng cơ chế đã dùng cho `app.omnisre.xyz`.

## Harbor (registry.omnisre.xyz)

- **URL**: `https://registry.omnisre.xyz` (chưa test — `harbor-values.yaml` ghi ClusterIP-only,
  không public ingress; nếu URL này không sống, dùng `svc/harbor` ClusterIP nội bộ)
- **User**: `admin`
- **Password**: `5d39921bfc6d263ae5784f80fb5e5d348e7ac286` (reset 2026-08-10 — cả secret cũ lẫn
  giá trị cũ trong doc này đều KHÔNG khớp Harbor thật, xác nhận qua log `harbor-core`
  "Invalid credentials"; reset trực tiếp qua `UPDATE harbor_user` trên Postgres của Harbor,
  đúng scheme `pbkdf2_sha256` — PBKDF2-HMAC-SHA256, 600000 iterations, dklen=16, xem
  `src/common/utils/encrypt.go` upstream Harbor v2.15.2)
- Lưu trong k8s Secret `harbor-admin-bootstrap` (namespace `harbor`) — đã đồng bộ lại.

## ArgoCD (argocd.omnisre.xyz)

- **URL**: `https://argocd.omnisre.xyz`
- **User**: `admin`
- **Password**: `hK9PKwFzggfWU3-M`
- Lưu trong k8s Secret `argocd-initial-admin-secret` (namespace `argocd`) — **nên đổi qua `argocd account update-password` sau khi lưu**, đây là password khởi tạo mặc định của ArgoCD, không tự đổi.

## HashiCorp Vault (nội bộ, không public)

- **URL nội bộ**: `http://vault.vault.svc.cluster.local:8200` (chỉ truy cập qua `kubectl port-forward` hoặc từ trong cluster — cố ý không expose ra Internet)
- **Root token**: `hvs.NpGBqGuiXetSOMrY15lXjdmS`
- **Unseal key** (Shamir 1-of-1 — mất key này là mất toàn bộ dữ liệu Vault): `68sRbxllbtBfRavE54VFbcL6VD9a7/CILs5USpCLK8g=`
- Cả hai giá trị trên cũng lưu trong k8s Secret `vault-unseal-bootstrap` (namespace `vault`) để Jenkins tự unseal lại khi pod restart.

## PostgreSQL (`omni-postgres`, namespace `multi-agent`)

**Cập nhật 2026-08-04 (security sweep)**: password cũ `omni-admin-s3cr3t-2026` (từng
commit plaintext trong `k8s/deployments/omni-postgres.yaml`) đã **rotate thật** —
`ALTER USER omni WITH PASSWORD ...` trong Postgres + patch Secret. Giá trị cũ không
còn hoạt động. `omni-postgres.yaml` không còn Secret trong git nữa — Jenkinsfile
bootstrap-once (`openssl rand -hex 20`, chỉ tạo nếu chưa có). Đọc DSN hiện tại:
```bash
kubectl get secret omni-pg-secret -n multi-agent -o jsonpath='{.data.OMNI_ADMIN_PG_DSN}' | base64 -d
```

## Redis (`redis`, namespace `multi-agent`)

- Không có password (unauthenticated), chỉ truy cập nội bộ cluster: `redis://redis:6379/0`

## Omni Gateway API Key

- **Key**: `c6711b3782795da5427de8d5273f83d1f45b9d963392ba3f`
- Lưu trong k8s Secret `omni-gateway-secret` (namespace `multi-agent`) — **giờ do Vault + External Secrets Operator quản lý** (nguồn thật ở Vault path `secret/omni-gateway-secret`, key `OMNI_GATEWAY_API_KEY`).

## Dex OIDC (aoip-dex, namespace `multi-agent`, GCP)

**Cập nhật 2026-08-04 (security sweep)**: 5 tài khoản test password (owner/support/
sre/approver..., mật khẩu chung `Password123!`) đã **GỠ BỎ HẲN** khỏi Dex GCP — chúng
từng nằm plaintext trong git (`aoip-dex.gcp.yaml`) và sống trên `dex.omnisre.xyz`
public, bao gồm cả tài khoản "Provider owner". Không còn đăng nhập bằng password nào
trên Dex GCP nữa — chỉ còn OIDC client-credential flow (provider/tenant portal).

Client secret (`provider_secret`/`tenant_secret`) giờ nguồn thật ở **Vault**
(`secret/aoip-dex-secret`), sync qua ExternalSecret `aoip-dex-secret` (namespace
`multi-agent`) — xem `k8s/gitops/aoip-dex-external-secret.yaml`. Đọc giá trị hiện tại:
```bash
kubectl get secret aoip-dex-secret -n multi-agent -o jsonpath='{.data.provider_secret}' | base64 -d
kubectl get secret aoip-dex-secret -n multi-agent -o jsonpath='{.data.tenant_secret}' | base64 -d
```
(Lab OrbStack vẫn giữ staticPasswords test — chấp nhận được vì không expose Internet,
chỉ /etc/hosts nội bộ. Client secret lab cũng đã rotate, nguồn ở K8s Secret
`aoip-dex-secret` namespace `multi-agent` context `orbstack`, không qua Vault vì lab
không có Vault.)

## Vaultwarden (self-hosted Bitwarden — nơi lưu trữ lâu dài mọi thông tin trong file này)

- **URL**: `https://bitwarden.omnisre.xyz`
- **Admin panel** (`https://bitwarden.omnisre.xyz/admin`) — dùng để bật/tắt đăng ký tài khoản, quản lý user: **Admin Token**: `t8HwHE/WYtsLCCj4/ZOdBWcj5muobzxN1KwCVW8FqvQ7kfFRqdlA7+VZNhSbSNta` (lưu trong k8s Secret `vaultwarden-admin`, namespace `vaultwarden`, key `admin-token`).
- **Tài khoản cá nhân (email + master password)**: **CHƯA tồn tại** — cần bạn tự tạo bằng cách vào `https://bitwarden.omnisre.xyz`, bấm "Create account", tự chọn email + master password (master password KHÔNG ai khác biết được, kể cả tôi — đây là thiết kế mã hoá đầu-cuối phía client của Bitwarden).
- **Đăng ký tạm thời đang MỞ** (`SIGNUPS_ALLOWED=true`) chỉ để bạn tạo tài khoản đầu tiên — **báo lại ngay sau khi tạo xong** để khoá lại (`SIGNUPS_ALLOWED=false`), tránh người khác tự đăng ký được.
- Sau khi có tài khoản: import toàn bộ nội dung file credentials này vào Vaultwarden dưới dạng Secure Note hoặc từng Login item riêng, rồi **xoá file `docs/handoffs/GCP_CREDENTIALS_2026-08-04.md` khỏi máy** — không cần giữ bản plaintext nữa.

## Cloudflare (DNS zone `omnisre.xyz`)

- **Zone ID**: `423c3b158f759474fe4f198d131b5feb`
- **API Token**: đã dùng LẦN THỨ HAI trong 2 phiên khác nhau (phiên trước + phiên
  2026-08-04 để đổi DNS `app.omnisre.xyz` sang GCP), **không lưu lại ở đây** — cả 2
  lần đều dán trực tiếp trong chat. **Khuyến nghị mạnh**: thu hồi token này hẳn tại
  Cloudflare dashboard (My Profile → API Tokens) và không dán token vào chat nữa —
  dùng biến môi trường cục bộ hoặc 1Password/Vaultwarden CLI thay thế.
- **`app.omnisre.xyz`**: đã đổi từ CNAME→Cloudflare Tunnel (proxied) sang A→GCP IP
  `136.85.2.181` (proxied=false, giống 8 subdomain kia). Cloudflare Tunnel
  (`com.omnisre.cloudflared` + `com.omni.cloudflare-tunnel`, launchd trên MacBook)
  đã tắt hẳn, plist chuyển vào `~/Library/LaunchAgents/disabled/`. `omnisre.xyz`/
  `www.omnisre.xyz` (Cloudflare Pages, `cloudflare/pages/`) KHÔNG đổi — độc lập với
  cả MacBook lẫn GCP từ đầu.

## Tailscale

- **API token đã dùng để mint device key**: đã cung cấp trực tiếp trong chat ở phiên trước — khuyến nghị thu hồi/tạo lại nếu coi chat log là kênh không an toàn tuyệt đối.

---

**Khuyến nghị ngay sau khi lưu file này vào chỗ an toàn:**
1. Xóa file này khỏi working tree (`rm docs/handoffs/GCP_CREDENTIALS_2026-08-04.md`) — không commit.
2. Đổi password ArgoCD admin (mặc định do ArgoCD tự sinh, nên đổi ngay).
3. Cân nhắc thu hồi Cloudflare API Token + Tailscale API Token đã dán trực tiếp trong chat, tạo token mới nếu lo ngại rò rỉ qua log hội thoại.
