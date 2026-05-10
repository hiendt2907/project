# Runbook: Chaos / Matrix + Self-Learning (local LLM) → Shadow → Review → Gold JSONL

**Phạm vi:** Lab / ngân hàng — **Phương án B:** không auto-ingest Redis shadow → PGVector; gold chỉ sau **VERIFIED_SUCCESS** và (sau) ingest tách bước.

**Tham chiếu code:** `[src/workers/selflearning_shadow.py](../../src/workers/selflearning_shadow.py)`, `[src/workers/evidence_consumer.py](../../src/workers/evidence_consumer.py)` (`RAG_HIT` / `RAG_MISS`).

---

## 1. VERIFIED_SUCCESS và faithfulness

**VERIFIED_SUCCESS** là trạng thái **reviewer** gán sau khi:

1. Có `**diagnostic_snapshot`** đủ để đối chiếu vật lý (z-score/sigma, log tail rút gọn, hoặc probe summary theo policy nội bộ).
2. Giả thuyết LLM trong `shadow_artifact` **không mâu thuẫn** rõ ràng với snapshot (không “bịa” nguyên nhân không có trong evidence).
3. Không có dấu hiệu hallucination nguy hiểm cho vận hành (theo checklist team).

**Từ chối (REJECTED)** khi: thiếu snapshot; mâu thuẫn; confidence không đáng tin mà không có con người xác nhận.

**Vai trò (RACI — điều chỉnh theo tổ chức):**


| Hoạt động                            | R         | A    | C        | I         |
| ------------------------------------ | --------- | ---- | -------- | --------- |
| Chạy Matrix / chaos lab              | SRE/Dev   | Lead | Reviewer | Owner RAG |
| Giữ Registry trace↔scenario          | Runner    | Lead | —        | Reviewer  |
| Review shadow + gán VERIFIED_SUCCESS | Reviewer  | Lead | —        | —         |
| Export/merge JSONL gold              | Dev       | Lead | Reviewer | —         |
| Ingest PGVector (sau UAT)            | Owner RAG | Lead | Security | —         |


---

## 2. Lab: bật / tắt WorkerSettings (ConfigMap / env)

**Chỉ lab.** Giữ `multi_hypothesis_shadow_only=true` để không đổi quyết định runtime.


| Mục đích                              | Env (prefix `OMNI`_ theo `WorkerSettings`) | Ghi chú               |
| ------------------------------------- | ------------------------------------------ | --------------------- |
| Bật sinh hypotheses                   | `MULTI_HYPOTHESIS_ENABLED=true`            | Ollama local          |
| Bật knowledge_draft trong JSON shadow | `KNOWLEDGE_DRAFT_ENABLED=true`             | Optional              |
| Shadow-only                           | `MULTI_HYPOTHESIS_SHADOW_ONLY=true`        | Mặc định an toàn      |
| **Tắt** autodoc git                   | `AUTODOC_GIT_PUSH_ENABLED=false`           | **Sprint A bắt buộc** |


Sau phiên: **tắt** các flag self-learning trên worker nếu không còn thử nghiệm.

---

## 3. Matrix / chaos — lệnh và trace

- **Chạy lab nhanh (smoke) + report + registry JSONL:** `NS=multi-agent bash scripts/chaos_rag_lab_run.sh` (hoặc `make chaos-rag-lab`) — gói `e2e_incident_matrix.sh` với 2 scenario `gateway_payload` mặc định, ghi `reports/chaos-rag-lab/latest.json` và `reports/chaos-rag-lab/registry-from-report.jsonl`. Full matrix: `NS=multi-agent CHAOS_RAG_FULL=1 bash scripts/chaos_rag_lab_run.sh`.
- `NS=multi-agent bash scripts/e2e_incident_matrix.sh` — `MATRIX_PATHS` mặc định gồm `config/incident_training_matrix.yaml` + `config/prometheus_firing_simulation.yaml`.
- Chaos nặng hơn (tùy lab): `scripts/agentic_chaos_validation.py`, `scripts/chaos_drill_v1.py`.
- Vòng lặp training: `NS=multi-agent bash scripts/rag_llm_training_loop.sh` (tham chiếu).

