# Guardrails — AI, Claude, automation

Quy tắc để **không** phá hợp đồng hệ thống khi sinh code hoặc vận hành Omni. Bám `.cursorrules`, `CLAUDE.md`, và `WorkerSettings`.

## Kiến trúc & phạm vi

| Quy tắc | Chi tiết |
|---------|----------|
| Gateway isolation | `src/gateway/` **không** import worker, `pkg/reasoning`, `pkg/executor`. Chỉ HTTP + Kafka + Redis + metrics. |
| Mutate-only | Mutation K8s chỉ qua **executor** (`OMNI_WORKER_ROLE=executor`); analyst/prober không mutate trực tiếp từ reasoning path. |
| Service names LLM | Endpoint Ollama: **`ollama-service:11434`** hoặc **`host.docker.internal:11434`** — không hardcode IP nội bộ lạ. |
| Context LLM | Worker Ollama: **`num_ctx=4096`** trừ khi có quyết định dự án đổi đồng bộ. |
| Queue | Kafka cho pipeline alert/evidence/action; **không** dùng Redis List `BLPOP` làm worker queue chính. |

## Môi trường (`OMNI_ENV_MODE`)

- Trong code Pydantic: **`prod`** | **`dev`**, default **`prod`** (`src/workers/settings.py`).
- **`prod`**: tắt `god_mode`, `lab_unchained`, `cluster_full_access` (validator `_god_mode_implies_lab` / prod strip).
- Gate tĩnh: `scripts/validate_env_mode_gate.py` (CI).

## Dữ liệu nhạy cảm

- Không hardcode secret/token/password trong repo; DSN placeholder, secret inject runtime (K8s Secret / env).

## Payload & hợp đồng Kafka

- Envelope alert/evidence/action phải có **`trace_id: str`** (string) xuyên suốt.

## `EXECUTE_MUTATE` vs gợi ý

- Chỉ công cụ mutate nằm trong allowlist executor; công cụ đọc/query → luồng `SUGGEST_REMEDIATION` / chẩn đoán, không điều khiển mutate trực tiếp từ analyst.

## Omni + RAG

- RAG gate trước LLM: `pkg/rag/gate.py`; điểm sàn và chunk limit xem `WorkerSettings` (`rag_gate_*`).
- Cảnh báo self-remediation / security: có `RAG_SCORE_FLOOR` cao hơn cho một số alert (xem `evidence_consumer.py`).

## Khi không chắc

1. Đọc `docs/vendor/OMNI_PROJECT_CANONICAL.md`.
2. Đọc `docs/reports/project-memory.md` (Invariants + FailurePatterns).
3. Tra cứu field cụ thể trong `src/workers/settings.py`.
