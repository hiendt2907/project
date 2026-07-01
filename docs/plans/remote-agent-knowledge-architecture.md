# Remote Agent — Knowledge Architecture

> **Status**: APPROVED, chưa implement  
> **Created**: 2026-06-26  
> **Owner**: SRE Platform

---

## Bối cảnh

Remote agent hiện tại đẩy mọi evidence (metrics, logs, discovery) qua cùng 1 pipeline
`omni-diagnostic-evidence` — pipeline được thiết kế cho alert, không phải thu thập kiến thức.
Hệ quả: log "all clean" và metric heartbeat bình thường gây nhiễu Active Traces, tốn RAG
round-trip vô nghĩa.

**Mental model đúng**: remote agent giống nhân viên system engineer mới tiếp nhận hệ thống
khách hàng — phải biết thu thập tài liệu, học pattern bình thường, giám sát liên tục, và
gặp cái không biết thì hỏi system admin.

---

## Architecture tổng quan

```
Remote Agent (VM khách hàng)
    │
    ├── signal_type = METRIC_SAMPLE  ──┐
    ├── signal_type = LOG_SAMPLE     ──┤──► omni-knowledge-evidence ──► Knowledge Worker
    ├── signal_type = DISCOVERY      ──┤         (mới)                      (mới)
    └── signal_type = CHANGE_DETECTED─┘
    │
    ├── signal_type = ANOMALY ────────────► omni-diagnostic-evidence ──► pipeline hiện tại
    └── (3-sigma breach / log surge / service down)

Knowledge Worker xử lý:
    METRIC_SAMPLE   → update 3-sigma baseline + add_confidence(+1/100 samples)
    LOG_SAMPLE      → rolling log store Redis (RAG context)
    DISCOVERY       → diff với baseline → sinh CHANGE_DETECTED nếu khác
    CHANGE_DETECTED → Telegram admin + lưu pending approval
    UNKNOWN_ENTITY  → Telegram admin hỏi + chờ doc upload

Confidence Score → Autonomy Level (per host):
    0–30   STATIC_GUARD  → chỉ static threshold, mọi action cần approve
    30–60  LEARNING      → 3σ AND static (double-check), action cần approve
    60–85  ASSISTED      → 3σ độc lập, suggest auto / execute cần approve
    85–100 AUTONOMOUS    → full auto trong scope tenant tier cho phép

    Effective autonomy = min(tenant_tier, confidence_level)
    Score tăng theo data: metric samples, doc upload, inventory approve,
    incident history, explicit admin verify. Decay -5/ngày khi offline.
```

---

## Phase 1 — Signal routing (agent + gateway)

**Mục tiêu**: mỗi evidence envelope mang `signal_type`, gateway route đúng Kafka topic.

### `signal_type` mapping

| Probe | Condition | signal_type |
|---|---|---|
| `remote_system_metrics` | result=PASSED, no 3σ breach | `METRIC_SAMPLE` |
| `remote_system_metrics` | 3σ breach detected | `ANOMALY` |
| `remote_log_errors` | result=PASSED | `LOG_SAMPLE` |
| `remote_log_errors` | result=FAILED | `ANOMALY` |
| discovery probes | any | `DISCOVERY` |
| change diff | new/removed service | `CHANGE_DETECTED` |
| unknown process/port | not in known list | `UNKNOWN_ENTITY` |

### Files thay đổi

- `src/remote_agent/evidence.py` — thêm `signal_type: str = "ANOMALY"` vào `build_envelope()`
- `src/remote_agent/agent.py` — gán `signal_type` per probe trước khi emit
- `src/gateway/routes/agent_webhook.py` — route `signal_type != ANOMALY` sang
  `omni-knowledge-evidence` (env: `OMNI_KAFKA_TOPIC_KNOWLEDGE_EVIDENCE`)
- `scripts/kafka_ensure_omni_topics.sh` — thêm `omni-knowledge-evidence` (partitions=3,
  retention=7d)
- `k8s/deployments/omni-worker-configmap.yaml` — thêm
  `OMNI_KAFKA_TOPIC_KNOWLEDGE_EVIDENCE: omni-knowledge-evidence`

