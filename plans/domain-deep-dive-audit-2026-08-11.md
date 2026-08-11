# Kế hoạch: Đi sâu 9-domain — collector → xử lý → Telegram → giá trị vận hành

**Ngày:** 2026-08-11
**Loại:** Kế hoạch khảo sát (audit), KHÔNG code, KHÔNG deploy.
**Không phải blueprint xây dựng** — đây là investigation plan, output là 1 tài liệu, không phải PR.

## Mục tiêu

Với từng domain trong 9 domain canonical (`src/pkg/domain/taxonomy.py`), trả lời dứt điểm 4 câu hỏi
bằng cách đọc code thật + (nếu có) evidence thật đã capture trong các phiên trước, KHÔNG suy đoán:

1. **Đang làm gì** — collector nào phát hiện, đo cái gì, ngưỡng bao nhiêu, chạy ở đâu (agent VM hay Omni).
2. **Trả về cái gì** — field nào trong payload (`METRIC_SAMPLE`/`ANOMALY`), verdict do agent tự tính
   (STATIC_GUARD) hay do Omni tính (baseline/z-score).
3. **Xử lý ra sao** — đường đi qua `assess_domain_severity` → RAG recall → LLM (có/không) →
   `AnalystAdvisory` → CRAT → dispatch. Domain nào có ANOMALY thật từng chạy qua toàn bộ pipeline,
   domain nào chỉ có METRIC_SAMPLE (chưa từng nâng cấp thành incident thật).
4. **1 evidence hoàn chỉnh → Telegram có gì** — nội dung thật của `unified_incident_card` (WHAT/
   Kiểm chứng/Khắc phục/Dự báo/Audit) khi domain đó phát sinh alert: field nào là generic
   (giống mọi domain), field nào là domain-specific thật sự hữu ích, field nào rỗng/placeholder.
5. **Có giúp người vận hành không** — với nội dung thật thu được ở bước 4, người trực có đủ thông
   tin để hành động ngay không (biết cái gì hỏng, hỏng ở đâu, làm gì tiếp) hay chỉ là noise.

## Phạm vi — 9 domain, trạng thái nền đã biết (từ CLAUDE.md, KHÔNG lặp lại điều tra)

| Domain | Trạng thái đã xác nhận trước đó |
|---|---|
| `os_host` | ✅ có ANOMALY thật, z=3.739, 8 lượt ReAct |
| `database` | ✅ critical → diagnosis loop |
| `service` | ✅ có 2 case thật (unit failed + active→inactive) |
| `kubernetes` | ✅ (chưa rõ chi tiết evidence) |
| `storage` | ✅ vá 2026-08-10 (Đ49 B6), field alias đã đúng |
| `application` | ✅ vá 2026-08-10 (Đ49 B5), field alias đã đúng |
| `network` | ✅ `NetworkListenerLost` verified tcp/80 |
| `security` | ⏳ sống nhưng CHƯA đủ ✅ — chưa thấy `omni-siem-chains`, `case_ledger` chưa mở ca |
| `hardware` | ❌ không có collector — giới hạn kiến trúc, không phải gap |

→ 8/9 domain có ít nhất 1 lần chạy thật để lấy evidence mẫu (`hardware` không có collector nên chỉ
ghi nhận "N/A — không thể tạo evidence", không cần investigation sâu).

## Phương pháp

**Không cần chạy drill mới trên VM** cho domain đã có evidence thật trong log/Redis/Kafka — chỉ cần
tìm lại. Chỉ khi domain thiếu evidence mẫu hoàn toàn (`security` phần chain, `kubernetes` nếu không
tìm thấy) mới cân nhắc trigger 1 lần thật, và đó là quyết định riêng sau khi thấy kết quả Phase 1.

