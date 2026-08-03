# Omni v2 — Architecture Redesign

**Vai trò:** Chief Software Architect review · **Ngày:** 2026-08-03 · **Trạng thái:** Đề xuất (chưa implement, không code)
**Nguồn sự thật:** code thật (`src/`), manifest K8s thật (`k8s/deployments/`), Makefile thật — KHÔNG dựa vào `docs/CODEBASE.md` (đã xác nhận lỗi thời, xem Phase 1.7) hay `CLAUDE.md` làm ground truth, chỉ dùng chúng để đối chiếu drift.
**Mục tiêu:** không phải "sửa bug", mà là kiến trúc để Omni còn bảo trì được sau 5 năm.

---

## Cách đọc tài liệu này

10 phase theo đúng yêu cầu. Phase 1 là sự thật quan sát được (bắt buộc đọc trước khi tranh luận bất kỳ đề xuất nào ở Phase 2+). Phase 2-7 là thiết kế. Phase 8-10 là lộ trình + quyết định + chấm điểm.

**Nguyên tắc bất biến xuyên suốt:** mọi capability đã verify runtime (Knowledge Pipeline, Known Fix Reflex, Remote Agent, Kill Switch, Safety Gates, Taxonomy, Executor, Gateway, Onboarding, Learning Pipeline, Evidence Pipeline, Postgres state, Kafka event flow, multi-tenant/multi-agent) phải sống sót qua redesign. Implementation của chúng được phép đổi hoàn toàn.

---

## PHASE 1 — Kiến trúc v1 thật (reverse-engineered)

### 1.1 Dependency graph (module → module, xác nhận bằng import thật)

```
remote_agent/  ──▶ pkg/domain, pkg/diagnostics          (SẠCH — không phụ thuộc gateway/workers)

gateway/       ──▶ pkg/*, services/admin_config,
                   services/agent_command_ledger,
                   services/case_ledger                  (SẠCH — 0 import workers/, đúng invariant)

workers/       ──▶ pkg/*, services/*, anomaly/*, rag/*   (đúng chiều — orchestration phụ thuộc domain)

pkg/           ──▶ workers/*  ⚠️ VI PHẠM             (reasoning/deterministic_mutate_from_evidence.py,
                                                        reasoning/sre_output.py, autonomy/gate.py,
                                                        executor/__init__.py, executor/mutate_governance.py
                                                        — import workers.* Ở TOP-LEVEL, không chỉ lazy)

anomaly/       ──▶ workers/sdk_service_tools          ⚠️ VI PHẠM
rag/           ──▶ workers/metrics_exporter           ⚠️ VI PHẠM

services/analyst, services/audit_ledger ──▶ pkg/*      (đúng chiều)
```

**Phát hiện quan trọng nhất của Phase 1:** `src/pkg/` được thiết kế và tự nhận (comment trong chính code) là "lớp thấp nhất, gateway/workers đều được phép phụ thuộc vào nó, nó không được phụ thuộc ngược" — nhưng thực tế **`pkg/`, `anomaly/`, `rag/` đều import ngược `workers/`**, một phần ở top-level (không chỉ lazy-import che giấu). Đây là **circular dependency thật**, không phải nghi ngờ lý thuyết. Nó chính là lý do sâu xa khiến tách `workers/` thành nhiều service độc lập (Phase 3) khó thực hiện — bất kỳ ai cũng có thể import `workers` từ "lớp domain" mà không có gate nào chặn.

### 1.2 Runtime interaction graph (dữ liệu chảy thật)

```
Remote Agent (VM khách)                  Omni cluster
─────────────────────                    ────────────────────────────────────────
collectors (9 domain)
  → agent.py loop (60s)
  → emitter.py (register/evidence/       gateway/routes/agent_webhook.py
     poll_commands, retry 1/2/4s)   ───▶   → dedup fingerprint
                                            → ANOMALY → omni-diagnostic-evidence
                                            → khác     → omni-knowledge-evidence
                                                              │
                                    ┌─────────────────────────┘
                                    ▼
                        workers/knowledge_pipeline.py
                        (_handle_metric_sample: baseline 3σ thuần số,
                         KHÔNG LLM cho mẫu bình thường)
                                    │ lệch → promote ANOMALY (result=FAILED)
                                    ▼
                        workers/evidence_consumer.py (3578 dòng)
                        → services/analyst (RAG recall → LLM nếu recall<0.75)
                        → pkg/reasoning (AnalystAdvisory: WHAT/WHO/WHY/HOW-TO + forecast)
                                    │
                                    ▼
                        pkg/autonomy/gate.py + services/audit_ledger (CRAT, fail-closed)
                                    │
                        ┌───────────┼────────────┐
                        ▼           ▼             ▼
                     SUGGEST     EXECUTE        HITL
                  (Telegram)  (kafka_actions   (chờ người duyệt)
                              _consumer.py →
                              autonomous_execute.py
                              → k8s_tools / kubectl_cluster
                              → omni-action-feedback)
                                    │
                                    ▼
                        workers/autonomous_feedback_loop.py (1811 dòng)
                        → re-evaluate, học lại (confidence, KPI)
```

Đường phụ (không qua LLM): `remote_agent → gateway/routes/agent_commands.py` (hàng đợi lệnh chẩn đoán, poll 5s) — kênh riêng biệt hoàn toàn với evidence flow, dùng để Omni chủ động "hỏi" agent chạy probe.

### 1.3 Deployment graph (K8s thật, `k8s/deployments/*.yaml` + Makefile)

