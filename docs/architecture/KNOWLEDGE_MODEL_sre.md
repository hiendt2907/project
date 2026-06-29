# Knowledge Model — Hệ thần kinh của AOIP

> **Status**: DESIGN ONLY — không code, không refactor.
> **Created**: 2026-06-29
> **Vị trí stack 8 tầng** (CTO chốt, đã đảo thứ tự + thêm Learning):
> `Vision → Operating → Cognitive → **Knowledge** → Learning → Domain → Architecture → Implementation`
> Knowledge Model = "AI **lưu & cấu trúc tri thức** thế nào". Learning Model (tầng sau, chưa làm) = "AI **giỏi lên** thế nào".

---

## 0. Nguyên tắc chống over-engineering (áp dụng toàn tài liệu)

> Mọi khái niệm mới phải qua **3-GATE** trước khi được thêm:
> **G1.** Giải quyết vấn đề gì mà tầng hiện tại CHƯA giải quyết?
> **G2.** Có input/output rõ ràng không?
> **G3.** Là **component độc lập** hay chỉ là **góc nhìn khác** của thứ đã có?
> *Không qua được 3-gate → KHÔNG thêm. Nếu chỉ là "view" → nói thẳng, không dựng store mới.*

Kết luận trước (đã chạy 3-gate cho từng khái niệm — chi tiết ở §3):
- **Memory Hierarchy**: KHÔNG phải 5 database mới. Là **5 LỚP TRUY CẬP (view + TTL policy)** trên cùng một nền graph + experience store. Chỉ Collective là store vật lý riêng (ở Omni).
- **Digital Twin / Decision Graph / Knowledge Graph**: KHÔNG phải 3 hệ thống. Là **3 lớp/chiếu** trên một graph substrate (§7).
- Khái niệm thật sự MỚI (qua gate): **Trust/Provenance propagation** (§5) và **Collective promotion pipeline** (§6). Phần còn lại là cấu trúc hóa thứ đã thiết kế.

---

## Q1. Tri thức phân loại thành những loại nào?

7 loại, mỗi loại có vai trò riêng (KHÔNG trộn vào "Knowledge" chung):

| Loại | Là gì | Ví dụ | Bản chất |
|---|---|---|---|
| **Observation** | dữ liệu thô vừa thấy | "port 6379 mở" | transient, chưa diễn giải |
| **Hypothesis** | diễn giải chưa chắc | "6379 ~ Redis" | có confidence, có thể bị bác |
| **Fact** | đã verify | "Redis ở 6379 (runtime confirm)" | semantic, bitemporal |
| **Procedure** | cách làm | "cách debug Redis" | reusable, ordered |
| **Experience** | bài học từ nhiều ca | "Sentinel split-brain → check quorum" | anonymized, cross-tenant |
| **Pattern** | quy luật tổng quát | "Redis fail + sentinel → 80% là bug X" | thống kê, global |
| **Policy** | ràng buộc/luật | "mutate Redis cần HITL" | governance |
| **System Model** | mô hình NHÂN-QUẢ của hệ thống tenant | "Deploy→cache cold→CPU↑ sau Δt" | causal, verify+decay (xem `META_MODEL.md §5`) |

→ Observation→Hypothesis→Fact là vòng tiến hóa (Cognitive Model). Experience/Pattern là sản phẩm của Learning Model (tầng sau). Policy là input từ governance.

---

## Q2. Mỗi loại sống ở đâu? (residency — quan trọng nhất)

```
                    REMOTE AGENT          TENANT KB (phía khách)        OMNI (global)
Observation         sinh ở đây (transient)  —                          —
Hypothesis          sinh, triage tại chỗ    treo "vùng nghi"           skeleton ẩn danh
Fact (semantic)     —                       ✅ DETAIL (value thật)      SKELETON (type+rel+crit, no value)
Procedure           cache cục bộ            ✅ Decision Graph chi tiết   pattern ẩn danh
Experience          —                       —                          ✅ Collective (anonymized)
Pattern             —                       —                          ✅ global
Policy              enforce                 áp dụng                      ✅ source-of-truth
```

