# Giao thức: Learning Delta (cùng scenario_id)

**Mục tiêu:** Lượt 2 sau baseline tri thức cố định trong PGVector kỳ vọng **RAG_HIT** thay vì **RAG_MISS** (cùng điều kiện đo).

---

## Điều kiện cố định

1. **Cùng `scenario_id`** (cùng kịch bản matrix / chaos có thể tái lập).
2. **Cùng baseline PGVector:** đã ghi nhận collection + phiên bản ingest + embed model **trước Lượt 2**; không thay đổi giữa Lượt 1 và Lượt 2 nếu đang so delta.
3. **Cùng cách đo `rag_signal`:** trích từ log/telemetry với cùng quy tắc (ví dụ cùng stage evidence_consumer).
4. **`learning_round`** ghi trong Registry và JSONL.

---

## Thứ tự đề xuất (Sprint A)

1. **Baseline:** ingest tri thức nền → ghi hash/version + thời điểm.
2. **Lượt 1:** chạy scenario → ghi `trace_id`, `rag_signal`, Registry.
3. (Tuỳ chính sách) **Ingest thêm** chỉ nội dung đã **VERIFIED_SUCCESS** (không từ Redis tự động) — nếu đang kiểm chứng “học sau review”.
4. **Lượt 2:** chạy lại **cùng scenario_id** → so `rag_signal`.

---

## Ngoại lệ

Báo cáo giai đoạn phải ghi rõ **ngoại lệ** (ví dụ không tái lập được load, thay đổi cluster) không tính vào delta.
