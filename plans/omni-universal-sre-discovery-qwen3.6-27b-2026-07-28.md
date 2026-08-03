# Plan: Omni → Qwen3.6:27b + Universal SRE Discovery (không chỉ K8s)

**Ngày:** 2026-07-28
**Trạng thái:** PLAN ONLY — chưa code, chưa chạy phase nào.
**Input gốc:** `fpt-loyalty-sre-compat-report.md` + `fpt_loyalty_topology.html` — **CHỈ LÀ 1 CASE
STUDY THAM KHẢO**, không phải target kiến trúc. FPT Loyalty là ví dụ mẫu để trích ra method luận
(cách Claude đóng vai SRE khảo sát read-only 1 hệ thống khách hàng chưa từng biết). Mục tiêu thật
của plan này **KHÔNG phải** "làm cho Omni hiểu FPT Loyalty" mà là: Omni (đang vận hành đa tenant —
xem `AdminConfigRepo.create_tenant()`, `tenant_readiness_state`) phải có sẵn **capability khảo sát
này cho BẤT KỲ khách hàng thứ N nào** được onboard, không riêng K8s, không riêng 1 kiến trúc cụ
thể — đúng như Claude đã làm được cho FPT Loyalty mà trước đó không có tài liệu nào.

## 0. Khung nhìn multi-tenant (N khách hàng) — ràng buộc bao trùm toàn bộ plan

Đã xác minh trong code: Omni **đã có sẵn** cách ly theo tenant xuyên suốt, không phải xây mới:
- System Twin theo tenant: `omni:aoip:system_model:{tenant}` (Redis key có `tenant_id`).
- Discovery snapshot theo tenant: `src/remote_agent/discovery.py` — `_snapshot_key(tenant_id, agent_id)`,
  `bump_suspect_streak(tenant_id, agent_id)`.
- Emitter gắn `tenant_id` vào mọi envelope (`src/remote_agent/emitter.py`).
- Provisioning tenant qua `AdminConfigRepo.create_tenant()` / `POST /autonomy/tenants` (PG
  `omni_admin.tenant_readiness_state`) — invariant đã ghi trong CLAUDE.md: agent onboarding cho
  tenant nào PHẢI qua provision trước, thiếu bước này gây FK violation (post-mortem
  `drift-correction-2026-07-02.md`).
- Invariant `INV_NAMESPACE_ISOLATION` đã tồn tại và được nhiều module tham chiếu
  (`evidence_consumer.py`, `agent/delivery.py`, `diagnostic_policy.py`, `reason_codes.py`).

**Nguyên tắc bắt buộc cho mọi phase P0-P5 dưới đây:**
1. Mọi Fact/node Knowledge Graph do collector mới sinh ra PHẢI gắn `tenant_id`, KHÔNG BAO GIỜ
   trộn dữ liệu 2 tenant vào cùng 1 graph/report — vi phạm sẽ là lỗ hổng rò rỉ thông tin giữa các
   khách hàng, nghiêm trọng hơn nhiều so với bug thông thường.
2. `same_codebase_as` (mục 4.2) chỉ so khớp fingerprint **trong cùng 1 tenant** — 2 khách hàng
   khác nhau dùng chung 1 sản phẩm thương mại (ví dụ cùng dùng Ocelot gateway như FPT) là trùng
   hợp bình thường, KHÔNG được suy ra bất kỳ liên hệ nào giữa 2 tenant.
3. Qwen3.6:27b là **tài nguyên dùng chung** (1 Ollama host phục vụ N tenant) — capacity phải tính
   theo N phiên khảo sát đồng thời tiềm năng, không phải 1 khách hàng như case study mẫu. Đây là
   lý do P0 (benchmark) không chỉ đo chất lượng câu trả lời mà còn phải đo throughput dưới tải
   nhiều tenant song song (xem P0 sửa lại ở mục 5).
4. Method luận (rộng→hẹp, không tin tên gọi, gắn confidence, phân loại sống/chết) là
   **capability chung** áp dụng như nhau cho mọi tenant — không hard-code bất kỳ chi tiết riêng
   của FPT Loyalty (tên service, domain, stack .NET/NestJS cụ thể) vào code. Case study chỉ dùng
   để viết test/fixture mẫu, không phải để hard-code logic.

