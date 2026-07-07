# CLAUDE.md

> **TRƯỚC MỌI TASK: đọc `MEMORY.md` + `docs/CODEBASE.md`.** Bản đồ nhanh ở memory `project_architecture_map`; chi tiết file-level ở `docs/CODEBASE.md`.

**Omni** — async-first multi-agent SRE automation for K8s. Ollama diagnoses via 4 evidence lanes; split Kafka pipeline executes remediation.

## Context Hygiene

- Mỗi session chỉ có một deliverable chính.
- Repository là source of truth; conversation history không phải source of truth.
- Sau mỗi checkpoint quan trọng, cập nhật `docs/handoffs/CURRENT_SESSION.md`.
- Báo cáo checkpoint tối đa 20 dòng: result, changed files, verification, blocker, next step.
- Không lặp lại toàn bộ lịch sử dự án.
- Khảo sát rộng phải dùng subagent và chỉ trả kết luận ngắn.
- Trước khi chuyển milestone hoặc `/clear`: cập nhật handoff và engineering artifacts bị ảnh hưởng (dùng `/prepare-clear`).
- Session mới phải kiểm tra Git state và handoff trước khi tiếp tục. Session hooks tự nạp ngữ cảnh; chi tiết ở `docs/engineering/claude-session-automation.md`.

## DIAGNOSTIC FLOWS

| Lane | Signal | Key file |
|---|---|---|
| 1 SYS_RESOURCE | 3σ z-score CPU/mem | `anomaly/three_sigma.py` (in-cluster), `anomaly/remote_host_baseline.py` (remote-host) |
| 2 SYS_HARD_FAIL | OS state machine + LLM | `os_state_validator.py`, `AnalystAdvisory` schema |
| 3 APP_HTTP | HTTP status classes (5xx/429/401) | `log_surge_probe.py` |
| 4 SIEM_SECURITY | FinGuard incidents, kill-chain | `siem_reasoning.py`, `_siem_diagnosis_from_batch()` |

**Advisory schema** (`src/pkg/reasoning/analyst_advisory_schema.py`): WHAT/WHO/WHY/HOW-TO + ForecastTimeline (5 horizons). L1→L4: os_baremetal → network → kubernetes → prometheus.
**Telegram**: `unified_incident_card.py` — nhãn VI (Sự cố/Workload/Kiểm chứng/Khắc phục/Dự báo/🧾 Audit). WHAT/WHO/WHY/HOW-TO = marker máy, KHÔNG đổi (parse-coupled).

## KNOWLEDGE PIPELINE (2026-06-27, commit c4635ab)

`INV_KNOWLEDGE_NOT_ALERT`: non-ANOMALY signals KHÔNG vào `omni-diagnostic-evidence`. Routing ở gateway (`agent_webhook.py`), không phải worker.
- `signal_type` trong `build_envelope()` (default=ANOMALY): ANOMALY → `omni-diagnostic-evidence`; METRIC_SAMPLE/LOG_SAMPLE/DISCOVERY → `omni-knowledge-evidence`
- ANOMALY (RemoteAgent) vẫn qua RAG+LLM đầy đủ: `remote_agent_pipeline.py` Stage2-6 (cluster→triage RAG→research LLM→learn→notify); chỉ healthy/PASSED rẽ Redis side-channel (`omni:remote_agent:baseline_ok:` TTL 600s), bỏ qua pipeline. "No RAG/LLM" chỉ đúng cho `knowledge_pipeline.py` dispatcher (non-ANOMALY); rolling log LPUSH+LTRIM 500/24h; change detection diff → Telegram approve/reject
- `src/anomaly/remote_host_baseline.py`: `ConfidenceLevel` (STATIC_GUARD 0-24 / LEARNING 25-49 / ASSISTED 50-74 / AUTONOMOUS 75-100); `add_confidence(delta)`, `decay_confidence(-5/day)`; key `omni:3sigma:confidence:{tenant}:{host}` TTL=30d
- `src/remote_agent/discovery.py`: `save/load_discovery_snapshot()`, `diff_discovery()` (SERVICE_ADDED/REMOVED, PORT_OPENED/CLOSED); agent re-discovery mỗi 1h
- `src/services/knowledge/document_store.py`: `ingest_customer_knowledge()` — metadata only (INV_DATA_RESIDENCY); +20 confidence per doc
- Kafka topic: `omni-knowledge-evidence` (partitions=3, retention=7d); env `OMNI_KAFKA_TOPIC_KNOWLEDGE_EVIDENCE`

