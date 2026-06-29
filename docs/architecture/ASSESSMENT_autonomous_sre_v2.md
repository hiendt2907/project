# Architecture Assessment — Autonomous SRE Framework (V2)

> **Status**: ASSESSMENT ONLY — không refactor, không đổi code.
> **Created**: 2026-06-29
> **Author**: Chief Software Architect (analysis pass)
> **Scope**: So sánh kiến trúc Omni hiện tại với vision "Autonomous SRE thay thế Senior SRE team".

---

## 0. TL;DR

Omni hiện tại là một **reactive incident-remediation pipeline** rất trưởng thành (4 lane chẩn đoán, RAG→LLM→CRAT→HITL→executor, audit hash-chain, autonomy tier, Telegram VI). Vision mới đòi hỏi một **lifecycle-driven SRE worker** (Phase 0→4) với mental-model "nhân viên SRE mới tiếp nhận hệ thống".

**Kết luận cốt lõi**: ~70% nền tảng có thể giữ nguyên. Khoảng cách lớn nhất KHÔNG phải reasoning/execution (đã tốt) mà là **tầng tri thức (knowledge layer)**:

1. **Không có Knowledge Graph** — tri thức nằm rời rạc ở RAG vector + Redis key, không có quan hệ first-class (Customer→Host→Service→API→DB→Owner). Đây là gap #1.
2. **Onboarding mới ở mức sơ khai** — discovery có, nhưng chưa có vòng "đọc doc → verify fact → hỏi human → cập nhật understanding → biết mình KHÔNG biết gì". Đây là gap #2.
3. **Remote Agent vẫn là collector**, chưa phải "SRE worker" (chưa có verify-before-believe, confidence per-fact, local reasoning). Gap #3.

KHÔNG cần rewrite. Cần **bổ sung một Knowledge/Twin plane** lên trên pipeline hiện có, và **nâng cấp Remote Agent + Onboarding** theo lifecycle.

---

## 1. Phần kiến trúc ĐÃ KHỚP vision (giữ nguyên)

| Vùng | Hiện trạng | Khớp nguyên tắc |
|---|---|---|
| Reasoning plane (analyst) | RAG gate (deterministic) → LLM (last resort) → AnalystAdvisory schema | ✅ "LLM is last, not first"; "không nhét business logic vào prompt" — `diagnostic_policy.py` giữ invariant bằng code |
| Execution plane | executor tách biệt, MUTATE_TOOL_ALLOWLIST, kill-switch fail-closed, post-mutate verify | ✅ "mutations only via executor", explainable, RBAC |
| CRAT audit | SHA-256 hash-chain + Ed25519, fail-closed trước mọi emit | ✅ "every action explainable", regulatory |
| Event model | Kafka split topics (alerts/evidence/actions/feedback/audit/knowledge) | ✅ "everything is an event" — đã event-driven thật |
| Autonomy tier | shadow/minimal/autonomous, Redis>PG>env, effective=min(tier, confidence) | ✅ graduated autonomy — đúng hướng "replace myself dần dần" |
| Verify loops | `post_mutate_sdk_verify`, `alert_sdk_truth_compare` (alert claim vs SDK ground truth) | ✅ "verify before believe" — đã có mầm mống ở phía diagnosis |
| Knowledge routing | `INV_KNOWLEDGE_NOT_ALERT`: non-ANOMALY tách khỏi diagnostic pipeline | ✅ tách "học" khỏi "chữa cháy" — đúng mental model |
| Confidence→autonomy | `remote_host_baseline.py` ConfidenceLevel + decay | ✅ "knowledge có confidence", evolves over time |

**Đây là tài sản. Vision message #2 nói đúng: assume valuable unless proven otherwise.**

---

## 2. Phần XUNG ĐỘT với vision

