# SRE Readiness Audit — Omni (não) + AOIP Agent (tay/chân/mắt)

**Ngày mở:** 2026-08-04 · **Phạm vi:** thuần kỹ thuật vận hành, lab nhưng đo như thật ·
**Trạng thái:** Pha A đang chạy

## Quy tắc bằng chứng (áp dụng cho mọi dòng trong file này)

1. Mỗi PASS/FAIL/PARTIAL phải kèm lệnh đã chạy + output thật. Không có bằng chứng ⇒ `UNKNOWN`.
2. CẤM dùng `CLAUDE.md`, `MEMORY.md`, `docs/`, handoff, ADR làm bằng chứng cuối cùng.
3. Tài liệu mâu thuẫn thực tế ⇒ ghi `DOC DRIFT` + cả hai giá trị.
4. Dữ liệu synthetic phải được gắn nhãn synthetic.
5. Mức tin cậy bắt buộc cho mỗi ô: `đo-thật` / `synthetic` / `chỉ-đọc-code` / `chưa-kiểm-được`.

## Môi trường tại thời điểm audit

| Thành phần | Trạng thái | Bằng chứng |
|---|---|---|
| Omni (GCP) | k3s single-node `omni-k3s-vm` Ready, v1.36.2+k3s1 | `kubectl get nodes` |
| Lab K8s (OrbStack) | **đã xoá hẳn** 2026-08-04 — subvolume btrfs `k8s/default` (6.2 GB) deleted | `btrfs subvolume list` chỉ còn `docker` + 3 subvol máy ảo |
| OrbStack | 2 vCPU / 4096 MiB (hạ từ 8 vCPU / 10240 MiB) | `orb config get cpu`, `memory_mib` |
| 3 VM lab | `cust-app` .237 · `cust-db` .225 · `cust-edge` .87 — running, mỗi VM thấy `cpu=2 mem=3988MiB` | `orbctl list`, `nproc`/`free -m` |
| Agent | `omni-remote-agent.service` active + enabled trên cả 3 | `systemctl is-active/is-enabled` |
| Đích agent | `https://gateway.omnisre.xyz` (GCP), tenant `staging-sim`, per-agent credential | PG `omni_admin.agent_credential` 3 dòng `active` |

## Phát hiện đã ghi nhận trước khi mở Pha A

### F-001 — Agent mất batch evidence ~26 lần/5 phút dù `systemctl` báo xanh

**Mức:** HIGH · **Tin cậy:** đo-thật · **Trục liên quan:** A1 (theo dõi), B1 (SLI mất evidence)

Đo trong 2 phút, ba VM độc lập nhau:

| VM | `200 OK` | `All connection attempts failed` |
|---|---|---|
| cust-edge | 39 | 43 |
| cust-app | 41 | 43 |
| cust-db | 39 | 43 |

Trong 5 phút: mỗi VM **26 lần `dropping batch`**, 11 lần `spooled batch`, `pending=0` ở cuối.

Con số trùng khít trên cả ba VM ⇒ lỗi hệ thống, không phải nhiễu mạng ngẫu nhiên.

**Đã loại trừ bằng bằng chứng:** DNS chỉ trả A record `136.85.2.181` (không có AAAA ⇒ không
phải lỗi IPv6); `curl -4` tuần tự 5/5 = 200; httpx tuần tự 8/8 = 200; httpx song song 8/8 = 200
tới cả GCP lẫn host ngoài.

**Chưa xác định nguyên nhân.** Nghi vấn: connection pool bị đóng phía ingress, request đầu trên
connection cũ chết, retry sau đó thành công — khớp tỉ lệ ~50% và tính đồng nhất, nhưng **chưa có
bằng chứng nên không kết luận**.

**Ý nghĩa với audit:** đây đúng lớp lỗi mà Pha A phải bắt — `systemctl is-active` xanh, evidence
vẫn tới nơi, mà mất ~26 batch/5 phút. Tín hiệu sống ≠ dữ liệu đủ.

