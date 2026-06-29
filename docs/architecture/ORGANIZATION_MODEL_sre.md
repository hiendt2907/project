# Organization Model — AOIP như một TỔ CHỨC SRE

> **Status**: DESIGN ONLY — không code. **Rev 2** (2026-06-29) — tiếp thu 5 challenge của CTO.
> **Vị trí stack** (Capability là trục gốc):
> `Vision → Capability → **Organization** → Operating → Cognitive → Knowledge → Learning → Domain → Architecture → Implementation`
> Tầng này trả lời: **"AI tồn tại như một TỔ CHỨC thế nào"** — đóng góp chiều **Coordination** + khung **Governance** cho Capability.

> **Rev 2 đổi gì** (so Rev 1): (1) Capability tách ra tầng gốc riêng (`CAPABILITY_MODEL.md`); (2) **tách Skill × Understanding × Authority** (bỏ Level làm primitive); (3) thêm **Posture** trục độc lập với Role; (4) thêm **Culture Model**; (5) **Communication Graph = information lineage**, graph riêng (không phải projection của Decision Graph); (+) Attention Allocation, Incentive; Governance thu nhỏ.

---

## 1. Phát hiện cốt lõi: một Agent = tổ chức thu nhỏ; nhiều Agent = tổ chức thật

Một Senior có nhiều **mindset** luân phiên (Incident Commander → Investigator → Security → Architect → Reviewer). Một người, nhiều vai.

```
Omni cũ:   Mission → Agent → Done                  (thiếu tầng Organization)
Phải là:   Culture → Roles/Posture → Teams → Coordination → Mission → Done
```

Onboarding nghĩ như phân vai đội (Architect/PlatformEng/Security/DBA/Observer) *trước*, rồi convert thành Mission.

---

## 2. BA trục tự chủ: Skill × Understanding × Authority (CTO challenge #2)

Trước đây gộp tất cả vào "Level" — sai. Tách 3 trục trực giao:

```
SKILL          = "tôi GIỎI cỡ nào"        — năng lực NỘI TẠI của agent (global, theo agent)
UNDERSTANDING  = "tôi HIỂU tenant cỡ nào" — hiểu biết môi trường CỤ THỂ (per-tenant)
AUTHORITY      = "tôi ĐƯỢC PHÉP cỡ nào"   — quyền trong tổ chức (granted, earned qua trust)
```

**Ví dụ CTO**: Principal Google sang Stripe → `Skill=Principal, Understanding=0 (mới), Authority=Junior (chưa có organizational trust)`.

```
Real autonomy(scope) = min( tenant_tier,
                            Authority,
                            role.posture_ceiling )
   với   Authority = f(Skill, Understanding(tenant,scope), track_record)   ← earned, tăng dần
   và    Capability(scope) phải đủ (CÓ THỂ)  — cổng AND độc lập (Capability Model §4)
```

- **Skill** (Domain Model): earned toàn cục từ mission/verify/approval; có decay.
- **Understanding** (Knowledge Model): UnderstandingScore per-tenant/scope (Competency Matrix). Đây là "5 năm tại đây".
- **Authority** (tầng này): bắt đầu THẤP ở tenant mới dù Skill cao; leo lên khi Skill×Understanding×track-record tích lũy = **organizational trust**.

### Authority KHÔNG phải RBAC permission (CTO ontology #3)

Authority hiện gần giống `permission` — quá hẹp. Trong tổ chức thật, Authority = **3 thành phần**:

```
Authority {
  trust          : float     # ĐƯỢC PHÉP cỡ nào (earned từ Skill×Understanding×track-record)
  responsibility : [ScopeRef]# CHỊU TRÁCH NHIỆM làm gì (phạm vi nhiệm vụ được giao)
  accountability : AgentRef  # AI TRẢ LỜI cho outcome (có thể ≠ người hành động)
}
```