| Xung đột | Mô tả | Mức |
|---|---|---|
| **C1 — Knowledge phi-graph** | Tri thức = RAG vectors (9 collections) + Redis keys phẳng. Không có node/edge, không truy được "API X phụ thuộc DB Y thuộc owner Z". Vision đòi GRAPH FIRST. | CAO |
| **C2 — Onboarding ≠ "Senior SRE mới"** | `pkg/onboarding/discovery_doc.py` + collectors chỉ snapshot trạng thái. Thiếu vòng lặp: hypothesis→verify→ask-human→knowledge update; thiếu "biết mình không biết". | CAO |
| **C3 — Remote Agent = collector** | `agent.py` chạy fixed lanes mỗi 60s, đẩy evidence. Không reason cục bộ, không verify trước khi tin doc/config, không sinh câu hỏi "không lười". | TRUNG BÌNH |
| **C4 — Discovery hardcode, chưa plugin** | Collectors là module cố định (mysql/proxysql/haproxy/k8s...). Vision đòi "plugins over hardcode" cho từng vendor (AWS/Azure/VMware/Confluence/ArgoCD...). | TRUNG BÌNH |
| **C5 — Lifecycle ngầm định** | Code xoay quanh "alert→remediation". Vision xoay quanh "customer lifecycle Phase 0–4". Phase 1 (onboarding) và Phase 3 (continuous improvement) gần như chưa có khung. | TRUNG BÌNH |
| **C6 — Data residency vs Twin** | Vision: Omni chỉ giữ experience/patterns, KHÔNG giữ infra khách (`INV_DATA_RESIDENCY` — doc chỉ metadata). Nhưng "Digital Twin" cần một bản đồ topology sống. → Twin phải sống **ở phía khách hàng/agent**, Omni chỉ giữ reference + experience. Hiện chưa phân định rõ ranh giới này. | CAO (thiết kế) |

---

## 3. Phần GIỮ NGUYÊN (không động tới)

- `services/audit_ledger/` (CRAT) — hoàn chỉnh, regulatory-grade.
- `pkg/reasoning/` schema + `diagnostic_policy.py` invariants — đây là "business logic ngoài prompt", đúng vision.
- Kafka transport (`messaging/kafka_bus.py`) + topic map.
- Execution plane (`execution/`, `pkg/executor/`, executor role).
- Autonomy tier machinery (PG `omni_admin`, resolve_tier cache).
- smart-siem Go services (brain-go/agent/bff) — pipeline song song độc lập, không cản vision.

---

## 4. Phần cần TIẾN HÓA (evolve, không thay)

| Thành phần | Tiến hóa thành |
|---|---|
| RAG vector store (`rag/`) | Vẫn giữ cho similarity recall, **nhưng** trở thành "index phụ" cạnh Knowledge Graph (graph là source-of-truth quan hệ, vector là recall). |
| Remote Agent (`remote_agent/`) | Từ collector → **SRE worker**: thêm local reasoning nhẹ, verify-before-believe, per-fact confidence, sinh câu hỏi giàu ngữ cảnh. |
| `pkg/onboarding/` | Từ snapshot → **Onboarding state machine** (Phase 1): đọc doc → dựng hypothesis → verify → hỏi human → cập nhật graph → đo "độ hiểu". |
| `knowledge_pipeline.py` | Từ dispatcher Redis → **graph upsert**: METRIC/LOG/DISCOVERY/CHANGE → node/edge mutation trên twin. |
| ConfidenceLevel (per-host) | Mở rộng thành **confidence per-fact/per-edge** trên graph, không chỉ per-host. |
| `proactive_observer` | Hạt giống của Phase 3 (Continuous Improvement) — mở rộng từ "fix anomaly" sang "đề xuất cải tiến HA/cost/coverage". |

---

## 5. Subsystem MỚI cần có

