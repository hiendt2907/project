# Domain Model — Autonomous SRE Framework

> **Status**: DESIGN ONLY — không code, không refactor. Đây là "trái tim" dự án.
> **Created**: 2026-06-29
> **Supersedes**: phần "Phase 0–4 lifecycle" và "Onboarding Engine" trong `ASSESSMENT_autonomous_sre_v2.md`.
> **Nguyên tắc chỉ đạo**: Khi Domain Model đủ tốt, code chỉ là hiện thực hóa các khái niệm.

---

## 0. Dịch chuyển paradigm (chốt từ feedback của sếp)

| Sai (assessment cũ) | Đúng (model này) |
|---|---|
| Agent = Collector++ | **Agent = nhân viên SRE (identity)**. Collector chỉ là một *tool* trong tay agent. |
| Onboarding Engine | **Mission Engine**. Onboarding chỉ là *mission đầu tiên*. |
| Phase 0→1→2→3→4 (lifecycle theo thời gian) | **Mission Lifecycle** (mọi việc đều là mission, khác Goal). |
| Chỉ có Mission | Mission **+ Role + Skill**. Agent đổi Persona liên tục. |
| Chỉ có Knowledge | Knowledge **≠** Experience. Và Observation→Hypothesis→Verified Fact. |

**Mental model**: Senior SRE không nghĩ "collect metrics → collect logs → done". Anh ta nghĩ
`Mission → hiểu hệ thống → thiếu gì? → tìm → verify → hỏi → hiểu`.

**Kiến trúc 4 tầng**:

```
                         OMNI (Brain / Chief SRE)
  Mission Planning · Reasoning · ReAct · Reflection · Experience · Learning · Policy
────────────────────────────────────────────────────────────────────────────────
            CONTRACT LAYER (ngôn ngữ chung Omni ↔ Agent)
        Mission · Role · Skill Requirement · Priority · Knowledge Gap
────────────────────────────────────────────────────────────────────────────────
                    REMOTE AGENT (Worker / field SRE)
        Observe · Discover · Verify · Ask Human · Execute · Report · Learn
────────────────────────────────────────────────────────────────────────────────
                              PLUGINS (tools)
   SSH · Docker · Kubernetes · AWS · VMware · GitLab · Prometheus · Grafana ·
   Confluence · Jira · LDAP · DNS · Firewall · ...
```

---

## Q1. Mission là gì? Lifecycle ra sao?

**Mission** = đơn vị công việc có mục tiêu (Goal), được giao cho một Agent đóng một Role. Mọi loại việc đều là Mission — chỉ khác Goal. **Một engine duy nhất chạy mọi mission.**

```
Mission
  id, tenant_id, parent_mission_id?        # mission có thể đẻ sub-mission
  goal: str                                # "Hiểu Payment System"
  role: RoleRef                            # persona bắt buộc (Q2)
  skill_requirements: list[SkillReq]       # (domain, min_level)  (Q3)
  required_capabilities: list[Capability]  # access cần có: ssh:host-x, kubeconfig:cluster-y
  scope: ScopeRef                          # target: host / service / namespace / business-cap
  priority: int                            # do Planner đặt (Q7)
  knowledge_gap: KnowledgeGapRef?          # cái KHÔNG biết đã sinh ra mission này
  success_criteria: SuccessCriteria        # khi nào COMPLETE (Q8)
  state: MissionState
  assigned_agent_id?: str
```

**Mission types = template của Goal + Role + SuccessCriteria** (KHÔNG phải engine riêng):

| Mission | Goal | Role mặc định |
|---|---|---|
| Onboarding | Hiểu hệ thống | OnboardingEngineer |
| Incident | Root Cause | IncidentCommander |
| Architecture Review | Improve HA | InfrastructureArchitect |
| Documentation | Update Wiki | TechnicalWriter |
| Security Audit | Review Firewall | SecurityAuditor |
| Performance Review | Review Redis | PerformanceEngineer |
| Capacity Planning | Forecast growth | PerformanceEngineer |
| Backup Verification | Verify restore | InfrastructureArchitect |