**Mỗi scenario phải có `trace_id` thống nhất** trên luồng ingest → worker (guardrail repo).

---

## 4. Registry (bắt buộc — ngay khi chạy Matrix)

Cập nhật **một bảng** (Sheet hoặc Markdown) — **không** chỉ truy vết sau qua Loki.

**Template:** `[reports/chaos-rag-selflearn/registry-template.md](../../reports/chaos-rag-selflearn/registry-template.md)`

Cột tối thiểu: `chaos_run_id`, `scenario_id`, `trace_id`, `learning_round`, thời điểm ghi.

---

## 5. Baseline PGVector (Learning Delta — Lượt 2)

Trước khi đo **Lượt 2** (kỳ vọng `RAG_HIT` cải thiện so với Lượt 1), **cố định baseline tri thức** đã ingest:

1. Chạy ingest vendor (ví dụ):
  ```bash
   .venv/bin/python -m knowledge.ingest_main --sources path/to/knowledge_sources.yaml
  ```
   (hoặc đường dẫn mặc định `OMNI_KNOWLEDGE_SOURCES` / ConfigMap trong cluster.)
2. Ghi trong **báo cáo giai đoạn**: collection (ví dụ `vendor_knowledge`), số điểm / hash snapshot nếu có, **phiên bản** image worker + Ollama embed model.
3. **Lượt 2** chỉ so sánh khi **cùng baseline** đó (không đổi ingest giữa chừng).

Chi tiết giao thức: `[docs/reports/chaos-rag-selflearn-learning-delta.md](chaos-rag-selflearn-learning-delta.md)`.

---

## 6. Redis shadow — TTL và SLA reviewer

- Key: `omni:selflearn:shadow:{trace_id}` — TTL **86400s** (24h) trong code.
- **Audit:** reviewer phải export hoặc quyết định review **trước expiry**; báo cáo phiên ghi nhận nếu TTL còn dưới SLA nội bộ (ví dụ dưới 4 giờ) mà chưa xử lý.

**Export thủ công / script:** `[scripts/omni_redis_shadow_jsonl_exporter.py](../../scripts/omni_redis_shadow_jsonl_exporter.py)` (merge manifest + `diagnostic_snapshot`).

---

## 7. Sprint A — an toàn (không autodoc knownbase)

- `**OMNI_AUTODOC_GIT_PUSH_ENABLED=false`** trong lab.
- **Không** triển khai pipeline worker → `git push` `docs/vendor/knownbase.md`.
- Cập nhật knownbase chỉ qua **PR người** sau Shadow & Review.

---

## 8. Tài liệu liên quan


| Tài liệu                                                                                           | Mô tả                                    |
| -------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| `[chaos-rag-selflearn-schema-jsonl.md](chaos-rag-selflearn-schema-jsonl.md)`                       | Schema JSONL + manifest + ví dụ          |
| `[chaos-rag-selflearn-learning-delta.md](chaos-rag-selflearn-learning-delta.md)`                   | Giao thức Learning Delta                 |
| `[chaos-rag-selflearn-export-ingest.md](chaos-rag-selflearn-export-ingest.md)`                     | Gold JSONL → ingest PGVector (tách bước) |
| `[templates/chaos-rag-session-report.template.md](templates/chaos-rag-session-report.template.md)` | Báo cáo phiên                            |
| `[templates/chaos-rag-phase-report.template.md](templates/chaos-rag-phase-report.template.md)`     | Báo cáo giai đoạn + Memory Applied       |
| `[chaos-rag-selflearn-uat-checklist.md](chaos-rag-selflearn-uat-checklist.md)`                     | Nghiệm thu                               |


---

## 9. Rủi ro nhanh

- Thiếu Registry đúng lúc → sai `scenario_id`, Learning Delta vô nghĩa.
- Review chậm → mất key Redis trước export.
- Đổi baseline ingest giữa Lượt 1 và Lượt 2 → delta sai.

