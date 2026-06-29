# Semantic Rules — Hợp đồng ngữ nghĩa giữa các Object

> **Status**: DESIGN ONLY — không code.
> **Created**: 2026-06-29
> **Bản chất**: KHÔNG phải Model mới, KHÔNG layer, KHÔNG object/noun mới (tuân `INV_NO_NEW_NOUNS`).
> Chỉ gom **semantic constraints** (đã rải ~60% ở Knowledge/Capability/Organization/Meta/Laws) vào một nơi — như OCL/DDD invariants. Execution & Architecture đọc đây như **hợp đồng hệ thống**, không tự định nghĩa lại.
> Ngang trục với `META_MODEL.md` (object ontology) — đây là **relation ontology**.

---

## 1. Từ vựng QUAN HỆ (relation, không phải noun)

Chỉ dùng đúng tập động từ này; không thêm:

| Relation | Nghĩa |
|---|---|
| `derives_from` | tính ra từ (Derived/Knowledge) — nguồn đổi thì phải tính lại |
| `references` | trỏ tới (không sở hữu) |
| `consumes` | đọc làm input để tạo ra thứ khác |
| `produces` | sinh ra |
| `owns` | sở hữu vòng đời (xóa chủ → xóa con) |
| `requires` | điều kiện tiên quyết, thiếu → không hợp lệ |
| `implements` | hiện thực hóa 1-1 |
| `invalidates` | làm mất hiệu lực |
| `supersedes` | thay thế phiên bản trước (giữ lịch sử) |

---

## 2. Chuỗi REASONING → ACTION (hợp đồng cốt lõi cho Execution)

```
Observation ─produces→ Hypothesis ─verify→ Finding ─consumed_by→ Decision ─implemented_by→ Action
                                                                     │
                                                              (sai) Rollback ─references→ Action
```

| Object | MUST | MUST NOT |
|---|---|---|
| **Observation** | immutable; `references` scope; `produced_by` plugin/collector | bị sửa sau khi ghi |
| **Hypothesis** | `derives_from` ≥1 Observation **hoặc** SystemModel; mang confidence | thành Fact khi chưa verify (`INV_VERIFY_BEFORE_BELIEVE`) |
| **Finding** | `references` ≥1 Observation/Fact; `owned_by` 1 agent; immutable | tồn tại không nguồn |
| **Decision** | `consumes` ≥1 Finding **và** CapabilityState **và** AuthorityState **và** Understanding(scope); explainable (`references` mọi input) | "LLM quyết" không nguồn (`INV_EXPLAINABILITY`) |
| **Action** | `implements` **đúng 1** Decision; `requires` Authority.trust đủ ∧ scope∈responsibility ∧ Capability(scope) đủ; recoverable (`INV_RECOVERABLE_ACTION`) | tồn tại không Decision (`INV_DECISION_ACTION_SEPARATION`) |
| **Rollback** | `references` **đúng 1** Action trước; bản thân là một Action (compensation) | thực thi không audit (`INV_AUDIT_EVERYTHING`) |

> **Lưu ý NO_NEW_NOUNS**: ví dụ của CTO có `Decision produces Intent`. "Intent" CHƯA tồn tại trong ontology → **không tạo noun mới**. Biểu diễn bằng object đã có: **Decision `produces` Action** (Action chính là "ý định hành động đã hợp lệ"). Nếu sau này cần tách "ý định chưa thực thi", thêm **field** `Action.state ∈ {planned, executing, done, rolled_back}`, KHÔNG thêm object.

---

## 3. Chuỗi DERIVED (luôn recompute — `INV_DERIVED_NEVER_PERSIST`)

```
CapabilityScore   derives_from  CapabilityState.dimensions      ( = Π )
CapabilityState   derives_from  CapabilityDefinition + evidence
Understanding     derives_from  KnowledgeGraph (Twin + CompetencyMatrix coverage)
Trust             derives_from  Skill × Understanding × track_record
Authority         references    Trust + Responsibility + Accountability
Level             derives_from  {Skill, Understanding, Authority}        (chỉ là nhãn hiển thị)
```

