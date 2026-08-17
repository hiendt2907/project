# CLAUDE.md

> **TRƯỚC MỌI TASK: đọc `MEMORY.md` + `docs/CODEBASE.md`.** Bản đồ nhanh ở memory `project_architecture_map`; chi tiết file-level ở `docs/CODEBASE.md`.

> ⚠️ **HẠ TẦNG ĐÃ DI DỜI SANG GCP (2026-08-04) — đọc [`docs/adr/0002-gcp-k3s-full-migration.md`](docs/adr/0002-gcp-k3s-full-migration.md) TRƯỚC khi tin bất kỳ mô tả "OrbStack/MacBook" nào bên dưới.**
> Core (gateway/fullstack/onboarding/portals/Dex/monitoring/GitOps đầy đủ:
> Harbor/ArgoCD/Vault/Istio/Argo Rollouts/Vaultwarden) giờ chạy trên GCP VM
> `omni-k3s-vm` (k3s single-node), domain thật `omnisre.xyz`.
> ⚠️ **LLM ĐÃ RỜI MACBOOK — SỬA 2026-08-17, xác nhận trực tiếp qua `kubectl get cm
> omni-worker-config`**: dòng cũ ngay dưới đây (*"Chỉ Ollama/LLM còn cố ý ở lại MacBook, nối qua
> Tailscale"*) đã LỖI THỜI — không rõ đổi chính xác lúc nào (không có entry handoff ghi lại), phát
> hiện khi audit roadmap Đ71. Production GCP hiện dùng **NVIDIA NIM cloud**:
> `OMNI_LLM_PROVIDER=nim`, `OMNI_OLLAMA_BASE_URL=OMNI_VLLM_BASE_URL=https://integrate.api.nvidia.com/v1`,
> `VLLM_MODEL=meta/llama-3.1-8b-instruct`, `OMNI_EMBED_MODEL=nvidia/nv-embedqa-e5-v5`,
> `OMNI_EMBED_DIM=1024`, trần `OMNI_NIM_RATE_LIMIT_RPM=40`. Deployment `omni-fullstack` đọc
> `OMNI_NIM_API_KEY` từ Secret `omni-nim-secret`. **SPOF "LLM phụ thuộc MacBook/Tailscale" mà ADR
> 0002 từng chấp nhận đã không còn** — GCP core giờ tự chủ hoàn toàn về năng lực suy luận. Điều
> này đổi hẳn phép tính chi phí/lợi ích của "retire OrbStack lab" (chỉ còn 3 VM khách hàng lab là
> lý do giữ OrbStack, không còn LLM). Xem `plans/omni-strategic-roadmap-2026-08-17.md` §1.2, §7.2.
> Câu dưới đây (lịch sử, giữ nguyên để biết ADR 0002 từng quyết định gì — **đã lỗi thời**): OrbStack
> lab **vẫn đang chạy song
> song**, chưa retire — xem "Chưa làm" trong ADR 0002. Mọi mô tả domain
> `ai-agent.local`, Cloudflare Tunnel `app.omnisre.xyz`, hay "core trên
> MacBook" bên dưới đây là **lịch sử của lab/ADR 0001**, không phải trạng thái
> GCP hiện tại. File manifest GCP nằm ở `k8s/gitops/` + các file `*.gcp.yaml`
> song song với file lab cùng tên — không bao giờ sửa file lab để thay đổi GCP.
> Credentials: `docs/handoffs/GCP_CREDENTIALS_2026-08-04.md` (không commit) +
> Vaultwarden tự host tại `https://bitwarden.omnisre.xyz`. Pipeline reproducible
> qua `Jenkinsfile` (Gitea nội bộ `http://100.67.117.19:30300`), nhưng job
> `omni-gcp-deploy` **KHÔNG có trigger tự động** (`<triggers/>` rỗng trong
> `config.xml`, xác nhận trực tiếp trên VM 2026-08-04) — push git KHÔNG tự
> deploy, phải bấm "Build Now" hoặc gọi Jenkins API tay.
> ⚠️ **Jenkins đã migrate vào pod trong chính k3s cluster (2026-08-10, Đ47)** —
> KHÔNG còn là systemd service trên VM nữa (`jenkins.service` đã `stop` +
> `disable` hẳn, cutover xác nhận sống). Manifest: `k8s/gitops/jenkins-incluster.yaml`
> (namespace `cicd`, Deployment 2 container: `jenkins` + sidecar DinD
> `docker:29-dind` cho `docker build`/`push`), image controller riêng
> `docker/jenkins-controller/Dockerfile` (kubectl/helm/istioctl/docker CLI +
> python3/PyYAML, build+push qua Harbor bằng chính DinD sidecar, không cần VM
> nữa). Truy cập: `http://100.67.117.19:30080` (NodePort, **không phải** `:8080`
> nữa). Image build/deploy giờ qua Harbor thật (ClusterIP `10.43.239.205`, k3s
> `registries.yaml` đã có mirror insecure cho IP đó) thay vì
> `sudo k3s ctr images import` cũ — `imagePullPolicy: Always` trên
> `omni-fullstack`/`omni-onboarding`/`omni-gateway-rollout`/portal deployments
> vì lý do đó (`:latest` giờ sống trên registry thật, không còn bị ghi đè cục bộ
> nữa nên `IfNotPresent` sẽ không bao giờ pull lại). Chi tiết đầy đủ + các gotcha
> (MTU overlay lồng nhau, bind-mount DinD, UID/GID lệch khi migrate data,
> `docker login` luôn thử HTTPS bất kể `insecure-registries`): xem mục Đ47 trong
> `docs/handoffs/CURRENT_SESSION.md`.
> `kubectl` local nối cluster qua Tailscale IP `100.67.117.19:6443` (không phải
> IP nội bộ VPC `10.x` — không route được từ máy ngoài); k3s server có thêm cờ
> `--tls-san 100.67.117.19` (chỉ sống trên VM, KHÔNG có trong git/manifest —
> nếu VM bị tái tạo phải thêm lại tay).
> ⚠️ **Security sweep 2026-08-04 — ĐÃ XỬ LÝ** (không còn nợ kỹ thuật ở các mục
> dưới, chỉ ghi lại để biết kiến trúc hiện tại): Postgres `omni_admin`
> (`omni-pg-secret`) + Dex OIDC client secret (`provider-portal-secret`/
> `tenant-portal-secret`, cũ) từng bị commit plaintext vào git — đã rotate thật
> (ALTER USER trong Postgres, secret mới trong Vault), giá trị cũ vô hiệu. Dex
> GCP giờ đọc config qua Secret `aoip-dex-secret` sync từ Vault
> (`secret/aoip-dex-secret`) bằng ExternalSecret
> (`k8s/gitops/aoip-dex-external-secret.yaml`) — KHÔNG còn ConfigMap plaintext.
> Dex GCP cũng đã **gỡ hẳn `staticPasswords`** (5 tài khoản test dùng chung
> `Password123!`, gồm 1 tài khoản "Provider owner", từng sống công khai trên
> `dex.omnisre.xyz`) — chỉ còn OIDC client-credential flow. Lab OrbStack giữ
> staticPasswords (chấp nhận được, không expose Internet) nhưng client secret
> cũng đã rotate, chuyển ConfigMap→Secret (không qua Vault, lab không có Vault).
> `k8s/deployments/omni-postgres.yaml`/`aoip-dex.yaml`/`aoip-dex.gcp.yaml` không
> còn Secret/ConfigMap plaintext trong git nữa — bootstrap qua Jenkinsfile
> (Postgres/`openssl rand`) hoặc Vault (Dex GCP). Giá trị mới: xem
> `docs/handoffs/GCP_CREDENTIALS_2026-08-04.md` (không commit).
> ⚠️ **Cloudflare Tunnel (ADR 0001) đã tắt hẳn 2026-08-04** — `app.omnisre.xyz`
> chuyển DNS sang A→GCP IP trực tiếp (giống các subdomain khác), có ingress
> `omnisre-landing-gcp` (`k8s/ingress/omnisre-gcp.yaml`) trỏ cùng backend với
> `provider.omnisre.xyz`. `com.omnisre.cloudflared` +
> `com.omni.cloudflare-tunnel` (launchd MacBook) đã unload, plist chuyển vào
> `~/Library/LaunchAgents/disabled/`. `omnisre.xyz`/`www.omnisre.xyz` là site
> tĩnh trên **Cloudflare Pages** (`cloudflare/pages/`, deploy qua
> `make deploy-landing`) — độc lập hoàn toàn với MacBook/GCP, không đổi.

**Omni** — async-first multi-agent SRE automation. **Omni là NÃO** (trung tâm điều hành duy nhất), **Remote Agent là CHÂN/TAY/MẮT** trên hạ tầng khách hàng. Omni phán bất thường bằng baseline nó tự học, phân loại theo **9 domain**, rồi tự điều tra nhiều lượt. Split Kafka pipeline thực thi khắc phục. Ranh giới sở hữu/quyết định/dữ liệu giữa hai bên: xem mục **NÃO vs THÂN** ngay dưới đây — đọc trước khi sửa bất cứ gì chạm cả hai phía, nhầm lẫn ở đây từng gây sai lệch tài liệu thật (audit 2026-08-03).

## NÃO vs THÂN — Omni (nội bộ) vs Remote Agent (khách hàng)

Xác nhận qua audit code + cluster K8s thật + VM thật + log thật (2026-08-03). Ba trục, không được gộp:

1. **Sở hữu hạ tầng** — Omni = chạy trong K8s namespace `multi-agent` (`omni-fullstack`, `omni-gateway`, `omni-onboarding` + Postgres/Redis/Kafka nội bộ) = hệ thống CỦA OMNI. Remote Agent = process `python -m remote_agent.agent` (code ở `src/remote_agent/`), systemd unit `omni-remote-agent.service`, chạy TRÊN server/VM khách hàng (lab: cust-edge/cust-app/cust-db qua `orb -m <machine>`) — KHÔNG phải một phần của cluster Omni dù code sống chung 1 repo.

2. **Quyền quyết định — "agent đề xuất, Omni quyết"**, KHÔNG phải "agent chỉ thu số" cho mọi domain:
   - Chỉ domain `os_host` (`src/remote_agent/collectors/system.py`) luôn gửi `result="OBSERVED"` thuần số, không tự phán.
   - 5 domain còn lại — `database/storage/service/network/application` — agent TỰ TÍNH verdict FAILED/PASSED bằng ngưỡng tĩnh hardcode ngay trên host khách trước khi gửi (vd `collectors/database.py`: threads>500/slow>100/repl_lag>300; `collectors/storage.py`: disk >95% critical/>90% warn; `collectors/network.py`: cổng đóng→FAILED; `collectors/services.py`: unit failed/stopped→FAILED). Đây là đề xuất thô, KHÔNG phải phán quyết cuối.
   - Omni luôn là nơi quyết định CUỐI CÙNG: `knowledge_pipeline.py` ghi đè cứng `result="FAILED"` khi nâng cấp thành `ANOMALY`; `assess_domain_severity` phán mức nghiêm trọng; `remote_host_baseline.py` tự học baseline + z-score cho `os_host`. Remote Agent không bao giờ tự dispatch mutate hay tự chốt mức khẩn cấp cuối cùng.

3. **Dữ liệu ở lại đâu (`INV_DATA_RESIDENCY`)** — nội dung tài liệu/log khách hàng chỉ lên Omni dưới dạng hash + metadata (`pkg/onboarding/discovery_doc.py` hash-on-arrival nếu agent gửi raw; `services/knowledge/document_store.py::ingest_customer_knowledge()` metadata-only ≤2000 chars). Số đo vận hành thuần (cpu/mem/disk/latency) KHÔNG thuộc phạm vi residency — đó là số đo, không phải dữ liệu khách.

## Context Hygiene

- Mỗi session chỉ có một deliverable chính.
- Repository là source of truth; conversation history không phải source of truth.
- Sau mỗi checkpoint quan trọng, cập nhật `docs/handoffs/CURRENT_SESSION.md`.
- Báo cáo checkpoint tối đa 20 dòng: result, changed files, verification, blocker, next step.
- Không lặp lại toàn bộ lịch sử dự án.
- Khảo sát rộng phải dùng subagent và chỉ trả kết luận ngắn.
- Trước khi chuyển milestone hoặc `/clear`: cập nhật handoff và engineering artifacts bị ảnh hưởng (dùng `/prepare-clear`).
- Session mới phải kiểm tra Git state và handoff trước khi tiếp tục. Session hooks tự nạp ngữ cảnh; chi tiết ở `docs/engineering/claude-session-automation.md`.

## DIAGNOSTIC FLOWS — 9 DOMAIN (4 lane đã bị bỏ, 2026-07-30)

Trục phân loại là **9 domain canonical** (`src/pkg/domain/taxonomy.py`), không còn 4 lane.
Lý do: lane là thuộc tính của một ALERT, và 4 lane không diễn đạt được
network/storage/database/service/hardware. Kế hoạch + bằng chứng:
`plans/lane-to-domain-and-omni-decides-2026-07-30.md`.

⚠️ **"lane" là BA trục khác nhau cùng tên. Chỉ trục A bị bỏ.** Đọc khối cảnh báo trong
`pkg/domain/taxonomy.py` trước khi chạm bất cứ gì có chữ `lane`:
- **A** `envelope.lane` (`SYS_RESOURCE|SYS_HARD_FAIL|APP_HTTP|SIEM_SECURITY`) → **ĐÃ GỠ khỏi
  tầng trace 2026-08-09** (Đ39): `mark_stage()`/trace meta/`omni:trace:events`/`/trace/*`/portal
  không còn trường `lane`. Thay bằng HAI trục tách bạch: `domain` (9 domain canonical) và
  `signal_kind` (`diagnostic|learning`). Lý do gỡ: một trường `lane` duy nhất đang gánh BỐN nghĩa
  (lane trục A, `proof_lane` trục B, loại tín hiệu `ONBOARDING_DISCOVERY`, chuỗi rỗng) và portal
  render thẳng nó ở cột "Lĩnh vực" nên hiện sai nhãn. `envelope.lane` **vẫn còn trên dây** để đọc
  payload từ agent bản cũ đã cài trên VM khách — `lane_to_domain()` giữ nguyên, chỉ còn là hàng
  chót của cascade `detect_domain()`. `SYS_HARD_FAIL` → `unknown` CỐ Ý (nó gánh 4 domain).
- **B** `proof_lane` (`resource|state|app_log`, `VALID_PROOF_LANES`) = *cần bằng chứng vật
  lý loại nào để mở cổng*. Lái `ERR_REA_NO_PHYSICAL_PROOF` + `LANE_BADGE` Telegram.
  **KHÔNG gộp vào domain.**
- **C** `proactive|reactive` (`llm_semaphore`) = pool đồng thời LLM. Không liên quan.

| Domain | Ai phát hiện | Trạng thái đã kiểm bằng lỗi thật (2026-07-30) |
|---|---|---|
| `os_host` | `remote_host_baseline.py` (3σ, Omni phán) · `three_sigma.py` in-cluster | ✅ `decided_by=omni_baseline` z=3.739 → critical → 8 lượt ReAct |
| `database` | `collectors/database.py` (dò cổng/health) | ✅ critical → diagnosis loop |
| `service` | `collectors/services.py` | ✅ unit `failed` **và** unit vừa chuyển active→inactive (dừng sạch) |
| `kubernetes` | `os_state_validator.py`, probe K8s | ✅ |
| `storage` | `collectors/storage.py` | ✅ ĐÃ VÁ 2026-08-10 (Đ49 B6) — dòng cũ ghi nhầm producer/field: `disk_percent` thực ra đến từ `collectors/system.py` (baseline os_host 3-sigma), KHÔNG phải `storage.py`. Producer thật `storage.py` (probe `disk_usage`) phát `disk_critical_count`/`disk_warn_count`, không khớp field `assess_domain_severity` từng đọc → cùng lớp bug lệch bí danh với `application` (B5). Đã thêm nhánh đọc đúng field. Ngưỡng VM-side (agent tự tính, STATIC_GUARD): 95% critical / 90% warn (94% ra `INCONCLUSIVE` là ĐÚNG thiết kế). |
| `application` | `collectors/logs.py`, `log_surge_probe.py` | ✅ ĐÃ VÁ 2026-08-10 (Đ49 B5) — root cause: `assess_domain_severity` đọc `error_rate`/`latency_p99_ms` nhưng producer thật phát `failed_file_count`/`files_scanned` (lệch bí danh, cùng lớp bug đã trả giá ở domain OS `cpu_pct`/`cpu_percent`), khiến `domain_severity` luôn `"none"` và urgency kẹt ở `medium` qua nhánh `failed_ratio` dự phòng. Thêm nhánh đọc đúng field thật vào `_check_numeric_thresholds`, nay lên đúng `critical`/`high` theo tỉ lệ file lỗi. |
| `network` | `collectors/network.py` (MỚI) | ✅ cổng lắng nghe vừa đóng → `NetworkListenerLost`; verified `tcp/80` trên VM |
| `security` | `siem_reasoning.py` + `collectors/security.py` (Remote Agent) + Smart SIEM nội bộ (FinGuard ngoài đã retired — `plans/finguard-to-smart-siem-merge-2026-08-04.md`) | ⏳ ĐANG SỐNG, có bằng chứng thật 2026-08-10 (Đ49 S0-S3, verify bằng drill sudo-failure thật trên `cust-edge`): evidence → domain detect đúng → diagnostic pipeline → fan-out `omni-siem-raw` → `decode_kafka_message` → `correlator.process()` ghi entity Redis thật (`corr:ent:staging-sim:user:siemdrilltest`) → CRAT `ADVISORY_DECISION` — TẤT CẢ xác nhận sống, không suy đoán. Chưa đạt ✅ đầy đủ: chưa thấy `omni-siem-chains` hình thành (cần ≥2 nguồn entity liên quan, đúng thiết kế, chưa test), `case_ledger` chưa mở ca cho domain này, `omni_admin.playbook` 0 dòng (seed để lại việc sau). Chi tiết đầy đủ + 3 gotcha thật tìm được qua drill (parser sudo sai định dạng, Kafka double-envelope sai, code không hot-reload): `docs/audit/invariant_audit_2026-08.md` mục S3. |
| `hardware` | — | ❌ giới hạn kiến trúc, không phải gap môi trường — xác nhận 2026-08-10: KHÔNG có collector nào cho domain này (0 file trong `src/remote_agent/collectors/`), và cả agent lẫn Omni đều chạy containerized (K8s pod) nên không có đường truy cập `/sys/class/hwmon`/cảm biến vật lý dù chạy trên OrbStack hay GCP VM. Chuyển hạ tầng sang GCP KHÔNG giải quyết được domain này — cần chạy trực tiếp trên host (systemd, không container) mới có, đó là quyết định kiến trúc riêng, không phải nợ kỹ thuật cần "làm nốt". |

**Ai phán "bất thường": OMNI, không phải agent.** Agent gửi `METRIC_SAMPLE`
(`result="OBSERVED"`), Omni dựng baseline và quyết định trong
`knowledge_pipeline._handle_metric_sample` theo thang `ConfidenceLevel`
(STATIC_GUARD → ngưỡng tĩnh tại Omni · ASSISTED/AUTONOMOUS → z-score). Nâng cấp thành
`ANOMALY` **phải** dùng `result="FAILED"` — `assess_domain_severity` so đúng chuỗi đó để
lên critical, thiếu là chết lặng ở Stage 4. Nguồn phán ghi ở `omni_decision.decided_by`.

**Advisory schema** (`src/pkg/reasoning/analyst_advisory_schema.py`): WHAT/WHO/WHY/HOW-TO + ForecastTimeline (5 horizons). L1→L4: os_baremetal → network → kubernetes → prometheus.
**Telegram**: `unified_incident_card.py` — nhãn VI (Sự cố/Workload/Kiểm chứng/Khắc phục/Dự báo/🧾 Audit). WHAT/WHO/WHY/HOW-TO = marker máy, KHÔNG đổi (parse-coupled).

## KNOWLEDGE PIPELINE (2026-06-27, commit c4635ab)

`INV_KNOWLEDGE_NOT_ALERT`: non-ANOMALY signals KHÔNG vào `omni-diagnostic-evidence`. Routing ở gateway (`agent_webhook.py`), không phải worker.
- `signal_type` trong `build_envelope()` (default=ANOMALY): ANOMALY → `omni-diagnostic-evidence`; METRIC_SAMPLE/LOG_SAMPLE/DISCOVERY → `omni-knowledge-evidence`
- ANOMALY (RemoteAgent) vẫn qua RAG+LLM đầy đủ: `remote_agent_pipeline.py` Stage2-6 (cluster→triage RAG→research LLM→learn→notify); chỉ healthy/PASSED rẽ Redis side-channel (`omni:remote_agent:baseline_ok:` TTL 600s), bỏ qua pipeline. "No RAG/LLM" chỉ đúng cho `knowledge_pipeline.py` dispatcher (non-ANOMALY); rolling log LPUSH+LTRIM 500/24h; change detection diff → Telegram approve/reject
- `src/anomaly/remote_host_baseline.py`: `ConfidenceLevel` (STATIC_GUARD 0-24 / LEARNING 25-49 / ASSISTED 50-74 / AUTONOMOUS 75-100); `add_confidence(delta)`, `decay_confidence(-5/day)`; key `omni:3sigma:confidence:{tenant}:{host}` TTL=30d
- `src/remote_agent/discovery.py`: `save/load_discovery_snapshot()`, `diff_discovery()` (SERVICE_ADDED/REMOVED, PORT_OPENED/CLOSED); agent re-discovery mỗi 1h
- `src/services/knowledge/document_store.py`: `ingest_customer_knowledge()` — metadata only (INV_DATA_RESIDENCY); +20 confidence per doc
- Kafka topic: `omni-knowledge-evidence` (partitions=3, retention=7d); env `OMNI_KAFKA_TOPIC_KNOWLEDGE_EVIDENCE`

## PIPELINE

Remote agents (non-ANOMALY) → `omni-knowledge-evidence` → knowledge_pipeline (no RAG/LLM)
Alert sources → `omni-diagnostic-evidence` → analyst (RAG → LLM → AnalystAdvisory → CRAT [FAIL-CLOSED] → SUGGEST/EXECUTE/HITL)
`omni-actions` → executor → `omni-action-feedback` → re-evaluation

## COMPONENT ROLES (OMNI_WORKER_ROLE)

| Role | Active loops | Deployed thật? |
|---|---|---|
| `full` | tất cả: evidence, actions, feedback, kpi, knowledge, siem-chains, siem-correlation (port Python của brain-go, gate `OMNI_SIEM_CORRELATION_ENABLED`), tier | ✅ pod `omni-fullstack` |
| `onboarding` | discovery-evidence worker | ✅ pod `omni-onboarding` (riêng `omni-fullstack`) |
| `analyst` | kafka_evidence_loop, action_feedback, kpi, knowledge, siem-chains, tier | ❌ RETIRED 2026-07-02 (manifest xóa từ `915e509`, object cluster đã dọn) |
| `prober` | kafka_alerts_loop, delayed_queue, circuit_breaker, telegram_polling | ❌ RETIRED 2026-07-02 |
| `core` | deep_scout, forecast, baseline_snapshot, proactive | ❌ RETIRED 2026-07-02 |
| `executor` | kafka_actions_loop | ❌ RETIRED 2026-07-02 (mutation logic nay chạy trong `full`) |
| `gateway` | FastAPI HTTP → kafka omni-alerts (separate image, deployment riêng `omni-gateway`) | ✅ |

Ghi chú: các role split (`analyst/prober/core/executor`) từng tồn tại như Deployment riêng ở giai
đoạn kiến trúc trước consolidation `915e509`; nay logic của chúng chạy gộp trong `role=full`
(`omni-fullstack`). Không tạo lại Deployment riêng cho các role này trừ khi có quyết định kiến trúc
mới rõ ràng.

## INVARIANTS (vi phạm = bug)

- Async-only: `asyncio`, `kubernetes-asyncio`, `redis[hiredis]`, `aiokafka`. No subprocess for K8s.
- `src/gateway/` KHÔNG import `workers/`. Shared code → `src/pkg/`.
- Mutations only via executor; analyst is read-only.
- `OMNI_AUTO_EXECUTE_ENABLED=false` — master kill-switch (fail-closed) tại **ConfigMap**
  `omni-worker-configmap` (default an toàn). Deployment env: có thể override `true` — hiện đang
  override cho lab, xem "Kill-switch" trong DEPLOYMENT STATE để biết giá trị hiệu lực thật.
- **CRAT Fail-Closed**: `write_audit_block()` MUST succeed trước Telegram emit / action dispatch.
- `kafka_evidence_loop` dùng `auto_offset_reset="earliest"` — KHÔNG đổi thành `latest`.
- `omni-audit-chain` topic cần message key (compact policy).
- `INV_NO_RESTART_ON_BROKEN_SPEC` · `INV_READ_BEFORE_MUTATE` · `INV_NAMESPACE_ISOLATION` · `ERR_REA_NO_PHYSICAL_PROOF` · `ERR_GOV_UNAUTHORIZED_MUTATION`
- `INV_KNOWLEDGE_NOT_ALERT` — **nới có kiểm soát 2026-07-30**: `METRIC_SAMPLE` nay ĐƯỢC phân
  tích (baseline + phát hiện lệch, thuần số, KHÔNG LLM); chỉ khi lệch mới nâng thành
  `ANOMALY` và vào pipeline đầy đủ. Một mẫu bình thường vẫn **không** gọi LLM, **không** tạo
  incident. Có dedup `omni:knowledge:promoted:{tenant}:{host}:{metric}` TTL 600s.
- `INV_DATA_RESIDENCY`: tài liệu khách hàng chỉ lưu metadata trên Omni (file_id + summary
  ≤2000 chars). Metric **số** (cpu/mem/disk) KHÔNG thuộc phạm vi này — đó là số đo, không
  phải dữ liệu khách.
- Catalogue lệnh chẩn đoán **fail-closed ở tầng LOAD**: nạp lỗi ⇒ từ chối MỌI lệnh. Đường
  dẫn mặc định phải resolve được ở **cả hai layout** (repo `src/pkg/...` và bundle
  `/opt/omni-remote-agent/pkg/...`) và **cả hai định dạng** (`.yaml` repo, `.json` bundle —
  host khách không có PyYAML). Xem `_default_catalog_candidates()`.
- Domain do collector **tự khai** thắng mọi suy đoán: luôn truyền `domain_hint=` vào
  `detect_domain()`. Bỏ sót là để cascade nội dung gán sai lĩnh vực (đã trả giá).
- RBAC: worker SA (`omni-fullstack`) **không có quyền Secrets tuỳ ý** — ngoại lệ duy nhất có chủ
  đích là `patch`/`update` qua `omni-executor-mutate-lab` ClusterRole
  (`k8s/deployments/omni-fullstack-rbac.yaml`), backing cho tool `k8s_patch_secret` đã gate bằng
  `required_evidence` + `MUTATE_TOOL_ALLOWLIST` (xoay vòng credential khi remediation SIEM/security
  — xem `src/workers/analyst_agentic_loop.py`). Không mở rộng quyền Secrets ngoài phạm vi tool này.
  Executor: NEVER cluster-admin.
  ⚠️ **Nhãn RBAC lỗi thời — ĐÃ SỬA 2026-08-13 (Đ68, phát hiện Đ62, gác quyết định ở Đ64)**:
  `k8s/deployments/omni-fullstack-rbac.yaml` từng tự gắn `omni.io/env: lab` +
  `omni.io/note: "Lab-only. Do not bind in prod."` trên `ClusterRole`/`ClusterRoleBinding
  omni-executor-mutate-lab` — sai vì đây là MỘT manifest DUY NHẤT (không có bản `.gcp.yaml` song
  song), đang bind thật trên cluster GCP production. Quyết định: **giữ nguyên tên
  `omni-executor-mutate-lab`** (đổi tên là breaking change cho binding + mọi chỗ tham chiếu, rủi
  ro cao hơn lợi ích), **không tách file GCP riêng** (cần rà lại toàn bộ đường sync ArgoCD
  `directory.include`, để sau nếu thực sự cần) — chỉ gỡ label `omni.io/env: lab` và sửa lại
  annotation cho khớp sự thật (đang bind thật trên cả lab và GCP, có chủ đích, đã gate đủ). Không
  đụng `rules:`/verbs/resources thật của ClusterRole — chỉ sửa metadata.
- `OMNI_LLM_NUM_CTX` default 8192. Dùng `build_llm_options(ctx)` — không inline getattr.
- Autonomy tier: `resolve_tier` ưu tiên Redis cache `omni:cfg:tier:{tenant}` > PG > env. Đổi env phải DEL cache.

## CRAT (SOX §404, PCI-DSS v4.0)

`src/services/audit_ledger/` — SHA-256 hash-chain + Ed25519. Events: `ADVISORY_DECISION`, `ADVISORY_DISPATCHED`, `MUTATION_TRAPPED`, `HITL_DECISION`, `ROLLBACK_EXECUTED`.
`OMNI_AUDIT_PRIVATE_KEY_PATH` — PEM Ed25519 (unset = unsigned, lab only).

## INFRASTRUCTURE

- **K8s**: OrbStack, namespace `multi-agent`. **KHÔNG phải pod duy nhất** — xem "DEPLOYMENT STATE" bên
  dưới cho topology thật (đã audit runtime, không phải suy diễn từ tài liệu cũ).
  `make deploy-worker` = `deploy-fullstack` (chỉ deploy `omni-fullstack`, không phải toàn bộ stack).
- **LLM (OrbStack lab — KHÔNG phải GCP, xem cảnh báo ⚠️ đầu file)**: Ollama `qwen3:8b` (active, đổi
  từ `qwen2.5-coder:7b` 2026-08-03 — xem comment `k8s/deployments/omni-worker-configmap.yaml`: đã
  thử `qwen3.6:27b` trước, revert vì llama-server ăn 300%+ CPU; `qwen3:8b` gần footprint
  `qwen2.5-coder:7b` cũ, thế hệ mới hơn) + `nomic-embed-text:latest` (768-dim). Host:
  `host.orb.internal:11434`. **GCP production KHÔNG dùng cấu hình này** — dùng NVIDIA NIM cloud
  (`OMNI_LLM_PROVIDER=nim`, embed 1024-dim), xem cảnh báo ⚠️ đầu file (sửa 2026-08-17).
- **DB**: PostgreSQL `omni_admin` schema (**32 bảng thật** — xác nhận qua `pg_tables` trực tiếp
  2026-08-03, có `migration_0014_state` nghĩa là ≥14 migration đã chạy, không phải 4; đừng tin số "19
  bảng"/"migration 000{1..4}" nếu thấy ở tài liệu cũ khác), migration tự động lúc worker khởi động qua
  `run_migrations()` cho role `full/analyst/onboarding` nếu
  `OMNI_ADMIN_PG_DSN` không rỗng) = source-of-truth autonomy config + tenant registry. Tenant PHẢI
  được provision qua `AdminConfigRepo.create_tenant()` / `POST /autonomy/tenants` trước khi
  onboarding pipeline ghi `tenant_readiness_state` cho tenant đó — thiếu bước này gây FK violation
  liên tục (xem post-mortem `docs/post-mortems/drift-correction-2026-07-02.md`). Redis = hot-path
  cache + RAG HNSW + audit chain.
- **Tests**: pytest `asyncio_mode=auto` `pythonpath=src`; dùng `FakeRedis(decode_responses=True)` cho ZSET tests (không AsyncMock).

## KEY DIRS

`src/workers/` · `src/gateway/` · `src/remote_agent/` · `src/anomaly/` · `src/services/{analyst,audit_ledger,knowledge}/` · `src/rag/` · `src/pkg/` · `k8s/deployments/` · `tests/`

## COMMANDS

```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration   # unit tests
make deploy-worker deploy-gateway ensure-kafka-topics              # deploy
make e2e-proactive e2e-incident-matrix                             # E2E
curl localhost:8090/healthz && curl localhost:8090/readyz          # health
make benchmark-advisory                                            # advisory quality
NS=multi-agent make omni-death-loop                                # chaos loop
```

## ENV (critical)

`OMNI_WORKER_ROLE` · `OMNI_ENV_MODE` (lab|prod) · `OMNI_KAFKA_BOOTSTRAP_SERVERS` · `OMNI_REDIS_URL` · `OMNI_OLLAMA_BASE_URL` · `OMNI_AUTO_EXECUTE_ENABLED` (default false) · `OMNI_LLM_NUM_CTX` (default 8192) · `OMNI_KAFKA_TOPIC_KNOWLEDGE_EVIDENCE` (default omni-knowledge-evidence) · `OMNI_AUDIT_PRIVATE_KEY_PATH` · `OMNI_TENANT_APIKEYS` (tenant_id:key,...) · `OMNI_GATEWAY_API_KEY` · `OMNI_EXECUTOR_FORCE_NSENTER` (bool, đang `true` trên omni-fullstack — khi bật, `autonomous_execute.py` chặn cứng `ERR_GOV_UNAUTHORIZED_MUTATION` cho mọi mutate tool KHÁC `kubectl_cluster`; xem `settings.py::omni_executor_force_nsenter`, `kubectl_cluster.py::_force_nsenter()`)

## DEPLOYMENT STATE (2026-07-02, xác minh qua Whole-System Reality Audit + Drift Correction Slice)

### Declared target topology
`omni-fullstack` (role=full) là workload lõi duy nhất được `make deploy-worker` deploy mặc định.
`omni-gateway`, `omni-onboarding` là các Deployment RIÊNG BIỆT, có
manifest/target Makefile riêng — không phải "instance phụ của omni-fullstack".
(`omni-brain-go` từng nằm trong danh sách này — RETIRED 2026-07-22, đã port vào omni-fullstack.)

**2026-07-06: `omni-ui` (Deployment/Service/Ingress, domain `omni.ai-agent.local` +
`portal.ai-agent.local`) đã RETIRED theo yêu cầu trực tiếp của user** — portal thật duy nhất
nay là provider/tenant Next.js apps (`aoip-provider-web`/`aoip-tenant-web` + BFF
`aoip-provider-portal`/`aoip-tenant-portal`, domain `provider.ai-agent.local` /
`tenant.ai-agent.local`). Manifest `k8s/deployments/omni-ui.yaml` đã xoá; ingress rules omni/portal
đã gỡ khỏi `k8s/ingress/ai-agent-local.yaml`. `make e2e-portal` đã trỏ lại
`tests/e2e_portals` (13 test thật, xanh) thay vì `ui/e2e` (omni-ui cũ). Root `ui/` (Next app cũ,
~25 route: pipeline/ledger/siem/workers/admin/...) CHƯA bị xoá khỏi source — không còn deploy
route nào tới nó nhưng code vẫn còn trong repo vì chưa xác nhận 100% feature-parity đã port hết
sang provider/tenant; xoá source tree là quyết định riêng cần xác nhận thêm.

### Current deployed topology (đã kubectl describe/exec xác minh trực tiếp)
| Deployment | Role/chức năng | Trạng thái |
|---|---|---|
| `omni-fullstack` | `OMNI_WORKER_ROLE=full`, tier hiệu lực = Redis cache `omni:cfg:tier:default`=`shadow` | 1/1 Running |
| `omni-gateway` | FastAPI HTTP ingress (không import `workers/`) | 1/1 Running (restart do race Kafka-chưa-ready lúc pod khởi động — dependency outage, tự phục hồi, không phải bug) |
| `omni-onboarding` | `OMNI_WORKER_ROLE=onboarding` — discovery-evidence worker | ✅ **FIX ĐÃ DEPLOY + VERIFY SỐNG 2026-08-03.** Trước fix: crash-loop thật, 15 restart/3h27m, exit 137. Root cause: `src/workers/omni_worker.py` (`_worker_background_tasks`) từng chạy `kafka_discovery_evidence_loop` cho CẢ role=full lẫn role=onboarding, cả hai join CÙNG 1 `consumer_group_onboarding` cố định — 2 member tranh nhau 1 topic 1-partition (`omni-discovery-evidence`), gây rebalance lặp lại mỗi khi 1 trong 2 pod restart (log "Heartbeat failed...rebalancing" xuất hiện ĐỒNG THỜI ở cả 2 pod — bằng chứng quyết định). Lý do cũ ("role=full phải chạy vì khi đó chưa có deployment onboarding riêng") đã lỗi thời từ khi `omni-onboarding` thành Deployment riêng thật. Fix: role=full không còn đăng ký loop này (điều kiện đổi `role in ("full","onboarding")` → `role == "onboarding"`). `make deploy-fullstack` + `kubectl rollout restart deployment/omni-onboarding` đã chạy — pod mới `Restart Count=0`, `grep -c rebalancing` = 0 trong 3 phút quan sát sau deploy (trước đó lặp lại mỗi ~66s). Test `tests/test_worker_role_discovery_consumer.py` cập nhật, 83 test liên quan xanh. |
| ~~`omni-brain-go`~~ | **RETIRED 2026-07-22** — đã port sang Python (`src/services/siem_correlation/` + loop `kafka_siem_correlation_loop` trong `omni-fullstack`, group `omni-siem-correlation`, cùng Redis key layout `corr:*`). Parity test PASS 2/2 (output Python == Go từng field trên cùng input) trước khi xoá Deployment + manifest. KHÔNG bật lại brain-go song song với `OMNI_SIEM_CORRELATION_ENABLED=true` — 2 engine sẽ đua trên cùng key `corr:*` và double-emit chains. Lưu ý parity: khi các event đến trong CÙNG 1 giây, sequence-score phụ thuộc thứ tự tie (Go sort không ổn định = ngẫu nhiên; Python ổn định newest-first = bảo thủ hơn) — hành vi vốn có của cả 2 engine, không phải bug port. | Deployment đã xoá |
| `redis-0`, `kafka`, `omni-postgres-0`, `redis-exporter`, `aoip-dex`, `aoip-provider-*`, `aoip-tenant-*` | portal/hạ tầng phụ trợ (provider/tenant portal là portal thật duy nhất, `omni-ui` đã retired) | Running |

### Kill-switch — effective value đã xác minh qua MCP (`mcp__kubernetes`, read-only) trên pod thật

**Cập nhật 2026-08-03** (describe pod `omni-fullstack` trực tiếp qua MCP, không phải cache tài
liệu): ConfigMap `omni-worker-configmap` vẫn giữ default an toàn
(`OMNI_AUTO_EXECUTE_ENABLED="false"`, `OMNI_SIEM_SUGGEST_ONLY="true"`) — nhưng Deployment
`omni-fullstack` hiện có **override `env:` sống** đè lên default đó:

```
OMNI_AUTO_EXECUTE_ENABLED:    true
OMNI_AUTO_ROLLBACK_ENABLED:   true
OMNI_SIEM_SUGGEST_ONLY:       false
OMNI_LAB_AUTO_EXECUTE_AGENTS: staging-sim_cust-app,staging-sim_cust-edge,staging-sim_cust-db
```

⚠️ **Giá trị `OMNI_LAB_AUTO_EXECUTE_AGENTS` ở trên đã LỖI THỜI kể từ audit 2026-08-13 (Đ62/Đ64)** —
`kubectl get deployment omni-fullstack -n multi-agent` xác nhận trực tiếp giá trị hiệu lực THẬT
hiện nay là tenant `loyalty-uat`, không phải `staging-sim`:
```
OMNI_LAB_AUTO_EXECUTE_AGENTS: loyalty-uat_cust-app,loyalty-uat_cust-db,loyalty-uat_cust-edge
```
Không rõ đổi từ khi nào giữa 2026-08-03 và 2026-08-13 (không có entry handoff nào ghi lại việc đổi
tenant lab) — cơ chế allowlist/blast-radius-control vẫn y nguyên như mô tả dưới đây, chỉ đổi TÊN
tenant. Đoạn tường thuật sự cố 401 bên dưới (dòng ~315-335) giữ nguyên `staging-sim` vì đó là ghi
chép lịch sử đúng tại thời điểm sự cố xảy ra (2026-08-03) — không sửa lại thành `loyalty-uat`.

⚠️ **`OMNI_AUTO_ROLLBACK_ENABLED`/`OMNI_SIEM_SUGGEST_ONLY=false` ở khối trên đã LỖI THỜI kể từ audit
2026-08-17** — `kubectl get deployment omni-fullstack -o jsonpath='{...env}'` xác nhận trực tiếp
Deployment env hiện tại KHÔNG có 2 biến này (chỉ còn `OMNI_AUTO_EXECUTE_ENABLED=true`,
`OMNI_LAB_AUTO_EXECUTE_AGENTS`, `OMNI_EXECUTOR_FORCE_NSENTER=true` và vài biến khác không liên quan
kill-switch). Giá trị hiệu lực thật của `OMNI_SIEM_SUGGEST_ONLY` hiện nay là `true` (đọc từ
ConfigMap `omni-worker-config`, không có override) — SIEM đang ở chế độ suggest-only, không phải
`false` như bảng trên ghi. Không rõ khi nào 2 biến này bị gỡ khỏi Deployment env giữa 2026-08-03 và
2026-08-17 — không có entry handoff nào ghi lại. Giữ nguyên khối cũ bên trên làm lịch sử, không sửa
lại.

Đây là **chủ đích** (không phải drift kiểu 2026-06-11) — chỉ mở autonomous mutate cho đúng 3 VM
lab qua allowlist `OMNI_LAB_AUTO_EXECUTE_AGENTS`, đúng cơ chế blast-radius control mà
`auto_recovery_bridge.dispatch_if_eligible()` đã kiểm (xem Đ8 trong handoff). Namespace K8s vẫn
giới hạn `OMNI_AUTONOMOUS_ALLOWED_NAMESPACES=multi-agent`. Claim cũ "đã revert 2026-07-02, gỡ khỏi
Deployment env" chỉ đúng tại thời điểm đó — **không còn đúng hiện tại**, đã lỗi thời.

`OMNI_AUTONOMY_TIER` override vẫn không có trên Deployment env — tier hiệu lực vẫn chỉ đến từ Redis
cache/PG theo đúng invariant `resolve_tier`. Precedence: ConfigMap (default an toàn) < Deployment
`env:` override (nay CÓ tồn tại, có chủ đích, scoped) < Redis cache (nguồn hiệu lực thật cho tier
riêng).

`OMNI_TELEGRAM_POLLING_ENABLED`: claim cũ ("ConfigMap false nhưng Deployment env override true")
đã SAI, kiểm tra lại trực tiếp Deployment không có override nào — **ĐÃ FIX 2026-08-13 (Đ61)**: giá
trị ConfigMap `omni-worker-config` (GCP) nay là `"true"` thật (trước đó là `"false"`, khiến
`telegram_loop` không đăng ký ⇒ nút Đúng/Sai HITL trên Telegram không được nhận — xem Đ61 trong
`docs/handoffs/CURRENT_SESSION.md`).

**Thêm 1 biến sống chưa từng ghi ở đây trước audit 2026-08-03**: `OMNI_EXECUTOR_FORCE_NSENTER=true`
trên Deployment `omni-fullstack`. Đây là một lớp gate ĐỘC LẬP với kill-switch/allowlist ở trên —
khi bật, `autonomous_execute.py` (dòng ~125-133) chặn cứng `ERR_GOV_UNAUTHORIZED_MUTATION` cho bất
kỳ mutate tool nào KHÁC `kubectl_cluster`; `kubectl_cluster.py::_force_nsenter()` bọc lệnh qua
`nsenter` thay vì exec thẳng trong container. Với giá trị hiện tại, executor trên thực tế bị thu hẹp
chỉ còn 1 con đường mutate.

**Gap vận hành ĐÃ FIX + DEPLOY + VERIFY SỐNG 2026-08-03** (trước đó là gap đang sống): agent
`staging-sim_cust-app` — 1 trong đúng 3 agent nằm trong `OMNI_LAB_AUTO_EXECUTE_AGENTS` ở trên — bị
Gateway trả **401 Unauthorized trên mọi request**, trong khi `staging-sim_cust-edge`/
`staging-sim_cust-db` đều 200 OK.

Root cause thật (có `kubectl exec` qua Bash local, không chỉ MCP read-only, nên điều tra được tận
gốc): `sha256()` của API key thật trên VM khớp **tuyệt đối** với `key_hash` trong
`omni_admin.agent_credential` (`status='active'`) — dữ liệu hoàn toàn đúng, không phải sai
credential. `kubectl logs --since=48h` phát hiện dòng quyết định:
`omni-gateway: admin store init fail: [Errno 111] Connection refused` — gateway khởi động TRƯỚC
khi Postgres sẵn sàng (cùng lớp race đã biết với Kafka producer ở pod này), thử kết nối
`create_admin_pool()` ĐÚNG 1 LẦN rồi bỏ cuộc vĩnh viễn (`app.state.admin_repo` treo `None` suốt
vòng đời pod, không có retry). Vì `_resolve_agent_credential()` trả `None` ngay khi `admin_repo is
None` (`api.py`), MỌI request dùng per-agent credential (chỉ `cust-app` dùng nhánh này — 2 agent
kia dùng tenant-shared key nên không đụng `admin_repo`) đều 401 bất kể key đúng hay sai.

Fix: thêm `_connect_admin_pool_with_retry()` (`src/gateway/api.py`) — bounded retry 5 lần, backoff
1s→10s quanh `create_admin_pool()`, tách hàm riêng để test được (`tests/test_gateway_admin_pool_retry.py`,
4 test). `make deploy-gateway` đã chạy — log pod mới: `"admin config store ready (omni_admin)"`,
và `staging-sim_cust-app` ngay sau đó register/evidence/commands đều 200 OK (verify trực tiếp qua
`kubectl logs`, không suy đoán). Auto-execute allowlist nay hoạt động đủ 3/3 agent thật.

### PUBLIC PLANE — app.omnisre.xyz (2026-07-29, đang sống trên Internet)

Omni đã public thật qua Cloudflare Free, core vẫn chạy trên MacBook. Không VPS, không
mở port router. `bash cloudflare/tunnel/verify.sh` → 17 PASS / 0 FAIL / 0 SKIP.

```
browser → Cloudflare Access (chỉ danghien2907@gmail.com, one-time PIN)
        → Tunnel `omnisre` (LaunchAgent com.omnisre.cloudflared)
        → Traefik 192.168.139.2:80  → Ingress `omnisre-public-console`
             /auth, /api/provider/v1 → aoip-provider-portal-public
             /dex                    → aoip-dex-public
             /                       → aoip-provider-web-public
```

**INV_PUBLIC_PLANE_ISOLATED (vi phạm = bug).** Mặt public có auth plane RIÊNG. Lab
`provider.ai-agent.local` / `aoip-dex` **KHÔNG được đổi một biến nào** — đặc biệt
`AOIP_OIDC_PROVIDER_ISSUER` và `issuer` trong ConfigMap `aoip-dex-config`. Lý do:
`verify_id_token` so `iss` bằng chuỗi tuyệt đối (`oidc.py:104`), đổi issuer là breaking
cho lab. `verify.sh` nhóm A canh đúng bất biến này. Quyết định đầy đủ: ADR 0001.

Frontend **cũng** phải tách, không chỉ backend: `aoip-provider-web` có
`AOIP_BACKEND_URL` cứng trỏ backend lab và server component gọi qua đó kèm cookie
(`ui/packages/api-client/src/index.ts:20`) — dùng chung sẽ khiến traffic public chui
qua backend lab và **chạy im lặng** vì `portal:session:` chung Redis.
`aoip-provider-web-public` dùng **cùng image**, chỉ khác một biến env.

Đồng bộ code local → public:
```bash
make sync-public-ui | sync-public-backend | sync-public | sync-public-all
```
Bắt buộc dùng target này, KHÔNG `kubectl rollout restart` tay: `imagePullPolicy:
IfNotPresent` + tag `:latest` ⇒ restart không build gì. Script build rồi so `imageID`
mọi pod Running với image local. Lab và public dùng CHUNG tag image — mặc định chỉ
restart public, `--with-lab` mới đụng lab.

`api.omnisre.xyz` / `agent.omnisre.xyz` **CHƯA public, cố ý.** Mở là phase riêng; chặn
kỹ thuật phải xử lý trước: `/auth` và `_require_api_key` chưa có rate limit tầng app.
⚠️ **Claim "chưa có rate limit" đã LỖI THỜI kể từ audit 2026-08-17** — `src/gateway/api.py:356
_rate_limit_key`, `:361 _take_rate_limit_token`, `:671-677` áp dụng rate limit thật + metric
`429_rate_limit`; `src/gateway/routes/agent_webhook.py:229 _check_rate_limit` tương tự. Đã verify
sống trên GCP: mọi endpoint nhạy cảm của `gateway.omnisre.xyz` (`/autonomy/tenants`, `/crat/stats`,
`/agents`, `/kpi/summary`, ...) trả 401 `{"detail":"Invalid or missing API key"}` khi thiếu key,
tức có tầng auth + rate-limit đang chặn. Không rõ code này được thêm khi nào — chặn kỹ thuật để mở
`api.omnisre.xyz`/`agent.omnisre.xyz` (mặt public riêng theo ADR 0001, MacBook) có thể đã bớt đi,
nhưng đó là mặt public khác GCP — cần audit lại riêng nếu định mở 2 domain này.

Không nằm trong git (mất là phải tạo lại tay): Secret `aoip-dex-public-config` +
`aoip-provider-portal-public-secret`, role `platform_owner` seed thủ công trong Redis,
credentials tunnel, LaunchAgent plist.

⚠️ `client_secret` PHẢI sinh bằng `openssl rand -hex`, không phải `-base64`: `+` bị Dex
URL-decode thành dấu cách (RFC 6749 §2.3.1) trong khi `httpx` gửi Basic thô → fail dù
Secret giống hệt. Debug callback OIDC: đọc log lần gọi **ĐẦU TIÊN**; các lần sau luôn là
400 "invalid or expired state" do state one-time, không phải nguyên nhân.

Tài liệu: `docs/adr/0001-cloudflare-pages-tunnel-local-core.md` ·
`docs/deployment/cloudflare-macbook.md` · `docs/runbooks/cloudflare-public-access.md`

### Retired compatibility artifacts
`omni-analyst`, `omni-core`, `omni-executor`, `omni-prober`, `omni-worker` — manifest đã bị xóa khỏi
git từ commit `915e509` (split-role consolidation) nhưng object Deployment (`replicas=0`) vẫn còn sót
trong cluster đến 2026-07-02 → đã `kubectl delete` dứt điểm (kèm Service `omni-analyst` orphan, đã
`git rm k8s/services/omni-analyst-service.yaml`). `omni-siem-bridge`, `omni-hitl-dispatcher`,
`omni-evidence-adapter` — **claim "scaled-down-intentional, KHÔNG xóa" HẾT HIỆU LỰC 2026-08-10
(Đ49 S0.1/S0.2)**: cả 3 manifest + `deploy-siem-stack` target + code (`hitl_dispatcher.py`,
`siem_bridge.py`, `evidence_adapter/worker.py`) đã xóa hẳn — đây là phần "gộp FinGuard thành Smart
SIEM nội bộ" (`plans/finguard-to-smart-siem-merge-2026-08-04.md`, phase S0). Namespace
`finguard-customer` mà `omni-hitl-dispatcher` từng gọi tới không còn tồn tại trong cluster.
Correlation engine SIEM vẫn chạy trong `omni-fullstack` (loop `kafka_siem_correlation_loop`,
không đổi). Đường ingest mới: agent → gateway → `omni-siem-raw` trực tiếp (phase S2), không qua
bridge nữa.

⚠️ **RAG `omni:rag:sop` HLEN=1019 đã LỖI THỜI, xác nhận SAI qua audit 2026-08-17** —
`kubectl exec redis-0 -- redis-cli hlen omni:rag:sop` = **0**, và `--scan --pattern 'omni:rag*'`
không trả về key nào (rỗng hoàn toàn). Nghi do sau re-index 768→1024 dim của Đ60, corpus SOP chưa
từng được nạp lại — nhánh triage RAG tra SOP hiện tra về rỗng. `FT._LIST` vẫn liệt kê tên index
(`itops_sop_ledger`/`itops_sop_ledger_v2`/`playbooks`) nhưng đều rỗng. Cần re-ingest corpus SOP,
chưa xử lý — xem `docs/audit/omni_audit_2026-08-17.xlsx` dòng 18. Câu gốc dưới đây giữ nguyên làm
lịch sử: RAG `omni:rag:sop` HLEN=1019 (khớp MEMORY.md). Redis AOF enabled. Knowledge pipeline active
(`omni-knowledge-evidence`). **Cập nhật 2026-07-03** (đã xác minh lại `scripts/kafka_ensure_omni_topics.sh`):
claim "mọi topic PartitionCount=1" đã lỗi thời — `omni-knowledge-evidence` dùng 3 partitions
(dòng ~47-52, comment "Enforcing omni-knowledge-evidence config... partitions=3"), topic SIEM dùng
6 partitions; phần lớn topic còn lại (`omni-diagnostic-evidence`, `omni-audit-chain`, `omni-alerts`,
...) vẫn 1 partition/1 replication-factor (lab single-broker, chưa phải rủi ro data-loss vì
`auto_offset_reset="earliest"` + không multi-broker failover). Không còn là drift tài liệu, chỉ là
throughput headroom thấp cho lab hiện tại — không cần sửa gấp.

### VM/Agent truth (2026-07-02, cập nhật 2026-08-11)
Access method đúng là `orb -m <machine> <command>` (không phải SSH thẳng tới IP). Cả 3 VM lab đã
audit trực tiếp: `cust-edge` (nginx :80, NFS, portmapper), `cust-app` (app :8080), `cust-db` (MySQL
:3306 + Redis :6379, cả hai bind localhost-only).

⚠️ **Unit systemd đúng là `aoip-agent.service` (chạy `aoip.agent.employee`) — KHÔNG phải
`omni-remote-agent.service`.** Đây là HAI service KHÁC NHAU, không phải một cái đổi tên (mô tả cũ
ở dòng này ghi *"`omni-remote-agent.service` (tên khác `aoip-agent`)"* là SAI, và chính câu sai đó
đã trực tiếp gây ra sự cố 2 agent chạy song song suốt 7 ngày — xem
`docs/audit/regression_agent_dual_process_2026-08-11.md`):
- `aoip-agent.service` → `aoip.agent.employee` — **runtime production duy nhất trên VM khách
  hàng** (1 process 2 vòng: telemetry reuse `remote_agent.run_agent()` **làm thư viện** + durable
  command/mutation daemon). Deploy thật từ Sprint IT-7, canonical theo ADR-001.
- `omni-remote-agent.service` → `remote_agent.agent` — unit gốc đã lỗi thời, **đã gỡ hẳn khỏi cả 3
  VM lab ngày 2026-08-11** (không giữ làm rollback path, có chủ đích — giữ-disabled từng khiến bug
  double-agent tái phát). Nếu thấy unit này sống lại ở đâu = có script/lệnh cũ đang gọi nhầm.

⚠️ **`/opt/omni-remote-agent/` vẫn là thư mục cài đặt ĐANG DÙNG** (tên thư mục giữ theo lịch sử,
đừng để tên đánh lừa): `aoip.agent.employee` import trực tiếp code `remote_agent/` bên trong đó.
TUYỆT ĐỐI không `rm -rf /opt/omni-remote-agent` khi "dọn agent cũ" — sẽ giết luôn agent đang chạy.

Chi tiết migration + lý do: `plans/consolidate-vm-agent-remote-to-aoip-employee-2026-08-11.md`.

### Productization Iteration 1 — System Twin (2026-07-02)
`omni:aoip:system_model:{tenant}` trống hoàn toàn dù O1/O2A/O2B claim DONE — root cause là
**deployment drift**: image `multi-agent-system:latest` chưa `make docker-worker` rebuild kể từ
`1bc6292`, pod chạy thiếu hẳn `_project_into_system_model`. Đã rebuild+redeploy (digest
`c2d433daac77...`). Twin nay có dữ liệu thật cho 2/3 host (cust-edge, cust-db) — `cust-app` chưa có
discovery probe nào (chỉ metrics/log probe), là bottleneck kế tiếp. Chi tiết + capability matrix:
`docs/product/PRODUCT_PROOF.md`. Bài học: `test pass + push` KHÔNG chứng minh đã deploy — luôn
`hasattr()` check module trong pod đang chạy trước khi coi slice DONE.

## COMMUNICATION

- **Code first.** Viết code ngay, không hỏi lại.
- **Giải thích tối đa 100 chữ** khi thật sự cần.

## AUTONOMY RULES

EXPLORE → PLAN → VERIFY → GIT. CI/CD loop tự động trong Lab.

**Standing authorization (2026-08-04, GCP VM, full CI/CD có sẵn):** với hạ tầng đã có CI/CD đầy đủ
(Gitea → Jenkins build/rollout, ArgoCD drift-detect, rollback được qua `kubectl rollout undo` /
revert commit), **mỗi thay đổi code/docs được quyền tự `git commit` + push lên CẢ HAI remote**
ngay sau khi verify xong (test/lint pass, hoặc với doc/comment-only change: đọc lại diff cho khớp
ý), KHÔNG cần hỏi lại xác nhận commit mỗi lần — vì lỗi triển khai đã có đường rollback an toàn.
Vẫn áp dụng nguyên Git Safety Protocol ở system prompt cho các thao tác phá hoại/khó đảo ngược
(force-push, reset --hard, xoá branch, sửa lịch sử) — những thao tác đó vẫn phải hỏi trước.

**Hai remote, hai mục đích khác nhau (2026-08-04, Đ34) — PHẢI push cả hai, không phải chọn một:**
- `git push gitea main` → Gitea nội bộ (`http://100.67.117.19:30300/hiendang/project.git`,
  namespace `cicd` TRÊN CHÍNH VM GCP) — đây là nguồn Jenkins/ArgoCD build & deploy thật.
- `git push origin main` → GitHub (`git@github.com:hiendt2907/project.git`) — bản lưu trữ ĐỘC LẬP
  khỏi GCP, sống sót nếu VM/credit GCP mất. Gitea chạy trong chính cluster đang được deploy nên
  KHÔNG thể coi là backup của chính nó.
- Gotcha đã xảy ra thật: 21 commit (toàn bộ quá trình migrate GCP — Harbor/ArgoCD/Vault/
  Vaultwarden/Istio/Dex/monitoring) chỉ được push `gitea`, GitHub bị bỏ quên suốt — nếu GCP mất
  trước khi phát hiện, GitHub sẽ chỉ còn bản rất cũ. Luôn `git push gitea main && git push origin
  main` cùng lúc, không chỉ push một bên.
- Submodule `smart-siem`: `.gitmodules` trỏ Gitea nội bộ (để ArgoCD repo-server clone được không
  cần token GitHub — repo GitHub gốc là private, gây `ComparisonError` cho app `omni-core` trước
  Đ34) nhưng repo GitHub `hiendt2907/smart-siem` vẫn giữ song song làm backup, cũng cần push cả
  hai khi sửa code trong `smart-siem/`.

`#` để ghi rule mới vào đây.
