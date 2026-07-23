# Confidence Axes Boundary — VerificationResult vs FacetState

> Phase 3 của `plans/omni-close-autonomous-sre-gaps-2026-07-23.md`. Quyết định:
> **GIỮ RANH GIỚI RÕ, KHÔNG hợp nhất.** Nghiêng theo premise mặc định của phase —
> bảng field-by-field dưới đây không lộ bằng chứng ngược lại (không tìm thấy 1 truth
> bị duplicate/persist song song).

## Câu hỏi cần trả lời

> "`VerificationResult` và `FacetState` có đo cùng 1 thứ (vi phạm
> `INV_SINGLE_SOURCE_OF_TRUTH`) hay 2 chiều khác nhau?"

**Trả lời: 2 chiều khác nhau — per-mutation-attempt vs per-knowledge-facet.** Không
vi phạm SSOT vì không có 2 nguồn cùng tuyên bố "sự thật" cho cùng một câu hỏi; chúng
trả lời hai câu hỏi khác nhau.

## Bản chất từng trục

### `VerificationResult` (`src/aoip/verification.py`)

- **Câu hỏi nó trả lời**: "Ngay bây giờ, HÀNH ĐỘNG vừa Execute có đạt đúng
  `expected_state` mà nó dự định tạo ra không?" — one-shot, gắn với 1 primitive-verb
  cycle Validate→Execute→**Verify**→Recover/Escalate của MỘT `RecoveryOutcome`/mutation.
- **Ai tạo**: `recovery.py::_verify_and_finalize` (hoặc adapter tương đương) sau khi
  Execute một Action; luôn transient — sống trong bộ nhớ 1 request, không có store
  layer.
- **Ai đọc**: `RecoveryOutcome.verification` field → 3 capability hiện có
  (`systemd_restart.py:435`, `systemd_reset_failed.py:368`,
  `systemd_journal_vacuum.py:408`) gọi `outcome.verification.to_dict()` để nhét vào
  structured audit payload (`log_action(...)`). Đây là **ghi vào audit event log**
  (append-only CRAT trail), KHÔNG phải "current known state" — không mâu thuẫn với
  `INV_DERIVED_NEVER_PERSIST` vì không có object nào tái tính lại từ bản ghi audit
  này để làm "sự thật hiện tại".
