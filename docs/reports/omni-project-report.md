# Omni — Hệ Thống SRE Tự Động Đa Tác Nhân
### Nền Tảng Giám Sát & Khắc Phục Sự Cố K8s Thế Hệ Tiếp Theo

---

# Bối Cảnh & Vấn Đề

## Thách Thức Của Vận Hành Hệ Thống Hiện Đại

- **Khối lượng cảnh báo khổng lồ** — hàng nghìn alert/ngày, phần lớn là nhiễu
- **Phản ứng chậm** — MTTD và MTTR cao, phụ thuộc hoàn toàn vào con người
- **Thiếu ngữ cảnh** — alert đơn lẻ, không tương quan đa nguồn
- **Chi phí vận hành tăng** — đội SRE phải on-call 24/7 để xử lý sự cố lặp đi lặp lại
- **Rủi ro tuân thủ** — không có audit trail đầy đủ cho SOX §404, PCI-DSS v4.0

## Giải Pháp: Hệ Thống SRE Tự Động

> Omni là nền tảng đa tác nhân async-first, tự động chẩn đoán, đề xuất và thực thi khắc phục sự cố trên K8s — có kiểm soát con người (HITL) và audit trail mật mã.

---

# Tổng Quan Hệ Thống

## Omni Platform — Thống Kê

| Chỉ Số | Giá Trị |
|--------|---------|
| Mã nguồn Python | **47,807 dòng** |
| File nguồn | **241 file** |
| Test functions | **3,809 test** |
| Test files | **151 file** |
| K8s manifests | **64 file YAML** |
| Kafka topics | **20+ topics** |
| UI pages | **11 trang** |

## Công Nghệ Cốt Lõi

- **Runtime**: Python async-first (`asyncio`, `aiokafka`, `kubernetes-asyncio`)
- **LLM**: Ollama — Qwen3.6 (35B MoE) + Nomic Embed Text (768-dim RAG)
- **Message Bus**: Apache Kafka (split pipeline)
- **Storage**: Redis Stack (HNSW vector store + semantic cache)
- **Orchestration**: Kubernetes (OrbStack lab / production K8s)
- **Observability**: Prometheus + Grafana + custom KPI dashboard

---

# Kiến Trúc Hệ Thống

## Pipeline End-to-End

```
[FinGuard Redis]          [Prometheus]          [SIEM Events]
      │                       │                      │
      ▼                       ▼                      ▼
omni-siem-bridge      omni-prober            evidence-adapter
      │                       │                      │
      └───────────────────────┴──────────────────────┘
                              │
                    kafka: omni-diagnostic-evidence
                              │
                              ▼
                    ┌─────────────────────┐
                    │   omni-analyst      │
                    │  ┌───────────────┐  │
                    │  │ RAG Gate      │  │
                    │  │ Redis HNSW    │  │
                    │  └───────────────┘  │
                    │  ┌───────────────┐  │
                    │  │ Ollama LLM    │  │
                    │  │ Qwen3.6 35B   │  │
                    │  └───────────────┘  │
                    │  ┌───────────────┐  │
                    │  │ CRAT Audit    │  │
                    │  │ SHA-256+Ed25519│  │
                    │  └───────────────┘  │
                    └─────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
    kafka: omni-actions            kafka: omni-hitl-pending
              │                               │
              ▼                       omni-hitl-dispatcher
    omni-executor                             │
    (K8s mutations)                   FinGuard HITL API
              │                               │
              └───────────────┬───────────────┘
                              ▼
                  kafka: omni-action-feedback
```

## Worker Roles

| Role | Chức Năng |
|------|----------|
| `prober` | Thu thập metrics Prometheus, quản lý Kafka alerts |
| `analyst` | Chẩn đoán LLM, KPI collector, advisory |
| `core` | Deep scout, forecast, baseline snapshot |
| `executor` | Thực thi K8s mutations (fail-closed) |
| `siem-bridge` | Redis → Kafka bridge cho SIEM events |
| `evidence-adapter` | Chuyển đổi SIEM raw events |
| `hitl-dispatcher` | Human-in-the-loop approval gateway |
| `gateway` | FastAPI HTTP → Kafka (separate image) |

---

# 4 Luồng Chẩn Đoán

## Lane 1 — Resource Anomaly Detection

**Phát hiện bất thường tài nguyên bằng thống kê 3-sigma**