**Ví dụ CTO**: một agent *được phép* restart (trust đủ) nhưng *không chịu trách nhiệm* incident → accountability vẫn thuộc Mission Commander/human.

- **Trust** = "được làm" (gate hành động — phần RBAC cũ).
- **Responsibility** = "việc của tôi" (scope nhiệm vụ; ngoài scope → không tự ý dù trust đủ).
- **Accountability** = "ai trả lời nếu sai" — tách khỏi người hành động. Quan trọng cho:
  - **CRAT/audit**: ghi cả actor (ai làm) lẫn accountable (ai chịu) — đã có actor, thêm accountable.
  - **Learning/Failure Model**: outcome sai → quy về accountable để cập nhật trust, không chỉ actor.
  - **Delegation**: Commander giao Junior thực thi (actor) nhưng giữ accountability → Junior dám làm trong khuôn khổ, Commander chịu trách nhiệm.

```
real_autonomy(scope) = min( tenant_tier, Authority.trust, role.posture_ceiling )
                       ∧ scope ∈ Authority.responsibility
   accountability được track song song (không gate hành động, nhưng gate học & audit).
```

3-gate: G1 ✅ (RBAC không tách được "được làm" vs "chịu trách nhiệm"), G2 ✅, G3 ✅ (object Authority 3-phần, độc lập).

→ "Level" (Junior→Architect) chỉ còn là **nhãn dẫn xuất** từ Skill/Understanding/Authority để hiển thị, KHÔNG phải primitive.

| Nhãn Level (derived) | Authority cho phép |
|---|---|
| Junior | đọc/discover/checklist, READ_ONLY |
| Mid | suy luận có giám sát, đề xuất |
| Senior | suy luận độc lập, review Junior/Mid, mutate qua HITL |
| Principal | tạo/sửa Playbook & Policy (RFC + co-approve) |
| Architect | thiết kế topology, chuẩn cross-discipline |

3-gate: G1 ✅ (3 trục riêng giải bài "giỏi nhưng chưa được phép"), G2 ✅, G3 ✅ (Authority resolver độc lập; Skill/Understanding đã ở tầng khác).

---

## 3. Role × Posture (CTO challenge #3)

Trước chỉ có Role. Thêm **Posture** = *thái độ/stance* — trực giao với Role.

```
ROLE     = chức năng/mindset      (Security, DBA, Architect, Observer, IncidentCommander...)
POSTURE  = lập trường hành động    (Observe → Advise → Suggest → Aggressive)
```

**Ví dụ**: cùng Role=Security:
```
Security AUDITOR   → Posture: Aggressive  (chủ động truy, đề xuất chặn)
Security ADVISOR   → Posture: Suggest     (chỉ khuyến nghị)
```

- Posture đặt **trần hành động** (`posture_ceiling`) độc lập với Authority. Mission khai báo `(Role, Posture)`; Authority quyết có với tới trần đó không.
- Posture cho phép cùng một agent vận hành "gắt" ở scope rủi ro-thấp và "dè dặt" ở scope critical mà không đổi Role.

3-gate: G1 ✅ (Role không biểu diễn được stance), G2 ✅, G3 ✅ (trục mô tả độc lập).

---

## 4. Coordination + Attention Allocation (CTO challenge #4 — coordination)

Senior không chỉ chia việc — còn **quản lý attention**. 5 task KHÔNG ngang nhau.

```
                 MISSION COMMANDER (agent Authority≥Senior)
                          │ phân rã + phân bổ ATTENTION
                          ▼
      TASK GRAPH (DAG) + ATTENTION BUDGET
   ┌──────────┬──────────┬──────────┬──────────┐
   ▼          ▼          ▼          ▼          ▼
 Deploy     Redis      CPU       Network    Monitoring
  80%        15%        5%         ...        ...        ← Attention, không phải đều nhau
   └──────────┴────┬─────┴──────────┴──────────┘
                   ▼
            MERGE FINDINGS → DECISION
```