**✅ ĐÃ TÌM RA NGUYÊN NHÂN (Pha A, trục A1) — không phải lỗi mạng.** Mỗi VM có **3 process agent**,
chỉ 1 cái đúng:

```
$ orb -m cust-app -u root ps aux | grep -E "remote_agent|omni"
root  263  /opt/omni-remote-agent-replay01/venv/bin/python -m remote_agent.agent
root  279  /opt/omni-remote-agent/venv/bin/python -m aoip.agent.employee
root 1274  /opt/omni-remote-agent/venv/bin/python -m remote_agent.agent

$ đọc /proc/<pid>/environ:
pid  279 (aoip-agent):  GATEWAY_URL=http://gateway.ai-agent.local  ← CHẾT   + AOIP_AGENT_MODE=mutation_enabled
pid  263 (replay01):    GATEWAY_URL=http://gateway.ai-agent.local  ← CHẾT
pid 1274 (đúng):        GATEWAY_URL=https://gateway.omnisre.xyz    ← OK
```

`run.env` mtime `21:47:55` nhưng `aoip-agent` khởi động `21:36:57` ⇒ nó nạp cấu hình **cũ** và không
được restart khi tôi trỏ sang GCP. Toàn bộ dòng `All connection attempts failed` là của 2 process
zombie, không phải của agent đúng. Ba VM ra số trùng khít vì cả ba đều có đúng 2 zombie.

**Mất mát ròng thực tế = 0** — outbox hoạt động đúng thiết kế (đo trên `cust-app` từ 21:50):
`85 POST 200` · `34 failed after retries` · `34 spooled batch` · `35 outbox flushed` ·
`ls /var/lib/aoip/outbox → total 0`.

⚠️ Nhưng phát sinh vấn đề **nặng hơn F-001 ban đầu**: `aoip-agent` (pid 279) chính là **daemon thực
thi lệnh, `AOIP_AGENT_MODE=mutation_enabled`**, và nó đang mù hoàn toàn với não GCP. Xem F-014.

### F-002 — Toàn bộ 13 alert rule SLO/KPI không thể kích hoạt: metric nền có 0 series

**Mức:** CRITICAL · **Tin cậy:** đo-thật · **Trục:** B1

Prometheus GCP đang nạp **41 alert rule**, trong đó nhóm `omni.slo` (5) + `omni.kpi` (4) + 4 recording
rule, tất cả `health=ok`. Nhìn qua thì SLO đầy đủ: `OmniMTTDBreached`, `OmniMTTRBreached`,
`OmniFalsePositiveRateTooHigh`, `OmniAdvisoryAcceptanceLow`…

Nhưng metric nền **không tồn tại một series nào**:

```
omni_kpi_advisory_total            -> số series: 0
omni_kpi_mttd_seconds_bucket       -> số series: 0
omni_kpi_mttr_seconds_bucket       -> số series: 0
omni_kpi_false_positive_rate       -> số series: 0
omni_kpi_advisory_acceptance_rate  -> số series: 0
```

(Không phải lỗi scrape: 25/25 target `up`, và có **116 metric `omni_*` khác** tồn tại bình thường.)

Vì mọi rule đều kèm guard `and omni_kpi_advisory_total > 0`, chúng vĩnh viễn `inactive`. Ngay cả
rule được thiết kế để bắt đúng tình huống này — `OmniKpiNoSamples: omni_kpi_advisory_total == 0` —
cũng **không bao giờ kêu**, vì so sánh trên vector rỗng trả về rỗng chứ không trả về true.