**Bất biến residency** (nối `INV_DATA_RESIDENCY` / `INV_DOC_RESIDENCY`):
- Value nhạy cảm (IP/config/secret/doc/PII) → CHỈ ở Tenant KB phía khách.
- Omni chỉ giữ **Skeleton** (type + anonymous-id + relationship + criticality + health + confidence + capability-tags) + **Experience/Pattern/Policy** đã ẩn danh.

---

## Q3. Memory Hierarchy hoạt động ra sao? (não người: ACT-R/Soar)

**3-GATE check trước**:
- G1: giải quyết "tri thức bị trộn lẫn, không phân biệt cái-đang-xử-lý vs cái-đã-biết vs cái-học-chung". ✅
- G2: I/O rõ — mỗi tầng có scope + TTL + nguồn ghi/đọc rõ. ✅
- G3: **phần lớn là VIEW, không phải store mới.** Chỉ Collective là store vật lý riêng. → KHÔNG dựng 5 DB.

| Tầng | Là gì | Map về thứ ĐÃ CÓ | Residency | TTL |
|---|---|---|---|---|
| **Working** | ngữ cảnh mission/incident HIỆN TẠI | `session_state`, mission context, `trace_memory` | Agent + Omni (ephemeral) | phút–giờ |
| **Episodic** | ca đã qua (incident tuần trước) | trace history, `archivist`, advisory pairs | Tenant KB | tháng |
| **Semantic** | fact về hệ thống (Redis 6379) | **Digital Twin** (VerifiedFact) | Tenant KB (detail) / Omni (skeleton) | dài, có decay |
| **Procedural** | cách làm (debug Redis) | Operating/Reasoning Playbook + Decision Graph | Omni (playbook) + Tenant (decision graph) | dài |
| **Collective** | pattern từ 500 khách (ẩn danh) | **Experience/Pattern store** | Omni global | rất dài |

**Luồng giữa các tầng**:
```
Working (đang xử lý) ──khi mission xong──► Episodic (lưu ca)
Episodic (nhiều ca) ──Learning Model──► Procedural (rút cách làm) + Semantic (cập nhật fact)
Procedural/Episodic nhiều tenant ──promotion (Q6)──► Collective
Semantic ──decay/expiry──► re-verify hoặc Forget (Q7)
```

→ Đây chính là Working/Episodic/Semantic/Procedural/Collective CTO nêu, nhưng **ánh xạ vào component đã có** thay vì đẻ mới (kỷ luật 3-gate).

---

## Q4. Vòng đời từng loại tri thức

```
SINH ──► VERIFY ──► COMMIT ──► VERSION ──► (DECAY) ──► RE-VERIFY ──► ARCHIVE ──► FORGET
 │         │          │          │            │           │            │          │
Observation Hypothesis Fact    bitemporal   confidence  mission      hạ tin     chỉ khi
 thô       →được test  vào Twin  (Q Temporal) giảm dần   sinh ra      giữ lịch   vô hại +
                                                                       sử         superseded
```

- **Fact**: bitemporal (observation/valid/verified/changed time) → decay theo `now - verified_time` → tự sinh re-verify mission cho fact critical.
- **Archive ≠ Delete**: giữ lịch sử niềm tin ("từng tin X, sai vì Y") cho Failure Model.
- **Forget** thật sự chỉ khi: superseded + confidence ~0 + không còn giá trị điều tra + qua retention. (Q7 chi tiết).

---

## Q5. Provenance & Trust lan truyền qua suy luận thế nào? ⭐ (khái niệm MỚI, qua 3-gate)

**3-GATE**: G1 — chưa tầng nào trả lời "fact suy ra từ nguồn yếu thì đáng tin tới đâu". G2 — input: provenance chain + trust nguồn; output: confidence của kết luận. G3 — component độc lập (một hàm propagation), không phải view. ✅✅✅

