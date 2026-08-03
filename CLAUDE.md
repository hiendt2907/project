# CLAUDE.md

> **TRƯỚC MỌI TASK: đọc `MEMORY.md` + `docs/CODEBASE.md`.** Bản đồ nhanh ở memory `project_architecture_map`; chi tiết file-level ở `docs/CODEBASE.md`.

**Omni** — async-first multi-agent SRE automation. **Omni là NÃO** (trung tâm điều hành duy nhất), **Remote Agent là CHÂN/TAY/MẮT** trên hạ tầng khách hàng. Omni phán bất thường bằng baseline nó tự học, phân loại theo **9 domain**, rồi tự điều tra nhiều lượt. Split Kafka pipeline thực thi khắc phục. Ranh giới sở hữu/quyết định/dữ liệu giữa hai bên: xem mục **NÃO vs THÂN** ngay dưới đây — đọc trước khi sửa bất cứ gì chạm cả hai phía, nhầm lẫn ở đây từng gây sai lệch tài liệu thật (audit 2026-08-03).

## NÃO vs THÂN — Omni (nội bộ) vs Remote Agent (khách hàng)

Xác nhận qua audit code + cluster K8s thật + VM thật + log thật (2026-08-03). Ba trục, không được gộp:

1. **Sở hữu hạ tầng** — Omni = chạy trong K8s namespace `multi-agent` (`omni-fullstack`, `omni-gateway`, `omni-onboarding` + Postgres/Redis/Kafka nội bộ) = hệ thống CỦA OMNI. Remote Agent = process `python -m remote_agent.agent` (code ở `src/remote_agent/`), systemd unit `omni-remote-agent.service`, chạy TRÊN server/VM khách hàng (lab: cust-edge/cust-app/cust-db qua `orb -m <machine>`) — KHÔNG phải một phần của cluster Omni dù code sống chung 1 repo.

2. **Quyền quyết định — "agent đề xuất, Omni quyết"**, KHÔNG phải "agent chỉ thu số" cho mọi domain:
   - Chỉ domain `os_host` (`src/remote_agent/collectors/system.py`) luôn gửi `result="OBSERVED"` thuần số, không tự phán.
   - 5 domain còn lại — `database/storage/service/network/application` — agent TỰ TÍNH verdict FAILED/PASSED bằng ngưỡng tĩnh hardcode ngay trên host khách trước khi gửi (vd `collectors/database.py`: threads>500/slow>100/repl_lag>300; `collectors/storage.py`: disk >95% critical/>90% warn; `collectors/network.py`: cổng đóng→FAILED; `collectors/services.py`: unit failed/stopped→FAILED). Đây là đề xuất thô, KHÔNG phải phán quyết cuối.
   - Omni luôn là nơi quyết định CUỐI CÙNG: `knowledge_pipeline.py` ghi đè cứng `result="FAILED"` khi nâng cấp thành `ANOMALY`; `assess_domain_severity` phán mức nghiêm trọng; `remote_host_baseline.py` tự học baseline + z-score cho `os_host`. Remote Agent không bao giờ tự dispatch mutate hay tự chốt mức khẩn cấp cuối cùng.

3. **Dữ liệu ở lại đâu (`INV_DATA_RESIDENCY`)** — nội dung tài liệu/log khách hàng chỉ lên Omni dưới dạng hash + metadata (`pkg/onboarding/discovery_doc.py` hash-on-arrival nếu agent gửi raw; `services/knowledge/document_store.py::ingest_customer_knowledge()` metadata-only ≤2000 chars). Số đo vận hành thuần (cpu/mem/disk/latency) KHÔNG thuộc phạm vi residency — đó là số đo, không phải dữ liệu khách.

## Context Hygiene

- Mỗi session chỉ có một deliverable chính.
- Repository là source of truth; conversation history không phải source of truth.
- Sau mỗi checkpoint quan trọng, cập nhật `docs/handoffs/CURRENT_SESSION.md`.
- Báo cáo checkpoint tối đa 20 dòng: result, changed files, verification, blocker, next step.
- Không lặp lại toàn bộ lịch sử dự án.
- Khảo sát rộng phải dùng subagent và chỉ trả kết luận ngắn.
- Trước khi chuyển milestone hoặc `/clear`: cập nhật handoff và engineering artifacts bị ảnh hưởng (dùng `/prepare-clear`).
- Session mới phải kiểm tra Git state và handoff trước khi tiếp tục. Session hooks tự nạp ngữ cảnh; chi tiết ở `docs/engineering/claude-session-automation.md`.