## PIPELINE

Remote agents (non-ANOMALY) → `omni-knowledge-evidence` → knowledge_pipeline (no RAG/LLM)
Alert sources → `omni-diagnostic-evidence` → analyst (RAG → LLM → AnalystAdvisory → CRAT [FAIL-CLOSED] → SUGGEST/EXECUTE/HITL)
`omni-actions` → executor → `omni-action-feedback` → re-evaluation

## COMPONENT ROLES (OMNI_WORKER_ROLE)

| Role | Active loops | Deployed thật? |
|---|---|---|
| `full` | tất cả: evidence, actions, feedback, kpi, knowledge, siem-chains, tier | ✅ pod `omni-fullstack` |
| `onboarding` | discovery-evidence worker | ✅ pod `omni-onboarding` (riêng `omni-fullstack`) |
| `analyst` | kafka_evidence_loop, action_feedback, kpi, knowledge, siem-chains, tier | ❌ RETIRED 2026-07-02 (manifest xóa từ `915e509`, object cluster đã dọn) |
| `prober` | kafka_alerts_loop, delayed_queue, circuit_breaker, telegram_polling | ❌ RETIRED 2026-07-02 |
| `core` | deep_scout, forecast, baseline_snapshot, proactive | ❌ RETIRED 2026-07-02 |
| `executor` | kafka_actions_loop | ❌ RETIRED 2026-07-02 (mutation logic nay chạy trong `full`) |
| `gateway` | FastAPI HTTP → kafka omni-alerts (separate image, deployment riêng `omni-gateway`) | ✅ |

Ghi chú: các role split (`analyst/prober/core/executor`) từng tồn tại như Deployment riêng ở giai
đoạn kiến trúc trước consolidation `915e509`; nay logic của chúng chạy gộp trong `role=full`
(`omni-fullstack`). Không tạo lại Deployment riêng cho các role này trừ khi có quyết định kiến trúc
mới rõ ràng.

## INVARIANTS (vi phạm = bug)

- Async-only: `asyncio`, `kubernetes-asyncio`, `redis[hiredis]`, `aiokafka`. No subprocess for K8s.
- `src/gateway/` KHÔNG import `workers/`. Shared code → `src/pkg/`.
- Mutations only via executor; analyst is read-only.
- `OMNI_AUTO_EXECUTE_ENABLED=false` — master kill-switch (fail-closed).
- **CRAT Fail-Closed**: `write_audit_block()` MUST succeed trước Telegram emit / action dispatch.
- `kafka_evidence_loop` dùng `auto_offset_reset="earliest"` — KHÔNG đổi thành `latest`.
- `omni-audit-chain` topic cần message key (compact policy).
- `INV_NO_RESTART_ON_BROKEN_SPEC` · `INV_READ_BEFORE_MUTATE` · `INV_NAMESPACE_ISOLATION` · `ERR_REA_NO_PHYSICAL_PROOF` · `ERR_GOV_UNAUTHORIZED_MUTATION`
- `INV_KNOWLEDGE_NOT_ALERT` (xem KNOWLEDGE PIPELINE) · `INV_DATA_RESIDENCY`: tài liệu khách hàng chỉ lưu metadata trên Omni (file_id + summary ≤2000 chars).
- RBAC: worker SA (`omni-fullstack`) **không có quyền Secrets tuỳ ý** — ngoại lệ duy nhất có chủ
  đích là `patch`/`update` qua `omni-executor-mutate-lab` ClusterRole
  (`k8s/deployments/omni-fullstack-rbac.yaml`), backing cho tool `k8s_patch_secret` đã gate bằng
  `required_evidence` + `MUTATE_TOOL_ALLOWLIST` (xoay vòng credential khi remediation SIEM/security
  — xem `src/workers/analyst_agentic_loop.py`). Không mở rộng quyền Secrets ngoài phạm vi tool này.
  Executor: NEVER cluster-admin.
