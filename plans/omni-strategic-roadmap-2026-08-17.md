# Omni — Kế hoạch chiến lược, tầm nhìn và lộ trình sửa chữa

**Ngày tạo:** 2026-08-17 (ngay sau Đ69/Đ70)
**Trạng thái:** DRAFT — chưa thực thi phase nào, cần user chốt §7 trước khi làm
**Cơ sở:** đọc `CLAUDE.md` + `docs/handoffs/CURRENT_SESSION.md` (Đ62→Đ70) + **kiểm chứng lại
trực tiếp trên cluster GCP** (`kubectl`/`redis-cli`/`psql`, read-only) lúc 2026-08-17 03:20-03:35 UTC
**Phạm vi:** toàn dự án — hạ tầng GCP k3s, pipeline dữ liệu, agent fleet, bảo mật, sản phẩm hoá
**KHÔNG đụng trong plan này:** không sửa code, không deploy, không đổi RBAC. Đây là tài liệu quyết định.

---

## 0. Phát hiện quyết định — đọc trước khi đọc phần còn lại

> **Omni (NÃO) đang chạy đúng code mới nhất trên production, nhưng đã mất kết nối
> với toàn bộ CHÂN/TAY/MẮT. Hệ thống không xử lý một sự cố thật nào trong 4 ngày qua.**

Ba bằng chứng độc lập, đo trực tiếp hôm nay, hội tụ về cùng một kết luận:

| Bằng chứng | Lệnh | Kết quả |
|---|---|---|
| Không có agent nào đăng ký | `redis-cli --scan --pattern 'omni:remote_agent:*'` | **0 key** |
| Gateway không nhận traffic agent | `kubectl logs pod/omni-gateway-6d6db989f6-6hcxq --since=24h \| grep -c 'agent/register\|agent/evidence'` | **0** (chỉ có `/healthz`, `/metrics`) |
| Không có ca sự cố mới | `psql -c 'select max(opened_at) from omni_admin.case_ledger'` | **2026-08-13 04:30:24** — 4 ngày trước |

Hệ quả dây chuyền, tất cả đều đo được:
- `case_ledger` vẫn đúng **305/305 dòng `domain='unknown'`** — bằng đúng số lúc audit Đ62
  (2026-08-13). Fix Đ63 (`get_trace_domain` → `open_case(domain=...)`) **không thể verify được**
  không phải vì fix sai, mà vì **chưa có ca mới nào được mở** kể từ khi fix lên production.
- `corr:*` (SIEM correlation entity) — **0 key** trên GCP. Chuỗi SIEM mà Đ49 từng verify sống
  trên lab OrbStack chưa từng chạy một lần nào trên GCP.
- `omni_admin.playbook` — **0 dòng** (đúng như Đ62 ghi, chưa seed).
- `omni_admin.tenant` chỉ có **2 tenant**: `default`, `loyalty-uat`. `staging-sim` (tenant chủ đạo
  của toàn bộ `docs/product/PRODUCT_PROOF.md`) **không còn tồn tại trong registry** — nhưng vẫn còn
  27.372 key `omni:onboarding:diagram:staging-sim:*` trong Redis (rác của tenant đã chết).

⇒ **Đây không phải "hệ thống production đang chạy". Đây là một bộ não đang chạy trong bể cách ly.**
Mọi ưu tiên kỹ thuật khác (RBAC, RAG, cronjob) đều **thứ yếu** so với việc nối lại đầu vào — vì
không có đầu vào thì không fix nào chứng minh được giá trị, và không audit nào đo được năng lực thật.

---

## 1. Đánh giá hiện trạng — snapshot có bằng chứng (2026-08-17)

### 1.1 Cái gì ĐANG TỐT (đã verify, đừng đụng)

| Hạng mục | Bằng chứng |
|---|---|
| Code trên production = HEAD | tất cả deploy/rollout đều `10.43.239.205/library/*:e1561ae` (`kubectl get deploy -o custom-columns=...image`) — khớp commit `e1561ae`. Gap 13-commit của Đ69 đã đóng thật. |
| CI/CD full loop hoạt động | Jenkins build #70 SUCCESS → Harbor → ArgoCD `omni-core: Synced/Healthy` → Argo Rollouts canary tự động. |
| Public plane có TLS thật + auth | 11 Ingress trên `omnisre.xyz` (argocd/grafana/prometheus/dex/provider/tenant/**gateway**/app/bitwarden), Let's Encrypt qua Traefik. |
| Rate limit gateway | `src/gateway/api.py:356,361,671-677` + `src/gateway/routes/agent_webhook.py:229` — đã có thật (claim "chưa có" trong CLAUDE.md đã sửa ở Đ70). |
| CPU headroom | Đ65 resize 4→8 vCPU, requests 4355m/8000m (54%). Namespace `monitor` full stack Running. |
| Redis leak đã chặn | TTL 30d đã set cho 38.075 key diagram (Đ70 mục 6). Hiện `used_memory 167.32M / maxmemory 2.00G` — an toàn, sẽ tự giảm. |

### 1.2 ⚠️ Phát hiện MỚI, chưa có trong audit hôm nay (Đ69 không bắt được)

#### [NGHIÊM TRỌNG — MỚI] Kafka crash-loop + **không có storage bền vững**

```
kafka-7464867fb6-7cth5   2/2 Running   33 (39s ago)   12d
```
- Container `kafka` **RestartCount = 29**, lần cuối `Exit Code: 137` (OOMKilled) lúc 03:34:12,
  tức **39 giây trước lúc đo**.
- `k8s/kafka/kafka-single.yaml:42-48` — `limits.memory: 1Gi`, `requests.memory: 512Mi`.
- **`kubectl get deploy kafka -o jsonpath='{...volumes}'` trả về RỖNG** — Deployment
  không có volume nào, không PVC, không `emptyDir` mount. `kubectl get pvc -n multi-agent` chỉ có
  3 PVC: `data-omni-postgres-0`, `data-redis-0`, `omni-postgres-backup-data`. **Kafka không có PVC.**

⇒ **Mỗi lần Kafka OOM restart là toàn bộ topic + offset + message bị xoá sạch** (ghi vào
container filesystem ephemeral). Bằng chứng gián tiếp: log `omni-fullstack` cho thấy TẤT CẢ consumer
group vừa `Successfully synced group ... with generation 1` — generation 1 nghĩa là
`__consumer_offsets` vừa mới được tạo lại từ đầu.

