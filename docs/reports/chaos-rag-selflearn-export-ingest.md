# Gold JSONL → Ingest PGVector (tách bước, không auto từ Redis)

**Nguyên tắc:** Chỉ ingest **sau** review **VERIFIED_SUCCESS**; **không** pipeline Redis shadow → vector trực tiếp.

---

## Bước 1 — Gold dataset

- Xuất JSONL từ exporter + manifest (chỉ dòng `review_status=VERIFIED_SUCCESS`).
- Lưu hash file (SHA-256) đính kèm báo cáo giai đoạn và biên bản UAT.

---

## Bước 2 — Ingest (tách repo / job riêng)

1. Chuyển đổi nội dung gold (ví dụ symptom + fix đã duyệt) sang định dạng chunk phù hợp collection (ví dụ SOP / errors — theo policy Owner RAG).
2. Chạy pipeline embed + upsert **giống** ingest vendor hiện có: [`src/knowledge/ingest_main.py`](../../src/knowledge/ingest_main.py) hoặc job nội bộ đã phê duyệt.
3. **Không** dùng `knowledge_promotion` từ worker trong Sprint A nếu chưa có UAT.

---

## Kiểm tra

- Trước ingest production: UAT checklist + ký duyệt dataset (xem [`chaos-rag-selflearn-uat-checklist.md`](chaos-rag-selflearn-uat-checklist.md)).