- `OMNI_LLM_NUM_CTX` default 8192. Dùng `build_llm_options(ctx)` — không inline getattr.
- Autonomy tier: `resolve_tier` ưu tiên Redis cache `omni:cfg:tier:{tenant}` > PG > env. Đổi env phải DEL cache.

## CRAT (SOX §404, PCI-DSS v4.0)

`src/services/audit_ledger/` — SHA-256 hash-chain + Ed25519. Events: `ADVISORY_DECISION`, `ADVISORY_DISPATCHED`, `MUTATION_TRAPPED`, `HITL_DECISION`, `ROLLBACK_EXECUTED`.
`OMNI_AUDIT_PRIVATE_KEY_PATH` — PEM Ed25519 (unset = unsigned, lab only).

## INFRASTRUCTURE

- **K8s**: OrbStack, namespace `multi-agent`. **KHÔNG phải pod duy nhất** — xem "DEPLOYMENT STATE" bên
  dưới cho topology thật (đã audit runtime, không phải suy diễn từ tài liệu cũ).
  `make deploy-worker` = `deploy-fullstack` (chỉ deploy `omni-fullstack`, không phải toàn bộ stack).
- **LLM**: Ollama `qwen2.5-coder:7b` (active) + `nomic-embed-text:latest` (768-dim). Host: `host.orb.internal:11434`.
- **DB**: PostgreSQL `omni_admin` schema (19 bảng, migration `migrations/omni_admin/000{1..4}_*.sql`,
  chạy tự động lúc worker khởi động qua `run_migrations()` cho role `full/analyst/onboarding` nếu
  `OMNI_ADMIN_PG_DSN` không rỗng) = source-of-truth autonomy config + tenant registry. Tenant PHẢI
  được provision qua `AdminConfigRepo.create_tenant()` / `POST /autonomy/tenants` trước khi
  onboarding pipeline ghi `tenant_readiness_state` cho tenant đó — thiếu bước này gây FK violation
  liên tục (xem post-mortem `docs/post-mortems/drift-correction-2026-07-02.md`). Redis = hot-path
  cache + RAG HNSW + audit chain.
- **Tests**: pytest `asyncio_mode=auto` `pythonpath=src`; dùng `FakeRedis(decode_responses=True)` cho ZSET tests (không AsyncMock).

## KEY DIRS

`src/workers/` · `src/gateway/` · `src/remote_agent/` · `src/anomaly/` · `src/services/{analyst,audit_ledger,knowledge}/` · `src/rag/` · `src/pkg/` · `k8s/deployments/` · `tests/`

## COMMANDS

```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration   # unit tests
make deploy-worker deploy-gateway ensure-kafka-topics              # deploy
make e2e-proactive e2e-incident-matrix                             # E2E
curl localhost:8090/healthz && curl localhost:8090/readyz          # health
make benchmark-advisory                                            # advisory quality
NS=multi-agent make omni-death-loop                                # chaos loop
```

## ENV (critical)

`OMNI_WORKER_ROLE` · `OMNI_ENV_MODE` (lab|prod) · `OMNI_KAFKA_BOOTSTRAP_SERVERS` · `OMNI_REDIS_URL` · `OMNI_OLLAMA_BASE_URL` · `OMNI_AUTO_EXECUTE_ENABLED` (default false) · `OMNI_LLM_NUM_CTX` (default 8192) · `OMNI_KAFKA_TOPIC_KNOWLEDGE_EVIDENCE` (default omni-knowledge-evidence) · `OMNI_AUDIT_PRIVATE_KEY_PATH` · `OMNI_TENANT_APIKEYS` (tenant_id:key,...) · `OMNI_GATEWAY_API_KEY`