Hệ quả trực tiếp lên các invariant đã ghi trong `CLAUDE.md`:
- `auto_offset_reset="earliest"` (INVARIANT) **mất tác dụng bảo vệ** — không có gì để "earliest" về.
- Topic `omni-audit-chain` — **CẬP NHẬT 2026-08-17 ~03:50 UTC, sau khi verify lại lần 2**: claim ban
  đầu của bản nháp plan này ("topic không còn tồn tại") **SAI**, đã tự sửa ngay khi phát hiện mâu
  thuẫn (không được để claim sai đứng làm phát hiện đầu bài). Sự thật đo trực tiếp:
  `kafka-topics.sh --list` → topic **CÓ tồn tại**, `--describe --topic omni-audit-chain` →
  `PartitionCount:1 ReplicationFactor:1 Configs:` **RỖNG** (không có `cleanup.policy=compact`), và
  `kafka-get-offsets.sh` → **6 message**. Nghĩa là: topic không biến mất, nhưng **đã bị Kafka tự
  tạo lại với config mặc định** (không compact, không retention vô hạn) tại một thời điểm nào đó
  giữa các lần crash-loop — mất đúng cấu hình mà `scripts/kafka_ensure_omni_topics.sh:108-128` từng
  thiết lập có chủ đích. Root cause suy luận hợp lý nhất (chưa verify 100%): Kafka có
  `auto.create.topics.enable` mặc định bật, nên khi 1 producer ghi vào topic trước khi
  `kafka_ensure_omni_topics.sh` kịp chạy lại sau một lần restart, Kafka tự tạo topic với config
  mặc định thay vì compact — và không có cơ chế nào tự sửa lại config sau đó.
  ⇒ Nhánh CRAT ghi Kafka (`src/workers/advisory_ack.py:266`,
  `src/workers/advisory_analyst_handler.py:486`, `src/execution/playbook_engine.py:69`) đang ghi
  thật (6 message không phải 0), nhưng **không còn được compact/giữ vô hạn** — nghĩa là retention
  mặc định của Kafka (thường theo thời gian, không phải theo key) có thể âm thầm xoá bằng chứng cũ
  hơn ngưỡng đó. Rủi ro thấp hơn "mất trắng" nhưng vẫn là vi phạm invariant `CLAUDE.md`
  *"`omni-audit-chain` topic cần message key (compact policy)"*.
- `INV_CRAT_FAIL_CLOSED` (`write_audit_block()` phải thành công trước khi emit) — cần kiểm lại xem
  fail-closed có thực sự trip khi topic biến mất, hay đang im lặng đi qua.

Đây là **rủi ro nghiêm trọng nhất của toàn hệ thống hiện nay**, cao hơn cả RBAC: nó vừa là nguồn
mất dữ liệu, vừa là nguồn phá hỏng bằng chứng tuân thủ, vừa là nguồn nhiễu log (7.883 dòng ERROR
trong 30 phút quan sát).

#### [CAO — MỚI] Vùng phủ GitOps rất hẹp — phần lớn hạ tầng vẫn "apply tay"

`k8s/gitops/argocd-application.yaml` — Application `omni-core` chỉ include **6 file + 1 Rollout**:
```
k8s/deployments: {omni-fullstack.yaml, omni-onboarding.yaml, omni-worker-configmap.gcp.yaml,
                  aoip-portals.gcp.yaml, aoip-portals-web.yaml, omni-fullstack-rbac.yaml}
k8s/gitops:      {omni-gateway-rollout.yaml}
```
**KHÔNG nằm trong GitOps:** `k8s/kafka/kafka-single.yaml`, Redis StatefulSet, Postgres StatefulSet,
toàn bộ `k8s/ingress/*`, `aoip-dex.gcp.yaml`, `k8s/jobs/crat-integrity-check-cronjob.gcp.yaml`,
`k8s/deployments/omni-postgres-backup-cronjob.yaml`, `k8s/gitops/vault-auto-unseal-cronjob.yaml`.

⇒ `selfHeal: true` + `prune: true` chỉ bảo vệ 7 resource. Mọi thứ còn lại (bao gồm chính Kafka đang
crash-loop) drift tự do, và ADR 0002 mục "Chưa làm" #2 vẫn còn nguyên hiệu lực.
Đây cũng là lý do gốc của bug Đ70 mục 4 (RBAC fix Đ68 không lên cluster vì file không nằm trong
`directory.include`) — bug đó đã vá cho 1 file, nhưng **lớp bug thì chưa vá**.

#### [THÔNG TIN — MỚI, TÍCH CỰC] LLM **đã rời MacBook**, `CLAUDE.md` và ADR 0002 đều lỗi thời

`kubectl get cm omni-worker-config -n multi-agent -o jsonpath='{.data}`:
```
OMNI_LLM_PROVIDER          = nim
OMNI_OLLAMA_BASE_URL       = https://integrate.api.nvidia.com/v1
OMNI_VLLM_BASE_URL         = https://integrate.api.nvidia.com/v1
OMNI_VLLM_EMBED_URL        = https://integrate.api.nvidia.com/v1
VLLM_MODEL                 = meta/llama-3.1-8b-instruct
OMNI_EMBED_MODEL           = nvidia/nv-embedqa-e5-v5
OMNI_EMBED_DIM             = 1024
OMNI_NIM_RATE_LIMIT_RPM    = 40
```
Deployment `omni-fullstack` có `OMNI_NIM_API_KEY` ← Secret `omni-nim-secret`.

Đối chiếu tài liệu — **cả hai đều SAI so với thực tế:**
- `CLAUDE.md:8-9` — *"Chỉ **Ollama/LLM** còn cố ý ở lại MacBook, nối qua Tailscale"*
- `CLAUDE.md:226-228` (mục INFRASTRUCTURE) — *"Ollama `qwen3:8b` ... Host: `host.orb.internal:11434`"*
- `docs/adr/0002-gcp-k3s-full-migration.md:38-40` — *"LLM (Ollama, `qwen3:8b`) cố ý ở lại MacBook ...
  `OMNI_VLLM_BASE_URL=http://100.93.3.96:11434/v1`"*
- `docs/adr/0002:60-64` — *"quyết định con #2: LLM ở lại MacBook — SPOF chuyển vị trí, không biến mất"*

⇒ **Quyết định kiến trúc #2 của ADR 0002 đã bị đảo ngược trên thực tế** (ở Đ59/Đ60, "rollback +
chuyển NIM"), nhưng ADR chưa được superseded và `CLAUDE.md` chưa cập nhật. Đây là tin tốt về chiến
lược: **SPOF "năng lực suy luận phụ thuộc MacBook" đã biến mất**, GCP core tự chủ hoàn toàn về LLM.
Nó thay đổi hoàn toàn phép tính chi phí/lợi ích của quyết định "retire OrbStack lab" (§7.2).