## 1. Việc Claude đã làm — bóc tách thành method luận tái dùng được

Đọc kỹ mục 10 của báo cáo (`Phương pháp luận khảo sát`), đây không phải là 1 lần LLM "trả lời câu
hỏi" mà là **1 ReAct loop khảo sát nhiều bước có chủ đích**, với đặc điểm:

1. **Từ rộng vào hẹp**: liệt kê toàn bộ tài nguyên trước (`get deploy,svc,ingress -o wide`), rồi
   chọn lọc service có tên gợi ý vai trò trung tâm để đào sâu.
2. **Không tin tên gọi** — luôn xác nhận bằng bằng chứng runtime thật:
   - Runtime thật = đọc `/proc/1/cmdline` bên trong container, không suy từ tên image.
   - API surface thật = đọc Swagger JSON đang chạy, hoặc grep decorator trên bundle đã build
     (`Controller\)\(...`), hoặc gọi `/discover` (Restate manifest), hoặc liệt kê DLL (.NET không
     decompile được thì dùng tên assembly làm tín hiệu).
   - Trùng lặp codebase = so `package.json.name` giữa các Deployment khác tên — phát hiện quan
     trọng nhất (nhiều Deployment K8s hoá ra cùng 1 image, đổi biến môi trường).
3. **Cross-link bằng grep có mục tiêu**: quét hậu tố biến môi trường (`_ENDPOINT`, `_URL`, `HOST`,
   `BROKERS`) trong ConfigMap để dựng bảng "service A gọi service B" mà không cần đọc code.
4. **Phân loại sống/chết**: `READY=0/0` → loại khỏi phạm vi phân tích compat, chỉ ghi nhận tồn tại.
5. **Gắn nhãn độ tin cậy cho MỌI phát hiện**: `[XÁC NHẬN]` (đọc trực tiếp giá trị đang chạy) vs
   `[SUY LUẬN]` (suy từ tên/vị trí). Không có mục nào là phỏng đoán trần trụi.
6. **Kỷ luật an toàn tuyệt đối**: chỉ lệnh đọc (`get/describe/logs/exec cat|ls|grep|curl 127.0.0.1`);
   không `apply/patch/delete/scale`; không gọi route nghiệp vụ `POST` có side-effect dù đã biết
   path; đọc được secret thật thì **không chép giá trị vào báo cáo**, chỉ ghi nhận rủi ro.
7. **Sản phẩm đầu ra kép**: (a) báo cáo văn bản có bảng bằng chứng, (b) topology diagram
   (Mermaid flowchart + sequence diagram) render từ đúng cấu trúc đã xác nhận.

Đây chính xác là hình mẫu Omni cần tái tạo tự động, không phải chỉ cho K8s.

## 2. Trạng thái Omni hiện tại — đã có nền, chưa đủ tổng quát

Đã xác minh trong code (không suy đoán):

- `src/aoip/system_graph.py` — đã có Knowledge Graph tổng quát (node type: service/host/container/
  port/api/database/queue/secret/firewall/team/runbook/business_capability), builder **không đọc
  nội dung file**, chỉ nhận HINT đã tách sẵn từ collector (đúng nguyên tắc INV_NO_DATA_EXFIL).
- `src/remote_agent/discovery.py` + `src/remote_agent/collectors/` (`services.py`, `system.py`,
  `database.py`, `storage.py`, `logs.py`, `api_contract.py`, `k8s.py`) — đã có discovery cho VM
  ngoài K8s (`_SERVICE_LOG_HINTS`/`_SERVICE_CONFIG_HINTS` theo tên process: mysql/nginx/postgres/
  redis/kafka/haproxy/proxysql/zabbix), chạy lại mỗi 1h.
- `src/aoip/onboarding_projection.py`, `understanding.py`, `competency_matrix.py` — tầng
  tổng hợp/hỏi-đáp trên Knowledge Graph đã tồn tại (Productization Iteration 19-23: trang
  `/understanding`, Mermaid diagram, diff lịch sử).