| Deployment | Vai trò | Build qua Makefile? |
|---|---|---|
| `omni-fullstack` (role=full) | Chạy **8+ domain loop** trong 1 pod (evidence, actions*, feedback, kpi, knowledge, siem-chains/correlation, tier, telegram, proactive...) | `make deploy-fullstack` |
| `omni-onboarding` (role=onboarding) | Discovery-evidence consumer riêng, tách khỏi full 2026-08-03 sau khi phát hiện 2 role tranh cùng consumer-group | `make deploy-fullstack` (chung target, khác role) |
| `omni-gateway` | FastAPI ingress, KHÔNG import `workers/` | `make deploy-gateway` |
| `omni-siem-bridge` / `omni-hitl-dispatcher` / `omni-evidence-adapter` (+ bản `-production` song song) | Manifest còn trong git, KHÔNG được Makefile nào apply thật (trừ 3 bản "lab" qua `deploy-siem-stack`) — logic thật đã port vào `omni-fullstack` (`kafka_siem_correlation_loop`) | Không rõ ràng — dead weight cần dọn |
| `omni-postgres`, `redis` (Stack) | Data plane | StatefulSet trực tiếp |
| `omni-agent` (DaemonSet) | **Không phải Remote Agent thật** — đây là artifact khác (`ghcr.io/omni/omni-agent`), cần làm rõ có phải leftover không |

**Smell xác nhận bằng Makefile thật:** `k8s/services/omni-analyst-service.yaml` được Makefile tham chiếu nhưng **không tồn tại trong git** — dangling reference sống trong build script. ConfigMap tên thật `omni-worker-config` (không phải `omni-worker-configmap` như tài liệu cũ ghi). Override lab (`omni-fullstack-autoexec-lab.yaml`) áp bằng `kubectl patch` tay, không nằm trong pipeline Makefile — nghĩa là **trạng thái hiệu lực của kill-switch không tái tạo được từ git**, chỉ tồn tại trong cluster.

### 1.4 Service/domain map (capability → file thật)

| Capability | File/module sở hữu (thật) |
|---|---|
| Observation (9-domain collectors) | `remote_agent/collectors/*` |
| Baseline/anomaly (os_host, K8s) | `anomaly/three_sigma.py`, `anomaly/remote_host_baseline.py`, `anomaly/sigma_calibrator.py` |
| Knowledge routing + promote | `workers/knowledge_pipeline.py`, `services/knowledge/document_store.py` |
| Reasoning/Advisory contract | `pkg/reasoning/analyst_advisory_schema.py`, `services/analyst/*` |
| RAG/Memory | `rag/redis_brain.py`, `rag/redis_vector_store.py` |
| Governance/Policy | `pkg/autonomy/*`, `pkg/executor/mutate_governance.py` |
| Compliance ledger (CRAT) | `services/audit_ledger/*` (hash-chain + Ed25519 — bounded context SẠCH NHẤT trong toàn bộ v1) |
| Execution | `workers/autonomous_execute.py`, `workers/kafka_actions_consumer.py`, `workers/k8s_tools.py` |
| Verification | `workers/verify_reconcile.py`, `workers/kb_verifier.py` |
| Learning | `workers/autonomous_feedback_loop.py` (1811 dòng), `anomaly/sigma_calibrator.py` |
| Agent lifecycle (register/credential/commands) | `gateway/routes/agent_*.py`, `services/admin_config` |
| Control plane config (policy/tier/tenant) | `gateway/routes/autonomy.py` (701 dòng, ~25 route — nhiều sub-domain gộp 1 router) |

### 1.5 Event flow / state flow — sở hữu dữ liệu

| Store | Ai ghi | Ai đọc | Nhận xét |
|---|---|---|---|
| `audit_chain:*` (Redis) | CHỈ `audit_ledger/chain_writer.py` | Nhiều nơi (read-only) | Bounded context ĐÚNG mẫu — 1 writer duy nhất, versioned schema, fail-closed |
| `omni:3sigma:confidence:*` | `anomaly/remote_host_baseline.py` | `workers/knowledge_pipeline.py` | Rõ ràng |
| `trace_orchestrator.state` | **CẢ** `evidence_consumer.py` **VÀ** `autonomous_feedback_loop.py` | — | **Hidden coupling xác nhận thật** — 2 loop khác nhóm role (`analyst`) chia sẻ ngầm 1 store không qua interface tường minh; đổi schema 1 bên sẽ âm thầm phá bên kia |
| Redis key namespace nói chung | Không có schema registry, mỗi module tự đặt prefix (`omni:kpi:*`, `omni:dlq:*`, `3sigma:remote:*`, `omni:agent:cmd:*`...) | — | Không sai nhưng không có nơi trung tâm liệt kê "ai sở hữu key gì" — rủi ro va chạm khi thêm domain mới |

### 1.6 God objects xác nhận bằng số dòng thật