**Nguyên nhân gốc (đã truy tới code):** `src/workers/kpi_metrics.py:216` gọi
`set_kpi_advisory_total(...)` — nhưng lời gọi này nằm **bên trong hàm xử lý một message feedback**.
Comment ngay trên nó viết: *"Mẫu số xuất TRƯỚC và LUÔN xuất — kể cả bằng 0. Đây là thứ duy nhất cho
phía đọc phân biệt '0% chấp nhận' với 'chưa ai phán lần nào'"*. Ý định đúng, nhưng đường đi sai:
không có message thì hàm không chạy, nên chính cái mẫu số dùng để phân biệt "chưa có dữ liệu" lại
chỉ xuất hiện khi đã có dữ liệu.

Kafka GCP xác nhận mẫu số bằng 0 thật:

```
omni-action-feedback         tổng offset = 0     ← chưa từng có message nào
omni-diagnostic-evidence     tổng offset = 21
omni-knowledge-evidence      tổng offset = 102
omni-actions                 tổng offset = 1
omni-audit-chain             tổng offset = 3
```

**Hệ quả:** hệ thống có vẻ được canh bởi 13 SLO alert, thực tế **không alert nào có khả năng kêu**.
Đây là bùa số ở mức nghiêm trọng nhất — không phải thiếu SLO, mà là SLO giả tạo cảm giác đã có.

### F-003 — Không có đường báo động nào tới con người; Alertmanager gửi ngược vào chính Omni

**Mức:** CRITICAL · **Tin cậy:** đo-thật · **Trục:** B2

Alertmanager trên GCP đang chạy bằng ConfigMap **`alertmanager-chaos-config`** (nguồn:
`k8s/chaos-test/alertmanager.yaml` — cấu hình vốn dành cho chaos test đang gánh vai trò alerting
thật). Toàn bộ route:

```yaml
route:
  receiver: omni-webhook          # receiver DUY NHẤT
receivers:
  - name: omni-webhook
    webhook_configs:
      - url: "http://omni-gateway.multi-agent.svc.cluster.local/webhook/prometheus"
```

Comment trong chính file tự thú: *"Telegram is intentionally omitted — secrets unavailable in this
ConfigMap."*

Nghĩa là mọi alert hạ tầng đều chảy **vào chính Omni**. Nếu Omni/gateway chết, alert báo Omni chết
được gửi tới Omni đã chết. Phụ thuộc vòng tròn, không có con người ở cuối đường.

Hiện có **1 alert đang active** (`PodMemoryWorkingSetVsLimitHigh`) đi đúng vào ngõ cụt này.

**Sắc thái công bằng:** Grafana có đường Telegram riêng, và nó **có thật** — deployment mount cả
`grafana-alerting-provisioning` lẫn `grafana-telegram-alerting`; secret có `bot-token` (len=64) và
`chat-id` (len=4). Log cho thấy rule `omni_gateway_down` đã fire lúc 09:08 và
`"Sending alerts to local notifier" count=1`. Nhưng **không có dòng log nào chứng minh Telegram giao
được tin**, và không có lỗi Telegram nào để suy ngược. Vì vậy trạng thái đúng là: đường tới con người
**tồn tại qua Grafana nhưng chưa chứng minh được là thông**; đường Alertmanager thì chắc chắn cụt.

Lỗi duy nhất liên quan alerting trong 6h là Loki ruler trả `502` cho
`/api/prometheus/loki/api/v1/rules` — alert dựa trên log hiện không đánh giá được.

### F-004 — Rò chéo tenant đã XÁC NHẬN SỐNG: key của `staging-sim` đọc được dữ liệu `default`

**Mức:** CRITICAL · **Tin cậy:** đo-thật (đã gửi request thật) · **Trục:** B5

Dùng chính `OMNI_AGENT_API_KEY` của agent `staging-sim_cust-app` (prefix `cbdV…`, đọc từ
`run.env` trên VM) gọi sang tenant `default`:

```
GET /autonomy/tenants/default/agent-credentials  -> http=200  {"agent_credentials":[]}
GET /autonomy/tier?tenant_id=default             -> http=200  {"tier":"shadow","tenant_id":"default"}
```

