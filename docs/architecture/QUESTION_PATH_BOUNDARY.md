# Ranh giới "hỏi khi không biết" — question_lifecycle vs _detect_gaps_and_ask

> Phase 1 của `plans/omni-close-autonomous-sre-gaps-2026-07-23.md`. Quyết định:
> **GIỮ CẢ HAI PATH, KHÔNG migrate-and-delete.** Xác nhận lại premise sau adversarial
> review (Opus) bằng cách đọc trực tiếp `question_lifecycle.py:3-15` và
> `_detect_gaps_and_ask` (`workers/onboarding_pipeline.py:177-239`) — không tìm thấy
> bằng chứng overlap thật nào khác với kết luận review.

## Câu hỏi cần trả lời

> "Hai path cùng có tên `_open_question`/`ensure_question_for_unknown`/`QUESTIONS_KEY`
> và cùng gửi Telegram khi không biết một điều gì đó — đây có phải 2 nguồn sự thật
> cho cùng 1 loại câu hỏi (vi phạm `INV_SINGLE_SOURCE_OF_TRUTH`) không?"

**Trả lời: không.** Chúng trả lời hai loại câu hỏi khác nhau, ghi vào hai namespace
Redis khác nhau, phục vụ hai consumer khác nhau, và không bao giờ đọc/ghi chéo dữ
liệu của nhau.

## Bản chất từng path

### `workers/onboarding_pipeline.py::_detect_gaps_and_ask` (legacy, giữ nguyên)

- **Câu hỏi nó trả lời**: "Probe onboarding vừa chạy (`service_topology`/
  `port_scan`/`api_access`) có lộ ra 1 khoảng trống rõ ràng, hẹp, cụ thể-theo-probe
  không?" — ví dụ: toàn bộ service chưa có mô tả, cổng mở chưa rõ dịch vụ, route API
  thấy trong access log nhưng chưa có OpenAPI contract.
- **Câu hỏi là gì**: free-text tiếng Việt, sinh trực tiếp từ nội dung `discovery_data`
  của 1 probe event cụ thể — không có khái niệm entity/facet, không dedup theo
  fingerprint (chỉ dedup thô cho case `api_access` bằng cách quét text
  `"OpenAPI/Swagger"` đã tồn tại chưa).
- **Ghi vào đâu**: `pkg.onboarding.discovery_doc.QUESTIONS_KEY` =
  `omni:onboarding:questions:{tenant_id}` và `QUESTIONS_OPEN_KEY` =
  `omni:onboarding:questions_open:{tenant_id}` (xem `discovery_doc.py:12-13`).
- **Ai đọc lại**: CHỈ `onboarding_pipeline.py` chính nó (dedup check dòng 208, resolve
  dòng 244/252) — grep xác nhận không module nào khác import
  `dd.QUESTIONS_KEY`/`discovery_doc.QUESTIONS_KEY`. Đây là input cho readiness
  scoring/UI onboarding, không cho Competency Matrix.
- **Vòng đời**: bắn ngay lập tức trong `accumulate_discovery_evidence` (inline, mỗi
  probe event), không có bước "batch/pace" — 1 câu hỏi cho 1 điều kiện thoả trong 1
  probe.

### `aoip/question_lifecycle.py::ensure_question_for_unknown` (mới, O2B)

- **Câu hỏi nó trả lời**: "Competency Matrix của entity X đang thiếu/mâu thuẫn 1
  facet quan trọng (`owner`/`monitoring`/`sla`) — có nên hỏi người không, và nếu có,
  hỏi đúng 1 lần (dedup theo fingerprint) không?"
- **Câu hỏi là gì**: entity/facet-aware — sinh từ `FacetState` (UNKNOWN/CONTRADICTED)
  của `competency_matrix.py`, không đọc trực tiếp `discovery_data` thô. Câu trả lời
  (Answer) không tự động trở thành sự thật — chỉ tạo `Claim` (CLAIMED), và chỉ
  `competency_matrix` mới nâng lên VERIFIED khi có Fact máy móc corroborate (xem
  docstring dòng 11-14 và `docs/architecture/CONFIDENCE_AXES_BOUNDARY.md`).
- **Ghi vào đâu**: `question_lifecycle.QUESTIONS_KEY` = `omni:aoip:questions:
  {tenant_id}` — namespace `aoip:` khác hẳn `onboarding:` ở path legacy, dù tên biến
  Python trùng (`QUESTIONS_KEY`). Cùng vậy, `UNKNOWNS_KEY`/`ANSWERS_KEY` đều nằm dưới
  `omni:aoip:*`.
- **Ai đọc lại**: `question_lifecycle.py` (dedup fingerprint, expire), `console/
  overview.py` (import trực tiếp `QUESTIONS_KEY` để hiển thị dashboard), và gián
  tiếp qua `EntityCompetency`/Claim bởi `console/human_inbox.py`,
  `console/understanding.py`, `gateway/routes/onboarding.py`.
- **Vòng đời**: KHÔNG bắn inline mỗi probe — `onboarding_pipeline.py::
  _sync_understanding_gaps` (dòng 127-160) chỉ đồng bộ `Unknown` record (bookkeeping,
  không tạo Question, không gửi Telegram — xem docstring dòng 130-138 tại chỗ), việc
  biến 1 Unknown thành Question thật là bước tách biệt, có chủ đích, dành cho 1
  caller đã batch/pace (chưa wired vào 1 cron/loop cụ thể tại thời điểm audit này —
  ngoài phạm vi Phase 1, không sửa ở đây).

