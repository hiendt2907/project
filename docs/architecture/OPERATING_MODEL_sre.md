# SRE Operating Model — Autonomous SRE Framework

> **Status**: DESIGN ONLY — không code, không refactor.
> **Created**: 2026-06-29
> **Quan hệ**: Tầng TRÊN của `DOMAIN_MODEL_autonomous_sre.md`.
> Domain Model = "AI **là gì**" (Mission/Role/Skill/Knowledge/Experience/Twin).
> Operating Model = "AI **làm việc như thế nào**" (Playbook/Curriculum/Competency/Acquisition Order/Question Strategy).
> **Đây là tài sản quan trọng nhất của dự án** — chuẩn hóa cách một Senior SRE tiếp nhận, vận hành, cải tiến hệ thống. Khó sao chép. Quyết định chất lượng Omni nhiều năm tới.

---

## 0. Phát hiện cốt lõi: Mission KHÔNG tự sinh

Trước đây ta để "Mission Planner chia mission theo tiêu chí". **Sai mental model.** Senior SRE không tự nghĩ ra mọi thứ — anh ta có **checklist trong đầu**.

```
Mission KHÔNG tự sinh. Mission sinh ra TỪ Playbook (Curriculum).

Playbook (Customer Onboarding)
   ├─ sinh ─► Mission 1: Inventory      → DONE ─┐
   ├─ sinh ─► Mission 2: Network        → DONE ─┤ (gated, tuần tự + song song có kiểm soát)
   ├─ sinh ─► Mission 3: Application     → DONE ─┤
   └─ ...                                        ▼
```

**Playbook mới là tài sản.** Không phải AI, không phải prompt, không phải Mission.

Kiến trúc bổ sung **một tầng nữa** lên trên Mission Planner:

```
┌──────────────────── SRE PLAYBOOK ENGINE (mới) ─────────────────────┐
│  Playbook (Curriculum) = checklist chuẩn của Senior SRE             │
│  → sinh ra chuỗi Mission có thứ tự + entry/exit gate                │
└────────────────────────────────────────────────────────────────────┘
                              │ sinh Mission
                              ▼
┌──────────────────── MISSION PLANNER (đã có ở Domain Model) ─────────┐
│  Giao Mission ↔ Agent (Capability→Role→Skill→Confidence→Load)       │
└────────────────────────────────────────────────────────────────────┘
                              │ giao
                              ▼
                        REMOTE AGENT (thực thi mission)
```

---

## Q1. Senior SRE có những Playbook chuẩn nào?

Tập Playbook chuẩn (= curriculum standardized). Mỗi Playbook có: entry-condition, chuỗi Mission có thứ tự, exit-condition (Definition of Done của cả playbook).

| Playbook | Trigger | Mục tiêu |
|---|---|---|
| **Customer Onboarding** | Agent mới join tenant | Hiểu đầy đủ hệ thống (mission đầu đời) |
| **Incident Investigation** | Anomaly/alert/SIEM | Root cause + remediation verified |
| **Architecture Review** | Định kỳ / on-demand | Cải thiện HA/độ bền |
| **Security Audit** | Định kỳ / compliance | Phát hiện lỗ hổng, drift |
| **Performance Review** | Định kỳ / degrade | Tối ưu hiệu năng (Redis/DB/JVM...) |
| **Backup Audit** | Định kỳ | Verify backup + restore thật |
| **Capacity Planning** | Định kỳ / forecast | Dự báo tăng trưởng, tài nguyên |
| **Documentation** | Sau onboarding / change | Wiki/runbook luôn cập nhật |
| **Change Validation** | Sau change/deploy | Xác nhận change an toàn |

→ Đây là "trí nhớ nghề nghiệp" của Omni. Mỗi tenant kế thừa tập playbook chuẩn, có thể tùy biến.

---

## Q2. Mỗi Playbook sinh ra Mission nào? (Curriculum + gate)

**Curriculum** = chuỗi mission có thứ tự, mỗi bước gated bởi DoD của bước trước. Đúng cách Senior nghĩ: *"chưa biết gì → phải có Inventory trước → rồi Network → rồi Service → rồi API → rồi Business"*.