- `ThreeSigmaGate`: cửa sổ 100 điểm, TTL 3600s
- Tính z-score cho CPU và memory mỗi tick
- Anomaly khi `|z| > 3.0` — ngưỡng thống kê nghiêm ngặt
- Baseline lưu trong Redis: `omni:baseline_snapshot`
- **Output**: `3-SIGMA RESOURCE BASELINE` block trong advisory evidence

```
CPU/Mem time series → Rolling z-score (window=100)
    → |z| > 3.0? → Anomaly → Inject evidence → LLM diagnosis
```

## Lane 2 — System Error Analysis (WHAT/WHO/WHY/HOW-TO)

**Phân tích lỗi hệ thống với schema có cấu trúc**

Schema `AnalystAdvisory`:
- **WHAT**: `root_cause` — 1 câu, scope cụ thể (ns/workload/pod)
- **WHO**: `affected_workload` — namespace/deployment
- **WHY**: `verification_steps[].rationale` — chứng minh/bác bỏ nguyên nhân
- **HOW-TO**: `proposed_remediation[]` — các bước an toàn, flag `approval_required`
- **Forecast**: `ForecastTimeline` — 5 khung thời gian (1h/3h/6h/12h/24h)

**Chẩn đoán từ dưới lên L1→L4:**
`os_baremetal` → `network` → `kubernetes` → `prometheus`

## Lane 3 — Business Error Detection (HTTP Status)

**Giám sát lỗi nghiệp vụ qua HTTP status codes**

| HTTP Class | Loại Lỗi | Sigma Bypass |
|-----------|---------|-------------|
| 5xx (500-504) | Server error | ✅ Có |
| 429 | Rate limiting | ✅ Có |
| 401/403 | Auth failure | ✅ Có |
| 499 | Client abort | ❌ Chỉ informational |

- `count_access_errors()` → `AccessErrorCounts` với histogram theo class
- `classify_http_status(status)` → phân loại tự động
- Tích hợp với `log_surge_probe.py` — phát hiện surge lỗi

## Lane 4 — Smart-SIEM (FinGuard Security Incidents)

**Phát hiện và phân tích sự cố bảo mật real-time**

Các loại sự cố được phát hiện:
- **DDoS Attack** — giám sát flow traffic bất thường
- **Malware Detection** — phân tích hành vi process
- **Data Exfiltration** — theo dõi luồng dữ liệu outbound
- **K8s Threats** — runtime anomaly trong cluster
- **Auth Failure** — brute force & credential stuffing
- **Lateral Movement** — phát hiện di chuyển ngang
- **Network Anomaly** — bất thường DNS/routing

```
SIEM Events → CorrelatingPublisher (ZSET window)
    → Chain Detection → LLM Analysis
    → Structured Output (WHAT/WHO/WHY/HOW-TO/Forecast)
    → Telegram Alert (với 5-horizon severity escalation)
```

---

# CRAT — Audit Trail Mật Mã

## Cryptographic Regulatory Audit Trail

**Tuân thủ SOX §404 & PCI-DSS v4.0**

### Cơ Chế Bảo Mật

```
Block N-1 hash ──────────────────────────┐
                                          ▼
[Event Data] ──► SHA-256 Hash ──► Block N ──► Ed25519 Sign
                                          │
                                          ▼
                              Redis: audit_chain:blocks
                              Kafka: omni-audit-chain
```

- **Hash chaining**: Block N bao gồm hash của Block N-1 → phát hiện giả mạo hồi tố
- **Ed25519 signing**: Mỗi block được ký số (K8s Secret mount)
- **Fail-closed**: `write_audit_block()` PHẢI thành công trước mọi hành động

### Event Types

| Event | Khi Nào |
|-------|---------|
| `ADVISORY_DECISION` | LLM ra quyết định advisory |
| `ADVISORY_DISPATCHED` | Advisory được gửi đi |
| `MUTATION_TRAPPED` | Thao tác mutation bị chặn |
| `HITL_DECISION` | Con người phê duyệt/từ chối |
| `ROLLBACK_EXECUTED` | Auto-rollback thực hiện |
| `SOP_PROMOTED` | SOP được học và thêm vào |

- `llm_reasoning_hash` + `llm_reasoning_ref` lưu trong mỗi `ADVISORY_DECISION`
- Raw LLM reason tại `omni:crat:llm_reason:{trace}:{step}` (TTL=86400s)

