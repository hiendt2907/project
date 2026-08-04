# ADR 0002 — Toàn bộ core chuyển sang GCP k3s, thay thế ADR 0001

- **Trạng thái**: Accepted
- **Ngày**: 2026-08-04
- **Người quyết định**: danghien2907@gmail.com
- **Phạm vi**: toàn bộ core Omni (trừ LLM). **Thay thế ADR 0001** cho phần public
  console; ADR 0001 vẫn đúng lịch sử nhưng không còn là kiến trúc đang chạy.

> ADR 0001 đã tự dự đoán đúng lộ trình này ở mục "Đường ra khi rời MacBook" —
> ADR này là lộ trình đó, thực thi thật, không phải suy đoán trước.

## Bối cảnh

ADR 0001 chấp nhận rủi ro MacBook-là-SPOF để đổi lấy chi phí 0đ, dùng Cloudflare
Tunnel đưa `app.omnisre.xyz` từ Internet vào core chạy trên OrbStack tại nhà. Rủi ro
đó được ghi rõ là **không khắc phục được trong kiến trúc đó** — đúng như dự đoán, đây
là lý do trực tiếp dẫn tới quyết định này: người quyết định chủ động rời bỏ ràng buộc
"không VPS, không hosting truyền thống" của ADR 0001 để loại SPOF, chấp nhận đổi lại
bằng chi phí GCP free-credit ($300/3 tháng).

Sự thật đã khảo sát, không suy diễn (2026-08-04):

- GCP VM `omni-k3s-vm` (`asia-southeast1-c`, `e2-custom-8-16384`, disk 100GB, IP
  public `136.85.2.181`) chạy k3s single-node, đã verify sống: 6 domain HTTPS thật
  (`gateway/provider/tenant/dex/registry/argocd/grafana/prometheus/vault.omnisre.xyz`),
  cert Let's Encrypt thật qua cert-manager, không phải self-signed lab CA.
  **Lỗi thời từ commit `a31aeb0` (cùng ngày, sau ADR này)**: `registry.omnisre.xyz` và
  `vault.omnisre.xyz` đã rút ingress công khai, quay lại ClusterIP-only theo yêu cầu
  "chỉ ArgoCD public" — chỉ còn `argocd.omnisre.xyz` cùng
  `gateway/provider/tenant/dex/grafana/prometheus.omnisre.xyz` là public thật. Truy cập
  Harbor/Vault UI nay qua `kubectl port-forward` (xem comment trong
  `k8s/gitops/harbor-values.yaml`).
- Toàn bộ pipeline CI/CD (Gitea + Jenkins, 13 lần build) là **reproducible từ đầu**
  qua Jenkinsfile — không phải trạng thái vá tay một lần không lặp lại được.
- `app.omnisre.xyz` (Cloudflare Tunnel từ ADR 0001) **vẫn đang chạy song song**
  (LaunchAgent `com.omnisre.cloudflared` active, xác nhận `curl` trả 302 redirect
  Access thật) — chưa retired, xem mục "Chưa làm" bên dưới.
- LLM (Ollama, `qwen3:8b`) **cố ý ở lại MacBook** — không di dời. GCP nối tới qua
  Tailscale (`OMNI_VLLM_BASE_URL=http://100.93.3.96:11434/v1`), xác nhận sống qua
  `omni_llm_up` trong Prometheus/Mimir.

## Quyết định

```
Trước (ADR 0001):  Internet → Cloudflare Access → Tunnel → MacBook (OrbStack, core)
Sau  (ADR 0002):   Internet → Let's Encrypt/Traefik → GCP k3s (core)
                                                           ↕ Tailscale
                                                        MacBook (chỉ Ollama)
```

Năm quyết định con:

**1. Domain thật thay self-signed lab CA.** `ai-agent.local` (lab) →
`omnisre.xyz` (GCP), giữ nguyên tên subdomain (`gateway/provider/tenant/dex`).
Theo đúng `INV_PUBLIC_PLANE_ISOLATED` của ADR 0001: **không đổi một biến nào** ở
file lab (`k8s/deployments/aoip-dex.yaml`, `aoip-portals.yaml`,
`k8s/ingress/ai-agent-local.yaml`) — mọi thay đổi domain nằm ở file `.gcp.yaml`
riêng, áp dụng chỉ bởi Jenkinsfile GCP.

**2. LLM ở lại MacBook, nối qua Tailscale — SPOF chuyển vị trí, không biến mất.**
MacBook không còn là SPOF cho **web plane** (portal/gateway/dashboard sống độc lập
trên GCP), nhưng vẫn là SPOF cho **năng lực suy luận** (chẩn đoán LLM, RAG, advisory).
Máy ngủ/mất mạng ⇒ các tính năng cần LLM treo, nhưng gateway/portal/Grafana/Vault UI
vẫn phản hồi bình thường (`/healthz` không phụ thuộc LLM).