| Ràng buộc | |
|---|---|
| Mọi object cột phải `derives_from` → **không lưu làm truth**; nguồn đổi → bắt buộc tính lại | `INV_DERIVED_NEVER_PERSIST` |
| `Authority` ≠ `Skill` ≠ `Understanding` — ba trục độc lập | `INV_SKILL_UNDERSTANDING_AUTHORITY` |
| `CapabilityScore` = Π(6 chiều); một chiều=0 → 0 | `INV_CAPABILITY_IS_PRODUCT` |

---

## 4. Chuỗi KNOWLEDGE (verify + decay + provenance)

```
Hypothesis  ─verify→  Fact   references→ provenance(sources, Trust)
Fact        supersedes  Fact'        (drift/change → giữ lịch sử)
Fact        invalidated_by  contradiction từ nguồn Trust cao hơn (cascade theo provenance)
SystemModel.edge  derives_from  observed(cause→effect, Δt)        (causal, verify+decay)
Experience  derives_from  Episodic (≥1 completed Mission)         (anonymized)
Pattern     derives_from  Experience của ≥N tenant độc lập        (`INV_PROMOTION_GATED`)
```

| MUST | |
|---|---|
| Fact bitemporal (observation/valid/verified/changed); confidence `derives_from` (now − verified_time) | `INV_KNOWLEDGE_TEMPORAL` |
| confidence(kết luận) ≤ min(inputs)×rule; nguồn độc lập → corroboration; gốc bị bác → cascade | `INV_TRUST_PROPAGATION` |
| value nhạy cảm chỉ ở Tenant KB; Pattern/Experience ẩn danh ở Omni | `INV_DATA_RESIDENCY`, `INV_PROMOTION_GATED` |
| mỗi sự thật tồn tại 1 nơi; còn lại `references` | `INV_SINGLE_SOURCE_OF_TRUTH` |

---

## 5. Chuỗi ORGANIZATION (mission, responsibility, communication)

```
Playbook    produces  Mission                          (Mission KHÔNG tự sinh)
Mission     produces  sub-Mission (parent_mission_id)  (mỗi cái có Goal+DoD+Deliverable)
Mission     references Role + Posture + scope
Responsibility  lifecycle: Assign→Accept→Transfer→Complete→Release   (chuyển khi delegate)
Accountability  NOT transfer cùng Responsibility       (có thể giữ ở Commander/human)
Communication.node  references Finding                  (information lineage, graph riêng)
Decision    consumes  Communication (thông tin đã nhận) ; KHÔNG là Communication
```

| MUST | |
|---|---|
| Definition (Playbook/Policy/Culture/Role/Posture) đổi qua Governance | `INV_DEFINITION_VIA_GOVERNANCE` |
| Responsibility transfer + mọi mutation → CRAT (actor + accountable) | `INV_AUDIT_EVERYTHING`, `INV_HUMAN_ACCOUNTABILITY` |
| Communication ≠ projection của Decision Graph (semantics khác) | Organization §5 |

---

## 6. Bảng CARDINALITY (tóm tắt kiểm thử được)

| Quan hệ | Cardinality |
|---|---|
| Decision → Finding | consumes **≥1** |
| Action → Decision | implements **đúng 1** |
| Rollback → Action | references **đúng 1** |
| Hypothesis → Observation/SystemModel | derives_from **≥1** |
| Finding → Observation/Fact | references **≥1** |
| Pattern → tenant | derives_from **≥N** (N≥3) |
| Mission → Playbook | produced_by **đúng 1** |
| sub-Mission → Mission | child_of **đúng 1** (parent_mission_id) |
| CapabilityScore → CapabilityState | derives_from **đúng 1** |

---

## 7. Cách dùng & ranh giới

- **Execution chỉ đọc**: cho một Decision đã thỏa mọi MUST ở §2 → "làm thế nào thực thi an toàn & phục hồi". Không định nghĩa lại object/lifecycle/relation.
- **Architecture map** mỗi constraint → ràng buộc schema/test (vd FK, check, guardrail). "Vi phạm constraint = bug".
- **Mở rộng**: chỉ thêm `relation`/`field`/`cardinality` cho object đã có; thêm noun → chặn bởi `INV_NO_NEW_NOUNS`.
- Tài liệu này + `META_MODEL.md` + `FRAMEWORK_LAWS.md` = **bộ ba hợp đồng** (object / relation / law) mà mọi tầng hành vi obey.

---

## 8. Trạng thái — sẵn sàng Behavior phase

