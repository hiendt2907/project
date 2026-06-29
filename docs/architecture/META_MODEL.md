# Meta-Model — Phân loại Object & Ranh giới Tầng

> **Status**: DESIGN ONLY — không code.
> **Created**: 2026-06-29
> **Mục đích**: KHÔNG thêm khái niệm. Giữ **ontology ổn định** — phân loại mọi object đã có và khóa ranh giới giữa các tầng, để Execution/Architecture kế thừa nền nhất quán thay vì tự định nghĩa lại.
> Tài liệu này là **luật biên** cho mọi tài liệu khác.

---

## 1. Bốn nhóm Object (CTO meta #1)

Mọi first-class object PHẢI thuộc đúng **một** nhóm. Nhóm quyết định: lifecycle, residency, ai được sửa, có versioned không, có recompute được không.

```
┌─ DEFINITION ─────────────┐  ┌─ RUNTIME ────────────────┐
│ tác giả viết, versioned, │  │ instance khi vận hành,    │
│ qua Governance, ổn định  │  │ ephemeral/per-execution   │
│  • Playbook (Op/Reason)  │  │  • Mission                │
│  • Policy                │  │  • Finding / Observation  │
│  • Culture               │  │  • Decision               │
│  • Role / Posture        │  │  • CapabilityState        │
│  • CapabilityDefinition  │  │  • AuthorityState         │
│  • Competency Matrix     │  │  • Communication node     │
│  • Acquisition Order     │  │  • Hypothesis (in-flight) │
└──────────────────────────┘  └───────────────────────────┘
┌─ KNOWLEDGE ──────────────┐  ┌─ DERIVED ─────────────────┐
│ học được, verify+decay,  │  │ TÍNH RA, không bao giờ là  │
│ bitemporal, per-tenant   │  │ source-of-truth, recompute │
│  • Fact                  │  │  • Understanding (score)   │
│  • Pattern               │  │  • Confidence              │
│  • Experience            │  │  • Trust                   │
│  • System Model (§4)     │  │  • Capability Score (=Π)   │
└──────────────────────────┘  │  • Level (nhãn)            │
                              └───────────────────────────┘
```

**Luật biên (quan trọng nhất)**:
- **DERIVED không bao giờ được lưu làm sự thật** — luôn recompute từ Knowledge/Runtime. Nếu thấy mình "ghi Trust vào DB làm nguồn" → sai, Trust phải tính lại được từ Skill×Understanding×track-record.
- **DEFINITION đổi → qua Governance** (ai-được-đổi-cái-gì, Organization §8). Runtime/Derived KHÔNG qua Governance.
- **KNOWLEDGE có vòng đời verify→decay→archive** (Knowledge Model Q4). Definition thì versioned (RFC), khác hẳn.
- **RUNTIME tham chiếu** Definition + Knowledge, KHÔNG sửa chúng.

3-gate: G1 ✅ (đang lẫn asset/runtime/derived → Architecture sẽ rối), G2 ✅, G3 ✅ (meta-model là lớp phân loại độc lập).

---

## 2. Capability: tách DEFINITION vs STATE (CTO meta #2)

`Capability = K×R×E×C×G×L` là **definition**. `Capability(Payment)=0.82` là **runtime state**. Tách đôi:

```
CapabilityDefinition  (DEFINITION, versioned, governed)
   discipline, dimensions cần đo, công thức tổng hợp (Π), thang chuẩn hóa, ngưỡng maturity

CapabilityState       (RUNTIME, per-scope, ephemeral + re-verified)
   scope, dimension_scores{}, maturity, owner, last_verified, evidence
   capability_score : DERIVED = Π(dimension_scores)   ← không lưu làm truth, recompute
```

→ Object cũ "Capability" (CAPABILITY_MODEL §3b) tách: phần `dimensions định nghĩa + công thức` = Definition; phần `scores + maturity + evidence` = State; `capability_score` = Derived. Execution đọc **State**, Governance sửa **Definition**.

---

## 3. Bảng RANH GIỚI TẦNG — khóa trước Execution (CTO meta #5)

