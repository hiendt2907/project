# SRE Cognitive Model — Autonomous Infrastructure Intelligence

> **Status**: DESIGN ONLY — không code, không refactor.
> **Created**: 2026-06-29
> **Vị trí trong stack 7 tầng** (CTO chốt):
> `Vision → Operating Model → **Cognitive Model** → Domain Model → Knowledge Model → Architecture → Implementation`
> Operating Model = "làm việc thế nào (quy trình)". **Cognitive Model = "TƯ DUY thế nào (suy luận)"** — tầng quyết định thành/bại.

---

## 0. Phát hiện cốt lõi: Senior KHÔNG chạy theo Playbook

```
Playbook nói:  CPU 98% → "Investigate CPU" → done
Senior nghĩ:   CPU cao → bình thường không? → server này làm gì? → giờ cao điểm?
               → user hay system CPU? → có deploy mới? → batch job? → cache miss?
               → lock? → IO wait? ...
```

Đó KHÔNG phải Playbook. Đó là **Hypothesis Generation**. Ta đã có *Knowledge Acquisition* (thu thập), nhưng thiếu *Reasoning Strategy* (suy luận). Cognitive Model bổ sung 6 cơ chế:

```
1. Hypothesis Engine     — sinh / xếp hạng / bác bỏ giả thuyết
2. Reasoning Playbook     — cây giả thuyết chuẩn theo lớp triệu chứng (≠ Operating Playbook)
3. Temporal Knowledge     — tri thức có thời gian; correlation theo timeline
4. Trust Model            — không nguồn nào tin như nhau
5. Organization Model     — học CON NGƯỜI, không chỉ hệ thống
6. Failure Model          — "tôi có thể sai" là first-class
   ─────────────────────────────────────────────
   → tất cả bồi đắp DECISION GRAPH (tài sản lớn nhất)
```

---

## Q1. Senior hình thành giả thuyết như thế nào?

Không random, không "hỏi LLM trước". Giả thuyết sinh từ **4 nguồn ưu tiên**:

```
Observation (CPU 98% on payment-svc)
   │
   ├─ A. PRIORS từ Experience    — "JVM service CPU spike → 70% là GC/leak"  (base-rate)
   ├─ B. DECISION GRAPH          — lần trước triệu chứng này → nguyên nhân gì (muscle memory)
   ├─ C. TEMPORAL correlation    — có change/deploy/batch gần thời điểm? (Q6)
   └─ D. TOPOLOGY (Twin)         — entity này phụ thuộc gì → cause có thể ở downstream
        ▼
   Hypothesis Set { h1, h2, h3, ... }
```

Mỗi **Hypothesis** là object có thể kiểm chứng (không phải câu chữ mơ hồ):

```
Hypothesis {
  claim:              "CPU cao do GC pause tăng"
  mechanism:          "heap pressure → full GC → CPU user cao"
  predicted_evidence: ["jvm_gc_time tăng", "heap_used >90%", "CPU user >> system"]
  prior_probability:  0.45     # từ experience/base-rate
  origin:             EXPERIENCE | DECISION_GRAPH | TEMPORAL | TOPOLOGY
  cost_to_test:       LOW
}
```

→ **LLM chỉ hỗ trợ sinh hypothesis từ 4 nguồn trên, không tự bịa.** Business logic (mechanism, predicted_evidence) là dữ liệu trong Reasoning Playbook, không nằm trong prompt.

### Reasoning Playbook (≠ Operating Playbook)

Operating Playbook = "làm gì theo thứ tự". **Reasoning Playbook = cây giả thuyết chuẩn cho một lớp triệu chứng**:

```
Symptom "High CPU"  →  hypothesis tree (ranked):
   ├─ recent deployment      (test: change timeline)        prior .30
   ├─ batch/cron job         (test: process + schedule)     prior .20
   ├─ GC / memory pressure   (test: gc metrics)             prior .15
   ├─ lock contention        (test: thread dump/db locks)   prior .10
   ├─ IO wait                (test: iostat)                  prior .10
   ├─ cache miss storm       (test: cache hit-rate)         prior .10
   └─ ...
```

Mỗi tenant/môi trường **điều chỉnh prior** theo lịch sử thật → đây là chỗ "Senior 5 năm tại công ty này" hình thành.