Credential cấp cho một tenant đọc được cấu hình tenant khác. Không phải suy luận từ code — đây là
response thật từ `https://gateway.omnisre.xyz`.

**Nguyên nhân:** `src/gateway/routes/autonomy.py` thiếu `_require_admin_ctx(request)` ở một số
route trong khi các route hàng xóm cùng resource **có**. Bất đối xứng này chứng minh là bỏ sót,
không phải chủ đích:

| Dòng | Route | Gate admin |
|---|---|---|
| 344 | `GET /tenants` | ✅ có |
| 351 | `POST /tenants` | ✅ có |
| **367** | `POST /tenants/{id}/status` | ❌ **thiếu** |
| **379** | `GET /tenants/{id}/environments` | ❌ **thiếu** |
| 385 | `POST /tenants/{id}/environments` | ✅ có |
| 426 | `POST /tenants/{id}/api-keys` | ✅ có |
| **451** | `DELETE /tenants/{id}/api-keys/{key_id}` | ❌ **thiếu** |
| **510** | `GET /tenants/{id}/agent-credentials` | ❌ **thiếu** |
| 518 | `DELETE /tenants/{id}/agent-credentials/{agent_id}` | ✅ có |

Ngoài ra nhóm GET cấu hình không gate: `/tier` (50), `/readiness` (127), `/risk-class` (153),
`/mutation` (243), `/flags` (291).

**Chỉ mới chứng minh nhánh ĐỌC.** Hai route ghi (`POST .../status` đình chỉ tenant khác,
`DELETE .../api-keys` thu hồi key tenant khác) **cố ý KHÔNG thử** vì audit là read-only — nhưng
chúng nằm cùng danh sách thiếu gate, nên phải coi là khai thác được cho tới khi chứng minh ngược lại.

**Đây là lỗi TÁI PHÁT.** Chính codebase đã tự ghi lại lần vá trước ở
`src/gateway/routes/competency.py:42-45`: *"Đây chính là lỗ hổng vừa vá ở `/autonomy/hitl/*`: SQL
vốn đã lọc theo `tenant_id`, nhưng client tự quyết định giá trị đem đi lọc, nên tenant A đọc được —
và phê duyệt được — dữ liệu của tenant B."* Đã vá `/hitl/*` và `/competency`, bỏ sót 6 route trong
chính `autonomy.py`.

**Không có lưới an toàn tầng dưới:** `ROW LEVEL SECURITY` / `CREATE POLICY` — không có ở bất kỳ đâu
trong repo. Cách ly hoàn toàn phụ thuộc mệnh đề `WHERE` ở tầng ứng dụng, mà tầng đó đã sai 6 lần.

### F-007 — `kubectl rollout undo` KHÔNG khôi phục được code: tiền đề của standing authorization là SAI

**Mức:** CRITICAL · **Tin cậy:** đo-thật · **Trục:** B8

```
$ kubectl get rs -n multi-agent -l app=omni-fullstack
rev 3..13 (11 ReplicaSet)  |  image: multi-agent-system:latest   ← TẤT CẢ giống hệt nhau
```

`Jenkinsfile:18-35` build `docker build -t multi-agent-system:latest` rồi
`k3s ctr images import`. Tag mutable, **không digest, không version**. Mọi revision trỏ cùng một
chuỗi tag ⇒ `rollout undo` tạo lại pod với **đúng image đang lỗi**.

`CLAUDE.md` mục AUTONOMY RULES cho phép tự commit+push với lý do *"lỗi triển khai đã có đường
rollback an toàn"*. **Đường đó không tồn tại.** Cách khôi phục thật duy nhất: revert commit + chạy
lại Jenkins tay (~3,5 phút, trong khi 5/12 build gần đây FAILURE = 42% đỏ).

Liên quan: Harbor chạy 7 pod suốt 7h30m nhưng **không workload nào pull từ đó** — Jenkinsfile không
có `docker push` nào. Không registry ⇒ không digest, không quét CVE, không reproducibility.