- `src/remote_agent/command_executor.py` — đã có allowlist lệnh đọc (dpkg/mysqladmin/ip/ps...) với
  path-bypass guard (audit follow-up 2026-07-22, commit `c7a1ed1`).

**Khoảng trống so với case study Claude:**

| Method Claude dùng | Omni hiện có gì | Gap |
|---|---|---|
| Đọc `/proc/1/cmdline` xác nhận runtime thật | `collectors/system.py` đọc process list, nhưng chưa map "process → runtime fingerprint" có cấu trúc | Cần chuẩn hoá 1 bước "runtime identification" tường minh, output có trường `confidence` |
| Grep Swagger/route/bundle theo từng ngôn ngữ (Node/. NET/Java) | Chưa có collector nào đọc API surface bên trong app | Thiếu hẳn "API surface discovery" — đây là phần tạo giá trị nhất trong báo cáo mẫu |
| So `package.json.name` phát hiện trùng codebase giữa Deployment khác tên | `system_graph.py` có node type nhưng chưa có step "dedupe theo fingerprint" | Thiếu bước "codebase fingerprint cross-reference" |
| Gắn nhãn `[XÁC NHẬN]`/`[SUY LUẬN]` cho từng fact | `aoip.objects.Fact` có `confidence` field (theo `system_graph.py` dùng `Fact`) — cần xác nhận đã render ra UI/report chưa | Cần audit xem confidence có tới được output cuối (report/diagram) hay bị rớt giữa đường |
| Output kép: báo cáo + Mermaid diagram | Có `/understanding` UI với Mermaid (Iteration 22) | Đã có, nhưng là artifact online-only; chưa có "báo cáo Markdown" xuất được như file mẫu |
| ReAct loop tự lái nhiều bước khảo sát (không phải 1 lần hỏi–đáp) | `deep_scout.py` (core role) có ReAct loop cho K8s cluster discovery | Deep Scout hiện là **K8s-only** (tên gọi + vị trí gợi ý); cần tổng quát hoá sang remote-host/VM bundle giống cách `remote_agent_pipeline.py` đã làm cho lane SYS_HARD_FAIL |
| An toàn tuyệt đối: chỉ đọc, không log secret value | `command_executor.py` đã có allowlist đọc | Cần audit riêng: có path nào vô tình đẩy nội dung file/secret vào RAG/Redis không (liên hệ `INV_DATA_RESIDENCY` đã có trong CLAUDE.md) |

## 3. Đổi model: qwen2.5-coder:7b → Qwen3.6:27b

**Vị trí cấu hình cần đổi** (đã xác minh, 4 field trong `src/workers/settings.py:751-755` +
`k8s/deployments/omni-worker-configmap.yaml:113-124`):

```
VLLM_MODEL, OMNI_CHAT_MODEL, OMNI_MODEL_REASONING_ENGINE, OMNI_MODEL_HEAVY_LIFTER,
OMNI_MODEL_HELPER, OMNI_DIAG_EVIDENCE_LLM_MODEL, OMNI_AUTONOMOUS_DECIDER_MODEL
```

**Rủi ro/điều kiện tiên quyết cần xử lý trước khi đổi, không đổi mù:**

1. **VRAM/RAM**: 27b (~16-18GB ở Q4) vs 7b (~5GB) — host Ollama là `host.orb.internal` (Apple
   Silicon, unified memory). Phải benchmark tốc độ token/s thật trên máy host trước, không giả định.
   `OMNI_OLLAMA_NUM_PARALLEL` (đã có ghi chú "lệch slot Redis ↔ Ollama → treo/timeout" trong
   configmap) — số lượng parallel slot phải giảm khi model nặng hơn, nếu không giữ nguyên sẽ
   tràn bộ nhớ dưới tải đồng thời (nhiều lane gọi LLM song song).
2. **`OMNI_LLM_NUM_CTX`** (default 8192, `settings.py:1768`) — nếu dùng model để đọc route/config
   dài (giống Claude đọc cả `dist/main.js` bundle 896KB), 8192 token là quá nhỏ. Cần đánh giá tăng
   `num_ctx` riêng cho tác vụ discovery (tương tự cách đã tách `proactive_llm_num_ctx` thấp hơn
   main — làm ngược lại: 1 profile num_ctx CAO hơn dành riêng cho discovery/topology-synthesis).