**Attention Model** (thứ làm Principal khác Senior):
- **Attention Budget**: phân bổ nỗ lực/agent/thời gian theo prior likelihood × business impact (nối Cognitive prior + Mission Economy).
- **Priority Shift**: tái phân bổ khi bằng chứng mới xuất hiện (Redis hóa ra vô can → dồn về Deploy).
- **Focus**: tập trung sâu một nhánh thay vì rải mỏng.
- **Escalation**: nâng cấp khi vượt Authority/Capability hiện có (gọi agent Level cao hơn / human).

3-gate: G1 ✅ (chưa có quản lý attention), G2 ✅ (in: task graph + evidence; out: budget allocation), G3 ✅ (Coordinator component).

---

## 5. Communication Graph = Information Lineage (CTO challenge #5)

CTO đúng: Communication ≠ projection của Decision Graph. **Hai semantics khác nhau, hai graph khác nhau, có thể chung substrate.**

```
DECISION GRAPH      = reasoning lineage      "VÌ SAO ta quyết định"
COMMUNICATION GRAPH = information lineage     "AI BIẾT GÌ, đã truyền cho ai, khi nào"
```

**Tại sao phải tách** (ví dụ CTO): Agent A không gửi finding → Decision sai. **Lỗi ở Communication, không ở Decision.** Nếu coi Communication là view của Decision → không truy được lỗi truyền tin.

```
Communication Graph lưu:
   node: finding (A biết Redis restart @10:03) + ai-sở-hữu + thời gian + đã-broadcast-cho-ai
   edge: truyền tin / liên kết thông tin (A→Commander, B→Commander)
   → optimize được riêng: broadcast, routing, consensus, compression
```

- Cùng **graph substrate** với Decision/Knowledge Graph (Knowledge Model §8) nhưng là **lớp riêng** với lifecycle riêng (sinh→truyền→nhận→ack→stale).
- Decision Graph *tiêu thụ* Communication Graph (suy luận trên thông tin đã nhận), không *là* nó.

3-gate: G1 ✅ (information lineage chưa ai giữ; lỗi truyền tin không quy được), G2 ✅, G3 ✅ (**component độc lập**, không phải view — sửa lại Rev 1).

---

## 6. Culture Model (CTO challenge #6 — thiếu lớn nhất)

Organization thật có **Culture** — sinh ra decision; Policy chỉ *enforce*.

```
Organization → CULTURE → Governance → Behavior
               (sinh        (cho      (hành
                quyết        phép)     động)
                định)
```

**Culture = nguyên tắc hành vi mặc định** (định hình mọi quyết định khi luật không nói rõ):
```
• Prefer Observation over Opinion       (runtime > phỏng đoán — nối Trust Model)
• Prefer Falsification                   (cố bác giả thuyết — nối Cognitive Q2)
• Prefer Small Blast Radius              (chọn hành động ít rủi ro nhất đủ giải quyết)
• Prefer Explainability                  (quyết định phải truy nguồn được)
• Never Hide Uncertainty                 (luôn phơi bày độ bất định — nối Failure Model)
```

**Culture ≠ Policy**:
- Policy = luật cứng, enforce, vi phạm = chặn (governance).
- Culture = thiên hướng mềm, định hình *lựa chọn* khi có nhiều đường hợp lệ. Là "default reasoning bias" đúng hướng.

3-gate: G1 ✅ (chưa có lớp định hình hành vi mặc định; Policy không làm việc này), G2 ✅ (in: tình huống nhiều lựa chọn hợp lệ; out: thiên hướng chọn), G3 ✅ (tập nguyên tắc độc lập, áp vào Cognitive làm prior bias).

---

## 7. Incentive / Objective Function (CTO challenge #7)

Tổ chức optimize theo incentive. Không có objective tường minh → agent optimize theo prompt (sai).