### F-008 — Guard fail-closed của executor thực chất FAIL-OPEN (chứng minh bằng chạy thật)

**Mức:** CRITICAL · **Tin cậy:** đo-thật · **Trục:** B8/A3

`src/workers/k8s_tools.py:168-170` nuốt `ApiException` → trả `{}`. Dòng 211 gate toàn bộ khối
revalidation bằng `if isinstance(snap, dict) and snap.get("deployment_generation") is not None:`
⇒ snapshot `{}` thì **bỏ qua sạch kiểm tra staleness**.

```
CASE A — evidence_snapshot = {}:            rollout_restart_ok — mutation executed? True   ← FAIL-OPEN
CASE B — snapshot hợp lệ, re-read fail:     stale_state        — mutation executed? False  ← đúng
```

Bất đối xứng chết người: *đọc lại lỗi* → chặn; *chưa từng đọc được* → cho qua. Vi phạm trực tiếp
invariant `INV_READ_BEFORE_MUTATE`. `evidence_consumer.py:2063-2070` có `try/except → return False`
trông như fail-closed nhưng là **code chết** cho chế độ lỗi phổ biến nhất, vì `ApiException` đã bị
nuốt trước đó. Coverage báo dòng 211-213 "COVERED" — được chạy nhưng **không test nào assert hành vi
đúng**.

Đây đúng lớp bug của commit `bbebf3d` (*"assess_blast_radius bị skip khi reader=None"*) — chưa quét hết.

### F-009 — readinessProbe của gateway đấu nhầm endpoint, vô hiệu hoá lưới an toàn của chính sự cố 2026-08-03

**Mức:** CRITICAL · **Tin cậy:** đo-thật · **Trục:** B8/B2

```
omni-gateway    readinessProbe: /healthz   ← endpoint liveness
omni-fullstack  readinessProbe: /readyz  ✅
omni-onboarding readinessProbe: /readyz  ✅
```

`api.py:540-543`: `/healthz` trả `{"status":"ok"}` **vô điều kiện**, docstring tự ghi *"KHÔNG chứng
minh dependency khả dụng. Xem /readyz"*. `/readyz` (563-576) kiểm Redis + `admin_pool` → 503. Logic
đúng, **K8s không bao giờ gọi tới**.

Kết hợp `api.py:420-421`: retry 5 lần (~25s) thất bại ⇒ `admin_repo = None` **vĩnh viễn**, không có
reconnect runtime. Gateway báo Ready, nhận traffic, mọi request per-agent-credential 401 mãi mãi —
đúng kịch bản `cust-app` đã xảy ra 2026-08-03. Fix retry chỉ **thu hẹp cửa sổ**, không đóng chế độ
lỗi. Coverage dòng 420-421: **MISSING**.

Không phải drift — cả `k8s/gitops/omni-gateway-rollout.yaml:86` lẫn `k8s/deployments/omni-gateway.yaml:63`
đều ghi `/healthz`.

### F-010 — ArgoCD "Synced" chỉ nói về 3/36 manifest

**Mức:** HIGH · **Tin cậy:** đo-thật · **Trục:** B8

```
omni-core   Synced   Healthy   b22b36e00935   ← == git HEAD
spec.source.directory.include = '{omni-fullstack.yaml,omni-onboarding.yaml,omni-worker-configmap.gcp.yaml}'
managed resource count: 3
```

`omni-gateway` đã bị **gỡ khỏi include list** (còn ở history id 0, mất từ id 1). Dex, portals, kafka,
redis, postgres, ingress, netpol, cronjob — **hoàn toàn ngoài GitOps**; drift ở đó không ai phát hiện.

Điểm sáng thật, cần ghi nhận: code trong pod **khớp HEAD byte-for-byte** (474 file), gateway 187/187
khớp, và 5 symbol mới đều xác nhận sống trong pod. Bài học cũ "test pass + push ≠ đã deploy" lần này
**không tái diễn**.

