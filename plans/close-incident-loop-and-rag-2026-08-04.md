# Kế hoạch: mọi sự cố được xử lý triệt để + LLM/RAG tận dụng tối đa

**Ngày:** 2026-08-04 · **Cơ sở:** audit thực chứng Postgres + Redis + code sống (không suy đoán)
**Mục tiêu (/goal):** 1 sự cố phải THỰC SỰ được giải quyết · hệ thống phải TỰ HỌC · KHÔNG có cái sai trong im lặng

---

## 0. Nguyên tắc chốt trước — thứ tự KHÔNG được đảo

> **Không thể tối ưu LLM/RAG trước khi có nhãn đúng/sai.**

Bằng chứng đo được 2026-08-04:

| Chỉ số | Giá trị thật | Ý nghĩa |
|---|---|---|
| `ADVISORY_DECISION` trong CRAT | 801 (default) + 202 (staging-sim) | Engine chẩn đoán **chạy thật, không rỗng** |
| `case_ledger` có verdict | **0** (2 dòng, cả 2 `UNJUDGED`) | Không sự cố nào từng được chấm đúng/sai |
| `hitl_decision` | **0, mãi mãi** | Không quyết định người nào được ghi |
| `omni:kpi:z:*` | **0 key** | Không có mẫu nào để tính acceptance/FP rate |
| `omni:rag:sop` HLEN | 1019 | **Corpus seed tĩnh**, không phải trí nhớ sống |

⇒ RAG hiện tại không học từ kết quả thật; mọi chỉ số chất lượng advisory không đo được.
**Tối ưu LLM lúc này là tối ưu mù** — không biết advisory nào đúng thì không biết cắt gì.

Thứ tự bắt buộc: **đóng vòng phản hồi → có nhãn → RAG học → LLM tiết kiệm có kiểm chứng.**

---

## Phase A — Nối vòng phản hồi cho nhánh chính (BLOCKER)

Gốc của mọi bảng trống: nhánh chẩn đoán chính không có đường thu phản hồi.

### A1. `#28` — Nhánh chính phải mở case + gắn nút phản hồi
`remote_diagnosis_emitter.emit_diagnosis_to_telegram` (dùng bởi `_run_diagnosis_and_notify_inner`,
`remote_agent_pipeline.py:540-559`, chạy cho MỌI cluster critical/high) hiện gửi Telegram trần —
không `reply_markup`, không gọi `case_ledger`. Đây là nơi phần lớn 1003 quyết định thật đi qua.

- Gắn ack-keyboard 3 nút (CORRECT/INCORRECT/PARTIAL) như `advisory_ack.py` đã có sẵn
- Gọi `open_advisory_case()` trước khi emit — case_id gắn vào callback token
- **Done-when:** chạy drill thật trên VM lab → `case_ledger` có dòng mới với `diagnosis_verdict != UNJUDGED` sau khi bấm nút

### A2. `#27` — `hitl_decision` phải INSERT được (CRITICAL, silent failure)
Không nơi nào trong `src/` INSERT dòng PENDING. Telegram (`hitl_telegram.py:210`) chỉ UPDATE →
0 dòng khớp → asyncpg không raise → `except Exception: logger.warning` (dòng 213) nuốt sạch.
Portal (`autonomy.py:596`) luôn raise 409. **Cả 2 kênh chết 100% mà log trông bình thường.**

- INSERT dòng PENDING ngay tại điểm phát sinh `HITL_ESCALATION_EMITTED`
- Đổi `hitl_telegram.py:213` thành **fail-loud**: UPDATE 0-row = lỗi thật, log ERROR + metric
- **Done-when:** escalation thật → thấy dòng PENDING → approve qua Telegram → dòng chuyển APPROVED, có `decided_at`

---

## Phase B — Tín hiệu âm (không có nhãn SAI thì không phải học)

### B1. `#29` — Nút "❌ Sai" phải ghi được
`advisory_ack.py:309-315` khi verdict=INCORRECT chỉ *bỏ qua* `record_accepted`, không gọi
`record_false_positive`. Hàm đó chỉ có call site trong `_handle_feedback` (cần mutation thật —
không tồn tại ở Advisory/Shadow mode). ⇒ acceptance_rate chỉ có thể "tốt" hoặc "chưa có dữ liệu".

### B2. `#30` — Callback cũ 1-nút coi `verdict=None` là đồng ý
Tái tạo đúng lớp bug "đọc = đồng ý". `None` phải bị từ chối rõ ràng, không tính accepted.

- **Done-when:** bấm ❌ → `omni:kpi:z:{tenant}:false_positive` có mẫu; `read_outcome_rates` trả `fp_rate` thật (không `None`)

---

## Phase C — Bất biến chống "sai trong im lặng" (chống tái phát, không chỉ vá)

Đây là phần trả lời trực tiếp cho vế thứ 3 của /goal. Vá #27 chỉ đóng 1 lỗ; cần cơ chế khiến
lớp lỗi này **không thể tái sinh im lặng**.

### C1. Reconciliation loop CRAT ↔ case_ledger
CRAT là nguồn đáng tin nhất (hash-chain + Ed25519, fail-closed). Mọi `ADVISORY_DECISION` trong
CRAT **phải** có case tương ứng trong vòng N phút.