Ràng buộc mới đi kèm: `OMNI_NIM_RATE_LIMIT_RPM=40` — trần cứng 40 request/phút cho **toàn bộ**
diagnosis + embed. Đây là ngữ cảnh trực tiếp cho "3 hướng giảm tải LLM còn lại của Đ51" (§3.2).

#### [TRUNG BÌNH] `crat-integrity-check` vẫn fail — cùng lớp bug Istio đã sửa cho Postgres backup

`kubectl get jobs -n multi-agent`:
```
crat-integrity-check-29770560   Failed     0/1   8d
crat-integrity-check-29776440   Failed     0/1   4d1h
crat-integrity-check-29776620   Failed     0/1   3d22h
crat-integrity-check-29782200   Failed     0/1   78m     ← sau cả reboot gần nhất
crat-integrity-check-29782260   Complete   1/1   31m
```
`k8s/jobs/crat-integrity-check-cronjob.gcp.yaml:20` — `backoffLimit: 1` (chưa sửa, đúng như Đ70 ghi).
Cùng root cause với `omni-postgres-backup` (đã sửa 2→6 tại
`k8s/deployments/omni-postgres-backup-cronjob.yaml:45`).
**Cùng lớp bug còn ở 3 file nữa chưa ai đụng:**
- `k8s/gitops/vault-auto-unseal-cronjob.yaml:33` — `backoffLimit: 1` (đây là job **auto-unseal
  Vault**, chạy mỗi 2 phút; fail sau reboot = Vault kẹt sealed = ExternalSecret ngừng sync)
- `k8s/deployments/knowledge-ingest-cronjob.yaml:15` — `backoffLimit: 1`
- `k8s/deployments/sop-ingest-job.yaml:9` — `backoffLimit: 1`

#### [TRUNG BÌNH] RAG SOP rỗng — và job re-ingest hiện có **không dùng được trên GCP**

Xác nhận lại hôm nay: `redis-cli hlen omni:rag:sop` = **0**.

Nhưng vấn đề sâu hơn "chưa chạy job": `k8s/deployments/sop-ingest-job.yaml` là **manifest lab thuần**,
không có bản `.gcp.yaml` song song:
- `image: multi-agent-system:latest` + `imagePullPolicy: IfNotPresent` → trên GCP phải là
  `10.43.239.205/library/multi-agent-system:<sha>` (Harbor), `:latest` không còn tồn tại ở đâu
  (xem ghi chú "no more :latest, anywhere" trong `argocd-application.yaml`).
- `OMNI_VLLM_BASE_URL: http://host.orb.internal:11434/v1` → **OrbStack**, không tồn tại trên GCP VM.
- Không set `OMNI_EMBED_DIM` → code mặc định 768 (`src/rag/redis_vector_store.py:47`
  `EMBED_DIM = int(os.environ.get("OMNI_EMBED_DIM", "768"))`), trong khi index GCP là **1024**
  (`nvidia/nv-embedqa-e5-v5`). **Chạy nguyên job này trên GCP sẽ tạo vector sai chiều** — chính là
  lớp bug đã trả giá ở Đ59/Đ60.
- Corpus nguồn có sẵn: `data/sop/sop_templates.yaml` (8.271 bytes) — không mất, chỉ chưa nạp.

⇒ Đây **không phải** việc "chạy 1 lệnh"; cần một manifest `.gcp.yaml` mới. Ước lượng effort phải
tính theo đó.

### 1.3 Nợ kỹ thuật đã biết, xác nhận vẫn còn nguyên

| Mục | Xác nhận hôm nay | Nguồn |
|---|---|---|
| RBAC `secrets` toàn cluster | `k8s/deployments/omni-fullstack-rbac.yaml:314-316` — `resources: ["secrets"]`, `verbs: ["get","list","watch","patch","update"]`, trong **ClusterRole** (không namespace-scoped) | Đ69 #3, Đ70 "chưa làm" |
| `ui/` source tree cũ | còn trong repo, 0 deploy route | Đ62 #5 |
| `omni_admin.playbook` = 0 dòng | `select count(*) from omni_admin.playbook` → 0 | Đ62 #5 |
| `omni-siem-chains` chưa từng hình thành trên GCP | `corr:*` = 0 key | CLAUDE.md domain `security` |
| domain `hardware` không có collector | giới hạn kiến trúc, **không phải nợ** — đừng đưa vào roadmap | `CLAUDE.md:126` |
| Gate 0 agent hardening cutover | chặn kỹ thuật, cần MacBook/Tailscale | Đ68 |
| Vault auto-unseal (GCP KMS) | ADR 0002 "Chưa làm" #3 | ADR 0002:176 |

---

## 2. Tầm nhìn và mục tiêu chiến lược

### 2.1 Omni đang ở giai đoạn nào — trả lời thẳng

Đối chiếu `docs/product/PRODUCT_CONTRACT.md` §6 (thang tier: Observe → Shadow → Advisory →
HITL Execute → Scoped Autonomy) với thực tế đo được:

| Chiều | Tài liệu tự nhận | Thực tế đo 2026-08-17 |
|---|---|---|
| Hạ tầng | Production trên GCP, domain thật, CI/CD đầy đủ | ✅ **ĐÚNG** — đây là phần trưởng thành nhất |
| Dữ liệu vào | 3 agent lab + 2 tenant | ❌ **0 agent sống**, 0 evidence 24h |
| Xử lý sự cố | Advisory/HITL-Execute, allowlist 3 agent | ⚠️ code sống, nhưng **0 ca mới 4 ngày** |
| Multi-tenant | 2 tenant trong registry | ⚠️ 1 tenant thật (`loyalty-uat`) + 1 `default`; tenant cũ `staging-sim` chết nhưng để lại 27k key rác |
| Bằng chứng tuân thủ | CRAT hash-chain SOX/PCI | ❌ topic `omni-audit-chain` **không tồn tại**; `crat-integrity-check` fail 4/7 lần gần nhất |

**Kết luận trung thực: Omni là một `production-grade platform` đang chạy một `lab-grade workload`.**
Vỏ hạ tầng đã vượt xa ruột vận hành. Đây chính xác là hình dạng rủi ro nguy hiểm nhất — vì mọi
dashboard đều xanh (`Synced/Healthy`, pod `Running`, `/healthz` 200) trong khi hệ thống **không làm
việc gì cả**.

