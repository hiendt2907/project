# Current Session Handoff

**Cập nhật:** 2026-08-13 (Đ62 — audit toàn dự án: 2 gap MỚI mức Cao (monitor namespace sập vì hết
CPU node; `case_ledger.domain` 100% `unknown` do lệch trục lane), 2 gap MỚI mức Trung (tenant
auto-execute allowlist trong CLAUDE.md lỗi thời; nhãn RBAC "lab-only" bind nhầm trên cluster
production) — CHƯA FIX, chỉ mới ghi nhận. Đ61 — fix Telegram callback (nút Đúng/Sai HITL) không
được nhận: bật lại `OMNI_TELEGRAM_POLLING_ENABLED`, verify sống. Đ60 — re-index RAG sang NIM
1024-dim DONE. Đ59 (rollback + chuyển NIM) DONE bên dưới.)

### Đ62 — Audit toàn dự án: chưa nối vào / đang lủng (2026-08-13)

Audit chủ động (subagent, đọc trực tiếp cluster qua `kubectl` + query Postgres + đọc code, không
suy đoán từ tài liệu cũ). Toàn bộ dưới đây là **phát hiện, chưa fix** — ghi lại để làm việc tiếp.

**1. [Cao] Namespace `monitor` sập hoàn toàn — hết CPU node.** `kubectl get pods -n monitor`:
grafana/loki/mimir/tempo đều `0/1 Pending`, `FailedScheduling: Insufficient cpu`.
`omni-k3s-vm` CPU requests 3935m/4000m (98%). prometheus/alertmanager/kube-state-metrics/
node-exporter/promtail vẫn Running. Không có giám sát observability (dashboard/log/trace) cho
production ngay lúc audit. Đây là hệ quả tiếp diễn của CPU-oversubscription đã biết ở Đ47/Đ59-CPU
(từng phải scale 0 tạm các pod này để rollout `omni-fullstack` lên lịch được ở Đ61) — nhưng lần
này chúng đứng yên ở `Pending`, không tự phục hồi.

**2. [Cao] `omni_admin.case_ledger.domain` = `'unknown'` cho 100% (305/305) dòng.** Root cause:
`telegram_advisory_emitter.py:451` / `remote_diagnosis_emitter.py:383` truyền `lane_label`/
`session["lane"]` (thực chất là **proof_lane trục B**: `resource/state/app_log`, xem cảnh báo
"lane là BA trục" trong `pkg/domain/taxonomy.py`) vào `open_advisory_case(lane=...)` →
`src/services/case_ledger/store.py:34` ghi thẳng vào cột `lane`. Migration `0014_lane_to_domain.sql`
hàm `lane_to_domain()` chỉ nhận biết lane **trục A** (`sys_resource/app_http/siem_security/
sys_hard_fail`); giá trị thật trong cột (303 rỗng, 2 `state`) không khớp case nào → luôn rơi vào
`ELSE→'unknown'`. Hệ quả: mọi báo cáo/competency phân loại theo domain trong case ledger
(`pattern_key_domain`, `/advocacy`) vô nghĩa từ trước tới giờ, không ai phát hiện vì không có lỗi
runtime rõ ràng.

**3. [Trung] CLAUDE.md ghi sai tenant trong `OMNI_LAB_AUTO_EXECUTE_AGENTS`.** Deployment
`omni-fullstack` thật đang chạy `loyalty-uat_cust-app,loyalty-uat_cust-db,loyalty-uat_cust-edge`;
CLAUDE.md (mục Kill-switch) vẫn ghi `staging-sim_cust-app,staging-sim_cust-edge,staging-sim_cust-db`
— lỗi thời, cần sửa CLAUDE.md (không phải sửa cluster).

**4. [Trung] ClusterRole `omni-executor-mutate-lab` tự gắn nhãn "lab-only" nhưng bind trên cluster
production.** `kubectl get clusterrole omni-executor-mutate-lab -o yaml`: annotation
`omni.io/note: Lab-only. Do not bind in prod.`, label `omni.io/env: lab`. Nhưng
`ClusterRoleBinding omni-fullstack-executor-mutate-lab` đang bind SA `omni-fullstack` trên chính
cluster GCP mà CLAUDE.md gọi là Core/production. Nhãn RBAC gây hiểu lầm khi audit bảo mật — cần
hoặc đổi nhãn cho khớp thực tế, hoặc xác nhận lại phạm vi dùng thật của ClusterRole này.

**5. [Đã biết, cập nhật]** SIEM: `omni-siem-chains` vẫn 0 offset, `playbook` vẫn 0 dòng — đúng như
CLAUDE.md đã ghi. Nhưng câu "case_ledger chưa mở ca cho domain này" trong CLAUDE.md (mục domain
`security`) đã lỗi thời — `case_ledger` đã có 305 dòng, chỉ là cột `domain` bị hỏng (xem #2), không
phải chưa mở ca.

**Thấp, chỉ xác nhận lại, không cần hành động ngay:** `ui/` root cũ (~25 route Next) vẫn còn trong
source nhưng không CI/Makefile nào chạm tới — dead code, không gây drift runtime. Kafka 12/14 topic
có consumer sống, 2 topic còn lại (audit log) ghi-only theo đúng thiết kế. Topology Deployment khớp
hoàn toàn CLAUDE.md, không Deployment lạ/CrashLoop.

**Next step đề xuất (ưu tiên theo thứ tự):** #1 (khôi phục observability — cần giải CPU
oversubscription tận gốc, không chỉ scale-0-tạm) → #2 (sửa `open_advisory_case` dùng đúng
`detect_domain()`/domain thật thay vì `proof_lane`, cân nhắc backfill 305 dòng cũ hoặc chấp nhận
mất dữ liệu lịch sử) → #3/#4 (sửa tài liệu, không cần thay đổi hạ tầng).

### Đ61 — Fix Telegram callback (nút Đúng/Sai HITL) không được nhận (2026-08-13)

**Triệu chứng:** user bấm nút Đúng/Sai trên card Telegram nhưng hệ thống không ghi nhận/cập nhật
quyết định — đây là đường agent xin quyền thực thi (HITL approve/reject).

**Nguyên nhân gốc rễ (xác nhận, không suy đoán):** `telegram_loop` (`src/workers/omni_worker.py:1123`,
vòng polling `getUpdates` nhận `callback_query`) không được đăng ký trong `_worker_background_tasks`
vì `OMNI_TELEGRAM_POLLING_ENABLED=false` trong ConfigMap `omni-worker-config` (namespace
`multi-agent`, file `k8s/deployments/omni-worker-configmap.gcp.yaml`). **Ghi chú CLAUDE.md cũ**
("Deployment env override thành true") **đã SAI/lỗi thời** — kiểm tra trực tiếp Deployment
`omni-fullstack` không có override nào, giá trị hiệu lực thật là `false` từ ConfigMap — cần sửa lại
đoạn đó trong CLAUDE.md. Bằng chứng thực nghiệm: bảng `omni_admin.hitl_decision` có 4 dòng kẹt
`PENDING` từ 2026-08-10/11, `actor=NULL`, chưa từng update. Code xử lý callback
(`src/workers/hitl_telegram.py::handle_hitl_callback`) hoàn toàn đúng logic (ghi CRAT, dispatch
Kafka, `record_hitl_decision`, xoá Redis pending, `answer_callback_query`), và `callback_data`
(`hitl:{decision}:{pending_id}`) khớp đúng giữa nơi tạo card và nơi parse — chỉ đứt ở chỗ vòng lặp
cha chưa từng chạy.

**Fix:** đổi `OMNI_TELEGRAM_POLLING_ENABLED` → `"true"` trong
`k8s/deployments/omni-worker-configmap.gcp.yaml`, commit+push cả `gitea` và `origin`
(`3709fa2`). Gotcha gặp phải: `kubectl apply` tay bị ArgoCD self-heal ghi đè lại (Application
`omni-core`, source `k8s/gitops` bao gồm đúng file này trong `directory.include` glob) — phải
push git trước rồi mới patch-sync ArgoCD thì giá trị mới mới giữ được. Rollout restart
`omni-fullstack` 2 lần bị `FailedScheduling: Insufficient cpu` (vấn đề CPU-oversubscription đã
biết ở Đ47/Đ59-CPU) — xử lý tạm bằng `kubectl scale grafana,loki,mimir,tempo -n monitor
--replicas=0` trong lúc pod mới lên lịch, rồi scale lại `1` ngay sau. **Verify sống:** env pod
`OMNI_TELEGRAM_POLLING_ENABLED=true`; gọi `getWebhookInfo` qua Bot API xác nhận `url=""` (đúng cơ
chế polling, không phải webhook) và `pending_update_count=0` — hàng đợi rỗng vì chính pod đang chủ
động tiêu thụ qua `getUpdates`, bằng chứng gián tiếp nhưng chắc chắn rằng loop đang chạy (nếu không
có ai poll, hàng đợi sẽ tích tụ).

**Chưa làm:** chưa có bằng chứng bấm nút thật (cần user tự bấm 1 nút trên Telegram để confirm
`hitl_decision` chuyển từ PENDING sang APPROVED/REJECTED thật) — nên coi đây là fix đã verify ở
tầng cơ chế (polling đang chạy), chưa verify ở tầng "nút bấm → DB update" đầu-cuối bằng người dùng
thật. 4 dòng `hitl_decision` PENDING cũ (2026-08-10/11) coi như đã hết hạn (Redis pending TTL
7200s từ lâu) — nếu còn card cũ trên Telegram, bấm nút sẽ không map lại được `action_body`, cần
escalate lại thủ công nếu vẫn cần.

### Đ60 — Re-index RAG (768→1024) + verify full luồng chẩn đoán qua gateway thật (2026-08-13)

**Re-index RAG: DONE, không mất dữ liệu.** 5 collection có dữ liệu thật đã re-embed qua NIM
(`nvidia/nv-embedqa-e5-v5`) và tạo lại HNSW index đúng DIM 1024: `diagnostic_history` (1094/1094),
`infra_topology` (205/205), `itops_error_ledger` (4/4), `action_experience` (15/15),
`action_experience:loyalty-uat` (5/5) — tổng 1323 doc, 0 thất bại. Script:
`/tmp/reindex_nim.py` + `/tmp/fix_action_experience.py` chạy trực tiếp trong pod
`omni-fullstack` (không commit vào repo — one-off, xoá theo pod restart tự nhiên). Lưu ý phát hiện
giữa chừng: 2 collection `action_experience*` có `text_content` rỗng (chỉ " ") cho các entry
`memory_kind=playbook` — nội dung thật nằm trong `omni_payload.symptom_text`/`lesson`, phải đọc
fallback từ đó mới embed đúng (đã xử lý, không phải bug ở NIM/re-index, là cách ghi dữ liệu cũ).
10 collection còn lại trong `FT._LIST` đều `num_docs=0` (chưa từng có dữ liệu, kể cả
`itops_sop_ledger` mà tài liệu cũ nhắc "1019 SOP entries" — con số đó là của môi trường/thời điểm
khác, KHÔNG áp dụng cho cluster GCP hiện tại, đã xác nhận trực tiếp bằng `FT.INFO`).

**Verify full luồng thật qua `/simulate/scenario/service` (gateway thật, Kafka thật, KHÔNG mock)**
— vì `orb` (OrbStack CLI) không có trên VM GCP này nên không tự chạy được `scripts/diag-test-vm.sh`
(cần VM lab trên MacBook user); dùng route `/simulate/scenario/{scenario}` thay thế — chính route
này CŨNG là production code thật (đẩy đúng Kafka topic `omni-diagnostic-evidence`, đúng envelope
`remote_agent`), không phải mock riêng cho test.

- **Lần 1 (sự cố mới, agent_id=e2e-test-cust-edge, tenant=e2e-test)**: trace `sim-service-
  fb1cbed1cf03`. Pipeline thật: `INGEST→EVIDENCE→RAG(skip, no_hit)→LLM(multi-turn diagnosis loop,
  2 lượt, cả 2 lượt gọi NIM thật outcome=ok)→SCHEMA(session stored)→CRAT(audit block written)→
  DISPATCH(Telegram)`. Xác nhận LLM ReAct loop chạy thật qua NIM, không phải qua Ollama cũ.
- **Ghi bài học (`action_experience`)**: xác nhận cơ chế thật `_upsert_action_experience()`
  (`workers/remote_command_outcome_loop.py:130`) chỉ chạy sau khi có OUTCOME THẬT từ agent qua
  lệnh dispatch — agent giả (`e2e-test-cust-edge`) không tồn tại nên không có outcome thật để ghi
  tự động. Để verify cơ chế RAG-recall mà không cần VM thật, đã **seed tay** 1 bản ghi đúng schema
  thật (`memory_kind=playbook`, `exec_outcome=success`, `auto_execute=true`,
  `tool=systemd.restart_unit`) + 1 discovery snapshot giả (services=[nginx]) cho agent test — script
  `/tmp/seed_known_fix.py`, đã xoá dọn sau khi test xong (tenant `e2e-test` cô lập, không đụng dữ
  liệu tenant thật).
- **Lần 2 (cùng sự cố, cùng agent)**: trace `sim-service-bd5fea65adb0`. **RAG stage: `ok
  recall=0.752 route=KNOWN_WITH_FIX`** — xác nhận re-index hoạt động đúng, nhận ra đây là sự cố đã
  biết. Nhưng **LLM stage vẫn chạy đầy đủ (không skip)** — điều tra trực tiếp bằng cách gọi thẳng
  `pkg.reasoning.known_fix_resolver.find_known_fix_candidate()` với đúng tham số production dùng:
  **tìm thấy candidate đúng, score=0.78, qua hết 2 lớp kiểm (placeholder + host_scope)** — nghĩa là
  tầng RAG-match của cơ chế reflex hoạt động đúng 100%. Lý do KHÔNG auto-dispatch trong lần chạy
  live: `auto_recovery_bridge.dispatch_if_eligible()` chặn ở allowlist
  `OMNI_LAB_AUTO_EXECUTE_AGENTS` (chỉ đúng 3 agent thật:
  `loyalty-uat_cust-app/cust-db/cust-edge`) — agent giả `e2e-test-cust-edge` không nằm trong danh
  sách nên bị từ chối dispatch, **ĐÚNG THIẾT KẾ** (chặn blast-radius cho agent không xác thực),
  không phải bug. Hệ quả: pipeline fallback về LLM đầy đủ thay vì auto-execute mù — hành vi AN
  TOÀN, đúng ý đồ kiến trúc.

**Kết luận verify:** cả 2 nửa của luồng "sự cố→chẩn đoán→RAG→tái dùng" đã CHỨNG MINH hoạt động
đúng bằng dữ liệu thật (không đoán): (1) LLM ReAct loop qua NIM thật, (2) RAG tìm đúng bài học đã
biết với điểm số hợp lý sau re-index. Phần DUY NHẤT chưa demo được end-to-end là bước "auto-execute
mà không hỏi LLM" cho một sự cố LẶP LẠI trên **agent thật** (không phải agent giả) — cần 1 trong 3
VM lab thật (`orb -m cust-edge/cust-app/cust-db` trên MacBook user, hoặc agent GCP tương ứng) để
demo trọn vẹn, vì allowlist cố ý chặn agent không xác thực — đây là rào an toàn, không sửa.

### Next step (Đ60)

1. Nếu muốn demo trọn vẹn bước auto-execute cuối: chạy `bash scripts/diag-test-vm.sh cust-edge
   nginx 90` (hoặc target tương đương) HAI LẦN trên máy có `orb` (MacBook user) — lần 2 sẽ tự
   động qua reflex nếu agent đó có mặt trong `OMNI_LAB_AUTO_EXECUTE_AGENTS` (đã xác nhận cả 3 agent
   `loyalty-uat_cust-*` đều có).
2. Các next step Đ59 (resize VM cho CPU, GitHub) — xem mục Đ59 gốc bên dưới, không đổi.

---

### Đ59 — Rollback k3s từ backup + chuyển LLM sang NVIDIA NIM (2026-08-13)

**Bối cảnh:** Đ57/Đ58 (xem bên dưới) đã backup toàn bộ cluster rồi xoá 9 namespace app để tạm
dừng dự án. User quay lại, yêu cầu khôi phục toàn bộ, sau đó báo Omni lỗi vì MacBook (host Ollama)
đã tắt — yêu cầu chuyển sang NVIDIA NIM.

**1. Rollback k3s (đã DONE, verify sống):** Chọn phương án rollback toàn bộ thay vì khôi phục chọn
lọc (user tự chọn, chấp nhận mất ~15h dữ liệu monitor/cert-manager phát sinh sau thời điểm backup):
`systemctl stop k3s` → move aside `state.db` + `storage/` hiện tại (giữ lại, không xoá, hậu tố
`.pre-restore-<ts>`) → ghi đè bằng `sqlite/state.db` + giải nén `storage/local-path-storage.tar.gz`
từ `~/k3s-backup-20260812-082206.tar.gz` → `systemctl start k3s`. Kết quả: cả 15 namespace (9 app
+ monitor + 4 core) Active trở lại, pod lần lượt Running (churn ContainerCreating/PodInitializing
bình thường khi tất cả pod cùng khởi động lại 1 lượt). Archive backup vẫn còn nguyên trên VM, CHƯA
copy ra ngoài — vẫn là việc tồn đọng (xem Next step Đ57 cũ, chưa đổi).

**2. Chuyển LLM Omni sang NVIDIA NIM (đã code + config + Vault, ĐANG deploy):**
- Root cause lỗi: `OMNI_VLLM_BASE_URL`/`OMNI_OLLAMA_BASE_URL` trỏ Tailscale IP MacBook
  (`100.93.3.96:11434`) — MacBook tắt nên toàn bộ chat/embeddings đứng.