---

## Q2. Senior loại bỏ giả thuyết như thế nào?

**Falsification-first** (Popper, không phải confirmation). Vòng:

```
Observation → Generate Hypotheses → RANK → Test (rẻ nhất / prior cao nhất trước)
                  ▲                                  │
                  │                                  ▼
          Generate New  ◄──── REJECT ──── predicted_evidence vắng mặt / mâu thuẫn
                                  │ (survive)
                                  ▼
                          posterior cao → ROOT CAUSE
```

- Mỗi hypothesis **dự đoán bằng chứng**. Thu evidence → so với prediction.
- **Bác bỏ** khi: predicted_evidence không xuất hiện, HOẶC có counter-evidence từ nguồn trust cao.
- Cập nhật posterior kiểu Bayesian nhẹ: `posterior ∝ prior × P(evidence|hypothesis)`.
- **Ưu tiên test rẻ + phân biệt mạnh** (test loại được nhiều hypothesis nhất / chi phí thấp nhất).
- Bác bỏ hết → sinh hypothesis mới (mở rộng từ Decision Graph / hỏi human).

→ Đây chính là `Observation → Hypothesis → Rank → Evidence → Reject → New Hypothesis → Root Cause` mà CTO vẽ.

---

## Q3. Senior đánh giá độ tin cậy từng nguồn ra sao? → **Trust Model**

Không nguồn nào tin như nhau. **Trust = f(source_type, facet, recency, track_record)** — KHÔNG phải hằng số toàn cục.

```
Base trust theo loại nguồn (cho câu hỏi "thực tế đang là gì"):
   Runtime introspection   100   ← ground truth tuyệt đối
   Owner (đúng domain)      95
   Monitoring/metrics       90
   Config (declared)        70    ← ý định, chưa chắc = thực tế
   Architect (topology)     85
   Developer (code/API)     80
   Operator (deploy)        75
   Intern / second-hand     50
   Wiki                     40    ← dễ lỗi thời
   README                   20    ← thường stale nhất
   LLM inference            thấp  ← chỉ interpret, không phải nguồn
```

**3 hiệu chỉnh quan trọng:**
1. **Theo facet** (nối Organization Model Q7): DBA trust 95 cho DB, nhưng ~50 cho network. Trust gắn `(person, domain)`.
2. **Theo recency** (nối Temporal Q6): trust giảm theo `now - verified_time`. Doc viết 2 năm trước < runtime đo 1 phút trước.
3. **Theo track-record**: nguồn từng đúng/sai → trust tự điều chỉnh (Owner hay nhầm → giảm dần).

**Conflict resolution** (config nói A, runtime nói B):
```
"Thực tế đang là gì"      → Runtime THẮNG luôn (trust 100). Sinh fact + flag "config drift".
"Ý định/thiết kế là gì"   → Config/Owner thắng.
"Tại sao"                 → Owner/Architect/Decision Graph thắng.
Tie → recency + track_record quyết định. Vẫn tie → hỏi human (Q5/Q7).
```

---

## Q4. Senior quyết định "đủ bằng chứng" khi nào?

Stop criteria nhiều điều kiện (KHÔNG phải "thu hết log rồi dừng"):

```
ĐỦ bằng chứng khi:
  1. posterior(top hypothesis) ≥ threshold       (vd 0.85)
  2. AND khoảng cách top vs #2 đủ lớn            (không còn ambiguity)
  3. AND các hypothesis còn lại đã bị bác bỏ rõ
  4. AND cost(thêm evidence) > value(thêm evidence)   ← dừng đúng lúc

GATE theo hậu quả hành động:
  - Hành động đảo ngược được, blast nhỏ  → threshold thấp hơn (dám act)
  - Hành động KHÔNG đảo ngược / blast lớn → threshold cao + bắt buộc human
```

→ "Đủ" là **hàm của độ chắc + chi phí + rủi ro hành động**, không phải lượng dữ liệu. Senior dừng sớm khi rủi ro thấp, đào sâu khi rủi ro cao.

---

## Q5. Senior biết mình "đang hiểu sai" bằng cách nào? → **Failure Model**