```
ĐÃ KHÓA:  Object ontology (Meta) · Relation ontology (Semantic, này) · Laws (Constitution)
TIẾP:     Execution (chỉ hành vi: Decision hợp lệ → Action an toàn/phục hồi)
          → Learning → Architecture
```

> Từ đây mọi tầng hành vi chỉ được: **đọc ontology + obey laws + thêm algorithm/field**. Không noun, không layer, không object.

---

# Appendix A — Canonical Lifecycles

> KHÔNG phải model/noun/object mới (tuân `INV_NO_NEW_NOUNS`). Chỉ khai báo **vòng đời chuẩn** cho object đã có, để Execution/Learning/Architecture obey thay vì tự phát minh.
> Bắt buộc bởi `INV_LIFECYCLE_BEFORE_ALGORITHM` (`FRAMEWORK_LAWS §0`): **No Lifecycle ⇒ No Algorithm**.
> Mỗi mục: legal states · transition hợp lệ · transition CẤM · terminal states.

## A.1 RUNTIME objects

**Action**
```
planned → validated → approved → executing → completed
   │          │           │           │
   │          │           │           └→ failed → rolling_back → rolled_back
   └──────────┴───────────┴→ aborted        (Abort: bất kỳ non-terminal → aborted)
```
- Terminal: `completed`, `rolled_back`, `aborted`, `failed` (nếu không rollback được).
- CẤM: `completed → executing`; `planned/validated → executing` (chưa approved); `approved` khi chưa `validated`.
- *(Clarification 2026-06-29 — runtime walking-skeleton ép lộ: Abort cần terminal state `aborted` tường minh; bổ sung field/state, KHÔNG noun mới.)*

**Mission**
```
CREATED → PLANNED → ASSIGNED → IN_PROGRESS ⇄ BLOCKED → COMPLETED
                                    │
                                    └→ FAILED | ABANDONED
COMPLETED|FAILED|ABANDONED → ARCHIVED
```
- Terminal: `ARCHIVED`. CẤM: `COMPLETED → IN_PROGRESS`; skip ASSIGNED.

**Decision**
```
drafted → justified → issued → enacted
              │           └→ superseded | void
              └→ void (thiếu input hợp lệ)
```
- Terminal: `enacted`, `superseded`, `void`. CẤM: `issued` khi consumes 0 Finding (`§2`).

**Hypothesis**
```
proposed → testing → confirmed   (→ trở thành Fact)
                  └→ rejected
```
- Terminal: `confirmed`, `rejected`. CẤM: `confirmed` khi chưa `testing` (`INV_VERIFY_BEFORE_BELIEVE`).

**Finding** — `recorded` (immutable) → `superseded`. Terminal: `recorded`, `superseded`. CẤM: sửa nội dung sau `recorded`.

**Observation** — `captured` (immutable, transient) → `expired`. Terminal: `expired`.

**CapabilityState**
```
nascent → developing → proven → degraded → (re-verify) → proven
                                     └→ retired
```
- Terminal: `retired`. `degraded` khi quá `last_verified + TTL` (`INV_KNOWLEDGE_TEMPORAL`).

**AuthorityState** — `granted → elevated → suspended → revoked`. Terminal: `revoked`. CẤM: `revoked → elevated` (phải cấp lại).

**Communication.node** — `created → broadcast → received → acked → stale`. Terminal: `stale`.

## A.2 RESPONSIBILITY (Organization §6)
```
assigned → accepted → [transferred] → completed → released
```
- Terminal: `released`. CẤM: `completed` khi chưa `accepted`. `transferred` chuyển Responsibility, KHÔNG chuyển Accountability.

## A.3 KNOWLEDGE objects
**Fact**
```
hypothesized → verified → committed → decaying → re-verified
                                          └→ archived → forgotten
```
- Terminal: `forgotten` (chỉ khi `INV` Q7: superseded + conf~0 + vô giá trị + qua retention). `archived` ≠ delete.

**SystemModel.edge** — `hypothesized → observed → verified → decaying → invalidated`. Terminal: `invalidated`.

**Experience** — `extracted → generalized → anonymized → promoted | discarded`. Terminal: `promoted`, `discarded`. CẤM: `promoted` khi chưa `anonymized` (`INV_DATA_RESIDENCY`).