- `src/llm/vllm_client.py`: thêm field `provider`/`api_key`/`rate_limit_rpm`/`embed_extra_body`
  vào `VLLMClient`. `_chat_ollama_native` (route `think=False` sang `/api/chat` thô) giờ CHỈ chạy
  khi `provider=="ollama"` — NIM không có endpoint này. Thêm `_RateLimiter` (sliding-window 60s,
  giữ trong `asyncio.Lock`) dùng chung cho cả chat lẫn embed, chặn client-side trước khi đụng rate
  limit free-tier NIM.
- `src/llm/factory.py`: đọc `OMNI_LLM_PROVIDER`/`OMNI_NIM_API_KEY`/`OMNI_NIM_RATE_LIMIT_RPM` từ
  env, mọi call site `build_llm_client()` hiện có tự động ăn theo, không cần sửa từng nơi gọi.
- `src/pkg/rag/ollama_embed.py` (helper gateway-safe, urllib thuần, không import openai/workers):
  thêm nhánh NIM riêng (`_embed_sync_nim`, OpenAI-shape response `data[0].embedding`, khác hẳn
  Ollama `{embeddings:[[...]]}`), `EMBED_DIM` đổi thành đọc `OMNI_EMBED_DIM` env thay vì hardcode
  768.
- **EMBED_DIM 768→1024 (nomic-embed-text → nvidia/nv-embedqa-e5-v5) — ĐỒNG BỘ 3 nơi từng hardcode
  riêng lẻ**, giờ đều đọc `OMNI_EMBED_DIM`: `src/rag/redis_vector_store.py` (nguồn chính, dùng cho
  HNSW `FT.CREATE ... DIM`), `src/pkg/clustering/incident_cluster.py` (`_EMBED_DIM`), `src/gateway/
  routes/kb.py` (`EMBED_DIM`). `pgvector_store.py` chỉ re-export từ `redis_vector_store` nên tự
  động ăn theo.
- **Model ID đã verify SỐNG bằng curl thật với đúng API key trước khi ghi vào ConfigMap** (không
  đoán): `meta/llama-3.1-8b-instruct` (200 OK, ~0.4s) → slot tần suất cao (chat mặc định/helper/
  diag-evidence); `meta/llama-3.1-70b-instruct` (200 OK, ~16s) → slot ít gọi hơn (reasoning
  engine/autonomous decider), tách 2 model để không đụng rate limit 40rpm free-tier. ĐÃ THỬ VÀ
  LOẠI: `qwen/qwen2.5-7b-instruct` (404 dù có trang doc — không có trên hosted endpoint),
  `meta/llama-3.3-70b-instruct` (timeout 30s), `nvidia/llama-3.1-nemotron-70b-instruct` (404 "not
  found for account"), `mistralai/mixtral-8x22b-instruct-v0.1` (410 Gone, EOL 2026-05-21). Embed:
  `nvidia/nv-embedqa-e5-v5` verify sống (200 OK, đúng response shape).
- **Secret qua Vault, không plaintext git** (user dán key thẳng vào chat — KHÔNG đưa vào bất kỳ
  file git nào): Vault đã unseal thủ công (`vault-unseal-bootstrap` Secret trong ns `vault`, đúng
  cronjob `vault-auto-unseal` */2min đang dùng — cronjob tự chạy được lần kế tiếp, không cần làm
  tay nữa cho các lần restart sau). Ghi `vault kv put secret/omni-nim-secret api_key=...` bằng
  root token cũng lấy từ `vault-unseal-bootstrap`. `k8s/gitops/omni-nim-external-secret.yaml` (mới)
  — ExternalSecret sync `secret/omni-nim-secret` → K8s Secret `omni-nim-secret` (cùng pattern
  `aoip-dex-secret.yaml`). `omni-fullstack.yaml` thêm `OMNI_NIM_API_KEY` qua `secretKeyRef`
  (`optional: true` — chỉ để pod không crash-loop nếu ExternalSecret chưa sync kịp lúc boot, KHÔNG
  phải fail-open khi gọi LLM: thiếu key thì openai SDK tự trả lỗi auth rõ ràng lúc gọi).
- `k8s/deployments/omni-worker-configmap.gcp.yaml`: `OMNI_LLM_PROVIDER=nim` (công tắc thật, đọc ở
  `factory.py`) + toàn bộ `OMNI_VLLM_BASE_URL`/`OMNI_OLLAMA_BASE_URL`/`VLLM_BASE_URL` đổi sang
  `https://integrate.api.nvidia.com/v1`, `OMNI_VLLM_STREAM_FOR_SLI` tắt (true→false, để giảm chi
  phí/độ phức tạp gọi hosted API rate-limited), 6 field model đổi theo 2 model đã verify ở trên,
  `OMNI_EMBED_MODEL=nvidia/nv-embedqa-e5-v5`, `OMNI_EMBED_DIM=1024`.

**⚠️ CHƯA XONG — rủi ro thật, đọc trước khi coi Đ59 là DONE:** HNSW index trong Redis
(`idx:itops_sop_ledger` + các collection khác, ~1019 SOP entry) được tạo với `DIM 768` từ trước.
`_ensure_index()` trong `redis_vector_store.py` là create-if-not-exists (`ft(idx).info()` thành
công → tái dùng nguyên trạng) — đổi `OMNI_EMBED_DIM=1024` KHÔNG tự động migrate index cũ. Sau khi
deploy, vector NIM 1024-dim ghi/query vào index 768-dim cũ sẽ lỗi ở tầng RediSearch (kích thước
blob không khớp DIM khai báo) — lỗi to, không phải im lặng, nhưng RAG (SOP/k8s_expert/action_
experience/...) sẽ KHÔNG hoạt động cho tới khi có người chủ động: (1) `FT.DROPINDEX` từng index
cũ, để `_ensure_index()` tạo lại đúng DIM 1024 ở lần gọi kế tiếp, VÀ (2) re-embed lại toàn bộ nội
dung cũ (chạy lại `training/sop_ingest.py` và các script ingest tương ứng qua NIM) — việc này tốn
thời gian thật vì bị giới hạn 40rpm (~1019 entry / 40 ≈ 26 phút tối thiểu, serialize qua
`_RateLimiter`). CHƯA làm bước này trong phiên — cố ý chưa làm vì đây là thao tác gần như phá huỷ
dữ liệu RAG hiện có nếu làm vội, cần user xác nhận trước khi drop index thật.

**Cập nhật thêm cùng phiên (sau khi viết đoạn trên) — 3 sự cố thật gặp khi build+deploy, đều đã xử
lý, build 69 đang chạy khi ghi dòng này:**

1. **CoreDNS watch stale sau rollback sqlite** — build 65/66 fail ở bước `git fetch` trong Jenkins:
   `Could not resolve host: gitea.cicd.svc.cluster.local`. Root cause xác nhận bằng debug pod
   (`nslookup` → NXDOMAIN dù Service `gitea` tồn tại thật trong `kubectl get svc`): CoreDNS pod
   sống sót qua thao tác rollback datastore ở mục 1 nhưng watch cũ của nó với apiserver không tự
   phục hồi đúng. Fix: `kubectl rollout restart deployment/coredns -n kube-system` — verify lại
   bằng debug pod, resolve đúng ngay. Bài học: sau bất kỳ lần rollback/swap sqlite datastore nào,
   restart CoreDNS luôn, đừng đợi lỗi mới phát hiện.

2. **CPU capacity thật sự thiếu trên node 4-core** — `omni-k3s-vm` chỉ có 4 CPU allocatable. Sau
   khi rollback đưa cả 15 namespace + Jenkins (giờ chạy in-cluster, permanent resident 500m, KHÔNG
   có trong baseline cũ trước Đ47) sống cùng lúc, node request thường trực ~96-98% (dù usage thực
   tế chỉ ~25%) — bất kỳ pod mới nào cần schedule (Jenkins pod tự nó, rồi tới `omni-fullstack` sau
   rollout, tính cả istio-proxy sidecar 100m nó tự inject = tổng 600m chứ không phải 500m như
   trong resources.requests khai báo) đều bị `FailedScheduling: Insufficient cpu`. Xử lý tạm thời
   (làm 2 lần, mỗi lần cho một pod cần lên): `kubectl scale` namespace `monitor`
   (prometheus/grafana/tempo/mimir/loki) xuống `replicas=0` vài phút, xoá pod Pending để buộc
   scheduler thử lại ngay (không tự retry kịp dù có headroom), rồi scale monitor lại `replicas=1`
   ngay sau khi pod cần thiết đã schedule xong. **Đây là workaround, không phải fix** — nếu không
   có ai chủ động làm vậy, việc restart/redeploy BẤT KỲ pod nào trên node 4-core này (không riêng
   omni-fullstack) có nguy cơ treo ở Pending. Khuyến nghị thật sự: tăng CPU của VM GCP hoặc giảm
   `resources.requests` một số Deployment không cần 100-500m thật (nhiều chỗ đang xin nhiều hơn
   usage thực đo được), CHƯA làm — cần quyết định của user vì đụng tới cost/kiến trúc GCP VM.

3. **Bug CI/CD nghiêm trọng: Docker layer cache tái dùng COPY src/ dù code đã đổi** — phát hiện
   sau khi build 68 "thành công" đẩy tag `bb33dd1` (đúng) nhưng pod chạy image đó vẫn 401 khi gọi
   NIM dù `OMNI_NIM_API_KEY`/`OMNI_LLM_PROVIDER` env đúng 100% (verify tay bằng `printenv` +
   `curl` cùng key ngoài container → 200 OK). `kubectl exec ... cat /app/src/llm/factory.py` lộ ra
   file trong image KHÔNG có code NIM (`provider`/`api_key`) — vẫn là bản `243a139`, cũ hơn 2
   commit. Log build 68 xác nhận: `Step 9/18 : COPY src/ /app/src/` → `Using cache` dù nội dung
   `src/` thật sự đổi. Đây là legacy Docker builder (non-BuildKit, có warning deprecated trong
   log) tái dùng cache sai. **Fix đã commit** (`Dockerfile` + `Jenkinsfile`, chưa verify build
   69 xong lúc ghi dòng này): thêm `ARG GIT_COMMIT` + `RUN echo "$GIT_COMMIT" > /tmp/.git_commit`
   ngay trước `COPY src/`, Jenkinsfile truyền `--build-arg GIT_COMMIT=$IMAGE_TAG` — mọi commit mới
   giờ chắc chắn invalidate cache từ layer đó trở đi. **Bài học nghiêm trọng cho việc tin tưởng CI
   trong tương lai**: tag image = git commit SHA KHÔNG đủ để đảm bảo code đúng đã deploy nếu build
   dùng legacy Docker builder — phải verify bằng cách đọc trực tiếp file trong container
   (`kubectl exec ... cat`), không chỉ tin `docker build` log "Successfully built" + tag khớp.

4. **GitOps tag-bump push bị non-fast-forward 2 lần** — cả hai lần đều do CHÍNH session này push
   thêm commit (pip-cache fix, cache-bust fix) TRONG LÚC một Jenkins build khác đang chạy dựa trên
   commit cũ hơn. Bài học: khi một build `omni-gcp-deploy` đang chạy, KHÔNG push thêm gì lên
   `gitea main` cho tới khi build đó kết thúc (thành công hay fail) — nếu fail, build tự động
   revert commit tag-bump của chính nó (post{failure{}} trong Jenkinsfile), an toàn để rebase lên
   trên rồi push tiếp.

**GitHub (`origin`) vẫn CHƯA có các commit của phiên này** — máy VM này không có SSH private key
cho `git@github.com` (`~/.ssh/` chỉ có `authorized_keys`, không có identity file/agent) — đây là
gap môi trường có sẵn, không phải lỗi phát sinh trong phiên. Cần user tự `git push origin main`
từ máy có key, hoặc cấp key/token cho VM này nếu muốn tự động hoá đủ 2 remote từ đây.

**KẾT QUẢ CUỐI (build 69, SUCCESS):** verify sống bằng `kubectl exec omni-fullstack-668fb764bd-kwdqt
... cat /app/src/llm/factory.py` — code NIM (`provider`/`api_key`/rate-limiter) có mặt thật trong
image `bae5c68` đang chạy; log pod có dòng `event=llm_call ... outcome=ok ... endpoint=/v1/chat/
completions` (không còn 401). `omni-fullstack` 2/2 Running, `omni-gateway` rollout Healthy 1 pod
(canary 5/5 bước, đã `promote` tay để không kẹt ở step pause). ArgoCD app `omni-core`: Synced/
Healthy.

**Nợ lại chưa xử lý, cố ý không đụng thêm để tránh vòng lặp workaround vô hạn:** phát hiện quan
trọng cuối phiên — **istio-proxy sidecar (native sidecar initContainer, KHÔNG nằm trong
`spec.containers` nên rất dễ bị bỏ sót khi tự tính tổng CPU) cộng dồn ~1300m CPU request trên
toàn cluster** (13 pod có sidecar × 100m) — đây là phần CHÍNH giải thích vì sao node 4-core cứ
liên tục báo `Insufficient cpu` mỗi khi có pod mới cần schedule suốt phiên này (Jenkins pod, rồi
`omni-fullstack` rollout, rồi `monitor` stack sau khi scale lại) dù `kubectl top nodes` luôn cho
thấy usage thực tế chỉ ~25%. Đã tạm thời scale `monitor` (prometheus/grafana/tempo/mimir/loki)
xuống 0 rồi lại 1 hai lần trong phiên để nhường chỗ — sau lần cuối, `prometheus` + toàn bộ
DaemonSet nhỏ (`alertmanager`/`node-exporter`/`promtail`/`kube-state-metrics`) đã Running, nhưng
**`grafana`/`loki`/`mimir`/`tempo` vẫn đang Pending** (thiếu ~355m so với headroom thật). KHÔNG ép
buộc schedule thêm bằng cách hạ istiod/omni-fullstack/jenkins — 3 cái đó cần chạy thường trực.
**Khuyến nghị thật cho user**: tăng CPU của `omni-k3s-vm` (GCP instance resize) là fix đúng gốc —
việc scale monitor lên/xuống tay chỉ là băng bó tạm thời, sẽ tái diễn ở MỌI lần deploy/restart pod
sau này trên node 4-core hiện tại.

### Next step (Đ59)

1. **Ưu tiên cao nhất còn lại**: `grafana`/`loki`/`mimir`/`tempo` (namespace `monitor`) đang
   Pending vì thiếu CPU — hỏi user có muốn resize VM GCP không (fix gốc), hoặc nếu chưa, có thể
   tạm chấp nhận monitor thiếu 4 component này (prometheus/alertmanager vẫn sống, core dashboard/
   log/trace tạm mất) cho tới khi resize.
2. Hỏi user có muốn tiến hành FT.DROPINDEX + re-embed RAG ngay (mất ~30 phút+, tốn budget rate
   limit NIM free-tier) hay để RAG tạm "lỗi to nhưng an toàn" tới khi có quyết định — ĐỪNG tự ý
   drop index.
3. `git push origin main` (GitHub) đang thiếu các commit của phiên này — cần user tự làm từ máy
   có SSH key, hoặc cấp key cho VM.
4. Backup archive Đ57 vẫn còn nguyên trên VM, vẫn CHƯA copy ra ngoài — nếu sau này quay lại ý định
   xoá VM, phải làm bước này trước (xem Next step Đ57 gốc, không đổi).

---

### Đ57 — Backup toàn bộ k3s trước khi xoá hệ thống (2026-08-12)

**Bối cảnh:** user báo tạm dừng dự án Omni để chuyển sang dự án khác, sẽ xoá toàn bộ hạ tầng
(`omni-k3s-vm` trên GCP). Yêu cầu: tạo 1 file backup toàn bộ hệ thống k3s trước khi xoá.

**Đã làm:**
- File mới `scripts/backup/k3s_full_backup.sh` (đã `chmod +x`, CHƯA commit) — backup toàn diện,
  không chỉ export YAML: (1) `kubectl get -o yaml` mọi resource namespaced + cluster-scoped ở tất
  cả namespace (gồm Secrets); (2) live copy sqlite datastore của k3s
  (`/var/lib/rancher/k3s/server/db/state.db`, qua `python3 sqlite3.Connection.backup()` vì máy
  không có sqlite3 CLI, chỉ có lib); (3) `/etc/rancher/k3s` (config/token/TLS); (4) tar toàn bộ
  `/var/lib/rancher/k3s/storage` (~13G, local-path PV data — đây là chỗ chứa Postgres/Vault file
  storage/Vaultwarden sqlite/Harbor registry blobs+db/Gitea repos+db/Jenkins home/
  Grafana-Loki-Mimir-Prometheus-Tempo, gộp hết vào 1 bước thay vì dump từng app); (5) `pg_dumpall`
  logic của `omni_admin` (bổ sung, không thay cho raw PGDATA copy ở trên); (6) `helm list -A` +
  values từng release. Output nén `tar.gz` + `.sha256` tại `$HOME/k3s-backup-<timestamp>.tar.gz`.
- Đã chạy thật (`sudo -n` hoạt động, không cần mật khẩu). Chạy nền vì bước tar 13G lâu — xem output
  tại `Bash` background task id `brf1ckqgd` (`/tmp/claude-*/tasks/brf1ckqgd.output`) khi session
  tiếp tục nếu bị ngắt giữa chừng.

**Đã xong (2026-08-12, cùng phiên):** archive backup hoàn tất, xác minh toàn vẹn —
`/home/hiendang/k3s-backup-20260812-082206.tar.gz` (6.7GB), sha256 kèm theo (`.sha256`). Đã kiểm
tra thật (giải nén lại, không chỉ tin log): gzip toàn vẹn OK; `storage/local-path-storage.tar.gz`
(7.1GB, PV data thật của Postgres/Vault/Vaultwarden/Harbor/Gitea/Jenkins/monitoring stack) có mặt
đầy đủ dù log script báo "WARNING: PV storage tar failed" — xác nhận đó là false alarm (tar exit
non-zero vì file DB đổi lúc đang đọc, archive vẫn nguyên vẹn); Secret đã giải mã base64 có mặt (vd
12 secret ở `multi-agent`, 12 ở `vault`); ArgoCD Applications + ExternalSecrets đã resolve template
runtime thật có mặt; `/etc/rancher/k3s/k3s.yaml` (kubeconfig+CA) có mặt. Gap thật duy nhất:
`pg-dump/omni_admin_all.sql` = 0 byte (pg_dumpall lỗi auth/exec, KHÔNG mất dữ liệu vì PGDATA thô
đã có trong `storage/`, chỉ là không có bản dump SQL logic dễ đọc).

**QUAN TRỌNG — CHƯA XONG, đừng xoá VM tới khi việc này xác nhận DONE:** archive backup hiện CHỈ
nằm trên chính `omni-k3s-vm` sắp xoá — nếu xoá VM trước khi copy archive ra ngoài, backup mất theo,
vô nghĩa. Đã hỏi user chọn phương án (`gsutil` lên GCS bucket / `scp` về máy / chưa quyết) —
**user chọn "Chưa xoá VM ngay"**, tức là dừng ở bước này, CHƯA chọn phương án copy, CHƯA cho phép
xoá bất cứ gì. Không tự ý chọn phương án hay tiến hành xoá VM ở phiên sau nếu chưa hỏi lại user.

**Không nằm trong backup này (cân nhắc thêm nếu cần trước khi xoá hẳn):**
- `docs/handoffs/GCP_CREDENTIALS_2026-08-04.md` — file local, không commit git, không tự động vào
  tarball trừ khi copy tay (nó nằm trong repo dir nên `git`/repo backup riêng sẽ không thấy nó vì
  gitignore, nhưng nó VẪN nằm trên đĩa VM — cần backup riêng nếu muốn giữ).
- Vault unseal key / root token — không rõ có ghi ở đâu ngoài trí nhớ user; nếu mất, dữ liệu Vault
  trong `storage/` tar ở trên (Seal Type shamir, Storage Type file) sẽ không unseal được dù còn
  file. Cần hỏi user đã lưu unseal key ở nơi khác chưa.
- Code repo (`/home/hiendang/project`) — đã có 2 remote (`gitea` nội bộ trong chính cluster sắp
  xoá, và `origin` GitHub độc lập) — GitHub còn sống sau khi xoá VM nên code KHÔNG mất, chỉ cần
  đảm bảo mọi commit đã push `origin` trước khi xoá (xem quy tắc 2-remote trong `CLAUDE.md`).

### Next step

1. Kiểm tra output nền của `k3s_full_backup.sh` (task `brf1ckqgd`) đã DONE chưa, xem MANIFEST +
   kích thước archive cuối cùng.
2. Hỏi user: copy archive ra khỏi VM bằng cách nào (GCS bucket có sẵn? scp về máy?) — PHẢI làm
   bước này trước khi cho phép xoá VM.
3. Hỏi user về Vault unseal key / root token đã lưu ở đâu ngoài VM chưa.
4. Xác nhận `git push origin main` (GitHub) đã có đủ mọi commit trước khi xoá VM (kể cả 4 file Gate
   0 Đ56 nếu user muốn commit trước khi dừng).
5. Sau khi backup xác nhận an toàn ở ngoài VM, việc xoá hệ thống là hành động phá huỷ — PHẢI hỏi
   xác nhận rõ ràng trước khi thực hiện bất kỳ lệnh xoá nào (`terraform destroy`, `gcloud compute
   instances delete`, v.v.), theo đúng Git/Infra Safety Protocol.

---

### Đ56 — Gate 0: agent hardening non-root, code xong, cutover VM bị chặn (2026-08-12)

**Bối cảnh:** phiên `/remote-control` mới trên VM GCP, brainstorm nợ kỹ thuật → quyết định vá
Gate 0 trước (audit `SRE_READINESS_2026-08.md` F-005/B7: agent thật `aoip-agent.service` chạy
root, không hardening — một bug validator tương lai = toàn quyền root trên máy khách). Kế hoạch
đầy đủ đã duyệt, lưu tại `/root/.claude/plans/jiggly-weaving-kazoo.md` trên VM này (KHÔNG nằm
trong git repo, chỉ local).

**Phát hiện quan trọng lúc điều tra:** bug PoC RCE root 3/3 (awk, 2026-07-31) mà audit
`SRE_READINESS_2026-08.md` trích dẫn **đã được vá từ trước** (commit `1ca82da`, cùng đêm phát
hiện) — audit dùng nó làm bằng chứng lịch sử cho luận điểm kiến trúc (phòng thủ đơn lớp), không
phải lỗ hổng còn sống. Gate 0 thật sự cần là lớp OS thứ 2 (non-root + `ProtectSystem=strict`), để
MỘT bug validator tương lai không tự động là root.

**Đã làm xong (local, an toàn, chưa đụng VM lab, 4 file, CHƯA COMMIT):**
1. `src/aoip/agent/updater.py:150` — thêm `sudo` vào `_default_restart()` (self-restart sau
   update sẽ fail permission-denied khi agent chạy non-root nếu thiếu). Kèm test mới
   `TestDefaultRestart` trong `tests/test_aoip_agent_updater.py`.
2. `scripts/omni-agent.sudoers` (file mới) — sudoers drop-in, đúng 5 lệnh NOPASSWD scope hẹp,
   khớp `config/aoip_agent_gate.env` thật (`AOIP_GATE_ALLOWED_FAILURE_MODES=process_down,
   failed_state_stale,disk_pressure_journal` — KHÔNG cấp sẵn kill/config-rollback vì 2 failure
   mode đó chưa bật).
3. `scripts/aoip-agent.service` — cập nhật tại chỗ thành bản hardened: `User=omni-agent`,
   `ProtectSystem=strict`, `ReadWritePaths` thu hẹp, **KHÔNG có `NoNewPrivileges=true`** (phát
   hiện quan trọng: nó vô hiệu hoá setuid của `sudo`, sẽ làm toàn bộ recovery/mutate/self-restart
   fail permission-denied âm thầm — ghi rõ lý do bằng comment trong unit).
4. `scripts/aoip-agent-harden-migrate.sh` (file mới) — script migrate idempotent, KHÔNG dùng
   `scripts/omni-agent-install.sh` làm nền (đã thử, phát hiện đó là công cụ fresh-install cho
   `remote_agent.agent` độc lập — venv/config/registry-check của nó sẽ phá cây thư mục agent
   đang chạy thật nếu áp dụng cho migrate). Script chỉ làm: tạo user, chown, cài sudoers, cài
   unit + `daemon-reload` — KHÔNG tự restart (restart/verify/drill/soak để làm riêng, có kiểm
   chứng từng bước, không gộp mù).

**Verify đã chạy:** `.venv/bin/python -m pytest tests/test_aoip_agent_updater.py -q` → 22 passed,
0 fail (venv được tạo mới trên VM này lúc này — VM GCP trước đó CHƯA TỪNG có `.venv`, cũng là một
gap môi trường mới phát hiện, không chặn gì, chỉ ghi nhận).

**BỊ CHẶN CỨNG — Task cutover thật lên `cust-db`/`cust-edge`/`cust-app` (3 VM lab OrbStack):**
VM GCP này (nơi Claude đang chạy) **không có đường kỹ thuật nào tới 3 VM lab**. `orb` CLI không
tồn tại trên VM GCP — OrbStack chỉ chạy trên MacBook. `tailscale status` xác nhận node
`macbook-pro-ca-hiendang` **offline** (last seen 3h trước lúc kiểm). Ngay cả khi MacBook online,
lệnh `orb -m <vm>` bắt buộc chạy TỪ chính MacBook, không remote-exec qua Tailscale được.

**Đang chờ user chọn hướng** (đã hỏi cuối lượt trước, CHƯA có câu trả lời):
1. User tự chạy cutover trên MacBook, theo đúng trình tự trong plan mục 4 (3 file đã có sẵn ở
   `scripts/`: `aoip-agent.service`, `omni-agent.sudoers`, `aoip-agent-harden-migrate.sh`) — dán
   output lại để Claude verify.
2. Bật lại Tailscale trên MacBook rồi thử điều khiển gián tiếp qua `ssh macbook-pro-ca-hiendang orb -m ...`.
3. Tạm dừng ở đây, để phần cutover VM cho phiên sau.

### Next step

1. Khi user trả lời hướng xử lý ở trên → tiếp tục Task #4-6 (cutover `cust-db` → `cust-edge` →
   `cust-app`, thứ tự này vì `cust-app` là host demo cafe, làm sau cùng — xem plan mục 4 cho lệnh
   + tiêu chí verify từng bước) rồi Task #7 (bằng chứng blast-radius `systemd-run` + viết
   `docs/audit/gate0-agent-hardening-verify.md`).