**Mission Lifecycle** (state machine — *outer*):

```
CREATED → PLANNED → ASSIGNED → IN_PROGRESS ──► COMPLETED ──► ARCHIVED
                                    │  ▲              ▲          (experience extracted)
                                    ▼  │              │
                                BLOCKED(WaitingHuman)─┘
                                    │
                                    ▼
                              FAILED / ABANDONED
```

**Inner loop trong IN_PROGRESS** (đây là cái sếp vẽ — mọi mission giống nhau):

```
Plan ─► Execute ─► Evidence ─► Reason ─► Verify ─┐
  ▲                                              │
  └──────────── (chưa đủ / contradiction) ───────┘
                         │ (đủ + verified)
                         ▼
                    Complete ─► Experience
```

- **Plan**: Agent (hoặc Omni) phân rã goal thành các bước (cần biết gì → tìm ở đâu).
- **Execute**: gọi plugin/tool (đây là chỗ "collect" sống — chỉ là một bước).
- **Evidence**: thu observation thô.
- **Reason**: biến observation → hypothesis (Q4); xác định còn thiếu gì.
- **Verify**: đối chiếu độc lập / hỏi human → hypothesis thành verified fact.
- **Complete**: success_criteria đạt.
- **Experience**: trích pattern/lesson (anonymized) về Omni global (Q4).

Mọi mission (Onboarding, Incident, Review...) **dùng đúng vòng này**. Khác nhau ở Goal + Role + SuccessCriteria + Plugin được phép.

---

## Q2. Role là gì? Agent đổi vai trò ra sao?

**Role/Persona** = lăng kính tư duy cho một mission. Role KHÔNG phải identity của agent — nó là *cái mũ* agent đội trong thời gian chạy mission. Một agent đội nhiều mũ theo thời gian.

```
Role
  name: str                       # IncidentCommander, SecurityAuditor...
  allowed_plugins: set[PluginId]  # capability scope (Security chỉ được đọc firewall/LDAP...)
  policy_posture: Posture         # READ_ONLY | SUGGEST | MUTATE_ALLOWED
  reasoning_persona: PromptFrag   # mảnh prompt định hướng (business logic vẫn ở code, không ở prompt)
  definition_of_done: DoDTemplate # "done" nghĩa là gì cho role này
  knowledge_domains: list[str]    # domain tri thức role này tra cứu
  risk_tolerance: RiskLevel
```

**Quan hệ Role → Mission** (đúng như sếp vẽ):

```
Role InfrastructureArchitect → Mission Review HA
Role TechnicalWriter         → Mission Update Wiki
Role IncidentCommander       → Mission Investigate Alert
Role SecurityAuditor         → Mission Review Firewall
Role PerformanceEngineer     → Mission Review Redis
```

**Cách đổi Role**: Agent nhận mission → adopt `mission.role` → load (allowed_plugins, posture, persona, DoD) → chạy inner loop dưới ràng buộc role đó → mission xong → trả mũ về Idle (không role). Một agent đang đội mũ IncidentCommander có thể bị giao mission khác sau đó đội mũ SecurityAuditor.

→ **Role = runtime constraint + lens; Identity = agent có skill.** Tách bạch hai cái này là điểm mấu chốt.

---

## Q3. Skill/Capability biểu diễn & đánh giá thế nào?

Hai khái niệm KHÁC nhau:

- **Capability** = *có quyền truy cập/vận hành* X trong môi trường này (nhị phân). VD: `ssh:prod-web-01`, `kubeconfig:cluster-a`, `aws:role-readonly`. Không có capability → không thể nhận mission cần nó (hard gate).
- **Skill** = *giỏi tới đâu* ở một domain (0–100, liên tục). VD: Linux 92, MySQL 65, Kubernetes 35.