Không phải "lab → production single-tenant". Đúng hơn: **"production infrastructure, pre-production
operation"** — và bước kế tiếp không phải mở rộng, mà là **đóng vòng lặp đầu-cuối một lần cho một
tenant thật**.

### 2.2 Tầm nhìn 3 tầng

```
TẦNG 1 — Sự thật (Truth)          "Cái Omni thấy là cái đang xảy ra"
   ├── Agent fleet sống, có heartbeat đo được
   ├── Event backbone không mất dữ liệu (Kafka bền vững)
   └── Audit chain liên tục, kiểm được (CRAT integrity xanh 7/7)

TẦNG 2 — Phán đoán (Judgment)     "Cái Omni nói là đúng và có bằng chứng"
   ├── RAG SOP có nội dung (triage không tra về rỗng)
   ├── case_ledger.domain đúng (đo được trên ca MỚI)
   └── domain coverage: 8/9 domain có chứng cứ ca thật trên GCP (hardware = out of scope)

TẦNG 3 — Hành động (Action)       "Cái Omni làm là an toàn và đảo ngược được"
   ├── RBAC least-privilege thật (không secrets cluster-wide)
   ├── Blast radius có gate + HITL verify được
   └── Rollback đường nào cũng chứng minh được
```

**Nguyên tắc thứ tự bất di bất dịch: KHÔNG đầu tư tầng 2/3 khi tầng 1 chưa đứng.**
Re-ingest RAG (tầng 2) khi không có evidence vào (tầng 1) là tối ưu hoá một nhánh code không ai gọi.
Thu hẹp RBAC (tầng 3) khi không có mutation nào chạy là thay đổi rủi ro cao mà không đo được lợi ích.

### 2.3 Bốn trục ưu tiên chiến lược (theo thứ tự)

1. **Độ tin cậy dữ liệu & bằng chứng** — Kafka bền vững, audit chain sống, backup/integrity xanh.
   *Đây là trục số 1 vì mọi claim còn lại của dự án đều dựa lên nó.*
2. **Nối lại vòng lặp thật (agent → não → hành động)** — có agent sống trên GCP, có ca sự cố mới,
   verify được các fix Đ63/Đ66/Đ67 bằng dữ liệu chứ không bằng test.
3. **Giảm rủi ro bảo mật có kiểm soát** — RBAC least-privilege, Vault auto-unseal, GitOps phủ hết.
4. **Giảm phụ thuộc MacBook/lab** — **đã đi được 70% mà chưa ai ghi nhận** (LLM đã sang NIM).
   Phần còn lại chỉ là 3 VM khách hàng lab và bản thân OrbStack.

---

## 3. Phân loại việc tồn đọng theo trục

Ma trận rủi ro × effort. **Rủi ro** = hậu quả nếu không làm. **Effort** = giờ người ước lượng.
**Chặn** = phụ thuộc gì.

### 3.1 Nhóm A — Kỹ thuật thuần, an toàn làm ngay (không cần user chốt kiến trúc)

| # | Việc | Rủi ro | Effort | Chặn | Ghi chú |
|---|---|---|---|---|---|
| A1 | **Kafka: thêm PVC + nâng memory limit** (`k8s/kafka/kafka-single.yaml`) | 🔴 Rất cao | M (2-4h) | không | Deployment→StatefulSet hoặc Deployment+PVC; `limits.memory` 1Gi→2Gi. **Có downtime ngắn, mất dữ liệu topic hiện tại — nhưng dữ liệu đó vốn đã mất 29 lần rồi.** |
| A2 | **Tái tạo topic `omni-audit-chain`** — chạy `scripts/kafka_ensure_omni_topics.sh` sau A1 | 🔴 Rất cao | S (<1h) | A1 | Không có ý nghĩa nếu làm trước A1 (restart kế tiếp lại xoá). |
| A3 | **`backoffLimit` cho 4 CronJob còn lại** — `crat-integrity-check-cronjob.gcp.yaml:20`, `vault-auto-unseal-cronjob.yaml:33`, `knowledge-ingest-cronjob.yaml:15`, `sop-ingest-job.yaml:9` | 🟠 Cao | S (<1h) | không | Cùng bug đã sửa+verify cho Postgres backup. `vault-auto-unseal` là mục nguy hiểm nhất trong 4 (Vault sealed = ExternalSecret chết). |
| A4 | **Cập nhật `CLAUDE.md` + supersede ADR 0002 quyết định #2** (LLM đã rời MacBook sang NIM) | 🟠 Cao | S (<1h) | không | Doc-only. Tài liệu sai về LLM đang khiến mọi phân tích "SPOF MacBook" bị lệch. |
| A5 | **Dọn 27.372 key `omni:onboarding:diagram:staging-sim:*`** của tenant đã xoá | 🟡 Thấp | S | không | TTL 30d đã set (Đ70) → tự rụng. Chỉ làm nếu muốn giải phóng RAM sớm. |
| A6 | **Bản `.gcp.yaml` cho `sop-ingest-job`** (Harbor image + NIM URL + `OMNI_EMBED_DIM=1024`) rồi re-ingest RAG SOP | 🟠 Cao | M (2-3h) | A4 (biết đúng config) | ⚠️ **Không chạy manifest lab hiện có trên GCP** — sẽ tạo vector 768-dim vào index 1024-dim. |
| A7 | **Audit hạng mục 5** (Telegram `unified_incident_card` / portal / advisory schema) | 🟡 Trung | M | (2) nên làm sau khi có ca mới | Audit UI khi không có ca nào để render là audit rỗng. |

### 3.2 Nhóm B — Quyết định kiến trúc, cần user chốt