### Tests

- `tests/test_agent_webhook_gigo.py` — extend routing test
- `tests/test_signal_type_routing.py` (mới) — mỗi probe → đúng topic

---

## Phase 2 — Knowledge Worker

**Mục tiêu**: consumer loop `omni-knowledge-evidence`, dispatcher theo `signal_type`.

### Redis keys mới

```
omni:knowledge:logs:{agent_id}:rolling   LIST, LPUSH+LTRIM 500, TTL=24h
omni:knowledge:change_pending:{tenant}:{change_id}   HASH, TTL=7d
omni:knowledge:docs:{tenant_id}:{doc_id}             STRING (JSON), TTL=30d
```

### Files mới / thay đổi

- `src/workers/knowledge_pipeline.py` (mới) — dispatcher:
  ```
  METRIC_SAMPLE   → update_remote_host_baseline()
  LOG_SAMPLE      → redis LPUSH omni:knowledge:logs:{agent_id}:rolling + LTRIM 500
  DISCOVERY       → load baseline snapshot → diff → emit CHANGE_DETECTED if changed
  CHANGE_DETECTED → send Telegram + store pending
  UNKNOWN_ENTITY  → send Telegram hỏi admin
  ```
- `src/workers/omni_worker.py` — thêm `kafka_knowledge_evidence_loop()`, wire vào
  role `full` và `analyst`

### Tests

- `tests/test_knowledge_pipeline.py` (mới) — mỗi signal_type → đúng handler

---

## Phase 3 — Data Confidence Score → Autonomy Level

> **Nguyên tắc từ sếp**: "Flow trên là khi không có mả mẹ gì. Mỗi đoạn có thêm dữ liệu
> là rút ngắn quá trình học. Càng nhiều dữ liệu chi tiết thì quá trình chạy có quyết định
> (được cho phép) tự động sẽ ngắn hơn."

**Không có time-based phase cố định.** Thời gian (72h, 24h...) chỉ là worst-case khi không
có data — là hệ quả tự nhiên của tốc độ tích lũy metric samples khi không có nguồn nào khác.
**Data là nhiên liệu**; càng nhiều data chất lượng → score tăng → autonomy unlock sớm hơn.

### Data Confidence Score (0–100)

Lưu tại `omni:3sigma:confidence:{tenant}:{host}` (TTL=30d, refresh mỗi khi score thay đổi).

**Các nguồn data tăng score:**

| Nguồn | Điểm | Điều kiện |
|---|---|---|
| Metric samples (rolling) | +1/100 samples, tối đa +30 | cpu + mem + disk đủ cả 3 |
| Admin approve service inventory | +15 | sau lần discovery đầu được confirm |
| Tài liệu kiến trúc upload (doc/pdf/diagram) | +20 | per doc, tối đa +20 |
| Change history được approve | +3/change, tối đa +10 | admin approve thay đổi |
| Runbook/SOP của tenant này | +15 | ingest vào knowledge store |
| Incident history (advisory pairs từ host này) | +10 | ≥ 3 pairs trong RAG |
| Admin explicit verify baseline ("baseline ok") | +20 | lệnh tường minh qua Telegram |

**Tổng tối đa**: 100 điểm. Score tích lũy cộng dồn, không reset khi agent restart.

**Score decay**: mỗi ngày agent offline → -5 điểm (tối đa -30 sau 6 ngày). Lý do: hệ thống
có thể thay đổi khi agent không giám sát — confidence giảm tự nhiên.

### Score → Autonomy Level

```
0–30   STATIC_GUARD
    → Alert chỉ khi vượt static threshold: CPU>95% / MEM>92% / disk>90%
    → Mọi action cần human approve
    → Worst case: host mới hoàn toàn, ~24h để đủ metric samples lên 30

30–60  LEARNING
    → Alert khi 3-sigma breach AND static threshold (AND logic — double-check)
    → Action cần human approve
    → Điển hình: có metric history nhưng chưa có tài liệu / service inventory

60–85  ASSISTED
    → Alert khi 3-sigma breach (độc lập)
    → Action: suggest tự động, execute cần approve
    → Điển hình: đủ metric + approved inventory + 1–2 tài liệu

85–100 AUTONOMOUS
    → Alert + action tự động trong scope được phép (autonomy tier hiện tại)
    → Điển hình: đủ tài liệu + incident history + admin explicit verify
```