```
SkillVector  = { domain: SkillScore }      # mỗi agent có một vector
SkillScore   = level(0-100) + sample_count + last_used_at + decay
SkillReq     = (domain, min_level)         # mission yêu cầu
Capability   = (plugin/resource, access_proof)
```

**Skill được EARNED, không declared** — tăng theo:
- Mission cùng domain hoàn thành thành công (+).
- Verify pass rate (hypothesis của agent về sau được xác nhận đúng) (+).
- Human approval rate cho action/advisory của agent (+).
- Decay theo thời gian không dùng (giống `remote_host_baseline.decay_confidence`).

→ Tái dùng cơ chế `ConfidenceLevel` hiện có nhưng **tổng quát hóa**: từ "confidence per-host" → "skill per-(agent,domain)" + "understanding-confidence per-scope" (Q8).

**Vì sao quan trọng**: khi có nhiều loại agent (Linux/K8s/Windows/DB Agent), Omni Planner dùng SkillVector + Capability để *biết giao mission cho ai* (Q7).

---

## Q4. Knowledge ≠ Experience. Observation→Hypothesis→Verified Fact

### Knowledge vs Experience (khác biệt cốt tử)

| | Knowledge | Experience |
|---|---|---|
| Là gì | Fact về hệ thống KHÁCH HÀNG này | Pattern/lesson về CÁCH làm SRE |
| Ví dụ | "payment-svc → mysql-01, owner=team-pay, critical" | "JVM service CPU 3σ → check GC trước khi scale" |
| Scope | Per-tenant (dữ liệu khách) | Global Omni, anonymized, cross-tenant |
| Sống ở đâu | Phía khách (Digital Twin, Q5) | Omni (experience/playbook store) |
| Dùng để | Reason về hệ thống này | Reason về cách giải quyết mọi hệ thống |

→ Đây là cách thỏa `INV_DATA_RESIDENCY`: **Knowledge ở lại khách; chỉ Experience (đã trừu tượng hóa) lên Omni.**

### Observation → Hypothesis → Verified Fact (vòng tiến hóa tri thức)

```
Observation     raw, vừa thấy            "port 8443 mở trên host-x"        no confidence
    │  Reason
    ▼
Hypothesis      diễn giải, chưa chắc     "8443 ~ internal auth service"    confidence + source(doc/llm/infer)
    │  Verify (đối chiếu độc lập / hỏi human)
    ▼
Verified Fact   đã xác nhận              "8443 = Keycloak (admin xác nhận   confidence=high
                                          + TLS CN match)"                  + provenance + last_verified_at
```

```
Observation { signal, source_plugin, ts }                         # transient
Hypothesis  { claim, confidence, evidence[], source, status }     # pending, có thể bị bác
VerifiedFact{ claim, confidence, provenance[], last_verified_at } # vào Twin làm node/edge
```

**Nguyên tắc**: Twin chỉ commit **VerifiedFact** thành node/edge "cứng". Hypothesis treo ở vùng "đang nghi". `last_verified_at` decay → fact cũ phải re-verify (twin sống). Contradiction (doc nói A, runtime nói B) → sinh mission verify / câu hỏi human.

**"Verify before believe"** = quy tắc bắt buộc: doc/config KHÔNG tự động thành fact; phải qua Verify.

---

## Q5. Digital Twin sống ở đâu (không vi phạm residency)?

**Tách Twin làm 2 lớp:**

```
┌─ Phía KHÁCH HÀNG (tenant-local, cạnh agent / trong cluster khách) ─┐
│  TWIN DETAIL  — graph đầy đủ: node + edge + VALUE nhạy cảm          │
│  (config thật, IP, secret ref, doc content, owner PII...)          │
│  = "Knowledge" thật. KHÔNG rời khỏi khách.                          │
└────────────────────────────────────────────────────────────────────┘
                          │ chỉ đẩy lên ▲ (structure, không value)
┌─ Phía OMNI (global) ────────────────────────────────────────────────┐
│  TOPOLOGY SKELETON — graph cấu trúc ẩn danh:                         │
│  node TYPES + edge TYPES + ids/hash + criticality + confidence       │
│  ĐỦ để mission-plan & reason về topology, KHÔNG có value nhạy cảm.    │
│  + EXPERIENCE store (patterns, playbooks, lessons) — anonymized.     │
└──────────────────────────────────────────────────────────────────────┘
```

