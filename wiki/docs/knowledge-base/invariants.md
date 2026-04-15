# Invariants & project memory

Tóm tắt từ `docs/reports/project-memory.md` và code liên quan. Chi tiết đổi theo thời điểm — luôn đối chiếu file gốc khi làm thay đổi hành vi.

## Proof-of-Fault & ba lane

- Bật mặc định: `OMNI_PROOF_LANE_ENABLED` (trong code: `omni_proof_lane_enabled`, default **true**).
- Ba lane: **`resource`** (baseline `dr` / z-score + cửa sổ quan sát Redis), **`state`** (tín hiệu K8s/container tất định, không bắt sigma), **`app_log`** (Loki 5xx bền khi sigma phẳng — điều kiện chặt).
- Planner/LLM **không** được phá vỡ `_proof_of_fault_gate` trong `evidence_consumer.py`.

## Diagnostic policy (`INV_*`)

- Các biến bất biến tất định trong `pkg/reasoning/diagnostic_policy.py`; enforced sau proof-of-fault, trước `EXECUTE_MUTATE`.
- Ví dụ đã ghi trong project-memory: `INV_NO_RESTART_ON_BROKEN_SPEC`, `INV_READ_BEFORE_MUTATE_DEFER`, `INV_NAMESPACE_ISOLATION`.
- Spec: `docs/reports/diagnostic-policy-spec.md`.

## Feedback & học

- Kết quả thực thi: Kafka **`omni-action-feedback`** (`kafka_topic_action_feedback`), không phải `omni-results`.
- Self-learning shadow: **không** auto-ingest Redis shadow vào PGVector; chỉ ingest sau **VERIFIED_SUCCESS** + bước ingest có kiểm soát (xem chaos-rag docs trong repo).

## Matrix & registry

- Kịch bản huấn luyện / ánh xạ: `config/incident_training_matrix.yaml` (và `MATRIX_PATHS` nếu gộp file). Không hardcode nhánh shell rời rạc thay cho registry.

## An toàn nâng cao (mặc định tắt / zero-impact)

- Các cờ như `OMNI_MULTI_HYPOTHESIS_ENABLED`, `OMNI_DEEP_PROBE_ORCHESTRATION_ENABLED`, `OMNI_KNOWLEDGE_DRAFT_ENABLED`, `OMNI_AUTODOC_GIT_PUSH_ENABLED` — giữ **false** theo mặc định zero-impact trừ khi có quyết định vận hành.

## Label schema (Golden Link)

- `docs/vendor/OMNI_LABEL_SCHEMA.md` + `config/omni_label_schema.yaml`; parser nhận diện alert: `src/pkg/reasoning/alert_identity.py`.

## Grafana

- Năm dashboard chuẩn (Omni Ops / Security / Learning / Pod / Node) — đồng bộ từ JSON canonical, không để drift ConfigMap.

## Failure patterns (đọc trước khi debug)

- Matrix E2E: script gateway phải propagate exit code; không coi matrix pass = audit strict luôn đúng (sigma/trace có thể fail trong lab yên tĩnh).
- Xem thêm mục **FailurePatterns** trong `project-memory.md`.
