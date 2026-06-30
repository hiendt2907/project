# Framework Laws — Hiến pháp của AOIP

> **Status**: DESIGN ONLY — không code.
> **Created**: 2026-06-29
> **Bản chất**: KHÔNG phải tầng thứ 11. Đây là **Constitution đứng TRÊN toàn stack** — luật mọi Model phải obey, không Model nào sở hữu.
> **Mục đích**: gom các invariant đang rải rác → ID chuẩn. Từ nay tài liệu chỉ ghi `obey INV_XXX`, không giải thích lại.

```
FRAMEWORK
├── Vision
├── LAWS  ◄── (tài liệu này — đứng trên, ràng buộc tất cả)
├── Capability
├── Organization
├── Operating · Cognitive · Knowledge · Learning
├── Domain
├── Architecture
└── Implementation
        └── Meta-Model (luật biên cho object — ngang trục)
```

---

## 0. META-LAW (luật về luật)

| ID | Luật | Ý nghĩa |
|---|---|---|
| **INV_NO_NEW_NOUNS** | Từ nay **mọi khái niệm mới mặc định BỊ TỪ CHỐI**, trừ khi chứng minh KHÔNG thể biểu diễn bằng object/relation/field/invariant đã có. | Giữ ontology ổn định. Chỉ được thêm: field, relation, invariant, lifecycle, algorithm cho object đã tồn tại. |
| **INV_3GATE** | Mọi bổ sung phải qua: (G1) giải vấn đề tầng hiện tại chưa giải? (G2) I/O rõ? (G3) độc lập hay chỉ là view? | 3-gate cấp framework. |
| **INV_OBJECT_CLASSIFIED** | Mọi object phải thuộc đúng 1 nhóm: Definition / Runtime / Knowledge / Derived. | `META_MODEL.md §1`. |
| **INV_LIFECYCLE_BEFORE_ALGORITHM** | **No Lifecycle ⇒ No Algorithm.** Mọi Runtime object PHẢI có lifecycle khai báo tường minh (legal states + transitions hợp lệ + terminal) TRƯỚC khi tham gia bất kỳ thuật toán nào. | `SEMANTIC_RULES.md Appendix A`. [C] |
| **INV_MINIMAL_PRIMITIVES** | **Verb-level NO_NEW_NOUNS.** Một hành vi mới chỉ được là first-class primitive nếu KHÔNG biểu diễn được bằng composition của primitive đã có. Biểu diễn được → là composition, KHÔNG phải primitive mới. | `EXECUTION_MODEL.md`. [C] |
| **INV_PRIMITIVE_COMPLETENESS** | Một primitive chỉ xứng đáng nếu thỏa **ĐỒNG THỜI cả 5**: (1) không biểu diễn được bằng composition; (2) tái dùng ở nhiều composition khác nhau; (3) map rõ tới một lifecycle transition; (4) không phụ thuộc discipline; (5) có pre/post-condition độc lập. Thêm verb thứ 9 phải chứng minh đủ cả 5. | `EXECUTION_MODEL.md §1a`. [C] |
| **INV_PATTERN_COMPLETENESS** | Một Composition Pattern (macro) chỉ tồn tại nếu thỏa **cả 5**: (1) lặp lại ở nhiều algorithm; (2) expand HOÀN TOÀN về primitive; (3) KHÔNG mang state riêng; (4) KHÔNG tạo lifecycle mới; (5) KHÔNG đổi semantic của primitive. Giữ Pattern là "macro", không phải "primitive trá hình". | `EXECUTION_MODEL.md §2a`. [C] |
| **INV_BEHAVIOR_ALGEBRA** | Mọi algorithm = primitive kết hợp **CHỈ** qua 5 toán tử {Sequence, Choice, Loop, Parallel, Interrupt}. Composition hợp lệ ⇔ well-formed theo lifecycle. Thêm toán tử thứ 6 = amendment, phải chứng minh control-flow hiện tại không biểu đạt được. | `SEMANTIC_RULES.md Appendix B`. [A] |