### F-011 — Unit test chạm cluster production và có khả năng MUTATE

**Mức:** HIGH · **Tin cậy:** đo-thật · **Trục:** B8

8 test trong `tests/test_track2a_k8s_sdk.py` gọi API server GCP thật (`src/pkg/k8s_config.py` fallback
`load_kube_config()` → context `temp-k8s`). `TestK8sRolloutRestart::test_explicit_restart_executes`
gọi `tool_k8s_rollout_restart(ns="multi-agent")`; hiện chỉ fail vì Deployment `nginx-test` không tồn
tại trên GCP. **Nếu ai apply `k8s/deployments/nginx-test.yaml`, chạy `pytest` sẽ restart workload
trong cluster thật.** Không marker, không nằm trong `tests/integration/`.

### F-012 — 205 test (3,2%) không có bất kỳ assert nào, gồm cả test gác an toàn

**Mức:** MEDIUM · **Tin cậy:** đo-thật · **Trục:** B8

`test_cov_proactive_react_runner.py:579 test_god_mode_expands_tool_set` khai báo `captured_allowed`
và `original_parse` rồi **không dùng**; stub trả `(None, 0.0, "done")` nên không tool nào chạy.
`:745 test_dev_mode_bypasses_confidence_check` kết thúc bằng comment `# Should complete without
low_confidence block`, không assert — gate confidence hỏng thì test vẫn xanh.

63 file `test_cov_*` chứa **2.125/6.498 test (33%)**, viết để kéo dòng coverage. Vì vậy con số
"80,0%" phải đọc thận trọng.

### F-013 — Bom hẹn giờ: `kubectl apply -f k8s/deployments/` sẽ bật autonomous mutate trên GCP

**Mức:** MEDIUM · **Tin cậy:** đo-thật · **Trục:** B8/A3

`k8s/deployments/` chứa **2 định nghĩa cùng tên** `omni-fullstack`: `omni-fullstack.yaml` và
`omni-fullstack-autoexec-lab.yaml` (file sau đặt `OMNI_AUTO_EXECUTE_ENABLED=true`). Apply cả thư mục
⇒ alphabet cho `-autoexec-lab` thắng ⇒ bật autonomous mutate trên GCP. Jenkins hiện apply từng file
nên chưa dính — an toàn do may, không do thiết kế.

Cùng nhóm: `Jenkinsfile:47` chạy
`kubectl label namespace multi-agent pod-security.kubernetes.io/enforce=privileged --overwrite` mỗi
build, trong khi `k8s/deployments/namespace.yaml:9` ghi `enforce: baseline`. Live = `privileged`.
`omni-fullstack` chạy `hostPID=true, privileged=true, runAsUser=0, SYS_ADMIN+SYS_PTRACE,
seccompProfile=Unconfined` — tương đương root trên node, gắn với đường mutate do LLM lái. Giảm nhẹ
hiện tại: `OMNI_AUTO_EXECUTE_ENABLED=false` trên GCP.

---

## Bảng năng lực (Pha A điền)

