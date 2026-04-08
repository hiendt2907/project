# Omni — Tài liệu canonical (một nguồn, bám code)

**Phiên bản:** repo snapshot — cập nhật khi đổi `Makefile`, `src/workers/settings.py`, `scripts/kafka_ensure_omni_topics.sh`, hoặc topology K8s.

**Mục đích:** Một file duy nhất để kiểm tra kiến trúc vận hành thật (split MPV3), Kafka, RAG, feedback, verify. Các doc khác chỉ bổ sung chi tiết hoặc lịch sử phase.

---

## 1. Chuẩn lab: split topology (Master Plan V3)

### 1.1 Image và deploy

| Thành phần | Image / lệnh | Manifest (tham chiếu) |
|------------|--------------|------------------------|
| Worker (shared image) | `docker build -t multi-agent-system:latest -f Dockerfile .` → `make docker-worker` | Một image, nhiều Deployment (`OMNI_WORKER_ROLE` khác nhau) |
| Gateway | `docker build -t omni-gateway:latest -f Dockerfile.gateway .` → `make docker-gateway` | `k8s/deployments/omni-gateway.yaml` |
| Rollout split | `make deploy-worker` | Áp `omni-worker-configmap`, `omni-worker-rbac`, `prober-rbac`, `analyst-rbac`, rồi `omni-prober`, `omni-analyst`, `omni-core`, `omni-executor` ([Makefile](../../Makefile) L22–35) |
| Gateway rollout | `make deploy-gateway` | Sau khi build image gateway |
| Kafka topics | `make ensure-kafka-topics` | [scripts/kafka_ensure_omni_topics.sh](../../scripts/kafka_ensure_omni_topics.sh) |
| Legacy monolith (một Pod) | `make legacy-deploy-worker` | `k8s/deployments/omni-worker.yaml`, `OMNI_WORKER_ROLE=full` — **không** chạy đồng thời với split cùng consumer trùng stream ([knownbase.md](knownbase.md)) |

Prefix kubeconfig: `./scripts/with_working_kube.sh` cho `kubectl` khi cần.

### 1.2 Bảng `OMNI_WORKER_ROLE` → task (nguồn: `src/workers/omni_worker.py` `_worker_background_tasks`)

| Role | Kafka / loops (tóm tắt) |
|------|-------------------------|
| `executor` | `kafka_actions_loop` only |
| `prober` | `kafka_alerts_loop`, `delayed_queue_loop`, `circuit_breaker_loop`; optional `telegram_loop` |
| `analyst` | `kafka_evidence_loop`, `kafka_action_feedback_loop` |
| `core` | `deep_scout_periodic`, `autonomous_forecast`, `baseline_snapshot`; optional `autonomous_decider`, `proactive_evaluate`, `kafka_proactive_incidents`; optional `deep_scout_autonomous` startup |
| `full` | Gộp nhánh prober + analyst + core (monolith) |

Deployment tên: `omni-prober`, `omni-analyst`, `omni-core`, `omni-executor` — mỗi pod set `OMNI_WORKER_ROLE` tương ứng trong manifest (không mở manifest ở đây do `.cursorignore`; đối chiếu khi apply).

### 1.3 Luồng dữ liệu tổng quát (Kafka)

1. Gateway / ingest → `omni-alerts` (prober đọc).
2. Prober → evidence → `omni-diagnostic-evidence` (analyst đọc).
3. Analyst / pipeline → `omni-actions` (executor đọc).
4. Executor → kết quả mutate → **`omni-action-feedback`** (analyst đọc — **không** có topic `omni-results` trong code).

---

## 2. Kafka — topic chuẩn (đồng bộ script + default settings)

**Ensure script:** [scripts/kafka_ensure_omni_topics.sh](../../scripts/kafka_ensure_omni_topics.sh) — danh sách:

`omni-alerts`, `omni-diagnostic-evidence`, `omni-actions`, **`omni-action-feedback`**, `omni-dlq`, `omni-proactive-incidents`, `omni-audit-sandbox`, `omni-audit-proactive`, `omni-audit-agent`, `omni-tool-audit`.

**Default topic names** trong [src/workers/settings.py](../../src/workers/settings.py) (`_sanitize_kafka_topic_names`) trùng các tên trên; override qua env/ConfigMap nếu cần.

---

## 3. Feedback loop thực thi → RAG (pgvector)

| Bước | File / hành động |
|------|------------------|
| Publish sau `EXECUTE_MUTATE` | [src/workers/autonomous_execute.py](../../src/workers/autonomous_execute.py) `publish_action_feedback` |
| Topic | `kafka_topic_action_feedback` → **`omni-action-feedback`** |
| Consume | [src/workers/autonomous_feedback_loop.py](../../src/workers/autonomous_feedback_loop.py) `kafka_action_feedback_loop` → `handle_action_feedback_envelope` (role `analyst` hoặc `full`) |
| Thành công | `_upsert_action_experience_on_success` → collection **`action_experience`** ([COLLECTION_ACTION_EXPERIENCE](../../src/rag/pgvector_store.py)) |
| Thất bại | Replan LLM, `emit_execute_mutate` lặp, giới hạn trong settings; escalate / tombstone |