---

# Human-in-the-Loop (HITL)

## Kiểm Soát Con Người Theo Tầng

```
Advisory Generated
        │
        ▼
OMNI_AUTO_EXECUTE_ENABLED?
    NO  │  YES
        │    └──► Auto-execute (low risk only)
        ▼
HITL Required?
        │
        ▼
kafka: omni-hitl-pending
        │
        ▼
omni-hitl-dispatcher ──► FinGuard HITL API
        │
   ┌────┴────┐
APPROVED   REJECTED
   │           │
   ▼           ▼
Execute    Action Feedback
(kafka:    (learning loop)
omni-actions)
```

### Bảo Vệ Nhiều Lớp

- **Kill switch**: `OMNI_AUTO_EXECUTE_ENABLED=false` — fail-closed toàn hệ thống
- **Advisory mode**: `OMNI_SIEM_SUGGEST_ONLY=true` — chỉ đề xuất, không thực thi
- **RBAC**: Executor KHÔNG BAO GIỜ có cluster-admin
- **Fallback**: `OMNI_HITL_FALLBACK_CHANNEL=slack` khi HITL API không available
- **Timeout**: `OMNI_HITL_ESCALATION_TIMEOUT_SEC=900` — tự động escalate

---

# Observability & Tự Giám Sát

## Worker Health Server (:8090)

**Thread-based HTTP server — passive model**

```
GET /healthz → {"status": "ok|degraded|unhealthy", "checks": {...}}
GET /readyz  → 200 nếu status != "unhealthy"
```

| Check | Threshold | Kết Quả |
|-------|----------|---------|
| `kafka_lag` | > 1000 msg | unhealthy |
| `redis_ping` | timeout | unhealthy |
| `llm_up` | = 0 | degraded |
| `last_message_age` | > 600s | unhealthy |

### K8s Probes
- **Readiness** `/readyz` :8090 — `initialDelaySeconds: 30`
- **Liveness** `/healthz` :8090 — `initialDelaySeconds: 90`

## 7 Prometheus Alerts

1. `OmniWorkerStalled` — worker ngừng xử lý message
2. `OmniWorkerHealthDegraded` — trạng thái degraded
3. `OmniWorkerHealthUnhealthy` — trạng thái critical
4. `OmniRedisConnectionLost` — mất kết nối Redis
5. `OmniLLMSustainedDown` — LLM down kéo dài
6. `OmniAdvisoryAcceptanceRateLow` — tỷ lệ chấp nhận advisory thấp
7. `OmniFalsePositiveRateHigh` — tỷ lệ false positive cao

## Business KPI Dashboard

**Rolling 24h window — Redis ZADD (không dùng INCR để tránh overflow)**

```
kafka: omni-action-feedback
    → omni-kpi-collector (consumer group)
        → Redis ZADD (timestamp-scored sets)
            → GET /kpi/summary
            → GET /kpi/trend?window=1h|6h|24h|7d
```

| KPI Metric | Mô Tả |
|-----------|------|
| Advisory Acceptance Rate | % advisory được chấp nhận |
| False Positive Rate | % advisory sai |
| MTTD per Lane | Thời gian phát hiện theo luồng |
| MTTR per Lane | Thời gian khắc phục theo luồng |
| Incidents Total | Tổng sự cố theo luồng & kết quả |

---

# Advisory Quality Benchmark

## Hệ Thống Đánh Giá Chất Lượng LLM

**10 golden cases từ post-mortems thực tế**

| Case | Tình Huống |
|------|-----------|
| case_001 | Missing ConfigMap |
| case_002 | Redis OOM |
| case_003 | Kafka lag |
| case_004 | 5xx HTTP surge |
| case_005 | DDoS attack |
| case_006 | Normal CPU (không có sự cố) |
| case_007 | ImagePullBackOff |
| case_008 | Auth surge |
| case_009 | LLM service down |
| case_010 | CRAT integrity |

### Thang Điểm 100pt

```
Verdict accuracy    ████████████████████████████████  30 điểm
Key keyword match   ████████████████████████          20 điểm
No hallucination    ████████████████████████          20 điểm
Remediation quality ██████████████████                15 điểm
Verification steps  ██████████████████                15 điểm
                                        Ngưỡng pass: 70/100
```

---