3. **Benchmark gate bắt buộc trước khi đổi production**: `make benchmark-advisory` đã tồn tại
   (100-điểm rubric, pass=70, theo `docs/handoffs` cũ ghi 30.4%→43.5%/63.5→69.7 cho advisory
   prompt). Chạy benchmark này với qwen3.6:27b trước/sau, không suy đoán "model to hơn = tốt hơn".
4. **Không đổi `embed_model` (`nomic-embed-text`, 768-dim)** — đây là model riêng cho RAG, đổi
   `OMNI_CHAT_MODEL` không ảnh hưởng, tránh nhầm lẫn đổi luôn embed model gây vỡ toàn bộ HNSW index
   hiện có (`omni:rag:sop` HLEN=1019).
5. **Rollout theo role, không đổi toàn bộ 1 lần**: đổi `OMNI_MODEL_HELPER` (tier nhẹ) trước, quan
   sát latency/lỗi timeout trong lab 24-48h, rồi mới đổi `OMNI_MODEL_REASONING_ENGINE`/
   `OMNI_CHAT_MODEL` (đường advisory chính, ảnh hưởng CRAT/Telegram thật).

## 4. Kiến trúc đề xuất — "Universal SRE Discovery Loop"

Không xây mới từ đầu — mở rộng 3 lớp đã có sẵn đúng ranh giới hiện tại:

### 4.1 Lớp thu thập (collector) — mở rộng `src/remote_agent/collectors/`
Thêm 1 collector mới `api_surface.py` (đặt cạnh `api_contract.py` hiện có — kiểm tra không trùng
trách nhiệm trước khi tạo file), tái tạo đúng Bước 4 trong playbook Claude:
- Runtime fingerprint: đọc `/proc/<pid>/cmdline` → phân loại `node|dotnet|java|python|go` theo
  token đầu, KHÔNG suy từ tên image/Deployment.
- Theo runtime, thử lần lượt (đọc-only, timeout ngắn, im lặng khi fail — đúng pattern `_run()` đã
  có trong `discovery.py`):
  - Node: `curl 127.0.0.1:<port>/api/docs/swagger.json` (và biến thể path phổ biến), nếu fail thì
    grep decorator trên file bundle nếu tìm thấy trong `/proc/<pid>/cwd`.
  - .NET: liệt kê `*.dll` trong working dir → tên assembly làm tín hiệu vendor/module.
  - Java: thử `curl 127.0.0.1:<port>/discover` với header Restate manifest; thử actuator
    `/actuator/health` (không actuator mở toang — ghi nhận đóng là "thực hành tốt" giống báo cáo
    mẫu, không phải lỗi).
  - Mọi trường hợp: đọc `package.json`/`*.csproj`/`pom.xml` chỉ lấy field `name`/`AssemblyName` —
    dùng để **fingerprint dedupe** (Deployment khác tên, cùng fingerprint → 1 node trong graph,
    không phải 2).
- **INV_NO_DATA_EXFIL giữ nguyên**: chỉ trả về path/tên/kích thước/danh sách route (method+path),
  KHÔNG BAO GIỜ trả nội dung response body thật hay giá trị secret đọc được trong config.

### 4.2 Lớp tổng hợp (Knowledge Graph) — mở rộng `src/aoip/system_graph.py` + `Fact.confidence`
- Xác nhận (đọc code) field `confidence` trên `Fact` đã được set đúng 2 giá trị tương đương
  `[XÁC NHẬN]`/`[SUY LUẬN]` chưa; nếu chưa có phân biệt rõ, thêm enum
  `FactConfidence.CONFIRMED | FactConfidence.INFERRED` — đây là thay đổi nhỏ nhưng chính là
  nguyên tắc cốt lõi khiến báo cáo mẫu đáng tin.