Payload: [src/pkg/autonomous_actions.py](../../src/pkg/autonomous_actions.py).

**Lưu ý:** Tài liệu cũ ghi `omni-results` — **không** tồn tại trong `src/`; bỏ hoặc coi là tên lịch sử.

---

## 4. RAG / pgvector — collection (tham chiếu code)

| Collection constant | Tên bảng logic | Ghi chú |
|---------------------|----------------|---------|
| `COLLECTION_K8S_EXPERT` | `k8s_expert` | Expert gate mặc định (`OMNI_PGVECTOR_COLLECTION_K8S_EXPERT`) |
| `COLLECTION_ACTION_EXPERIENCE` | `action_experience` | Học từ feedback thành công |
| `COLLECTION_SOP` / `COLLECTION_SOP_V2` | `itops_sop_ledger` / `itops_sop_ledger_v2` | SOP |
| Khác | `itops_error_ledger`, `infra_topology`, `cli_hil_context`, `vendor_knowledge` | Theo `src/rag/pgvector_store.py` |

DSN: `POSTGRES_RAG_DSN` — không hardcode password trong repo ([PostgresRAGSettings](../../src/rag/pgvector_store.py)).

**Self-learning shadow (Redis):** không auto-ingest PGVector; xem [project-memory.md](../reports/project-memory.md) Invariants.

---

## 5. Trace ID

Luồng nghiệp vụ giữ **`trace_id`** xuyên suốt ingest → probe → reasoning → execution → report. Entrypoint sinh id nếu thiếu; Kafka payload nên có `trace_id` (xem `.cursorrules`).

---

## 6. Observability

- **Grafana dashboards:** JSON canonical trong `k8s/monitor/dashboards/` — sync bằng [scripts/sync_grafana_dashboard_configmaps.py](../../scripts/sync_grafana_dashboard_configmaps.py) → `k8s/monitor/grafana-dashboards.yaml`.
- **Proactive metrics** (`omni_proactive_*`): code trong `metrics_exporter.py` / `proactive_observer.py`; deployment chạy proactive là **`omni-core`** khi split (không phải `omni-worker` trừ legacy). Chi tiết PromQL: [proactive_slo.md](../proactive_slo.md).
- **Prometheus rules:** `k8s/monitor/prometheus.yaml` (lab thresholds).

---

## 7. Verify / E2E / gates

| Mục đích | Lệnh |
|----------|------|
| CI worker/gateway runtime | `.cursor/rules/omni-cicd-k8s.mdc` — build, `make deploy-worker`, pytest, một E2E |
| Proactive | `make e2e-proactive` → `scripts/proactive_e2e.sh` |
| Alert + gateway + Loki | `bash scripts/gateway_alert_loki_verify.sh` (exec mặc định `omni-prober`) |
| Incident matrix | `make e2e-incident-matrix` → `scripts/e2e_incident_matrix.sh`; report JSON: `reports/incident-matrix/latest.json` (+ `git_sha`, `config_sha256_primary_matrix`, `matrix_paths`) |
| Gates | `make autonomy-gate`, `mutate-only-gate`, `classifier-regression-gate`, `nonimpact-guards-gate`, `learning-loop-gate`, `secret-gate`, … ([Makefile](../../Makefile)) |

**Matrix pass ≠ strict full_system_audit:** có thể fail sigma/trace trong lab — [project-memory.md](../reports/project-memory.md).

---

## 8. Corpus — ghi doc ở đâu

| Nội dung | File |
|----------|------|
| **File này** | Kiến trúc + vận hành canonical |
| Symptom → fix (incident thật) | [knownbase.md](knownbase.md) |
| Invariants / guardrails / failure patterns | [project-memory.md](../reports/project-memory.md) |
| MPV3 lịch sử + bảng rộng | [master_plan_v3_review_report.md](master_plan_v3_review_report.md) |
| Pointer RAG / retrieval | [omni_playbook_index.md](../omni_playbook_index.md) |
| ADR RBAC (nháp) | [adr-rbac-executor.md](adr-rbac-executor.md) |

---

## 9. Nợ / giới hạn đã biết (tóm tắt)

- **RBAC:** `omni-executor` / core thường gắn SA rộng trong lab — thu hẹp theo [adr-rbac-executor.md](adr-rbac-executor.md).
- **Ollama:** service `ollama-service:11434` hoặc `host.docker.internal` — xem knownbase embed/DNS.
- **Strict audit** nhạy môi trường lab.

---

## 10. Liên kết nhanh code

- Worker entry: `src/workers/omni_worker.py`
- Settings: `src/workers/settings.py`
- Evidence: `src/workers/evidence_consumer.py`
- Actions consumer: `src/workers/kafka_actions_consumer.py`
- RAG gate: `src/pkg/rag/gate.py`
- Store: `src/rag/pgvector_store.py`

---

**Chỉ mục tài liệu đầy đủ:** [../DOCUMENTATION_INDEX.md](../DOCUMENTATION_INDEX.md)

*Tài liệu này thay thế vai trò “điểm vào duy nhất” cho kiến trúc Omni; các doc chi tiết khác không được mâu thuẫn phần đã khóa ở đây.*
