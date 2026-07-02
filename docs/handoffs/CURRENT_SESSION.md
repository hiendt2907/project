# Current Session Handoff

## Deliverable hiện tại
**Slice O2B — HOÀN THÀNH**: Structured Unknown → deduplicated Question → human Answer-as-Claim
→ Competency Matrix CLAIMED/VERIFIED/CONTRADICTED transitions → resolution lifecycle. O1
(`1bc6292`) và O2A (`cf9133f`) đã commit trước đó. O2B chưa commit (ở cuối báo cáo này). Trước
khi làm O2B, đã sửa 2 rủi ro review nêu trong O2A (xem "Sửa risk review O2A" bên dưới).

### Sửa risk review O2A (trước khi bắt đầu O2B)
- **Risk #1 (identity quá dễ VERIFIED)**: `identity` giờ chỉ VERIFIED nếu có ≥2 Fact riêng biệt
  làm bằng chứng (không tính 2 phần tử provenance của CÙNG một Fact — sửa lỗi thiết kế: mỗi Fact
  từ O1 luôn có provenance 2 phần `discovery:probe:trace` + `agent:id`, nên đếm source_types
  luôn ≥2 một cách giả — bỏ điều kiện đó). 1 Fact duy nhất → `OBSERVED`, không phải `VERIFIED`.
- **Risk #2 (Host facet bị NOT_APPLICABLE che gap collector)**: thêm `owner`, `runbook` vào tập
  facet áp dụng cho Host (giờ → `UNKNOWN` thay vì `NOT_APPLICABLE`, đúng vì host thật sự có
  owner/runbook trong vận hành). Giữ `NOT_APPLICABLE` chỉ cho facet thật sự chỉ có nghĩa ở cấp
  service (`business_capability`, `upstream`, `downstream`, `sla`).
- Test mới trong `tests/test_aoip_competency_matrix.py`: `TestIdentityCorroboration` (2 test),
  `TestClaimableFacets` (6 test) — tổng file này giờ 21 test (từ 13).

### Slice O2B — nội dung
`src/aoip/claims_store.py` (mới): `ClaimRecord` (subject/predicate/value/answered_by/
answered_at/question_id/confidence) — lưu Redis hash `omni:aoip:claims:{tenant_id}`, 1 claim
mới nhất mỗi (subject,predicate). KHÔNG fold vào SystemModel qua `fold_and_persist` — cố ý tách
khỏi luồng Fact máy móc để một câu trả lời human không bao giờ tự động ghi đè/thắng Fact máy
(việc so khớp CLAIMED vs VERIFIED vs CONTRADICTED do `competency_matrix.py` quyết định, không
phải do fold semantics).

`src/aoip/competency_matrix.py` (mở rộng): thêm `FACET_PREDICATE` (map facet→predicate cho 6
facet chưa có collector: owner/business_capability/monitoring/logging/runbook/sla),
`build_entity_competency(..., claims=...)`, `_claimable_facet()` implement đúng priority yêu
cầu: **CONTRADICTED > VERIFIED > CLAIMED > OBSERVED > STALE/UNKNOWN**:
- Không claim, không machine fact → UNKNOWN.
- Chỉ machine fact → theo logic O1 hiện có (VERIFIED/OBSERVED/STALE).
- Chỉ claim, còn "tươi" (< `CLAIM_FRESHNESS_SEC`=180 ngày) → CLAIMED.
- Claim quá hạn, không có machine fact → STALE.
- Claim + machine fact cùng giá trị → VERIFIED (machine xác nhận claim).
- Claim + machine fact khác giá trị → CONTRADICTED (không đoán ai đúng).
- Machine-vs-machine contradiction (từ O1 contradiction log) → CONTRADICTED, ưu tiên cao nhất,
  kiểm tra trước cả claim.
- Không có bất kỳ LLM call nào trong toàn bộ path — test `test_llm_never_promotes_claimed_to_verified`
  xác nhận bằng cách grep source không có `import ollama`/`openai`/`chat_completion`/
  `generate_advisory`.

`src/aoip/question_lifecycle.py` (mới) — Unknown/Question/Answer model + lifecycle:
- `Unknown`: unknown_id/tenant_id/entity_type/entity_id/facet/reason(missing|contradicted)/
  evidence_refs/created_at/last_seen_at/status/severity/source. Status: OPEN/QUESTION_PENDING/
  CLAIMED/VERIFIED/CONTRADICTED/RESOLVED/STALE.
