# Capability Model — Trục gốc của AOIP

> **Status**: DESIGN ONLY — không code.
> **Created**: 2026-06-29
> **Vị trí stack** (CTO chốt — Capability là TRỤC GỐC, ngay sau Vision):
> `Vision → **Capability** → Organization → Operating → Cognitive → Knowledge → Learning → Domain → Architecture → Implementation`
> Mọi Model khác đều **phục vụ việc tạo ra Capability**. Đây là đơn vị giá trị khách hàng mua.

---

## 0. Khách hàng mua gì?

```
Framework khác bán:  "AI BIẾT"          (knowledge-centric)
AOIP bán:            "AI LÀM ĐƯỢC"      (capability-centric)  ← Operational Capability
```

Knowledge chỉ là một thành phần. Thứ khách trả tiền là **năng lực vận hành thực tế** trên hệ thống của họ.

---

## 1. Capability là TÍCH, không phải tổng (CTO chốt)

```
Capability = Knowledge × Reasoning × Execution × Coordination × Governance × Learning
             ────────────────────────────────────────────────────────────────────────
             Một chiều = 0  →  Capability = 0.
```

**Tại sao tích, không phải cộng**:
```
Knowledge=100, Reasoning=100, Execution=0   → cộng=200 (ảo)   → tích=0 (thật)
   "AI biết cách rollback nhưng execute disabled → năng lực thực = 0"

Execution=100, Governance=0                 → tích=0
   "Thực thi mạnh nhưng không kiểm soát → framework chết"
```

→ Triết lý: **chiều yếu nhất quyết định năng lực thật.** Không có "bù trừ" — biết nhiều không cứu được không-làm-được. Đây cũng là cách đo trung thực, chống ảo tưởng "AI thông minh".

---

## 2. Sáu chiều — định nghĩa & tầng sở hữu

| Chiều | Câu hỏi | Tầng sở hữu (đã/đang thiết kế) |
|---|---|---|
| **Knowledge** | Biết hệ thống không? | Knowledge Model |
| **Reasoning** | Suy luận đúng không? | Cognitive Model |
| **Execution** | Làm được & làm đúng không? | Execution Model (⬜ chưa) |
| **Coordination** | Phối hợp nhiều agent không? | Organization Model |
| **Governance** | Có trong giới hạn không? | Organization (ai-được-đổi-gì) + Architecture |
| **Learning** | Giỏi lên theo thời gian không? | Learning Model (⬜ chưa) |

→ Capability Model KHÔNG tự định nghĩa nội dung 6 chiều (các Model làm việc đó). Nó định nghĩa **cách 6 chiều hợp thành giá trị** và **cách đo**.

---

## 3. Capability đo theo SCOPE, không phải toàn cục

Năng lực không phải một con số cho cả hệ thống — nó **per-scope** (per service/host/discipline):

```
Capability(Payment service) = K(payment) × R × E(payment) × C × G × L
   → có thể AUTONOMOUS với Payment (đã hiểu 5 năm) nhưng STATIC_GUARD với service mới toanh.
```

- Nối thẳng "Senior 5 năm tại đây": cùng một agent, Capability cao ở scope quen, thấp ở scope lạ.
- Mỗi mission hành động trong một scope → đọc Capability(scope) để quyết được-phép-tới-đâu.

---

## 3b. Capability là FIRST-CLASS OBJECT (CTO ontology #1)

Capability không chỉ là công thức — nó là **đối tượng** có thể lưu/truy/đo/verify:

```
Capability {
  id            : ổn định, addressable
  scope         : ScopeRef          # Payment service / host-x / discipline:SRE
  discipline    : str               # SRE (sau: FinOps/SecOps...) — discipline-pluggable
  dimensions    : { K, R, E, C, G, L → score[0,1] }   # 6 chiều, mỗi chiều đo riêng
  capability    : float             # = Π(dimensions) — dẫn xuất, cache
  confidence    : float             # độ tin của chính phép đo này (meta)
  maturity      : enum              # NASCENT | DEVELOPING | PROVEN | DEGRADED
  owner         : AgentRef          # agent/role chịu trách nhiệm chiều này ở scope này
  last_verified : timestamp         # Capability cũng decay (nối Temporal Model)
  evidence      : [ref]             # vì sao tin có năng lực này (missions, verify, outcomes)
}
```

**Hệ quả**:
- `Capability(Payment)` = một object cụ thể, query được, hiển thị 6 chiều + chiều yếu nhất.
- **maturity ≠ capability score**: một capability có thể score cao nhưng maturity=NASCENT (mới, ít evidence) → vẫn dè dặt. `maturity = f(evidence count, age, track-record)`.
- **confidence (meta)**: "tôi tin tôi đo đúng năng lực này tới đâu" — khác `dimensions` (năng lực thật). Đo sai cũng nguy hiểm như yếu thật.
- Capability **last_verified + decay**: năng lực không verify lại sẽ rớt maturity → sinh mission re-verify (giống Knowledge fact).
- `evidence` làm Capability **explainable** (nối Culture: Prefer Explainability) — luôn truy được "vì sao tin agent làm được X".

3-gate: G1 ✅ (công thức không lưu/truy/verify được; thiếu maturity/evidence), G2 ✅, G3 ✅ (object độc lập, là single-source về "năng lực gì ở scope nào").

---

## 4. Capability ↔ Autonomy (khép với Organization Model)

```
Capability(scope) cho biết agent CÓ THỂ làm gì.
Autonomy cho biết agent ĐƯỢC PHÉP làm gì = min(tenant_tier, Authority, role.posture_ceiling).
Hành động xảy ra ⇔ CÓ THỂ (Capability đủ) ∧ ĐƯỢC PHÉP (Authority đủ).
```

→ Capability (có thể) và Authority (được phép) là **hai cổng AND độc lập** — đúng tinh thần "Skill ≠ Authority" CTO nêu (chi tiết ở Organization Model §2).

---

## 5. Hệ quả kiến trúc

1. **Mọi tài liệu quy về Capability**: mỗi Model trả lời "tôi đóng góp chiều nào, làm chiều đó mạnh lên ra sao".
2. **Đo sản phẩm bằng Capability/scope** (không phải "độ thông minh"): dashboard nên hiển thị 6 chiều × scope, chiều yếu nhất nổi bật.
3. **Roadmap ưu tiên chiều = 0**: hiện Execution≈10%, Learning=0% → đó là nơi Capability thực đang bị kéo về 0, phải làm trước (khớp đề xuất Execution Model kế tiếp).
4. **Discipline-pluggable**: SRE = bó Capability đầu tiên; thêm FinOps/SecOps = thêm bó Capability mới trên cùng 6 chiều.

---

## 6. Điểm cần CTO chốt

1. **Trọng số chiều**: tích thuần (mọi chiều bình đẳng) hay có exponent/weight theo rủi ro (vd Governance, Execution trọng hơn)? (đề xuất: tích thuần ở v1, đo rồi tinh chỉnh).
2. **Chuẩn hóa thang đo** mỗi chiều về [0,1] thế nào để tích có nghĩa (tránh một chiều luôn ~0 kéo sập)?
3. **Capability/scope granularity**: scope nhỏ tới đâu (service? endpoint?) trước khi quá tốn để đo.