# Smart-SIEM — Hệ Thống Bảo Mật Thông Minh

## Kiến Trúc Smart-SIEM

```
SIEM Events (Kafka/Redis)
        │
        ▼
  brain-go (Go)
  ┌─────────────────────────────┐
  │  CorrelatingPublisher       │
  │  (ZSET window correlation)  │
  │                             │
  │  LLMAnalyzer                │
  │  (Local LLM analysis)       │
  │                             │
  │  Redis Rate Limiter         │
  │  (1 alert/tenant/5min)      │
  └─────────────────────────────┘
        │
        ▼
  CHAIN_DETECTED incident
        │
  ┌─────┴──────┐
  │  Omni      │
  │  Agent     │ ─────► Telegram Alert
  │  (Python)  │        (5-horizon forecast)
  └────────────┘
```

## Thành Phần Smart-SIEM

| Component | Ngôn Ngữ | Vai Trò |
|-----------|---------|---------|
| `brain-go` | Go | Event correlation, LLM analysis |
| `agent` | Python | Omni integration, Kafka bridge |
| `bff` | Go | Backend-for-frontend API |
| `ui-nextjs` | Next.js | SOC dashboard |
| `math-gateway` | Go | Behavioral analytics |
| `local-llm` | Python | Local LLM inference |

### Dual Transport Mode
- `BRAIN_TRANSPORT=redis` — Redis streams (default lab)
- `BRAIN_TRANSPORT=kafka` — Kafka topics (production)

---

# Giao Diện Người Dùng

## Omni UI — 11 Trang Dashboard

```
/                  ── Landing & system overview
/incidents         ── Incident list & details
/workers           ── Worker health & status
/kpi               ── Business KPI charts
/ledger            ── CRAT audit trail viewer
/siem              ── Smart-SIEM dashboard
/playbooks         ── SOP playbook management
/deploy            ── Deployment controls
/config/autonomy   ── Autonomy settings
/onboarding        ── Setup wizard
/login             ── Authentication
```

## KPI Dashboard — Visualizations

- **4 stat cards**: Acceptance rate, False positive, MTTD, MTTR
- **Pie chart**: Acceptance vs False positive distribution
- **Bar chart**: Lane resolution time comparison (SYS_RESOURCE / SYS_HARD_FAIL / APP_HTTP / SIEM_SECURITY)
- **Trend lines**: 1h / 6h / 24h / 7d rolling window

---

# Continuous Learning

## Hệ Thống Tự Học

```
Incident resolved
        │
        ▼
omni-action-feedback (Kafka)
        │
        ▼
Learning Promoter
        │
OMNI_SOP_AUTO_PROMOTE_ENABLED=true
min_success = 3 lần
        │
        ▼
SOP auto-promoted → RAG vector store
        │
        ▼
Future incidents: RAG-first diagnosis
```

### Vòng Phản Hồi

1. **Advisory phát sinh** → CRAT ghi nhận
2. **Người vận hành phê duyệt/từ chối** → Feedback loop
3. **Kết quả thành công** được đếm per SOP
4. **>= 3 lần thành công** → SOP promoted vào RAG
5. **Lần sau** → RAG match trước LLM → nhanh hơn + rẻ hơn

---

# Bảo Mật & RBAC

## Defense In Depth

### Container Security
- `USER appuser` uid 10001 — không chạy root
- Secrets: env + K8s Secrets only
- Gitleaks CI gate — quét secrets trước khi commit

### Kubernetes RBAC
- `omni-worker` SA: chỉ đọc pods/log, events trong namespace `multi-agent`
- `omni-temporal-prober-role`: get/list/watch pods, nodes, deployments — **không write, không Secrets**
- Executor: **KHÔNG BAO GIỜ cluster-admin**

### Invariants Không Thể Vi Phạm
- `INV_NO_RESTART_ON_BROKEN_SPEC` — không restart khi spec bị hỏng
- `INV_READ_BEFORE_MUTATE` — phải đọc trạng thái trước khi thay đổi
- `INV_NAMESPACE_ISOLATION` — tuyệt đối không cross-namespace
- `ERR_GOV_UNAUTHORIZED_MUTATION` — block mọi mutation không được phép

---

# Infrastructure & Deployment

## Môi Trường

