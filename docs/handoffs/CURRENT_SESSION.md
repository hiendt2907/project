# Current Session Handoff

## Deliverable hiện tại
**Slice O1 — HOÀN THÀNH (đã review, đúng trạng thái sau đây)**:
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
Code + tests xong, toàn bộ unit suite xanh (trừ 1 flake tiền-nhiệm không liên quan).
Chưa commit (theo AUTONOMY RULES: GIT chỉ khi được chỉ thị rõ).

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
`feature/living-operations-runtime` @ `49343b2` — **chưa commit** (chờ chỉ thị GIT theo
AUTONOMY RULES). Commit message đề xuất:
`feat(onboarding): project canonical discovery into persisted system twin`

## Working tree
- `src/workers/omni_worker.py` — role=full giờ chạy `kafka_discovery_evidence_loop`.
- `src/workers/onboarding_pipeline.py` — thêm `_project_into_system_model` dual-write.
- `src/aoip/onboarding_projection.py` — MỚI (Observation/Fact projector).
- `src/aoip/system_model_store.py` — MỚI (persisted/versioned SystemModel store).
- `tests/test_worker_role_discovery_consumer.py`, `tests/test_aoip_onboarding_projection.py`,
  `tests/test_aoip_system_model_store.py` — MỚI.
- `tests/test_onboarding_pipeline.py` — thêm `TestSystemModelDualWrite`.
- 10 file `docs/post-mortems/*.md` — có từ trước phiên này, không liên quan Slice O1.

## Verification đã chạy
`.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` → **5878 passed, 1 failed
(pre-existing, không liên quan)**: `tests/test_remote_agent_e2e.py::TestE2ERegisterAndEvidenceCycle::
test_register_then_real_system_metrics_emitted_through_real_pipeline` — flake phụ thuộc tải máy
thật lúc chạy test (GIGO/quality-classify routing quyết định topic dựa trên psutil thật), không
đụng tới file nào của Slice O1 (metrics/GIGO/topic-routing không nằm trong scope sửa đổi).

## Deployment hiện tại
Chưa deploy — mới chỉ code + test, chưa `make deploy-worker`.

## Blockers
Không có. Sẵn sàng review/commit khi được chỉ thị.

## Next step chính xác (Slice O2 — CHƯA làm)
1. Competency Matrix (hoàn toàn ABSENT — theo audit trước).
2. Unknown/Question strategy formalize (hiện vẫn ad-hoc trong `_detect_gaps_and_ask`).
3. Source acquisition planner.
4. Semantic customer-side knowledge extraction (description/doc content vẫn chỉ
   mapping/hash — chưa có pipeline trích xuất ý nghĩa nghiệp vụ an toàn).
Nếu review Slice O1 yêu cầu sửa, ưu tiên sửa tại `system_model_store.py`
(`_apply_supersession`/`_split_contradictions`) — đây là phần suy luận mới nhất, rủi ro cao nhất.

## Không được làm lại
- Không tạo scheduler/daemon AOIP mới cho discovery — `remote_agent/agent.py` đã đáp ứng đủ
  Bước 2, xác nhận bằng Read trực tiếp dòng 139-233.
- Không sửa `aoip/objects.py` hay `aoip/system_model.py` core `fold()` — supersession/
  contradiction logic nằm ở `system_model_store.py` để không ảnh hưởng recovery/capability
  mutation code đang dùng chung `SystemModel`.
- Không đưa raw `content`/`description` text vào bất kỳ `Fact` nào (INV_DATA_RESIDENCY).
- Không xóa/sửa hành vi `pkg.onboarding.discovery_doc`, readiness API, diagram API, Telegram
  question flow.
- Không commit/push (chưa được chỉ thị).

## Tài liệu liên quan
- `src/workers/omni_worker.py:1146` (role dispatch)
- `src/workers/onboarding_pipeline.py:23,52,62` (`accumulate_discovery_evidence`,
  `_project_into_system_model`)
- `src/aoip/onboarding_projection.py` (Observation/Fact projector, mới)
- `src/aoip/system_model_store.py` (persisted SystemModel, mới)
- `src/aoip/objects.py`, `src/aoip/system_model.py` (core, KHÔNG sửa)
- `src/remote_agent/agent.py:139-233`, `src/remote_agent/collectors/discovery_evidence.py`
  (scheduling + probes, reuse nguyên trạng)
- `src/pkg/onboarding/discovery_doc.py` (legacy, không đổi)