| File | Dòng | Vấn đề |
|---|---|---|
| `workers/evidence_consumer.py` | 3578 | Riêng 1 file gánh cả diagnostic dispatch + reasoning trigger |
| `workers/settings.py` | 1927 | Mọi cấu hình toàn hệ thống trong 1 class — mini god-object |
| `workers/autonomous_feedback_loop.py` | 1811 | Học + re-evaluate + KPI trong 1 file |
| `workers/omni_worker.py` | 1420 | **God object rõ nhất**: vừa là entrypoint, vừa định nghĩa business logic ≥8 domain loop khác nhau ngay trong `_worker_background_tasks`; role-routing bằng if/else rải rác — đã gây ra bug production thật (2 role join chung consumer-group, crash-loop `omni-onboarding`, xem CLAUDE.md Đ12) |
| `gateway/routes/autonomy.py` | 701 | Gộp policy + tier + risk-class + tenant + api-key + HITL — 6 sub-domain, 1 router |
| `gateway/api.py` | 790 | Gộp auth (4 cơ chế) + lifecycle + Prometheus ingest + `/metrics` + forecast regression — không liên quan nhau |

### 1.7 Documentation drift (đã xác nhận, không suy đoán)

- `docs/CODEBASE.md` (2026-07-14) mô tả UI "4-lane status cards", **không nhắc `src/remote_agent/`**, không nhắc 9-domain — trong khi `docs/architecture/SYSTEM_DIAGRAMS.md` (2026-08-02, mới nhất) đã khẳng định 9-domain là canonical. `plans/lane-to-domain-and-omni-decides-2026-07-30.md` tự ghi nhận cần sửa CODEBASE.md nhưng chưa làm.
- 11 file "hiến pháp AOIP" (`CAPABILITY_MODEL.md`, `DOMAIN_MODEL_autonomous_sre.md`, ...) đứng yên từ 2026-06-29 — không phản ánh các thay đổi runtime sau đó (9-domain, knowledge pipeline mở rộng, SIEM Python port...).
- 6 "ADR" thật ra nằm trong `docs/architecture/` (không phải `docs/adr/` — chỉ có 1 ADR thật ở đó, về Cloudflare). Không gian đặt tên ADR bị phân mảnh.

**Kết luận Phase 1:** v1 không hỏng — phần lớn capability chạy đúng, có bằng chứng runtime thật (audit trước đó). Nhưng kiến trúc có 3 lỗ hổng cấu trúc nghiêm trọng cần v2 giải quyết tận gốc, không phải vá thêm:
1. **Circular dependency thật** giữa `pkg/anomaly/rag` (domain layer) và `workers` (orchestration layer).
2. **God object `omni_worker.py`** biến role-routing thành nguồn bug lặp lại (đã xảy ra ít nhất 1 lần thật, chi phí là crash-loop production).
3. **Không có ranh giới capability nào được thực thi bằng công cụ** (không lint rule, không CI check cấm import ngược) — ranh giới hiện tại chỉ tồn tại dưới dạng comment "must not import X", dựa vào kỷ luật con người.

---

## PHASE 2 — Bounded Contexts (redesign theo capability, không theo folder)

| Context | Purpose | Public API | Data Ownership | Events Published | Events Consumed | Forbidden Dependencies |
|---|---|---|---|---|---|---|
| **Agent Runtime** (thay `remote_agent/`) | Sensor + effector trên hạ tầng khách hàng | Emit envelope, Poll commands | Local snapshot cache (đĩa, không phải Omni) | `observation.raw`, `discovery.diff` | `command.assigned` | Không được phụ thuộc bất kỳ context nào của Omni cluster (đã đúng ở v1, giữ nguyên) |
| **Observation** | Chuẩn hoá evidence thô → fact có domain | Nội bộ: `normalize(envelope) -> Fact` | Không giữ state lâu dài (stateless transform) | `fact.normalized` | `observation.raw` | Không gọi Reasoning/Execution trực tiếp |
| **Baseline & Anomaly** | Học baseline, tính độ lệch (z-score, confidence) | `evaluate(fact) -> Deviation?` | `confidence:{tenant}:{host}`, `3sigma:*` | `anomaly.detected` | `fact.normalized` | Không gọi LLM, không gọi Execution |
| **Knowledge** | RAG, SOP, tài liệu khách hàng (metadata-only) | `recall(query) -> Snippet[]`, `ingest_doc()` | `omni:rag:*`, `omni:knowledge:doc:*` | `knowledge.updated` | `anomaly.detected` (để tăng confidence) | Không tự quyết định remediation |
| **Reasoning** | LLM + RAG → AnalystAdvisory (WHAT/WHO/WHY/HOW-TO) | `diagnose(anomaly) -> AnalystAdvisory` | Không giữ state lâu (đọc Knowledge, ghi Advisory tạm) | `advisory.proposed` | `anomaly.detected`, `knowledge.updated` | **Read-only tuyệt đối** — không được import Execution (giữ đúng invariant "analyst is read-only" đã có ở v1, nhưng lần này thực thi bằng lint, không chỉ comment) |
| **Governance & Policy** | Tier gate, blast-radius, mutation allowlist, CRAT hash-chain | `authorize(advisory) -> Decision`, `audit(event)` | `audit_chain:*` (giữ nguyên — đây là bounded context tốt nhất của v1) | `decision.made`, `audit.appended` | `advisory.proposed` | Không phụ thuộc Execution (Execution phụ thuộc NÓ, không ngược lại) |
| **Execution** | Mutate hạ tầng, rollback | `execute(decision) -> Outcome` | `omni-actions` outbox | `execution.outcome` | `decision.made` (chỉ khi `decision=EXECUTE`) | KHÔNG được tự đọc Reasoning để "quyết định lại" — chỉ thi hành quyết định đã ký |
| **Verification** | Xác nhận outcome đúng như kỳ vọng | `verify(outcome) -> VerifiedResult` | Không giữ state riêng, ghi vào Learning | `verification.result` | `execution.outcome` | — |
| **Learning** | Cập nhật confidence, KPI, feedback loop | `learn(verified_result)` | KPI Redis ZSET, confidence deltas | `learning.updated` | `verification.result` | Không tự execute lại (chỉ điều chỉnh tham số cho Baseline/Reasoning) |
| **Agent Management** | Đăng ký, credential, lifecycle của từng Remote Agent | REST: register/credential/rotate/commands | Postgres `agent_credential`, Redis registry | `agent.registered` | — | — |
| **Platform Runtime** (thay `omni_worker.py`) | CHỈ orchestrate: đọc config, khởi loop đúng theo capability nào được bật cho role nào — **không chứa business logic của bất kỳ domain nào** | — | Task registry runtime | — | — | Không chứa `if domain == X: ...` — mọi logic domain nằm trong context tương ứng |
| **Control Plane** (thay phần lớn `gateway/`) | Cấu hình chính sách (tier/risk-class/tenant/api-key), API bên ngoài | REST/Webhook | Postgres `omni_admin.*` | `config.changed` | — | Không chứa business logic diagnostic |
| **Data Plane** | Postgres/Redis/Kafka hạ tầng thuần | — | — | — | — | — |