- `Question`: question_id/unknown_id/tenant_id/entity_type/entity_id/facet/question_type/
  normalized_fingerprint/text/context_summary/known_evidence/created_at/expires_at/status/
  asked_via/target_role/answer_id. Status: PENDING/ANSWERED/RESOLVED/EXPIRED/CANCELLED.
- `Answer`: answer_id/question_id/tenant_id/answered_by/answered_at/value(≤500 ký tự, KHÔNG
  raw content)/source_channel/confidence/evidence_reference.
- `compute_fingerprint(tenant, entity_type, entity_id, facet, reason)` = sha256 24 ký tự đầu —
  deterministic, KHÔNG dùng text câu hỏi làm identity. `question_id == unknown_id` (1:1 trong
  scope này) để dedup Question/Unknown dùng chung 1 khóa.
- `sync_unknowns_from_competency()`: quét facet UNKNOWN/CONTRADICTED của một EntityCompetency,
  mở/refresh Unknown theo fingerprint (không tạo bản sao); facet không còn UNKNOWN/CONTRADICTED
  → tự RESOLVE Unknown + Question liên quan (Bước 6 — machine evidence tự resolve, KHÔNG phải
  vì câu hỏi được trả lời).
- `ensure_question_for_unknown()`: KHÔNG tạo Question mới nếu đang PENDING hoặc đã ANSWERED
  (chỉ tạo lại khi EXPIRED/CANCELLED/RESOLVED — Unknown mở lại).
- `submit_answer()`: chỉ nhận nếu Question đang PENDING và thuộc đúng tenant (namespace theo
  Redis key tự nhiên chặn cross-tenant); ghi Answer, set Question→ANSWERED, Unknown→CLAIMED,
  và ghi `ClaimRecord` (predicate lấy từ `FACET_PREDICATE`; facet không có mapping (machine-only
  facet) → answer vẫn lưu nhưng không tạo claim, log rõ lý do).
- `render_telegram_text()`: text thô để tương thích kênh Telegram hiện có (Bước 7).

**Quyết định quan trọng (khác với prompt gốc)**: KHÔNG wire tự động gửi Telegram/tạo Question
ngay trên mỗi discovery event. `onboarding_pipeline._sync_understanding_gaps` (mới, gọi sau
`_project_into_system_model`) CHỈ đồng bộ Unknown (bookkeeping âm thầm), KHÔNG gọi
`ensure_question_for_unknown`/Telegram. Lý do: một entity có tới 6+ facet UNKNOWN cùng lúc
(owner/business_capability/monitoring/logging/runbook/sla) — nếu tạo Question+gửi Telegram cho
MỌI facet trên MỌI evidence event sẽ spam tenant (đã thấy trực tiếp: test cũ kỳ vọng 1 Telegram
message nhưng nhận 17). Việc biến Unknown→Question→Telegram vẫn là hàm thư viện độc lập, sẵn
sàng gọi có kiểm soát (batched/paced) — chưa wire thành job tự động trong phiên này, ghi vào gap
O2C.

`src/gateway/routes/onboarding.py` (mở rộng, Bước 8): 4 route mới, cùng pattern với route
onboarding cũ (import `aoip.*` trực tiếp — không vi phạm INV gateway-không-import-workers vì
`aoip` không phụ thuộc `workers`):
- `GET /onboarding/competency?entity_type=&entity_id=&tenant_id=` — trả full facet grid +
  coverage + critical_unknowns + contradicted_facets.
- `GET /onboarding/unknowns?tenant_id=`
- `GET /onboarding/questions?tenant_id=`
- `POST /onboarding/questions/{question_id}/answer` — body {answered_by, value, source_channel,
  confidence, tenant_id}; 404 nếu question không tồn tại/không PENDING/sai tenant.

### Test (Bước 9)
- `tests/test_aoip_question_lifecycle.py` (15 test mới): fingerprint determinism, dedup (same
  evidence lặp lại → 1 Unknown), pending-question-không-tạo-lại, tenant/entity/facet phân biệt,
  answer→claim với human provenance, answer→CLAIMED (không phải VERIFIED), machine fact khớp→
  VERIFIED, machine fact xung đột→CONTRADICTED, answer đơn độc KHÔNG BAO GIỜ tự thành VERIFIED,
  không trả lời được question đã ANSWERED, machine evidence tự resolve Unknown+Question, repeated
  evidence không mở lại question đã trả lời, tenant B không trả lời được question tenant A, answer
  value bị cắt ≤500 ký tự.