**Pattern** (bắt buộc khai báo trước Learning — `INV_LIFECYCLE_BEFORE_ALGORITHM`)
```
candidate → validated → published → degrading → revalidated
                                        └→ deprecated → retired
[merge]: ≥2 published → 1 published   [split]: 1 published → ≥2 candidate
```
- Terminal: `retired`. CẤM: `published` khi chưa `validated`; `published` khi `derives_from` < N tenant (`INV_PROMOTION_GATED`).

## A.4 Quy tắc chung lifecycle
- Mọi transition → ghi CRAT nếu object là mutation/asset (`INV_AUDIT_EVERYTHING`).
- Object Derived KHÔNG có lifecycle (recompute, không trạng thái lưu) — `INV_DERIVED_NEVER_PERSIST`.
- Execution chỉ **dịch chuyển theo transition hợp lệ**, không tạo state mới. Thêm state = sửa Appendix này qua Governance (Architectural amendment), KHÔNG làm ở Execution.

---

# Appendix B — Behavior Algebra (grammar của behavior)

> KHÔNG noun/object/layer mới — chỉ **luật kết hợp** primitive. Tương tự compiler: Appendix A + 8 verb Execution + 7 verb Learning = **instruction set**; Appendix này = **control flow**.
> Bắt buộc bởi `INV_BEHAVIOR_ALGEBRA` (`FRAMEWORK_LAWS §0`): mọi algorithm = primitive kết hợp **CHỈ** qua 5 toán tử dưới đây.

## B.1 Năm toán tử composition

| Operator | Nghĩa | Ràng buộc |
|---|---|---|
| `Sequence(a, b, …)` | a rồi b rồi … | `b.pre` phải đạt được từ `a.post` (lifecycle Appendix A) |
| `Choice(g1→a, g2→b, …)` | rẽ nhánh theo guard (state/Finding) | guard loại trừ; mỗi nhánh well-formed |
| `Loop(body, until)` | lặp body tới `until` (Verify) hoặc bound (max/timeout = field) | phải có điều kiện dừng (`INV_FAIL_CLOSED`) |
| `Parallel(a, b, …)` | đồng thời rồi join | scope rời nhau (`INV_NAMESPACE_ISOLATION`); join hợp findings |
| `Interrupt(body, sig→handler)` | body bị ngắt → handler | handler thường `Abort`/`Recover`; phủ timeout/cancellation |

→ Sequence+Choice+Loop = đầy đủ control-flow (structured program theorem); Parallel+Interrupt thêm cho concurrency + preemption. **Tập 5 là closed.**

## B.2 Well-formedness (luật hợp lệ — lifecycle-driven)

Một composition HỢP LỆ ⇔ tại mọi mối nối, precondition của primitive kế đạt được từ trạng thái lifecycle hiện tại.

| Ví dụ | Hợp lệ? | Vì |
|---|---|---|
| `Sequence(Execute, Recover)` | ✗ | Recover cần Action `failed`; Execute có thể `completed` → phải `Choice(Execute→completed \| failed→Recover)` |
| `Sequence(Abort, Recover)` | ✗ | Abort → terminal; Recover cần non-terminal |
| `Sequence(Verify, Plan)` | ✓ | Verify không đạt → Re-plan (lifecycle cho phép) |
| `Loop(Observe)` | ✓ (có bound) | nhiều Observe hợp lệ; cần điều kiện dừng |
| `Sequence(Recover, Execute)` | ✓ | rolled_back → Plan/Execute lại |

→ Toán tử + Appendix A **tự kiểm chứng** composition: nối sai lifecycle = ill-formed, bị chặn (`INV_LIFECYCLE_BEFORE_ALGORITHM`).

## B.3 Composition Library viết lại bằng algebra

```
Retry  = Loop( Sequence(Execute, Observe, Verify), until = verified ∨ max )
DryRun = Sequence( Plan, Execute(scope=sandbox), Observe, Verify )
Canary = Loop( Sequence(Plan(scope↑), Execute, Observe, Verify), until = full_scope )
Saga   = Sequence( Execute*, Choice(success | Recover_reverse) )
Assess = Sequence( Observe, Verify, recompute_derived )
Reflection = Sequence( Observe, Verify, Choice(đạt→Promote | không→Demote) )
```

→ Mọi pattern = primitive + 5 toán tử. Không pattern nào cần toán tử thứ 6.