2. Commit 4 file đã sửa/thêm (`src/aoip/agent/updater.py`, `tests/test_aoip_agent_updater.py`,
   `scripts/aoip-agent.service`, `scripts/omni-agent.sudoers`, `scripts/aoip-agent-harden-migrate.sh`)
   — CHƯA commit, đang chờ vì muốn gộp cùng nhịp với cutover thật để tránh commit code chưa từng
   chạy trên VM lab nào (dù test unit đã xanh). Nếu user muốn commit ngay (tách khỏi cutover) —
   được, đã verify bằng test, khớp standing authorization.
3. Plan đầy đủ + toàn bộ lệnh cutover từng bước: `/root/.claude/plans/jiggly-weaving-kazoo.md`
   (chỉ tồn tại LOCAL trên VM GCP này, không sync qua git — nếu VM này mất, plan mất theo, cân
   nhắc chép nội dung vào `docs/audit/` nếu việc cutover kéo dài qua nhiều phiên).

### Đ55 — Script demo live cho buổi cafe với CTO cũ (2026-08-11, ĐÃ ĐÓNG)

**Bối cảnh:** nối tiếp Đ54 — user có đầu mối thật (sếp cũ, vận hành hạ tầng doctorcheck.vn, y tế
VN). User yêu cầu: không dùng slide, cần **script chứng minh được** những gì sẽ nói khi gặp cafe —
mở rộng thêm: phải chứng minh **3 mức độ tự trị** (shadow/assist/auto) với Telegram đúng ở từng
mức, agent **làm được gì/không làm được gì** (cấm ở mức nào), và **thật sự hiểu hệ thống** (không
đoán mù). Hình thức đã chốt: gửi video ngắn trước để đo hứng thú, rồi gặp cafe demo LIVE trên laptop.

**Đã tạo (untracked, chưa commit):**
- `scripts/demo/cafe_demo_preflight.sh` — chạy Ở NHÀ trước khi đi, check 7 điều kiện. Đã chạy
  thật, PASS toàn bộ sau khi fix 1 bug thật: `omni-gateway` là Argo Rollout
  (`kubectl get rollout`), không phải Deployment — script cũ dùng `kubectl get deploy omni-gateway`
  luôn FAIL dù pod thực tế khoẻ mạnh.
- `scripts/demo/cafe_demo_payment_api.sh` — script chạy LIVE trước mặt CTO: dừng thật
  `payment-api.service` trên VM `cust-app` (tenant `loyalty-uat`, agent thật) → tail log thật từ
  pod `omni-fullstack` → chờ tự phục hồi → verify trạng thái thật → cho xem entry audit chain thật
  → dọn dẹp cuối. Bug thật đã bắt qua dry-run (không chỉ đọc code): `wait -n` không chạy được trên
  bash 3.2 mặc định macOS — đã đổi sang `sleep 240` (tương thích bash 3.2).

**Phát hiện quan trọng cho vận hành demo — cooldown 900s (Đ52):** chạy đúng kịch bản 2 lần liên
tiếp trong <15 phút khiến cooldown theo fingerprint chặn hoàn toàn lần 2 — Telegram im lặng dù hệ
thống đúng thiết kế. CHƯA thêm bước clear cooldown vào preflight — còn treo.

**Fix thật đã tìm+vá qua chính quá trình chuẩn bị demo (Đ55, KHÔNG phải giả lập):**
- Lúc đầu nghi ngờ "discovery snapshot rỗng cho cả 3 VM" — **SAI, do tôi tra nhầm Redis key**
  (dùng hostname `cust-app` thay vì agent_id đầy đủ `loyalty-uat_cust-app`). Snapshot thật có 32
  service, có `payment-api`. Đã xin lỗi + sửa lại thông tin cho user ngay khi phát hiện.
- **Fix thật, đã verify bằng chính log production**: `action_experience` (collection quyết định
  known-fix reflex có tự dispatch hay không) trước đây là **MỘT pool KHÔNG cách ly theo tenant** —
  bắt được qua log thật `event=auto_recovery_skipped reason=confidence_below_threshold ...
  confidence=0.71` trong lúc tenant `loyalty-uat` chưa từng có kinh nghiệm riêng: candidate 0.71
  đến từ dữ liệu tenant KHÁC (từ các đợt drill `staging-sim`/`default` trước đó), chỉ bị chặn nhờ
  ngưỡng confidence 0.75, KHÔNG phải nhờ cách ly. Đã vá: `find_known_fix_candidate()` +
  `_upsert_action_experience()` nay nhận `tenant_id`, dùng `scoped_collection_name()` giống hệt cơ
  chế `recall_playbook_advisory` đã dùng cho các collection khác. `reconcile_one` truyền thẳng
  `tenant_id` (tham số có sẵn của chính nó) — KHÔNG đọc từ `meta` dict (meta không hề có field này,
  thử đọc từ đó sẽ luôn fallback "default" và không fix được gì).
- 2 test cũ trong `test_remote_command_outcome_learning.py` sửa lại theo hành vi mới (collection
  nay là `action_experience:t1` không phải `action_experience` cho tenant "t1"); 1 test mới trong
  `test_remote_known_fix.py` khoá lại collection_name được scope đúng qua vector_store.query_points.
  **Full suite 7348 pass, 0 fail.**
- System Twin (`omni:aoip:system_model:{tenant}`, khác hẳn discovery snapshot ở trên) xác nhận SỐNG
  THẬT cho tenant `loyalty-uat`: 92 facts, ví dụ `host:cust-app runs_service rpcbind conf=0.85`,
  `host:cust-db connects_to host:cust-app conf=0.7` — nhưng **KHÔNG có fact nào tên `payment-api`**
  (nó thấy process `python3` chung chung, không gắn được tên systemd unit cụ thể) — hạn chế thật,
  chưa fix, cần biết khi thiết kế phần "chứng minh hiểu hệ thống" của demo.

**Đã xong (2026-08-11, verify sống):**
1. Fix cách ly tenant trong `action_experience` — commit `0f25e6d`, build #63 SUCCESS, kubectl exec
   xác nhận `find_known_fix_candidate`/`_upsert_action_experience` có `tenant_id` + gọi
   `scoped_collection_name` thật trong image đang chạy.
2. Telegram tier-aware — commit `c63fa16`, build #64 SUCCESS. `_resolve_tier_info()` mới trong
   `remote_diagnosis_emitter.py`: gọi ĐÚNG `gate_decision_for_tool()`/`resolve_tier()` mà gateway
   dùng để chặn/duyệt dispatch thật (`_enforce_tier_gate` trong `agent_runtime.py`) — không suy
   đoán riêng ở tầng render. Section 4 (CẦN LÀM) nay in rõ tier hiện tại + quyết định
   ALLOW/SUGGEST/HITL cho đúng capability đề xuất, thay câu chung chung cũ. Best-effort: lỗi resolve
   không chặn gửi Telegram, chỉ rơi về wording chung chung. 8 test mới
   (`test_diagnosis_card_tier_aware.py`). kubectl exec xác nhận `render_diagnosis_session` có tham
   số `tier_info`, render đúng chữ "AUTO"/"TỰ THỰC HIỆN" khi truyền tier_info thật.
   **Full suite 7356 pass, 0 fail** cả 2 lần.

**Seed drill sạch — xác nhận sống 2026-08-11 18:03-18:08:** payment-api dừng thật lúc 18:03:28, tự
phục hồi lúc 18:08:20 (~5 phút). Log xác nhận `auto_recovery_dispatched` → `remote_command_outcome_reconciled
state=COMPLETED rc=0` × 2 lần, KHÔNG có dòng `tier_info_resolve_failed` nào (card tier-aware render
đúng, không rơi về wording chung chung). `FT.INFO idx:action_experience:loyalty-uat` xác nhận
`num_docs=2` — tenant demo giờ có kinh nghiệm RIÊNG của chính nó, không lẫn tenant khác.

**Câu chuyện "cấm ở mức nào" + storyboard video — ĐÃ VIẾT**, xem `scripts/demo/TALKING_POINTS.md`:
quyết định KHÔNG thêm capability MEDIUM giả cho demo (tránh mở rộng bề mặt rủi ro thật chỉ để phục
vụ 1 buổi demo) — thay vào đó dùng nguyên trạng làm bằng chứng "phạm vi hẹp = thiết kế": 3 lớp chặn
đã verify sống hôm nay (danh sách capability đóng cứng trong code, tier gate, ngưỡng confidence
0.75 từng chặn thật 1 candidate 0.71). Storyboard video 2-3 phút, 7 cảnh, dùng lại đúng luồng đã
verify — không cần dàn dựng thêm.

**Đã thêm bước 8 vào preflight** (`cafe_demo_preflight.sh`, commit `58f2731`): báo cáo cooldown
fingerprint còn lại trước khi đi — đã bắt được chính cooldown thật (~438s) sinh ra từ seed drill
vừa chạy, xác nhận script hoạt động đúng như thiết kế (không tự sửa, chỉ báo).