```
Ví dụ: A (2 phút, 95% chắc)  vs  B (30 phút, 98% chắc)  → chọn gì?
   → cần OBJECTIVE FUNCTION rõ ràng.

maximize:  Safety × Learning × (1/Latency)      (tích — nối triết lý Capability, Safety=0 → loại)
   tinh chỉnh per-mission: incident gấp → trọng Latency; onboarding → trọng Learning.
```

- Objective được Mission Economy (Operating, ⬜) cung cấp trọng số theo business context.
- **Safety là factor nhân** (không phải cộng): hành động kém an toàn → objective sụp về 0, dù nhanh/học nhiều.

3-gate: G1 ✅ (chưa có hàm mục tiêu, agent optimize prompt), G2 ✅, G3 ◑ (objective *function* thuộc tầng này về định nghĩa; *trọng số* lấy từ Mission Economy — ranh giới rõ).

---

## 8. Governance — thu nhỏ về ĐÚNG MỘT câu hỏi (CTO challenge #8)

> **Governance chỉ trả lời: "AI được phép THAY ĐỔI cái gì?"** — không hơn.

```
Governance định nghĩa:  ai (Authority nào) được đổi {Playbook, Pattern, Policy, Experience, automation tier, mutation}.
Audit / RFC / Approval / Compliance  =  IMPLEMENTATION của governance (thuộc Architecture/CRAT), KHÔNG phải định nghĩa.
```

Ma trận (gắn Authority §2):
| Thay đổi | Authority tối thiểu |
|---|---|
| Operating/Reasoning Playbook | Principal |
| Pattern / Experience global | Principal + human |
| Policy | Architect + human |
| Automation tier-up / mutation | human luôn |

→ Governance KHÔNG còn rải khắp các tầng (chống "thùng rác"). Mọi cơ chế thực thi (CRAT hash-chain, RFC flow, fail-closed) là *implementation* — đã có, sẽ hợp nhất ở Architecture.

---

## 9. Stack cập nhật + tiến độ

`Vision → Capability → Organization → Operating → Cognitive → Knowledge → Learning → Domain → Architecture → Implementation`

| Tầng | Tiến độ | Ghi chú |
|---|---|---|
| Vision | 100% | AOIP |
| **Capability** | ~85% | `CAPABILITY_MODEL.md` — tích 6 chiều |
| **Organization** | ~70% | Rev 2 — 3-trục, Posture, Attention, Comm Graph, Culture, Incentive, Governance-nhỏ |
| Operating | 95% | thiếu **Mission Economy** (Strategic+Economy+objective weights) |
| Cognitive | 90% | + Meta-Cognition, Culture làm prior bias |
| Knowledge | 90% | |
| Learning | 0% | + Evolution Model |
| Domain | 85% | Skill sống ở đây |
| Execution | 10% | + **Execution Philosophy** (Decision≠Action) |
| Architecture | 15% | governance implementation hợp nhất ở đây |

Tổng ~50%. Nửa còn lại = tổ chức AI vận hành thật (cộng tác/quyết định/tiến hóa/quản trị/thực thi).

---

## 10. Điểm cần CTO chốt tiếp

1. **"Level" giữ làm nhãn dẫn xuất hay bỏ hẳn** khỏi từ vựng (chỉ dùng Skill/Understanding/Authority)? (đề xuất: giữ làm nhãn hiển thị, không phải primitive).
2. **Culture có versioned & per-discipline không** (SRE culture vs FinOps culture khác)? (đề xuất: có, Culture là tài sản pluggable theo discipline).
3. **Objective function**: cố định `Safety × Learning × 1/Latency` hay để Mission Economy cấu hình hoàn toàn? (đề xuất: Safety là factor cứng, phần còn lại Mission Economy cấu hình).
4. **Tầng kế**: đề xuất **Execution Model (Decision≠Action, khi nào KHÔNG làm)** — vì Execution≈10% đang kéo Capability (tích) về gần 0.
