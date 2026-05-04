# Integration Plan: Smart-SIEM as an Omni Feature

## Context

Hiện tại Omni và Smart-SIEM là 2 hệ thống độc lập kết nối lỏng lẻo qua bridge:

```
Smart-SIEM (finguard-customer ns)     Omni (multi-agent ns)
  brain-go → Redis Stream    ←──── siem_bridge.py (cross-ns XReadGroup)
  agent HITL API ←────────────── hitl_dispatcher.py (HTTP polling 10s)
  bff ←──── OMNI_GATEWAY_URL proxy ──── omni-gateway FastAPI
```

**3 integration points hiện tại:**
1. `src/workers/siem_bridge.py` — đọc `stream:actionable_incidents` từ FinGuard Redis → Kafka `omni-alerts`
2. `src/services/evidence_adapter/` — chuyển incident → evidence envelopes vào `omni-diagnostic-evidence`
3. `src/workers/hitl_dispatcher.py` — poll HTTP `hitl-api.finguard-customer.svc` mỗi 10s

**Vấn đề:** 2 message buses (Redis Streams vs Kafka), cross-namespace credentials, HTTP polling latency, schema drift giữa contracts Go và Omni alert labels.

**Mục tiêu:** Smart-SIEM trở thành **module siem** bên trong Omni — cùng Kafka bus, cùng Postgres cho HITL, cùng `trace_id`, cùng CRAT audit chain. Namespace merger là **option**, không bắt buộc (bank có thể yêu cầu data isolation).

---

## Repo đích — Sơ đồ duy nhất

> **Giải quyết xung đột:** `src/` Python vẫn ở root (KHÔNG di chuyển). Chỉ `smart-siem/` được đưa vào `omni/siem/`. Hai kế hoạch restructure không mâu thuẫn.

```
project/
├── src/                        # Python Omni workers — GIỮ NGUYÊN ở root
│   ├── workers/
│   ├── services/
│   └── pkg/
├── tests/                      # Python tests — GIỮ NGUYÊN ở root
├── omni/
│   └── siem/                   # ← smart-siem/ di chuyển vào đây (Phase 1)
│       ├── brain-go/           # ← smart-siem/customer/brain-go/
│       ├── agent/              # ← smart-siem/customer/agent/
│       ├── bff/                # ← smart-siem/customer/bff/
│       ├── contracts-go/       # ← smart-siem/customer/contracts/ (Go struct types)
│       ├── contracts/          # ← smart-siem/contracts/ (JSON/proto schemas)
│       ├── ui-frontend/        # ← smart-siem/customer/ui-frontend/
│       └── (other customer/)   # license-validator, local-llm, math-gateway, ...
├── k8s/
│   └── deployments/            # GIỮ NGUYÊN paths (constraint)
├── smart-siem/provider/        # Provider-side — giữ tách, không đổi (DRM, release-hub)
├── scripts/
├── docs/
├── CLAUDE.md
└── Makefile
```