> ✔ Hợp lệ: `CapabilityState += verification_cost`, `Authority += expiry`, `SystemModel += causal_strength`.
> ✘ Không hợp lệ: "Adaptive Capability Object", "Execution Context Object", "Operational DNA Object" — dấu hiệu ontology trôi.

---

## 0a. LAW STRENGTH — phân cấp độ cứng (CTO)

Không phải mọi INV_* ngang nhau. Mỗi luật mang **strength** quyết định: ai được đổi, đổi bằng cách nào, có override theo discipline được không.

| Strength | Phá được khi | Override per-discipline | Ví dụ |
|---|---|---|---|
| **CONSTITUTIONAL** | chỉ khi đổi Vision | ✗ KHÔNG | `INV_NO_NEW_NOUNS`, `INV_LIFECYCLE_BEFORE_ALGORITHM`, `INV_CAPABILITY_IS_PRODUCT`, `INV_LAYER_BOUNDARY`, `INV_HUMAN_ACCOUNTABILITY`, `INV_FAIL_CLOSED`, `INV_DATA_RESIDENCY` |
| **ARCHITECTURAL** | qua architecture amendment (RFC) | ✗ (chỉ amendment) | `INV_SINGLE_SOURCE_OF_TRUTH`, `INV_DERIVED_NEVER_PERSIST`, `INV_DECISION_ACTION_SEPARATION`, `INV_AUDIT_EVERYTHING`, `INV_PROMOTION_GATED` |
| **BEHAVIORAL** | tinh chỉnh theo domain/discipline | ✓ CÓ (override có kiểm soát) | `INV_SMALL_BLAST_RADIUS`, `INV_FALSIFICATION_FIRST`, `INV_LLM_NOT_FIRST`, `INV_INFER_BEFORE_ASK`, `INV_CULTURE_NOT_POLICY` |

> **Mỗi mục luật ở §1–§6 dưới đây ngầm mang strength** (residency/accountability/fail-closed = Constitutional; one-source/derived/decision-action = Architectural; culture/blast-radius/falsification = Behavioral). Khi cần, ghi `[C]/[A]/[B]` cạnh ID.

**Discipline override (chỉ BEHAVIORAL)**:
```
FinOps  obey  INV_CAPABILITY_IS_PRODUCT        (Constitutional — bắt buộc)
        override INV_SMALL_BLAST_RADIUS → INV_COST_FIRST   (Behavioral — hợp lệ, trong phạm vi discipline)
```
- Override = thêm field/relation cho luật đã có (KHÔNG noun mới) + khai báo discipline-scoped.
- Constitutional/Architectural KHÔNG override được — chỉ amendment toàn cục.
- Không phân cấp → mọi luật thành "bất khả xâm phạm" → framework không tiến hóa được.

---

## 0b. OBJECT BUDGET — complexity budget chống ontology inflation (CTO)

`INV_NO_NEW_NOUNS` cấm tùy tiện; Object Budget **đo** sự tuân thủ.

```
Mỗi milestone/quý, thống kê count theo nhóm (Meta §1):
   Definition · Runtime · Knowledge · Derived

Tín hiệu CẢNH BÁO (ontology inflation):
   số object TĂNG liên tục mà Capability KHÔNG tăng tương ứng.
```

| Snapshot | Definition | Runtime | Knowledge | Derived |
|---|---|---|---|---|
| 2026 Q2 (baseline) | ~7 | ~8 | ~5 | ~5 |
| Q sau | nếu nhảy vọt (vd 31) → **phải có lý do rất mạnh + ghi nhận** | | | |

- Giống complexity budget của compiler/kernel: **không cấm mở rộng, buộc trả "chi phí" rõ ràng**.
- Mỗi object mới phải kèm: thuộc nhóm nào, tăng chiều Capability nào, qua `INV_3GATE` + `INV_NO_NEW_NOUNS`.
- Review kiến trúc định kỳ đối chiếu Object count ↔ Capability gain. Lệch → dừng mở rộng, consolidate.

---

## 0c. ALGORITHM BUDGET — complexity budget cho HÀNH VI (CTO)

Object Budget (§0b) đo *ontology*; Algorithm Budget đo *hành vi* (GĐ3). Mỗi algorithm mới phải khai báo & qua kiểm:

```
Mỗi algorithm mới TRẢ LỜI:
   1. consume Runtime object nào?
   2. produce Runtime object nào?
   3. obey Lifecycle nào? (transition nào nó dịch)
   4. obey Law nào?
   5. TÁI SỬ DỤNG primitive cũ — hay đang COPY logic?

Self-check (verb-level NO_NEW_NOUNS):
   "Xóa algorithm này, có biểu diễn được bằng composition của primitive đã có không?"
      Có  → KHÔNG phải first-class; là composition.
      Không → mới đáng là primitive.
```

- Execution giữ **cực nghèo**: ~5–8 primitive. Mọi hành vi khác = composition.
- ✘ CẤM noun inflation cấp thuật toán: `SafeExecute`, `ProgressiveExecute`, `BlueGreenExecute`, `CanaryExecute`, `EmergencyExecute`... — đều là composition, KHÔNG primitive.
- Review GĐ3 đối chiếu: số algorithm ↔ behavior gain. Copy logic = nợ, phải refactor về composition.

---

## 0d. MATURITY RATIOS — đo "nén complexity" (CTO)

Đơn vị đo trưởng thành đã ĐỔI: không còn "thêm bao nhiêu concept" mà **"behavior mới tái dùng bao nhiêu phần framework"**. Theo dõi 3 tỷ lệ (tăng = nén complexity; giảm = tích lũy complexity):

| Tỷ lệ | Đo | Dấu hiệu tốt |
|---|---|---|
| **Object Reuse Ratio** | thiết kế mới thêm field/relation thay vì object | → 1.0 (gần như không object mới) |
| **Primitive Reuse Ratio** | hành vi mới = composition của 8 verb | → 1.0 (không verb mới) |
| **Law Coverage Ratio** | thuật toán mới tham chiếu law/semantic/lifecycle có sẵn thay vì diễn giải lại | → 1.0 (không định nghĩa lại thuật ngữ) |

**Cặp đối xứng quản trị** (Object Budget §0b ↔ Algorithm Budget §0c):
```
Ontology  → Object Budget    → chống Object Inflation (noun)
Behavior  → Algorithm Budget → chống Verb Inflation  (SafeExecute/SmartExecute/... = chết sau 1 năm)
```
→ Mục tiêu framework: **hành vi phong phú dần, ontology gần như không tăng complexity.**

**Đại lượng bao trùm — COMPLEXITY COMPRESSION**: 3 ratio trên là 3 chỉ số của một đại lượng duy nhất.
```
THẮNG  =  Capability ↑  trong khi  Complexity ~giữ nguyên (hoặc tăng rất ít)
THUA   =  phải thêm nhiều object/primitive/law chỉ để tăng một chút Capability
```
→ Mọi đề xuất tương lai đo bằng: **ΔCapability / ΔComplexity**. Tỷ lệ thấp = đi sai hướng.

---

## 1. Luật về TRI THỨC (Knowledge / Meta-Model)

| ID | Luật | Bind |
|---|---|---|
| **INV_SINGLE_SOURCE_OF_TRUTH** | Mỗi sự thật tồn tại đúng một nơi; mọi thứ khác tham chiếu. | Knowledge §8, Twin |
| **INV_NO_DUPLICATED_KNOWLEDGE** | Không sao chép tri thức; dùng quan hệ/tham chiếu. | Knowledge |
| **INV_DERIVED_NEVER_PERSIST** | Object nhóm Derived (Understanding/Confidence/Trust/CapabilityScore/Level) KHÔNG được lưu làm truth — luôn recompute. | Meta §1 |
| **INV_VERIFY_BEFORE_BELIEVE** | Nguồn (config/doc) KHÔNG tự thành Fact; phải đối chiếu độc lập. | Cognitive, Knowledge |
| **INV_KNOWLEDGE_TEMPORAL** | Mọi Fact bitemporal (observation/valid/verified/changed); confidence decay theo verified_time. | Knowledge Q4/Cognitive Q6 |
| **INV_TRUST_PROPAGATION** | confidence(kết luận) ≤ min(inputs)×rule; nguồn độc lập → corroboration; gốc bị bác → cascade. | Knowledge §5 |
| **INV_KNOWLEDGE_NOT_ALERT** | signal_type ≠ ANOMALY KHÔNG vào diagnostic pipeline. | *(codebase, đang chạy)* |