**Toàn bộ Đ55 đã đóng.** `scripts/demo/` có đủ 3 file (`cafe_demo_preflight.sh`,
`cafe_demo_payment_api.sh`, `TALKING_POINTS.md`), tất cả đã chạy/verify bằng dữ liệu thật, không
phải lý thuyết chưa test. Chỉ còn chờ user chốt ngày gặp CTO.

**Next step (không thuộc Đ55, backlog xa hơn nếu có thời gian):** kho SOP (`itops_sop_ledger`) vẫn
0 doc, chưa điều tra tiếp (nêu từ Đ53); Twin chưa gắn tên service cụ thể như `payment-api` (nêu
trong Đ55, hạn chế thật của discovery, chưa fix).
nếu cooldown là nguyên nhân duy nhất từng chặn, thêm bước clear cooldown vào preflight → dry-run
sạch 1 lần cuối → viết storyboard video → commit.

**Cập nhật:** 2026-08-11 (Đ53 — fix Telegram + nối known-fix reflex vào nhánh service/application.
**Full suite 7347 pass, 0 fail. Commit `e8fc230` đã push cả 2 remote, build Jenkins #61+#62 SUCCESS,
verify sống bằng `kubectl exec` trong pod `omni-fullstack` xác nhận cả 3 thay đổi.** Đ52 giữ nguyên
bên dưới. Sau Đ53: session chuyển sang brainstorm chiến lược sản phẩm — xem mục "Đ54" cuối phần này.)

### Đ54 — Brainstorm chiến lược: có nên tiếp tục + hướng bán được (2026-08-11)

**Không phải task code.** User hỏi thẳng "dự án có đáng tiếp tục không, hướng nào bán được".
Đã search thị trường thật (không đoán): Resolve AI **có thật** — $125M Series A, valuation $1B
(Lightspeed dẫn đầu, 4/2/2026), khách hàng thật Coinbase/DoorDash/MongoDB/MSCI/Salesforce/Zscaler.
Cùng tầng còn có Datadog Bits AI SRE, PagerDuty SRE Agent, Azure/AWS/New Relic/Dynatrace agent,
Cleric, Traversal, NeuBird, BigPanda, NudgeBee (self-hosted, gần kiến trúc Omni nhất).

**Kết luận đưa cho user:** đừng định vị Omni là "AI SRE tổng quát" — Resolve AI đã chiếm vị trí đó
với đội ngũ + vốn lớn hơn nhiều bậc, khách hàng của họ (enterprise đã có sẵn observability) không
phải đối tượng Omni nên nhắm. Khuyến nghị thu hẹp vào khoảng trống thật: **SMB/tổ chức chưa có sẵn
observability stack**, kênh **MSP** (B2B2B, một Omni phục vụ N tenant), và **data residency**
(metadata-only — khác biệt với SaaS Mỹ). Từ chối đề xuất "Competitive Architecture Matrix 30-50
capability" của user (lấy từ ChatGPT) vì đó là phân tích không tạo doanh thu — khuyến nghị thay
bằng tìm 1 design-partner thật trong 2-4 tuần.

**User đã quyết (lượt trước Đ54):** "chỉ cắm đúng con aoip agent vào, còn hạ tầng tôi lo" — xác
nhận mô hình SaaS lõi tập trung (Omni core do user vận hành) + footprint khách hàng tối thiểu (chỉ
agent). Đã khớp kiến trúc hiện có (multi-tenant qua `tenant_id`, Postgres `omni_admin`). 3 việc
phải xong TRƯỚC khi đưa agent ra khỏi VM lab vào máy khách thật — **CHƯA làm, chỉ mới nêu**:
1. Gói cài đặt 1 lệnh (hiện tại cài tay qua `orb -m`, chưa có installer thật).
2. `aoip-agent.service` chạy root, không sandbox (`ProtectSystem`/`NoNewPrivileges` thiếu) — chấp
   nhận được ở VM lab, **bắt buộc phải vá trước khi chạm máy khách thật**.
3. Chưa đo footprint CPU/RAM của agent trên host khách — cần số liệu cụ thể để làm lời hứa bán hàng.

**Next step:** chờ user trả lời câu hỏi đang treo (có kênh MSP/SMB nào ở VN chưa, hay từ số 0) rồi
mới quyết bước kỹ thuật tiếp theo — KHÔNG tự ý bắt đầu vá hardening/installer khi chưa rõ hướng đi.

### Đ53 — Fix Telegram + audit RAG học lỗi — ĐÃ DEPLOY + VERIFY SỐNG (2026-08-11)

**Yêu cầu user:** (1) fix Telegram, (2) kiểm tra luồng tự học ghi RAG có giảm tải LLM không,
(3) giải thích các mô hình RAG hiện có.

**1) Fix Telegram — 2 lỗi, cả hai đã sửa:**
- `_render_section4_remediation` (`remote_diagnosis_emitter.py`) LUÔN khẳng định "Omni không tự
  thực thi" — SAI kể từ khi tier=assist bật (Đ52). Đổi thành câu đúng trong mọi trường hợp: đây
  là đề xuất, bước rủi ro thấp CÓ THỂ được Omni tự làm.
- **Vòng khép kín từng câm ở bước cuối**: sau khi `reconcile_one` ghi CRAT + bài học xong, KHÔNG
  kênh nào báo cho người dùng — họ chỉ thấy thẻ chẩn đoán ban đầu rồi im lặng vĩnh viễn dù lệnh
  đã COMPLETED/FAILED từ lâu (xác nhận: `payment-api` COMPLETED 13:51:21 nhưng không ai được báo).
  Thêm `_notify_telegram_outcome` gửi tin ✅/❌ riêng. Cần plumbing `chat_id` xuyên suốt: sửa
  `register_pending_command`/`dispatch_if_eligible` (`auto_recovery_bridge.py`) thêm tham số
  `chat_id`, `_dispatch_auto_recovery_if_eligible` (`remote_agent_pipeline.py`) truyền nó xuống.
- Phụ: nâng `logger.debug`→`logger.warning` cho lỗi ghi `action_experience` thất bại
  (`remote_command_outcome_loop.py`) — root logger prod chạy ở WARNING nên DEBUG trước đây vô hình.

**2) Luồng tự học RAG — CÓ THẬT, ghi đúng, nhưng KHÔNG giảm tải đúng chỗ đang nghẽn:**
- Xác nhận trên Redis thật: `action_experience` đã ghi đúng ca `payment-api` sáng nay
  (`cmd-d79afbcf3e1c4b1f`, `exec_outcome=success verification_result=pass`).
- **Phát hiện chính**: `remote_agent_pipeline.py` dòng ~340 —
  `needs_research = ... or (route in (KNOWN_BASELINE, KNOWN_WITH_FIX) and urgency in
  NOTIFY_TIERS)` — dù recall trúng (`KNOWN_WITH_FIX`), urgency critical/high (đúng loại chiếm
  96% tải: domain `service`+`application`) vẫn chạy NGUYÊN vòng LLM 8 lượt. Comment xác nhận chủ
  đích ("known pattern but urgent — still diagnose") — an toàn nhưng có nghĩa recall không tiết
  kiệm LLM cho đúng traffic cần tiết kiệm nhất. Tải giảm hôm nay là nhờ COOLDOWN (Đ52), không
  phải nhờ tái dùng bài học.
- `try_remote_known_fix` (phản xạ dispatch thẳng, bỏ qua LLM hoàn toàn) chỉ nối vào
  `knowledge_pipeline.py` (đường `METRIC_SAMPLE`/`os_host` baseline z-score) — KHÔNG nối vào
  `remote_agent_pipeline.py` (đường `service_systemd_units`/`remote_log_errors`, đường chính).
- **Phát hiện phụ**: kho SOP (`itops_sop_ledger`, CLAUDE.md ghi "1019 mục") hiện **0 doc** trên
  cluster này — cả HNSW index lẫn hash gốc `omni:rag:sop:default` đều rỗng. Chưa rõ mất từ khi
  nào; KHÔNG tự ý ingest lại (cần file JSONL nguồn, chưa xác nhận vị trí).

**3) Giải thích RAG — đã trả lời user trực tiếp trong hội thoại.** Tóm tắt: 1 backend (Redis
HNSW, `pgvector_store.py` chỉ là shim), 11 collection phân vai khác nhau. Chỉ `action_experience`
(9 doc) đang sống trên đường xử lý chính; `diagnostic_history` (1085) + `infra_topology` (204) có
dữ liệu nhưng không phải recall path; 8 collection còn lại (SOP, k8s_expert, SRE_KNOWLEDGE,
vendor_knowledge, cli_hil_context, os_hard_fail_diagnostic, playbooks, semcache) đều **0 doc**.
`redis_brain.py` (multi-turn RAG session, không gọi LLM) tồn tại nhưng không có call site trong
`remote_agent_pipeline.py`/`remote_triage.py` — chưa đấu vào đường VM khách.

**Cập nhật 2026-08-11 (sau đợt brainstorm):** mục (4) ở trên **ĐÃ LÀM** — `_try_remote_known_fix_reflex`
mới trong `remote_agent_pipeline.py`, gọi trước cả cooldown fingerprint khi
`needs_research and urgency in NOTIFY_TIERS`: tra `action_experience` (không phải collection
playbook mà triage dùng để định route), có discovery snapshot xác nhận resource đúng host thì
dispatch thẳng qua `auto_recovery_bridge.dispatch_if_eligible` (CRAT fail-closed không đổi) và bỏ
qua nguyên vòng LLM 8 lượt. Không snapshot/không candidate → rơi về đường cũ, hành vi không đổi.
4 test mới (`tests/test_remote_known_fix_reflex.py`). **Full suite 7347 pass, 0 fail.** Commit
`e8fc230`, push cả 2 remote (gitea+github), build Jenkins #62 SUCCESS (~11 phút). Verify sống bằng
`kubectl exec` trong pod `omni-fullstack-dd6bd8c95-7gcpt`: `_try_remote_known_fix_reflex` có thật
trong image đang chạy, `_render_section4_remediation` không còn câu "Omni không tự thực thi",
`_notify_telegram_outcome` có thật — cả 3 thay đổi Đ53 đều sống, không chỉ "rollout successful".

**Còn treo, CHƯA làm (mục 3 cũ):** kho SOP (`itops_sop_ledger`, CLAUDE.md ghi "1019 mục") vẫn 0
doc trên cluster — chưa điều tra tiếp, chưa rõ mất từ khi nào, KHÔNG tự ý ingest lại.

**Next step:** xem mục "Đ54" phía trên — session đã chuyển sang brainstorm chiến lược sản phẩm,
chưa quay lại việc kỹ thuật nào mới.



### Đ52 — Cooldown chẩn đoán theo fingerprint — ĐANG KIỂM CHỨNG (2026-08-11)

**Đã xong:** commit `8ab9375` + `769b910`. Build Jenkins **#58 SUCCESS**, image
`10.43.239.205/library/multi-agent-system:8ab9375` đã chạy trên pod
`omni-fullstack-8655857f66-bsn6q` — verify sống bằng `kubectl exec` import module thật
(`COOLDOWN_S=900 RETRY=180`, pipeline có đủ `should_diagnose`/`_mark_diagnosed_best_effort`/
`_verdict_from_session`). Test: 24 test mới, **7303 test toàn bộ pass, 0 fail**.

**Root cause (nối tiếp audit Đ51):** `mark_cluster_diagnosed()`/`get_seen_state()`/trường
`last_diagnosis` đã tồn tại sẵn trong `pkg/reasoning/evidence_cluster.py` nhưng **KHÔNG có call
site nào** — hạ tầng cooldown viết rồi mà chưa bao giờ đấu dây. Xác nhận trên Redis thật trước
drill: **0/60** key `omni:evcluster:seen:*` có `last_diagnosis` khác `null`.

**Lỗi thiết kế tự bắt được giữa đường (ghi lại để không lặp):** bản đầu cho "lượt trước thất bại"
bỏ qua cooldown HOÀN TOÀN. Nhưng 77.7% lượt đang thất bại ⇒ mọi ca hỏng retry sau 20s ⇒ tải không
giảm ⇒ cooldown vô tác dụng đúng lúc cần nhất. Đã đổi thành `RETRY_COOLDOWN_S=180` và khoá bằng
test `test_lan_truoc_that_bai_thi_thu_lai_som_hon_nhung_KHONG_ngay_lap_tuc`.

⚠️ **PHÉP ĐO BỊ NHIỄU — chưa được kết luận cooldown có tác dụng.** Sau khi deploy, tải tự giảm về
gần 0 nhưng **KHÔNG phải nhờ cooldown**: Đ50 đã xoá `omni-remote-agent`/`replay01` vốn là nguồn
sinh lỗi log, nên `remote_log_errors` nay trả `PASSED` và thoát sớm ở
`remote_agent_pipeline.py:232` (trace không có stage nào). Baseline trước deploy đã lưu ở
`/tmp/baseline_truoc_cooldown.json` (204 session/3h, 23 cảnh báo duy nhất, lặp 88.7%, hữu ích
3.4%, LLM chết 77.7%, trễ trung vị 591s) — nhưng so trực tiếp với sau deploy là **sai phương
pháp**, phải nói rõ.

**✅ COOLDOWN ĐÃ CHỨNG MINH HOẠT ĐỘNG — đo trên UAT thật, sự cố đang diễn ra:**
4 phút liên tiếp: **12 evidence vào → 0 vòng chẩn đoán khởi động → 2 lần `diagnosis_cooldown`
chặn** (đếm ngược 771s→749s→728s, count tăng 4→5→6), unit drill vẫn `failed`. Trước khi có
cooldown, đúng 12 evidence đó sẽ tạo 12 vòng LLM — chính cơ chế đẻ ra 989 lượt cho 33 vấn đề.

**⚠️ Lỗ đua in-flight — ĐO ĐƯỢC, đã vá ở `42d82c7`, đang chờ build #59 xác minh:**
`12:12:36 / 12:12:57 / 12:13:19` — 3 vòng khởi động cách nhau ĐÚNG 20s (= chu kỳ collect), tổng 4
lượt trùng trước khi cooldown bám được lúc `12:17:39`. Nguyên nhân: mốc chỉ ghi SAU khi vòng chẩn
đoán xong (~2-4 phút). Vá: `IN_FLIGHT_VERDICT`/`IN_FLIGHT_TIMEOUT_S=600`, đánh dấu TRƯỚC khi chạy;
mốc tự hết hạn 600s để pod chết giữa vòng không bịt fingerprint 7 ngày. Kỳ vọng sau #59: **4 → 1**.

**Hai sai sót về PHƯƠNG PHÁP ĐO của chính phiên này (ghi để không lặp):**
1. Drill đầu `stop payment-api` KHÔNG tái hiện được lỗi: "vừa dừng" là **edge-triggered** (báo 1
   lần lúc chuyển trạng thái, `total_count` chỉ 3-4). Cơn lũ 398 lần đến từ danh sách `failed` —
   **level-triggered**. Đo bằng drill sai thì kết luận cũng sai.
2. Drill thứ hai ban đầu cũng vô hiệu: unit `disabled`+`failed` bị "migration residue guard"
   (`collectors/services.py:291-297`) lọc đúng thiết kế. Phải `systemctl enable` mới thành sự cố
   thật với collector.

**✅ AUTO-EXECUTE ĐÃ BẬT trên VM khách (quyết định trực tiếp của user, `42d82c7`).** Trước đó GCP
CHƯA TỪNG bật — đo thật: Omni chẩn đúng `payment-api` bị SIGTERM (**confidence 0.9**, 3 lượt, không
generic) và ĐÃ muốn tự chữa nhưng dừng ở `auto_recovery_skipped reason=agent_not_in_lab_allowlist`.
Allowlist cũ `staging-sim_*` (ghi trong CLAUDE.md) VÔ HIỆU từ khi đổi tenant sang `loyalty-uat`.
Bán kính nổ chặn ĐỘC LẬP 2 tầng, cố ý không gộp: (1) Omni chọn AGENT — 3 VM `loyalty-uat_*`;
(2) VM chọn UNIT — `AOIP_ALLOWED_SYSTEMD_UNITS` trong run.env (hiện `payment-api.service`,
`systemd-journald.service`). ConfigMap `omni-worker-config` GIỮ NGUYÊN `false` để cluster dựng lại
từ nó phải câm. Cộng `min_dispatch_confidence` + CRAT fail-closed.

**🔴 LỖI #6 — mutate CHẾT vì agent phải nối Redis nội bộ của Omni (đã vá, CHƯA COMMIT)**

Đo trên UAT 13:25: lệnh `systemd.restart_unit` tới agent thành công (`state=QUEUED http=200`,
agent chạy đủ `accept→progress→terminal`) nhưng thất bại:
`{"rc":1,"reason":"executor_exception: Timeout connecting to server"}`.

Nguyên nhân: `run.env` cả 3 VM trỏ `AOIP_REDIS_URL=redis://redis.multi-agent.svc.cluster.local`
— DNS **chỉ phân giải trong k3s**; từ VM khách `getent hosts` không ra, kết nối treo. Redis là
ClusterIP, cố ý không có đường ra ngoài. `run_guarded_recovery` cần Redis cho lease+ledger.

**User chọn hướng 2** (chuyển điều phối về đúng chỗ, KHÔNG mở Redis ra ngoài). Khi đọc code phát
hiện việc còn đơn giản hơn: `ExecutionLease`/`IdempotencyLedger` **chỉ có call site trong
`aoip/agent/*`** (không chỗ nào phía Omni), scope là `{tenant}:{unit-systemd}` ⇒ writer luôn là
agent TRÊN CHÍNH HOST đó ⇒ **không tồn tại nhu cầu điều phối liên máy**. Nên kho CỤC BỘ mới là
đúng ngữ nghĩa, không phải giải pháp tình thế.

