# Learning Model — Hành vi trên Tri thức (GĐ3)

> **Status**: DESIGN ONLY — không code.
> **Created**: 2026-06-29
> **Vai trò**: Learning = **phép kiểm tra cuối cùng (completeness test) của toàn ontology**. Nó dùng gần như TẤT CẢ (Capability/Knowledge/SystemModel/Pattern/Experience/Trust/Lifecycle/Semantic/Constitution) nhưng **KHÔNG được phát minh vocabulary mới**. Vượt qua = meta-architecture tự chứng minh đủ biểu đạt.
> Tài liệu này **chủ yếu là thuật toán** — không định nghĩa lại thuật ngữ (đọc Meta/Semantic/Laws).

> Đối xứng với Execution: Execution = verb trên **Action**; Learning = verb trên **Knowledge** (Fact/Pattern/Experience/SystemModel). Cùng kỷ luật `INV_MINIMAL_PRIMITIVES` + `INV_PRIMITIVE_COMPLETENESS` + `INV_LIFECYCLE_BEFORE_ALGORITHM`.

---

## 1. Câu hỏi của Learning (ranh giới — `INV_LAYER_BOUNDARY`)

> *"Khi nào & làm thế nào một tri thức tiến hóa qua các trạng thái lifecycle của nó — một cách AN TOÀN, KHÔNG drift khỏi Constitution?"*

KHÔNG thuộc Learning: "tri thức là gì" (Knowledge), "có đúng không tại thời điểm thu" (Cognitive verify), "được phép publish không" (Governance). Learning chỉ **dịch chuyển lifecycle tri thức**.

---

## 2. Tập EVOLUTION PRIMITIVE — tối thiểu

**Tái dùng từ Execution (discipline-agnostic, không định nghĩa lại):**

| Reused verb | Trong ngữ cảnh Learning |
|---|---|
| **Observe** | thu outcome về tri thức (advisory đúng/sai, pattern có lặp lại) |
| **Verify** | đối chiếu tri thức vs thực tế mới (Pattern còn giữ không) |

→ Primitive Reuse Ratio: 2/7 verb Learning là verb Execution có sẵn.

**5 verb MỚI (mỗi cái dịch một transition lifecycle Knowledge — Appendix A):**

| Primitive | Consume | Produce / Lifecycle dịch | Obey |
|---|---|---|---|
| **Promote** | Experience(anonymized, ≥N tenant) | Pattern `candidate→published`; Experience→`promoted` | PROMOTION_GATED, DATA_RESIDENCY, HUMAN_ACCOUNTABILITY |
| **Demote** | Pattern + counter-evidence/decay | Pattern `published→degrading→deprecated` | TRUST_PROPAGATION, KNOWLEDGE_TEMPORAL, NEVER_HIDE_UNCERTAINTY |
| **Forget** | Fact/Pattern hết giá trị | `→archived→forgotten` (archive≠delete) | AUDIT_EVERYTHING, EXPLAINABILITY (giữ lịch sử niềm tin) |
| **Merge** | ≥2 Pattern cùng cơ chế | 1 Pattern `published` | SINGLE_SOURCE_OF_TRUTH, NO_DUPLICATED_KNOWLEDGE |
| **Split** | 1 Pattern quá tổng quát | ≥2 Pattern `candidate` (scoped) | TRUST_PROPAGATION |

**Primitive Completeness check (5 verb mới)**: mỗi verb (1) không composable từ verb khác — Promote≠Merge≠Split (transition khác nhau); (2) tái dùng nhiều composition; (3) map 1 lifecycle transition (Appendix A Pattern/Fact/Experience); (4) discipline-agnostic; (5) pre/post độc lập. → cả 5 pass.

---

## 3. Composition Library (hành vi Learning "tên kêu" = composition, KHÔNG primitive)

Tuân `INV_PATTERN_COMPLETENESS` (expand hết về primitive, không state riêng, không lifecycle mới):