## DIAGNOSTIC FLOWS — 9 DOMAIN (4 lane đã bị bỏ, 2026-07-30)

Trục phân loại là **9 domain canonical** (`src/pkg/domain/taxonomy.py`), không còn 4 lane.
Lý do: lane là thuộc tính của một ALERT, và 4 lane không diễn đạt được
network/storage/database/service/hardware. Kế hoạch + bằng chứng:
`plans/lane-to-domain-and-omni-decides-2026-07-30.md`.

⚠️ **"lane" là BA trục khác nhau cùng tên. Chỉ trục A bị bỏ.** Đọc khối cảnh báo trong
`pkg/domain/taxonomy.py` trước khi chạm bất cứ gì có chữ `lane`:
- **A** `envelope.lane` (`SYS_RESOURCE|SYS_HARD_FAIL|APP_HTTP|SIEM_SECURITY`) → đang bỏ,
  chỉ giữ để đọc dữ liệu lịch sử qua `lane_to_domain()`. `SYS_HARD_FAIL` → `unknown` CỐ Ý
  (nó gánh 4 domain).
- **B** `proof_lane` (`resource|state|app_log`, `VALID_PROOF_LANES`) = *cần bằng chứng vật
  lý loại nào để mở cổng*. Lái `ERR_REA_NO_PHYSICAL_PROOF` + `LANE_BADGE` Telegram.
  **KHÔNG gộp vào domain.**
- **C** `proactive|reactive` (`llm_semaphore`) = pool đồng thời LLM. Không liên quan.

| Domain | Ai phát hiện | Trạng thái đã kiểm bằng lỗi thật (2026-07-30) |
|---|---|---|
| `os_host` | `remote_host_baseline.py` (3σ, Omni phán) · `three_sigma.py` in-cluster | ✅ `decided_by=omni_baseline` z=3.739 → critical → 8 lượt ReAct |
| `database` | `collectors/database.py` (dò cổng/health) | ✅ critical → diagnosis loop |
| `service` | `collectors/services.py` | ✅ unit `failed` **và** unit vừa chuyển active→inactive (dừng sạch) |
| `kubernetes` | `os_state_validator.py`, probe K8s | ✅ |
| `storage` | `collectors/storage.py` · metric `disk_percent` | ✅ ngưỡng: 95% critical / 90% warn (94% ra `INCONCLUSIVE` là ĐÚNG thiết kế, không phải bug) |
| `application` | `collectors/logs.py`, `log_surge_probe.py` | ⚠️ chỉ đạt urgency `medium` |
| `network` | `collectors/network.py` (MỚI) | ✅ cổng lắng nghe vừa đóng → `NetworkListenerLost`; verified `tcp/80` trên VM |
| `security` | `siem_reasoning.py`, FinGuard | ❌ chưa kiểm được trong lab |
| `hardware` | — | ❌ không kiểm được trên OrbStack (không có cảm biến) |

**Ai phán "bất thường": OMNI, không phải agent.** Agent gửi `METRIC_SAMPLE`
(`result="OBSERVED"`), Omni dựng baseline và quyết định trong
`knowledge_pipeline._handle_metric_sample` theo thang `ConfidenceLevel`
(STATIC_GUARD → ngưỡng tĩnh tại Omni · ASSISTED/AUTONOMOUS → z-score). Nâng cấp thành
`ANOMALY` **phải** dùng `result="FAILED"` — `assess_domain_severity` so đúng chuỗi đó để
lên critical, thiếu là chết lặng ở Stage 4. Nguồn phán ghi ở `omni_decision.decided_by`.

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
| `full` | tất cả: evidence, actions, feedback, kpi, knowledge, siem-chains, siem-correlation (port Python của brain-go, gate `OMNI_SIEM_CORRELATION_ENABLED`), tier | ✅ pod `omni-fullstack` |
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
- `OMNI_AUTO_EXECUTE_ENABLED=false` — master kill-switch (fail-closed) tại **ConfigMap**
  `omni-worker-configmap` (default an toàn). Deployment env: có thể override `true` — hiện đang
  override cho lab, xem "Kill-switch" trong DEPLOYMENT STATE để biết giá trị hiệu lực thật.
