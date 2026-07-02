# Current Session Handoff

## Deliverable hiện tại
**Slice O2A — HOÀN THÀNH**: Entity Competency Matrix (Host + Service) — projection thuần,
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
Slice O1 đã commit (`1bc6292`). Slice O2A code+test xong, xanh, **CHƯA commit** (chờ chỉ thị
GIT theo AUTONOMY RULES — commit đề xuất `feat(onboarding): add host and service competency matrix`).

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
`feature/living-operations-runtime`. Slice O1 = commit `1bc6292` (đã push? KHÔNG — chỉ commit
local, chưa push). Slice O2A = working tree hiện tại, **chưa commit**. Commit message đề xuất
cho O2A: `feat(onboarding): add host and service competency matrix`.

## Working tree (sau O1 đã commit, phần chưa commit là O2A)
- `src/aoip/competency_matrix.py` — MỚI (Entity Competency Matrix, thuần/derived).
- `src/aoip/system_model_store.py` — thêm `load_contradictions()` (đọc-only, không đổi logic
  fold_and_persist hiện có).
- `tests/test_aoip_competency_matrix.py` — MỚI (13 test).
- 10 file `docs/post-mortems/*.md` — có từ trước phiên này, không liên quan Slice O1/O2A.

## Verification đã chạy
- Sau O1 (trước commit): `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` →
  5878 passed, 1 failed (pre-existing, không liên quan — xem dưới).
- Sau O2A: cùng lệnh → **5892 passed, 1 failed (cùng 1 flake cũ)**:
  `tests/test_remote_agent_e2e.py::TestE2ERegisterAndEvidenceCycle::
  test_register_then_real_system_metrics_emitted_through_real_pipeline` — flake phụ thuộc tải
  máy thật lúc chạy test (GIGO/quality-classify routing quyết định topic dựa trên psutil thật),
  không đụng tới file nào của O1/O2A.

## Deployment hiện tại
Chưa deploy — mới chỉ code + test, chưa `make deploy-worker`.

## Blockers
Không có. Sẵn sàng review/commit O2A khi được chỉ thị.

## Next step chính xác (Slice O2B/O2C — CHƯA làm)
- **O2B**: Structured Unknown → question fingerprint → answer-as-Claim (human trả lời tạo Fact
  provenance `human:...` → facet thật sự chuyển CLAIMED, hiện tại CLAIMED chưa từng xảy ra vì
  chưa có luồng này) → dedup/resolution contradiction+question.
- **O2C**: Source acquisition planner — KHÔNG gộp vào cùng phiên với O2B (theo đề xuất, tránh
  scope quá lớn).
- Wiring `entity_coverage`/`critical_unknowns`/`contradicted_facets` vào gateway API thật (hiện
  chỉ là hàm Python thuần, `import`-được nhưng chưa có HTTP route) — làm khi O2B/O2C cần UI.
- Semantic customer-side knowledge extraction (description/doc content vẫn chỉ mapping/hash) —
  vẫn để lại, chưa giải quyết.
- Technical debt đã ghi nhận: discovery scheduling vẫn do legacy `remote_agent/agent.py` cung
  cấp, chưa có AOIP daemon sở hữu scheduling (xem mục "QUAN TRỌNG" ở đầu file).

## Không được làm lại
- Không tạo scheduler/daemon AOIP mới cho discovery — `remote_agent/agent.py` đã đáp ứng đủ
  Bước 2 (O1), xác nhận bằng Read trực tiếp dòng 139-233.
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