- Omni plan & reason trên **skeleton + experience**. Khi cần chi tiết → ra mission/command để agent **query twin local**, trả lời cục bộ, chỉ gửi kết luận tối thiểu.
- Thỏa `INV_DATA_RESIDENCY` / `INV_DOC_RESIDENCY`: value khách không bao giờ nằm trên Omni.
- Công nghệ: bắt đầu nhẹ (graph-in-Redis hoặc networkx persist phía agent) trước khi cân nhắc Neo4j.

---

## Q6. Agent có những trạng thái hoạt động nào?

State của **agent** (hoạt động hiện tại) — tách khỏi state của **mission** (Q1):

```
REGISTERING → IDLE ──► DISCOVERING ──► INVESTIGATING ──► EXECUTING ──► VERIFYING
                ▲           │                │              │              │
                │           ▼                ▼              ▼              ▼
                │      WAITING_HUMAN ◄────────────────────────────────────┘
                │           │
                │           ▼
                └──── REPORTING ──► LEARNING ──► IDLE
                          (OFFLINE / DEGRADED có thể xảy ra ở bất kỳ đâu)
```

| State | Ý nghĩa |
|---|---|
| REGISTERING | join tenant, khai báo SkillVector + Capability (Phase 0 cũ → chỉ là khởi động) |
| IDLE | chờ mission |
| DISCOVERING | quét/khám phá scope (tool: collector) |
| INVESTIGATING | reason, dựng hypothesis, tìm cái thiếu |
| WAITING_HUMAN | đã hỏi câu hỏi không-lười, chờ trả lời |
| EXECUTING | thực thi action (under role posture + policy) |
| VERIFYING | đối chiếu kết quả / xác nhận fact |
| REPORTING | đẩy kết quả + skeleton update về Omni |
| LEARNING | cập nhật skill cục bộ, đóng góp experience |
| OFFLINE/DEGRADED | mất kết nối → confidence/skill decay |

→ "Idle, Discovering, Investigating, Waiting Human, Executing, Learning" mà sếp nêu = đúng tập này.

---

## Q7. Mission Planner giao việc theo cơ chế nào?

Planner (ở Omni) match **Mission** ↔ **Agent** qua nhiều tầng gate:

```
1. CAPABILITY GATE (hard)  : agent phải có required_capabilities tới mission.scope
                             (không có ssh/kubeconfig tới scope → loại ngay)
2. ROLE COMPATIBILITY      : agent có thể đội mission.role? (plugin role cần ∈ capability agent)
3. SKILL FIT (score)       : SkillVector agent vs skill_requirements (đủ min_level? dư bao nhiêu?)
4. DATA/CONFIDENCE         : understanding-confidence của scope (đủ để autonomous? hay cần human?)
5. LOCATION/REACHABILITY   : agent ở đúng mạng/host chạm được scope
6. LOAD/STATE              : agent IDLE? đang quá tải?
   ───────────────────────────────────────────────
   → score tổng + mission.priority + urgency(knowledge_gap) → chọn agent tốt nhất
```

- **Effective autonomy** cho action trong mission = `min(tenant_tier, understanding_confidence(scope), role.policy_posture)`. Mở rộng `effective = min(tenant_tier, confidence)` hiện có.
- Priority queue: mission do `knowledge_gap` critical (VD critical service chưa hiểu) hoặc incident → ưu tiên cao.
- Nếu KHÔNG agent nào đủ skill → Omni vẫn giao cho agent gần nhất + **tự bù reasoning** (Chief SRE kèm lính mới), đồng thời mission đó làm skill tăng (learning).

---