- **CRAT Fail-Closed**: `write_audit_block()` MUST succeed trước Telegram emit / action dispatch.
- `kafka_evidence_loop` dùng `auto_offset_reset="earliest"` — KHÔNG đổi thành `latest`.
- `omni-audit-chain` topic cần message key (compact policy).
- `INV_NO_RESTART_ON_BROKEN_SPEC` · `INV_READ_BEFORE_MUTATE` · `INV_NAMESPACE_ISOLATION` · `ERR_REA_NO_PHYSICAL_PROOF` · `ERR_GOV_UNAUTHORIZED_MUTATION`
- `INV_KNOWLEDGE_NOT_ALERT` — **nới có kiểm soát 2026-07-30**: `METRIC_SAMPLE` nay ĐƯỢC phân
  tích (baseline + phát hiện lệch, thuần số, KHÔNG LLM); chỉ khi lệch mới nâng thành
  `ANOMALY` và vào pipeline đầy đủ. Một mẫu bình thường vẫn **không** gọi LLM, **không** tạo
  incident. Có dedup `omni:knowledge:promoted:{tenant}:{host}:{metric}` TTL 600s.
- `INV_DATA_RESIDENCY`: tài liệu khách hàng chỉ lưu metadata trên Omni (file_id + summary
  ≤2000 chars). Metric **số** (cpu/mem/disk) KHÔNG thuộc phạm vi này — đó là số đo, không
  phải dữ liệu khách.
- Catalogue lệnh chẩn đoán **fail-closed ở tầng LOAD**: nạp lỗi ⇒ từ chối MỌI lệnh. Đường
  dẫn mặc định phải resolve được ở **cả hai layout** (repo `src/pkg/...` và bundle
  `/opt/omni-remote-agent/pkg/...`) và **cả hai định dạng** (`.yaml` repo, `.json` bundle —
  host khách không có PyYAML). Xem `_default_catalog_candidates()`.
- Domain do collector **tự khai** thắng mọi suy đoán: luôn truyền `domain_hint=` vào
  `detect_domain()`. Bỏ sót là để cascade nội dung gán sai lĩnh vực (đã trả giá).
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
- **LLM**: Ollama `qwen3:8b` (active, đổi từ `qwen2.5-coder:7b` 2026-08-03 — xem comment
  `k8s/deployments/omni-worker-configmap.yaml`: đã thử `qwen3.6:27b` trước, revert vì
  llama-server ăn 300%+ CPU; `qwen3:8b` gần footprint `qwen2.5-coder:7b` cũ, thế hệ mới hơn)
  + `nomic-embed-text:latest` (768-dim). Host: `host.orb.internal:11434`.