- **Vòng đời**: được tạo mới cho MỖI lần verify, không tồn tại giữa các lần gọi, và
  bị bỏ qua sau khi audit ghi xong (không phải nguồn để hỏi lại "trạng thái hiện tại
  của X là gì").

### `FacetState` (`src/aoip/competency_matrix.py`)

- **Câu hỏi nó trả lời**: "Omni hiểu bao nhiêu về facet Y của entity X, dựa trên toàn
  bộ lịch sử Fact + Claim + contradiction đã tích luỹ?" — long-lived epistemic state
  của 1 tri thức, không gắn với 1 lần mutate cụ thể nào.
- **Ai tạo**: `build_entity_competency()` — PURE DERIVED PROJECTION
  (`INV_DERIVED_NEVER_PERSIST`), tính lại mỗi lần gọi từ `SystemModel` (Facts đã
  persist) + `claims_store` + contradiction log. Không có write-path riêng —
  `competency_matrix.py` không import bất kỳ hàm ghi Redis nào.
- **Ai đọc**: `question_lifecycle.py` (quét UNKNOWN/CONTRADICTED để sinh Question),
  `console/human_inbox.py` + `console/understanding.py` (UI hiển thị), `gateway/
  routes/onboarding.py` (API `/onboarding/entities`), `workers/onboarding_pipeline.py`,
  `pkg/onboarding/discovery_doc.py` (readiness doc).
- **Vòng đời**: KHÔNG có instance riêng tồn tại lâu dài — mỗi lần đọc là 1 lần tính
  lại từ input persist. Cùng input → cùng output (deterministic, reconstructable).

## Bảng field-by-field

| `VerificationResult` field | `FacetValue` field tương ứng? | Kết luận |
|---|---|---|
| `status` (PASS/FAIL/UNKNOWN) | `state` (UNKNOWN/OBSERVED/CLAIMED/VERIFIED/CONTRADICTED/STALE/NOT_APPLICABLE) | **Trùng tên gọi ý niệm ("verified"/"pass"), KHÔNG trùng ý nghĩa.** `status=PASS` nghĩa là "hành động vừa chạy đạt kết quả mong đợi ngay lúc verify". `state=VERIFIED` nghĩa là "tri thức về facet này đã được 2+ nguồn độc lập/corroborate qua thời gian" (`_identity_facet`: cần ≥2 Fact riêng biệt). Một cái đo tức thời 1 hành động, một cái đo độ tin cậy tích luỹ của 1 tri thức. |
| `expected_state` (str, bắt buộc) | *(không có)* | Riêng của VerificationResult — mô tả trạng thái đích 1 mutation nhắm tới. FacetValue không có khái niệm "đích" vì nó không mô tả 1 hành động. |
| `checks` (dict, tuỳ chọn) | *(không có)* | Riêng của VerificationResult — chi tiết probe check (vd `systemctl is-active`). |
| `evidence_refs` (tuple[str], bắt buộc non-empty) | `evidence_refs` (tuple[str], mặc định rỗng) | **Tên trùng, encoding tương thích (cả hai đều là chuỗi provenance-ref), nguồn gốc khác nhau.** VerificationResult.evidence_refs là output của probe verify tại thời điểm đó; FacetValue.evidence_refs là `Fact.provenance` tích luỹ + `human:{answered_by}`/`question:{id}` nếu có Claim. Không có chỗ nào 2 tập evidence này được hợp nhất hay dùng thay thế nhau. |
| `confidence` (float 0-1, bắt buộc trong post_init) | `confidence` (float 0-1, mặc định 0.0) | **Tên trùng, phép tính khác nhau, scope khác nhau.** VerificationResult.confidence do verifier tự báo cáo cho 1 lần check (thường 1.0 mặc định trong `pass_`/`fail`). FacetValue.confidence lấy từ `Fact.confidence` (max/aggregate qua nhiều Fact), chịu ảnh hưởng bởi corroboration count (`_identity_facet` hạ trần 0.7 nếu chưa corroborate) và bởi Claim. Không thể gán trực tiếp 1 sang 1. |
| `reason` (str, bắt buộc nếu UNKNOWN) | *(không có field riêng, nhưng `question_lifecycle._reason_for(state)` tính reason TỪ `FacetState` khi cần sinh Question)* | Không phải field lưu trong FacetValue — là hàm phái sinh ở tầng tiêu thụ (`question_lifecycle.py`), không phải bằng chứng 2 object dùng chung field. |
| *(không có)* | `value` (Any) | Riêng của FacetValue — giá trị thực tế của facet (vd owner name, port list). VerificationResult không mang "giá trị tri thức", chỉ mang trạng thái pass/fail của 1 hành động. |
| *(không có)* | `source_types` (tuple[str]) | Riêng của FacetValue — phân loại nguồn (discovery/human/agent) để UI hiển thị. Không tương ứng gì bên VerificationResult. |
| *(không có)* | `last_observed_at` / `last_verified_at` (timestamp) | Riêng của FacetValue — theo dõi độ tươi (freshness) qua thời gian, khái niệm không tồn tại cho 1 verification tức thời (nó luôn "now"). |

**Kết luận từ bảng**: 3/6 field của VerificationResult trùng TÊN với FacetValue
(`evidence_refs`, `confidence`, và ý niệm status/state), nhưng không trùng NGỮ NGHĨA
hay VÒNG ĐỜI ở bất kỳ cặp nào — không có "1 khái niệm confidence bị lưu 2 lần". Đây
là 2 trục orthogonal: per-action (transient, audit-logged) và per-facet (derived,
never-persisted). Việc dùng tên giống nhau (`evidence_refs`, `confidence`) là quy ước
đặt tên chung hợp lý cho "bằng chứng"/"độ tin cậy" trong toàn bộ AOIP — không phải
dấu hiệu duplicate.

## Ranh giới quyết định cho người viết code tương lai

- **"X thuộc VerificationResult hay FacetState?"** — ví dụ cụ thể:
  - *"Lệnh `systemctl restart nginx` mà agent vừa chạy có thành công không?"* →
    `VerificationResult` (per-action, tức thời, gắn 1 `RecoveryOutcome`).
  - *"Omni có biết ai là owner của host `cust-db` không, và độ tin cậy bao nhiêu?"*
    → `FacetState`/`FacetValue` (per-knowledge-facet, derived từ Fact+Claim tích luỹ).
  - *"Sau khi restart, unit đã về trạng thái `active` chưa?"* → `VerificationResult`
    (đây là bước Verify của chính hành động restart đó).
  - *"Sau khi restart nhiều lần, Omni có coi `runtime_state` của service này là
    VERIFIED không?"* → `FacetState` (câu hỏi về tri thức tích luỹ theo thời gian,
    không phải kết quả 1 lần hành động).

## Không hợp nhất — vì sao

Hợp nhất sẽ buộc chọn 1 trong 2 hướng xấu:
1. Persist `FacetState` per-action để khớp vòng đời VerificationResult → vi phạm
   trực tiếp `INV_DERIVED_NEVER_PERSIST`.
2. Làm `VerificationResult` trở thành long-lived/derived (tính lại theo thời gian) →
   phá hợp đồng hiện có mà 3 capability + `recovery.py` đang dùng làm audit-event
   transient, không có ý nghĩa "tái tính lại 1 verification cũ".

Cả 2 hướng đều tệ hơn giữ nguyên ranh giới. Quyết định: **giữ 2 object tách biệt**,
tài liệu hoá cross-reference trong docstring của cả 2 file.

## Ảnh hưởng tới Phase 4 / Phase 1 (không đổi)

- `VerificationResult` shape KHÔNG đổi (không sửa `verification.py`) — Phase 4 có thể
  tiếp tục dùng `outcome.verification.to_dict()` đúng như 3 capability hiện có, không
  cần re-check breaking change.
- `FacetState`/`EntityCompetency`/`FACET_PREDICATE` KHÔNG đổi field
  (`competency_matrix.py` không sửa) — Phase 1 có thể re-read và thấy import ở
  `question_lifecycle.py:27` vẫn nguyên vẹn.
