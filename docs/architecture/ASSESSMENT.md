# Assessment — Vòng phản hồi đo Capability (Phase 4: Runtime Dynamics)

> **Status**: DESIGN ONLY — không code.
> **Created**: 2026-06-29
> **Bản chất**: KHÔNG phải layer/model/object/noun mới (tuân `INV_NO_NEW_NOUNS`). Đây là **thuật toán + feedback loop** chạy XUYÊN các layer đã có. `Assess` KHÔNG phải verb thứ 9 — nó là **composition** `Observe → Verify → recompute(Derived)`.
> **Giải quyết khoảng trống lớn nhất**: có CapabilityDefinition/State/Score nhưng chưa có *cách đo*. Không có Assess → "Learning học được nhưng Capability không đổi".

---

## 1. Vòng vận hành ĐẦY ĐỦ (thêm mắt xích Assess)

```
Observe → Reason → Execute → Learn → ASSESS → CapabilityState update
                                        │              │
                                        │       recompute Derived
                                        ▼              ▼
                              AuthorityState update ← Trust update ← Understanding update
                                        │
                                  effective_autonomy đổi
```

→ Đóng vòng: Capability↑ ⇒ Authority↑ ⇒ tự chủ rộng hơn; thất bại ⇒ Capability↓ ⇒ Authority suspended. Không có vòng này, hệ thống học mà không "lớn lên".

---

## 2. Assess là COMPOSITION (không primitive — `INV_MINIMAL_PRIMITIVES`)

```
Assess = Observe(outcomes) → Verify(outcome vs predicted) → attach Finding vào CapabilityState.evidence
         → recompute Derived (DERIVED_NEVER_PERSIST: Understanding, Trust, CapabilityScore)
```
- `Observe`/`Verify`: tái dùng verb Execution (Primitive Reuse).
- `attach evidence`: ghi vào **field** `CapabilityState.evidence` (đã có ở object model) — không noun mới.
- `recompute`: bắt buộc bởi `INV_DERIVED_NEVER_PERSIST` — Derived luôn tính lại, không lưu làm truth.

→ Pass `INV_PATTERN_COMPLETENESS`: expand hoàn toàn về primitive, không state riêng, không lifecycle mới.

---

## 3. Thuật toán đo từng chiều Capability (từ evidence → [0,1])

Mỗi chiều = aggregate Finding/outcome (evidence) đã verify, chuẩn hóa [0,1]; maturity từ count+recency.

| Chiều | Nguồn evidence (Finding đã verify) | Công thức (ý niệm) |
|---|---|---|
| **K** Knowledge | = `Understanding(scope)` (Knowledge §Q9, đã có) | weighted coverage × verified_ratio |
| **R** Reasoning | hypothesis falsification accuracy; root-cause về sau verify đúng; decision→outcome | success_rate có trọng số recency |
| **E** Execution | Action success; rollback rate; recovery success; blast-radius adherence | success − penalty(rollback, blast) |
| **C** Coordination | multi-agent mission completion; merge quality; conflict resolved | completion_rate × merge_quality |
| **G** Governance | tỉ lệ 0 vi phạm law; audit completeness | 1 − violation_rate |
| **L** Learning | promote accuracy; demote/forget đúng; drift = 0 | promotion_precision × (1−drift) |

- `CapabilityScore = Π(6 chiều)` (Derived, `INV_CAPABILITY_IS_PRODUCT`) — chiều yếu nhất kéo xuống.
- `maturity` (nascent→…→proven) `derives_from` evidence count + age + track-record; score cao + maturity thấp → vẫn dè dặt.
- Decay theo `INV_KNOWLEDGE_TEMPORAL`: thiếu evidence mới → chiều rớt → re-assess.

---

## 4. Propagation — evidence lan tới Authority (đóng vòng tổ chức)

```
CapabilityState (mới) + Understanding (mới)
   → Trust = f(Skill × Understanding × track_record)        (Derived, recompute)
   → AuthorityState transition:
        trust ↑ qua ngưỡng  → granted → elevated
        trust ↓ / vi phạm    → suspended → (re-earn) | revoked
   → effective_autonomy(scope) = min(tenant_tier, Authority.trust, posture_ceiling) ∧ scope∈responsibility
```

- Tuân `INV_AUTHORITY_EARNED_PER_TENANT`: agent mới → evidence ít → Authority thấp; tích evidence đúng môi trường → leo dần ("5 năm tại đây").
- Mọi AuthorityState transition → CRAT (`INV_AUDIT_EVERYTHING`, actor + accountable).
- Confidence/Trust propagation theo `INV_TRUST_PROPAGATION` (weakest-link + corroboration) — không thuật toán mới, tái dùng Knowledge §5.

---

## 5. Đặc tính & luật

- **Evidence-driven, explainable**: mọi thay đổi Capability/Authority truy ngược được tới Finding (`INV_EXPLAINABILITY`). Không "tự dưng giỏi lên".
- **Đề xuất luật mới** `INV_ASSESSMENT_CLOSES_LOOP` [Architectural]: Derived state (Capability/Understanding/Trust/Authority) CHỈ được đổi qua Assess từ evidence; Learning/Execution KHÔNG được trực tiếp sửa autonomy mà không qua Assessment. → chống "tự phong quyền".
- 0 object/layer/noun mới; chỉ field (`evidence`) + algorithm + feedback wiring.

---

## 6. Phase 4 — Runtime Dynamics (bản đồ phần còn lại)

Assessment là item đầu. Các item còn lại của Phase 4 đều là **thuật toán/cơ chế runtime**, KHÔNG ontology:

| Item | Tái dùng |
|---|---|
| **Capability Assessment** (này) | CapabilityState.evidence, Understanding, Trust, AuthorityState |
| Confidence propagation | `INV_TRUST_PROPAGATION` (Knowledge §5) |
| Authority evolution | AuthorityState lifecycle (Appendix A) + §4 |
| Resource / Attention allocation | Organization §4 (Attention Budget) |
| Runtime scheduling | Mission lifecycle + Coordination |
| Graph execution / Storage topology / Distributed consistency | Architecture (`implements FRAMEWORK_LAWS`) |

→ Tất cả là Phase 4, đo bằng ΔCapability/ΔComplexity. Không mở ontology.

---

## 7. Maturity Ratios (tài liệu này)

| Ratio | Đạt |
|---|---|
| Object Reuse | 100% (0 object mới; chỉ field `evidence` + algorithm) |
| Primitive Reuse | Assess = composition Observe/Verify, 0 verb mới |
| Law Coverage | 100% (tham chiếu law có sẵn; thêm 1 law feedback `INV_ASSESSMENT_CLOSES_LOOP`) |

> Sau Assessment, chu trình vận hành ĐÓNG: Observe→Reason→Execute→Learn→Assess→(Capability/Authority/Understanding update)→Observe. Đây là điều kiện để Architecture hiện thực một runtime nhất quán.