> **Execution chỉ trả lời MỘT câu**: *"Làm thế nào biến một Decision hợp lệ thành Action AN TOÀN & PHỤC HỒI được?"*

| Câu hỏi | Tầng sở hữu | KHÔNG thuộc Execution |
|---|---|---|
| "Có **nên** làm?" | Operating / Cognitive | ✗ |
| "Có **quyền** làm?" | Organization (Authority) | ✗ |
| "Đủ **năng lực** làm?" | Capability | ✗ |
| "**Biết** hệ thống chưa?" | Knowledge | ✗ |
| "**Làm thế nào** thực thi an toàn?" | **Execution** | ✓ DUY NHẤT |

**Hệ quả — các khái niệm hay bị "vứt vào Execution", phân lại đúng chỗ:**

| Khái niệm | Thuộc về | Lý do |
|---|---|---|
| Decision vs Action | ranh giới: Decision = Cognitive/Operating; Action = Execution | Execution nhận Decision *đã hợp lệ* |
| Risk assessment | Cognitive (đánh giá) + Mission Economy (đánh đổi) | "có nên" — trước Execution |
| Blast Radius (đánh giá) | Cognitive/Culture; Execution chỉ *giới hạn* khi thực thi | đo ≠ thực thi |
| Rollback / Compensation | **Execution** | cách phục hồi an toàn |
| Retry / Idempotency | **Execution** | cách thực thi bền |
| Sandbox / Dry-run / Simulation | **Execution** | cách thực thi an toàn (đã có `execution/`) |
| Progressive delivery / Safe mutation | **Execution** | cách thực thi tiệm tiến |
| Approval / Guardrail | Governance (định nghĩa) → Execution (enforce điểm thực thi) | định nghĩa ≠ enforce |

→ Execution = "động cơ thực thi an toàn", KHÔNG phải nơi quyết định. Ranh giới sắc, chống "thùng rác".

---

## 4. Object đã phân loại — bảng tổng (single source)

| Object | Nhóm | Sống ở | Sửa bởi |
|---|---|---|---|
| Playbook (Op/Reason) | Definition | Omni | Principal + RFC |
| Policy | Definition | Omni | Architect + human |
| Culture | Definition | Omni (per-discipline) | Architect + human |
| Role / Posture | Definition | Omni | Principal |
| CapabilityDefinition | Definition | Omni | Governance |
| Competency Matrix / Acquisition Order | Definition | Omni | Principal |
| Mission | Runtime | Omni + Agent | sinh từ Playbook |
| Finding / Observation | Runtime | Agent → tenant | agent |
| Decision | Runtime | tenant (detail) / Omni (skeleton) | reasoning |
| Hypothesis (in-flight) | Runtime | Agent/tenant | reasoning |
| CapabilityState / AuthorityState | Runtime | per-scope | hệ thống |
| Fact | Knowledge | Tenant KB | verify |
| Pattern / Experience | Knowledge | Omni (anonymized) | Learning + human |
| **System Model** (§5) | Knowledge | Tenant KB | learn + verify |
| Understanding / Confidence / Trust / Capability Score / Level | Derived | recompute | — (không lưu làm truth) |

---

## 5. System Model — lớp còn thiếu của Knowledge (CTO meta #3)

> Hai agent cùng coverage 100% (Redis/Deploy/Payment) nhưng một debug nhanh hơn nhiều — vì có **causal model trong đầu**. Đó KHÔNG phải Fact/Pattern/Playbook/coverage. Đây là **lỗ lớn nhất còn lại của Knowledge**.

**System Model = mô hình NHÂN-QUẢ/HÀNH-VI của hệ thống tenant này:**
```
Deploy → Config reload → Redis reconnect → Cache cold → CPU↑ → Latency↑
   (causal edges, có độ trễ, điều kiện, xác suất lan truyền)
```

| Phân biệt | |
|---|---|
| Topology (Twin) | "A *phụ thuộc* B" (cấu trúc tĩnh) |
| **System Model** | "A *gây ra* B sau Δt" (động, nhân quả) |
| Fact | một sự thật điểm |
| Pattern | quy luật thống kê cross-tenant |
| Decision Graph | lý do một quyết định *đã* ra |