- `tests/test_gateway_onboarding_competency_routes.py` (4 test mới): GET competency trả đúng
  facet, entity_type sai bị 422, full flow GET unknowns→GET questions→POST answer→trả lời lần 2
  bị 404, answer question không tồn tại → 404.
- `tests/test_aoip_competency_matrix.py`: +8 test (identity corroboration + claimable facets),
  sửa 1 test cũ (`test_host_not_applicable_facets`) cho khớp Risk #2.
- Full suite: `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` → **5919 passed,
  1 failed (cùng flake môi trường cũ, không liên quan)**.

### Gaps để lại cho O2C (đúng như đề xuất, KHÔNG làm trong phiên này)
- Source acquisition planner (quyết định tìm ở đâu trước khi hỏi người).
- Entity normalization/canonical identity (review risk #3: `nginx` vs `nginx.service` vs full
  path hiện bị coi là entity khác nhau do exact-string match — chưa giải quyết).
- Facet-specific freshness (review risk #4: hiện `DEFAULT_FRESHNESS_SEC`=24h dùng chung cho mọi
  facet máy móc, `CLAIM_FRESHNESS_SEC`=180 ngày dùng chung cho mọi claim — cần khác nhau theo
  facet, vd runtime_state vài phút vs owner vài tháng).
- Batched/paced Question issuance + Telegram dispatch (hiện là hàm thư viện, chưa wire thành job
  tự động — xem "Quyết định quan trọng" ở trên).
- Customer-side semantic knowledge vault (vẫn từ O1/O2A, chưa giải quyết).

---

## Slice O2A — đã commit (`cf9133f`) Entity Competency Matrix (Host + Service) — projection thuần,
derived, không persist song song, xây trên `aoip.system_model.SystemModel` +
`system_model_store` contradiction log đã có từ O1. Đã commit Slice O1
(`1bc6292`); O2A chưa commit (chờ ở cuối báo cáo này).

### Slice O2A — nội dung
`src/aoip/competency_matrix.py` (mới):
- `FacetState`: UNKNOWN/OBSERVED/CLAIMED/VERIFIED/CONTRADICTED/STALE/NOT_APPLICABLE.
- `FACET_SCHEMA` 13 facet; `ENTITY_APPLICABLE_FACETS`: Service = toàn bộ 13 facet áp dụng;
  Host = {identity, runtime_state, process, listening_ports, monitoring, logging} — facet
  ngoài tập này của Host trả `NOT_APPLICABLE` (owner/business_capability/upstream/downstream/
  runbook/sla không có ý nghĩa ở cấp host).
- `FacetValue`: state, value, evidence_refs, source_types, confidence, last_observed_at,
  last_verified_at — đúng yêu cầu #6.
- `build_entity_competency(model, contradictions, entity_type, entity_id, now, freshness_sec)`
  — hàm THUẦN (pure), deterministic, không I/O, không LLM. `build_entity_competency_from_store`
  là wrapper đọc Redis (load_system_model + `load_contradictions` mới thêm vào
  `system_model_store.py`) rồi gọi hàm thuần ở trên — projection reconstructable 100% từ Fact
  đã persist (INV_DERIVED_NEVER_PERSIST, không lưu matrix riêng).
