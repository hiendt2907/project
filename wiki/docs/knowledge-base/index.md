# Knowledge base — tri thức vận hành Omni

Phần này **tổng hợp** các hợp đồng hệ thống, bất biến (invariants), mã lý do, và quy tắc cho **agent / LLM / vận hành**. Nguồn đầy đủ vẫn là repo: `docs/vendor/OMNI_PROJECT_CANONICAL.md`, `docs/reports/project-memory.md`, `src/workers/settings.py`.

!!! caution "Độ tin cậy"
    Ưu tiên **code** khi mâu thuẫn với tài liệu cũ. `OMNI_ENV_MODE` trong code là `prod` | `dev` (xem `WorkerSettings`); một số doc legacy có thể ghi `lab` — hiểu là môi trường thử nghiệm tương đương `dev` theo ngữ cảnh.

| Trang | Mục đích |
|-------|----------|
| [System contract](system-contract.md) | Topology MPV3, Kafka, `trace_id`, vai trò worker |
| [Invariants & project memory](invariants.md) | Bất biến chứng minh lỗi, policy chẩn đoán, self-learning |
| [Guardrails for agents](guardrails-for-agents.md) | Quy tắc cho AI/Claude: gateway, mutate, queue, bí mật |
| [Reason codes](reason-codes.md) | `ERR_*`, `INV_*`, terminal — tra cứu nhanh |
| [Verification & gates](verification-matrix.md) | Gate CI, E2E, khi nào cần verify runtime |

Sau khi đọc Tầng 0 ở đây, mở thêm [DOCUMENTATION_INDEX](../library/project-docs/DOCUMENTATION_INDEX.md) trong thư viện repo để lấy báo cáo phase / spec chi tiết.
