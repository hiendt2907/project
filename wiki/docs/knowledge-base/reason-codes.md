# Reason codes — tra cứu nhanh

Danh mục rút gọn từ `docs/reports/project-memory.md` (mục ReasonCodes). Mã đầy đủ nằm trong `pkg/reasoning/reason_codes.py` và payload reasoning.

## Semantic / channel

- `ERR_SEM_CHANNEL_MISMATCH`
- `ERR_SEM_INVALID_TOOL_TAXONOMY`

## Governance

- `ERR_GOV_NS_OUT_OF_BOUNDS`
- `ERR_GOV_UNAUTHORIZED_MUTATION`
- `ERR_GOV_ENV_PROD_STRICT`

## Reasoning / evidence

- `ERR_REA_NO_PHYSICAL_PROOF` — không đủ evidence “critical” cho proof.
- `ERR_REA_SIGMA_GATE_BLOCKED` — sigma/baseline hoặc cửa sổ quan sát chưa đạt.
- `ERR_REA_LOG_SOURCE_UNAVAILABLE` — Loki lỗi khi cần log bypass (fail-closed, không mutate).
- `ERR_REA_SCHEMA_VIOLATION`
- `ERR_REA_HALLUCINATION_DETECTED`

## Planner / phase

- `PLANNER_PHASE_DONE` — trong taxonomy reason codes (đúng ngữ cảnh pipeline).

## Governance invariant (định tính)

- Các mã `INV_*` trong diagnostic policy — không liệt kê hết ở đây; xem `diagnostic_policy.py` và spec.

## Terminal / thành công

- `SUCCESS_VERIFIED_EVIDENCE`
- `ESC_TIMEOUT_TOMBSTONE`
- `ESC_MAX_ATTEMPTS_EXCEEDED`

Khi phân tích log, grep `reason_code` / `ERR_REA_` / `INV_` trong payload Kafka hoặc structured log.