**3. GitOps đầy đủ thay vì apply tay.** Không chỉ "di dời", mà dựng luôn Harbor
(registry riêng) + ArgoCD (đồng bộ từ Gitea, `Synced+Healthy` xác nhận qua API) +
Vault + External Secrets Operator (PoC `omni-gateway-secret` verify khớp giá trị) +
Istio (mesh sidecar toàn `multi-agent`) + Argo Rollouts (canary thật, demo 20→50→100%
zero-downtime). Đây là yêu cầu rõ ràng, không phải mở rộng phạm vi tự ý.

**4. Bảo mật khác cơ chế nhưng không giảm lớp.** ADR 0001 dùng 3 lớp (Cloudflare
Access → Dex OIDC → principal/role). ADR 0002 dùng: TLS thật (Let's Encrypt) +
Dex OIDC (không đổi) + Traefik BasicAuth riêng cho Grafana/Prometheus/Vault UI +
Vaultwarden tự host lưu toàn bộ credential (không còn ghi ra file plaintext lâu
dài). Không có Cloudflare Access ở lớp GCP — public trực tiếp qua TLS, đánh đổi
lấy độc lập khỏi Cloudflare Access/MacBook.

**5. Monitoring dựng lại từ metric thật, không kế thừa mù.** 7 dashboard lab cũ bị
xóa vì tham chiếu datasource `mimir` chưa từng deploy trên GCP (luôn "No data").
Dashboard mới ("Omni System — Full Capability View", 64 panel) build từ 114 metric
`omni_*` thật đang chảy, mỗi query verify có dữ liệu trước khi commit.

## Vì sao không giữ nguyên Cloudflare Tunnel (quyết định quan trọng nhất)

Phương án rẻ hơn là giữ MacBook làm core, chỉ thêm GCP làm... gì đó phụ. Bị bác bỏ.

ADR 0001 tự liệt kê giới hạn: *"MacBook là SPOF. Không khắc phục được trong kiến
trúc này."* Đây không phải rủi ro lý thuyết — nó là **thuộc tính cấu trúc** của việc
đặt core trên một máy tính cá nhân không có redundancy, không UPS, không giám sát
uptime độc lập. Không có cấu hình Cloudflare Tunnel/Access nào sửa được điều đó vì
vấn đề nằm ở tầng vật lý bên dưới tunnel, không phải ở tunnel.

GCP VM giải quyết đúng thuộc tính đó: uptime SLA của nhà cung cấp, không phụ thuộc
việc ai đó đóng nắp laptop. Điều **không** giải quyết được: LLM cố ý không di dời
(chi phí GPU cloud không nằm trong ngân sách free-credit, và Apple Silicon tại nhà
đã đủ nhanh cho tải hiện tại) — nên SPOF không biến mất, nó **thu hẹp phạm vi** từ
"toàn bộ hệ thống" xuống "chỉ năng lực cần suy luận LLM".

## Cái gì dùng chung, và vì sao an toàn

| Thành phần | Vị trí | Vì sao an toàn khi tách |
|---|---|---|
| Ollama / qwen3:8b | MacBook, qua Tailscale | Chỉ nhận traffic từ tailnet nội bộ (100.x.x.x), không public |
| PostgreSQL / Redis / Kafka | GCP (mới, tách biệt hoàn toàn khỏi Postgres/Redis/Kafka lab trên OrbStack) | Dữ liệu GCP khởi tạo mới (theo đúng chỉ thị "khởi tạo mới, cleanup local sau"), không migrate — không có state cũ lẫn vào |
| Dex OIDC config (lab vs GCP) | Hai ConfigMap tách biệt (`aoip-dex.yaml` vs `aoip-dex.gcp.yaml`) | `issuer` khác chuỗi tuyệt đối (`http://dex.ai-agent.local/dex` vs `https://dex.omnisre.xyz/dex`) — không có token nào hợp lệ chéo hai bên |
| Vault (HashiCorp) | Chỉ GCP, mới hoàn toàn | Không tồn tại ở lab trước đây — không có gì để đồng bộ/xung đột |

## Phương án đã cân nhắc và loại

| Phương án | Vì sao loại |
|---|---|
| Giữ nguyên ADR 0001, chỉ thêm domain mới | Không giải quyết SPOF — vẫn là yêu cầu gốc của phiên này |
| Di dời cả Ollama lên GCP | Không có GPU trong free-credit; Apple Silicon tại nhà đã đủ nhanh, không đáng đổi chi phí |
| Migrate dữ liệu Postgres/Redis/Kafka từ lab sang GCP | Người quyết định chọn "khởi tạo mới" — dữ liệu lab là dữ liệu thử nghiệm, không cần bảo toàn |
| 3 VM riêng (UI/DB/K3s) | Phức tạp không cần thiết cho tải hiện tại — 1 VM đủ resource (đã đo: 8 vCPU/16GB, dùng ~30% khi full stack chạy) |
| Dùng Istio VirtualService cho canary (thay vì Traefik) | Argo Rollouts' Traefik plugin nhắm API group cũ (`traefik.containo.us`), k3s Traefik v3 chỉ có `traefik.io` — xác nhận lỗi thật qua log, chuyển sang basic canary (ReplicaSet-driven) |
| Cloudflare Access cho Grafana/Prometheus/Vault UI (đồng bộ ADR 0001) | Token Cloudflare có sẵn trong phiên chỉ có quyền DNS, không có quyền Access — dùng Traefik BasicAuth làm giải pháp tạm, có ghi rõ đường nâng cấp |

## Hệ quả

**Tích cực** — SPOF web-plane bị loại; TLS thật đầu-cuối (không còn ngoại lệ HTTP-tới-
origin như ADR 0001 mục "Vì sao HTTP tới origin Traefik"); GitOps đầy đủ (audit trail
qua ArgoCD, secret quản lý qua Vault, canary qua Argo Rollouts); registry riêng
(Harbor) không phụ thuộc Docker Hub rate limit; dashboard phản ánh đúng năng lực thật.

**Tiêu cực** — chi phí GCP không còn là 0đ sau khi hết free-credit 3 tháng (chưa
định lượng chi phí thực tế sau đó). SPOF chuyển sang Tailscale + MacBook cho riêng
năng lực LLM. Thêm bề mặt vận hành đáng kể: Harbor + ArgoCD + Vault + Istio + Argo
Rollouts + Vaultwarden — mỗi thành phần là một điểm cần theo dõi/vá bảo mật. Vault
dùng Shamir 1-of-1 (không phải HA thật) — mất unseal key là mất toàn bộ secret; đã
ghi rõ trong `vault-bootstrap.sh` kèm đường nâng cấp lên GCP KMS auto-unseal.

## Ảnh hưởng bảo mật

- **CRAT, RBAC, kill-switch** (`OMNI_AUTO_EXECUTE_ENABLED`, `OMNI_EXECUTOR_FORCE_NSENTER`)
  — không đổi, kế thừa nguyên trạng từ core, chưa audit lại riêng cho GCP trong ADR này.
- **Istio mTLS**: cài ở chế độ PERMISSIVE mặc định (không ép STRICT) — sidecar có mã
  hoá pod-to-pod nhưng không bắt buộc, giữ tương thích với Prometheus (không có
  sidecar) scrape trực tiếp.
- **NetworkPolicy `omni-gateway-netpol`**: viết trước khi có Istio, ban đầu chặn nhầm
  port merged-metrics `15020` của sidecar (đã fix, xem commit "allow Prometheus
  ingress to Istio merged-metrics port 15020") — bài học: mọi NetworkPolicy siết chặt
  theo port cụ thể cần review lại khi thêm sidecar mesh.
- **BasicAuth thay Cloudflare Access** cho Grafana/Prometheus/Vault UI là suy giảm so
  với mô hình 3 lớp của ADR 0001 (1 cặp user/pass tĩnh so với OTP + edge WAF) — chấp
  nhận tạm thời, có ghi rõ đường nâng cấp trong `monitoring-basicauth-ingress.yaml`.

## Giới hạn đã biết

- Vault Shamir 1-of-1 — không có quorum thật, chỉ phù hợp single-node.
- BasicAuth tĩnh cho Grafana/Prometheus/Vault UI — không có MFA, không có audit log
  truy cập riêng (chỉ có access log Traefik thô).
- ArgoCD Application chỉ theo dõi `omni-fullstack.yaml`/`omni-onboarding.yaml`/
  `omni-worker-configmap.gcp.yaml` — `omni-gateway` (giờ là Rollout) và toàn bộ
  portal/Dex/monitoring **chưa** nằm trong vòng GitOps tự động, vẫn do Jenkins áp
  trực tiếp.
- Argo Rollouts chạy basic canary (replica-driven) do version gap với Traefik v3 —
  không có weighted traffic split thật ở mức L7, chỉ 0%/100% theo pod.
- `api.omnisre.xyz` / `agent.omnisre.xyz` — vẫn chưa public, giữ nguyên quyết định
  từ ADR 0001 (chưa có remote agent nào thật ngoài mạng local cần tới).

## Chưa làm — kế thừa sang task tiếp theo

1. **`app.omnisre.xyz` (Cloudflare Tunnel, ADR 0001) vẫn đang chạy song song** —
   xác nhận trực tiếp: LaunchAgent `com.omnisre.cloudflared` active, `curl` trả
   302 (Access redirect) thật. Đây là ứng viên retire theo đúng lộ trình "Đường ra
   khi rời MacBook" của ADR 0001, nhưng **chưa retire** — cần xác nhận GCP ổn định
   đủ lâu trước khi gỡ (task #8, dọn hạ tầng local).
2. Chưa đưa gateway/portal/Dex/monitoring vào vòng đồng bộ ArgoCD tự động.
3. Chưa nâng Vault lên GCP KMS auto-unseal.
4. Chưa nâng BasicAuth lên Cloudflare Access (thiếu token đủ quyền tại thời điểm
   viết ADR này).

## Tham chiếu

`docs/adr/0001-cloudflare-pages-tunnel-local-core.md` (ADR trước, vẫn đúng lịch sử) ·
`docs/handoffs/GCP_CREDENTIALS_2026-08-04.md` (không commit, chỉ lưu local/Vaultwarden) ·
`Jenkinsfile` · `k8s/gitops/` · `k8s/deployments/*.gcp.yaml`