**Fast-path thực tế**: nếu khách hàng upload đầy đủ tài liệu ngay khi onboard
(+20 doc + +15 runbook + +15 inventory approve + +20 explicit verify = 70) → vào ASSISTED
ngay mà không cần chờ metric samples. Thêm +30 metric (72h) → AUTONOMOUS.

**Mối quan hệ với autonomy tier hiện tại** (`shadow`/`minimal`/`autonomous` trong
`omni:cfg:tier:{tenant}`): confidence score là gate bổ sung **phía dưới** — tier của tenant
là ceiling, confidence score là floor. Agent không thể vượt tier của tenant, nhưng cũng
không được autonomous nếu score chưa đủ.

```
Effective autonomy = min(tenant_tier, confidence_level)
```

### Omni thông báo khi score thay đổi tầng

Telegram tự động khi cross threshold:
- Lên LEARNING: "📊 [host] đủ dữ liệu cơ bản. Chuyển sang học pattern. Gửi tài liệu hệ thống để tăng tốc."
- Lên ASSISTED: "🟡 [host] đủ độ tin cậy để tự alert. Action vẫn cần approve."
- Lên AUTONOMOUS: "🟢 [host] đủ độ tin cậy. Tự động hóa trong scope được phép."
- Score decay về tầng thấp hơn: "⚠️ [host] offline X ngày, confidence giảm về LEARNING."

### Files thay đổi

- `src/anomaly/remote_host_baseline.py` — thêm:
  - `ConfidenceLevel` enum (STATIC_GUARD / LEARNING / ASSISTED / AUTONOMOUS)
  - `get_confidence_score(redis, tenant, host)` → int
  - `add_confidence(redis, tenant, host, points, reason)` — cộng điểm + log
  - `decay_confidence(redis, tenant, host)` — trừ điểm theo ngày offline
  - `score_to_level(score)` → ConfidenceLevel
  - `should_emit_anomaly(level, z_scores, fact)` — gate logic theo level
- `src/workers/knowledge_pipeline.py` — gọi `add_confidence()` mỗi khi nhận data
  mới (metric sample, doc upload, inventory approve)
- `src/workers/change_approval_handler.py` — gọi `add_confidence()` khi admin approve

### Tests

- test score tích lũy từ nhiều nguồn
- test level transition (STATIC_GUARD → LEARNING → ASSISTED → AUTONOMOUS)
- test decay khi agent offline
- test effective autonomy = min(tenant_tier, confidence_level)
- test worst-case: 0 data → cần ~30 metric samples để lên LEARNING (~30 phút ở 60s interval)

---

## Phase 4 — Change detection + admin approval

**Mục tiêu**: phát hiện service mới/mất mỗi 1h, hỏi admin approve qua Telegram.

### Flow

```
agent mỗi 1h → run_vm_discovery() → diff_discovery(old_snapshot, new_snapshot)
    ├── Không thay đổi → heartbeat log, không làm gì
    ├── Service mới   → emit CHANGE_DETECTED
    │                  → Telegram: "🔍 nginx 1.24 mới cài trên prod-web-01. Approve?"
    │                  → lưu pending: omni:knowledge:change_pending:{tenant}:{id}
    │                  → admin reply "approve" → update baseline, ghi knowledge store
    │                  → admin reply "reject"  → flag UNAUTHORIZED_CHANGE → ANOMALY pipeline
    └── Service mất   → emit CHANGE_DETECTED (type=REMOVED)
                       → Telegram: "⚠️ apache2 dừng trên prod-web-01. Planned hay sự cố?"
                       → approve = planned maintenance, reject = incident
```

### Files thay đổi / mới