**Nguyên tắc phân chia:** một context chỉ được sở hữu **đúng 1** loại state chính, và publish/consume qua event, không gọi hàm chéo trực tiếp giữa các context khác domain (trừ Governance ↔ Execution là quan hệ lệnh trực tiếp có chủ đích, vì đây là ranh giới an toàn quan trọng nhất hệ thống).

---

## PHASE 3 — Kiến trúc Omni v2 (capability-driven)

```
┌─────────────────────────────── MANAGEMENT PLANE ───────────────────────────────┐
│  Control Plane (Gateway API)  │  Agent Management  │  Governance & Policy       │
└──────────────────────────────────────────────────────────────────────────────┘
                                        │ events (Kafka)
┌─────────────────────────────── KNOWLEDGE PLANE ────────────────────────────────┐
│  Observation → Baseline&Anomaly → Knowledge → Reasoning                        │
└──────────────────────────────────────────────────────────────────────────────┘
                                        │ AnalystAdvisory + Decision
┌─────────────────────────────── EXECUTION PLANE ────────────────────────────────┐
│  Execution  →  Verification  →  Learning                                       │
└──────────────────────────────────────────────────────────────────────────────┘
                                        │
┌─────────────────────────────────── AGENT PLANE ────────────────────────────────┐
│  Agent Runtime (Remote Agent SDK) — chạy trên hạ tầng khách hàng               │
└──────────────────────────────────────────────────────────────────────────────┘
                                        │
┌──────────────────────────────────── DATA PLANE ────────────────────────────────┐
│  Postgres (state)  │  Redis (hot cache + RAG + audit chain)  │  Kafka (event bus)│
└──────────────────────────────────────────────────────────────────────────────┘
```

Khác biệt cốt lõi so với v1: **Platform Runtime không còn là 1 file 1420 dòng chứa business logic** — nó chỉ đọc một **Capability Registry** (khai báo tĩnh: capability nào bật cho role/tenant nào) và khởi task tương ứng bằng cách gọi vào interface của từng bounded context. Thêm capability mới = thêm entry registry + implement interface, không sửa file trung tâm.

---

## PHASE 4 — Canonical Runtime Pipeline

```
Observe → Normalize → Diagnose → Plan → Approve → Execute → Verify → Learn
```

Map trực tiếp từ pipeline THẬT đang chạy (không phải lý tưởng hoá):

| Bước canonical | Bounded context v2 | Hiện trạng v1 (file thật) |
|---|---|---|
| Observe | Agent Runtime + Observation | `remote_agent/collectors/*` → `gateway/routes/agent_webhook.py` |
| Normalize | Observation + Baseline&Anomaly | `pkg/reasoning/domain_signals.py`, `anomaly/three_sigma.py` |
| Diagnose | Reasoning | `services/analyst`, `workers/evidence_consumer.py` (RAG recall + LLM) |
| Plan | Reasoning (đầu ra AnalystAdvisory.proposed_remediation) | `pkg/reasoning/analyst_advisory_schema.py` |
| Approve | Governance & Policy | `pkg/autonomy/gate.py`, `services/audit_ledger` (CRAT fail-closed) |
| Execute | Execution | `workers/autonomous_execute.py`, `k8s_tools.py` |
| Verify | Verification | `workers/verify_reconcile.py` |
| Learn | Learning | `workers/autonomous_feedback_loop.py` |

**Capability KHÔNG khớp thẳng vào pipeline (và tại sao vẫn giữ):**
- **Discovery** — chạy liên tục nền (mỗi giờ), không gắn với 1 incident cụ thể. Nó **nuôi dữ liệu đầu vào** cho bước Observe/Normalize (System Twin) chứ không tự đi qua toàn bộ pipeline. Xử lý như một "background feeder", không phải một luồng incident.
- **Knowledge ingestion** (tài liệu khách hàng qua Telegram) — tương tự, không có "incident" nào kích hoạt, nó chỉ tăng confidence cho Baseline & làm giàu Reasoning. Model đúng: side-channel ghi vào Knowledge context, được Reasoning *đọc* ở bước Diagnose, không phải một bước riêng trong chuỗi.
- **HITL (Human-in-the-loop)** — không phải bước riêng, là **một nhánh giá trị của Approve** (Decision ∈ {AUTO_EXECUTE, HITL_PENDING, REJECTED}), không phải pipeline song song.

