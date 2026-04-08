# Nghiệm thu (UAT) — Gold dataset + điều kiện ingest

**Ngày:**  
**Phiên bản:**  


| #   | Tiêu chí                                                                                                                               | Pass / Fail | Ghi chú |
| --- | -------------------------------------------------------------------------------------------------------------------------------------- | ----------- | ------- |
| 1   | Governance: không ingest tự động từ Redis; gold chỉ từ **VERIFIED_SUCCESS**                                                            |             |         |
| 2   | Dataset gold: schema đã chốt; đủ `trace_id`, `scenario_id`, `shadow_artifact`, `diagnostic_snapshot`, `review_status=VERIFIED_SUCCESS` |             |         |
| 3   | Faithfulness: sample đã reviewer ký (policy sample: …)                                                                                 |             |         |
| 4   | Learning Delta: cải thiện RAG_HIT round2 vs round1 **hoặc** ngoại lệ có lý do                                                          |             |         |
| 5   | An toàn: flag lab / autodoc tắt; không worker → git push knownbase                                                                     |             |         |
| 6   | Memory: `project-memory.md` cập nhật hoặc “không đổi” + lý do                                                                          |             |         |
| 7   | Baseline Learning Delta: PGVector baseline ghi nhận trước Lượt 2                                                                       |             |         |
| 8   | Registry: bảng trace↔scenario đủ dòng                                                                                                  |             |         |


**Chữ ký**


| Vai trò   | Tên | Ngày |
| --------- | --- | ---- |
| Vận hành  |     |      |
| Reviewer  |     |      |
| Owner RAG |     |      |


**Đính kèm:** link/hash báo cáo giai đoạn + file gold JSONL.