- `src/remote_agent/discovery.py` — thêm:
  - `save_discovery_snapshot(redis, tenant, host, profile)` — lưu baseline
  - `load_discovery_snapshot(redis, tenant, host)` — load để diff
  - `diff_discovery(old, new)` → list `ChangeEvent(type, service, detail)`
- `src/remote_agent/agent.py` — re-discovery 24h → **1h**; sau diff emit
  `CHANGE_DETECTED` nếu có diff
- `src/workers/knowledge_pipeline.py` — handler `CHANGE_DETECTED`:
  - lưu pending + gửi Telegram với inline keyboard approve/reject
- `src/workers/change_approval_handler.py` (mới) — xử lý Telegram callback:
  - approve → `update_discovery_baseline()` + notify admin "đã cập nhật"
  - reject → emit `UNAUTHORIZED_CHANGE` → `omni-diagnostic-evidence`

### Tests

- `tests/test_change_detection.py` (mới) — diff logic, approve/reject flow

---

## Phase 5 — Telegram doc-upload loop

**Mục tiêu**: admin reply doc/ảnh → Omni ingest vào knowledge base per-tenant.

### Flow

```
Omni hỏi qua Telegram: "Process X port 8443 là gì? Gửi tài liệu nếu có."
    ↓
Admin reply (3 cách):
  1. Text thuần: "Đây là internal auth service"
  2. Upload file: architecture.pdf / network_diagram.png / runbook.docx
  3. /skip: bỏ qua
    ↓
Webhook → detect reply_to_message_id → link về pending question
    ↓
  Text  → store trực tiếp vào knowledge store
  File  → download từ Telegram Bot API → extract (PDF→text, image→store raw)
          → embed → upsert vector store collection "customer_knowledge"
  /skip → mark pending as skipped, không hỏi lại trong 7d
```

### Redis / storage

```
omni:knowledge:pending_q:{tenant}:{msg_id}   HASH (question, change_id, ts), TTL=7d
omni:knowledge:docs:{tenant_id}:{doc_id}     STRING (JSON: content, source, ts), TTL=30d
Vector store collection: customer_knowledge  (per-tenant filter by tenant_id)
```

### Files thay đổi / mới

- `src/workers/handlers.py` — extend Telegram update handler:
  - detect `document` / `photo` / `video` message type
  - detect `reply_to_message_id` → lookup pending question
  - route tới `ingest_customer_document()`
- `src/services/knowledge/document_store.py` (mới):
  - `ingest_customer_document(tenant_id, content, context, source_type)`
  - embed text → upsert `customer_knowledge` vector collection
  - store raw tại `omni:knowledge:docs:{tenant_id}:{doc_id}`
- `src/gateway/routes/agent_webhook.py` — forward Telegram document update về
  knowledge worker (không xử lý inline ở gateway)

### Tests

- `tests/test_telegram_doc_upload.py` (mới) — text reply, file reply, /skip, no reply_to

---

## Thứ tự triển khai và deploy

```
Phase 1 ──deploy──► Phase 2 ──deploy──► Phase 3 ──► Phase 4 ──► Phase 5
  (routing)           (worker)           (warmup)    (change)    (doc)

Phase 1+2 là core — phải deploy xong thì pipeline mới sạch.
Phase 3–5 là enhancement, có thể độc lập.
```

## Invariants mới

- `INV_KNOWLEDGE_NOT_ALERT`: signal_type != ANOMALY không được đi vào `omni-diagnostic-evidence`
- `INV_CONFIDENCE_GATE`: alert 3-sigma chỉ được emit khi confidence_level >= LEARNING;
  dưới LEARNING chỉ dùng static threshold
- `INV_EFFECTIVE_AUTONOMY`: effective_autonomy = min(tenant_tier, confidence_level) —
  confidence score không được vượt ceiling của tenant tier
- `INV_SCORE_DECAY`: score PHẢI decay khi agent offline, không được giữ nguyên indefinitely
- `INV_CHANGE_APPROVAL`: `UNAUTHORIZED_CHANGE` (admin reject) phải đi qua CRAT trước khi emit alert
- `INV_DOC_RESIDENCY`: nội dung tài liệu khách hàng lưu per-tenant, không share cross-tenant