"Tôi có thể sai" là **first-class**. Mọi belief mang confidence + có thể bị thách thức. 5 cơ chế phát hiện sai:

```
1. PREDICTION FAILURE   — đã act theo root cause nhưng triệu chứng không hết → hiểu sai
2. CONTRADICTION        — nguồn trust cao mâu thuẫn fact đang giữ
3. DRIFT                — Twin (skeleton) lệch reality khi re-verify
4. HUMAN CORRECTION     — admin nói "sai rồi"
5. VERIFICATION EXPIRY  — fact quá last_verified_at + TTL → nghi ngờ lại (Q6)
```

**Khi phát hiện sai — quy trình sửa tri thức:**
```
Detect → Quarantine fact (hạ confidence, KHÔNG xóa ngay)
       → Conflict Resolution (Trust Model Q3 + Temporal Q6)
       → Re-verification mission (sinh mission verify lại)
       → Rollback Knowledge: fact sai → archive (giữ lịch sử "từng tin X, sai vì Y")
       → Decision Graph học: "lần sau triệu chứng này, đừng kết luận vội X"
```

→ Rollback knowledge có versioning (bitemporal Q6): không mất lịch sử niềm tin. "Từng nghĩ Redis ở :6379, sai, thực tế :6380 sau migration 2 tuần trước."

---

## Q6. Senior quản lý THỜI GIAN của tri thức thế nào? → **Temporal Model**

Tri thức KHÔNG tĩnh. Mọi fact mang **4 mốc thời gian (bitemporal)**:

```
VerifiedFact {
  observation_time : khi quan sát được
  valid_time       : khoảng thời gian fact ĐÚNG trong thực tế (from–to)
  verified_time    : lần cuối xác minh độc lập
  changed_time     : khi thực tế thay đổi (→ fact cũ hết valid)
}
```

- **Re-verification scheduling**: fact critical TTL ngắn → tự sinh mission re-verify. Confidence decay theo `now - verified_time`.
- **Correlation engine** (cái CTO nhấn mạnh): mọi event nằm trên một **timeline**; suy luận nhân quả bằng gần-thời-gian:
```
Redis restart @10:03  ⟵correlate⟶  Deployment @10:02     → "restart do deploy?"
CPU tăng dần 2 tuần trước  ⟵correlate⟶  Release vX       → "regression sau release?"
```
- Change history là first-class node trên Twin → "điều gì đã đổi gần đây" là câu hỏi đầu tiên của Senior cho mọi incident (nối Q1 nguồn C).

---

## Q7. Senior xây mô hình TỔ CHỨC thế nào? → **Organization Model**

SRE không chỉ học hệ thống — học **con người**. Khác hẳn Business Knowledge.

```
People Graph (per-tenant, residency: PII ở KHÁCH, Omni giữ role-skeleton ẩn danh):
   Person(anon-id) ──is──► Role (Owner/Lead/Architect/Developer/Operator/DBA/SRE)
   Person ──owns──► Entity        (anh Nam owns Payment)
   Person ──SME_of──► Domain      (chị Hương SME Redis/DB)
   Team ──responsible_for──► Capability
   EscalationChain: Operator → Lead → Architect → Owner
```

**Ứng dụng trực tiếp** (nối Trust Q3 + Question Strategy):
```
Muốn hiểu Redis  → KHÔNG hỏi CTO  → hỏi DBA (SME_of Redis = chị Hương)
Incident Payment → escalate theo chain của Payment, không phải chain chung
Trust câu trả lời = f(người, domain): DBA về Redis = 95, DBA về firewall = 50
```

→ "Hỏi ai, hỏi như thế nào" trở nên thông minh vì Omni có bản đồ tổ chức. Đây là phần khiến khách thấy như "Senior đã làm ở đây 5 năm" (biết ai phụ trách gì).

---

## ⭐ Decision Graph — tài sản LỚN NHẤT (lớn hơn Playbook)

Mọi suy luận để lại vết. Decision Graph lưu **chuỗi lý do**, không chỉ kết luận:

```
CPU cao → (xem IO trước) → vì Database → vì Query tăng → vì Deployment → ...
   mỗi cạnh: WHY (lý do chọn nhánh này) + outcome (đúng/sai) + cost + thời gian
```

