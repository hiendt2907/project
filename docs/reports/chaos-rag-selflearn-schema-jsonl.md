# Schema: JSONL gold + manifest review

**Mục đích:** Một dòng JSON per `trace_id` sau khi merge Redis shadow + metadata review + (tuỳ chọn) `diagnostic_snapshot` từ Loki/audit.

---

## Trường (mục tiêu thiết kế)

| Trường | Bắt buộc | Mô tả |
|--------|-----------|--------|
| `trace_id` | Có | Chuỗi trace Omni |
| `scenario_id` | Có | Từ matrix / catalog |
| `chaos_run_id` | Khuyến nghị | UUID hoặc stamp phiên |
| `learning_round` | Khuyến nghị | 1, 2, … đo Learning Delta |
| `shadow_artifact` | Có (sau export Redis) | Object: hypotheses, knowledge_draft, … |
| `diagnostic_snapshot` | Có trước VERIFIED_SUCCESS | z-score/sigma, log tail rút gọn, probe summary |
| `rag_signal` | Khuyến nghị | `RAG_HIT`, `RAG_MISS`, hoặc mã từ log worker |
| `ground_truth` | Tuỳ chọn | Từ catalog scenario |
| `review_status` | Có | `PENDING` \| `REJECTED` \| `VERIFIED_SUCCESS` |

**Nguồn `diagnostic_snapshot`:** Post-hoc (Loki theo `trace_id`, snapshot sigma, audit) — merge vào manifest khi review; không bắt buộc nằm trong Redis.

---

## Manifest review (song song)

File JSONL (hoặc CSV) do reviewer điền:

- `trace_id`
- `review_status`
- `diagnostic_snapshot` (object JSON)
- `reviewer`, `reviewed_at` (tuỳ chọn)

Script exporter merge manifest theo `trace_id`: [`scripts/omni_redis_shadow_jsonl_exporter.py`](../../scripts/omni_redis_shadow_jsonl_exporter.py).

---

## Ví dụ một dòng (minh họa)

```json
{
  "trace_id": "tr-abc-001",
  "scenario_id": "prom_cpu_high",
  "chaos_run_id": "run-20260407-01",
  "learning_round": 1,
  "shadow_artifact": {
    "trace_id": "tr-abc-001",
    "ts": 1712500000,
    "shadow_only": true,
    "hypotheses": [{"name": "cpu_saturation", "why": "...", "confidence": 0.7}],
    "knowledge_draft": {}
  },
  "diagnostic_snapshot": {
    "z_cpu": 0.0,
    "sigma_note": "lab_low_noise",
    "log_tail_ref": "loki:query_id=..."
  },
  "rag_signal": "RAG_MISS",
  "ground_truth": "cpu pressure on workload",
  "review_status": "VERIFIED_SUCCESS",
  "exported_at": "2026-04-07T12:00:00Z"
}
```