### Ví dụ: Playbook **Customer Onboarding**

```
Mission: Understand System  (mission CHÍNH)
│  exit = UnderstandingComplete (Q6) cho mọi CRITICAL entity
│
├─ M1  Inventory            (hosts, VMs, containers, clusters)        [gate: phải xong trước]
├─ M2  Network & Topology   (subnet, firewall, DNS, ingress, LB)
├─ M3  Storage              (disk, NFS, bucket, volume)
├─ M4  Application/Service  (microservices, processes, units)
├─ M5  API                  (OpenAPI/Swagger, endpoints, contracts)
├─ M6  Data Layer           (DB, Redis, Kafka, RabbitMQ, Elastic)
├─ M7  Observability        (Prometheus, Grafana, logging, tracing)
├─ M8  CI/CD & Source       (Git, Jenkins, GitLab, ArgoCD, Helm, TF)
├─ M9  Security             (RBAC, secrets, certs, firewall rules)
├─ M10 Business Workflow    (service→business capability, criticality, owner)
└─ M11 Verify & Draw Twin   (đối chiếu chéo, vẽ topology, đóng dependency graph)
```

- **Thứ tự bắt buộc** ở các bước nền (M1 trước M2 trước M4...). Các bước độc lập (M3 storage, M7 observability) có thể chạy **song song** dưới mission chính (đúng câu trả lời #4 của sếp: 1 mission chính + nhiều sub-mission song song).
- Mỗi sub-mission có **Goal + DoD + Deliverable** rõ ràng (câu trả lời #2 của sếp). VD M6 Deliverable = "danh sách DB/Cache/Queue đã verified + version + role + dependency edges".
- Mission **đẻ sub-mission** khi cần (M4 → "understand payment-svc" → con của nó: discover infra/api/db/monitoring/backup + verify).

### Sub-mission chuẩn của một entity (đệ quy)

```
Understand <Entity X>
├── Discover Infrastructure
├── Discover APIs
├── Discover Database/Data deps
├── Discover Monitoring/Logging
├── Discover Backup
└── Verify Knowledge   ← bắt buộc, không có thì entity "chưa hiểu"
```

---

## Q3. Mỗi Mission cần loại Knowledge nào? → **Competency Matrix** (Definition of Understanding)

**"Hiểu" KHÔNG phải score. Là Competency Matrix.** Một entity được coi là "hiểu" khi **mọi facet bắt buộc** được điền **và verified**. Thiếu một facet → chưa hiểu (score chỉ là tổng hợp phụ trợ).

### Competency Matrix — entity type **Service** (ví dụ Payment Service)

| Facet | Bắt buộc? | Verified bằng |
|---|---|---|
| Owner | ✅ | doc/Confluence/Git CODEOWNERS + human |
| Business capability | ✅ | doc + human |
| API (endpoints, contract) | ✅ | OpenAPI/Swagger + probe thật |
| DB dependency | ✅ | config + runtime connection |
| Cache dependency | ⬜ | config + runtime |
| Queue dependency | ⬜ | config + runtime |
| Deployment (how/where) | ✅ | k8s/systemd/docker introspection |
| Monitoring coverage | ✅ | Prometheus targets/dashboards |
| Logging | ✅ | log path/pipeline thật |
| Backup | ✅ (nếu stateful) | backup job + restore test |
| Runbook | ✅ | doc store |
| SLA | ✅ | doc + human |
| Firewall/network exposure | ✅ | rule + netstat thật |
| Upstream dependency | ✅ | trace/config |
| Downstream dependency | ✅ | trace/config |

→ Mỗi entity type (Service / DB / Host / Cluster / API / Queue...) có **một matrix riêng**. Matrix là *hợp đồng* định nghĩa "Definition of Understanding". Mỗi facet mang `{status: unknown|hypothesis|verified, confidence, source, last_verified_at}` (nối với Observation→Hypothesis→VerifiedFact ở Domain Model).

---

## Q4. Knowledge Acquisition Order — Senior không random, luôn có thứ tự

**Nguyên tắc: LLM KHÔNG đứng đầu.** Senior đọc nguồn xác định trước, LLM chỉ để *diễn giải*, không phải *nguồn sự thật*.

### Thứ tự tổng quát (deterministic → human → LLM-interpret)

```
1. Declared config     (application.yml, env, docker-compose, helm values, TF)
2. Orchestration       (kubectl, docker, systemctl — trạng thái khai báo)
3. Runtime introspect  (process list, /proc, ss/netstat — sự thật đang chạy)
4. Network truth       (port thật mở, kết nối thật, DNS resolve)
5. Documentation       (README, Wiki, Confluence, diagram)
6. Observability       (Prometheus, Grafana, logs, tracing)
7. Human               (hỏi — khi mọi nguồn trên không trả lời được)
   ── LLM xuyên suốt: chỉ để INTERPRET các nguồn trên, không tự bịa fact ──
```

### Ví dụ cụ thể — "hiểu Redis" (đúng thứ tự sếp đưa)

```
application.yml → docker-compose → kubectl → systemctl → netstat
   → README → Wiki → Monitoring → Logs → Human
```

- Config nói Redis ở `:6379` → **verify** bằng netstat/ss thật → match thì thành VerifiedFact; lệch thì sinh contradiction → mission verify / câu hỏi.
- **Verify-before-believe**: nguồn cao trong danh sách (config/doc) KHÔNG tự thành fact nếu chưa đối chiếu runtime.

→ Mỗi entity type có một **Acquisition Order** template. Đây là phần "kinh nghiệm nghề" được mã hóa thành dữ liệu (data), không nhét vào prompt.

---

## Q5. Khi nào Agent tự quyết, khi nào phải hỏi? → **Question Strategy** + **Autonomy Decision**

### 5a. Question Strategy (decision tree khi gặp cái KHÔNG biết)

```
Gặp unknown
   │
   ├─ Tự verify được không?  ──yes──► tự verify (KHÔNG hỏi)
   │                          no
   ├─ Còn nguồn khác không?  ──yes──► đọc nguồn (theo Acquisition Order)
   │                          no
   └─ Hỏi human ──► hỏi AI? người nào? (escalation theo độ liên quan)
                     Owner → Lead → Architect → Developer → Operator
```

- **Câu hỏi không-lười** (đã chốt ở vision): mỗi câu hỏi mang `{đã-biết, hiểu-hiện-tại, bất-định, câu-hỏi-chính-xác}`.
- **Never ask twice**: đã hỏi → lưu pending; có answer → vào Twin; /skip → không hỏi lại 7d.
- **Chọn người**: theo facet — Owner cho business/SLA, Architect cho topology/HA, Developer cho API/code, Operator cho deploy/runtime.

### 5b. Autonomy Decision (khi nào tự *hành động* — mutate)

```
effective_autonomy = min(
    tenant_tier,                       # ceiling do khách đặt
    understanding_confidence(scope),   # đủ hiểu scope chưa
    role.policy_posture                # READ_ONLY | SUGGEST | MUTATE_ALLOWED
)
+ gate: reversibility & blast-radius & contradiction
```

- Hành động **không thể đảo ngược** / blast-radius lớn → luôn HITL, bất kể score.
- Có **contradiction** chưa giải quyết trong knowledge → cấm mutate, ưu tiên verify.
- Tái dùng nguyên: kill-switch fail-closed, CRAT trước mọi emit, autonomy tier.

---

## Q6. "Hiểu đầy đủ" — vượt ngoài Understanding Score

Score là tổng hợp. **Truth là Competency Matrix.** Một hệ thống "hiểu đầy đủ" khi:

```
✓ Mọi CRITICAL entity có Competency Matrix = 100% facet bắt buộc, status=verified
✓ Dependency graph ĐÓNG: không còn edge trỏ tới entity unknown (no dangling)
✓ Contradiction count = 0  (doc vs runtime đã hòa giải)
✓ Mọi CRITICAL entity có Owner + Runbook + SLA verified
✓ Open critical unknown = 0  (mọi câu hỏi quan trọng đã có answer hoặc accepted)
   ─────────────────────────────────────────────
   UnderstandingScore = hàm tổng hợp các điều trên (để hiển thị/gate), KHÔNG thay thế chúng
```

→ "Đã hiểu" là **mệnh đề kiểm chứng được trên matrix + graph**, không phải con số đẹp. Onboarding Playbook EXIT khi điều kiện trên đạt cho toàn bộ CRITICAL scope.

---

## Tổng hợp kiến trúc Operating Model (data, không phải prompt)

```
PLAYBOOK (curriculum chuẩn)                    ← tài sản #1, data-driven
   defines → [Mission templates có thứ tự + gate]
MISSION template
   declares → required Competency Matrix (Definition of Understanding)
COMPETENCY MATRIX (per entity type)
   each facet → Knowledge Acquisition Order (thứ tự nguồn)
ACQUISITION fails → QUESTION STRATEGY (verify? → source? → ask whom?)
ACTION needed → AUTONOMY DECISION (act vs ask vs HITL)
MISSION done → EXPERIENCE extracted (anonymized) → cải thiện Playbook
```

**Vòng tự cải tiến**: Experience từ mission hoàn thành → tinh chỉnh Playbook/Acquisition Order → Senior "giỏi lên" theo thời gian. Đây là chỗ AI/LLM đóng góp (học pattern), nhưng **xương sống vẫn là Playbook data**.

---

## 4 câu trả lời của sếp → đã tích hợp

| # | Quyết định | Đưa vào model |
|---|---|---|
| 1 | Skeleton = type + anon-id + **relationship** + criticality + health + confidence + capability tags (KHÔNG IP/hostname thật) | Cập nhật `DOMAIN_MODEL` Q5 — xem mục dưới |
| 2 | Sub-mission **bắt buộc**, mỗi cái có Goal + DoD + Deliverable | Q2 (curriculum + sub-mission đệ quy) |
| 3 | Skill = **Built-in** (theo loại agent) + **Learned** (tích lũy) — không phải tờ giấy trắng | Cập nhật Domain Model Q3 |
| 4 | **1 Mission chính + nhiều sub-mission song song** (không nhiều mission chính) | Q2 (mission chính Understand System + sub song song) |

### Bổ sung Skeleton (chốt residency — quan trọng nhất)

Omni Skeleton chứa, per entity:
- Entity **type** (Service/DB/Queue/Host/Cluster/API...)
- **Anonymous ID** (hash, KHÔNG hostname/IP thật)
- **Quan hệ** (Service A → DB B) ← bắt buộc để Omni suy luận topology
- **Criticality**
- **Health/Status**
- **Confidence**
- **Capability tags**

Omni KHÔNG cần biết `10.0.0.12`, nhưng CẦN biết "Service Payment phụ thuộc Database Primary". Twin DETAIL (IP, config, secret, doc, owner PII) **ở lại khách**.

---

## Điểm cần sếp chốt tiếp (trước implement)

1. **Playbook representation**: YAML/JSON data-driven (như `diagnostic_matrix.yaml` hiện có) — đề xuất theo hướng này để Playbook là tài sản editable, version-controlled, không nằm trong code.
2. **Competency Matrix per entity type**: cần liệt kê đủ entity types ở v1 (Service/DB/Host/Cluster/API/Queue/Cache/Storage/Network?) và facet bắt buộc từng loại.
3. **Acquisition Order per entity type**: bộ thứ tự nguồn cho từng loại (đã có mẫu Redis) — cần chuẩn hóa cho DB/Service/Network/K8s.
4. **Curriculum gate strictness**: bước nền (Inventory→Network) cứng tuần tự tới đâu, bước nào được song song.
5. **Playbook tự tiến hóa**: cho phép Experience sửa Playbook tự động, hay chỉ đề xuất human-approve? (đề xuất: đề xuất + human-approve, giữ Playbook là tài sản có kiểm soát).