---

## PHASE 5 — Dependency Rules (thực thi bằng công cụ, không chỉ comment)

| Module | Allowed imports | Forbidden imports | Enforcement |
|---|---|---|---|
| Agent Runtime | domain taxonomy (đóng gói thành SDK riêng, versioned) | Bất kỳ thứ gì thuộc Omni cluster | Không cùng codebase deploy — SDK là package riêng, cài qua pip/vendor |
| Observation, Baseline&Anomaly, Knowledge, Reasoning (Knowledge Plane) | nhau (theo hướng 1 chiều: Observation→Baseline→Knowledge→Reasoning), Data Plane client | Execution, Governance, Platform Runtime | CI job: `grep -R "from execution" src/knowledge_plane/` phải rỗng; import-linter (Python) rule `knowledge_plane` không phụ thuộc `execution_plane` |
| Governance & Policy | Knowledge Plane (đọc Advisory) | Execution (Execution phụ thuộc nó, không ngược) | import-linter |
| Execution, Verification, Learning (Execution Plane) | Governance (đọc Decision), Data Plane | Reasoning trực tiếp (không được tự ý "hỏi lại" LLM để quyết định) | import-linter + code review checklist |
| Control Plane (Gateway) | Data Plane, Agent Management | Platform Runtime, mọi Knowledge/Execution Plane module | Giữ nguyên invariant v1 (đã đúng), thêm CI check thay vì chỉ dựa comment |
| Platform Runtime | TẤT CẢ (nó là composition root duy nhất) nhưng chỉ qua interface đã khai báo, không import thẳng internal của context khác | — | Interface registry pattern — mỗi context export 1 `register(runtime)` duy nhất |