- Loop nền so 2 nguồn, phát metric `omni_ledger_divergence_total`
- Divergence > 0 kéo dài = alert thật (đây chính là loại lỗi vừa tìm ra, sẽ tự lộ ngay lần sau)
- **Done-when:** cố tình chặn 1 đường ghi case → alert nổ trong ≤ 1 chu kỳ

### C2. Quét toàn bộ `except` nuốt lỗi quanh đường ghi ledger
Đã biết: `advisory_ack.py:126,166,200,303,314,344` · `hitl_link.py:113` · `hitl_telegram.py:213`
· `advisory_promoter.py:128,154`. Phần lớn là best-effort **có chủ đích** — nhưng phải phân loại
rõ: best-effort (giữ) vs che lỗi thật (sửa thành fail-loud + metric).

### C3. `#31` — PlaybookGovernor write-through Postgres
Hiện `playbook_graduation` Postgres 0 dòng **không chứng minh được gì** về engine gate thật
(sống trong Redis). Bảng đang vô dụng cho audit.

---

## Phase D — RAG thành trí nhớ sống (chỉ khả thi SAU Phase A+B)

### D1. Ingest có nhãn
Mỗi case đóng với verdict thật → ghi vào `omni:rag:sop` kèm nhãn outcome + domain + host context.
Hiện 1019 entry là seed tĩnh; sau phase này corpus mới bắt đầu phản ánh hạ tầng khách hàng thật.

### D2. Recall-first gate (tiết kiệm LLM có kiểm chứng)
Áp dụng đúng nguyên tắc đã có: gọi `recall_playbook_advisory()` **trước** khi quyết định gọi LLM;
recall score ≥ 0.75 → dùng lại, **bỏ hẳn LLM**.

- Đo tỉ lệ skip thật bằng metric, không ước lượng
- **Chỉ bật sau khi có nhãn** — skip dựa trên corpus chưa verify là khuếch đại lỗi

### D3. Embedding một lần, tái dùng vector
Khi ghi cùng nội dung vào nhiều collection: embed đúng 1 lần, reuse `vector`.

---

## Phase E — Tối ưu LLM (đo được, không phỏng đoán)

### E1. Neo bằng benchmark có sẵn
`make benchmark-advisory` là thước đo duy nhất đã tồn tại (baseline lịch sử: 30.4% → 43.5%
root-cause, avg 63.5 → 69.7). **Mọi thay đổi prompt/model phải chạy trước-sau**, không merge nếu tụt.

### E2. Cắt lãng phí đã định lượng được
- Multi-turn ReAct thật: `total_turns` 2–5/quyết định (đọc từ CRAT). Sau khi có recall-first,
  đo lại phân bố turns — mục tiêu giảm turn thừa, **không** giảm bằng cách cắt bằng chứng
- Advisory `META_SELF_DETERMINISTIC` **không tốn LLM** (mode `deterministic_contrast`) — đã kiểm,
  không phải chỗ để tối ưu. Đừng đuổi nhầm mục tiêu
- Grounding gate đang bắt được LLM thổi phồng confidence thật (`confidence_inflation`: turn 2,
  0.0 → 0.65 với `unsupported_quantities: [cpu]`) — **giữ nguyên**, đây là cơ chế chống bùa số đang hoạt động

### E3. Tham số
`OMNI_LLM_NUM_CTX` 8192 qua `build_llm_options(ctx)` (không inline getattr) · semaphore
`proactive|reactive` — chỉ chỉnh khi benchmark chứng minh có lợi.

---

## Phase F — Chứng minh bằng drill thật (nghiệm thu /goal)

Không nhận "test pass" làm bằng chứng. Một sự cố đi trọn vòng trên VM lab thật:

```
gây lỗi thật trên VM  →  agent phát hiện  →  Omni chẩn đoán (CRAT ADVISORY_DECISION)
   →  case_ledger mở case  →  Telegram có nút  →  người chấm đúng/sai
   →  hitl_decision ghi PENDING→APPROVED  →  KPI có mẫu (cả + và −)
   →  RAG ingest có nhãn  →  lần sau recall hit, bỏ LLM  →  case đóng, verdict != UNJUDGED
```

**Nghiệm thu = truy vấn được toàn bộ chuỗi trên Postgres/Redis thật**, mỗi mắt xích có dòng dữ liệu.

---

## Thứ tự thực thi & phụ thuộc

```
A2 (#27 CRITICAL) ─┐
A1 (#28 HIGH)     ─┴─→ B1/B2 (#29,#30) ─→ D1 ─→ D2/D3 ─→ E1/E2 ─→ F
C1 (bất biến)  ────────→ chạy song song, không chặn ai
C2/C3 (#31)    ────────→ dọn dẹp, độc lập
```

- **A2 trước A1**: A1 tạo thêm traffic vào đúng đường A2 đang gãy im lặng
- **C1 nên làm sớm**: nó là cái lưới bắt mọi lỗi cùng lớp trong các phase sau
- **D/E bị chặn cứng bởi A+B** — không có nhãn thì không có gì để học và không đo được gì

## Kỷ luật giữ nguyên

Scope Freeze · 1 commit = 1 concern · mỗi task rollback độc lập · verify sống trong pod
(`kubectl exec`) chứ không tin "rollout successful" · thấy lỗi ngoài scope thì **log task,
không tự sửa im lặng**.