```
Reflection            = Observe → Verify → (đạt) Promote | (không) Demote
Consolidation         = Merge        (gộp pattern trùng)
Generalization        = Split → … → Promote   (tách scoped rồi nâng cái đúng)
Knowledge GC          = Forget       (theo điều kiện Q7 Knowledge)
Catastrophic-Forget-Guard = Verify(trước Forget) → (còn giá trị) Demote thay vì Forget
Curriculum-Update     = Reflection → cập nhật prior của Reasoning Playbook (field, không noun)
Pattern-Evolution     = Demote(cũ) → Promote(mới)   (thay vì xóa-tạo)
```

→ Mọi "Adaptive/Reflective/Meta Learning" CẤM thành noun/primitive (`INV_NO_NEW_NOUNS` + `INV_MINIMAL_PRIMITIVES`) — đều là composition trên 7 verb.

---

## 4. Trả lời các bài toán khó của Learning (bằng composition + law, KHÔNG vocabulary mới)

| Bài toán | Giải |
|---|---|
| Khi nào promote Experience→Pattern? | `Promote` gated bởi `INV_PROMOTION_GATED` (≥N tenant + anonymize + generalize + validate + human) |
| Khi nào degrade Pattern? | `Demote` khi `INV_KNOWLEDGE_TEMPORAL` (quá TTL) hoặc counter-evidence theo `INV_TRUST_PROPAGATION` |
| Khi nào quên? | `Forget` chỉ khi Q7: superseded + conf~0 + vô giá trị + qua retention. Trước đó `Catastrophic-Forget-Guard` |
| Split / Merge? | `Split` khi Pattern quá tổng quát (sai ở sub-scope); `Merge` khi trùng cơ chế |
| Rollback tri thức? | Pattern `Demote→deprecated`; Fact dùng `supersedes` (Semantic §4) — giữ version, không xóa |
| Tránh catastrophic forgetting? | `Verify` trước `Forget`; ưu tiên `Demote`/`archived` hơn `forgotten`; archive≠delete |
| Evolution KHÔNG drift khỏi Constitution? | Pattern học được KHÔNG override được law `[C]/[A]`; chỉ Behavioral law override per-discipline (`§0a`). Mọi Promote → Governance + CRAT |

---

## 5. CLOSURE TEST — Learning là completeness test của ontology

```
∀ knowledge evolution  →  biểu diễn bằng {Observe, Verify, Promote, Demote, Forget, Merge, Split} ?
   CÓ  → tập evolution-primitive CLOSED; ontology ĐỦ biểu đạt (không cần noun/object mới).
   KHÔNG → phản ví dụ; mới xét amendment.
```

**Bằng chứng đủ (reuse, không phát minh)**:
- Object: 0 mới — chỉ thao tác Experience/Pattern/Fact/SystemModel đã có.
- Lifecycle: 0 mới — chỉ thêm khai báo Pattern/Experience vào Appendix A (lifecycle của object đã có, đúng `INV_LIFECYCLE_BEFORE_ALGORITHM`).
- Verb: tái dùng 2 (Observe/Verify) + 5 mới có cơ sở lifecycle.
- Law: tái dùng toàn bộ, 0 law mới.

→ Nếu closure giữ, **GĐ1+GĐ2 được chứng minh đủ**: framework mở rộng được Learning mà ontology gần như không tăng complexity (ΔCapability cao / ΔComplexity ~0).

---

## 6. Trạng thái & 3 Maturity Ratios cho tài liệu này

| Ratio | Learning Model đạt |
|---|---|
| Object Reuse | 100% (0 object mới) |
| Primitive Reuse | 2/7 verb từ Execution; 5 verb mới đều map lifecycle có sẵn |
| Law Coverage | 100% (chỉ tham chiếu law/semantic/lifecycle có sẵn) |

- Còn lại (chi tiết, không vocabulary): thuật toán pre/post từng evolution verb; lịch re-verify; ngưỡng decay cụ thể — đều field/algorithm.
- Kế tiếp: **Architecture** (`implements FRAMEWORK_LAWS`; storage/topology/runtime cho graph + lifecycle + CRAT), rồi quay lại chi tiết hóa Execution/Learning.

> Learning vượt closure test = toàn bộ meta-architecture (Ontology + Constitution) tự chứng minh tính đầy đủ. Đây là cột mốc lớn nhất của GĐ3.
