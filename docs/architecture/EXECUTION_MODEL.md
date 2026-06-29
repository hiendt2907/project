# Execution Model — Tập động từ tối thiểu (GĐ3)

> **Status**: DESIGN ONLY — không code. **Mới mở (GĐ3 — Behavior phase).**
> **Created**: 2026-06-29
> **Phạm vi đã KHÓA** (`INV_LAYER_BOUNDARY`): Execution chỉ trả lời **một câu** —
> *"Cho một Decision đã hợp lệ, làm thế nào biến thành Action AN TOÀN & PHỤC HỒI được?"*
> Execution **đọc** `META_MODEL` (object) + `SEMANTIC_RULES` (relation + Appendix A lifecycle) + `FRAMEWORK_LAWS` (law). KHÔNG định nghĩa object/lifecycle/relation/noun.
> **Tài liệu này chỉ KHÓA ĐỘNG TỪ** (verbs). Chưa phát minh hành vi nào. Mọi hành vi phức tạp = composition (`INV_MINIMAL_PRIMITIVES`).

> GĐ1 khóa danh từ (vocabulary) · GĐ2 khóa ngữ pháp (grammar) · **GĐ3 khóa động từ (verbs)**.

---

## 1. Tám PRIMITIVE (minimal behavioral set)

Mỗi primitive khai báo **Algorithm Budget contract** (`FRAMEWORK_LAWS §0c`): consume · produce · lifecycle · laws.

| Primitive | Consume | Produce / Lifecycle dịch | Obey |
|---|---|---|---|
| **Validate** | Decision, CapabilityState, AuthorityState, SemanticRules | Action `planned→validated` | DECISION_ACTION_SEP, EFFECTIVE_AUTONOMY, LAYER_BOUNDARY |
| **Plan** | Decision, SystemModel | Action(`planned`) + execution plan (field) | RECOVERABLE_ACTION, SMALL_BLAST_RADIUS |
| **Execute** | Action(`approved`) | Action `executing→completed\|failed`; Observation | FAIL_CLOSED, AUDIT_EVERYTHING, READ_BEFORE_MUTATE, NAMESPACE_ISOLATION |
| **Observe** | Action, scope | Observation (effect thực tế) | VERIFY_BEFORE_BELIEVE |
| **Verify** | Observation, SystemModel (predicted) | Finding (đạt/không); Action xác nhận | FALSIFICATION_FIRST, EXPLAINABILITY |
| **Recover** | Action(`failed`) | Rollback → Action `rolling_back→rolled_back` | RECOVERABLE_ACTION, AUDIT_EVERYTHING |
| **Escalate** | Action/Decision bế tắc | nâng Authority/HITL (AuthorityState/Responsibility transition) | HUMAN_ACCOUNTABILITY |
| **Abort** | Action bất kỳ non-terminal | dừng an toàn → terminal state | FAIL_CLOSED, SMALL_BLAST_RADIUS |

> **Approve KHÔNG phải primitive Execution** — là cổng Governance/HITL (`INV_DEFINITION_VIA_GOVERNANCE`, autonomy gate). Execute chỉ nhận Action đã `approved`.

3-gate cho từng primitive: mỗi cái dịch một transition lifecycle riêng KHÔNG primitive khác làm được → đều qua `INV_MINIMAL_PRIMITIVES`.

---

## 1a. Primitive Completeness — 8 verb đối chiếu 5 tiêu chí (`INV_PRIMITIVE_COMPLETENESS`)

Mỗi verb phải thỏa **đồng thời cả 5**. Verb thứ 9 muốn thêm phải chứng minh đủ 5.

| Verb | 1. Không composable | 2. Tái dùng nhiều composition | 3. Map 1 lifecycle transition | 4. Discipline-agnostic | 5. Pre/post độc lập |
|---|---|---|---|---|---|
| Validate | ✓ | ✓ (DryRun/SafeExecute/Canary) | Action planned→validated | ✓ | pre: Decision enacted / post: validated |
| Plan | ✓ | ✓ (mọi composition) | Decision→Action(planned) | ✓ | pre: Decision / post: plan |
| Execute | ✓ | ✓ (Retry/Canary/...) | executing→completed\|failed | ✓ | pre: approved / post: terminal effect |
| Observe | ✓ | ✓ (DryRun/Verify loop) | →Observation | ✓ | pre: Action / post: Observation |
| Verify | ✓ | ✓ (Re-plan/Canary) | →Finding | ✓ | pre: Observation+SystemModel / post: Finding |
| Recover | ✓ | ✓ (Compensation) | failed→rolled_back | ✓ | pre: failed / post: rolled_back |
| Escalate | ✓ | ✓ (bế tắc bất kỳ) | Authority/Responsibility transition | ✓ | pre: vượt quyền / post: escalated |
| Abort | ✓ | ✓ (mọi nhánh fail-safe) | non-terminal→terminal an toàn | ✓ | pre: non-terminal / post: terminal |

→ Cả 8 pass. Không verb nào là composition của verb khác (tiêu chí 1), mỗi verb dịch một transition riêng (tiêu chí 3). **Bộ 8 là complete & minimal** cho câu hỏi Execution.

---

## 1b. CLOSURE TEST — phép thử thực chiến (quan trọng hơn "đủ verb chưa")

Câu hỏi đúng KHÔNG phải "có đủ verb chưa" mà **"8 verb có ĐÓNG (closed) dưới composition không"**:

```
∀ workflow  →  expand thành  PRIMITIVE {8 verb}  +  OPERATORS {Sequence, Choice, Loop, Parallel, Interrupt} ?
   CÓ  → CLOSED. Không cần verb thứ 9 / toán tử thứ 6.
   KHÔNG → phản ví dụ:
            • thiếu verb  → xét INV_PRIMITIVE_COMPLETENESS
            • thiếu cách ghép → xét INV_BEHAVIOR_ALGEBRA (toán tử)
```

- Closure mạnh = **instruction set (verb) + control-flow algebra (operator)** đều đóng — giống compiler.
- Phép tự kiểm chứng: ai viết `SmartExecute` → yêu cầu expand. Expand được (vd `Choice(Retry | Recover | Escalate)`) → composition; không expand được → primitive/operator thiếu.
- Mọi pattern §2/§2a + Learning library đều expand được (Appendix B) → chưa có phản ví dụ → **8 verb + 5 toán tử closed**.

---

## 2. Composition — mọi hành vi "tên kêu" đều ở đây (KHÔNG primitive)

Self-check (`§0c`): "biểu diễn được bằng composition?" → là composition.

```
DryRun           = Plan → Execute(scope=sandbox) → Observe → Verify   (sandbox = field scope, không verb mới)
Retry            = Execute* (lặp trên failed, backoff)
SafeExecute      = Validate → Plan → Execute → Observe → Verify → (Recover khi fail)
Canary/Progressive/BlueGreen = Plan(scope nhỏ) → Execute → Observe → Verify → mở rộng scope → lặp
Compensation     = Recover
Re-plan          = Verify(không đạt) → Plan (vòng lại)
```

→ Tất cả CẤM thành first-class (`INV_MINIMAL_PRIMITIVES`). Khác biệt chỉ là **scope/field/thứ tự composition**, không phải động từ mới.

### 2a. Composition Library — bước trưởng thành (Primitive → Composition Pattern → Algorithm)

Mô hình phát triển ĐÚNG (giống ISA: CPU không có `SORT`, chỉ `LOAD/COMPARE/JUMP/STORE`; SORT là composition):

```
Primitive (8 verb)  →  Composition Pattern (recipe có tên, reuse)  →  Algorithm (instance)
```

**Composition Pattern** = công thức ghép verb có tên, để TÁI DÙNG. KHÔNG phải object/noun/primitive — không vào ontology, không vào taxonomy. Chỉ là **syntax sugar / thư viện**; luôn expand về 8 verb.

| Pattern | Expand thành |
|---|---|
| `Retry` | `Execute → Observe → Verify → (fail) Execute*` (backoff) |
| `DryRun` | `Plan → Execute(scope=sandbox) → Observe → Verify` |
| `Canary` | `Plan(scope nhỏ) → Execute → Observe → Verify → (đạt) mở rộng scope → lặp` |
| `BlueGreen` | `Plan(scope=green) → Execute → Verify → (đạt) switch scope` |
| `Rolling` | `Canary lặp theo từng đơn vị scope` |
| `Compensation` | `Recover` |
| `Saga` | chuỗi `Execute` + `Recover` bù trừ theo thứ tự nghịch |

- Không phá `INV_MINIMAL_PRIMITIVES` vì **chỉ reuse**, không thêm verb.
- Pattern có thể thêm thoải mái (không phải Algorithm Budget verb) — nhưng mỗi pattern PHẢI expand được về 8 verb; không expand được → đó là dấu hiệu cần primitive (qua `INV_PRIMITIVE_COMPLETENESS`).
- Algorithm = một pattern được instantiate với scope/timeout/field cụ thể cho một Decision.

---

## 3. Khung composition chuẩn (vòng đời một Action)

```
Decision(enacted) → Validate → Plan → [Approve: Governance] → Execute → Observe → Verify
                        │                                          │          │
                        └─ fail → Abort                            └─ fail → Recover
                                                            Verify không đạt → Re-plan (→ Plan)
                                                            vượt quyền/bế tắc → Escalate
```

- Đây là **composition**, không phải primitive thứ 9 — chỉ là đồ thị nối 8 verb theo lifecycle Appendix A.
- Mọi nhánh tuân transition hợp lệ; transition CẤM (vd `completed→executing`) bị chặn bởi `INV_LIFECYCLE_BEFORE_ALGORITHM`.

---

## 4. Ranh giới — Execution KHÔNG làm gì

| Câu hỏi | Tầng (KHÔNG phải Execution) |
|---|---|
| Có nên làm? Risk/đánh đổi? | Operating / Cognitive / Mission Economy |
| Có quyền làm? | Organization (Authority) |
| Đủ năng lực? | Capability |
| Biết hệ thống chưa? | Knowledge |
| Blast radius lớn cỡ nào (đánh giá)? | Cognitive/Culture (Execute chỉ *giới hạn* khi thực thi) |

Execution nhận Decision **đã** hợp lệ (đã qua mọi cổng trên) → chỉ lo thực thi an toàn & phục hồi.

---

## 5. Trạng thái

- Đã khóa: 8 primitive verb + composition rule + Algorithm Budget.
- Chưa làm (chờ CTO): chi tiết thuật toán từng primitive (precondition/postcondition cụ thể, timeout/interrupt/cancellation semantics — đều phải là field/algorithm trên 8 verb, KHÔNG verb mới).
- Kế tiếp sau Execution: **Learning Model** → **Architecture** (`implements FRAMEWORK_LAWS`).

> Kỷ luật GĐ3: chỉ thêm **algorithm/field**; mọi "khả năng mới" phải chứng minh không là composition trước khi thành primitive.