**Provenance** = mọi fact/hypothesis mang chuỗi nguồn gốc:
```
Fact "Redis = cache cho Payment"
   provenance: [ config(app.yml, trust 70), runtime(netstat, trust 100), owner(anh Nam, trust 95) ]
```

**Trust propagation qua inference** (lan truyền kiểu taint — weakest-link cho kết luận hợp thành):
```
Kết luận = suy ra từ {fact A (conf .9), fact B (conf .6)}
   → confidence(kết luận) ≤ min(inputs) × strength(inference rule)
   (một mắt xích yếu kéo cả chuỗi xuống — đúng cách Senior nghi ngờ)

Nếu nhiều nguồn ĐỘC LẬP cùng khẳng định → confidence TĂNG (corroboration):
   conf = 1 - Π(1 - conf_i)   (independent boost)
```

**Quy tắc**:
- Kết luận KHÔNG được tin hơn nguồn yếu nhất trong chuỗi (trừ khi có corroboration độc lập).
- Provenance đi kèm fact suốt đời → khi nguồn gốc bị bác (Failure Model), mọi fact dẫn xuất tự động bị hạ confidence (cascade invalidation).
- Đây là cơ chế khiến AI "giải thích được" (Principle: every action explainable) — luôn truy ngược được vì sao tin.

---

## Q6. Khi nào tri thức tenant → Experience toàn cục? ⭐ (pipeline MỚI, qua 3-gate)

**3-GATE**: G1 — chưa có cơ chế biến bài học 1 khách thành tài sản chung an toàn. G2 — input: episodic tri thức nhiều tenant; output: Pattern ẩn danh. G3 — pipeline độc lập (Learning Model sẽ sở hữu). ✅

**KHÔNG promote ở lần đầu.** Ví dụ CTO: Customer A Redis fail → sentinel bug; chỉ khi B, C cũng vậy → mới thành Pattern.

```
Episodic (Customer A)  ┐
Episodic (Customer B)  ├─► Reflection ─► Extract Pattern ─► Generalize ─► Anonymize ─► Validate ─► Publish
Episodic (Customer C)  ┘                                                                              │
                                                                                            Collective (Omni)
```

**Tiêu chí promotion (gate cứng)**:
1. **Recurrence**: xuất hiện ≥ N tenant độc lập (N≥3) — không phải đặc thù 1 môi trường.
2. **Anonymization hoàn toàn**: strip mọi định danh tenant (hostname/IP/tên người/tên service riêng) → chỉ còn cơ chế trừu tượng. Verify bằng scan PII trước publish.
3. **Generalization**: pattern phát biểu ở mức cơ chế ("Sentinel quorum < N/2+1 → split-brain"), không ở mức instance.
4. **Validation**: kiểm chứng pattern giữ đúng trên holdout / không mâu thuẫn experience hiện có.
5. **Human-approve** (giai đoạn đầu): pattern global ảnh hưởng mọi khách → cần duyệt.

→ Đây là chỗ "Collective Experience" trở thành **tài sản #1** (Q ranking dưới): nó nâng MỌI khách, không chỉ một.

---

## Q7. Khi nào AI được "quên" / hạ confidence?

**Hạ confidence (mềm)** khi:
- Fact quá `verified_time + TTL` (Temporal decay).
- Nguồn trust cao mâu thuẫn (Trust Model).
- Provenance gốc bị bác → cascade (Q5).
- Drift: Twin lệch reality khi re-verify.

**Forget (cứng — archive rồi loại khỏi active)** chỉ khi ĐỦ cả:
- Superseded bởi fact mới đã verified, VÀ
- Confidence ~0 kéo dài, VÀ
- Không còn giá trị điều tra (không liên quan incident/lịch sử nào), VÀ
- Qua retention policy.