---

## 2. Luật về RESIDENCY (data khách hàng)

| ID | Luật | Bind |
|---|---|---|
| **INV_DATA_RESIDENCY** | Value nhạy cảm (IP/config/secret/PII) CHỈ ở Tenant KB phía khách. Omni giữ Skeleton ẩn danh + Experience/Pattern/Policy. | Knowledge Q2/Q5 *(codebase)* |
| **INV_DOC_RESIDENCY** | Tài liệu khách lưu per-tenant, không share cross-tenant. | *(codebase)* |
| **INV_PROMOTION_GATED** | Tri thức tenant → Experience global chỉ khi: recurrence ≥N tenant + anonymize hoàn toàn + generalize + validate + human-approve. | Knowledge Q6 |

---

## 3. Luật về NĂNG LỰC & TỰ CHỦ (Capability / Organization)

| ID | Luật | Bind |
|---|---|---|
| **INV_CAPABILITY_IS_PRODUCT** | Capability = Π(6 chiều); một chiều=0 → Capability=0. Không bù trừ. | Capability §1 |
| **INV_SKILL_UNDERSTANDING_AUTHORITY** | Ba trục độc lập, KHÔNG gộp. "Giỏi" ≠ "hiểu tenant" ≠ "được phép". | Organization §2 |
| **INV_EFFECTIVE_AUTONOMY** | real_autonomy(scope) = min(tenant_tier, Authority.trust, role.posture_ceiling) ∧ scope ∈ responsibility ∧ Capability(scope) đủ. | Organization §2, Capability §4 *(mở rộng từ codebase)* |
| **INV_AUTHORITY_NOT_RBAC** | Authority = Trust + Responsibility + Accountability, không chỉ permission. | Organization §2 |
| **INV_HUMAN_ACCOUNTABILITY** | Accountability cuối cùng luôn quy về một chủ thể chịu trách nhiệm (có thể ≠ actor); automation tier-up & mutation cần human. | Organization §6/§8 |
| **INV_AUTHORITY_EARNED_PER_TENANT** | Agent mới ở tenant khởi đầu Authority thấp dù Skill cao, leo theo Understanding×track-record. | Organization §2 |

---

## 4. Luật về HÀNH VI & THỰC THI (Cognitive / Execution / Culture)

| ID | Luật | Bind |
|---|---|---|
| **INV_DECISION_ACTION_SEPARATION** | Decision (có nên) ≠ Action (làm thế nào). Execution chỉ nhận Decision đã hợp lệ. | Meta §3, Execution (⬜) |
| **INV_LLM_NOT_FIRST** | Deterministic/nguồn xác định trước; LLM là bước suy luận cuối, chỉ interpret — không phải nguồn sự thật. | Operating Q4, Cognitive |
| **INV_FALSIFICATION_FIRST** | Loại giả thuyết bằng bác bỏ (predicted_evidence vắng), không chỉ xác nhận. | Cognitive Q2 |
| **INV_EXPLAINABILITY** | Mọi quyết định/hành động truy được nguồn (provenance → evidence). Không "LLM quyết". | Culture, Knowledge §5 |
| **INV_NEVER_HIDE_UNCERTAINTY** | Luôn phơi bày độ bất định; "tôi có thể sai" first-class. | Culture, Cognitive Q5 |
| **INV_INFER_BEFORE_ASK** | **Never ask what can be inferred.** Một khoảng trống tri thức (Unknown) chỉ được chuyển thành câu hỏi cho người SAU KHI đã exhaust thang bằng chứng: (1) suy luận từ Fact/graph đã có → (2) xác minh runtime → (3) tài liệu → (4) host/agent khác. Chỉ Unknown KHÔNG nguồn nào chứng minh mới thành Communication. KPI = tối thiểu hóa số câu hỏi, không phải tối đa hóa. | Cognitive, Knowledge §5; Evidence Completion Engine *(codebase)* |
| **INV_SMALL_BLAST_RADIUS** | Khi nhiều cách hợp lệ, chọn cách rủi ro/blast nhỏ nhất đủ giải quyết. | Culture |
| **INV_RECOVERABLE_ACTION** | Action phải an toàn & phục hồi được (rollback/compensation/idempotency); không đảo ngược → cần ngưỡng cao + human. | Execution (⬜) |