## DEPLOYMENT STATE (2026-07-02, xác minh qua Whole-System Reality Audit + Drift Correction Slice)

### Declared target topology
`omni-fullstack` (role=full) là workload lõi duy nhất được `make deploy-worker` deploy mặc định.
`omni-gateway`, `omni-onboarding`, `omni-brain-go` là các Deployment RIÊNG BIỆT, có
manifest/target Makefile riêng — không phải "instance phụ của omni-fullstack".

**2026-07-06: `omni-ui` (Deployment/Service/Ingress, domain `omni.ai-agent.local` +
`portal.ai-agent.local`) đã RETIRED theo yêu cầu trực tiếp của user** — portal thật duy nhất
nay là provider/tenant Next.js apps (`aoip-provider-web`/`aoip-tenant-web` + BFF
`aoip-provider-portal`/`aoip-tenant-portal`, domain `provider.ai-agent.local` /
`tenant.ai-agent.local`). Manifest `k8s/deployments/omni-ui.yaml` đã xoá; ingress rules omni/portal
đã gỡ khỏi `k8s/ingress/ai-agent-local.yaml`. `make e2e-portal` đã trỏ lại
`tests/e2e_portals` (13 test thật, xanh) thay vì `ui/e2e` (omni-ui cũ). Root `ui/` (Next app cũ,
~25 route: pipeline/ledger/siem/workers/admin/...) CHƯA bị xoá khỏi source — không còn deploy
route nào tới nó nhưng code vẫn còn trong repo vì chưa xác nhận 100% feature-parity đã port hết
sang provider/tenant; xoá source tree là quyết định riêng cần xác nhận thêm.

### Current deployed topology (đã kubectl describe/exec xác minh trực tiếp)
| Deployment | Role/chức năng | Trạng thái |
|---|---|---|
| `omni-fullstack` | `OMNI_WORKER_ROLE=full`, tier hiệu lực = Redis cache `omni:cfg:tier:default`=`shadow` | 1/1 Running |
| `omni-gateway` | FastAPI HTTP ingress (không import `workers/`) | 1/1 Running (restart do race Kafka-chưa-ready lúc pod khởi động — dependency outage, tự phục hồi, không phải bug) |
| `omni-onboarding` | `OMNI_WORKER_ROLE=onboarding` — discovery-evidence worker | 1/1 Running |
| `omni-brain-go` | SIEM correlation engine THẬT (image `finguard/brain-go:siem-v2-corr`, consume `omni-siem-raw`→produce `omni-siem-incidents`/`omni-siem-chains`, consumer group `brain-go-kafka` không trùng lặp) — **không liên quan onboarding** | 1/1 Running |
| `redis-0`, `kafka`, `omni-postgres-0`, `redis-exporter`, `aoip-dex`, `aoip-provider-*`, `aoip-tenant-*` | portal/hạ tầng phụ trợ (provider/tenant portal là portal thật duy nhất, `omni-ui` đã retired) | Running |

### Kill-switch — effective value đã xác minh trên pod thật
`OMNI_AUTO_EXECUTE_ENABLED=false` (đã revert 2026-07-02; trước đó bị override thành `true` từ phiên
lab `2090ac7` ngày 2026-06-11 "bật SIEM self-heal user-authorized" và bị bỏ quên chưa rollback —
xem post-mortem). `OMNI_SIEM_SUGGEST_ONLY=true` (advisory-only, đã revert). `OMNI_AUTONOMY_TIER`
override đã gỡ khỏi Deployment env — tier hiệu lực nay chỉ đến từ Redis cache/PG theo đúng invariant
`resolve_tier`. Precedence xác nhận: ConfigMap `omni-worker-configmap` (default an toàn) < Deployment
`env:` override (đã dọn) < Redis cache (nguồn hiệu lực thật cho tier).