**smart-siem/provider/** không di chuyển vào omni/ — đây là Cloud HQ side, không liên quan Omni.

---

## Namespace Strategy (quyết định kiến trúc)

Hai lựa chọn, **Omni chủ động chọn trước khi implement Phase 2** (quyết định này ảnh hưởng NetworkPolicy Phase 2 ngay):

### Option A — Shared Kafka, 2 namespaces (Recommended cho bank)
```
multi-agent (Omni control plane)   finguard-customer (SIEM data plane)
  Kafka cluster (shared) ────────────────────────────────────┐
  omni-siem-raw topic ──────────────► brain-go (reads Kafka) │
  omni-siem-incidents ◄───────────── brain-go (writes Kafka) │
  Omni Postgres (HITL) ◄─────────── agent (reads HITL table) │
  CRAT Redis ──────────────────────── audit writes            │
```
- **Ưu:** Giữ data isolation (bank compliance), cross-namespace chỉ còn Kafka + 1 Postgres DSN
- **Nhược:** Vẫn 2 deployments, 2 K8s release cycles

### Option B — Single namespace `multi-agent`
- **Ưu:** Đơn giản hơn (1 namespace), không cần NetworkPolicy cross-ns
- **Nhược:** Mất data/control-plane separation, có thể vi phạm bank security policy

**Plan này implement Option A trước.** Option B là upgrade path nếu bank chấp thuận.

---

## HITL Store Strategy (điều chỉnh từ bản trước)

**Quyết định: Postgres là source of truth, Redis chỉ là pub/sub notification.**

Lý do: single DSN, plain SQL files (đồng bộ với Migration Convention bên dưới), dễ audit, ACID transactions.

```
Omni Postgres (omni_hitl_decisions table)  ← source of truth
    ↑ INSERT (hitl_dispatcher.py)
    ↓ SELECT (smart-siem agent reads via OMNI_PG_DSN)

Redis pub/sub (channel: hitl:notify:{incident_id})  ← wake-up signal only
    ← hitl_dispatcher.py publishes AFTER Kafka (thứ tự đầy đủ: xem Phase 3D)
    → smart-siem agent subscribes (thay polling HTTP 10s)
```
> **Assumption:** Omni Postgres là instance hiện có trong namespace `multi-agent` (CNPG hoặc standalone). Đây là dep mới đối với smart-siem agent — SRE cần confirm sizing, backup policy, và cấp DSN riêng trước Phase 3 begin. **Lưu ý:** CLAUDE.md ghi "Postgres removed" chỉ áp dụng cho RAG/embedding (đã migrate sang Redis Stack HNSW). Postgres Omni này là instance riêng chỉ dùng cho HITL (`omni_hitl_decisions`), không liên quan RAG.

**Security hardening (bank-grade):**
- `OMNI_PG_DSN` cho agent (Go): `GRANT SELECT ON omni_hitl_decisions TO siem_agent_ro` (chỉ SELECT; agent không cần LISTEN/NOTIFY vì dùng Redis pubsub)
- `OMNI_PG_DSN` cho hitl_dispatcher: `GRANT SELECT, INSERT ON omni_hitl_decisions TO siem_dispatcher_rw` (SELECT để check idempotency + RETURNING; INSERT để ghi — PostgreSQL GRANT INSERT không bao gồm SELECT)
- `OMNI_PG_DSN` cho reconciler: `GRANT SELECT, INSERT ON omni_hitl_decisions TO siem_reconciler_rw` (INSERT để backfill; không cần UPDATE — crat_block NULL khi backfill là acceptable)
- TLS required: `sslmode=require` trong DSN cho cả ba
- Không dùng superuser DSN cho bất kỳ component nào — least privilege

**Schema:** `omni_hitl_decisions` table (Omni quản lý migration):
```sql
CREATE TABLE omni_hitl_decisions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id  TEXT NOT NULL UNIQUE,
    tenant_id    TEXT NOT NULL,
    decision     TEXT NOT NULL CHECK (decision IN ('approved','rejected')),
    reason       TEXT,
    actor        TEXT NOT NULL,
    trace_id     TEXT NOT NULL,  -- xuyên suốt từ omni-siem-raw → CRAT
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    crat_block   BIGINT            -- FK → CRAT block sequence (audit linkage)
);
CREATE INDEX ON omni_hitl_decisions (incident_id);
CREATE INDEX ON omni_hitl_decisions (tenant_id, recorded_at DESC);
```

---

## Topic & Schema Mapping

> **Tránh drift:** Mọi topic mới đều có schema canonical trong `omni/siem/contracts/`.

| Redis Stream (hiện tại) | Kafka Topic (mới) | Schema | Producer | Consumer |
|------------------------|-------------------|--------|----------|----------|
| `stream:siem_normalized` | `omni-siem-raw` | `omni/siem/contracts/incident.json` (v1.0.0) | **siem_bridge** (dual-emit flag) | brain-go |
| `stream:actionable_incidents` | `omni-siem-incidents` | `omni/siem/contracts/incident.json` + `correlated_chain` ext | **brain-go** | agent (khi `BRAIN_TRANSPORT=kafka`), bff-sse |
| *(new)* | `omni-hitl-decisions` | `omni/siem/contracts/openapi/hitl-decision.yaml` (CREATE) | **hitl_dispatcher** (Omni internal routing) | analyst, executor — **không** phải agent (agent dùng Redis pubsub) |
| `omni-alerts` *(không đổi)* | `omni-alerts` | Prometheus AlertManager v4 | siem_bridge | prober |
| `omni-diagnostic-evidence` *(không đổi)* | `omni-diagnostic-evidence` | `src/pkg/reasoning/schema.py` | evidence_adapter | analyst |
| `omni-audit-chain` *(không đổi)* | `omni-audit-chain` | `src/services/audit_ledger/chain_writer.py` | CRAT writer | — |

> **Pipeline note:** analyst không đọc `omni-siem-incidents` trực tiếp — nhận qua `omni-diagnostic-evidence` (evidence_adapter transform). `EVIDENCE_SOURCE=incidents` chỉ ảnh hưởng evidence_adapter: đọc `omni-siem-incidents` (schema `incident.json`) thay vì `omni-alerts` (schema AlertManager v4) — cần đảm bảo `siem_adapter.py` map đúng schema trước khi bật flag.

**Contract files cần tạo mới:**
- `omni/siem/contracts/openapi/hitl-decision.yaml` — HITL decision schema
- `omni/siem/contracts/openapi/siem-kafka-events.yaml` — Kafka message envelope cho omni-siem-raw/incidents

**trace_id propagation** (xuyên suốt pipeline):
```
omni-siem-raw message (incident.id = UUID v4)
    → brain-go giữ nguyên incident.id → omni-siem-incidents.id
    → evidence_adapter inject trace_id = incident.id → omni-diagnostic-evidence
    → hitl_dispatcher ghi trace_id = incident.id → omni_hitl_decisions.trace_id
    → CRAT block.trace_id = incident.id  (audit linkage)
```

> **Lưu ý gap với Omni Gateway:** Gateway HTTP hiện gán `trace_id` riêng (`gw-{uuid8}`). Nếu cần join SIEM incidents với Loki/Gateway logs, thêm field `omni_trace_id` vào `contracts/incident.json` và populate nó bằng gateway trace khi siem_bridge emit vào omni-siem-raw. Đây là quyết định Phase 2 — ghi vào ADR nếu chọn implement.

---

## Migration Convention (ownership & tooling)

> **Quy ước dứt khoát — tránh hai hệ migration cho cùng DB.**

| DB | Migration location | Tooling | Format | Owner |
|----|-------------------|---------|--------|-------|
| FinGuard Postgres (finguard-customer) | `omni/siem/agent/db/migrations/` (đã có) | Plain SQL, `BEGIN/COMMIT` | `NNN_description.sql` | smart-siem agent |
| Omni Postgres (multi-agent) | `src/db/migrations/` (NEW, Phase 3 tạo) | Plain SQL, `BEGIN/COMMIT` | `NNN_description.sql` | Omni Python repo |

**Không dùng alembic, không dùng goose.** Cả hai DB đều dùng plain SQL files — nhất quán với convention đã có trong `customer/db/migrations/`.

Naming: `001_omni_hitl_decisions.sql`, `002_...` — sequential, không timestamp.

Apply command:
```bash
# Omni Postgres migration
psql "$OMNI_PG_DSN" -f src/db/migrations/001_omni_hitl_decisions.sql

# CI gate: chạy tất cả migrations trong test container
for f in src/db/migrations/*.sql; do psql "$TEST_PG_DSN" -f "$f"; done
```

**smart-siem agent** tiếp tục apply `omni/siem/agent/db/migrations/` vào FinGuard Postgres. Khi Phase 3 hoàn tất và `omni_hitl_decisions` đã ổn, ta sẽ đánh dấu `002_hitl_approvals.sql` là deprecated (không xóa — audit trail).

---

## Contract CI

> **Mọi PR đụng `omni/siem/contracts/**` phải pass schema check.**

**New file:** `.github/workflows/contract-check.yml`
```yaml
name: Contract CI
on:
  pull_request:
    paths:
      - 'omni/siem/contracts/**'
      - 'omni/siem/contracts-go/**'
      - 'src/pkg/reasoning/analyst_advisory_schema.py'
      - 'src/services/evidence_adapter/siem_adapter.py'

jobs:
  openapi-diff:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }

      - name: OpenAPI diff (breaking change detection)
        # Bootstrap: nếu file chưa tồn tại sau Phase 1 (path mới),
        # job chỉ chạy jsonschema + validate_contract_sync. Skip diff để không đỏ CI mãi.
        run: |
          BASE_FILE="omni/siem/contracts/openapi/customer-bff.yaml"
          if git show origin/${{ github.base_ref }}:"$BASE_FILE" > /dev/null 2>&1; then
            git show origin/${{ github.base_ref }}:"$BASE_FILE" > /tmp/base-bff.yaml
            docker run --rm \
              -v $(pwd)/$BASE_FILE:/revision.yaml \
              -v /tmp/base-bff.yaml:/base.yaml \
              oasdiff/oasdiff breaking /base.yaml /revision.yaml && echo "No breaking changes"
          else
            echo "Bootstrap: customer-bff.yaml not in base branch yet, skipping diff"
          fi

      - name: Validate incident.json schema
        run: |
          pip install jsonschema
          python -c "
          import json, jsonschema
          schema = json.load(open('omni/siem/contracts/incident.json'))
          jsonschema.Draft202012Validator.check_schema(schema)
          print('incident.json schema valid')
          "

      - name: Go struct ↔ JSON schema sync check
        run: |
          # Verify required fields in incident.json match Go struct tags in contracts-go/types.go
          python scripts/validate/validate_contract_sync.py \
            --json-schema omni/siem/contracts/incident.json \
            --go-types omni/siem/contracts-go/types.go
```

**New file:** `scripts/validate/validate_contract_sync.py` — parse Go struct json tags, compare required fields với JSON schema properties.

---

## Migration Phases (thứ tự điều chỉnh)

### Phase 1 — Repo restructure (NO behavior change)

Di chuyển `smart-siem/customer/` → `omni/siem/`. Provider-side giữ nguyên.

```bash
mkdir -p omni/siem
git mv smart-siem/customer/brain-go      omni/siem/brain-go
git mv smart-siem/customer/agent         omni/siem/agent
git mv smart-siem/customer/bff           omni/siem/bff
git mv smart-siem/customer/contracts     omni/siem/contracts-go
git mv smart-siem/customer/ui-frontend   omni/siem/ui-frontend
git mv smart-siem/customer/local-llm     omni/siem/local-llm
git mv smart-siem/customer/math-gateway  omni/siem/math-gateway
git mv smart-siem/customer/license-validator omni/siem/license-validator
git mv smart-siem/contracts              omni/siem/contracts  # JSON/proto schemas
# smart-siem/customer/k3s     → deprecated, giữ tạm cho reference
# smart-siem/provider/        → giữ nguyên tại root/smart-siem/provider/
```

Cập nhật `go.work` paths, Makefile build paths, `.github/workflows/`, `CLAUDE.md`.

**CI path filter checklist (Phase 1 PR):**
- `.github/workflows/` — đổi filter `smart-siem/customer/**` → `omni/siem/**` trong mọi workflow đang trigger theo path
- `contract-check.yml` — thêm `omni/siem/contracts-go/**` vào paths trigger (sync Go types)
- Gitea/internal webhook nếu có: cập nhật tương tự

**Không thay đổi:** Logic Go, K8s manifests, Kafka topics, Redis keys.

---

### Phase 2 — Kafka transport cho brain-go (CORE CHANGE)

brain-go hiện dùng Redis XREADGROUP/XADD. Thêm Kafka transport với feature flag `BRAIN_TRANSPORT=redis|kafka`.

**New files:**
- `omni/siem/brain-go/internal/transport/kafka.go`
  ```go
  // KafkaTransport: implement Transport interface
  // Consume: sarama consumer group omni-siem-raw
  // Produce: sarama producer omni-siem-incidents
  // Preserves incident.ID làm Kafka message key (partition affinity per tenant)
  ```

- `omni/siem/brain-go/internal/transport/interface.go`
  ```go
  type Transport interface {
      Consume(ctx context.Context) (<-chan contracts.Incident, error)
      Produce(ctx context.Context, incident contracts.Incident) error
  }
  // RedisTransport (existing), KafkaTransport (new)
  ```

**Modify:** `omni/siem/brain-go/cmd/brain-go/main.go`
```go
// BRAIN_TRANSPORT=kafka → KafkaTransport; default=redis (backward compat)
```

**Chạy CROSS-NAMESPACE** trong Phase 2 (brain-go vẫn ở finguard-customer, nhưng read/write Kafka của multi-agent). Namespace merge là Phase 5.

**NetworkPolicy Phase 2** — egress Kafka (port 9092), pod label selector:

**New file:** `k8s/deployments/siem-brain-go-netpol.yaml`
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: siem-brain-go-egress-kafka
  namespace: finguard-customer
spec:
  podSelector:
    matchLabels:
      app: siem-brain-go          # pod label trên Deployment (không phải ServiceAccount)
  policyTypes: [Egress]
  egress:
  - ports:
    - port: 9092                  # Kafka broker plaintext
      protocol: TCP
    - port: 9093                  # Kafka broker TLS (nếu dùng)
      protocol: TCP
    to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: multi-agent
          # ⚠️ Điều chỉnh `to:` theo chart Kafka thực tế: nếu broker nằm ngoài
          # namespace multi-agent (e.g. kafka namespace riêng, NodePort, ExternalName),
          # thay namespaceSelector bằng ipBlock hoặc podSelector phù hợp.
  # Nếu brain-go vẫn dùng Redis (ZSET correlate) khi BRAIN_TRANSPORT=kafka, thêm egress 6379
  # tới Redis đúng namespace (thường finguard-customer hoặc multi-agent — khớp REDIS_URL thực tế).
  # - ports:
  #   - port: 6379
  #     protocol: TCP
  #   to:
  #   - namespaceSelector: ...
  - ports:                        # DNS (bắt buộc cho cross-ns lookup)
    - port: 53
      protocol: UDP
```

**siem_bridge.py — dual-emit trong giai đoạn chuyển tiếp:**
```python
# Modify: src/workers/siem_bridge.py
# Hiện tại emit → omni-alerts (giữ nguyên, prober vẫn dùng)
# Thêm: emit raw FinGuard incident (JSON nguyên văn) → omni-siem-raw
#        chỉ khi SIEM_BRIDGE_DUAL_EMIT=true
#
# DEDUP: cùng một incident_id sẽ xuất hiện ở cả omni-alerts VÀ omni-siem-raw.
# analyst pipeline (evidence_adapter / evidence_worker) chỉ được subscribe MỘT trong hai:
#   - Giai đoạn chuyển tiếp: ingest path mặc định vẫn là topic omni-alerts (consumer = evidence path trong code, không nhất thiết “qua prober”)
#   - Sau Phase 2 stable: switch sang omni-siem-incidents (output brain-go sau correlate)
# Không subscribe cả hai đồng thời → duplicate evidence cho cùng incident_id.
#
# Cutover không-restart qua feature flag trên evidence_worker:
# EVIDENCE_SOURCE=alerts    → subscribe omni-alerts (default, giai đoạn chuyển tiếp)
# EVIDENCE_SOURCE=incidents → subscribe omni-siem-incidents (sau Phase 2 stable)
```

**New Kafka topics:**
```bash
# Thêm vào scripts/kafka/kafka_ensure_omni_topics.sh
kafka-topics.sh --create --topic omni-siem-raw       --partitions 6 --replication-factor 1
kafka-topics.sh --create --topic omni-siem-incidents --partitions 6 --replication-factor 1
kafka-topics.sh --create --topic omni-hitl-decisions --partitions 3 --replication-factor 1
```

**Files:**
- `omni/siem/brain-go/internal/transport/kafka.go` — CREATE
- `omni/siem/brain-go/internal/transport/interface.go` — CREATE
- `omni/siem/brain-go/cmd/brain-go/main.go` — MODIFY
- `src/workers/siem_bridge.py` — MODIFY (dual-emit flag)
- `k8s/deployments/siem-brain-go.yaml` — CREATE (brain-go in finguard-customer, Kafka env vars)
- `scripts/kafka/kafka_ensure_omni_topics.sh` — MODIFY

---

### Phase 3 — Unify HITL: Postgres source of truth + Redis pub/sub

**Mục tiêu:** Thay HTTP polling 10s bằng Postgres + Redis notify. Cross-namespace vẫn ok (chỉ cần DB connection).

#### 3A. Tạo migration Omni Postgres

**New file:** `src/db/migrations/001_omni_hitl_decisions.sql`
```sql
-- =============================================================================
-- Omni — HITL Decisions (source of truth, replaces FinGuard hitl_approvals)
-- Owned by: Omni Python repo. Apply: psql "$OMNI_PG_DSN" -f this_file.sql
-- =============================================================================
BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS omni_hitl_decisions (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id  TEXT        NOT NULL UNIQUE,
    tenant_id    TEXT        NOT NULL,
    decision     TEXT        NOT NULL CHECK (decision IN ('approved','rejected')),
    reason       TEXT,
    actor        TEXT        NOT NULL,
    trace_id     TEXT        NOT NULL,   -- propagated from omni-siem-raw message
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    crat_block   BIGINT                  -- FK → CRAT block seq; NULL if CRAT write raced
);

CREATE INDEX IF NOT EXISTS idx_omni_hitl_incident ON omni_hitl_decisions (incident_id);
CREATE INDEX IF NOT EXISTS idx_omni_hitl_tenant   ON omni_hitl_decisions (tenant_id, recorded_at DESC);

COMMIT;
```

#### 3B. Cập nhật hitl_dispatcher.py

**Modify:** `src/workers/hitl_dispatcher.py`
```python
# Thay POST /v1/hitl/decisions HTTP → logic trong _record_decision() (xem 3D)
# Giữ HITL_LEGACY_API_URL env: nếu set → vẫn gọi HTTP (backward compat)
# Không còn polling vòng lặp 10s.
#
# Luồng DISPATCHER (phía Omni, sau khi nhận quyết định từ operator):
#   1. write_audit_block(HITL_DECISION)        [CRAT — fail-closed]
#   2. INSERT INTO omni_hitl_decisions          [Postgres — recoverable]
#   3. KafkaBus.send(omni-hitl-decisions, ...)  [Kafka — analyst/executor routing, raise on fail]
#   4. redis.publish(hitl:notify:{id})          [Redis — agent wake-up, warn-only on fail]
# Chi tiết thứ tự + idempotency + retry: xem 3D.
#
# omni-hitl-decisions Kafka topic: dành cho Omni internal (analyst → executor routing).
# KHÔNG phải để agent (Go) đọc. Agent wake-up chỉ qua Redis pubsub.
```

#### 3C. Cập nhật smart-siem agent để đọc từ Omni Postgres

**New file:** `omni/siem/agent/internal/hitl/omni_store.go`
```go
// OmniPGStore implements HITLDecisionStore, reads from Omni Postgres
// Env: OMNI_PG_DSN (cross-namespace connection string)
// SELECT decision, trace_id FROM omni_hitl_decisions WHERE incident_id=$1
// Wake-up: subscribe Redis pubsub "hitl:notify:{incident_id}" (replaces HTTP polling loop)
// Feature flag: HITL_STORE=finguard|omni (default=finguard, migrate to omni)
```

**NetworkPolicy Phase 3** — agent egress to Omni Postgres (port 5432):

**New file:** `k8s/deployments/siem-agent-netpol.yaml`
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: siem-agent-egress-omni-pg
  namespace: finguard-customer
spec:
  podSelector:
    matchLabels:
      app: siem-agent               # pod label trên Deployment (không phải ServiceAccount)
  policyTypes: [Egress]
  egress:
  - ports:
    - port: 5432                    # PostgreSQL
      protocol: TCP
    to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: multi-agent
      podSelector:
        matchLabels:
          app: postgres             # label trên Omni Postgres pod
  - ports:                          # Redis pubsub (wake-up only)
    - port: 6379
      protocol: TCP
    to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: multi-agent
  - ports:
    - port: 53
      protocol: UDP
```

#### 3D. Ghi HITL_DECISION vào CRAT — thứ tự fail-closed

> **CRAT invariant (từ CLAUDE.md):** `write_audit_block()` MUST succeed TRƯỚC bất kỳ action nào. Failure aborts transaction.

**Thứ tự bắt buộc:**

```python
# src/workers/hitl_dispatcher.py  (Modify)
async def _record_decision(incident_id, decision, actor, reason, trace_id):
    # ── Step 1: CRAT FIRST (fail-closed) ──────────────────────────────────
    # Nếu CRAT fail → raise, không ghi DB, không notify Redis.
    # Unaudited action là worse-than-nothing trong SOX context.
    try:
        crat_seq = await write_audit_block(
            event_type="HITL_DECISION",
            payload={"incident_id": incident_id, "decision": decision,
                     "actor": actor, "reason": reason},
            trace_id=trace_id,
        )
    except Exception:
        raise  # abort — caller sẽ log và retry

    # ── Step 2: INSERT DB (CRAT đã recorded) ─────────────────────────────
    # Nếu DB fail → CRAT block là orphan, nhưng decision đã được audit.
    # Acceptable: CRAT is ground truth; DB row là recoverable cache.
    try:
        await pg.execute(
            "INSERT INTO omni_hitl_decisions "
            "(incident_id, tenant_id, decision, reason, actor, trace_id, crat_block) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (incident_id) DO NOTHING",
            incident_id, tenant_id, decision, reason, actor, trace_id, crat_seq,
        )
    except Exception as e:
        logger.error("hitl_db_insert_failed_after_crat",
                     crat_seq=crat_seq, incident_id=incident_id, error=str(e))
        # Không re-raise — decision đã có trong CRAT
    # ON CONFLICT DO NOTHING: HITL coi là terminal cho incident_id; nếu cần đổi quyết định sau
    # khi đã ghi, dùng luồng/phiên bản riêng (ADR) — không silent overwrite.

    # ── Step 3: Kafka (Omni internal routing — analyst/executor) ─────────
    # Kafka TRƯỚC Redis: đảm bảo Omni routing set up trước khi agent thức.
    # Nếu Kafka fail → re-raise (retryable); agent sẽ không thức → không lệch automation.
    try:
        await kafka_bus.send_dict("omni-hitl-decisions",
                                  {"incident_id": incident_id, "decision": decision,
                                   "trace_id": trace_id, "crat_block": crat_seq})
    except Exception as e:
        logger.error("hitl_kafka_publish_fail",
                     incident_id=incident_id, crat_seq=crat_seq, error=str(e))
        raise  # retryable — caller retry toàn bộ _record_decision
    # IDEMPOTENCY + RETRY PATTERN:
    # Trước bước 1, caller nên check: đã có row trong omni_hitl_decisions (hoặc CRAT block)
    # cho incident_id này chưa?
    #   - Có crat_block trong DB row → skip CRAT + DB, retry chỉ từ bước 3 (Kafka)
    #   - Có CRAT block nhưng không có DB row → reconciler sẽ tự backfill; retry từ bước 3
    #   - Không có gì → retry toàn hàm từ bước 1
    # write_audit_block phải idempotent theo (incident_id, HITL_DECISION) — xem chain_writer note.

    # ── Step 4: Redis notify (fire-and-forget wake-up cho agent Go) ─────────
    # Redis SAU Kafka: nếu Redis fail → warn-only; agent timeout và retry poll Postgres.
    # Không raise — CRAT + DB + Kafka đã đủ để decision được xử lý.
    await redis.publish(f"hitl:notify:{incident_id}", decision)
```

> **ADR (Architecture Decision Record):** `HITL_DECISION` là audit event bắt buộc trước khi bất kỳ downstream nào (Redis notify, Kafka routing) được kích hoạt. Điều này mở rộng CRAT invariant trong CLAUDE.md ("write_audit_block() MUST succeed before Telegram emit or action dispatch") sang HITL decisions. Security reviewer: mọi thay đổi thứ tự này cần sign-off riêng.

> **CRAT orphan reconciler:** Nếu bước 2 (INSERT DB) fail sau khi CRAT đã ghi, block CRAT tồn tại nhưng không có DB row. Agent đọc Postgres sẽ không thấy quyết định dù đã có trong audit trail.
> **Giải pháp:** Background job `src/workers/crat_hitl_reconciler.py` chạy mỗi 5 phút:
> ```python
> # Nguồn: Kafka consumer group 'crat-reconciler' trên topic omni-audit-chain
> #   - Không iterate Redis chain trực tiếp (không có index event_type, đắt)
> #   - Không dùng offset-by-timestamp portable; thay vào đó: filter bằng
> #     payload["event_type"] == "HITL_DECISION" từ committed offset gần nhất
> #     (consumer commit sau mỗi backfill batch → SLA: max lag = 5 phút reconciler interval)
> # Cross-check: SELECT incident_id FROM omni_hitl_decisions WHERE incident_id = payload.incident_id
> # Nếu thiếu → INSERT từ CRAT payload (idempotent: ON CONFLICT (incident_id) DO NOTHING)
> # Alert nếu gap > 30 phút (có thể là DB outage kéo dài)
> # Metric: crat_hitl_orphan_backfill_total
> # Role: siem_reconciler_rw — INSERT + SELECT on omni_hitl_decisions
> ```
> Không cần OUTBOX pattern (thêm complexity) vì gap là hi hữu và reconciler đủ.
> **Compact retention note:** `omni-audit-chain` dùng key=seq (mỗi seq là một key compact riêng, không bị gộp với seq khác). Rủi ro mất message chủ yếu là **retention** (time/size), không phải compaction trùng key. Cần `retention.ms` (và/hoặc `retention.bytes`) đủ dài so với SLA reconciler (vd. ≥ 24h nếu reconciler + buffer alert 30 phút).

**Modify:** `src/services/audit_ledger/chain_writer.py`
```python
# Thêm event type: "HITL_DECISION"
# Returns: crat_seq (int) — sequence number của block vừa ghi
# Lưu ý: chain_writer đã pass key=str(seq).encode() cho omni-audit-chain (compact policy).
# HITL_DECISION không thay đổi key policy — chỉ thêm vào allowed event_types set.
#
# IDEMPOTENCY implementation: trước khi ghi block, kiểm tra duplicate:
# Lựa chọn A (không thêm bảng): Redis dedup — tránh SET NX *trước* khi ghi CRAT xong
# (lỗi giữa chừng → dedup key tồn tại nhưng chưa có block → retry skip nhầm). Khuyến nghị:
#   SET dedup key *sau khi* write chain + Kafka Redis head thành công, hoặc dùng giao dịch/rollback:
#   nếu write_audit_block fail sau khi đã SET thì DEL key; hoặc chỉ SET sau bước persist cuối.
#   Hoặc: kiểm tra tồn tại key → skip write (đọc crat_seq đã lưu trong value nếu cần).
#   TTL (vd. 24h) chống leak.
# Lựa chọn B (cần migration): thêm bảng audit_meta(incident_id, event_type, crat_seq, UNIQUE)
#   → SELECT + INSERT sẽ cần migration riêng trong src/db/migrations/
# Khuyến nghị: Lựa chọn A ít thay đổi hơn; B cần migration + nguồn sự thật mới.
# Implement nên chọn một trong hai và ghi vào ADR — không để tự diễn giải.
# Mục đích: caller retry toàn hàm _record_decision mà không double-audit CRAT block.
```

**Files:**
- `src/db/migrations/001_omni_hitl_decisions.sql` — CREATE
- `src/workers/hitl_dispatcher.py` — MODIFY (`_record_decision` với CRAT→DB→Kafka→Redis)
- `omni/siem/agent/internal/hitl/omni_store.go` — CREATE (OmniPGStore + Redis subscribe)
- `omni/siem/agent/internal/hitl/store.go` — MODIFY (wire factory: `HITL_STORE=omni` → OmniPGStore)
- `src/services/audit_ledger/chain_writer.py` — MODIFY (HITL_DECISION event type, return crat_seq)
- `src/workers/crat_hitl_reconciler.py` — CREATE (orphan CRAT block backfill, chạy mỗi 5 phút)
- `omni/siem/contracts/openapi/hitl-decision.yaml` — CREATE (schema contract)

---

### Phase 4 — BFF integration (LOW RISK, cross-namespace ok)

BFF đã có `OMNI_BFF_REDIS_ADDR` và `OMNI_GATEWAY_URL`. Chỉ cần:
1. Đổi SSE source từ Redis `stream:actionable_incidents` → Kafka `omni-siem-incidents`
2. Đổi endpoint URLs sang Omni namespace

**New file:** `omni/siem/bff/internal/sse/kafka_consumer.go`
```go
// KafkaSSEConsumer: sarama consumer group omni-siem-incidents
// Broadcast SSE events tới frontend clients
// Feature flag: BFF_SSE_SOURCE=redis|kafka
```

**Modify:** `omni/siem/bff/internal/config/config.go`
```go
// Thêm: KafkaBootstrap, KafkaSIEMIncidentsTopic, KafkaConsumerGroup
// OmniRedisAddr: point to multi-agent Redis
// OmniGatewayURL: http://omni-gateway.multi-agent:8080
```

**Files:**
- `omni/siem/bff/internal/sse/kafka_consumer.go` — CREATE
- `omni/siem/bff/internal/config/config.go` — MODIFY
- `k8s/deployments/siem-bff.yaml` — CREATE (BFF env vars update)

---

### Phase 5 — Namespace merge (OPTIONAL, quyết định sau khi Phase 2-4 ổn định)

> **Defer nếu bank yêu cầu data/control-plane separation.** Run lab environment song song để validate trước.

Nếu tiến hành:
```bash
# Tạo K8s manifests mới trong multi-agent namespace
k8s/deployments/siem-brain-go.yaml    # namespace: multi-agent
k8s/deployments/siem-agent.yaml       # namespace: multi-agent
k8s/deployments/siem-bff.yaml         # namespace: multi-agent (đã dùng từ Phase 4)

# NetworkPolicy: xóa cross-namespace rules
# Redis: tất cả point to redis.multi-agent.svc
# Postgres: shared DSN OMNI_PG_DSN

# Lab validation trước production:
kubectl apply -k k8s/overlays/lab-merged-ns/
```

**Lab environment song song:**
```bash
# Overlay lab: merge cả 2 namespace vào multi-agent-lab
# Chạy smoke test E2E (xem Phase 6)
# Không ảnh hưởng production cho đến khi lab pass
```

---

### Phase 6 — E2E trace_id smoke test

> **DELIVERABLE:** Phase 6 phải tạo mới các tools sau nếu chưa tồn tại:
> - `scripts/tools/inject_siem_event.py` — inject incident JSON vào Kafka topic với known incident_id + trace_id
> - `scripts/tools/audit_trace.sh` — query CRAT blocks theo trace_id hoặc event_type
>
> Không assume tools đã có — kiểm tra `ls scripts/tools/` trước khi viết E2E script.

**New file:** `scripts/test/e2e_siem_integration.sh`
```bash
#!/usr/bin/env bash
# E2E: Verify trace_id flows omni-siem-raw → brain-go → incidents → analyst → HITL → CRAT

TRACE_ID="e2e-$(date +%s)"
INCIDENT_ID="$(uuidgen)"

# 1. Inject event vào omni-siem-raw với known trace_id
python scripts/tools/inject_siem_event.py \
  --topic omni-siem-raw \
  --incident-id "$INCIDENT_ID" \
  --trace-id "$TRACE_ID" \
  --severity high --category ddos --tenant-id e2e-tenant

# 2. Verify brain-go emit vào omni-siem-incidents
timeout 30 kafka-console-consumer \
  --topic omni-siem-incidents --max-messages 1 \
  | jq --arg id "$INCIDENT_ID" 'select(.id == $id) | .trace_id'
# Expect: "$TRACE_ID"

# 3. Verify omni-analyst nhận (diagnostic evidence)
sleep 5
kubectl logs -l app=omni-analyst -n multi-agent --tail=20 \
  | grep "$INCIDENT_ID" | grep "siem_incident"

# 4. Trigger HITL (nếu hitl_required=true trong incident)
# Verify omni_hitl_decisions INSERT với đúng trace_id
psql "$OMNI_PG_DSN" -c \
  "SELECT decision, trace_id FROM omni_hitl_decisions WHERE incident_id='$INCIDENT_ID'"
# Expect: trace_id = "$TRACE_ID"

# 5. Verify CRAT block được ghi với trace_id
bash scripts/tools/audit_trace.sh --trace-id "$TRACE_ID" --last 5
# Expect: HITL_DECISION block với trace_id

echo "E2E PASS: trace_id $TRACE_ID verified end-to-end"
```

---

## Thứ tự thực hiện (cập nhật)

| Phase | Rủi ro | Namespace | Prereqs | Ước tính |
|-------|--------|-----------|---------|---------|
| **1** — Repo restructure | Thấp | N/A | — | 2-4h |
| **2** — Kafka transport brain-go | Cao | Cross-ns ok | Phase 1 | 2-3 ngày |
| **3** — HITL → Omni Postgres | Trung bình | Cross-ns ok | Phase 2 | 1-2 ngày |
| **4** — BFF Kafka SSE | Thấp | Cross-ns ok | Phase 2 | 0.5-1 ngày |
| **5** — Namespace merge | Cao (optional) | Merge → multi-agent | Phase 2-4 stable, lab pass | 1-2 ngày |
| **6** — E2E + cleanup | Thấp | — | Phase 5 hoặc Phase 4 | 1 ngày |

**Phase 5 không block Phase 6.** Có thể chạy E2E smoke test ở Phase 4 (cross-namespace) và Phase 5 (merged). Cleanup (xóa bridge, xóa FinGuard hitl-api) chỉ sau khi E2E pass.

---

## SLO & Rollback Runbooks

> **Mỗi phase: tắt 1 flag → traffic về legacy trong < 2 phút.** Runbook 5 dòng mỗi phase.

### Phase 2 rollback — Kafka transport → Redis legacy
```bash
kubectl set env deployment/siem-brain-go -n finguard-customer BRAIN_TRANSPORT=redis
kubectl set env deployment/omni-siem-bridge -n multi-agent SIEM_BRIDGE_DUAL_EMIT=false
kubectl rollout status deployment/siem-brain-go -n finguard-customer
# Verify: Redis stream lag về 0
redis-cli -h redis.finguard-customer.svc XLEN stream:actionable_incidents
```

**SLO trigger:** Error rate `omni-siem-incidents` consumer > 5% trong 5 phút → rollback tự động hoặc manual.

### Phase 3 rollback — Omni Postgres → FinGuard HTTP legacy
```bash
# Nếu hitl-api đã bị scale down trong Phase 3, phải redeploy trước:
kubectl apply -f k8s/legacy/hitl-api-backup.yaml -n finguard-customer
kubectl rollout status deployment/hitl-api -n finguard-customer
# Sau khi hitl-api healthy mới set flag:
kubectl set env deployment/omni-hitl-dispatcher -n multi-agent HITL_STORE=finguard
kubectl set env deployment/siem-agent -n finguard-customer HITL_STORE=finguard
kubectl logs -l app=hitl-api -n finguard-customer --tail=10 | grep "POST /v1/hitl"
```

> **Prerequisite:** Lưu manifest `hitl-api` vào `k8s/legacy/hitl-api-backup.yaml` trước khi Phase 3 begin. Thiếu file này → rollback sẽ blocked.

**SLO trigger:** P99 HITL decision latency > 30s trong 3 phút → rollback.

### Phase 4 rollback — Kafka SSE → Redis SSE legacy
```bash
BFF_NS="${BFF_NAMESPACE:-finguard-customer}"
kubectl set env deployment/siem-bff -n "${BFF_NS}" BFF_SSE_SOURCE=redis
kubectl rollout status deployment/siem-bff -n "${BFF_NS}"
# Verify: SSE stream active
curl -N --max-time 5 "http://siem-bff.${BFF_NS}.svc/api/v1/stream/metrics" | head -3
```

### Phase 5 rollback — namespace merge → split legacy
```bash
# Khởi động lại services trong finguard-customer namespace từ backup manifests
kubectl apply -f k8s/legacy/finguard-customer-backup/
kubectl rollout status deployment/siem-brain-go -n finguard-customer
# Update NetworkPolicy để re-enable cross-namespace egress
kubectl apply -f k8s/deployments/siem-brain-go-netpol.yaml
```

**SLO trigger:** Bất kỳ pod siem-* CrashLoopBackOff > 3 lần sau merge → rollback ngay.

---

## Key files cần đọc khi implement

| File | Phase | Tại sao quan trọng |
|------|-------|-------------------|
| `src/workers/siem_bridge.py` | 2 | Dual-emit: omni-alerts + omni-siem-raw; dedup note |
| `src/workers/hitl_dispatcher.py` | 3 | `_record_decision`: CRAT→DB→Kafka→Redis sequence |
| `src/services/evidence_adapter/siem_adapter.py` | 2 | trace_id propagation pattern |
| `src/services/audit_ledger/chain_writer.py` | 3 | CRAT write pattern; thêm return crat_seq |
| `omni/siem/brain-go/internal/correlate/correlator.go` | 2 | Giữ nguyên logic, chỉ đổi transport |
| `omni/siem/agent/internal/hitl/store.go` | 3 | Wire factory để chọn OmniPGStore vs FinGuardStore |
| `omni/siem/agent/internal/hitl/omni_store.go` | 3 | NEW: OmniPGStore + Redis pubsub subscribe |
| `omni/siem/bff/internal/sse/consumer.go` | 4 | Redis SSE → port sang Kafka |
| `omni/siem/bff/internal/config/config.go` | 4 | OmniRedisAddr, OmniGatewayURL |
| `omni/siem/contracts/incident.json` | 2 | Canonical schema, Kafka message format |
| `omni/siem/contracts-go/types.go` | 2 | Go struct validate tags — sync với incident.json |
| `k8s/deployments/omni-siem-bridge-production.yaml` | 2 | NetworkPolicy, env vars reference |
| `k8s/legacy/hitl-api-backup.yaml` | 3 | Lưu trước Phase 3 — rollback prerequisite |
| `CLAUDE.md` | 6 | Cập nhật Key Dirs + CRAT invariant + Kafka topics |

---

## Verification per phase

**Phase 1:**
```bash
make docker-worker && make docker-gateway  # build vẫn pass
go build ./... # trong omni/siem/brain-go, omni/siem/agent, omni/siem/bff
```

**Phase 2:**
```bash
# brain-go với BRAIN_TRANSPORT=kafka, inject event:
python scripts/tools/inject_siem_event.py --topic omni-siem-raw --severity high --category ddos
kafka-console-consumer --topic omni-siem-incidents --max-messages 1  # expect correlated incident

# Regression:
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
cd omni/siem/brain-go && GOWORK=off go test ./... -count=1
```

**Phase 3:**
```bash
# HITL decision flow:
psql "$OMNI_PG_DSN" -c "SELECT * FROM omni_hitl_decisions LIMIT 5"
# CRAT audit:
python scripts/tools/audit_trace.sh --event-type HITL_DECISION --last 5
```

**Phase 4:**
```bash
# BFF SSE với BFF_SSE_SOURCE=kafka:
# Namespace phụ thuộc manifest Phase 4 (Option A: finguard-customer; Option B/Phase 5: multi-agent)
BFF_NS="${BFF_NAMESPACE:-finguard-customer}"
curl -N "http://siem-bff.${BFF_NS}.svc/api/v1/stream/metrics" | head -3  # expect SSE events
```

**Phase 5 (namespace merge):**
```bash
kubectl get pods -n multi-agent | grep siem  # tất cả siem pods ở đây
kubectl get pods -n finguard-customer        # empty hoặc chỉ còn provider services
bash scripts/test/e2e_siem_integration.sh    # full trace_id E2E pass
```

**Regression toàn bộ:**
```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
cd omni/siem && GOWORK=off go test ./brain-go/... ./agent/... ./bff/... -count=1
bash scripts/test/e2e_siem_integration.sh
```