**Vai trò**:
- Là **nguồn #1 cho Hypothesis Generation** (Cognitive Q1): có causal model → sinh giả thuyết đúng hướng, không mò. Thay/làm mạnh nguồn "Topology" trước đây.
- Cho phép **predict** ("nếu deploy thì cache sẽ cold → CPU sẽ tăng") → Execution dùng để lường hậu quả trước khi act.
- Là Knowledge (per-tenant, verify + decay): causal edge phải *kiểm chứng* (quan sát Deploy→thật sự CPU tăng), không phải giả định.
- Khi promote: System Model một tenant → trừu tượng hóa thành **Pattern** (cơ chế chung) qua promotion pipeline (Knowledge Q6).

3-gate: G1 ✅ (coverage không nắm nhân-quả; đây là lỗ thật), G2 ✅ (in: observed cause→effect + Δt; out: causal graph), G3 ✅ (loại Knowledge độc lập, là lớp causal trên graph substrate — Knowledge Model §8). → Bổ sung vào taxonomy Knowledge Q1.

---

## 6. Responsibility cần LIFECYCLE (CTO meta #4)

Authority = Trust + Responsibility + Accountability (Organization §2). **Responsibility là ĐỘNG** — phải có vòng đời:

```
ASSIGN ─► ACCEPT ─► (TRANSFER ─►) COMPLETE ─► RELEASE
   │         │          │            │           │
Commander  agent      delegate     done        trả về
giao       nhận       sang agent   nhiệm vụ    pool
                      khác
```

- **Responsibility chuyển** khi delegate (Commander → DB Agent → Junior).
- **Accountability có thể KHÔNG chuyển** (Commander delegate nhưng vẫn chịu outcome) — tách bạch ở Organization §2.
- Mỗi transition ghi CRAT → **audit cực mạnh**: truy được "ai cầm trách nhiệm gì, lúc nào, ai vẫn accountable".
- Nối Coordination (Organization §4): Task Graph chính là đồ thị Responsibility được assign/transfer.

3-gate: G1 ✅ (Responsibility tĩnh không mô tả delegation thật), G2 ✅, G3 ◑ (không object mới — thêm *state machine* cho Responsibility đã có).

---

## 7. Nguyên tắc giữ ontology ổn định (từ đây trở đi)

1. **Ưu tiên consolidation hơn mở rộng**: trước khi thêm khái niệm, hỏi "nó là Definition/Runtime/Knowledge/Derived?" + chạy 3-GATE. Không xếp được nhóm → chưa đủ chín.
2. **Derived không bao giờ là truth.** Recompute, đừng lưu.
3. **Definition đổi qua Governance; Knowledge qua verify; Runtime ephemeral.**
4. **Mỗi tầng một câu hỏi** (bảng §3) — không lấn.
5. **Tên ổn định**: không đổi tên object đã phân loại; mở rộng bằng field, không bằng khái niệm mới.

---

## 8. Cập nhật trạng thái

| Tầng | Maturity | Ghi chú |
|---|---|---|
| Vision | 10 | |
| Capability | 9.5 | + tách Definition/State (meta §2) |
| Organization | 9 | + Responsibility lifecycle (meta §6) |
| Operating | 9 | thiếu Mission Economy |
| Cognitive | 8.5 | System Model thành nguồn hypothesis #1 |
| Knowledge | 9.5 | + System Model (meta §5) |
| **Meta-Model** | mới | tài liệu này — luật biên |
| Learning | 2 | chưa thiết kế |
| Domain | 8 | |
| Execution | 1 | ranh giới đã KHÓA (meta §3), chưa thiết kế |
| Architecture | 3 | sẽ hưởng lợi lớn từ object taxonomy |

→ Sẵn sàng sang **Execution Model** với ranh giới sắc: chỉ "làm thế nào biến Decision hợp lệ thành Action an toàn & phục hồi được".