| Trục | Nội dung | Điểm | Mức | Tin cậy | Bằng chứng |
|---|---|---|---|---|---|
| A1 | Theo dõi (observe) | — | — | — | — |
| A2 | Giám sát / phát hiện (9 domain) | — | — | — | — |
| A3 | Vận hành (mutate, rollback, HITL) | — | — | — | — |
| A4 | Xử lý sự cố đầu-cuối | — | — | — | — |
| B1 | SLI/SLO/error budget của Omni | 15 | ❌ | đo-thật | 41 rule nạp, 13 rule SLO/KPI, 0 series metric nền → không rule nào kêu được (F-002). Không có khái niệm error budget ở bất kỳ đâu |
| B2 | Tự quan sát + ai được gọi | 35 | ❌ | đo-thật | Stack quan sát đầy đủ và sống (Prometheus/Loki/Mimir/Tempo/Grafana, 25/25 target up, 116 metric omni_*). Nhưng Alertmanager route 100% về chính Omni (F-003); đường Telegram qua Grafana có cấu hình + secret nhưng chưa chứng minh giao được tin |
| B3 | SPOF, backup, test restore, RTO/RPO | 35 | ❌ | **chỉ-đọc-code** | Mọi workload `replicas: 1`; Kafka RF=1 kể cả `omni-audit-chain` (SOX/PCI); có CronJob backup PG + script restore nhưng verify là thủ công, không có cơ chế tự test restore; chỉ backup Postgres — Redis/Kafka/Vault không có. **Runtime chưa kiểm** |
| B4 | Bảo mật kỹ thuật + CRAT + residency | — | — | — | — |
| B5 | Cách ly tenant | 30 | ❌ | **đo-thật** (nhánh đọc) | Rò chéo tenant xác nhận sống bằng request thật (F-004); 6 route thiếu gate admin; không có RLS ở Postgres; `is_admin_ctx(None) → True` fail-open. Điểm sáng: RAG cách ly bằng index scoped theo tenant |
| B6 | Onboarding (số bước, số phút) | 62 | ⚠️ | chỉ-đọc-code | ~5 bước tay + 3 bước tự động; token one-time single-use atomic trong 1 transaction PG (đúng); bundle offline-capable. Nhưng enroll fail phải xin token mới thủ công; `ttl_seconds=None` mặc định = token không hết hạn |
| B7 | Agent trên host mục tiêu | 48 | ⚠️ | **chỉ-đọc-code** | Unit thật `aoip-agent.service` chạy **root, không hardening systemd**, trong khi `omni-agent.service` (User riêng, ProtectSystem, NoNewPrivileges) tồn tại nhưng không dùng. Chống RCE chỉ bằng regex/allowlist trong-tiến-trình — đã từng bị xuyên thủng (PoC RCE root 3/3, awk, 2026-07-31). Uninstall bỏ sót user/log/data + không gỡ được layout fleet thật. **Chưa xác minh trên VM** |
| B8 | Coverage, CI/CD, drift git↔cluster | — | — | — | — |

## Vấn đề (xếp mức)

| ID | Mức | Trục | Mô tả | Hệ quả nếu không sửa |
|---|---|---|---|---|
| F-004 | CRITICAL | B5 | Rò chéo tenant đã xác nhận sống; 6 route thiếu gate admin; không có RLS | Một khách đọc/đình chỉ được dữ liệu khách khác |
| F-005 | CRITICAL | B7 | Agent chạy root không hardening trên host khách; chống RCE đơn lớp bằng regex | Một bypass regex = toàn quyền root trên hạ tầng khách |
| F-006 | CRITICAL | B3 | Kafka RF=1 kể cả `omni-audit-chain`; mọi workload replicas=1; chưa có bằng chứng test restore | Mất VM = mất Omni; hỏng đĩa = mất sổ audit tuân thủ |
| F-002 | CRITICAL | B1 | 13 alert rule SLO/KPI không thể kích hoạt (metric nền 0 series) | Tưởng có SLO canh gác, thực tế không cái nào kêu được — sự cố trôi qua im lặng |
| F-003 | CRITICAL | B2 | Alertmanager gửi 100% alert ngược vào chính Omni; không có người ở cuối đường | Omni chết thì cảnh báo Omni chết cũng chết theo |
| F-001 | HIGH | A1/B1 | Mất ~26 batch evidence/5 phút, nguyên nhân chưa rõ | Omni chẩn đoán trên dữ liệu khuyết mà không biết là khuyết |

## DOC DRIFT

_(Pha A điền)_

## Những gì KHÔNG kiểm được và tại sao

_(Pha A điền — bắt buộc có nội dung, cấm để trống giả tạo)_

## Cần bật/dựng gì mới đo được

_(Pha A điền)_