## Bảng field-by-field (tên trùng, namespace/ngữ nghĩa khác)

| Điểm chung bề mặt | Path legacy (`onboarding_pipeline`) | Path mới (`question_lifecycle`) | Có phải cùng 1 nguồn sự thật? |
|---|---|---|---|
| Tên biến `QUESTIONS_KEY` | `omni:onboarding:questions:{tenant_id}` | `omni:aoip:questions:{tenant_id}` | **Không** — namespace Redis khác nhau hoàn toàn, không đọc/ghi chéo (grep xác nhận). |
| Cùng gửi Telegram | `_open_question` gọi `ctx.telegram.send_message` trực tiếp | `ensure_question_for_unknown` tạo record, gửi Telegram do batched caller riêng thực hiện | Cùng kênh vận chuyển (Telegram), khác nguồn nội dung và khác cơ chế dedup. |
| Cùng mục đích chung ("hỏi khi thiếu thông tin") | Đúng ở tầng ý định sản phẩm | Đúng ở tầng ý định sản phẩm | Đây là lý do bề ngoài trông giống nhau, nhưng "cùng ý định sản phẩm" không phải là "cùng 1 nguồn sự thật kỹ thuật" — mỗi path tự chủ hoàn toàn về input/storage/consumer. |
| Input | `discovery_data` thô của 1 probe (service list, port list, access log routes) | `FacetState` đã derive (owner/monitoring/sla UNKNOWN/CONTRADICTED) | Input hoàn toàn khác nguồn — 1 bên đọc probe payload trực tiếp, 1 bên đọc Competency Matrix đã tính. |
| Dedup | Thô, per-probe-type (case `api_access` quét text đã hỏi) | Fingerprint xác định (`_fingerprint` theo entity+facet) | Cơ chế khác nhau, không share state dedup. |
| Answer xử lý ra sao | Không có bước "answer → Claim" — chỉ đóng câu hỏi thủ công (`resolve_question`, dòng 242-252 trong onboarding_pipeline.py) | `submit_answer` → `Claim` (CLAIMED, chưa VERIFIED) qua `claims_store.put_claim` | Path mới có full lifecycle Answer→Claim; path cũ dừng ở "hỏi rồi ghi nhận đã hỏi", không model hoá câu trả lời thành tri thức có thể verify. |

**Kết luận từ bảng**: điểm chung duy nhất là ý định sản phẩm ("hỏi khi thiếu thông
tin qua Telegram") và tên biến Python trùng do trùng convention đặt tên
(`QUESTIONS_KEY`) — không phải bằng chứng 2 hệ thống cùng ghi 1 nguồn sự thật. Mọi
input/storage-namespace/consumer/dedup/answer-handling đều tách biệt.

## Ranh giới quyết định cho người viết code tương lai

- **"Câu hỏi X nên đi qua path nào?"** — ví dụ cụ thể:
  - *"Vừa quét thấy 5 service không có mô tả nghiệp vụ — hỏi luôn trong probe này"*
    → `_detect_gaps_and_ask` (per-probe, free-text, không cần entity/facet model).
  - *"Competency Matrix báo `owner` của host `cust-db` vẫn UNKNOWN sau nhiều lần
    quét — cần hỏi 1 lần, dedup, và câu trả lời phải có thể verify sau này bằng
    Fact máy móc"* → `question_lifecycle.ensure_question_for_unknown` (entity/facet
    aware, Answer→Claim→VERIFIED lifecycle).
  - *"Cần hiển thị danh sách toàn bộ câu hỏi đang mở cho dashboard understanding"*
    → đọc `question_lifecycle.QUESTIONS_KEY` (đã có consumer `console/overview.py`),
    KHÔNG đọc `discovery_doc.QUESTIONS_KEY` (đó là sổ sách nội bộ của onboarding
    readiness, không phải nguồn hiển thị tri thức entity).

## Không migrate — vì sao

Migrate (xoá `_detect_gaps_and_ask`, chuyển toàn bộ sang `question_lifecycle`) sẽ
buộc thêm entity/facet model cho 3 loại câu hỏi hiện tại vốn không cần nó (mô tả
service, xác nhận cổng, thiếu OpenAPI contract — đều là câu hỏi nông, một-lần,
per-probe, không có khái niệm "facet của entity" tự nhiên đi kèm). Đó là thêm độ
phức tạp không cần thiết (vi phạm YAGNI) để giải quyết một vấn đề (SSOT violation)
không thực sự tồn tại theo bằng chứng ở bảng trên. Quyết định: **giữ 2 path tách
biệt**, đã tài liệu hoá cross-reference trong docstring của cả 2 file.

## Ảnh hưởng tới Phase 5 (không đổi)

- Kịch bản E2E "Senior SRE nhận bàn giao" ở Phase 5 khi cần "hỏi người vì gặp gap
  kiến thức entity/facet" (ví dụ unit systemd lạ không có trong KB) phải đi qua
  `question_lifecycle.py` (Unknown→Question→Answer→Claim), KHÔNG qua
  `_detect_gaps_and_ask` — path đó chỉ dành cho gap onboarding per-probe nông, không
  có Answer→Claim lifecycle mà Phase 5 bước 3-4 cần.