## Q8. Khi nào Mission COMPLETE? Đo "Agent đã hiểu hệ thống" thế nào?

**Mission complete** khi: `success_criteria` đạt **AND** Verify pass **AND** không còn open hypothesis chặn (hoặc đã được human chấp nhận).

`success_criteria` khác theo mission:
- Incident → root cause là VerifiedFact + remediation verified.
- Architecture Review → đủ N đề xuất HA có bằng chứng.
- Documentation → wiki cập nhật + human approve.

**Đo "đã hiểu hệ thống" (cho Onboarding & nói chung)** = **Understanding Score per scope** (0–100), tổng hợp đo được:

```
UnderstandingScore(scope) = weighted(
    coverage          : % node-type kỳ vọng đã discover (server/service/DB/dep/owner/runbook)
    verification_ratio: verified_facts / (verified + hypothesis + unknown)
    criticality_cover : % CRITICAL service đã verified đầy đủ (deps+owner+runbook+SLA)
    contradiction_count → 0
    unknown_count     → ổn định/giảm
)
```

- **Onboarding mission DONE** khi: `UnderstandingScore(scope) ≥ threshold` **AND** mọi CRITICAL service có {owner, deps, criticality, runbook} verified **AND** open critical unknown = 0.
- "Hiểu" = đo được, không cảm tính. Score này cũng feed Q3 (skill) và Q7 (autonomy gate) → khép vòng.
- Tái dùng `remote_host_baseline` confidence: từ per-host → per-scope understanding.

---

## Ánh xạ sang code hiện có (không đổi gì bây giờ)

| Khái niệm mới | Tài sản hiện có để tái dùng |
|---|---|
| Mission inner loop (Plan→Execute→Reason→Verify) | `analyst_agentic_loop.py`, `autonomous_decider.py` (ReAct) — tổng quát hóa |
| Mission output (Incident) | `AnalystAdvisory` schema — 1 loại mission result |
| Verify | `post_mutate_sdk_verify.py`, `alert_sdk_truth_compare.py` |
| Observation intake | `knowledge_pipeline.py`, `omni-knowledge-evidence` |
| Skill/Understanding confidence | `anomaly/remote_host_baseline.py` ConfidenceLevel + decay |
| Experience store | RAG (`rag/`) + `execution/experience.py` + `archivist.py` |
| Plugins | `remote_agent/collectors/*` → tổng quát hóa thành plugin interface |
| Policy/posture | `diagnostic_policy.py`, autonomy tier, kill-switch |
| Twin skeleton | (MỚI) `src/graph/` — chưa tồn tại |

---

## Điểm quyết định — ĐÃ CHỐT (sếp, 2026-06-29)

1. **Skeleton vs Detail boundary** (Q5): Omni Skeleton = entity **type** + **anonymous ID** (hash, KHÔNG IP/hostname thật) + **relationship** (Service A→DB B) + **criticality** + **health/status** + **confidence** + **capability tags**. Twin DETAIL (IP/config/secret/doc/owner PII) ở lại khách. Omni biết "Payment phụ thuộc DB Primary", KHÔNG biết `10.0.0.12`.
2. **Mission đẻ sub-mission**: **CÓ, bắt buộc**. `parent_mission_id`. Mỗi sub-mission phải có Goal + DoD + Deliverable rõ ràng.
3. **Skill khởi tạo**: **Built-in Skill** (do loại agent: Linux/K8s/Windows/DB Agent) + **Learned Skill** (tích lũy qua Experience, Omni cập nhật). Không phải tờ giấy trắng — như nhân viên mới có chuyên môn nền.
4. **Mission concurrency**: **1 Mission CHÍNH + nhiều Sub-mission song song**. Không cho nhiều mission chính song song (tránh mất ngữ cảnh/ưu tiên).

> Operating Model (Playbook/Curriculum/Competency Matrix/Acquisition Order/Question Strategy) ở `OPERATING_MODEL_sre.md` — tầng trên Domain Model này.