---

## 5. Luật về QUẢN TRỊ & AN TOÀN (Governance / CRAT)

| ID | Luật | Bind |
|---|---|---|
| **INV_FAIL_CLOSED** | Bất định/lỗi/kill-switch → KHÔNG hành động. `write_audit_block()` phải thành công trước mọi emit/dispatch. | *(codebase CRAT, kill-switch)* |
| **INV_DEFINITION_VIA_GOVERNANCE** | Object Definition (Playbook/Policy/Culture/CapabilityDefinition) chỉ đổi qua Governance (ai-được-đổi-cái-gì + RFC). | Organization §8, Meta §1 |
| **INV_AUDIT_EVERYTHING** | Mọi thay đổi tài sản + mọi mutation + mọi Responsibility transition → CRAT (actor + accountable). | CRAT *(codebase)* + Meta §6 |
| **INV_ASSESSMENT_CLOSES_LOOP** | Derived state (Capability/Understanding/Trust/Authority) CHỈ đổi qua Assess từ evidence; Learning/Execution KHÔNG được sửa autonomy mà không qua Assessment. Chống "tự phong quyền". | `ASSESSMENT.md`. [A] |
| **INV_READ_BEFORE_MUTATE** | Đọc ground truth trước khi mutate. | *(codebase)* |
| **INV_NAMESPACE_ISOLATION** | Tôn trọng ranh giới namespace/scope khi hành động. | *(codebase)* |

---

## 6. Luật về RANH GIỚI TẦNG (mỗi tầng một câu hỏi)

| ID | Luật |
|---|---|
| **INV_LAYER_BOUNDARY** | "Có nên"→Operating/Cognitive · "Có quyền"→Authority · "Đủ năng lực"→Capability · "Biết hệ thống"→Knowledge · "Làm thế nào an toàn"→Execution. Không tầng nào lấn câu hỏi tầng khác. |
| **INV_CULTURE_NOT_POLICY** | Culture = prior (sinh quyết định, mềm); Policy = enforce (luật cứng). Không trộn. |

---

## Cách dùng (tiết kiệm tài liệu)

- Model docs từ nay viết: *"Section này obey `INV_DERIVED_NEVER_PERSIST`, `INV_VERIFY_BEFORE_BELIEVE`"* — không lặp lại giải thích.
- Khi đề xuất bất kỳ thứ gì mới → **trước hết chạy `INV_NO_NEW_NOUNS` + `INV_3GATE`**. Không qua → từ chối.
- Implementation/Architecture sau này map từng INV_* → kiểm thử (test/guardrail) tương ứng. "Vi phạm INV = bug" (như codebase đang làm).
- Mỗi PR/amendment kiến trúc khai báo **Affected Laws** + **Law Strength** + **Object Budget delta** thay vì đọc lại 10 document.
- Architecture chỉ cần `Architecture implements FRAMEWORK_LAWS` — không nhắc lại philosophy; chỉ lo deployment/topology/storage/runtime/scaling/observability.

---

## Lộ trình (CTO chốt 3 giai đoạn)

```
GĐ1  Xây ontology              → ĐÃ xong phần lớn
GĐ2  Cố định Framework Laws +  → ĐANG (tài liệu này) — hoàn tất TRƯỚC khi mở rộng
     kiểm soát complexity
GĐ3  Thiết kế hành vi động     → Execution → Learning → Architecture
     (trên ontology đã KHÓA)
```

> Từ thời điểm này: **NO NEW NOUNS**. Các tầng còn lại tập trung vào *hành vi & cơ chế* (field/relation/invariant/lifecycle/algorithm), không mở rộng hệ khái niệm.