Vá: `src/aoip/agent/local_coord.py` (MỚI) — `LocalCoordStore` file JSON + `fcntl.flock` + TTL,
giữ đúng bề mặt Redis (`set/get/delete/eval`) nên **`lease.py` và `idempotency.py` KHÔNG đổi một
dòng**. `runtime_config.py` không còn đọc `AOIP_REDIS_URL` (cố ý — run.env cũ vẫn còn dòng đó, và
bootstrap không được phụ thuộc vào việc ai nhớ xoá). `eval` chỉ nhận 2 script CAS của `lease.py`,
script lạ ⇒ `NotImplementedError` (im lặng trả 0 sẽ khiến renew luôn hỏng mà không ai hiểu vì sao).

Lợi thế so với Redis: đúng ranh giới NÃO/THÂN; không phơi kho dữ liệu lõi mọi tenant cho VM khách;
`flock` serialize đúng kể cả mất mạng hoàn toàn (ca dual-agent Đ50 từng có 2 process/host).

**18 test mới** (`tests/test_local_coord_store.py`), chạy THẬT qua chính `ExecutionLease`/
`IdempotencyLedger` chứ không mock.

**7 test cũ `TestUnitsThatStopped` đã vỡ do bản vá #5 — ĐÃ SỬA ĐÚNG CÁCH:** chúng mock theo THỨ TỰ
lệnh (`AsyncMock(side_effect=[...])`) nên cạn khi thêm lệnh. Đổi sang fake ĐIỀU PHỐI THEO LỆNH
(`_fake_run`) — chặt hơn, không vỡ khi đổi thứ tự, không nới lỏng assertion nào. Riêng
`test_first_cycle_reports_nothing` là **xung đột hợp đồng thật** (nó khoá đúng hành vi #5 cố ý
đổi) → viết lại thành `..._when_everything_healthy` kèm lý do đầy đủ.

**Đã triển khai lên CẢ 3 VM** (`local_coord.py` + `runtime_config.py` + `services.py`), gỡ
`AOIP_REDIS_URL`, thêm `AOIP_COORD_STORE_PATH=/var/lib/omni-agent/coord.json`, restart agent —
cả 3 `active`. Backup run.env: `/opt/omni-remote-agent/run.env.bak-D52` trên mỗi máy.

**⚠️ WORKING TREE CHƯA COMMIT:** `src/aoip/agent/local_coord.py`, `src/aoip/agent/runtime_config.py`,
`tests/test_local_coord_store.py`, `tests/test_remote_agent.py`. Full suite đang chạy nền — đọc kết
quả TRƯỚC khi commit.

**Đang chạy:** drill vòng khép kín lần 2 — `payment-api` dừng lúc 13:46:36 trên cust-app, KHÔNG can
thiệp. Kiểm bằng `orb -m cust-app -u root systemctl is-active payment-api`, KHÔNG chỉ đọc log.

⚠️ **Lần 1 phép thử đã bị TÔI làm hỏng**: tôi chạy `systemctl restart payment-api` để kiểm chứng
lệnh có hoạt động không (rc=0, có hoạt động) — nên lần đó dịch vụ sống lại DO TÔI, không phải do
Omni. Đừng đọc nhầm log 13:26 thành bằng chứng tự khắc phục.

**🔴 LỖI NỀN TẢNG #5 — hệ thống MÙ trước sự cố đang diễn ra (nghiêm trọng nhất phiên này)**

Đo trên VM thật 13:16: `payment-api` ở trạng thái `enabled`+`inactive` (outage sống) nhưng
collector trả `"all monitored services OK"` / `result=PASSED`.