- **DB**: PostgreSQL `omni_admin` schema (**32 bảng thật** — xác nhận qua `pg_tables` trực tiếp
  2026-08-03, có `migration_0014_state` nghĩa là ≥14 migration đã chạy, không phải 4; đừng tin số "19
  bảng"/"migration 000{1..4}" nếu thấy ở tài liệu cũ khác), migration tự động lúc worker khởi động qua
  `run_migrations()` cho role `full/analyst/onboarding` nếu
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

`OMNI_WORKER_ROLE` · `OMNI_ENV_MODE` (lab|prod) · `OMNI_KAFKA_BOOTSTRAP_SERVERS` · `OMNI_REDIS_URL` · `OMNI_OLLAMA_BASE_URL` · `OMNI_AUTO_EXECUTE_ENABLED` (default false) · `OMNI_LLM_NUM_CTX` (default 8192) · `OMNI_KAFKA_TOPIC_KNOWLEDGE_EVIDENCE` (default omni-knowledge-evidence) · `OMNI_AUDIT_PRIVATE_KEY_PATH` · `OMNI_TENANT_APIKEYS` (tenant_id:key,...) · `OMNI_GATEWAY_API_KEY` · `OMNI_EXECUTOR_FORCE_NSENTER` (bool, đang `true` trên omni-fullstack — khi bật, `autonomous_execute.py` chặn cứng `ERR_GOV_UNAUTHORIZED_MUTATION` cho mọi mutate tool KHÁC `kubectl_cluster`; xem `settings.py::omni_executor_force_nsenter`, `kubectl_cluster.py::_force_nsenter()`)

## DEPLOYMENT STATE (2026-07-02, xác minh qua Whole-System Reality Audit + Drift Correction Slice)

### Declared target topology
`omni-fullstack` (role=full) là workload lõi duy nhất được `make deploy-worker` deploy mặc định.
`omni-gateway`, `omni-onboarding` là các Deployment RIÊNG BIỆT, có
manifest/target Makefile riêng — không phải "instance phụ của omni-fullstack".
(`omni-brain-go` từng nằm trong danh sách này — RETIRED 2026-07-22, đã port vào omni-fullstack.)

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
| `omni-onboarding` | `OMNI_WORKER_ROLE=onboarding` — discovery-evidence worker | ✅ **FIX ĐÃ DEPLOY + VERIFY SỐNG 2026-08-03.** Trước fix: crash-loop thật, 15 restart/3h27m, exit 137. Root cause: `src/workers/omni_worker.py` (`_worker_background_tasks`) từng chạy `kafka_discovery_evidence_loop` cho CẢ role=full lẫn role=onboarding, cả hai join CÙNG 1 `consumer_group_onboarding` cố định — 2 member tranh nhau 1 topic 1-partition (`omni-discovery-evidence`), gây rebalance lặp lại mỗi khi 1 trong 2 pod restart (log "Heartbeat failed...rebalancing" xuất hiện ĐỒNG THỜI ở cả 2 pod — bằng chứng quyết định). Lý do cũ ("role=full phải chạy vì khi đó chưa có deployment onboarding riêng") đã lỗi thời từ khi `omni-onboarding` thành Deployment riêng thật. Fix: role=full không còn đăng ký loop này (điều kiện đổi `role in ("full","onboarding")` → `role == "onboarding"`). `make deploy-fullstack` + `kubectl rollout restart deployment/omni-onboarding` đã chạy — pod mới `Restart Count=0`, `grep -c rebalancing` = 0 trong 3 phút quan sát sau deploy (trước đó lặp lại mỗi ~66s). Test `tests/test_worker_role_discovery_consumer.py` cập nhật, 83 test liên quan xanh. |
| ~~`omni-brain-go`~~ | **RETIRED 2026-07-22** — đã port sang Python (`src/services/siem_correlation/` + loop `kafka_siem_correlation_loop` trong `omni-fullstack`, group `omni-siem-correlation`, cùng Redis key layout `corr:*`). Parity test PASS 2/2 (output Python == Go từng field trên cùng input) trước khi xoá Deployment + manifest. KHÔNG bật lại brain-go song song với `OMNI_SIEM_CORRELATION_ENABLED=true` — 2 engine sẽ đua trên cùng key `corr:*` và double-emit chains. Lưu ý parity: khi các event đến trong CÙNG 1 giây, sequence-score phụ thuộc thứ tự tie (Go sort không ổn định = ngẫu nhiên; Python ổn định newest-first = bảo thủ hơn) — hành vi vốn có của cả 2 engine, không phải bug port. | Deployment đã xoá |
| `redis-0`, `kafka`, `omni-postgres-0`, `redis-exporter`, `aoip-dex`, `aoip-provider-*`, `aoip-tenant-*` | portal/hạ tầng phụ trợ (provider/tenant portal là portal thật duy nhất, `omni-ui` đã retired) | Running |

### Kill-switch — effective value đã xác minh qua MCP (`mcp__kubernetes`, read-only) trên pod thật

**Cập nhật 2026-08-03** (describe pod `omni-fullstack` trực tiếp qua MCP, không phải cache tài
liệu): ConfigMap `omni-worker-configmap` vẫn giữ default an toàn
(`OMNI_AUTO_EXECUTE_ENABLED="false"`, `OMNI_SIEM_SUGGEST_ONLY="true"`) — nhưng Deployment
`omni-fullstack` hiện có **override `env:` sống** đè lên default đó:

```
OMNI_AUTO_EXECUTE_ENABLED:    true
OMNI_AUTO_ROLLBACK_ENABLED:   true
OMNI_SIEM_SUGGEST_ONLY:       false
OMNI_LAB_AUTO_EXECUTE_AGENTS: staging-sim_cust-app,staging-sim_cust-edge,staging-sim_cust-db
```

Đây là **chủ đích** (không phải drift kiểu 2026-06-11) — chỉ mở autonomous mutate cho đúng 3 VM
lab qua allowlist `OMNI_LAB_AUTO_EXECUTE_AGENTS`, đúng cơ chế blast-radius control mà
`auto_recovery_bridge.dispatch_if_eligible()` đã kiểm (xem Đ8 trong handoff). Namespace K8s vẫn
giới hạn `OMNI_AUTONOMOUS_ALLOWED_NAMESPACES=multi-agent`. Claim cũ "đã revert 2026-07-02, gỡ khỏi
Deployment env" chỉ đúng tại thời điểm đó — **không còn đúng hiện tại**, đã lỗi thời.

`OMNI_AUTONOMY_TIER` override vẫn không có trên Deployment env — tier hiệu lực vẫn chỉ đến từ Redis
cache/PG theo đúng invariant `resolve_tier`. Precedence: ConfigMap (default an toàn) < Deployment
`env:` override (nay CÓ tồn tại, có chủ đích, scoped) < Redis cache (nguồn hiệu lực thật cho tier
riêng).

`OMNI_TELEGRAM_POLLING_ENABLED`: ConfigMap `"false"` nhưng Deployment env override `"true"` —
drift đã biết, carry sang từ Đ7/Đ8, vẫn chưa xử lý (không liên quan tới lô kill-switch trên).

**Thêm 1 biến sống chưa từng ghi ở đây trước audit 2026-08-03**: `OMNI_EXECUTOR_FORCE_NSENTER=true`
trên Deployment `omni-fullstack`. Đây là một lớp gate ĐỘC LẬP với kill-switch/allowlist ở trên —
khi bật, `autonomous_execute.py` (dòng ~125-133) chặn cứng `ERR_GOV_UNAUTHORIZED_MUTATION` cho bất
kỳ mutate tool nào KHÁC `kubectl_cluster`; `kubectl_cluster.py::_force_nsenter()` bọc lệnh qua
`nsenter` thay vì exec thẳng trong container. Với giá trị hiện tại, executor trên thực tế bị thu hẹp
chỉ còn 1 con đường mutate.

**Gap vận hành ĐÃ FIX + DEPLOY + VERIFY SỐNG 2026-08-03** (trước đó là gap đang sống): agent
`staging-sim_cust-app` — 1 trong đúng 3 agent nằm trong `OMNI_LAB_AUTO_EXECUTE_AGENTS` ở trên — bị
Gateway trả **401 Unauthorized trên mọi request**, trong khi `staging-sim_cust-edge`/
`staging-sim_cust-db` đều 200 OK.

Root cause thật (có `kubectl exec` qua Bash local, không chỉ MCP read-only, nên điều tra được tận
gốc): `sha256()` của API key thật trên VM khớp **tuyệt đối** với `key_hash` trong
`omni_admin.agent_credential` (`status='active'`) — dữ liệu hoàn toàn đúng, không phải sai
credential. `kubectl logs --since=48h` phát hiện dòng quyết định:
`omni-gateway: admin store init fail: [Errno 111] Connection refused` — gateway khởi động TRƯỚC
khi Postgres sẵn sàng (cùng lớp race đã biết với Kafka producer ở pod này), thử kết nối
`create_admin_pool()` ĐÚNG 1 LẦN rồi bỏ cuộc vĩnh viễn (`app.state.admin_repo` treo `None` suốt
vòng đời pod, không có retry). Vì `_resolve_agent_credential()` trả `None` ngay khi `admin_repo is
None` (`api.py`), MỌI request dùng per-agent credential (chỉ `cust-app` dùng nhánh này — 2 agent
kia dùng tenant-shared key nên không đụng `admin_repo`) đều 401 bất kể key đúng hay sai.

Fix: thêm `_connect_admin_pool_with_retry()` (`src/gateway/api.py`) — bounded retry 5 lần, backoff
1s→10s quanh `create_admin_pool()`, tách hàm riêng để test được (`tests/test_gateway_admin_pool_retry.py`,
4 test). `make deploy-gateway` đã chạy — log pod mới: `"admin config store ready (omni_admin)"`,
và `staging-sim_cust-app` ngay sau đó register/evidence/commands đều 200 OK (verify trực tiếp qua
`kubectl logs`, không suy đoán). Auto-execute allowlist nay hoạt động đủ 3/3 agent thật.

### PUBLIC PLANE — app.omnisre.xyz (2026-07-29, đang sống trên Internet)

Omni đã public thật qua Cloudflare Free, core vẫn chạy trên MacBook. Không VPS, không
mở port router. `bash cloudflare/tunnel/verify.sh` → 17 PASS / 0 FAIL / 0 SKIP.

```
browser → Cloudflare Access (chỉ danghien2907@gmail.com, one-time PIN)
        → Tunnel `omnisre` (LaunchAgent com.omnisre.cloudflared)
        → Traefik 192.168.139.2:80  → Ingress `omnisre-public-console`
             /auth, /api/provider/v1 → aoip-provider-portal-public
             /dex                    → aoip-dex-public
             /                       → aoip-provider-web-public
```

**INV_PUBLIC_PLANE_ISOLATED (vi phạm = bug).** Mặt public có auth plane RIÊNG. Lab
`provider.ai-agent.local` / `aoip-dex` **KHÔNG được đổi một biến nào** — đặc biệt
`AOIP_OIDC_PROVIDER_ISSUER` và `issuer` trong ConfigMap `aoip-dex-config`. Lý do:
`verify_id_token` so `iss` bằng chuỗi tuyệt đối (`oidc.py:104`), đổi issuer là breaking
cho lab. `verify.sh` nhóm A canh đúng bất biến này. Quyết định đầy đủ: ADR 0001.

Frontend **cũng** phải tách, không chỉ backend: `aoip-provider-web` có
`AOIP_BACKEND_URL` cứng trỏ backend lab và server component gọi qua đó kèm cookie
(`ui/packages/api-client/src/index.ts:20`) — dùng chung sẽ khiến traffic public chui
qua backend lab và **chạy im lặng** vì `portal:session:` chung Redis.
`aoip-provider-web-public` dùng **cùng image**, chỉ khác một biến env.

Đồng bộ code local → public:
```bash
make sync-public-ui | sync-public-backend | sync-public | sync-public-all
```
Bắt buộc dùng target này, KHÔNG `kubectl rollout restart` tay: `imagePullPolicy:
IfNotPresent` + tag `:latest` ⇒ restart không build gì. Script build rồi so `imageID`
mọi pod Running với image local. Lab và public dùng CHUNG tag image — mặc định chỉ
restart public, `--with-lab` mới đụng lab.

`api.omnisre.xyz` / `agent.omnisre.xyz` **CHƯA public, cố ý.** Mở là phase riêng; chặn
kỹ thuật phải xử lý trước: `/auth` và `_require_api_key` chưa có rate limit tầng app.

Không nằm trong git (mất là phải tạo lại tay): Secret `aoip-dex-public-config` +
`aoip-provider-portal-public-secret`, role `platform_owner` seed thủ công trong Redis,
credentials tunnel, LaunchAgent plist.

⚠️ `client_secret` PHẢI sinh bằng `openssl rand -hex`, không phải `-base64`: `+` bị Dex
URL-decode thành dấu cách (RFC 6749 §2.3.1) trong khi `httpx` gửi Basic thô → fail dù
Secret giống hệt. Debug callback OIDC: đọc log lần gọi **ĐẦU TIÊN**; các lần sau luôn là
400 "invalid or expired state" do state one-time, không phải nguyên nhân.

Tài liệu: `docs/adr/0001-cloudflare-pages-tunnel-local-core.md` ·
`docs/deployment/cloudflare-macbook.md` · `docs/runbooks/cloudflare-public-access.md`

### Retired compatibility artifacts
`omni-analyst`, `omni-core`, `omni-executor`, `omni-prober`, `omni-worker` — manifest đã bị xóa khỏi
git từ commit `915e509` (split-role consolidation) nhưng object Deployment (`replicas=0`) vẫn còn sót
trong cluster đến 2026-07-02 → đã `kubectl delete` dứt điểm (kèm Service `omni-analyst` orphan, đã
`git rm k8s/services/omni-analyst-service.yaml`). `omni-siem-bridge`, `omni-hitl-dispatcher`,
`omni-evidence-adapter` — manifest VẪN còn trong git (`replicas: 1`, target `make deploy-siem-stack`,
có PDB riêng) nhưng đang scale 0 trong lab hiện tại vì SIEM correlation đã chạy trong
`omni-fullstack` (trước 2026-07-22 là `omni-brain-go`, nay là loop `kafka_siem_correlation_loop`);
đã annotate `omni.io/status=scaled-down-intentional` + owner + sunset condition
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