**Nguyên tắc**: ưu tiên *quên có kiểm soát* (archive + version) hơn *xóa*. Lịch sử niềm tin là dữ liệu cho Failure Model & Learning. "Quên" giống người: không truy cập được ở active memory, nhưng episodic vẫn lưu vết.

---

## Q8. Digital Twin ↔ Decision Graph ↔ Knowledge Graph liên hệ thế nào? ⭐

**3-GATE phát hiện**: KHÔNG phải 3 hệ thống độc lập → là **3 LỚP trên một graph substrate**. Dựng 3 DB riêng = over-engineering.

```
                  KNOWLEDGE GRAPH (substrate — tất cả node + edge + fact)
                  ════════════════════════════════════════════════════════
                   nodes: Entity, Person, Incident, Change, Capability...
                   edges: depends_on, owns, caused_by, part_of...
                          │                              │
            ┌─────────────┘                              └──────────────┐
            ▼                                                            ▼
   DIGITAL TWIN (chiếu HIỆN TẠI)                    DECISION GRAPH (lớp SUY LUẬN)
   = projection present-state của KG                = overlay reasoning trên KG
   "hệ thống ĐANG thế nào"                          "VÌ SAO ta kết luận/hành động"
   = Semantic Memory                                = Procedural + Episodic
   present-tense, decay/re-verify                   append-only, có outcome
```

- **Knowledge Graph** = nền chung (mọi loại tri thức Q1 đều là node/edge ở đây).
- **Digital Twin** = lát cắt "trạng thái hiện tại" của KG cho infra một tenant (Semantic Memory).
- **Decision Graph** = lớp phủ nhân-quả/lý-do trên KG (chuỗi observation→hypothesis→conclusion→action→outcome).
- Cả 3 dùng chung graph store → một fact xuất hiện một lần, Twin và Decision Graph **tham chiếu** nó (nối Principle "no duplicated knowledge").

---

## Q9. Understanding — ontology (CTO ontology #2)

"Understanding(tenant, scope)" được nhắc nhiều nhưng chưa định nghĩa. Đây là object đo được, KHÔNG mơ hồ.

**Understanding = weighted coverage × verification, trên 8 chiều hiểu biết:**

```
Understanding {
  scope          : ScopeRef
  dimensions     : { chiều → coverage[0,1] × verified_ratio[0,1] }
  score          : float       # weighted sum các chiều (hiển thị/gate)
  blind_spots    : [facet]     # cái BIẾT là mình CHƯA biết (first-class)
  last_assessed  : timestamp
}
```

| Chiều hiểu biết | "Hiểu" nghĩa là | Nguồn |
|---|---|---|
| **Topology** | biết entity + quan hệ (Service→DB→Queue) | Digital Twin |
| **Application** | biết app làm gì, API, contract | discovery + OpenAPI |
| **Data** | biết DB/cache/queue, schema, flow | Twin + runtime |
| **People/Ownership** | biết ai sở hữu/SME gì | Organization Model (people graph) |
| **History/Change** | biết đã thay đổi gì, khi nào | Temporal + change log |
| **Runbook/Procedure** | biết cách vận hành/khắc phục | Procedural memory |
| **Business** | biết entity phục vụ business gì, criticality | doc + human |
| **Security posture** | biết exposure, RBAC, secrets, certs | security discovery |

**Nguyên tắc**:
- Understanding(scope) = Σ `weight_dim × coverage_dim × verified_ratio_dim`. Chưa verify → không tính đủ (nối verify-before-believe).
- **blind_spots first-class**: hệ thống biết rõ "tôi CHƯA hiểu Backup của Payment" → sinh mission/câu hỏi. Đây là phần phân biệt "Senior 5 năm" (biết mình chưa biết gì) với "Senior mới" (tưởng đã hiểu).
- Understanding feed thẳng **Authority** (Organization §2) và **Capability.K dimension** (Capability Model). Hiểu kém → không được tự chủ scope đó, dù Skill cao.
- Per-scope: hiểu Payment ≠ hiểu cả tenant. Onboarding Playbook đẩy Understanding mọi CRITICAL scope tới ngưỡng.