```
Lab:        OrbStack K8s — namespace multi-agent
Production: K8s cluster — multi-agent + finguard-customer

Ingress:  ai-agent.local (Traefik v3)
Gateway:  :8080 (FastAPI)
Health:   :8090 (Worker health server)
```

## Quy Trình CI/CD

```
git push
    │
    ▼
Build (Docker) ──► Rollout (K8s) ──► Unit Tests ──► E2E Tests
                                                          │
                                              ┌──────────┴──────────┐
                                              │  make e2e-proactive  │
                                              │  make e2e-incident   │
                                              │  make lab-nginx-cpu  │
                                              └─────────────────────┘
```

## Kafka Topics Chính

| Topic | Mục Đích |
|-------|---------|
| `omni-diagnostic-evidence` | Evidence từ mọi nguồn vào analyst |
| `omni-actions` | Lệnh thực thi cho executor |
| `omni-action-feedback` | Kết quả thực thi → feedback loop |
| `omni-audit-chain` | CRAT immutable audit trail |
| `omni-hitl-pending` | Chờ phê duyệt con người |
| `omni-alerts` | Cảnh báo từ prober/gateway |
| `omni-siem-raw` | SIEM raw events |
| `omni-siem-incidents` | SIEM processed incidents |

---

# Kết Quả & Thành Tựu

## Metrics Chất Lượng

| Hạng Mục | Kết Quả |
|----------|---------|
| Test coverage | **3,809 test functions** |
| Advisory benchmark | **10/10 golden cases** |
| Pass threshold | **≥ 70/100 điểm** |
| Diagnostic lanes | **4 luồng song song** |
| CRAT compliance | **SOX §404 + PCI-DSS v4.0** |
| Kafka recovery | `auto_offset_reset="earliest"` |
| Response audit | **100% events** được ghi nhận |

## Tính Năng Nổi Bật

✅ **Multi-agent async pipeline** — tất cả components async, không blocking  
✅ **4 diagnostic lanes** — resource, system, business, security song song  
✅ **Fail-closed by default** — mọi thứ an toàn khi thiếu config  
✅ **CRAT crypto audit trail** — tamper-evident, signed, SOX compliant  
✅ **Continuous learning** — SOP tự động học từ lịch sử thành công  
✅ **Human-in-the-loop** — con người kiểm soát ở mọi tầng  
✅ **Self-monitoring** — worker tự báo cáo trạng thái sức khỏe  
✅ **Smart-SIEM integration** — bảo mật real-time với LLM analysis  

---

# Roadmap

## Đã Hoàn Thành (2026 Q1-Q2)

- [x] **Sprint 0**: Core pipeline, 4 diagnostic lanes
- [x] **Sprint 1**: Production hardening — K8s probes, Kafka recovery, asyncio gate
- [x] **Sprint 2**: CRAT audit trail, HITL gateway, RBAC
- [x] **Sprint 3**: Observability — health server, KPI dashboard, benchmark
- [x] **Sprint 4**: Smart-SIEM integration, LLM advisory tuning

## Tiếp Theo (2026 Q3-Q4)

- [ ] **Auto-scaling** theo KPI metrics (MTTD/MTTR threshold)
- [ ] **Multi-cluster support** — federation across multiple K8s clusters
- [ ] **Advanced ML models** — thay thế 3-sigma bằng LSTM/Transformer
- [ ] **SLA enforcement** — tự động trigger SLO burn rate alerts
- [ ] **Cost optimization** — model routing theo task complexity (Haiku → Sonnet → Opus)

---

# Tổng Kết

## Omni — Tầm Nhìn

> **Biến SRE từ "chữa cháy" thành "phòng ngừa"**
> 
> Omni không chỉ là công cụ — đây là đồng đội SRE AI làm việc 24/7, học hỏi từ mỗi sự cố, và ngày càng thông minh hơn theo thời gian.

### 3 Giá Trị Cốt Lõi

| 🔍 Chẩn Đoán | 🛡️ Kiểm Soát | 📈 Học Hỏi |
|-------------|-------------|-----------|
| 4 luồng song song | HITL + fail-closed | SOP auto-promotion |
| LLM + RAG + 3σ | CRAT audit trail | Benchmark quality |
| WHAT/WHO/WHY/HOW | RBAC nghiêm ngặt | Continuous feedback |

---

*Tài liệu được tạo: 2026-05-15 | Phiên bản: Sprint 4 (LLM Advisory Tuning)*