- Thêm predicate mới vào `RELATIONAL_PREDICATES` (nếu chưa có) cho quan hệ "cùng fingerprint":
  `same_codebase_as` — dựng đúng bảng mục 8.3 trong báo cáo mẫu ("nếu Omni suy luận theo tên
  Deployment sẽ sai") một cách tự động thay vì phải người đọc thấy trùng tên package thủ công.
- Thêm node type `frozen_component` (READY 0/0) — tách khỏi vòng phân tích compat mặc định, giữ
  nguyên tinh thần mục 7 báo cáo mẫu (không tốn effort phân tích phần đã đóng băng).

### 4.3 Lớp điều phối (ReAct loop) — tổng quát hoá `deep_scout.py`
- Hiện `deep_scout.py` là K8s-only theo tên. Không viết lại — tách phần **chiến lược khảo sát**
  (rộng→hẹp, ưu tiên service tên gợi ý vai trò trung tâm, dừng khi đủ % coverage) thành 1 policy
  dùng chung, còn phần **thực thi lệnh** đã tách theo backend sẵn (K8s qua `collectors/k8s.py`,
  VM qua `discovery.py`+`collectors/`). Đây đúng tinh thần AOIP `backends.py`/`remote_linux_backend.py`
  đã có (đa backend, cùng 1 primitives layer) — không phát minh kiến trúc mới, chỉ lắp API-surface
  collector vào cùng đường ống.
- Output kép giữ nguyên hướng đã có: (a) cập nhật System Twin (`omni:aoip:system_model:{tenant}`)
  — machine-readable, (b) sinh báo cáo Markdown + Mermaid HTML theo đúng mẫu 2 file user vừa đưa
  — đây là phần **mới hoàn toàn**: hiện `/understanding` chỉ render trực tiếp trên UI, chưa có
  endpoint "export báo cáo tĩnh" như file mẫu. Thêm 1 export step tái dùng chính Jinja/string
  template dựng lại đúng cấu trúc mục 1-10 của báo cáo mẫu, dữ liệu lấy từ Knowledge Graph +
  `Fact.confidence`.

### 4.4 An toàn — không nới lỏng invariant nào có sẵn
- Universal discovery **vẫn chỉ chạy qua** `command_executor.py` allowlist đã audit
  (`c7a1ed1`) — API-surface collector mới không tự thêm quyền exec ngoài allowlist, chỉ thêm entry
  đọc mới (`curl 127.0.0.1:*`, `cat *.dll`/`package.json` listing) vào đúng allowlist hiện có.
- Không gọi bất kỳ route nghiệp vụ `POST/PUT/DELETE` nào dù phát hiện được path — giữ nguyên ràng
  buộc mục "Ràng buộc đã tuân thủ" trong báo cáo mẫu, mã hoá thành 1 test cố định
  (`test_remote_agent_e2e.py` theo `remote-agent-test` skill: chỉ test qua real emitter/collector,
  không mock nội bộ) khẳng định collector mới không bao giờ gọi method ghi.
- Secret value đọc được (vd tìm thấy ClientSecret plaintext trong appsettings.json) → chỉ ghi
  **sự tồn tại của rủi ro** vào Fact/report, không bao giờ đưa giá trị thật vào Redis/RAG — đúng
  `INV_DATA_RESIDENCY` đã ghi trong CLAUDE.md, cần thêm 1 test khẳng định giá trị match pattern
  secret-like (entropy cao, tên field `*Secret*`/`*Password*`/`*Token*`) bị redact trước khi ghi
  Fact, không chỉ dựa vào "prompt bảo LLM đừng chép".

## 5. Phase hoá (đề xuất, KHÔNG tự chạy — chờ user duyệt từng phase)

| Phase | Nội dung | Rủi ro nếu bỏ qua thứ tự |
|---|---|---|
| **P0 — Benchmark model** | Chạy `make benchmark-advisory` với qwen3.6:27b trên lab, so sánh điểm + latency + VRAM thật với qwen2.5-coder:7b hiện tại. **Đo thêm throughput dưới N request song song** (mô phỏng nhiều tenant khảo sát đồng thời, không chỉ 1 request tuần tự) — `OMNI_OLLAMA_NUM_PARALLEL` hiện tại có thể không đủ khi model nặng gấp ~4x. KHÔNG đổi configmap production tới khi có số liệu. | Đổi mù → có thể advisory chậm hơn/tệ hơn, không ai biết vì thiếu baseline; nếu chỉ benchmark 1 request thì bỏ sót đúng rủi ro thật khi scale N tenant |
| **P1 — Đổi model theo role, thấp rủi ro trước** | `OMNI_MODEL_HELPER` trước, quan sát lab 24-48h, rồi `OMNI_MODEL_REASONING_ENGINE`/`OMNI_CHAT_MODEL`/`OMNI_DIAG_EVIDENCE_LLM_MODEL`/`OMNI_AUTONOMOUS_DECIDER_MODEL`. Đồng thời tune `OMNI_OLLAMA_NUM_PARALLEL` theo VRAM thật đo ở P0. | Đổi hết 1 lần → nếu lỗi không biết role nào gây ra, khó rollback có mục tiêu |
| **P2 — API-surface collector** | Thêm `collectors/api_surface.py`, test riêng theo `remote-agent-test` skill (E2E thật, không mock nội bộ), giới hạn allowlist đọc. | Bỏ qua test E2E thật → lặp lại đúng lỗi trong PRODUCT_PROOF.md cũ ("test pass + push không chứng minh đã deploy") |
| **P3 — Fact.confidence + same_codebase_as predicate** | Audit `Fact.confidence` hiện tại, bổ sung enum nếu thiếu, thêm predicate dedupe fingerprint. | Bỏ qua → Knowledge Graph tiếp tục coi 2 Deployment trùng codebase là 2 node độc lập, sai lệch blast-radius y hệt rủi ro #3 nêu trong báo cáo mẫu |
| **P4 — Tổng quát hoá ReAct policy khảo sát** | Tách chiến lược rộng→hẹp khỏi phần thực thi K8s-only trong `deep_scout.py`, áp cho backend VM/remote-host qua `remote_agent_pipeline.py`. | Bỏ qua → universal discovery chỉ "gọi được lệnh mới" nhưng không có chiến lược tự lái nhiều bước như Claude đã làm, LLM sẽ gọi lệnh rời rạc không hội tụ thành hiểu biết hệ thống |
| **P5 — Export báo cáo tĩnh (Markdown + Mermaid HTML)** | Endpoint export tái dùng đúng cấu trúc 10-mục của báo cáo mẫu, nguồn dữ liệu = Knowledge Graph + confidence. | Không có gì hỏng nếu bỏ qua — đây là tính năng giá trị gia tăng, làm sau cùng |

## 6. Việc KHÔNG làm (giữ đúng quyết định đã chốt trong CLAUDE.md/handoff)

- Không mở lại `FRAMEWORK_LAWS.md`.
- Không tự ý đổi configmap production và deploy — mỗi phase P0-P5 cần user duyệt riêng, đúng
  nguyên tắc EXPLORE → PLAN → VERIFY → GIT trong CLAUDE.md.
- Không dùng discovery mới để gọi bất kỳ API nghiệp vụ ghi nào của hệ thống khách hàng, kể cả khi
  đã biết path — đúng ràng buộc trong chính báo cáo mẫu.
- Không copy giá trị secret thật (dù đọc được) vào bất kỳ nơi nào Omni lưu trữ (Redis/RAG/CRAT/
  report) — chỉ ghi nhận sự tồn tại của rủi ro.

## 7. Next step cụ thể nếu user duyệt

1. Xác nhận với user: chạy P0 (benchmark) trước — cần biết Qwen3.6:27b đã pull về Ollama host chưa
   (`ollama list` trên `host.orb.internal`), nếu chưa cần `ollama pull qwen3.6:27b` (thao tác tải
   model lớn — xác nhận băng thông/dung lượng đĩa trước khi chạy).
2. Đọc `docs/product/PRODUCT_PROOF.md` + `src/aoip/onboarding_projection.py` trước khi code P2-P4
   (tránh trùng lặp với System Twin đã có).
3. Sau P0 có số liệu, quay lại hỏi user có tiếp tục P1 không (dựa trên kết quả benchmark thật, không
   giả định trước).