3-gate: G1 ✅ (Understanding chưa có ontology, chỉ là từ), G2 ✅ (in: Twin/people/temporal coverage; out: score + blind_spots), G3 ◑ (không phải store mới — là **phép đo dẫn xuất** trên Knowledge Graph + Competency Matrix; sống ở Knowledge Model).

---

## Re-ranking TÀI SẢN (CTO chốt)

```
#1  Collective Experience   — nâng MỌI khách (ẩn danh). Tài sản chiến lược.
#2  Decision Graph          — muscle memory, nhưng chỉ giúp chính tenant đó.
#3  Reasoning Playbook      — cây giả thuyết (nghĩ gì).
#4  Operating Playbook      — checklist (làm gì).
```

Lý do đảo (Decision Graph #1 → #2): Decision Graph của khách A chỉ giúp khách A. Collective Experience giúp toàn bộ khách hàng → đòn bẩy lớn nhất, là thứ khiến sản phẩm "càng nhiều khách càng giỏi".

---

## Đổi VISION: AIIP → **AOIP** (CTO chốt)

```
Cũ:  Autonomous Infrastructure Intelligence Platform (hẹp — chỉ infra)
Mới: Autonomous OPERATIONS Intelligence Platform (AOIP)
```

AI cuối cùng hiểu: Infrastructure + Application + Business + Organization + Security + Compliance + Change + People.
→ **SRE chỉ là chuyên ngành (discipline) ĐẦU TIÊN.** Nền tảng (Operating/Cognitive/Knowledge/Learning Model) không đổi khi thêm:
`Platform Engineering · DevOps · FinOps · SecOps · ComplianceOps · DataOps`.

→ Hệ quả kiến trúc: Playbook/Reasoning Playbook/Competency Matrix phải **discipline-pluggable** (SRE là bộ playbook đầu tiên, không hardcode).

---

## Stack 8 tầng — bản đồ tài liệu (cập nhật)

| Tầng | Tài liệu | Trạng thái |
|---|---|---|
| 1. Vision | memory `project_autonomous_sre_vision_v2` | ✅ (AOIP) |
| 2. Operating Model | `OPERATING_MODEL_sre.md` | ✅ |
| 3. Cognitive Model | `COGNITIVE_MODEL_sre.md` (+Meta-Cognition: self-critique→re-plan, nên bổ sung) | ✅ ◑ |
| 4. **Knowledge Model** | `KNOWLEDGE_MODEL_sre.md` (này) | ✅ MỚI |
| 5. **Learning Model** | *(chưa làm)* — Experience→Reflection→Pattern→Generalize→Validate→Publish; Skill Growth; Playbook/Reasoning/Policy Evolution | ⬜ TẦNG KẾ |
| 6. Domain Model | `DOMAIN_MODEL_autonomous_sre.md` (đảo xuống dưới Knowledge/Learning) | ✅ |
| 7. Architecture | `ASSESSMENT_autonomous_sre_v2.md` | ◑ |
| 8. Implementation | — | ⬜ chưa cho phép |

---

## Việc còn nợ (đề xuất thứ tự)

1. **Learning Model (tầng 5)** — câu hỏi "AI giỏi lên thế nào": Skill Growth, Knowledge Consolidation, Playbook/Reasoning/Policy Evolution, pipeline promotion (Q6 ở đây mới là cửa vào).
2. **Meta-Cognition** bổ sung vào Cognitive Model: `Reason→Self-Critique→Re-plan→Reflect` (bias check, "đã thử hướng khác chưa?", "confidence quá cao?"). — chạy 3-gate: G1 ✅ (chưa có vòng tự phản biện), G3 = mở rộng Cognitive, KHÔNG phải tầng riêng → fold vào tầng 3.
3. **Knowledge Model v2**: chuẩn hóa graph schema (node/edge types) khi sang Architecture.