**Cơ chế thực thi cụ thể:** dùng [`import-linter`](https://pypi.org/project/import-linter/) (Python, đã có sẵn hệ sinh thái, không cần viết mới) khai báo `Contracts` cho từng layer y hệt bảng trên, chạy trong CI — biến "invariant ghi bằng comment" (hiện đang bị vi phạm thật ở `pkg/anomaly/rag`) thành lỗi build cứng nếu ai đó import ngược.

---

## PHASE 6 — Runtime Architecture (Planes)

| Plane | Thành phần v2 | Lý do tách |
|---|---|---|
| **Control Plane** | Gateway (Control Plane API), Agent Management | Đây là bề mặt duy nhất tiếp xúc trực tiếp với khách hàng/agent qua HTTP — cần scale/patch độc lập với tốc độ nhanh hơn (API thay đổi thường xuyên hơn reasoning logic), và cần security posture riêng (rate-limit, auth) không nên chung pod với executor có quyền mutate cluster |
| **Knowledge Plane** | Observation, Baseline&Anomaly, Knowledge, Reasoning | Chi phí LLM/RAG nặng CPU/GPU khác hẳn Control Plane (I/O-bound) — tách để scale theo tải suy luận, không kéo theo scale API |
| **Execution Plane** | Execution, Verification | **Đây là plane có quyền mutate hạ tầng khách hàng — PHẢI cô lập nghiêm ngặt nhất.** RBAC/ServiceAccount riêng (đã có phần đúng ở v1: `omni-executor-mutate-lab` ClusterRole), không share pod với Knowledge Plane (hiện tại `omni-fullstack` gộp chung — vi phạm nguyên tắc least-privilege ở mức pod, dù RBAC namespace đã đúng) |
| **Agent Plane** | Remote Agent SDK trên hạ tầng khách hàng | Đã đúng ở v1 — giữ nguyên, chỉ cần chuẩn hoá SDK (Phase 7) |
| **Management Plane** | Governance & Policy, CRAT audit ledger | Cần tính bất biến (immutability) và audit trail độc lập khỏi mọi plane khác — không được phép cùng failure domain với Execution (nếu Execution pod chết, audit ledger phải vẫn ghi được sự kiện đó) |
| **Data Plane** | Postgres, Redis, Kafka | Giữ nguyên, không đổi |

**Lý do tách Execution Plane khỏi Knowledge Plane (thay đổi lớn nhất so với v1):** ở v1, `omni-fullstack` (role=full) chạy CẢ evidence/reasoning loop LẪN action-execution loop trong cùng 1 pod, cùng 1 ServiceAccount. Về mặt blast-radius, một lỗ hổng injection trong Reasoning (LLM tool-call bị thao túng qua prompt injection từ log khách hàng) có đường đi tắt tới cùng quyền mutate vì chung process/ServiceAccount. Tách 2 plane bắt buộc chúng giao tiếp qua Kafka (`decision.made` event) thay vì gọi hàm cùng process — đúng defense-in-depth.

---

## PHASE 7 — Product Architecture

| Bề mặt | Thiết kế v2 | Ghi chú |
|---|---|---|
| **Remote Agent SDK** | Đóng gói `remote_agent/` thành package độc lập versioned (`omni-agent-sdk`), publish qua registry riêng (không phải toàn bộ monorepo). Chuẩn hoá **mọi** collector theo đúng mẫu `system.py` hiện tại (chỉ OBSERVED, ngưỡng do server đẩy xuống qua `register`) — sửa 5/6 domain đang tự phán FAILED/PASSED hardcoded (đã xác nhận vi phạm ở Phase 1). Đây là thay đổi ưu tiên cao nhất về mặt sản phẩm vì nó là ranh giới hợp đồng "não/thân" chính thức. |
| **REST/Webhook API** | Giữ `gateway/routes/agent_*`, tách `autonomy.py` (701 dòng) thành router con theo sub-domain (policy/tier/tenant/api-key/HITL) — versioned (`/v1/...`) để không breaking khi v2 đổi schema |
| **Event API** | Kafka topics đã có (`omni-diagnostic-evidence`, `omni-knowledge-evidence`, `omni-actions`, `omni-action-feedback`) chính thức hoá thành **Event Contract** version hoá (JSON Schema per topic, kiểm tra ở CI) thay vì chỉ dựa vào Pydantic model rải rác nhiều nơi |
| **Plugin API** | Domain collector mới (customer muốn thêm domain thứ 10) chỉ cần implement interface `Collector.collect() -> Envelope[]` + khai `domain_hint` — không đụng core agent loop. Tương tự phía Omni: "Known Fix Reflex" mới = plugin đăng ký vào `pkg/reasoning/known_fix_resolver.py` registry, không sửa evidence_consumer.py |
| **Knowledge SDK** | Chuẩn hoá `ingest_customer_knowledge()` (`services/knowledge/document_store.py`) thành API public có versioning, để đối tác/khách hàng tự động push tài liệu (hiện chỉ qua Telegram thủ công) |
| **Automation SDK / CLI** | CLI mỏng gọi thẳng Control Plane API (không gọi trực tiếp Postgres/Redis như một số script hiện tại trong `scripts/`) — để mọi thao tác vận hành đi qua cùng 1 audit trail (CRAT) |
| **Extension model / hệ sinh thái tương lai** | Bounded context = đơn vị mở rộng. Đối tác muốn thêm 1 loại Governance policy mới chỉ cần implement `Policy` interface, không cần hiểu executor/reasoning nội bộ |

---

## PHASE 8 — Migration Roadmap (không rewrite, từng phase deploy được)

### Phase A — Thực thi ranh giới bằng công cụ (không đổi runtime behavior)
- Thêm `import-linter` + CI gate cho các contract ở Phase 5.
- Sửa 5 điểm import ngược thật (`pkg/reasoning/*`, `pkg/autonomy/gate.py`, `pkg/executor/*`, `anomaly/sigma_calibrator.py`, `rag/redis_vector_store.py`) — di chuyển phần bị import (từ `workers.sdk_service_tools`, `workers.metrics_exporter`...) xuống `pkg/` hoặc một `platform_runtime_client` interface mỏng.
- **Benefit:** loại bỏ circular dependency thật mà không đổi 1 dòng business logic.
- **Risk:** thấp — chỉ di chuyển import, có test suite (7348 test) làm lưới an toàn.
- **Rollback:** revert commit, vì không đổi behavior.
- **Success criteria:** `import-linter` xanh trong CI, 0 regression trong 7348 test.

### Phase B — Tách `omni_worker.py` (god object) thành Capability Registry
- Trích từng loop trong `_worker_background_tasks` thành 1 hàm `register(runtime)` độc lập theo bounded context (Phase 2), giữ nguyên hành vi Kafka consumer-group hiện tại (không đổi tên group — tránh lặp lại đúng bug rebalance đã xảy ra).
- `omni_worker.py` co lại còn ~200 dòng: đọc Capability Registry (config khai báo capability nào bật cho role nào), gọi `register()`.
- **Benefit:** loại bỏ chính xác lớp bug đã gây crash-loop production (role if/else rải rác).
- **Risk:** trung bình — đây là refactor lớn nhất, cần chạy song song staging trước khi thay `omni-fullstack` thật.
- **Rollback:** giữ `omni_worker.py` cũ dưới flag, rollback bằng đổi image tag.
- **Success criteria:** cùng số lượng task/consumer-group đăng ký như trước (diff `_worker_background_tasks` output), 0 restart bất thường qua ≥24h quan sát (đối chiếu chuẩn đã dùng ở fix onboarding 2026-08-03).

### Phase C — Tách Execution Plane khỏi Knowledge Plane (Deployment mới)
- Deploy `omni-execution` (Deployment riêng, ServiceAccount `omni-execution-mutate`, chỉ chứa Execution+Verification) tách khỏi `omni-fullstack` (co lại chỉ còn Knowledge Plane: Observation/Anomaly/Knowledge/Reasoning + Learning).
- Giao tiếp qua Kafka `omni-actions`/`omni-action-feedback` (đã tồn tại, không đổi contract).
- **Benefit:** blast-radius thật sự giảm — 1 lỗ hổng ở Reasoning (LLM tool injection) không còn chung ServiceAccount với quyền mutate.
- **Risk:** trung bình-cao — đổi topology triển khai, cần verify RBAC mới đúng least-privilege trước khi cắt.
- **Rollback:** `omni-fullstack` cũ vẫn giữ được khả năng chạy full (feature-flag), scale `omni-execution` về 0 để quay lại monolith tạm thời.
- **Success criteria:** RBAC audit xác nhận `omni-fullstack` (Knowledge Plane) không còn quyền patch Secrets/Deployments; `omni-execution` là nơi DUY NHẤT có ClusterRole mutate.

### Phase D — Chuẩn hoá Remote Agent SDK (đóng gói + sửa 5 collector tự phán)
- Sửa từng collector (storage/logs/database/services/network) theo mẫu `system.py`: verdict cuối do server (Baseline&Anomaly context) quyết, agent chỉ gửi số + `thresholds_seen`.
- Đóng gói `remote_agent/` thành package versioned riêng.
- **Benefit:** đúng 100% nguyên tắc "agent đề xuất, Omni quyết" ở MỌI domain (hiện chỉ đúng ở os_host).
- **Risk:** cao nhất về mặt vận hành — đây là thay đổi chạm tới code chạy trên VM khách hàng thật, cần rollout dần (canary 1 VM lab trước, giữ tương thích ngược cho payload cũ).
- **Rollback:** agent versioning + `updater.py` đã có cơ chế rollback (verify sha256, restart) — tái sử dụng nguyên xi.
- **Success criteria:** 3/3 VM lab gửi payload OBSERVED-only cho cả 6 domain, Baseline&Anomaly context tính verdict đúng như agent cũ từng tính (parity test, giống mẫu đã dùng khi port SIEM brain-go→Python).

### Phase E — Dọn manifest chết + chuẩn hoá deployment
- Xoá hoặc annotate rõ ràng `omni-siem-bridge`/`omni-hitl-dispatcher`/`omni-evidence-adapter` (không target Makefile nào áp dụng thật) và sửa dangling reference `k8s/services/omni-analyst-service.yaml`.
- **Benefit:** giảm nợ kỹ thuật, tránh nhầm lẫn "deployment nào là thật" (đã từng gây audit sai trong quá khứ).
- **Risk:** thấp.
- **Rollback:** git revert.
- **Success criteria:** `make deploy-*` không còn tham chiếu file không tồn tại; annotate rõ status trên mọi manifest còn giữ nhưng không active.

---

## PHASE 9 — Architecture Decision Records

### ADR-V2-01: Tách Execution Plane khỏi Knowledge Plane
- **Decision:** Execution+Verification chạy Deployment riêng, ServiceAccount riêng, giao tiếp qua Kafka event only.
- **Alternatives:** (a) giữ nguyên 1 pod nhưng thêm RBAC filter mềm ở tầng ứng dụng — bị bác vì không chặn được injection ở tầng process; (b) tách theo tenant thay vì theo capability — bị bác vì không giải quyết vấn đề gốc (blast-radius theo capability, không theo khách hàng).
- **Trade-off:** thêm 1 network hop (Kafka) cho mọi lệnh execute — chấp nhận được vì Execution vốn đã async qua Kafka ở v1.
- **Rejected:** chạy Execution như sidecar container cùng pod — vẫn chung network namespace, không đạt cô lập ServiceAccount thật.
- **Long-term impact:** mọi capability mutate mới (rollback, patch, scale) tự động thừa hưởng cô lập này.
- **AI impact:** giới hạn rõ ràng "LLM không bao giờ đứng cùng process với quyền mutate" — giảm surface area cho prompt injection leo thang thành RCE-như hành vi.

### ADR-V2-02: Thực thi dependency rule bằng `import-linter`, không bằng comment
- **Decision:** mọi bounded-context boundary ở Phase 5 phải có `Contract` trong `import-linter` config, chạy trong CI, fail build nếu vi phạm.
- **Alternatives:** giữ nguyên comment + code review — đã CHỨNG MINH THẤT BẠI (5 vi phạm thật đã lọt qua nhiều review).
- **Trade-off:** thêm 1 bước CI, có thể chặn PR hợp lệ tạm thời trong giai đoạn Phase A migration (cần allowlist tạm).
- **Rejected:** viết custom AST checker riêng — không cần, `import-linter` đã đủ tốt và có cộng đồng bảo trì.
- **Maintainability impact:** cao nhất trong toàn bộ roadmap — đây là "ADR mẹ" khiến mọi ADR khác có thể enforce được lâu dài thay vì trôi dần như v1.

### ADR-V2-03: `omni_worker.py` trở thành Composition Root, không chứa business logic
- **Decision:** entrypoint chỉ đọc Capability Registry + gọi `register(runtime)` của từng context.
- **Alternatives:** giữ nguyên nhưng thêm test coverage cho mọi nhánh role — đã thử (test hiện tại), vẫn không ngăn được lỗi cấu trúc (bug rebalance vẫn xảy ra dù có test, vì test kiểm tra "có/không có task" chứ không kiểm tra "task này join group nào đang bị ai khác join").
- **Trade-off:** chi phí refactor 1 lần cho toàn bộ 8+ loop.
- **Operational impact:** thêm role/capability mới trong tương lai không còn rủi ro tái phát lớp bug rebalance — vì consumer-group ownership khai báo tường minh trong registry, có thể lint "2 capability không được khai cùng consumer-group trừ khi explicit".

### ADR-V2-04: Remote Agent SDK đóng gói độc lập, chuẩn hoá "chỉ OBSERVED"
- **Decision:** mọi collector domain gửi số thô, verdict luôn tính ở Baseline&Anomaly context (Omni).
- **Alternatives:** giữ nguyên tại chỗ (agent tự phán 5/6 domain) — bị bác vì vi phạm trực tiếp mission statement của sản phẩm ("Omni phán, agent đề xuất") mà chính CLAUDE.md đã khẳng định nhưng code chưa khớp.
- **Trade-off:** phải định nghĩa `thresholds_seen` schema cho từng domain (storage/logs/database/services/network) — việc mà `system.py` đã làm mẫu sẵn.
- **Long-term impact:** baseline có thể học/tự điều chỉnh ngưỡng theo tenant cho MỌI domain, không chỉ os_host như hiện tại — mở khoá tính năng "autonomous tuning" thật sự cho toàn bộ 9 domain thay vì 1.

### ADR-V2-05: Event Contract version hoá cho Kafka topics
- **Decision:** mỗi topic có JSON Schema versioned, kiểm ở CI, không chỉ dựa Pydantic rải rác.
- **Alternatives:** giữ nguyên Pydantic per-consumer (mỗi consumer tự validate theo ý mình) — bị bác vì đã gây incident thật trong quá khứ (`coerce_evidence_dict()` gotcha — serialize khác producer/consumer parse sai).
- **Trade-off:** thêm bước generate/publish schema, chi phí ban đầu nhỏ.
- **Maintainability impact:** consumer mới (ví dụ 1 dashboard bên ngoài) subscribe topic mà không cần đọc source code producer.

---

## PHASE 10 — Architecture Scorecard

| Tiêu chí | v1 (hiện tại) | v2 (sau roadmap) | Giải thích |
|---|---|---|---|
| Scalability | 5/10 | 8/10 | v1: mọi domain chung 1 pod (`omni-fullstack`) → scale = scale tất cả cùng lúc. v2: Knowledge/Execution/Control Plane scale độc lập theo tải riêng. |
| Maintainability | 4/10 | 8/10 | v1: god object 1420-3578 dòng, role if/else rải rác, đã gây bug thật. v2: mỗi context 1 module nhỏ, ranh giới lint-enforced. |
| Extensibility | 5/10 | 8/10 | v1: thêm domain/capability phải sửa file trung tâm (`omni_worker.py`, `evidence_consumer.py`). v2: registry + interface, thêm không sửa core. |
| Reliability | 6/10 | 8/10 | v1 đã có bounded retry, outbox, dedup (tốt) nhưng hidden coupling (`trace_orchestrator.state`) vẫn là rủi ro tiềm ẩn. v2 tách rõ owner từng state. |
| Autonomy | 6/10 | 7/10 | v1 đã có tier gate + confidence-based autonomy thật (không phải mock) cho os_host — nhưng chỉ 1/9 domain đúng chuẩn "agent đề xuất, Omni quyết". v2 mở rộng ra cả 9. |
| Observability | 7/10 | 8/10 | v1 đã có KPI/trace/pipeline dashboard thật, CRAT audit đầy đủ — điểm mạnh có sẵn, v2 chỉ cần giữ và thêm observability cho ranh giới plane mới. |
| Operability | 5/10 | 8/10 | v1: kill-switch hiệu lực nằm rải rác ConfigMap + Deployment env + `kubectl patch` tay, không tái tạo được từ git. v2: mọi override phải qua Control Plane API có audit, không patch tay. |
| Security | 6/10 | 9/10 | v1: Execution và Reasoning chung ServiceAccount/pod — blast-radius lớn nếu injection. v2: tách plane, RBAC least-privilege theo capability. |
| Knowledge Evolution | 7/10 | 8/10 | v1 đã có confidence-based learning thật (STATIC_GUARD→AUTONOMOUS), RAG đã sống. v2 mở rộng cơ chế này cho mọi domain thay vì chỉ os_host. |
| AI Readiness | 6/10 | 8/10 | v1: LLM tool-call (`sdk_service_tools.py`) sống chung process với mutate quyền lực — rủi ro injection→execution. v2: LLM chỉ ở Knowledge Plane, không bao giờ chung quyền mutate. |
| Cloud Native readiness | 6/10 | 8/10 | v1 đã dùng K8s đúng cách (Deployment/StatefulSet/RBAC scoped) — điểm cộng có sẵn; điểm trừ là manifest chết còn tồn tại, ConfigMap/Deployment-env drift không reconcile tự động. |
| Plugin readiness | 3/10 | 7/10 | v1: thêm collector/policy mới phải sửa nhiều file lõi. v2: interface đăng ký rõ ràng (Collector, Policy, KnownFixResolver). |
| Developer Experience | 5/10 | 8/10 | v1: hiểu `omni_worker.py` cần đọc 1420 dòng + biết role nào chạy gì. v2: đọc 1 context = hiểu 1 capability, không cần đọc toàn bộ entrypoint. |
| Operator Experience | 6/10 | 8/10 | v1 đã có Telegram + Portal khá tốt cho operator. v2 thêm: mọi cấu hình override đi qua audit trail thay vì thao tác cluster tay không dấu vết. |
| Technical Debt | 3/10 (nợ cao) | 7/10 | v1: circular dependency thật, dangling Makefile reference, doc drift xác nhận nhiều lớp. v2: từng Phase A-E trực tiếp trả nợ cụ thể đã đo được. |

**Tổng kết:** v1 không phải hệ thống tồi — phần lớn capability cốt lõi (audit ledger, knowledge pipeline, autonomy tier gate, remote agent outbox/retry) đã đúng chuẩn production-grade thật sự, có bằng chứng runtime. Điểm yếu không nằm ở "logic sai" mà ở **ranh giới không được thực thi bằng công cụ** — mọi vi phạm invariant (circular import, role coupling, ServiceAccount chung giữa Reasoning và Execution) đều đã xảy ra dù invariant được ghi rõ bằng comment/CLAUDE.md. v2 giữ nguyên toàn bộ capability, chỉ thay đổi: (1) ranh giới được lint/CI thực thi thay vì kỷ luật con người, (2) Execution Plane cô lập vật lý khỏi Reasoning, (3) entrypoint không còn là god object, (4) Remote Agent SDK nhất quán 100% theo nguyên tắc "đề xuất không phán quyết" ở mọi domain.