1. **Knowledge Graph plane** (gap #1) — node types: Customer/Site/Cluster/Namespace/Node/Pod/Service/API/DB/Topic/Bucket/Firewall/Owner/BusinessCapability/Runbook/Incident/Change. Edge first-class. Lưu **per-tenant ở phía khách** (residency), Omni giữ reference + schema.
2. **Digital Twin sync** — incremental sync từ agent → graph (Principle: no full scans, incremental). Reality thay đổi → twin thay đổi.
3. **Onboarding engine** (Phase 1) — vòng observe→hypothesize→verify→ask→learn, với metric "coverage / unknown count".
4. **Human-learning loop** — câu hỏi "không lười" (đã-biết + hiểu-hiện-tại + bất-định + câu-hỏi-chính-xác); answer → graph update; never ask twice.
5. **Discovery plugin registry** — mỗi capability (AWS/VMware/Confluence/ArgoCD/Prometheus/DNS...) là plugin theo 1 interface, thay cho collector hardcode.
6. **Continuous Improvement engine** (Phase 3) — proactive review (architecture/capacity/security/cost/coverage gaps).
7. **Experience store tách khỏi customer knowledge** (Phase 4) — Omni global chỉ chứa reasoning patterns/playbooks/lessons; customer infra ở lại tenant.

---

## 6. Abstraction hiện SAI / lệch

- **"Evidence" gánh quá nhiều vai**: vừa là alert proof, vừa là knowledge sample, vừa là discovery. Đã tách topic nhưng cùng một envelope shape — nên tách contract: `DiagnosticEvidence` vs `KnowledgeObservation` vs `TopologyFact`.
- **Collector = capability** bị trộn: collector vừa thu thập vừa quyết định criticality. Nên tách "discover" (plugin) khỏi "judge criticality" (reasoning).
- **Confidence chỉ per-host**: tri thức thực tế có độ tin khác nhau theo từng fact (doc nói A, config nói B). Cần per-fact.
- **Onboarding là worker role phụ** (`omni-onboarding` pod): vision coi onboarding là Phase QUAN TRỌNG NHẤT — nên là first-class engine, không phải side worker.

---

## 7. Folder structure — còn phù hợp không?

**Phần lớn còn tốt** (đã chia theo domain: workers/gateway/remote_agent/services/rag/pkg). KHÔNG cần đại phẫu như "Constitution V1" (control-plane/discovery-plane/... 17 thư mục) — đó là over-engineering với codebase đã chạy.

Đề xuất bổ sung **tối thiểu**, không phá vỡ:
- `src/graph/` — knowledge graph model + store (mới).
- `src/discovery/plugins/` — di chuyển dần `remote_agent/collectors/` về interface plugin (giữ backward-compat).
- `src/services/onboarding_engine/` — nâng `pkg/onboarding` thành engine.
- Giữ nguyên mọi thứ khác.

→ **Evolution, không revolution.** Mâu thuẫn giữa hai vision message (V1 đòi đập đi xây lại 17-plane; V2 nói "đừng rewrite") — chọn V2.

---

## 8. Event model — còn phù hợp không?

✅ **Có, rất phù hợp.** Kafka split-topic + envelope trace_id là nền event-driven tốt. Chỉ cần thêm event types mới (graph mutation, twin-diff, onboarding-question, fact-verified) — không đổi transport. `kafka_knowledge_evidence_loop` đã là chỗ neo cho graph upsert.

---

## 9. Planner / Policy / Worker — có cần tiến hóa?

- **Planner** (analyst_agentic_loop, ReAct) → nâng từ "plan remediation" sang "plan mission" (Chief SRE giao mission cho agent). Giữ cơ chế, mở rộng scope.
- **Policy** (diagnostic_policy, env_mode, autonomy tier) → giữ; thêm policy "fact phải verified mới được dùng để mutate".
- **Worker = Remote Agent** → tiến hóa mạnh nhất (xem §4). Omni = Chief (plan/reason/reflect/learn); Agent = field SRE (discover/observe/execute/verify/ask/report). Phân vai này ĐÚNG và codebase đã gần — chỉ cần dịch chuyển reasoning nhẹ xuống agent và mission-planning lên Omni.

---

## 10. Knowledge Graph — có cần không, ở đâu?

**CẦN — đây là trục xương sống của vision** (digital twin, dependency, "hiểu WHY không chỉ WHAT").

Vị trí (giải quyết xung đột residency C6):
- **Graph dữ liệu khách (twin thật)**: sống **per-tenant ở phía khách hàng** (cạnh agent / trong cluster khách). Chứa node/edge thật.
- **Omni global**: chỉ giữ **graph schema + reference + experience patterns** (anonymized). KHÔNG copy infra khách lên Omni.
- Vector RAG → demote thành index recall cạnh graph.

Công nghệ: bắt đầu nhẹ (graph-in-Redis hoặc networkx + persist) trước khi cân nhắc Neo4j — tránh hạ tầng nặng sớm.

---

## 11. Remote Agent tiến hóa từ hôm nay thế nào

Hiện tại (`agent.py`): discovery on-start → derive collectors → loop 60s đẩy evidence + poll command. Tốt làm nền.

Lộ trình tiến hóa (giữ loop, thêm năng lực):
1. **Verify-before-believe**: khi đọc doc/config, không tin ngay → đối chiếu runtime (port thật mở? service thật chạy?) → gắn confidence per-fact.
2. **Local triage nhẹ**: agent tự phân loại "biết / nghi ngờ / không biết" trước khi đẩy lên.
3. **Question generation**: khi gặp UNKNOWN_ENTITY → sinh câu hỏi giàu ngữ cảnh (đã-biết + hiểu + bất-định + hỏi).
4. **Plugin collectors**: chuyển `collectors/` sang interface plugin để thêm vendor không sửa core.
5. **Twin reporter**: emit TopologyFact (node/edge) thay vì chỉ raw evidence.

---

## 12. Migration strategy (an toàn, incremental)

- **Strangler pattern**: Graph plane chạy SONG SONG, đọc từ `omni-knowledge-evidence` đang có. Không đụng diagnostic pipeline.
- **Dual-write**: knowledge_pipeline vừa giữ Redis key cũ vừa upsert graph → so sánh → cutover khi graph đủ tin.
- **Contract-first**: định nghĩa `TopologyFact` / graph schema trước, agent emit dần.
- **Onboarding engine** dựng cạnh worker `onboarding` hiện có, không thay.
- KHÔNG đụng CRAT / executor / autonomy tier trong giai đoạn đầu.

---

## 13. Rủi ro

| Rủi ro | Mức | Giảm thiểu |
|---|---|---|
| Data residency: graph vô tình copy infra khách lên Omni | CAO | Enforce `INV_DATA_RESIDENCY` ở graph store; twin sống phía khách |
| Over-engineering theo Constitution V1 (17-plane) | CAO | Bám V2: evolve, đo bằng giá trị thật |
| LLM quality (qwen2.5-coder:7b 7B) cho mission-planning | TRUNG BÌNH | Giữ deterministic-first; LLM chỉ khi RAG miss (carry-over F28/F31 model-ceiling) |
| Graph + vector double source-of-truth lệch nhau | TRUNG BÌNH | Graph = truth quan hệ, vector = recall; one-way sync |
| Onboarding hỏi human quá nhiều → mệt mỏi | TRUNG BÌNH | "never ask twice", batch câu hỏi, ưu tiên theo criticality |
| Agent local reasoning làm nặng host khách | THẤP | Giữ triage nhẹ, reasoning nặng vẫn ở Omni |

---

## 14. Roadmap đề xuất (theo lifecycle, không theo plane)

- **R0 — Contract & Graph schema** (nền): định nghĩa node/edge, `TopologyFact`, residency boundary. Không code pipeline.
- **R1 — Twin plane (read-only)**: graph store + upsert từ `omni-knowledge-evidence` (dual-write, strangler). Visualize twin.
- **R2 — Remote Agent → SRE worker**: verify-before-believe + per-fact confidence + plugin collectors.
- **R3 — Onboarding engine (Phase 1)**: observe→hypothesize→verify→ask-human→learn; metric coverage/unknown.
- **R4 — Human-learning loop**: câu hỏi không-lười + answer→graph; never-ask-twice.
- **R5 — Mission planner (Phase 2 nâng cấp)**: Chief SRE giao mission, agent thực thi + report.
- **R6 — Continuous Improvement (Phase 3)**: proactive review (HA/cost/capacity/coverage).
- **R7 — Experience store (Phase 4)**: tách global patterns khỏi customer knowledge; reflection loop.

Mỗi R đứng độc lập, có giá trị riêng, không bắt buộc làm hết.

---

## 15. Nguyên tắc chỉ đạo (chốt mâu thuẫn V1 vs V2)

> Vision message #1 (Constitution V2 "đập đi xây lại") và message #2 ("đừng rewrite, hiểu trước") mâu thuẫn về **mức độ**.
> **Chọn message #2 làm kim chỉ nam**: Omni đã là production system trưởng thành. Tối ưu cho 10 năm tới = **bổ sung knowledge/twin plane đúng + nâng cấp agent theo lifecycle**, KHÔNG phá nền pipeline/CRAT/executor đang chạy tốt.
> "Never optimize bad architecture, replace it" chỉ áp dụng cho **tầng knowledge phi-graph (C1)** và **onboarding sơ khai (C2)** — đó là chỗ thật sự cần xây mới.