| # | Quyết định | Rủi ro nếu để treo | Effort sau khi chốt | Ghi chú quyết định |
|---|---|---|---|---|
| B1 | **Thu hẹp RBAC `omni-executor-mutate-lab`** | 🔴 Rất cao | M-L | Xem §7.1 — đã có đường đi an toàn cụ thể, không phải "cần rà thêm" chung chung. |
| B2 | **Retire OrbStack lab hay không** | 🟠 Cao | L | Phép tính đã đổi hẳn: LLM không còn ở MacBook (§1.2). Xem §7.2. |
| B3 | **Mở rộng GitOps coverage** (đưa Kafka/Redis/Postgres/Ingress/CronJob vào ArgoCD) | 🟠 Cao | L | Đây là fix lớp bug của Đ70 mục 4. `prune: true` đang bật ⇒ **phải cẩn thận**, thêm sai file có thể bị prune xoá resource sống. |
| B4 | **Vault GCP KMS auto-unseal** (ADR 0002 "Chưa làm" #3) | 🟠 Cao | L | Hiện đang dựa vào CronJob auto-unseal mỗi 2 phút với `backoffLimit: 1` — vá tạm mong manh. |
| B5 | **Xoá source tree `ui/`** | 🟢 Thấp | S | Dead code, 0 route deploy. Rủi ro thật = mất tham chiếu feature khi port. Đề xuất: **hoãn**, chi phí giữ ≈ 0. |
| B6 | **3 hướng giảm tải LLM còn lại của Đ51** | 🟡 Trung | M | Ngữ cảnh MỚI: trần cứng `OMNI_NIM_RATE_LIMIT_RPM=40`. Nên chốt lại sau khi có lưu lượng thật (2). |
| B7 | **Seed `omni_admin.playbook` + domain `security`** | 🟡 Trung | M | Phụ thuộc (2) — không seed playbook cho một pipeline không có input. |

### 3.3 Nhóm C — Bị chặn hạ tầng, cần môi trường khác

| # | Việc | Chặn | Đường gỡ |
|---|---|---|---|
| C1 | **Nối 3 agent VM lab về GCP gateway** | Cần MacBook/OrbStack/Tailscale — VM GCP không có route tới VM lab | Ingress `gateway.omnisre.xyz` **đã sống public** (`kubectl get ingress`) ⇒ agent chỉ cần đổi `OMNI_AGENT_GATEWAY_URL` + API key. **Không cần đường ngược từ GCP vào lab.** Thao tác chạy từ MacBook. |
| C2 | **Gate 0 agent hardening cutover** | Như trên | Gộp chung một lượt với C1 — cùng 3 VM, cùng cần MacBook. |
| C3 | Xác minh `case_ledger.domain` fix Đ63 | Cần ca sự cố mới | Tự động thoả sau C1 |

**Nhận xét quan trọng:** C1 bị coi là "bị chặn" nhưng thực ra **không bị chặn về mặt mạng** — hướng
kết nối là agent → gateway public, không phải GCP → lab. Cái bị chặn chỉ là **việc gõ lệnh phải từ
MacBook**. Đây là việc user làm được trong <1 giờ và nó **mở khoá toàn bộ trục 2**.

### 3.4 Không phải nợ — đừng đưa vào roadmap

- Domain `hardware` (`CLAUDE.md:126`) — giới hạn kiến trúc (cần chạy trực tiếp trên host, không
  container). Chỉ vào roadmap nếu có quyết định riêng về agent bare-metal.
- Kafka 1 partition / RF=1 — throughput headroom, không phải data-loss (**lưu ý: điều này chỉ đúng
  SAU KHI A1 xong**; hiện tại data-loss là thật và nguyên nhân là thiếu PVC, không phải RF).

---

## 4. Lộ trình theo giai đoạn

### 4.1 Dependency graph

```
GIAI ĐOẠN 0 — Cầm máu (48h)
   A1 Kafka PVC + memory ──▶ A2 tái tạo omni-audit-chain ──▶ verify CRAT ghi được
   A3 backoffLimit ×4 (song song, độc lập)
   A4 sửa CLAUDE.md + ADR 0002 (song song, độc lập)
        │
        ▼
GIAI ĐOẠN 1 — Nối lại vòng lặp (2 tuần)     ★ mở khoá mọi thứ phía sau
   C1 trỏ 3 agent lab → gateway.omnisre.xyz  (chạy TỪ MACBOOK)
        ├──▶ C2 Gate 0 hardening cutover (cùng lượt, cùng 3 VM)
        ├──▶ C3 verify case_ledger.domain trên ca MỚI (fix Đ63)
        ├──▶ verify fix Đ66/Đ67 (tool_output_status) bằng dữ liệu thật
        └──▶ A6 re-ingest RAG SOP (lúc này mới đo được tác động lên triage)
                 │
                 ▼
GIAI ĐOẠN 2 — Củng cố (1 tháng)
   B1 thu hẹp RBAC     (cần §7.1 chốt; cần Giai đoạn 1 để đo được có gãy mutation không)
   B3 mở rộng GitOps   (cần §7.3 chốt; làm SAU A1 để Kafka manifest mới vào git đúng dạng)
   B4 Vault KMS unseal (cần §7.4 chốt)
   A7 audit hạng mục 5 (cần có ca thật để render)
        │
        ▼
GIAI ĐOẠN 3 — Mở rộng năng lực (dài hạn, quý)
   B7 seed playbook + đóng domain `security` (omni-siem-chains hình thành lần đầu trên GCP)
   B6 chốt lại chính sách giảm tải LLM dưới trần NIM 40 RPM
   B2 quyết định retire OrbStack (chỉ khi Giai đoạn 1 chứng minh GCP đủ sống)
   Multi-tenant thật: tenant thứ 2 có agent thật, chứng minh cách ly (PRODUCT_PROOF iteration 9 style)
```

### 4.2 Giai đoạn 0 — Cầm máu (48 giờ tới)

**Mục tiêu:** hệ thống ngừng mất dữ liệu và ngừng mất bằng chứng tuân thủ.

| Bước | Việc | Exit criteria (đo được, không suy đoán) |
|---|---|---|
| 0.1 | A1 — Kafka PVC + `limits.memory` 1Gi→2Gi | `kubectl get pvc` có PVC của Kafka; `RestartCount` giữ nguyên 0 sau 24h quan sát |
| 0.2 | A2 — chạy `scripts/kafka_ensure_omni_topics.sh` | `kafka-topics.sh --list \| grep omni-audit-chain` trả về kết quả; `--describe --topic omni-audit-chain` cho `cleanup.policy=compact` |
| 0.3 | Kiểm chứng CRAT fail-closed | ghi thử 1 audit block, `kafka-console-consumer` đọc lại được; nếu `write_audit_block()` **không** trip fail-closed lúc topic vắng mặt ⇒ đó là bug INVARIANT, mở việc riêng |
| 0.4 | A3 — `backoffLimit` ×4 | `crat-integrity-check` xanh 5/5 lần liên tiếp (5 giờ); `vault-auto-unseal` không fail sau reboot thử |
| 0.5 | A4 — doc | `CLAUDE.md:8-9,226-228` + ADR 0002 §quyết-định-#2 ghi đúng NIM; ADR 0002 đánh dấu superseded phần LLM |

**Rủi ro của chính giai đoạn 0:** thao tác Kafka gây downtime pipeline. **Chấp nhận được** — pipeline
hiện không xử lý gì (0 agent, 0 evidence). **Đây là cửa sổ rẻ nhất có thể có để làm việc này.**
Làm sau Giai đoạn 1 sẽ đắt gấp nhiều lần.

### 4.3 Giai đoạn 1 — Nối lại vòng lặp (2 tuần)

**Mục tiêu:** có ít nhất 1 ca sự cố thật đi hết vòng agent → evidence → advisory → CRAT → Telegram,
**trên GCP**, và các fix đã làm 2 tuần qua được chứng minh bằng dữ liệu.

| Bước | Việc | Exit criteria |
|---|---|---|
| 1.1 | C1 — từ MacBook: đổi `OMNI_AGENT_GATEWAY_URL` → `https://gateway.omnisre.xyz` trên 3 VM, cấp/xác nhận credential (`omni_admin.agent_credential` đã có 3 dòng) | `redis-cli --scan --pattern 'omni:remote_agent:registry:*'` ≥ 3 key; gateway log có `agent/register 200` |
| 1.2 | C2 — Gate 0 hardening cutover cùng lượt | theo exit criteria của plan Gate 0 (commit `243a139`) |
| 1.3 | Drill sự cố thật (kiểu Đ49 S0-S3) trên `cust-edge` | `case_ledger` có dòng mới, `opened_at` > 2026-08-17 |
| 1.4 | C3 — verify fix Đ63 | ca mới có `domain` ≠ `unknown` (305 dòng cũ giữ nguyên có chủ đích, đúng quyết định Đ63) |
| 1.5 | A6 — re-ingest RAG SOP qua `sop-ingest-job.gcp.yaml` mới | `redis-cli hlen omni:rag:sop` > 0; `FT.INFO` cho thấy vector 1024-dim; 1 truy vấn triage trả về SOP thật |
| 1.6 | Verify Đ66/Đ67 bằng dữ liệu | có ít nhất 1 message `[AUTO-FIX-LEARNING]` phân loại đúng fail/ok qua `classify_tool_output()` |

**Đây là giai đoạn quan trọng nhất của toàn bộ roadmap.** Nếu chỉ làm được 1 giai đoạn, làm giai đoạn này.

### 4.4 Giai đoạn 2 — Củng cố (1 tháng)

| Bước | Việc | Điều kiện tiên quyết |
|---|---|---|
| 2.1 | B1 — thu hẹp RBAC theo phương án §7.1 | Giai đoạn 1 xong (để có mutation thật mà đo hồi quy) |
| 2.2 | B3 — mở rộng GitOps từng file một, mỗi lần 1 file, verify `Synced` trước khi thêm tiếp | A1 xong (Kafka manifest ở dạng cuối) |
| 2.3 | B4 — Vault GCP KMS auto-unseal, gỡ CronJob vá tạm | A3 xong |
| 2.4 | A7 — audit hạng mục 5 | 1.3 xong (có ca để render) |
| 2.5 | Chốt `INV_KAFKA_DURABLE` thành invariant mới trong `CLAUDE.md` | A1 xong |

### 4.5 Giai đoạn 3 — Mở rộng (dài hạn, theo quý)

- **B7 — đóng domain `security` trên GCP**: seed `omni_admin.playbook`, chạy drill sudo-failure như
  Đ49, mục tiêu `omni-siem-chains` hình thành lần đầu (cần ≥2 nguồn entity liên quan).
- **B6 — chính sách LLM dưới trần 40 RPM**: đo lưu lượng thật ở Giai đoạn 1 trước, rồi mới chọn
  trong 3 hướng của Đ51. Có thể trần NIM đã tự giải quyết vấn đề bằng cách khác.
- **B2 — retire OrbStack**: chỉ mở khi Giai đoạn 1 chứng minh 3 agent chạy ổn định trỏ GCP ≥ 2 tuần.
- **Multi-tenant thật**: tenant thứ 2 có agent thật + chứng minh cách ly cross-tenant. Đây mới là
  ngưỡng "multi-tenant" theo `PRODUCT_CONTRACT.md`, không phải "có 2 dòng trong bảng `tenant`".
- **Cập nhật `PRODUCT_PROOF.md`**: toàn bộ tài liệu đang dựa trên tenant `staging-sim` **đã không còn
  tồn tại** và môi trường OrbStack. Cần một iteration mới dựa trên GCP + `loyalty-uat`, nếu không
  nó sẽ trở thành nguồn drift tài liệu lớn nhất còn lại của dự án.

---

## 5. Rủi ro lớn nhất nếu không hành động

Xếp theo mức độ, tất cả đều dựa trên bằng chứng đo được ở §1.

### R1 — 🔴 Mất bằng chứng tuân thủ, âm thầm và không thể phục hồi
Kafka không có PVC + OOM crash-loop liên tục (RestartCount=33 lúc verify lần 2, tăng từ 29 chỉ
trong ~40 phút — crash-loop vẫn đang diễn ra ngay lúc viết plan này). Topic `omni-audit-chain`
**vẫn tồn tại và có dữ liệu** (6 message, đã verify trực tiếp — claim "đã biến mất" ở bản nháp đầu
của phần §1.2 là SAI, đã tự sửa ngay khi phát hiện mâu thuẫn khi kiểm chứng lại), nhưng **mất cấu
hình `cleanup.policy=compact`** (Configs rỗng = default), nghĩa là retention không còn theo key mà
theo thời gian mặc định — bằng chứng cũ có thể bị xoá âm thầm mà không ai biết ngưỡng chính xác.
`CLAUDE.md` mục CRAT tuyên bố SOX §404 / PCI-DSS v4.0 với hash-chain SHA-256 + Ed25519 —
**chuỗi đó có đứt đoạn hay không, hiện không ai biết**, và `crat-integrity-check` (công cụ duy nhất
trả lời câu hỏi đó) đã fail 4/7 lần gần nhất. Không hành động ⇒ mỗi lần Kafka OOM lại có nguy cơ
tạo thêm một khoảng trống audit hoặc làm mất compact policy lần nữa (topic tồn tại không đồng nghĩa
config đúng), và không có cách nào tái tạo lại bằng chứng đã mất về sau. **Đây là rủi ro khó đảo
ngược nhất trong danh sách — khác mọi rủi ro còn lại — nhưng mức độ đã xảy ra thấp hơn bản nháp đầu
mô tả; cần A1+A2 xác minh lại `Configs` sau khi sửa, không chỉ xác nhận topic "tồn tại".**

### R2 — 🔴 RBAC: một prompt injection = đọc toàn bộ secret của cluster
`omni-fullstack` (SA bị LLM điều khiển, `OMNI_AUTO_EXECUTE_ENABLED=true`) có `secrets:
get/list/watch/patch/update` **mọi namespace** (`omni-fullstack-rbac.yaml:314-316`). Trong cluster đó
có: `vault`, `argocd`, `cicd` (Jenkins + Gitea), Harbor, `omni-pg-secret`, `omni-nim-secret`,
`telegram-bot`. Các gate hiện có (`required_evidence`, `MUTATE_TOOL_ALLOWLIST`,
`OMNI_EXECUTOR_FORCE_NSENTER`, `OMNI_AUTONOMOUS_ALLOWED_NAMESPACES=multi-agent`) đều là gate **tầng
ứng dụng** — chúng bảo vệ đường `k8s_patch_secret`, nhưng **không ngăn được `get`/`list`**, và một
lỗi logic hay một prompt injection thành công sẽ bỏ qua toàn bộ. Lớp phòng thủ cuối cùng (RBAC) hiện
không có. Việc này đã bị hoãn 3 phiên liên tiếp (Đ62 → Đ64 → Đ68 → Đ70), mỗi lần đều với lý do hợp
lý — nhưng **rủi ro không tự giảm theo thời gian hoãn**.

### R3 — 🟠 Hệ thống "xanh giả" — mọi chỉ báo tốt trong khi không làm gì
ArgoCD `Synced/Healthy`, pod `Running`, `/healthz` 200, Grafana sống — nhưng 0 agent, 0 evidence,
0 ca mới trong 4 ngày. Không có alert nào bắn. Không hành động ⇒ hệ thống có thể chết im lặng hàng
tuần/tháng mà không ai biết, và mọi kết luận rút ra từ nó (bao gồm cả các audit như Đ69) đều là kết
luận về một hệ thống rỗng. **Cần một SLI/alert kiểu "evidence freshness" — chưa có.**

### R4 — 🟠 RAG rỗng làm triage tệ đi mà không báo lỗi
`omni:rag:sop` HLEN=0. Nhánh triage tra SOP trả rỗng — LLM phải suy luận thuần, mất toàn bộ tri thức
vận hành đã tích luỹ. Không có exception, không có log lỗi ⇒ **degrade âm thầm**. Đây cùng lớp với
bug `assess_domain_severity` lệch bí danh field (Đ49 B5/B6) mà dự án đã trả giá 2 lần.

### R5 — 🟠 Vault sealed → toàn bộ chuỗi secret đứt
`vault-auto-unseal-cronjob.yaml:33` `backoffLimit: 1` — cùng bug Istio webhook race đã được chứng
minh gây fail thật cho Postgres backup 3 lần. Vault sealed ⇒ ExternalSecret ngừng sync ⇒
`aoip-dex-secret` (và mọi secret sync từ Vault) hết hạn/không cập nhật được ⇒ Dex OIDC chết ⇒
portal không đăng nhập được. Chuỗi này chưa xảy ra nhưng mọi mắt xích đều đã được chứng minh mong manh.

### R6 — 🟡 GitOps chỉ phủ 7 resource
Kafka (đang crash), Redis, Postgres, mọi Ingress, mọi CronJob — nằm ngoài `selfHeal`. Sửa tay không
vào git ⇒ VM tái tạo là mất. Đây chính xác là lớp bug đã cắn ở Đ70 mục 4 (RBAC fix không lên cluster
suốt nhiều ngày mà ArgoCD vẫn báo `Synced`) — đã vá 1 file, chưa vá lớp.

### R7 — 🟡 `PRODUCT_PROOF.md` mô tả một thế giới không còn tồn tại
Toàn bộ tài liệu bằng chứng sản phẩm (91KB, 33 iteration) dựa trên tenant `staging-sim` **không còn
trong registry**, VM OrbStack, unit `omni-remote-agent.service` **đã gỡ khỏi cả 3 VM** (Đ_2026-08-11).
Đây là tài liệu sẽ được đưa cho khách hàng/nhà đầu tư. Không hành động ⇒ rủi ro uy tín, không phải
rủi ro kỹ thuật.

---

## 6. Bảng ưu tiên tổng hợp (rủi ro × effort)

```
                        EFFORT THẤP              EFFORT CAO
                 ┌─────────────────────┬──────────────────────────┐
   RỦI RO   CAO  │  A3 backoffLimit×4  │  A1 Kafka PVC ★★★        │
                 │  A2 audit-chain     │  B1 thu hẹp RBAC ★★      │
                 │  A4 sửa doc LLM     │  C1 nối agent → GCP ★★★  │
                 │                     │  B4 Vault KMS            │
                 ├─────────────────────┼──────────────────────────┤
   RỦI RO  THẤP  │  A5 dọn key rác     │  B3 mở rộng GitOps       │
                 │  B5 xoá ui/ (hoãn)  │  A6 RAG re-ingest (.gcp) │
                 │                     │  B7 seed playbook        │
                 └─────────────────────┴──────────────────────────┘
   ★ = đòn bẩy cao nhất (mở khoá nhiều việc khác)
```

**Làm 4 việc này trước, theo đúng thứ tự, là đủ để đảo ngược tình thế:**
`A1 → A2 → A3 → C1`.

---

## 7. Quyết định cần user chốt (không tự làm)

### 7.1 [ƯU TIÊN CAO NHẤT] Thu hẹp RBAC — đã có đường đi an toàn, không còn là câu hỏi mở

Vấn đề đã bị hoãn 3 lần vì "cần rà kỹ". Sau khi rà, có một phương án rủi ro thấp cụ thể:

**Phương án đề xuất — tách `secrets` khỏi ClusterRole, chuyển thành Role namespace-scoped:**
- Gỡ rule `resources: ["secrets"]` khỏi ClusterRole `omni-executor-mutate-lab`
  (`omni-fullstack-rbac.yaml:314-316`).
- Thêm một `Role` (namespaced) + `RoleBinding` trong **chỉ** namespace `multi-agent` với đúng
  verbs đó, bind cùng SA `omni-fullstack`.
- **Không đổi tên ClusterRole** (giữ quyết định Đ68 — đổi tên là breaking change).
- Cơ sở kỹ thuật: `OMNI_AUTONOMOUS_ALLOWED_NAMESPACES=multi-agent` đã giới hạn executor ở đúng
  namespace này ở tầng ứng dụng ⇒ về lý thuyết **không mất năng lực nào**. Tool `k8s_patch_secret`
  (`src/workers/k8s_cluster_tools.py`, `src/pkg/reasoning/deterministic_mutate_from_evidence.py:237`)
  vẫn hoạt động nguyên vẹn trong `multi-agent`.
- Rủi ro thật cần chấp nhận: nếu có đường code nào đọc secret ngoài `multi-agent` mà chưa ai biết,
  nó sẽ gãy. Giảm thiểu: làm ở Giai đoạn 2 (sau khi Giai đoạn 1 có mutation thật để quan sát), và
  bật audit log trên `Forbidden` trong 48h trước khi coi là xong.

**Câu hỏi cho user:** đồng ý phương án tách Role namespace-scoped này, hay muốn giữ nguyên
cluster-wide và ghi lại chính thức đó là chủ đích (chấp nhận R2)?

### 7.2 Retire OrbStack lab — phép tính đã đổi
Lý do lớn nhất để giữ OrbStack ("LLM Ollama ở MacBook") **đã không còn đúng** — production dùng
NVIDIA NIM cloud (§1.2). Lý do còn lại: 3 VM khách hàng lab (`cust-edge/app/db`) chạy trên OrbStack.
**Câu hỏi:** giữ OrbStack **chỉ** để nuôi 3 VM lab (và trỏ agent của chúng lên GCP — phương án C1),
hay dựng 3 VM lab trên GCP luôn để retire MacBook hoàn toàn?
*Khuyến nghị: phương án C1 trước (rẻ, 1 giờ), quyết định retire sau khi có 2 tuần dữ liệu ổn định.*

### 7.3 Mở rộng GitOps — mức độ tới đâu
`prune: true` đang bật. Thêm file vào `directory.include` sai cách có thể khiến ArgoCD **xoá**
resource sống. **Câu hỏi:** làm từng file một có verify (an toàn, chậm) hay đổi sang chiến lược
`exclude` (nhanh, rủi ro cao hơn)? *Khuyến nghị: từng file một, bắt đầu từ `k8s/kafka/kafka-single.yaml`
ngay sau A1.*

**Cập nhật 2026-08-17 (Đ72, cùng phiên)**: đã thêm `kafka-single.yaml`, `redis-standalone.yaml`,
`omni-postgres.yaml` — cả 3 đều `kubectl diff`=rỗng trước khi thêm, verify sau khi ArgoCD sync:
pod không restart, PVC/dữ liệu nguyên vẹn (case_ledger vẫn 305 dòng). **Ingress ĐÃ ĐÁNH GIÁ,
QUYẾT ĐỊNH HOÃN**: 11 Ingress sống trên cluster rải rác qua ≥4 file khác nhau
(`k8s/gitops/monitoring-basicauth-ingress.yaml`, `argocd-ingress.yaml`, `vaultwarden.yaml`,
`k8s/ingress/omnisre-gcp.yaml`), trải trên 4 namespace (`argocd`/`monitor`/`vaultwarden`/
`multi-agent`) — khác hẳn Kafka/Redis/Postgres (mỗi cái 1 file tự chứa, 1:1 rõ ràng). Đây là
routing công khai của toàn bộ platform (`*.omnisre.xyz`) — rủi ro nếu sai cao hơn nhiều so với
lợi ích, và `kubectl diff` xác nhận **hiện KHÔNG có drift nào** ở cả 4 file (rủi ro của việc
"không làm" hiện tại = thấp). Hoãn có chủ đích, không phải bỏ sót — cần dành riêng 1 phiên để map
chính xác file→namespace→Ingress trước khi thêm.

### 7.4 Vault auto-unseal
Giữ CronJob vá tạm (đã fix `backoffLimit` ở A3) hay đầu tư GCP KMS auto-unseal (ADR 0002 #3)?

### 7.5 `PRODUCT_PROOF.md`
Viết iteration mới trên nền GCP + `loyalty-uat`, hay đánh dấu toàn bộ tài liệu là "lịch sử OrbStack"
và bắt đầu tài liệu bằng chứng mới? *Khuyến nghị: cách 2 — tài liệu 91KB không sửa đè được.*

---

## 8. Exit criteria toàn bộ roadmap

Roadmap này coi là hoàn thành khi **tất cả** các điều sau đo được cùng lúc:

1. `kubectl get pod -n multi-agent kafka-*` — `RestartCount` không tăng trong 7 ngày liên tiếp.
2. `kafka-topics.sh --describe --topic omni-audit-chain` — tồn tại, `cleanup.policy=compact`.
3. `crat-integrity-check` — xanh 7/7 lần liên tiếp.
4. `redis-cli --scan --pattern 'omni:remote_agent:registry:*'` — ≥3 key, liên tục 7 ngày.
5. `select count(*) from omni_admin.case_ledger where opened_at > '<ngày bắt đầu Giai đoạn 1>'` — ≥1,
   và **100% các dòng đó có `domain` ≠ `'unknown'`**.
6. `redis-cli hlen omni:rag:sop` — > 0, vector 1024-dim.
7. `kubectl auth can-i list secrets --as=system:serviceaccount:multi-agent:omni-fullstack -n vault`
   → **no** (nếu chốt phương án §7.1).
8. `CLAUDE.md` + ADR 0002 không còn mô tả LLM ở MacBook.

---

## 9. Tham chiếu

- `CLAUDE.md` — mục NÃO vs THÂN, INVARIANTS (RBAC dòng ~196-211), CRAT, DEPLOYMENT STATE
- `docs/handoffs/CURRENT_SESSION.md` — Đ62 (audit), Đ63-Đ68, Đ69 (audit 19 phát hiện), Đ70 (8/8 xử lý)
- `docs/audit/omni_audit_2026-08-17.xlsx` — 19 phát hiện có bằng chứng (nền của plan này)
- `docs/adr/0002-gcp-k3s-full-migration.md` — mục "Chưa làm" #2/#3/#4; **quyết định con #2 cần supersede**
- `docs/product/PRODUCT_CONTRACT.md` §6 (thang tier) · `docs/product/PRODUCT_PROOF.md` (cần iteration mới)
- `plans/consolidate-vm-agent-remote-to-aoip-employee-2026-08-11.md` — nền cho C1/C2
- `plans/finguard-to-smart-siem-merge-2026-08-04.md` — nền cho B7
- `k8s/kafka/kafka-single.yaml:29,42-48` · `k8s/gitops/argocd-application.yaml:41-48`
- `k8s/deployments/omni-fullstack-rbac.yaml:280-322` · `k8s/deployments/sop-ingest-job.yaml`
- `src/rag/redis_vector_store.py:44-47` · `scripts/kafka_ensure_omni_topics.sh:108-128`