### Retired compatibility artifacts
`omni-analyst`, `omni-core`, `omni-executor`, `omni-prober`, `omni-worker` — manifest đã bị xóa khỏi
git từ commit `915e509` (split-role consolidation) nhưng object Deployment (`replicas=0`) vẫn còn sót
trong cluster đến 2026-07-02 → đã `kubectl delete` dứt điểm (kèm Service `omni-analyst` orphan, đã
`git rm k8s/services/omni-analyst-service.yaml`). `omni-siem-bridge`, `omni-hitl-dispatcher`,
`omni-evidence-adapter` — manifest VẪN còn trong git (`replicas: 1`, target `make deploy-siem-stack`,
có PDB riêng) nhưng đang scale 0 trong lab hiện tại vì `omni-brain-go` đã đảm nhiệm SIEM correlation
cho kịch bản lab này; đã annotate `omni.io/status=scaled-down-intentional` + owner + sunset condition
trên cả 3 — KHÔNG coi là zombie, KHÔNG xóa.

RAG `omni:rag:sop` HLEN=1019 (khớp MEMORY.md). Redis AOF enabled. Knowledge pipeline active
(`omni-knowledge-evidence`). **Cập nhật 2026-07-03** (đã xác minh lại `scripts/kafka_ensure_omni_topics.sh`):
claim "mọi topic PartitionCount=1" đã lỗi thời — `omni-knowledge-evidence` dùng 3 partitions
(dòng ~47-52, comment "Enforcing omni-knowledge-evidence config... partitions=3"), topic SIEM dùng
6 partitions; phần lớn topic còn lại (`omni-diagnostic-evidence`, `omni-audit-chain`, `omni-alerts`,
...) vẫn 1 partition/1 replication-factor (lab single-broker, chưa phải rủi ro data-loss vì
`auto_offset_reset="earliest"` + không multi-broker failover). Không còn là drift tài liệu, chỉ là
throughput headroom thấp cho lab hiện tại — không cần sửa gấp.

### VM/Agent truth (2026-07-02, giải quyết)
Access method đúng là `orb -m <machine> <command>` (không phải SSH thẳng tới IP). Cả 3 VM lab đã
audit trực tiếp: `cust-edge` (nginx :80, NFS, portmapper), `cust-app` (app :8080), `cust-db` (MySQL
:3306 + Redis :6379, cả hai bind localhost-only). Agent chạy qua systemd unit
`omni-remote-agent.service` (tên khác `aoip-agent` — đừng tìm nhầm unit).

### Productization Iteration 1 — System Twin (2026-07-02)
`omni:aoip:system_model:{tenant}` trống hoàn toàn dù O1/O2A/O2B claim DONE — root cause là
**deployment drift**: image `multi-agent-system:latest` chưa `make docker-worker` rebuild kể từ
`1bc6292`, pod chạy thiếu hẳn `_project_into_system_model`. Đã rebuild+redeploy (digest
`c2d433daac77...`). Twin nay có dữ liệu thật cho 2/3 host (cust-edge, cust-db) — `cust-app` chưa có
discovery probe nào (chỉ metrics/log probe), là bottleneck kế tiếp. Chi tiết + capability matrix:
`docs/product/PRODUCT_PROOF.md`. Bài học: `test pass + push` KHÔNG chứng minh đã deploy — luôn
`hasattr()` check module trong pod đang chạy trước khi coi slice DONE.

## COMMUNICATION

- **Code first.** Viết code ngay, không hỏi lại.
- **Giải thích tối đa 100 chữ** khi thật sự cần.

## AUTONOMY RULES

EXPLORE → PLAN → VERIFY → GIT (chỉ khi được chỉ thị). CI/CD loop tự động trong Lab. `#` để ghi rule mới vào đây.