Nguyên nhân: `_collect_units_that_stopped()` EDGE-TRIGGERED (`gone = prev - now_active`) — bắn
đúng 1 lần lúc chuyển trạng thái, sau đó unit không còn trong `prev` lẫn `now_active` nên rỗng
vĩnh viễn. Code tự mâu thuẫn với comment của chính nó ở `services.py:307` ("enabled + inactive
cũng là FAILED").

Hậu quả: (1) sự cố kéo dài báo 1 lần, mà 77% lượt chẩn đoán chết vì LLM timeout ⇒ mất khỏi radar;
(2) agent restart lúc dịch vụ đang chết ⇒ KHÔNG BAO GIỜ phát hiện; (3) vòng tự khắc phục không
chạy lại được. **Lỗi này chỉ lộ ra vì phép thử vòng khép kín TREO thay vì chạy** — nếu coi "treo"
là đang chờ thì đã bỏ qua.

Vá 2 lớp:
- `_known_stopped_units` — nhớ unit đã xác nhận dừng, báo lại mỗi chu kỳ tới khi chạy lại (commit `0fdcf37`)
- `_collect_already_down_units()` — quét level-triggered 1 lần lúc `prev is None`, bịt lỗ "outage
  có trước khi agent khởi động" (**CHƯA COMMIT** — còn trong working tree)

Phản chứng ghi chú gốc ("không có thuộc tính systemd nào phân biệt daemon với chạy-một-lần"): đo
trên cust-app 18 unit `enabled`+`inactive`, áp CẢ HAI bộ lọc còn **đúng 1** (`payment-api`), 0 false
positive — `ConditionResult=no` loại 13, `Type` ngoài `_DAEMON_TYPES` loại `dmesg`(idle)/
`e2scrub_reap`(oneshot), template loại `getty@`. Ghi chú gốc chỉ đúng khi dùng RIÊNG `ConditionResult`.
Dùng ALLOWLIST `_DAEMON_TYPES` chứ không denylist: kiểu lạ ⇒ coi là không-phải-daemon (nghiêng về
ít nhiễu); daemon kiểu hiếm vẫn được edge-trigger bắt khi nó dừng lúc agent đang theo dõi.

Verify sống trên VM sau vá: `result=FAILED`, `stopped=['payment-api']`, đúng 1 unit không nhiễu.
8 test mới, 1412 test vùng liên quan pass. Code đã copy sang `cust-app` + restart `aoip-agent`;
**CHƯA đẩy sang `cust-db`/`cust-edge`**. Backup file gốc: `/tmp/services.py.bak` trên cust-app.

**⚠️ WORKING TREE CHƯA COMMIT:** `src/remote_agent/collectors/services.py` (level-trigger) +
`tests/test_service_outage_persistence.py` (3 test cuối). Commit trước khi làm gì khác.

**🔑 BA CỔNG CHẶN MUTATE — phải mở CẢ BA, tìm ra bằng đo thật (đừng phải dò lại lần nữa):**

| # | Cổng | Ở đâu | Cách mở | Trạng thái |
|---|---|---|---|---|
| 1 | Allowlist agent | env `omni-fullstack` | `OMNI_LAB_AUTO_EXECUTE_AGENTS` | ✅ `42d82c7` |
| 2 | `runtime_flag` per-tenant | Postgres `omni_admin.runtime_flag` | `POST /autonomy/mutation` | ✅ đã bật |
| 3 | Master kill-switch | env **POD GATEWAY** (Rollout riêng!) | `OMNI_AUTO_EXECUTE_ENABLED` | ✅ `c370e02` (build #60) |
| 4 | tier × risk gate | Redis/PG | `POST /autonomy/tier` | ✅ `shadow`→`assist` |

**Bẫy đã vấp (ghi để không lặp):** đặt `OMNI_AUTO_EXECUTE_ENABLED=true` trên `omni-fullstack` là
CHƯA ĐỦ — `_master_auto_execute_enabled()` ở `gateway/routes/agent_runtime.py:223` đọc env của
**chính pod gateway**, một Rollout độc lập (`k8s/gitops/omni-gateway-rollout.yaml`). Triệu chứng:
`auto_recovery_dispatched ... http=423`, log KHÔNG nói lý do, phải đọc code mới ra. May là
`POST /autonomy/mutation` tự khai `requested=true effective=false reason=master_kill_switch_off`.

**Vì sao chọn tier `assist` chứ không `auto`:** `systemd.restart_unit` = risk **LOW**; bảng
`evaluate_tier_gate` cho `assist`→ALLOW với LOW, còn `auto` mở luôn MEDIUM (nới quá mức cần).
Với `assist`, mọi thao tác MEDIUM/HIGH vẫn buộc HITL. Tên tier canonical chỉ có 3:
`shadow|assist|auto` — `minimal`/`autonomous` KHÔNG hợp lệ, rơi vào nhánh fail-closed → SUGGEST.

**Bằng chứng chuỗi tự khắc phục (drill 12:53:27 dừng `payment-api` trên cust-app):**
- 12:53:47 (20s sau) `diagnosis_loop launched` — phát hiện đúng chu kỳ collect
- chẩn đoán: **confidence 0.9**, 3 lượt, root cause đúng ("terminated with signal=TERM status 15"),
  `remediation_steps[0]` = `sudo systemctl start payment-api` — KHÔNG generic fallback
- `auto_recovery_dispatched ... command_id=cmd-391686cdec054aa2 http=423` ← cổng 3 chặn
- 2 fingerprint riêng (`service_systemd_units` + `network_listeners`) cùng bắt được sự cố — hợp lệ,
  không phải lỗ đua; trace `network` trả `no_suggested_recovery` (đúng, nó chỉ thấy cổng 8080 đóng)

**⚠️ CẦN DỌN — trạng thái drill còn để lại trên `cust-app`:**
- `payment-api.service` đang **dừng** (chủ ý: để thử vòng khép kín auto-execute sau build #59).
- `omni-cooldown-drill.service` — ✅ ĐÃ DỌN lúc 12:52 (disable + rm + daemon-reload + reset-failed);
  xác nhận `systemctl list-units --type=service --state=failed,activating` trả về RỖNG.

**Next step:** (1) đọc kết quả full pytest suite (đang chạy nền) rồi COMMIT 4 file working tree
ở lỗi #6; (2) đọc kết quả drill vòng khép kín lần 2 — `payment-api` dừng 13:46:36, kiểm bằng
`orb -m cust-app -u root systemctl is-active payment-api`; (3) nếu vẫn FAILED, đọc bản ghi lệnh
MỚI (không phải `tail -1` tuỳ tiện): `redis-cli --scan --pattern "omni:cmd:rec:loyalty-uat:*"` rồi
lọc theo `created_at` lớn nhất — bản ghi cũ `cmd-039277bf` vẫn còn với lỗi Redis đã vá, dễ đọc
nhầm; (4) sửa chân thẻ Telegram (`unified_incident_card`/`remote_diagnosis_emitter`) đang ghi
"Omni không tự thực thi" — SAI kể từ khi tier=assist + auto-execute bật; (5) đo lại
`.venv/bin/python scripts/measure_diagnosis_health.py --hours 3 --compare
/tmp/baseline_truoc_cooldown.json`.

**CI/CD — user hỏi, đã kiểm chứng, TẠM GÁC theo yêu cầu user:** job `omni-gcp-deploy` có
`<triggers/>` RỖNG, Jenkins không có plugin Gitea/generic-webhook, và build #55/#56/#57 đều
`Started by user` ⇒ push KHÔNG tự build. Đây là **CỐ Ý**, không phải thiếu sót — `Jenkinsfile`
dòng 217-220 ghi rõ lý do: chính Jenkins tự push commit `ci: bump image tags` về gitea, bật
trigger ngây thơ sẽ tạo vòng build vô hạn. Muốn tự động phải cắt vòng lặp trước (lọc commit của
`jenkins-ci`, hoặc `[skip ci]` + stage tự thoát, hoặc bump ở branch riêng).

### Đ51 — Audit 9 domain × Telegram evidence — HOÀN TẤT, ĐO THẬT (2026-08-11)

### Đ51 — Audit 9 domain × Telegram evidence — HOÀN TẤT, ĐO THẬT (2026-08-11)

**Output:** `docs/audit/domain_telegram_evidence_audit_2026-08-11.md` (đầy đủ, có lệnh tái kiểm ở §8).
Plan gốc: `plans/domain-deep-dive-audit-2026-08-11.md` (đã thực thi, không cần chạy lại).

**Số đo thật — 989 diagnosis session trong Redis, không ước lượng:**

| Chỉ số | Giá trị |
|---|---|
| Tin Telegram **thực sự hữu ích** | **22/989 = 2.2%** (6h gần nhất: 3.0%) |
| Confidence = 0.0 | 767 = 77.6% (6h gần nhất: 81%) |
| Remediation "generic fallback" (`df -h`/`free -h`) | 909 = 91.9% |
| Được gửi Telegram | **989 = 100%** (không cái nào bị chặn) |
| Lượt LLM chết timeout | 2059/2773 = 74.3% |
| Trễ evidence→Telegram | trung vị **8.3 phút**, p90 14.6, max 23.2 |

**Nguyên nhân gốc — đo được, không suy đoán:** `OMNI_LLM_NUM_PARALLEL=1` ⇒ LLM xử lý tuần tự.
Đo 3 request đồng thời: 18s/37s/51s (xếp hàng tuyến tính hoàn hảo). 1 lượt = 24s ⇒ công suất
~150 lượt/giờ, nhu cầu thật ~173 lượt/giờ = **115% công suất** ⇒ hàng đợi vô hạn ⇒ vượt
`llm_chat_timeout_sec=120s`. Khớp ghi chú có sẵn ở `diagnosis_loop.py:214`.

**Bug thứ 2 (độc lập, sửa rẻ):** `remote_diagnosis_emitter.py:69` `diagnosis_has_real_finding()`
KHÔNG kiểm tra confidence — chuỗi "Diagnosis inconclusive…" thoả điều kiện ⇒ trả True ⇒ gửi.
Đó là lý do tỉ lệ gửi 100% thay vì ~2%. **Bug thứ 3:** cờ `degraded` chỉ True 32/989 dù 767 ca
LLM chết ⇒ không dùng được để lọc.

**Phần chạy TỐT (đừng sửa nhầm):** thu thập evidence, định tuyến domain, 1423 lệnh chẩn đoán chạy
thật trên VM (allowlist chặn `nc` đúng), CRAT fail-closed, và **cổng 3σ trên đường Prometheus
(`gw-prom-*`) chặn false-positive chính xác** — nên dùng làm hình mẫu cho đường `ra-*`.

**Sai lầm đã tự phát hiện + ghi lại trong audit §3.3:** tôi từng nghi cảnh báo `service` lúc
10:54–10:58 là "cảnh báo ma" về unit đã xoá ở Đ50. Sai — evidence thu lúc 10:43–10:44, unit xoá
lúc 10:48:47; cảnh báo THẬT, chỉ đến muộn 10–15 phút. Chính độ trễ tạo ảo giác đó.

**Phủ domain thực tế:** `service` 507 + `application` 444 = 96.2% toàn bộ tải. `security` 20,
`network` 8, `database` 6, `os_host` 2, `storage` 2. `kubernetes` 0 session (5 trace nhưng bị 3σ
chặn đúng), `hardware` 0 (không có collector — giới hạn kiến trúc, đúng như CLAUDE.md).

**Next step:** audit đã đóng, KHÔNG tự sửa gì. 3 hướng chờ user quyết (audit §7): (1) thêm ngưỡng
confidence vào cổng lọc — vài dòng, cắt ngay ~92% nhiễu; (2) hạ tải LLM xuống dưới công suất
(tăng NUM_PARALLEL / thêm cổng định lượng trước LLM / giảm tần suất probe `service_systemd_units`
đang chiếm 51% tải); (3) sửa cờ `degraded`. Cộng 4 rủi ro tồn đọng từ Đ50 vẫn nguyên.

### Đ50 — HOÀN TẤT (2026-08-11)

**Kết quả cuối, verify bằng lệnh thật trên cả 3 VM:**

| Kiểm chứng | cust-app | cust-db | cust-edge |
|---|---|---|---|
| Evidence rate TRƯỚC → SAU (lần/60s, đơn=3) | 6 → **3** | 7 → **3** | 6 → **3** |
| Process agent còn lại | 1 | 1 | 1 |
| `systemctl status omni-remote-agent` | `could not be found` | `could not be found` | `could not be found` |
| `/opt/omni-remote-agent/remote_agent/` còn nguyên | ✅ | ✅ | ✅ |
| `aoip-agent.service` | active | active | active |
| Soak NRestarts=0 + registry tươi | 30' PASS | 25' PASS | 25' PASS |

**Bằng chứng cơ chế chống-regression hoạt động** — chạy lại đúng lệnh đã gây bug 2026-08-04:
`orb -m cust-app -u root systemctl enable --now omni-remote-agent.service` →
`Failed to enable unit: Unit file omni-remote-agent.service does not exist.` (exit 1).
Trước cutover lệnh này thành công IM LẶNG và tạo process trùng — đó là lý do fix 2026-07-22
(`disable` thôi) đã regress còn lần này thì không thể.

Test: 1184 pass (`-k "agent or enroll or catalog or grounding"`). `payment-api.service` trên
cust-app đã khôi phục `active` sau drill.

**Phase 1 ✅ — ROOT CAUSE XÁC ĐỊNH TUYỆT ĐỐI (không suy đoán).** Thủ phạm KHÔNG nằm trong script
nào của repo — là 1 lệnh thủ công chạy 1 lần bởi phiên Claude Code trước:
`for m in cust-edge cust-app cust-db; do orb -m "$m" -u root systemctl enable --now
omni-remote-agent.service; done` lúc `2026-08-04T14:42:50.988Z`. Bằng chứng khớp 4 nguồn: mtime
symlink 3 VM (edge .549 → app .650 → db .770, đúng thứ tự loop), journal systemd, transcript
`48a26b0d-e15d-469d-baf6-013954b7f800.jsonl`, ngữ cảnh user "Bật và xoá đi". **Nguyên nhân sâu
xa: CLAUDE.md mô tả SAI rằng 2 unit là một cái đổi tên** → chọn nhầm unit. 3 nghi phạm đặt ra
trong plan đều KHÔNG phải thủ phạm khởi phát (chi tiết trong audit doc). Bài học: grep repo không
bao giờ tìm ra loại lỗi này.

**Phase 2 ✅ (cust-app)** — evidence rate **6→3 lần/60s** (interval=20s ⇒ 3 đúng kỳ vọng),
process còn đúng 1 (`aoip.agent.employee`), drill thật payment-api down → `domain=service`
`urgency=critical`, **0** cảnh báo `agent OFFLINE`.
**Phase 3 ✅ (cust-db, cust-edge)** — cust-db **7→3**; cust-edge **6→4** (ghi nhận trung thực:
KHÔNG đúng 1/2 chính xác, nghi do cadence collector `security` riêng — nhưng process=1 là bằng
chứng quyết định). Capability registry đủ: cust-db có `database`, cust-edge có `services`/`network`.
**LƯU Ý: cả 3 VM hiện đang ở trạng thái `stop` (chưa `disable`, chưa xoá unit)** — rollback tức
thời bằng `systemctl start omni-remote-agent.service` nếu cần.

**Phase 5a/5b/5c ✅** — CLAUDE.md + ADR-001 sửa đúng trạng thái đích; 3 script chuyển sang
`aoip-agent`; **dòng `rm -rf "$INSTALL_DIR/$p"` (dòng 84 `omni-agent-update-fleet.sh`) CỐ Ý giữ
nguyên** (routine code-sync hợp lệ, khác hẳn cảnh báo "không rm /opt/omni-remote-agent"); syntax
bash+python pass.

**Rollback path duy nhất nếu cần quay lui:** `docs/audit/backup-units/` chứa bản sao nguyên văn
unit file lấy từ từng VM trước khi xoá (repo KHÔNG có template nào khác tái tạo được). Xem
README trong thư mục đó. Lưu ý: bật lại unit cũ SONG SONG với `aoip-agent` sẽ tái tạo đúng bug
double-fire — phải stop `aoip-agent` trước.

⚠️ **Ghi nhớ vĩnh viễn:** `/opt/omni-remote-agent/` vẫn là thư mục cài đặt ĐANG DÙNG (tên là lịch
sử) — `aoip.agent.employee` import code `remote_agent/` bên trong đó làm thư viện. Không bao giờ
`rm -rf` thư mục này khi "dọn agent cũ".

**Rủi ro tồn đọng — CHƯA fix, có chủ đích ngoài phạm vi (cần quyết định riêng):**
1. **Hardening**: `aoip-agent.service` chạy **root, không sandbox** (không `ProtectSystem`/
   `NoNewPrivileges`), trong khi template `scripts/omni-agent.service` đã có hardening nhưng
   không dùng. PoC RCE root từng xuyên thủng 2026-07-31. Xem `docs/audit/SRE_READINESS_2026-08.md`
   mục B7.
2. **ADR-001 §5**: `src/gateway/routes/agent_runtime.py` vẫn duplicate command-lifecycle logic của
   `aoip.agent.delivery.DurableCommandChannel`. Lý do kỹ thuật ban đầu đã hết hiệu lực từ `409dcb2`.
3. **`aoip.agent.daemon`** (canonical target dài hạn ADR-001 §1) vẫn CHƯA từng deploy thật.
4. **Không có cơ chế phát hiện double-fire**: registry Redis dùng chung `agent_id` nên luôn 1
   key/host bất kể mấy process gửi — chính phép đo này tạo false-negative cho fix 2026-07-22.
   Đề xuất: so tần suất evidence thực nhận với `collect_interval` khai báo.

**Next step:** Đ50 đã đóng. Không có việc tồn dở. 4 rủi ro trên chờ user quyết định ưu tiên.

## Đ50 — Loyalty-UAT tenant cutover + phát hiện bug dual-agent VM (2026-08-11)

**Việc đã làm, tất cả xác nhận sống trên GCP UAT + 3 VM lab:**
1. Xóa tenant `staging-sim` (Postgres `omni_admin.tenant` + toàn bộ bảng phụ thuộc theo đúng thứ
   tự FK), tạo tenant mới `loyalty-uat` (nhớ tạo kèm `tenant_plan` — quên bước này lần đầu gây
   lỗi "không có entitlement agent hoạt động", đã tự phát hiện + fix).
2. Provision 3 agent credential mới (`loyalty-uat_cust-app/db/edge`) qua enroll-token flow
   (`POST /autonomy/tenants/{t}/enroll-tokens` rồi `POST /webhook/agent/enroll`), cập nhật
   `run.env` trên cả 3 VM, restart `omni-remote-agent.service` — verify 200 OK + registry Redis
   sống cho cả 3.
3. Chạy drill thật (dừng `payment-api.service` trên `cust-app`) với agent online thật (khác hẳn
   Admin Simulator trước đó) — `diagnosis_loop` khởi động đúng `agent_online=True`, nhưng cả 2
   lượt LLM đều timeout (120-240s, `qwen3:8b -np 1`) → không ra kết luận. Xác nhận: bottleneck
   LLM tồn tại độc lập với việc agent online/offline, không phải bug mới.
4. **Phát hiện bug lớn hơn phạm vi ban đầu:** cả 3 VM đang chạy **2 systemd unit song song**
   — `omni-remote-agent.service` (gốc, lỗi thời) VÀ `aoip-agent.service` (`aoip.agent.employee`,
   đã chốt target theo ADR-001) — cả 2 cùng `enabled+active`, double-fire toàn bộ evidence. Đây
   là **regression** của 1 fix đã tưởng xong 2026-07-22
   (`docs/architecture/AUDIT_autonomous_sre_team_2026_07_22.md` Lane B). KHÔNG phải do phiên này
   gây ra — xác nhận trạng thái này đã tồn tại từ trước khi tôi đụng vào VM.
5. Đã dọn 1 process rác thật sự riêng biệt: `omni-remote-agent-replay01.service` (tenant
   `tenant-replay-01`, trỏ domain lab đã retired `ai-agent.local`, luôn fail) — xóa hẳn khỏi
   cust-app + cust-edge. Đã restart `aoip-agent.service` trên cả 3 VM để nhận credential mới.
6. Điều tra Dex 500 (login provider portal) → root cause là 1 lần `redis.exceptions.ConnectionError`
   thoáng qua trùng khớp lúc tôi chạy loạt `kubectl exec` xóa/tạo tenant dồn dập — KHÔNG phải bug
   cấu hình. Verify sống: `/auth/login` → 302 đúng, `redis.ping()` → True. Không cần sửa gì.
7. **Đã lập plan đầy đủ** (KHÔNG code, đúng yêu cầu user) để dọn dứt điểm dual-agent:
   `plans/consolidate-vm-agent-remote-to-aoip-employee-2026-08-11.md` — 6 phase, đã qua
   adversarial review (architect agent), sửa 4 CRITICAL finding. Root cause thật của regression
   rất có thể là `scripts/e2e_onboarding_full_flow.py` dòng 345-347 (chạy vô điều kiện
   `systemctl enable/start omni-remote-agent`) — khả tín hơn giả thuyết ban đầu (rollback drill
   thủ công quên hoàn tác). Quyết định đã chốt với user: target = `aoip.agent.employee` (không
   build `aoip.agent.daemon` mới), gỡ hẳn `omni-remote-agent.service` (không giữ rollback path),
   gộp sửa CLAUDE.md dòng 411 (sai, nói 2 unit là 1) + ghi nợ ADR-001 §5. **CẢNH BÁO sống còn ghi
   trong plan:** 2 unit chia sẻ chung thư mục `/opt/omni-remote-agent/` trên VM (aoip-agent
   import code remote_agent làm thư viện) — tuyệt đối không `rm -rf` thư mục đó, chỉ xóa unit
   file, nếu không sẽ outage cả 3 VM.

**Next step:** Chờ user xác nhận trước khi chạy Phase 1 (điều tra root-cause) của plan Đ50 —
KHÔNG tự ý bắt đầu thực thi. Task #24/#39 (test domain service + phân tích 5 câu hỏi) coi như đã
trả lời trong hội thoại nhưng CHƯA ghi lại thành báo cáo cố định — nếu resume, có thể bỏ qua
(không phải blocker) trừ khi user hỏi lại.

Đ49 — ĐÃ ĐÓNG. Blueprint dọn dẹp + hoàn thiện Omni tự vận hành (Track A+B,
`plans/omni-finish-autonomous-sre-and-repo-cleanup-2026-08-10.md`) VÀ gộp FinGuard→Smart SIEM nội
bộ (S0-S4, `plans/finguard-to-smart-siem-merge-2026-08-04.md`) — theo yêu cầu trực tiếp của user
"merge luôn vào omni đi, nó là tính năng có sẵn và phải có của omni, không phải thêm tính năng
mới". domain `security` chuyển từ ❌ (0 collector, 0 dữ liệu) sang ⏳ có bằng chứng thật (verify sống
bằng drill sudo-failure trên VM lab `cust-edge`, tới tận `corr:*` Redis + CRAT ADVISORY_DECISION).
3 bug thật tìm được qua drill sống — xem mục Đ49 dưới đây + `docs/audit/invariant_audit_2026-08.md`.)
Đ48 — task #16 (việc gốc phiên trước) **XONG, VERIFY SỐNG bằng build thật #49 SUCCESS** — Jenkins
giờ CHỈ test/build/push Harbor + bump tag git-SHA + commit-back; ArgoCD (`selfHeal: true, prune:
true`, multi-source) là bên DUY NHẤT apply/rollout. Build #49 xác nhận trực tiếp qua `kubectl`:
`omni-fullstack`/`omni-onboarding`/`aoip-provider-portal`/`aoip-tenant-portal`/`omni-gateway` đều
chạy tag thật `e2afaf7`, ArgoCD `Synced Healthy`. Đ47 — migrate Jenkins từ VM systemd vào pod
trong k3s ĐÃ CUTOVER XONG HOÀN TOÀN. Đ46 — build #40 SUCCESS, P0+P1 verify sống bằng incident
thật qua Alertmanager. Đ45 — CI/CD do phiên này đảm nhận hoàn toàn.

## Đ49 — Blueprint hoàn thiện Omni tự vận hành + dọn repo (ĐANG LÀM, chưa xong)

**Bối cảnh:** User yêu cầu rõ: không thêm tính năng mới, chỉ sửa cái sai/xóa cái dư/đồng bộ cái
lệch/bổ sung cái thiếu; đồng thời dọn repo cho gọn ("nhìn nó như bãi rác quá"). Quy ước môi trường
mới chốt: **MacBook = dev, GCP k3s = UAT (KHÔNG phải production)** — luồng bắt buộc: test local
trước → push Gitea → Jenkins build/push Harbor/bump tag → ArgoCD deploy UAT → verify qua kubectl
trên UAT thật. Đã lưu vào memory `project_env_convention_macbook_dev_gcp_uat`.

Blueprint đầy đủ tại `plans/omni-finish-autonomous-sre-and-repo-cleanup-2026-08-10.md` (2 track,
đã review đối kháng Opus trước khi chạy — 6 CRITICAL đã sửa vào plan). Chi tiết bằng chứng từng
bước nằm ở `docs/audit/invariant_audit_2026-08.md` (audit doc liên tục cập nhật theo B0-B6).

### Track A — Repo hygiene: ĐÃ ĐÓNG (commit 1ed174f, b81e380, f9dd56c)
- A1: tách `docs/handoffs/CURRENT_SESSION.md` 3682→316 dòng, archive tại
  `docs/handoffs/archive/SESSION_ARCHIVE_2026-08.md`.
- A2: đối chiếu bảng RETIRED trong CLAUDE.md — 0 file rác sót (đã dọn từ trước).
- A3: xóa `ui/app/` (Next app cũ 19 route, đã retired 2026-07-06), `ui/Dockerfile` root,
  `ui/e2e/`+`ui/playwright.config.ts` (target `svc/omni-ui` đã retired). GIỮ NGUYÊN
  `ui/package.json`/`ui/packages/` — workspace root thật cho 2 portal sống. User đã xác nhận
  trước khi xóa. Ngoài phạm vi: dọn 6/14 worktree sạch trong `.claude/worktrees/` (~600MB, gitignored).

### Track B — Đồng bộ/sửa lệch 9-domain: B0-B6 ĐÃ ĐÓNG + PUSH + DEPLOY, C1 gần xong
- **B0** (commit 89787ed): 8 invariant chính trong CLAUDE.md đối chiếu code+test thật — cả 8 đúng.
- **B1** (commit 9c6fefe, 097239f): **bug thật tái hiện sống trên UAT bằng Admin Simulator**
  (`POST /simulate/sys_hard_fail {target:omni}` qua gateway port-forward) —
  `evidence_mutate_emit.py::emit_hitl_pending()` gửi Kafka+Redis nhưng KHÔNG ghi
  `omni_admin.hitl_decision` (consumer `hitl_dispatcher.py` không đăng ký trong worker loop nào).
  Đã vá: thêm `repo.create_hitl_pending()` trực tiếp tại nguồn, mirror `hitl_telegram.py` đã đúng.
- **B2** (commit 61170a0): vòng học chỉ nhận nhãn khen — ĐÃ FIX TỪ TRƯỚC (commit 383cc1a), không
  cần sửa gì, chỉ xác nhận qua code+test.
- **B3** (commit ff3e6b4): FinGuard→Smart SIEM merge (`plans/finguard-to-smart-siem-merge-2026-08-04.md`)
  CHƯA XONG (S0 dở, S1 collector chưa viết, `omni_admin.playbook`=0 dòng) — KHÔNG tự ý viết S1 vì
  là khối lượng triển khai đáng kể, ranh giới với "tính năng mới" mờ. Domain `security` giữ ❌.
- **B4** (commit 42f3053): domain `hardware` — xác nhận là giới hạn kiến trúc (containerized, 0
  collector), không phải gap môi trường có thể giải quyết bằng đổi hạ tầng. Cập nhật CLAUDE.md.
- **B5** (commit 3428f7f): **bug thật thứ 2** — domain `application` urgency kẹt "medium" vì
  `assess_domain_severity` đọc `error_rate`/`latency_p99_ms` nhưng producer thật
  (`collectors/logs.py`) phát `failed_file_count`/`files_scanned` — lệch bí danh, cùng lớp bug
  `cpu_pct`/`cpu_percent` đã vá năm ngoái ở domain OS. Đã vá + 3 test.
- **B6** (commit a3dc845, deploy qua build #52): quét lại 9 domain tìm bug B5 có lặp ở đâu khác —
  **tìm thấy domain `storage` cùng lỗi hệt B5** (`collectors/storage.py` phát
  `disk_critical_count`/`disk_warn_count`, không phát `disk_pct`/`result`-trong-fact; field
  `disk_percent` mà CLAUDE.md từng ghi thực ra đến từ `collectors/system.py`, cơ chế khác hẳn). Đã
  vá `src/pkg/reasoning/domain_signals.py` + 2 test + cập nhật CLAUDE.md bảng 9-domain. `os_host`/
  `database`/`service`/`network` xác nhận ĐÚNG (không lệch). `kubernetes`/`security`/`hardware`
  ngoài phạm vi quét (cơ chế khác/chưa build).
- **VERIFY SỐNG B1 trên UAT** (sau build #51 SUCCESS, tag `3428f7f`): trigger lại
  `POST /simulate/sys_hard_fail {target:omni}` → trace `sim-sys_hard_fail-604075f2371d` escalate
  `L3_HITL` → `hitl_pending_emitted` → query `omni_admin.hitl_decision` trên `omni-postgres-0`:
  **1 dòng PENDING mới thật** (`pending_id=mut-sim-sys_hard_fail-604075f2371d`,
  `tool_name=human_escalation`, `risk_class=HIGH`) — khác hẳn lần verify B1 đầu tiên (0 dòng,
  trước fix). Fix B1 xác nhận hoạt động đúng trên UAT thật, không chỉ "code đã sửa" suông.
- **C1** (task #11): cập nhật bảng 9-domain trong CLAUDE.md xong (storage/application → ✅ ĐÃ VÁ).
  Còn: build #52 (deploy B6) — kiểm tra kết quả, commit+push phần cập nhật CLAUDE.md/handoff này,
  đóng task list, báo cáo tổng kết cho user.

### Gotcha vận hành mới phát hiện (Đ49) — quan trọng cho phiên sau
1. **Jenkins KHÔNG tự trigger khi push Gitea** (đã biết từ trước, nhắc lại): sau mỗi `git push
   gitea main`, phải tự gọi Jenkins API để trigger build nếu muốn deploy lên UAT ngay. Credential:
   `docs/handoffs/GCP_CREDENTIALS_2026-08-04.md` (không commit) — Jenkins user/pass ở đó. Cách
   trigger qua curl (crumb + cookie jar, xem lịch sử bash trong session này nếu cần lại):
   `curl -c jar -u user:pass .../crumbIssuer/api/json` lấy crumb → `curl -b jar -u user:pass -H
   "Jenkins-Crumb: $CRUMB" -X POST .../job/omni-gcp-deploy/build`.
2. **RACE CONDITION đã gây build #50 FAIL**: trigger Jenkins build RỒI vẫn tiếp tục `git push`
   commit khác lên `gitea` trong lúc build đang chạy → bước "Update image tags in git (GitOps)"
   của Jenkins bị `git push` từ chối (non-fast-forward) vì `main` đã đổi. Image vẫn build/push
   Harbor thành công (không mất gì), chỉ bước tag-bump git fail nên ArgoCD không thấy tag mới.
   **Bài học: sau khi trigger Jenkins build, KHÔNG push thêm commit nào tới khi build xong.** Build
   #51 được trigger lại sau khi ngừng push — xem kết quả ở "Next step" nếu wakeup đã chạy.
3. Bảng `omni_admin` (32 bảng, đúng CLAUDE.md) sống trong schema `omni_admin` của DB `omnidb`
   trên pod `omni-postgres-0` — `psql -U omni -d omnidb` mặc định vào schema `public` (rỗng),
   phải `SELECT ... FROM omni_admin.<table>` tường minh hoặc `\dn`/`SET search_path`.
4. Secret `telegram-bot` (namespace `multi-agent`) trên UAT vẫn là **placeholder rỗng**
   (`bot-token=""`, `chat-id="0"`) + `OMNI_TELEGRAM_ENABLED=false` trong ConfigMap — đây là thiết
   kế cố ý (file `k8s/deployments/telegram-bot-secret.yaml` ghi rõ), KHÔNG phải bug. User hỏi
   trong phiên này, chưa cung cấp bot token thật nên chưa tạo secret thật — cần bot token +
   chat_id thật từ BotFather nếu muốn bật.

### Đ49 tiếp — Telegram bật thật + Merge FinGuard→Smart SIEM (S0-S4) — ĐÃ ĐÓNG

**Telegram**: user xác nhận có bot thật (`@Leader_Agentic_bot`) trong `.env` gốc (gitignored).
Tạo Secret thật trên UAT qua `kubectl` + bật `OMNI_TELEGRAM_ENABLED=true` trong ConfigMap (đã push
git để ArgoCD không tự revert). Verify sống: `telegram_outbound_ok chat_id=-5174042122
message_id=4454` — tin nhắn thật đã gửi.

**Merge FinGuard→Smart SIEM nội bộ** (user: "merge luôn vào omni đi, nó là tính năng có sẵn và
phải có của omni, không phải thêm tính năng mới") — theo đúng
`plans/finguard-to-smart-siem-merge-2026-08-04.md`:

- **S0** (dọn hệ ngoài chết): xóa 7 manifest (`omni-siem-bridge`/`hitl-dispatcher`/
  `evidence-adapter` + production, `finguard-customer-netpol`), xóa code
  (`hitl_dispatcher.py`, `siem_bridge.py`, `evidence_adapter/worker.py`), gỡ 10 Makefile
  target + 3 script + 1 runbook phụ thuộc. Bỏ nhánh "SIEM luôn suggest-only bất kể tier/risk" —
  đi chung ma trận tier×risk như 8 domain khác. Đổi hardcode `siem_source=="finguard"` →
  chấp nhận bất kỳ giá trị không rỗng, canonical mới `"omni_siem"`.
- **S1**: viết `src/remote_agent/collectors/security.py` — collector ĐẦU TIÊN cho domain
  `security` (2 probe: auth_failures qua `lastb`, privilege_escalation qua
  `journalctl _COMM=sudo`). Opt-in `OMNI_AGENT_SECURITY_ENABLED`.
- **S2**: gateway `agent_webhook.py` fan-out evidence domain=security+FAILED sang thêm
  `omni-siem-raw`. Vá luôn gotcha thật: `EvidenceItem` thiếu field `domain` khiến
  `domain_hint` collector tự khai bị Pydantic âm thầm bỏ.
- **S3**: drill thật trên VM lab `cust-edge` (không có sshd nên dùng probe sudo thay vì
  lastb) — tìm và vá **3 bug thật**:
  1. `_parse_sudo_lines` giả định sai định dạng log (bắt nhầm `"pam_unix(sudo:auth)"`
     làm username) — sửa đọc field `user=`/`ruser=` thật.
  2. Gateway gói `omni-siem-raw` bằng double-envelope `{"data": "..."}` giống
     `omni-diagnostic-evidence`, nhưng `decode_kafka_message` (port từ brain-go Go) đọc
     field PHẲNG — mọi message bị drop `missing_id_or_tenant`. Sửa bỏ double-envelope.
  3. Sửa file `.py` trên đĩa VM KHÔNG hot-reload process Python đang chạy — phải
     `systemctl restart` sau mỗi lần sửa, nếu không process cũ chạy code lỗi trong bộ nhớ
     (đã xảy ra thật, để lại rác `corr:ent:staging-sim:user:pam_unix` làm bằng chứng).
  Verify CUỐI: `corr:ent:staging-sim:user:siemdrilltest` (entity đúng trong Redis) +
  CRAT `ADVISORY_DECISION` ghi thật. Chain `omni-siem-chains` CHƯA hình thành (cần ≥2
  nguồn entity liên quan, đúng thiết kế — chưa test). `case_ledger`/`omni_admin.playbook`
  (0 dòng) để lại việc sau.
- **S4**: không cần làm gì thêm trong phạm vi phiên này — `omni_admin.playbook` 0 dòng
  là thiếu dữ liệu vận hành (seed playbook), không phải lỗi code; `PlaybookMatcher` trả
  `None` một cách hợp lệ khi chưa có playbook.

Domain `security` trong CLAUDE.md: ❌ → **⏳ có bằng chứng thật** (không phải ✅ đầy đủ —
xem bảng chi tiết trong `docs/audit/invariant_audit_2026-08.md` mục S3).

Dọn dẹp VM lab: xóa user tạm `siemdrilltest`, xóa file `.bak-s3drill`. Deploy qua build
#53-#56 SUCCESS (S0→S1+S2→fix parser→fix double-envelope, mỗi build verify riêng trước
khi push tiếp — đúng kỷ luật "1 commit = 1 concern" của plan gốc).

**Task list (11 + 8 task S0-S4 = 19 task): TẤT CẢ completed. Đ49 đóng hoàn toàn.**

## Đ48 — Bỏ hẳn `:latest`, Jenkins chỉ build+push, ArgoCD là bên deploy duy nhất — XONG, VERIFY SỐNG build #49 SUCCESS

**Bối cảnh:** Đ47 migrate Jenkins vào k3s xong nhưng vẫn còn nợ việc GỐC của cả phiên (user hỏi từ
đầu: tại sao Harbor/ArgoCD deploy sẵn mà không thực sự dùng để tag/rollout, pipeline vẫn tag
`:latest` + `kubectl rollout restart` tay). User chốt rõ 2 yêu cầu giữa phiên này: (1) "tôi không
chấp nhận việc gán latest, bắt buộc phải đánh version" — bỏ hẳn `:latest`, chỉ dùng git-SHA thật;
(2) "jenkins chỉ làm nhiệm vụ build, sau đó push lên harbor để ArgoCD deploy chứ nhỉ" — tách bạch
CI (Jenkins: test/build/push/tag) và CD (ArgoCD: deploy/rollout), không phải Jenkins tự
`kubectl apply`+`rollout restart` như trước.

### Đã sửa (Jenkinsfile + k8s/gitops/argocd-application.yaml), validated qua
`/pipeline-model-converter/validate` (Jenkins pod thật, PASS) + `kubectl apply --dry-run=client`
(argocd-application.yaml, PASS)

1. **`Build images` / `Push images to Harbor`**: bỏ hẳn mọi `docker build/push ...:latest`. Chỉ
   build/push `$IMAGE_TAG` = `git rev-parse --short HEAD`, ghi 1 lần vào `.image_tag` NGAY ĐẦU
   stage Build (trước khi bất kỳ stage nào commit ngược vào git) — mọi stage sau đọc file này thay
   vì tự `git rev-parse` lại, vì sau khi stage GitOps commit chạy, HEAD đã đổi sang commit MỚI,
   `git rev-parse HEAD` lúc đó sẽ trả sai SHA (không khớp image thật đã build/push).
2. **Stage mới `Update image tags in git (GitOps)`**: sed thay tag `:latest`/SHA cũ → `$IMAGE_TAG`
   trong 6 file (`omni-fullstack.yaml`, `omni-onboarding.yaml`, `aoip-portals.gcp.yaml`,
   `crat-integrity-check-cronjob.gcp.yaml`, `omni-gateway-rollout.yaml`, và có điều kiện
   `aoip-portals-web.yaml` khi `.build_ui` tồn tại), `git commit` + `git push` thẳng lên
   `gitea.cicd.svc.cluster.local` bằng credential Jenkins `gitea-hiendang` (không phải remote
   `origin` đã checkout — dùng URL tường minh kèm token để tránh phụ thuộc credential-helper).
   AN TOÀN không lặp vô hạn: job `omni-gcp-deploy` xác nhận **không có SCM trigger**
   (`<triggers/>` rỗng, xác nhận 2026-08-04, không đổi qua migrate Đ47) — push này không tự kích
   build mới.
3. **`Apply manifests` / `Deploy Argo Rollouts` / `Deploy portals + Dex`**: bỏ hẳn
   `kubectl apply`/`kubectl rollout restart`/patch `restartAt` cho 6 resource ArgoCD giờ quản
   (chỉ còn "tạo nếu chưa tồn tại" — bootstrap cluster mới trước khi ArgoCD tồn tại). Patch
   `restartAt` trên Rollout `omni-gateway` XOÁ HẲN — lý do tồn tại của nó (tag không đổi nên
   `kubectl apply` không tạo ReplicaSet mới) không còn đúng nữa khi mỗi build đều đổi tag thật.
4. **Stage mới `Wait for ArgoCD rollout`**: thay `kubectl rollout status --timeout=180s` từng
   Deployment bằng poll `kubectl get application omni-core -o jsonpath sync.status/health.status`
   sau khi `kubectl patch ... argocd.argoproj.io/refresh=hard`. Timeout nâng lên **300s** (không
   phải 180s) vì Rollout canary của `omni-gateway` có 2 bước `pause: {duration: 60}` cố ý — riêng
   phần chờ đã 120s, 180s không đủ margin cho canary chạy xong thật.
5. **`k8s/gitops/argocd-application.yaml`**: `selfHeal: true, prune: true` (trước `false/false`);
   chuyển `source:` đơn sang `sources:` (multi-source, ArgoCD 2.13 hỗ trợ) — source 1
   `path: k8s/deployments` include 5 file, source 2 `path: k8s/gitops` include
   `omni-gateway-rollout.yaml` (khác thư mục nên cần source riêng).
   `crat-integrity-check-cronjob.gcp.yaml` CỐ Ý không đưa vào ArgoCD (khác thư mục `k8s/jobs/`,
   CronJob tần suất thấp, không đáng thêm source thứ 3) — vẫn được Jenkins bump tag + `kubectl
   apply` trực tiếp như cũ.
6. **`post{failure{}}` — rollback logic tách 2 nhánh theo ai sở hữu resource**: 6 resource giờ do
   ArgoCD `selfHeal` quản lý (`omni-fullstack`, `omni-onboarding`, `aoip-provider-portal`,
   `aoip-tenant-portal`, `aoip-provider-web`, `aoip-tenant-web`, Rollout `omni-gateway`) — `kubectl
   rollout undo`/`kubectl argo rollouts undo` cho các resource này bị XOÁ khỏi rollback cũ vì
   selfHeal sẽ ĐÈ NGƯỢC lại undo đó ngay lần reconcile kế tiếp (undo làm live-state lệch khỏi git
   → selfHeal "sửa" nó về lại đúng cái commit lỗi). Rollback đúng kiểu GitOps: `git revert` chính
   commit tag-bump mà BUILD NÀY vừa push (SHA đọc từ `.gitops_commit_sha`, file này được `rm -f`
   ngay đầu stage Test mỗi build để không bao giờ lỡ revert nhầm commit của build TRƯỚC), rồi push
   — ArgoCD tự hội tụ về tag tốt cuối cùng. aoip-dex + Prometheus/Loki/Mimir/Grafana (không do
   pipeline này tag) vẫn giữ `kubectl rollout undo` như cũ.

### VERIFY SỐNG — build #49, 2026-08-10, SUCCESS (~10.4 phút)

- `Update image tags in git`: sed bump đúng 5 manifest (`omni-fullstack.yaml`,
  `omni-onboarding.yaml`, `aoip-portals.gcp.yaml`, `crat-integrity-check-cronjob.gcp.yaml`,
  `omni-gateway-rollout.yaml`) sang `e2afaf7`, `git commit` (detached HEAD `d964e1a`) + push thẳng
  lên `gitea.cicd.svc.cluster.local` bằng credential `gitea-hiendang` — log console xác nhận
  `e2afaf7..d964e1a HEAD -> main`. `aoip-portals-web.yaml` ĐÚNG THIẾT KẾ không bump lần này (build
  không đổi `ui/`, `.build_ui` không được tạo).
- `Wait for ArgoCD rollout`: patch hard-refresh → poll 35 lần (175s, trong ngưỡng 300s) →
  `sync=Synced health=Healthy`. Thấy rõ `health=Suspended` xen giữa (đúng dự đoán trong comment —
  Argo Rollouts canary `pause: {duration: 60}` của `omni-gateway` báo trạng thái đó qua ArgoCD).
- Verify trực tiếp qua `kubectl` (không suy đoán từ log): `omni-fullstack`, `omni-onboarding`,
  `aoip-provider-portal`, `aoip-tenant-portal`, `omni-gateway` — cả 5 pod đang chạy đều có
  `image: .../*:e2afaf7`, 0 restart. `kubectl get application omni-core` → `Synced Healthy`.
  `kubectl get rollout omni-gateway` → `Healthy`, image đúng tag.
- Local repo đã fetch+fast-forward `d964e1a` từ `gitea`, đã push tiếp sang `origin` (GitHub) —
  cả 2 remote khớp nhau, đúng convention 2 remote của repo này.
- `crat-integrity-check-cronjob.gcp.yaml` (không do ArgoCD track) — Jenkins tự `kubectl apply`
  trực tiếp trong "Deploy OrbStack-parity gaps" như thiết kế, log xác nhận `configured`.

### Còn lại (không chặn, việc phụ)

- `aoip-provider-web`/`aoip-tenant-web` vẫn `:latest` trong git + trên cluster — chờ lần build kế
  tiếp có đổi `ui/` mới chuyển sang tag thật (thiết kế cố ý, không phải nợ).
- CHƯA có cơ hội test đường `post{failure{}}` git-revert thật (build #49 không fail) — để dành khi
  có 1 build fail thật xảy ra tự nhiên, không nên cố tình phá build chỉ để test rollback.
- `CLAUDE.md` phần Jenkins/CI-CD nên cập nhật ngắn cho khớp kiến trúc mới (Jenkins = CI-only,
  ArgoCD = CD) — chưa làm, việc nhỏ.

## Đ47 — Migrate Jenkins vào k3s — ĐÃ CUTOVER XONG, production khoẻ

**Bối cảnh:** user hỏi tại sao Harbor/ArgoCD không thực sự dùng để tag/rollout image (đúng —
pipeline luôn `docker save | k3s ctr images import` + tag `:latest`, `kubectl rollout restart`
tay). Đề xuất ban đầu: tag git-SHA + push Harbor + `set image`. User chọn hẳn phương án lớn hơn:
Jenkins chạy trong pod k3s (network/DNS cluster thật) thay vì VM systemd, làm nền cho GitOps
đầy đủ sau này.

### Đã xong, đang sống
1. **Sự cố ArgoCD `omni-core` tự phát hiện + vá** (không liên quan yêu cầu gốc, tình cờ thấy):
   `ComparisonError("authentication required")` từ 2026-08-09 — Secret `omni-gitea-repo` (ns
   `argocd`) có password RỖNG. Root cause: `Jenkinsfile` cũ suy token từ `git remote get-url
   gitea`, nhưng Jenkins tự checkout bằng remote `origin` + `credentialsId: gitea-hiendang`
   (xác nhận qua `config.xml` thật), KHÔNG có remote `gitea` — lệnh fail, nhưng pipe thẳng vào
   `sed` (không `pipefail`) nên nuốt lỗi âm thầm, ghi Secret rỗng "thành công" mỗi build. Fix:
   patch Secret sống ngay (dùng token local đang hoạt động) + sửa Jenkinsfile dùng
   `withCredentials(gitea-hiendang)` thay vì parse. Verify sống: `Synced Healthy`. Commit
   `bf8df1a` (đã push cả 2 remote).
2. **Harbor admin password reset trực tiếp qua Postgres** — cả Secret `harbor-admin-bootstrap`
   lẫn giá trị trong `GCP_CREDENTIALS_2026-08-04.md` đều SAI (401, log harbor-core xác nhận
   "Invalid credentials" thật, không phải lock/network). User tự cho 1 giá trị nữa — VẪN sai.
   Root cause thật: không rõ (có thể rotate không đồng bộ ở lần security-sweep nào đó). Fix:
   đọc source Harbor v2.15.2 thật (`src/common/utils/encrypt.go` qua GitHub) xác nhận đúng
   scheme `pbkdf2_sha256` = PBKDF2-HMAC-SHA256, **600000 iterations** (không phải 10000 như lần
   đầu tôi đoán sai), dklen=16 byte → hex. Generate password mới + salt mới, `UPDATE harbor_user
   SET password=..., salt=... WHERE username='admin'` trực tiếp trên `harbor-database-0`. Verify
   sống: HTTP 200 `/api/v2.0/users/current`. Password mới đã ghi vào Secret
   `harbor-admin-bootstrap` VÀ `docs/handoffs/GCP_CREDENTIALS_2026-08-04.md` — file này ĐÃ
   tracked trong git từ trước (commit `2b85d4d`, không gitignore — pattern sẵn có của repo,
   không phải tôi đổi), nên commit password mới cùng handoff update này.
3. **Image `jenkins-controller:v1` build + push Harbor thành công.** Dockerfile mới:
   `docker/jenkins-controller/Dockerfile` (base `jenkins/jenkins:lts-jdk21` + kubectl v1.36.2 +
   helm v3.21.3 + istioctl 1.30.3 + docker CLI 29.7.1, khớp đúng version VM cũ — xác nhận qua
   Jenkins Script Console). Digest:
   `sha256:80ac53cdee9210b37cb486a7dd621d029775c72a7fdddee70f99228c983f598e`.
   Gotcha: `docker login` LUÔN thử HTTPS bất kể `insecure-registries` trong `daemon.json`
   (setting đó chỉ áp dụng cho daemon khi PUSH/PULL, không áp dụng cho lệnh `login` — 2 code path
   khác nhau trong Docker CLI) → bypass bằng ghi thẳng `~/.docker/config.json` với
   `{"auths":{"<ip>":{"auth":"<base64 admin:pw>"}}}`, không gọi `docker login`.
4. **PVC + ServiceAccount + ClusterRoleBinding + Deployment (DinD sidecar) đã apply** —
   `k8s/gitops/jenkins-incluster.yaml` (namespace `cicd`). `ClusterRoleBinding` cluster-admin
   (khớp quyền kubeconfig VM cũ đang có, single-tenant box, không scope hẹp hơn). DinD sidecar
   `docker:29-dind` privileged, `--insecure-registry=10.43.239.205` riêng cho sidecar (không cần
   sửa Docker host nữa về lâu dài). hostPort **8081** (KHÔNG phải 8080) — cố ý, để chạy song
   song với Jenkins VM cũ (vẫn đang là kênh Script Console tôi dùng để thao tác VM) cho tới khi
   verify xong mới cutover sang 8080.
5. **Data copy `/var/lib/jenkins` (483.8M) → PVC `jenkins-home` THÀNH CÔNG** — pod tạm
   `busybox` mount `hostPath:/var/lib/jenkins` (readOnly) + PVC, `cp -a`. Đã xác nhận có đủ
   `secrets/`, `credentials.xml`, `jobs/`, `secret.key`, `identity.key.enc` — tức là credential
   `gitea-hiendang` và toàn bộ job `omni-gcp-deploy` NÊN còn nguyên khi Jenkins pod mới đọc PVC
   này (CHƯA verify — xem BLOCKER).

### k3s restart — user tự chạy xong, node/cluster khoẻ
User tự `sudo tee /etc/rancher/k3s/registries.yaml` (mirror HTTP cho Harbor ClusterIP) +
`sudo systemctl restart k3s` trên VM. Verify sau restart: node `Ready`, không pod nào khác trên
cluster bị crash-loop do containerd restart (chỉ có 1 pod `svclb-istio-ingressgateway` Pending
từ trước, không liên quan). An toàn.

### 2 bug hạ tầng phát sinh SAU restart, đã tự phát hiện + vá cùng đợt
6. **UID/GID lệch sau copy** — pod `jenkins` `1/2 CrashLoopBackOff`, log
   `"missing rw permissions on JENKINS_HOME"`. Root cause: data copy (mục 5) giữ nguyên UID/GID
   gốc từ VM (`107:109`, user `jenkins` hệ thống Debian), nhưng image `jenkins/jenkins` chạy
   user `1000:1000`. Fix: `chown -R 1000:1000` toàn bộ PVC qua 1 pod debug tạm (root), xoá pod
   sau khi xong.
7. **DinD tự bật TLS mặc định** — `docker build`/`docker images` (kể cả lệnh không chạm registry)
   lỗi `"Client sent an HTTP request to an HTTPS server"`. Root cause: `docker:dind`'s entrypoint
   tự generate cert + bật TLS qua `DOCKER_TLS_CERTDIR` mặc định `/certs`, BẤT KỂ arg
   `--host=tcp://0.0.0.0:2375` tường minh. Fix: set `env: DOCKER_TLS_CERTDIR=""` trên container
   `dind` — cách chính thức Docker tài liệu hoá để tắt hẳn auto-TLS.
8. **insecure-registry match theo string, không theo IP đã resolve** — push bằng DNS name
   (`harbor.harbor.svc.cluster.local`) vẫn bị coi là HTTPS dù IP `10.43.239.205` đã có trong
   `--insecure-registry`, vì Docker match chuỗi TRƯỚC khi resolve DNS. Fix: liệt kê CẢ HAI dạng
   (IP và DNS name) trong `--insecure-registry` của sidecar `dind`.

### VERIFY SỐNG — đã xác nhận đầy đủ (không chỉ "rollout thành công")
- `kubectl get pods -n cicd -l app=jenkins` → `2/2 Running`.
- `curl http://100.67.117.19:8081/login` → `200`.
- API `/api/json` → job `omni-gcp-deploy` còn nguyên (kèm build history cũ, thấy cả build #42).
- Credential `gitea-hiendang` (Username-password) còn đọc được qua API.
- `kubectl exec ... -- kubectl get ns` → chạy được qua ServiceAccount trong pod (đã bỏ kubeconfig
  cũ trỏ `127.0.0.1:6443` — file đó chỉ đúng khi Jenkins chạy trực tiếp trên node, sai trong pod;
  đã `mv` sang `.bak`, không xoá).
- `getent hosts harbor.harbor.svc.cluster.local` → resolve ra ClusterIP thật — **đây là bằng
  chứng trực tiếp cho mục tiêu gốc của việc migrate** ("thông network", không có được khi Jenkins
  chạy trên VM host).
- `docker build` (qua DinD, pull base image từ docker.io) → thành công.
- `docker push` tới `harbor.harbor.svc.cluster.local/library/...` → chạm đúng Harbor qua HTTP,
  chỉ báo thiếu credential (`no basic auth credentials`) — đúng hành vi kỳ vọng, chưa cấu hình
  login cho test thủ công này, KHÔNG phải lỗi.

### Việc gốc CHƯA làm — Harbor git-SHA tag + ArgoCD selfHeal (task #16)
Đây là câu hỏi ban đầu của user ("CI/CD có full harbor, argocd, phải đánh tag image và cập nhật
trên k3s chứ?") — migrate Jenkins vào cluster chỉ là NỀN TẢNG (network/DNS thật) để làm việc này,
CHƯA phải bản thân việc đó. Hiện tại image vẫn tag `:latest` cố định, `kubectl rollout restart`
tay để force pull. Việc còn lại, session sau làm:
1. Jenkinsfile: tính `IMAGE_TAG=$(git rev-parse --short HEAD)`, build/push CẢ `:latest` (giữ
   tương thích) LẪN `:$IMAGE_TAG` lên Harbor.
2. Cập nhật `image:` trong 6 manifest đã sửa Đ47 (`omni-fullstack.yaml`, `omni-onboarding.yaml`,
   `omni-gateway-rollout.yaml`, `aoip-portals.gcp.yaml`, `aoip-portals-web.yaml`,
   `crat-integrity-check-cronjob.gcp.yaml`) sang tag `$IMAGE_TAG`, `git commit` NGAY TRONG pipeline
   (Jenkins pod đã cluster-admin, chỉ cần `git config user.email/name` + push qua credential
   `gitea-hiendang` đã có sẵn).
3. Bật `selfHeal: true, prune: true` trên Application `omni-core`
   (`k8s/gitops/argocd-application.yaml`) — ArgoCD giờ mới thật sự là nguồn sự thật, không chỉ
   drift-detector. Cân nhắc mở rộng `directory.include` để phủ luôn
   `omni-gateway-rollout.yaml`/portal manifest thay vì chỉ 3 file hiện tại.
4. Bỏ hẳn `kubectl rollout restart` thủ công cho các deployment này — thay đổi tag ảnh tự nhiên
   trigger rollout thật, không cần force nữa.

### Việc khác trong phiên (không liên quan Jenkins migrate)
- ⚠️ **Tự phát hiện + báo ngay:** đầu phiên, 1 lệnh `sed` mask lỗi làm lộ password Jenkins VM
  cleartext trong transcript (regex không khớp format `**Password**:`). Đã báo user, khuyến
  nghị đổi password Jenkins — **VẪN CHƯA XÁC NHẬN user đã đổi hay chưa, nhắc lại đầu phiên sau.**
  (Không còn quan trọng bằng trước vì VM Jenkins đã tắt hẳn, nhưng account `hiendang` trên VM vẫn
  dùng password đó cho SSH/sudo nếu có — vẫn nên đổi.)

### Files changed (Đ47) — ĐÃ COMMIT + PUSH ĐẦY ĐỦ (4 commit, cả gitea+origin)
`bf8df1a` → `c89e8e9` → `3ffa38a` → `fc63a8b`. Không còn gì treo chưa commit từ Đ47.
- `Jenkinsfile`: fix credential ArgoCD (`withCredentials`), build→push Harbor (bỏ hẳn
  `sudo k3s ctr images import`), stage "Push images to Harbor" mới.
- `docker/jenkins-controller/Dockerfile`: image controller (kubectl/helm/istioctl/docker CLI/
  python3/PyYAML) — build thật trên Harbor `10.43.239.205/library/jenkins-controller:v3`.
- `k8s/gitops/jenkins-incluster.yaml`: Deployment 2 container (jenkins+dind) + PVC + RBAC +
  Service NodePort `30080`.
- `k8s/deployments/omni-fullstack.yaml`, `omni-onboarding.yaml`, `aoip-portals.gcp.yaml`,
  `aoip-portals-web.yaml`, `k8s/gitops/omni-gateway-rollout.yaml`,
  `k8s/jobs/crat-integrity-check-cronjob.gcp.yaml`: `image:` trỏ Harbor ClusterIP,
  `imagePullPolicy: Always` (trừ cronjob dùng `IfNotPresent`).
- `docs/handoffs/GCP_CREDENTIALS_2026-08-04.md`: password Harbor mới (đã tracked git từ trước).
- `CLAUDE.md`: cập nhật kiến trúc Jenkins (không còn "systemd trên VM" nữa).

### Verify cutover cuối cùng (đã xác nhận sống, không chỉ tin log)
- Build #48: SUCCESS end-to-end trên Jenkins in-cluster (build→push Harbor→apply→rollout).
- `omni-fullstack`/`omni-onboarding`/`omni-gateway`/portal pods: `2/2 Running`, image field trỏ
  đúng Harbor ClusterIP.
- `omni-gateway` `/healthz` 200, log cho thấy đang xử lý traffic AGENT THẬT
  (`staging-sim_cust-edge/cust-app/cust-db`) — không phải giả lập.
- VM `jenkins.service`: `stop` + `disable`, port 8080 không còn phản hồi (`HTTPCODE:000`).
- Jenkins in-cluster NodePort `:30080`: `200`, pod `2/2 Running`, không crash-loop.
- `kubectl get pods -n multi-agent --field-selector=status.phase!=Running,!=Succeeded`: rỗng —
  không pod nào bất thường sau toàn bộ quá trình.

### Gotcha tổng hợp Đ47 (đọc trước khi động vào Jenkins-in-cluster lần sau)
1. ArgoCD auth broken do bug parse token trong Jenkinsfile cũ (pipe nuốt lỗi, không `pipefail`).
2. Harbor admin password không khớp bất kỳ nguồn lưu trữ nào — phải reset qua Postgres trực tiếp
   (`pbkdf2_sha256`, 600000 iterations, dklen=16 — xem Harbor `src/common/utils/encrypt.go`).
3. `docker login` luôn thử HTTPS bất kể `insecure-registries` — dùng thẳng `~/.docker/config.json`.
4. `insecure-registry` match theo STRING trước khi resolve DNS — cần khai cả IP lẫn DNS name.
5. `docker:dind` tự bật TLS mặc định (`DOCKER_TLS_CERTDIR`) — phải set rỗng tường minh.
6. Copy data giữ nguyên UID/GID gốc — phải `chown` lại khớp UID image mới.
7. kubeconfig copy từ VM trỏ `127.0.0.1:6443` — sai trong pod, xoá để dùng ServiceAccount.
8. Job Jenkins tự cấu hình git URL `localhost:30300` (đúng trên VM, sai trong pod) — sửa qua
   `config.xml` API sang `gitea.cicd.svc.cluster.local:3000`.
9. DinD là container/daemon RIÊNG — bind-mount (`docker build -v $(pwd):/repo`) cần PVC workspace
   mount vào CẢ HAI container, không chỉ container `jenkins`.
10. MTU lồng nhau: pod `eth0` (flannel VXLAN) 1410 vs `docker0` mặc định 1500 — gói lớn bị rớt
    âm thầm (DNS/gói nhỏ vẫn qua được, đánh lừa chẩn đoán ban đầu). Set `--mtu=1400` cho dockerd.
11. Image controller thiếu `python3`/`python3-yaml` — 2 chỗ trong pipeline (`vault-bootstrap.sh`,
    Grafana apply) cần, lỗi rất khác nhau (1 cái âm thầm fallback sai, 1 cái lỗi rõ ràng).

## Đ46 — Verify sống P0+P1 THÀNH CÔNG (build #40), + 1 sự cố tự gây do chính P0 #1 đã vá cùng đợt

**Build #40 SUCCESS** (464s, build #39 trước đó cũng SUCCESS 475s nhưng thiếu code bearer-token —
xem sự cố dưới). `omni-fullstack`/`omni-gateway` đều rolled out, symbol P0/P1 xác nhận có thật
trong pod đang chạy (không chỉ tin "rollout successful"):
- `diagnosis_loop._AGENT_ONLINE_MAX_AGE_S = 150.0`, `_AGENT_REGISTRY_TTL_SEC = 300`
- `evidence_consumer` có `ERR_ALERT_CLASS_READ_FAILED`
- `remote_agent_pipeline.REMOTE_Z_THRESHOLD = 3.0`
- `gateway.api` có `_take_rate_limit_token`, `_MAX_RATE_LIMIT_KEYS=500`, `_verify_webhook_auth`
- `case_ledger.advocacy._MAX_CONCURRENT_PATTERN_FETCHES = 4`

**Verify hành vi sống bằng incident thật** (không chỉ đọc symbol):
- Inject alert `OmniAdvisoryAcceptanceRateLow` qua Alertmanager thật → log xác nhận đúng chuỗi:
  `alert_class=meta_self` → `mutate_eligible=false` → `SUGGEST_REMEDIATION` deterministic,
  KHÔNG qua LLM/mutate, đúng thiết kế P0 #2.
- Unauthenticated POST tới `https://gateway.omnisre.xyz/webhook/prometheus` từ ngoài → **401**
  (đúng, không làm yếu bảo mật P0 #1).
- Alert từ Alertmanager nội bộ (đã có bearer token) → **200** (đường hợp lệ không còn bị chặn).

### Sự cố tự gây — P0 #1 chặn nhầm Alertmanager nội bộ, đã phát hiện+vá+verify cùng session
Sau khi build #39 deploy, `kubectl get ingress` xác nhận `omni-gateway` **thật sự lộ ra Internet**
qua `gateway.omnisre.xyz` (đúng threat model finding #1) — NHƯNG cùng route đó cũng là đường
Alertmanager nội bộ gửi self-monitoring alert, và Alertmanager `webhook_configs` **không có khả
năng tự ký HMAC** (chỉ hỗ trợ bearer token tĩnh qua `authorization.credentials_file`). Fail-closed
đúng ý nhưng chặn nhầm luôn đường hợp lệ duy nhất — mọi alert (kể cả self-monitoring) bị 503 một
thời gian ngắn giữa build #39 và #40.

Fix (commit `6958476`): `_verify_webhook_auth()` (đổi tên từ `_verify_hmac_signature`) chấp nhận
HMAC HOẶC bearer token `OMNI_ALERTMANAGER_WEBHOOK_TOKEN`. Hạ tầng sửa trực tiếp trên cluster:
- `vault kv patch secret/omni-gateway-secret OMNI_ALERTMANAGER_WEBHOOK_TOKEN=...` (giữ nguyên
  `OMNI_GATEWAY_API_KEY`).
- `k8s/gitops/omni-gateway-external-secret.yaml` map thêm key (namespace `multi-agent`).
- **Gotcha phát hiện live**: mount thẳng `omni-gateway-secret` vào pod alertmanager (namespace
  `monitor`) làm pod treo `ContainerCreating` vĩnh viễn — "secret ... not found" — Secret
  **không cross-namespace được**. Fix: ExternalSecret RIÊNG
  `k8s/gitops/alertmanager-webhook-token-external-secret.yaml` (namespace `monitor`), cùng
  property Vault, một nguồn sự thật, 2 Secret ở 2 namespace.

### Còn treo
- P1 #5 (blocking `psutil.cpu_percent`) chạy trên `src/remote_agent/` — process trên VM khách
  hàng, KHÔNG nằm trong cluster GCP này, chưa verify sống (cần deploy riêng lên VM lab, ngoài
  phạm vi build Jenkins hiện tại).
- **P2** (8 mục MEDIUM, đặc biệt #8 RBAC scope + #9 credential_source_of_truth governance) —
  chưa bắt đầu, cần bàn thiết kế trước.
- **CI/CD architecture gap** (user hỏi trực tiếp, chưa làm): Harbor + ArgoCD đã deploy đầy đủ
  nhưng **chưa nối vào luồng deploy thật** — image build xong `docker save | k3s ctr images
  import` thẳng, bỏ qua Harbor hoàn toàn; luôn tag `:latest` nên phải `kubectl rollout restart`
  tay thay vì rollout theo tag/digest đổi; ArgoCD Application `omni-core` cố tình
  `selfHeal:false, prune:false`, chỉ là drift-detector, Jenkins vẫn là nguồn sự thật duy nhất
  cho rollout (ghi rõ trong comment `argocd-application.yaml`, không phải oversight nhưng cũng
  chưa fix). Đề xuất: build → tag git-SHA → push Harbor → ArgoCD sync theo tag — việc lớn, cần
  quyết định riêng.



---

> Lịch sử checkpoint trước Đ45: xem `docs/handoffs/archive/SESSION_ARCHIVE_2026-08.md`.