### Phase 0 — Chuẩn bị bản đồ code dùng chung (làm 1 lần, không lặp lại per-domain)
- Đọc `src/pkg/domain/taxonomy.py` (cascade `detect_domain`, `domain_hint`)
- Đọc `src/pkg/reasoning/domain_signals.py` + hàm `assess_domain_severity` (map field → mức độ)
- Đọc `src/workers/unified_incident_card.py` toàn bộ (đã có: `UnifiedCard` dataclass field list,
  `render_unified_card`, `render_audit_footer`, `render_recurrence_notice`)
- Đọc `src/services/reasoning/analyst_advisory_schema.py` (WHAT/WHO/WHY/HOW-TO + ForecastTimeline)
→ Output: 1 bảng ánh xạ "field nào trong AnalystAdvisory/UnifiedCard được điền từ đâu" — dùng chung
cho cả 8 domain, không cần đọc lại 8 lần.

### Phase 1 — 8 investigation song song, 1 domain/agent (độc lập, không phụ thuộc nhau)
Mỗi agent nhận đúng 1 domain, tự đọc:
- Collector: file tương ứng trong `src/remote_agent/collectors/` (hoặc `os_state_validator.py` cho
  k8s, `remote_host_baseline.py`/`three_sigma.py` cho os_host)
- Điểm vào `assess_domain_severity` cho domain đó (field/ngưỡng cụ thể)
- Tìm evidence thật: `grep`/`kubectl logs`/Redis (`omni:trace:*`, `corr:ent:*`) hoặc audit doc cũ đã
  dẫn ở bảng trên (vd Đ49, Đ39, S0-S3) — KHÔNG tự bịa mẫu
- Nếu tìm được 1 case thật đầy đủ: trích nguyên văn nội dung Telegram card cuối cùng (hoặc dựng lại
  từ `AnalystAdvisory` đã log nếu tin nhắn không còn) và đánh giá theo 5 câu hỏi ở Mục tiêu.
- Nếu KHÔNG tìm được evidence thật: ghi rõ "chưa từng có ANOMALY thật cho domain này, chỉ có
  METRIC_SAMPLE" — không dựng giả.

Domain nào KHÔNG dùng agent riêng: `hardware` (ghi N/A trực tiếp, không cần investigate).

### Phase 2 — Tổng hợp (sau khi cả 8 xong)
- 1 bảng master: domain × {collector, verdict-source, pipeline path, Telegram field thật, đánh giá
  hữu ích cho operator: Có/Một phần/Không}
- Liệt kê pattern lặp lại giữa các domain (nếu WHAT/WHY luôn generic, ghi rõ đây là root cause
  chung — không lặp lại nhận xét riêng lẻ 8 lần)
- Không đề xuất fix trong tài liệu này (đây là audit, không phải plan sửa) — chỉ nếu user yêu cầu
  ở bước sau mới lên kế hoạch sửa.

## Output

1 file duy nhất: `docs/audit/domain_telegram_evidence_audit_2026-08-11.md`
- Bảng master (Phase 2)
- 8 mục chi tiết (Phase 1), mỗi mục self-contained: collector → payload → pipeline → Telegram thật
  → đánh giá.
- Trích dẫn nguồn cho mọi claim (đường dẫn file:dòng, hoặc audit doc cũ, hoặc lệnh đã chạy) — không
  câu nào không có nguồn.

## Ước lượng effort

8 agent song song (Phase 1) + 1 tổng hợp (Phase 2) = phù hợp `general-purpose` Agent tool, không
cần Workflow multi-agent trừ khi bạn muốn chạy tự động toàn bộ không giám sát — quy mô này (9
domain, đọc code có sẵn, không sinh nhiều tầng verify) nằm trong khả năng làm trực tiếp bằng vài
lần Agent tool tuần tự/song song, không cần opt-in workflow.

## Câu hỏi cần bạn xác nhận trước khi chạy Phase 1

- Domain `security` và `kubernetes` nếu Phase 1 phát hiện KHÔNG có evidence thật nào từng chạy hết
  pipeline: có muốn trigger 1 drill thật ngay trong phiên này để lấy mẫu, hay chỉ ghi nhận "chưa có
  dữ liệu" và dừng ở đó?