- Facet logic hiện tại (chỉ dựa trên Fact đã có từ O1, KHÔNG bịa thêm predicate mới):
  - `identity`: VERIFIED nếu entity nằm trong `model.known_nodes`, else UNKNOWN.
  - Host `runtime_state`: VERIFIED/STALE dựa trên fact mới nhất về host đó (bằng chứng host
    "còn sống" = có bất kỳ fact nào gắn với nó).
  - Host `process`/`listening_ports`: multi-value, aggregate `runs_process`/`exposes_port`.
  - Service `host`: subject của fact `runs_service` khớp tên service; NẾU >1 host phân biệt
    cùng claim một tên service (trong cửa sổ fresh) → CONTRADICTED (không đoán ai đúng).
  - Service `runtime_state`: VERIFIED "running" nếu có fact `runs_service` fresh.
  - Mọi facet chưa có Fact nguồn tương ứng (owner/business_capability/upstream/downstream/
    monitoring/logging/runbook/sla, và Service.process/listening_ports vì chưa có Fact nối
    port↔service) → UNKNOWN trung thực, không giả định.
  - Contradiction log (từ O1) được tra theo (subject,predicate) → nếu khớp facet đang tính,
    facet đó = CONTRADICTED bất kể model hiện có gì (ưu tiên cao nhất, đúng yêu cầu #8).
  - STALE: fact fresh nhất của facet quá `freshness_sec` (default 24h) so với `now` (đúng #9).
  - CLAIMED chỉ xuất hiện khi provenance có tiền tố `human:` — hiện KHÔNG có nguồn nào tạo
    provenance này (Question/Communication vẫn là luồng riêng, chưa fold vào Fact) → CLAIMED
    trên thực tế chưa từng xuất hiện, đúng thực trạng hệ thống, không giả (đúng #7: không LLM
    nào tự nâng CLAIMED→VERIFIED vì không có logic đó).
- Query API (#12): `entity_coverage`, `critical_unknowns`, `contradicted_facets` — hàm Python
  thuần, CHƯA wire vào gateway HTTP route (ngoài phạm vi phiên, để O2B/O2C nếu cần).
- Test: `tests/test_aoip_competency_matrix.py` (13 test) — cover đủ 6 case yêu cầu ở #13
  (determinism, missing→UNKNOWN, conflict→CONTRADICTED, stale→STALE, tenant isolation,
  persist/reload, import-boundary không đụng recovery/executor).

### Gaps để lại cho O2B (đúng như đề xuất)
- Structured Unknown / question fingerprint.
- Answer-as-Claim (human trả lời → Fact với provenance `human:...` → facet CLAIMED thật sự).
- Dedup + resolution của contradiction/question.
- Wiring `entity_coverage`/`critical_unknowns` vào một API đọc thực (gateway) nếu cần UI.

---

## Slice O1 — đã commit (`1bc6292`)
`Legacy Remote Agent discovery scheduler → AOIP onboarding projection (Observation/Fact) →
persisted/versioned SystemModel`, additive dual-write alongside legacy onboarding flat-Redis
pipeline. Không xóa/thay legacy onboarding trong phiên này.

**QUAN TRỌNG — sửa lại phát biểu sai trong báo cáo trước**: Slice O1 KHÔNG hoàn thành
"Canonical AOIP Agent continuous discovery" như mục tiêu gốc đề ra theo nghĩa đen. Discovery
scheduling vẫn hoàn toàn do `src/remote_agent/agent.py` (legacy Remote Agent) cung cấp — chưa
có AOIP daemon nào sở hữu scheduling. Cái đã hoàn thành đúng là **canonical AOIP onboarding
projection** (Observation/Fact/SystemModel) ăn trên output của legacy scheduler. Đây là
technical debt cần theo dõi cho slice sau, KHÔNG phải blocker cho O2.

## Trạng thái hiện tại
Slice O1 (`1bc6292`) và O2A (`cf9133f`) đã commit. Slice O2B code+test xong, xanh, **CHƯA
commit** (chờ chỉ thị GIT — commit đề xuất `feat(onboarding): add structured unknown and
question lifecycle`).

## Đã hoàn thành

### Bước 0 — Worker runtime
Xác nhận `role=full` KHÔNG chạy `kafka_discovery_evidence_loop` (chỉ `role=onboarding`).
Sửa `src/workers/omni_worker.py:1146` — `if role in ("full", "onboarding"):` — không đổi gì
khác. Test mới `tests/test_worker_role_discovery_consumer.py` (3 test: full đăng ký, onboarding
không duplicate, executor không đăng ký).

### Bước 1 — Contracts (Explore subagent, xác nhận qua Read trực tiếp)
| Concern | Legacy | AOIP | Reuse |
|---|---|---|---|
| Observation | raw dict trong envelope | `aoip.objects.Observation` | Adapter mới: `onboarding_projection.to_observation` |
| Fact | flat JSON per-probe trong `discovery_doc` Redis hash | `aoip.objects.Fact` (bitemporal+provenance) | `onboarding_projection.project_facts` |
| SystemModel persistence | không có (chỉ per-probe blob) | `aoip.system_model.SystemModel` (in-memory, `fold()`) | `aoip.system_model_store` (Redis CAS + revision + history) mới |
| Discovery scheduling | `src/remote_agent/agent.py` — startup + 1h periodic re-scan, 4 probe/cycle collect_interval, per-collector try/except (không raise), graceful shutdown qua signal handler | Không có daemon riêng tương đương | **Reuse nguyên trạng** — đã thỏa mãn toàn bộ yêu cầu Bước 2 (startup/periodic/shutdown/isolation/identity/observe-only), không xây scheduler mới, không copy Agent |
| Envelope | `remote_agent/evidence.py::build_envelope` (probe/trace_id/extracted_fact/tenant_id/agent_id/namespace) | không có envelope riêng | Đọc trực tiếp từ envelope hiện có |
| Versioning/contradiction | Mermaid diagram versioned; discovery doc flat overwrite | `fold()` chỉ supersede TRIỆT ĐỂ trùng (subject,predicate,obj); khác obj thì CỘNG DỒN | Store layer tự thêm supersession + contradiction logic (xem Bước 5) |
| Tenant isolation | Redis key `omni:onboarding:*:{tenant_id}` | `SystemModel.scope` field | Key mới `omni:aoip:system_model:{tenant_id}` cùng pattern |

### Bước 2 — Scheduling: KHÔNG code mới
`src/remote_agent/agent.py` đã chạy 4 probe (`process_list/port_scan/service_topology/
doc_snapshot`) mỗi `collect_interval` (không chỉ 1h — 1h chỉ là VM-profile rescan riêng),
startup ngay khi agent start, shutdown sạch qua `_handle_shutdown` signal handler, mỗi
collector tự bọc try/except trả `None` khi lỗi (không crash loop), có `agent_id`/`hostname`/
`tenant_id` identity. Thỏa mãn toàn bộ yêu cầu Bước 2 — không tạo daemon/adapter scheduling
mới (tránh trùng lặp Agent, đúng chỉ thị "không copy toàn bộ legacy Agent").

### Bước 3-4 — Observation + Fact projection (file mới)
`src/aoip/onboarding_projection.py`:
- `to_observation(ev_doc, tenant_id, agent_id, host)` — envelope → `Observation`
  (scope=`{tenant}/{host}`, data mang `observation_id`-equivalent qua content_hash +
  schema_version=1 + trace_id). Trả `None` cho probe không hỗ trợ/evidence hỏng (không raise).
- `project_facts(observation)` — 4 probe: `process_list`→`runs_process`,
  `port_scan`→`exposes_port`+`runs_service`, `service_topology`→`runs_service` (KHÔNG mang
  description text), `doc_snapshot`→`observed_from` (subject=`document:{sha256[:16]}`, KHÔNG
  mang raw content — đúng INV_DATA_RESIDENCY, mirror `discovery_doc._sanitize_documents`).
- Test: `tests/test_aoip_onboarding_projection.py` (9 test).

### Bước 5 — Contradiction/supersession (trong store, không sửa `aoip.system_model` core)
`SystemModel.fold()` chỉ supersede khi triple TRÙNG HỆT; khác `obj` thì cộng dồn vô hạn —
không đúng ý "temporal replacement phải supersede". Thêm ở store layer (không đổi core dùng
chung với recovery/capability mutation):
- `_apply_supersession`: cùng nguồn (subject,predicate) khác obj, KHÔNG bị đánh dấu
  contradiction → coi là temporal replacement, loại bỏ fact cũ trước khi fold.
- `_split_contradictions`: cùng (subject,predicate) khác obj, provenance khác nguồn, quan sát
  trong cùng cửa sổ 60s → contradiction — GIỮ CẢ HAI (fact cũ ở nguyên model, fact mới ghi vào
  `omni:aoip:contradictions:{tenant_id}`, KHÔNG fold vào model). Không LLM chọn đúng/sai.

### Bước 6 — Persisted System Model (file mới)
`src/aoip/system_model_store.py`:
- `load_system_model`/`fold_and_persist` — Redis hash `omni:aoip:system_model:{tenant_id}`
  (facts JSON + revision int), optimistic CAS qua `WATCH`/`MULTI` (retry ≤5, raise
  `RevisionConflictError` nếu hết retry — KHÔNG rơi vào im lặng).
  Revision tăng đơn điệu (chỉ bump khi nội dung thực sự đổi — so `frozenset(facts)`).
- History append-only `omni:aoip:system_model_history:{tenant_id}` (list, cap 200).
- Contradictions log `omni:aoip:contradictions:{tenant_id}` (list, cap 200).
- Test: `tests/test_aoip_system_model_store.py` (9 test — persist/reload, revision, tenant
  isolation, history, temporal-supersede, contradiction-kept-both).

### Bước 7 — Dual-write compatibility
`src/workers/onboarding_pipeline.py::accumulate_discovery_evidence` — SAU khi legacy
`dd.accumulate_probe_fact` + diagram + readiness đã chạy xong (không đổi thứ tự/logic cũ),
gọi thêm `_project_into_system_model` (try/except riêng, KHÔNG raise ra ngoài, log
`logger.error` có tenant/agent/host khi lỗi, `mark_stage(..., "SYSTEM_MODEL", "ok", ...)` CHỈ
khi thành công — không bao giờ báo "twin updated" khi lỗi). Poison-message retry/DLQ đã có sẵn
ở `kafka_discovery_evidence_loop` (3 lần retry rồi ack+drop, structured log) — không cần thêm
transport mới.
Test: `TestSystemModelDualWrite` trong `tests/test_onboarding_pipeline.py` (2 test — fold
thành công + projection lỗi không mất legacy write).

### Bước 8 — Data residency
`doc_snapshot` → Fact chỉ mang `sha256[:16]` làm node id, KHÔNG mang `content`/`path` gốc.
`service_topology` → Fact chỉ mang tên service, KHÔNG mang `description`. Chưa giải quyết
(để lại O2): semantic customer-side knowledge extraction (khi nào derived fact được phép mang
ý nghĩa nghiệp vụ chứ không chỉ mapping cấu trúc) — vẫn y nguyên gap đã ghi trong audit trước.

### Bước 9 — Tests
44 test mới/sửa, toàn bộ pass:
- `tests/test_worker_role_discovery_consumer.py` (3)
- `tests/test_aoip_onboarding_projection.py` (9)
- `tests/test_aoip_system_model_store.py` (9)
- `tests/test_onboarding_pipeline.py` — thêm `TestSystemModelDualWrite` (2), giữ nguyên tất cả
  test cũ (đều pass).

## Branch và commit
`feature/living-operations-runtime` (chỉ commit local, chưa push). O1=`1bc6292`, O2A=`cf9133f`.
O2B = working tree hiện tại, **chưa commit**. Commit đề xuất:
`feat(onboarding): add structured unknown and question lifecycle`.

## Working tree (O1+O2A đã commit; phần chưa commit là O2B)
- `src/aoip/claims_store.py` — MỚI (ClaimRecord, Redis store riêng, không fold vào SystemModel).
- `src/aoip/question_lifecycle.py` — MỚI (Unknown/Question/Answer model + lifecycle + dedup).
- `src/aoip/competency_matrix.py` — sửa: identity corroboration (risk #1), Host owner/runbook
  = UNKNOWN không NOT_APPLICABLE (risk #2), `FACET_PREDICATE` + `_claimable_facet` (claim
  projection), `build_entity_competency(..., claims=...)`.
- `src/gateway/routes/onboarding.py` — thêm 4 route (competency/unknowns/questions/answer).
- `tests/test_aoip_question_lifecycle.py` — MỚI (15 test).
- `tests/test_gateway_onboarding_competency_routes.py` — MỚI (4 test).
- `tests/test_aoip_competency_matrix.py` — +8 test, sửa 1 test cũ.
- `src/workers/onboarding_pipeline.py` — thêm `_sync_understanding_gaps` (bookkeeping-only, xem
  "Quyết định quan trọng" ở trên — KHÔNG tự gửi Telegram).
- 10 file `docs/post-mortems/*.md` — có từ trước phiên này, không liên quan.

## Verification đã chạy
- Sau O1 (trước commit): 5878 passed, 1 failed (flake cũ).
- Sau O2A (trước commit): 5892 passed, 1 failed (cùng flake).
- Sau O2B: `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` → **5919 passed,
  1 failed (cùng flake môi trường cũ, không liên quan)**:
  `tests/test_remote_agent_e2e.py::TestE2ERegisterAndEvidenceCycle::
  test_register_then_real_system_metrics_emitted_through_real_pipeline`.

## Deployment hiện tại
Chưa deploy — mới chỉ code + test, chưa `make deploy-worker`.

## Blockers
Không có. Sẵn sàng review/commit O2B khi được chỉ thị.

## Next step chính xác (Slice O2C — CHƯA làm)
- Source acquisition planner (quyết định tìm ở đâu trước khi hỏi người) — KHÔNG gộp cùng phiên
  với việc khác, theo đề xuất tách riêng.
- Entity normalization/canonical identity (review risk #3 — chưa giải quyết, exact-string match
  vẫn coi `nginx`/`nginx.service` là khác nhau).
- Facet-specific freshness (review risk #4 — `DEFAULT_FRESHNESS_SEC`=24h và
  `CLAIM_FRESHNESS_SEC`=180 ngày vẫn là hằng số dùng chung, chưa theo từng facet).
- Batched/paced Question issuance + Telegram dispatch tự động (hiện là hàm thư viện độc lập,
  `ensure_question_for_unknown` phải được gọi có kiểm soát, không tự động trên mọi evidence
  event — xem "Quyết định quan trọng" trong mục O2B ở trên).
- Semantic customer-side knowledge extraction (từ O1/O2A, chưa giải quyết).
- Technical debt đã ghi nhận: discovery scheduling vẫn do legacy `remote_agent/agent.py` cung
  cấp, chưa có AOIP daemon sở hữu scheduling (xem mục "QUAN TRỌNG" ở đầu file — không phải lệnh
  cấm vĩnh viễn, xem "Không được làm lại" bên dưới).

## Không được làm lại
- Không tạo scheduler/daemon AOIP mới cho discovery **trong phạm vi O2A/O2B** —
  `remote_agent/agent.py` đã đáp ứng đủ Bước 2 (O1), xác nhận bằng Read trực tiếp dòng
  139-233. Đây KHÔNG phải lệnh cấm vĩnh viễn: migrate scheduling từ legacy Remote Agent sang
  một AOIP daemon thật vẫn là technical debt hợp lệ cho một slice riêng trong tương lai (không
  được lấy ghi chú này để khóa cứng kiến trúc legacy mãi mãi).
- Không sửa `aoip/objects.py` hay `aoip/system_model.py` core `fold()` — supersession/
  contradiction logic nằm ở `system_model_store.py`; competency projection nằm ở
  `competency_matrix.py` — cả hai KHÔNG đụng recovery/capability-mutation code
  (`aoip.recovery`, `aoip.runner`, `aoip.primitives`, `workers.executor` — có test import-boundary
  xác nhận `competency_matrix.py` không import các module này).
- Không đưa raw `content`/`description` text vào bất kỳ `Fact` nào (INV_DATA_RESIDENCY).
- Không xóa/sửa hành vi `pkg.onboarding.discovery_doc`, readiness API, diagram API, Telegram
  question flow.
- Không tạo persistence riêng cho competency matrix — nó PHẢI luôn là projection thuần từ Fact
  đã persist (INV_DERIVED_NEVER_PERSIST), không lưu state riêng trong Redis.
- Không push (chỉ commit local khi được chỉ thị, chưa từng được yêu cầu push).

## Tài liệu liên quan
- `src/workers/omni_worker.py:1146` (role dispatch, O1)
- `src/workers/onboarding_pipeline.py:23,52,62` (`accumulate_discovery_evidence`,
  `_project_into_system_model`, O1)
- `src/aoip/onboarding_projection.py` (Observation/Fact projector, O1)
- `src/aoip/system_model_store.py` (persisted SystemModel + `load_contradictions`, O1+O2A)
- `src/aoip/competency_matrix.py` (Entity Competency Matrix, O2A, MỚI)
- `src/aoip/objects.py`, `src/aoip/system_model.py` (core, KHÔNG sửa)
- `src/remote_agent/agent.py:139-233`, `src/remote_agent/collectors/discovery_evidence.py`
  (scheduling + probes, reuse nguyên trạng)
- `src/pkg/onboarding/discovery_doc.py` (legacy, không đổi)