- Lần sau gặp triệu chứng tương tự → **traverse Decision Graph** thay vì suy luận lại từ đầu (= muscle memory của Senior).
- Decision Graph + outcome → cập nhật **prior** trong Reasoning Playbook (Q1) → Omni "giỏi lên" đúng theo môi trường này.
- Đây là thứ **khó sao chép nhất** và là khác biệt cạnh tranh: không phải model, không phải prompt, mà là *lịch sử suy luận đã được kiểm chứng tại tenant này*.

```
Operating Playbook  = checklist (làm gì)        — tài sản #2
Reasoning Playbook  = cây giả thuyết (nghĩ gì)  — tài sản #1.5
Decision Graph      = lịch sử suy luận đã verify — TÀI SẢN #1
Experience          = pattern anonymized global  — tài sản chuyển giao cross-tenant
```

---

## Đổi TRIẾT LÝ dự án (CTO chốt)

```
SAI:   "Làm sao để AI thay thế Senior SRE?"
ĐÚNG:  "Làm sao để khách hàng mới có cảm giác họ vừa tuyển được một Senior SRE
        đã làm ở công ty họ 5 NĂM."
```

Tiêu chuẩn "Senior 5 năm tại đây" = **tacit knowledge đặc thù môi trường**, không phải kiến thức Linux/K8s chung:
- Biết hệ thống CỦA KHÁCH (Twin + Competency Matrix).
- Biết LỊCH SỬ thay đổi (Temporal + change history).
- Biết AI chịu trách nhiệm phần nào (Organization Model).
- Biết "bình thường" của môi trường này (baseline + prior điều chỉnh theo tenant).
- Biết khi nào hỏi, hỏi ai, hỏi thế nào (Question Strategy + Org Model + Trust).
- Biết khi nào tự hành động, khi nào DỪNG (Autonomy Decision + Failure Model).

→ Khi đạt trạng thái này, Omni KHÔNG còn là "Autonomous SRE Framework" mà là **Autonomous Infrastructure Intelligence Platform**. Tầm nhìn đủ lớn để đầu tư nhiều năm; rất ít dự án AI theo đuổi.

---

## Stack 7 tầng — bản đồ tài liệu

| Tầng | Tài liệu | Trạng thái |
|---|---|---|
| 1. Vision | memory `project_autonomous_sre_vision_v2` | ✅ (đổi triết lý: AIIP) |
| 2. Operating Model | `OPERATING_MODEL_sre.md` | ✅ |
| 3. **Cognitive Model** | `COGNITIVE_MODEL_sre.md` (này) | ✅ MỚI |
| 4. Domain Model | `DOMAIN_MODEL_autonomous_sre.md` | ✅ |
| 5. Knowledge Model | *(chưa tách riêng — nằm rải trong Domain/Cognitive)* | ⬜ TODO |
| 6. Architecture | `ASSESSMENT_autonomous_sre_v2.md` (đánh giá hiện trạng) | ◑ một phần |
| 7. Implementation | — | ⬜ chưa cho phép |

> **Tiến độ (CTO ước lượng): ~35–40%** của roadmap Vision→Implementation. Phần khó nhất (thiết kế "nghề SRE") đang được làm trước code — đúng hướng.

---

## Điểm cần CTO chốt tiếp

1. **Knowledge Model (tầng 5)** hiện chưa tách: gộp Twin + Competency Matrix + Temporal + Trust + Provenance thành một tài liệu riêng? (đề xuất: có — đây là "single source of truth" về tri thức).
2. **Reasoning Playbook representation**: data-driven (YAML hypothesis-tree) giống Operating Playbook?
3. **Decision Graph residency**: lưu ở đâu? Lý-do-suy-luận có thể chứa chi tiết khách → đề xuất Detail ở khách, Omni giữ pattern ẩn danh (như Experience).
4. **Bayesian update**: làm nhẹ bằng heuristic (rule-based posterior) hay xác suất thật? (đề xuất: heuristic trước, đo, rồi nâng).
5. **Prior calibration**: prior khởi tạo từ Experience global, rồi tự điều chỉnh per-tenant — cần cơ chế tránh overfit môi trường nhỏ.
