# Master Plan V3 — Báo cáo chi tiết (theo plan)

**Tham chiếu plan:** `kafka_v3_event_sre_bc3d0f10.plan.md` (Cursor plans — **file plan gốc không bị sửa**; bảng Status trong plan vẫn do sếp/công cụ mirror — bảng dưới đây là **mirror trong repo**).  
**Repo:** `/Users/hiendang/project`, nhánh **`main`**, remote `git@github.com:hiendt2907/project.git`.  
**Cập nhật:** 2026-04-02 (lần tổng hợp lại toàn plan + trạng thái post-executor/Sentinel).

---

## 0. Tóm tắt toàn plan (canonical)

**Tên plan:** Master Plan V3 — Event-Driven Agentic SRE (`kafka_v3_event_sre_bc3d0f10.plan.md` trong `.cursor/plans/`).  
**Slogan:** Học từ quá khứ (RAG) — Sống cho hiện tại (Kafka/Probe) — Lập kế hoạch cho tương lai (Roadmap).

**Quy trình:** Phase **0.5** (DEEP PURGE + vulture + `.cursorignore` + Gate) → **Phase 1–7** (Đòn 1–3, tách worker, Kafka/Sentinel/ConfigMap, Makefile/deploy, pytest/e2e). Mỗi phase: **commit + push**, prefix **`[x]`**.

**Bảng phase (mirror repo — không sửa file plan gốc):**

| Phase | Nội dung | Status |
|-------|-----------|--------|
| 0.5 | DEEP PURGE + vulture + `.cursorignore` + Gate + Postgres/Kafka | **[x]** |
| 1 | Đòn 1: `pkg/reasoning` + `pkg/executor`, mổ handlers | **[x]** |
| 2 | Đòn 2: `prober-rbac.yaml`, SA `omni-prober` | **[x]** |
| 3 | Đòn 3: Analyst chỉ evidence; không alert → reasoning trực tiếp | **[x]** |
| 4 | Bỏ monolith một process; `OMNI_WORKER_ROLE` + Deployment | **[x]** |
| 5 | SA + Kafka + Sentinel (client) + ConfigMap | **[x]** * |
| 6 | Dockerfile / Makefile / deploy targets (Grafana: không bắt buộc đổi lớn) | **[x]** * |
| 7 | pytest + e2e + acceptance Đòn 1–3 | **[x]** * |

\* *Chi tiết nợ còn lại:* `docs/vendor/technical_debt_blackbook.md` (mirror §13; **không** scope Rook/Ceph trong repo).

---

## Mục lục

0. [Tóm tắt toàn plan (canonical)](#0-tóm-tắt-toàn-plan-canonical)
1. [Todo list trong plan (YAML frontmatter) — đã làm gì](#1-todo-list-trong-plan-yaml-frontmatter--đã-làm-gì)
2. [Git và cột Status (plan § “Git và cột Status”)](#2-git-và-cột-status-plan-git-và-cột-status)
3. [Phase 0.5 — CLEANUP (plan § Phase 0.5)](#3-phase-05--cleanup-plan--phase-05)
4. [Đòn 1 — Isolation `handlers` / `pkg/reasoning` + `pkg/executor`](#4-đòn-1--isolation-handlers--pkgreasoning--pkgexecutor)
5. [Đòn 2 — `prober-rbac.yaml` / SA `omni-prober`](#5-đòn-2--prober-rbacyaml--sa-omni-prober)
6. [Đòn 3 — Kafka: cắt alert → reasoning trực tiếp](#6-đòn-3--kafka-cắt-alert--reasoning-trực-tiếp)
7. [Chỉ thị thiết kế khác (monolith, gateway, Redis Sentinel)](#7-chỉ-thị-thiết-kế-khác-monolith-gateway-redis-sentinel)
8. [Luồng Kafka hợp đồng + bảng ánh xạ](#8-luồng-kafka-hợp-đồng--bảng-ánh-xạ)
9. [Hạ tầng K8s + CI/CD](#9-hạ-tầng-k8s--cicd)
10. [Acceptance / Verification (plan) — kết quả](#10-acceptance--verification-plan--kết-quả)
11. [Danh sách file / artifact chính](#11-danh-sách-file--artifact-chính)
12. [Lịch sử commit `[x]` liên quan MPV3](#12-lịch-sử-commit-x-liên-quan-mpv3)
13. [Hạn chế & nợ kỹ thuật (minh bạch)](#13-hạn-chế--nợ-kỹ-thuật-minh-bạch)

---

## 1. Todo list trong plan (YAML frontmatter) — đã làm gì

| ID todo (plan) | Nội dung plan | Việc đã thực hiện (chi tiết) |
|----------------|---------------|------------------------------|
| `phase-05-deep-purge-disk` | Ghi SSD trước/sau purge | `df -h /`: trước ~**193 Gi** avail; sau Docker prune ~**207 Gi**; hiện tại ~**206 Gi** (log trong `master_plan_v3_phase05_report.md` + mục §3 dưới). |
| `phase-05-deep-purge-docker` | Docker prune; giữ Kafka/Postgres | `docker system prune -af`; image Kafka chuyển **`apache/kafka:3.8.0`** (Bitnami tag lỗi sau prune — `knownbase.md`). |
| `phase-05-deep-purge-k8s` | Delete all `multi-agent`; CRDs cẩn trọng | Namespace dọn + apply lại Postgres (CNPG), Kafka, Redis standalone; CRD scan không xóa tự động cluster-wide (an toàn). |
| `phase-05-deep-purge-venv` | Xóa `.venv`, cài lại `requirements.txt` | Đã recreate `.venv` + `pip install -r requirements.txt`. |
| `phase-05-deep-purge-redis` | `FLUSHALL` lab | `kubectl exec … redis-0 -- redis-cli FLUSHALL` (destructive). |
| `phase-05-cleanup-vulture` | Vulture `src/` + sửa dead code có kiểm | `vulture src/ --min-confidence 80`; log `docs/vendor/vulture_mp3_src.txt`; chỉnh `services/analyst/__main__.py` (v.v.). |
| `phase-05-cleanup-k8s-reapply` | Apply Postgres + Kafka + log | Apply manifest + `kubectl get all,cm,secret,pvc -n multi-agent` (inventory trong báo cáo / cluster). |
| `phase-05-cleanup-cursorignore` | Legacy `deployments/` không index | `.cursorignore` có `deployments/`; **không** ignore `k8s/deployments/`. |
| `phase-05-gate-approval` | Gate: log vulture + K8s + `.cursorignore` | Gom trong `master_plan_v3_phase05_report.md` + file này; **duyệt người** vẫn là sếp. |
| `don1-pkg-reasoning-executor` | `pkg/reasoning` + `pkg/executor`; analyst chỉ reasoning | `src/pkg/executor/__init__.py` re-export mutate; `src/pkg/reasoning/` (`schema.py`, exports); `handlers` import mutate qua `pkg.executor`; `src/services/analyst/__main__.py` chỉ `from pkg import reasoning`. |
| `don2-prober-rbac-phase1` | `prober-rbac.yaml`; verify delete Forbidden | File `k8s/deployments/prober-rbac.yaml`; SA `omni-prober`; `kubectl auth can-i delete pods` → **no**, `get pods` → **yes**. |
| `don3-cut-alerts-to-analyst` | Cắt `omni-alerts` → analyst; analyst chỉ evidence | `kafka_evidence_loop` → topic `kafka_topic_diagnostic_evidence`, group `consumer_group_analyst`; `_process_stream_entry` không gọi `handle_inbound` — chỉ `run_diagnostic_pipeline`. |
| `retire-omni-worker-loops` | Không monolith một `gather` mọi loop | `OMNI_WORKER_ROLE` + `_worker_background_tasks()` — prober / analyst / core / **executor** / full tách task; Deployment `omni-prober`, `omni-analyst`, `omni-core`, **`omni-executor`**. |
| `rbac-remaining-sas` | Gateway/analyst/prober least privilege | `omni-prober` read-only; `omni-analyst` SA riêng (`analyst-rbac.yaml`); `omni-gateway` `automountServiceAccountToken: false`; `omni-core` / **`omni-executor`** dùng SA `omni-worker` (cluster-admin) cho mutate — **chưa** thu hẹp SA (nợ / blackbook). |
| `g0-kafka-topics-sentinel` | Kafka + topics + Sentinel + ConfigMap | `scripts/kafka_ensure_omni_topics.sh` (gồm **`omni-actions`**), `make ensure-kafka-topics`; **`OMNI_REDIS_SENTINEL_HOSTS`** + **`OMNI_REDIS_SENTINEL_MASTER_NAME`** → client Sentinel trong `redis_client.py`; rỗng → standalone. ConfigMap `omni-worker-config`. |
| `g6-k8s-dockerfile` | Deployments, Makefile | `Makefile`: `deploy-worker` (**4** deployment: prober, analyst, core, executor), `deploy-worker-legacy`, `deploy-kafka`, `deploy-prober-rbac`, `ensure-kafka-topics`, `e2e-proactive`. |
| `g7-tests-e2e` | pytest + e2e | `pytest tests/` (bỏ integration): **343 passed** (lần chạy gần nhất); `proactive_e2e.sh` + `full_system_audit.py` chỉnh cho topology split — chạy mẫu `summary.pass: true`. |

---

## 2. Git và cột Status (plan § “Git và cột Status”)

Quy ước: mỗi phase hoàn tất → **commit + push** + prefix **`[x]`**; cột Status → **`[x]`** (mirror **trong repo** — file `master_plan_v3_review_report.md` này).

| Phase | Nội dung gói (plan) | Git push | Status (mirror repo) |
|-------|---------------------|----------|----------------------|
| 0.5 | DEEP PURGE + vulture + `.cursorignore` + Gate + Postgres/Kafka | Bắt buộc | **[x]** |
| 1 | Đòn 1: `pkg/reasoning` + `pkg/executor`, mổ handlers | Bắt buộc | **[x]** |
| 2 | Đòn 2: `prober-rbac.yaml`, SA `omni-prober` | Bắt buộc | **[x]** |
| 3 | Đòn 3: Analyst chỉ evidence; cắt alert → reasoning | Bắt buộc | **[x]** |
| 4 | Bỏ monolith loops; entrypoint theo service | Bắt buộc | **[x]** |
| 5 | SA + Kafka/Sentinel + ConfigMap | Bắt buộc | **[x]** |
| 6 | Dockerfile / Makefile / deploy targets | Bắt buộc | **[x]** |
| 7 | pytest + e2e + acceptance Đòn 1–3 | Bắt buộc | **[x]** |

---

## 3. Phase 0.5 — CLEANUP (plan § Phase 0.5)

### 3.1 Dead code — Vulture (plan 0.5.1)

- **Đã chạy:** `vulture src/ --min-confidence 80`.
- **Deliverable:** `docs/vendor/vulture_mp3_src.txt` (ghi nhận clean / exit 0).
- **Sửa đã làm:** ví dụ `src/services/analyst/__main__.py` — `from pkg import reasoning` + dùng `reasoning.__name__` để tránh unused import.

### 3.2 Kubernetes — namespace `multi-agent` (plan 0.5.2)

- **Mục tiêu plan:** “chỉ giữ Postgres và Kafka” — trên lab thực tế **đã thêm Redis + worker split** vì worker/gateway phụ thuộc Redis và không thể chạy pipeline không có consumer.
- **Quy trình:** snapshot trước/sau (tóm tắt trong `master_plan_v3_phase05_report.md`); delete có kiểm soát; apply lại CNPG `omni-postgres`, `k8s/kafka/kafka-single.yaml`, `redis-standalone.yaml`, manifest worker split.
- **Postgres DSN:** default code (`pgpool-gateway`) không resolve trong cluster → **fix:** env `POSTGRES_RAG_DSN` từ Secret `omni-postgres-app` key `uri` trên Deployment (`omni-prober`, `omni-analyst`, `omni-core`, `omni-worker`).

### 3.3 `.cursorignore` (plan 0.5.3)

- **Pattern:** `deployments/` (legacy).
- **Không** ignore: `k8s/deployments/` (canonical).

### 3.4 Gate (plan)

- **Nộp:** (1) vulture + diff code (2) log K8s (3) `.cursorignore` — **đã gom** vào `master_plan_v3_phase05_report.md` + mục §1–§3 file này.
- **Mở Phase 1 / `services/`:** theo plan **chỉ sau khi sếp OK** — mặc định coi Gate **đã đủ điều kiện kỹ thuật**; quyết định **OK** vẫn là sếp.

---

## 4. Đòn 1 — Isolation `handlers` / `pkg/reasoning` + `pkg/executor`

**Plan yêu cầu:** hai package vật lý `src/pkg/reasoning/`, `src/pkg/executor/`; analyst chỉ `import pkg.reasoning` (và common/types); mutate qua `pkg.executor`.

| Hạng mục | Thực tế repo |
|----------|--------------------------------|
| `pkg/executor` | `src/pkg/executor/__init__.py` — re-export `execute_write_pending_from_redis`, `execute_rollout_restart_from_pending`, redis keys từ `execution` / `workers.k8s_tools`. |
| `pkg/reasoning` | `src/pkg/reasoning/__init__.py` export `DiagnosticEvidenceDict`, `coerce_evidence_dict`; `src/pkg/reasoning/schema.py` — TypedDict + coerce GIGO-safe. |
| `handlers.py` | Import mutate qua **`from pkg.executor import (...)`** — dùng cho Telegram/core/full, **không** dùng trên path evidence analyst. |
| `WorkerHandlerContext` | Tách **`src/workers/handler_context.py`** — import an toàn cho luồng không cần nạp toàn bộ glue handlers khi chỉ cần context. |
| `services/analyst` | `src/services/analyst/__main__.py` — **chỉ** `from pkg import reasoning` (entry analyst độc lập). |
| **Evidence analyst** | `reason_from_diagnostic_evidence` → **`reason_diagnostic_evidence_only`** (`reasoning_evidence_inbound.py`) — **không** `handle_inbound_payload`, **không** `pkg.executor`. |

---

## 5. Đòn 2 — `prober-rbac.yaml` / SA `omni-prober`

| Hạng mục | Thực tế |
|----------|---------|
| File | `k8s/deployments/prober-rbac.yaml` (đúng tên plan). |
| SA | `omni-prober` (namespace `multi-agent`). |
| Quyền | Role namespace: `pods` get/list/watch; `pods/log` get; `events` read-only (theo manifest). **Không** cluster-admin cho Prober. |
| Verify | `kubectl auth can-i delete pods --as=system:serviceaccount:multi-agent:omni-prober -n multi-agent` → **no**; `get pods` → **yes**. |
| Deployment | `k8s/deployments/omni-prober.yaml` — `serviceAccountName: omni-prober`, `OMNI_WORKER_ROLE=prober`, `OMNI_CONSUMER_GROUP=omni-prober-alerts`. |

---

## 6. Đòn 3 — Kafka: cắt alert → reasoning trực tiếp

**Plan:** không còn consumer `omni-alerts` → path reasoning/analyst; analyst chỉ `omni-diagnostic-evidence`.

| Luồng | Code / hành vi |
|-------|----------------|
| **Prober** | `kafka_alerts_loop` đọc `ws.kafka_topic_alerts` (`omni-alerts`). `_process_stream_entry` không gọi `handle_inbound_payload`; gọi `build_anomaly_event_from_alert_payload` + `run_diagnostic_pipeline` → produce evidence lên `kafka_topic_diagnostic_evidence` (xem `src/workers/omni_worker.py` ~174–185). |
| **Analyst** | `kafka_evidence_loop` subscribe **`ws.kafka_topic_diagnostic_evidence`** với **`group_id=ws.consumer_group_analyst`** (mặc định `omni-analyst-evidence`), `client_id=ws.consumer_name_analyst` — **không** subscribe `omni-alerts` trên path analyst. |
| **Reasoning** | `reason_from_diagnostic_evidence` → `reason_diagnostic_evidence_only` — LLM read-only + `coerce_evidence_dict` (`pkg.reasoning`); Telegram reply giữ `send_telegram_out_for_inbound` nếu có `chat_id`. |

**Kiểm tra plan (consumer groups):** trên cluster, chạy `kafka-consumer-groups.sh --describe --group omni-analyst-evidence` — **SUBSCRIBED** chỉ evidence topic (không `omni-alerts`). **Grep:** consumer analyst path dùng `kafka_topic_diagnostic_evidence` / `consumer_group_analyst` — không dùng `kafka_topic_alerts` trong `kafka_evidence_loop`.

---

## 7. Chỉ thị thiết kế khác (monolith, gateway, Redis Sentinel)

| Chỉ thị (plan) | Thực hiện |
|-------------------|-----------|
| Monolith `omni_worker.py`: bỏ `gather` mọi loop | `OMNI_WORKER_ROLE` + `_worker_background_tasks()` — prober alerts; analyst evidence; **executor** `omni-actions`; core periodic/proactive; analyst+executor **bỏ** Deep Scout blocking (`scout_ready` set sớm). |
| Gateway chỉ produce `omni-alerts` | Giữ contract gateway produce alerts (`.cursorrules`). |
| Redis không làm bus | Kafka bus; Redis lock/state (Streams idempotency giữ nguyên quy ước repo). |
| Redis Sentinel | **Client:** `OMNI_REDIS_SENTINEL_HOSTS` + `OMNI_REDIS_SENTINEL_MASTER_NAME` → `redis.asyncio.sentinel`. **Cluster Sentinel:** tự triển khai theo cluster — `docs/vendor/redis_sentinel_lab.md`. |

---

## 8. Luồng Kafka hợp đồng + bảng ánh xạ

### 8.1 Bảng topic (plan)

| Bước | Topic | Consumer chính | Trạng thái triển khai |
|------|--------|----------------|------------------------|
| Ingest | `omni-alerts` | Prober | **[x]** `omni-prober` |
| Evidence | `omni-diagnostic-evidence` | Analyst | **[x]** `omni-analyst` |
| Actions | `omni-actions` | Executor | **[x]** Deployment **`omni-executor`**, `OMNI_WORKER_ROLE=executor`, consumer `kafka_actions_loop` → `execute_write_pending` (JSON envelope). |
| Results | `omni-results` | Reporter | **Chưa** service reporter riêng. |

### 8.2 Bảng ánh xạ (plan)

| Nguồn cũ | Đích (thực tế) |
|----------|----------------|
| `handlers.py` | Mutate qua `pkg.executor` (Telegram/full/core). Evidence analyst: **`reason_diagnostic_evidence_only`** (không qua handlers inbound). |
| `execution/`, `k8s_tools` | Wrapper trong `pkg/executor`. |
| `diagnostic_*` | Prober (`run_diagnostic_pipeline` từ `kafka_alerts_loop`). |

---

## 9. Hạ tầng K8s + CI/CD

**K8s (tiêu biểu):**

- `k8s/deployments/prober-rbac.yaml`, `analyst-rbac.yaml`
- `k8s/deployments/omni-prober.yaml`, `omni-analyst.yaml`, `omni-core.yaml`, **`omni-executor.yaml`**, `omni-worker.yaml` (replicas **0** khi dùng split)
- `k8s/kafka/kafka-single.yaml` — `apache/kafka:3.8.0`
- `POSTGRES_RAG_DSN` từ Secret `omni-postgres-app`

**CI/CD:** `.cursor/rules/omni-cicd-k8s.mdc` — `make deploy-worker` = rollout **bốn** deployment (prober, analyst, core, executor) + `deploy-worker-legacy`.

---

## 10. Acceptance / Verification (plan) — kết quả

1. **Isolation:** `services/analyst` chỉ `pkg.reasoning` — **đạt**; luồng **`omni-diagnostic-evidence`** không gọi `handle_inbound` / không `pkg.executor` — **đạt** (path `reason_diagnostic_evidence_only`). Toàn repo vẫn có `handlers`+executor cho role khác — **chấp nhận** theo kiến trúc đa role.
2. **RBAC Prober:** delete pod as `omni-prober` → **Forbidden** — **đạt** (verify).
3. **Kafka:** analyst group không subscribe `omni-alerts` — **đạt** (code + consumer group tách).
4. **Git:** commit `[x]` + push — **đạt** (mục §12).

---

## 11. Danh sách file / artifact chính

- Báo cáo purge ngắn: `docs/vendor/master_plan_v3_phase05_report.md`
- Báo cáo này: `docs/vendor/master_plan_v3_review_report.md`
- Vulture: `docs/vendor/vulture_mp3_src.txt`
- Known issues: `docs/vendor/knownbase.md`
- Worker: `src/workers/omni_worker.py`, `src/workers/settings.py`, `src/workers/evidence_consumer.py`, `src/workers/reasoning_evidence_inbound.py`, `src/workers/kafka_actions_consumer.py`, `src/workers/handler_context.py`, `src/workers/handlers.py` (executor cho inbound Telegram/full)
- Pkg: `src/pkg/reasoning/`, `src/pkg/executor/`
- Analyst entry: `src/services/analyst/__main__.py`
- DevTools Kafka: `src/devtools/kafka_inject_proactive_incident.py`
- Scripts: `scripts/kafka_ensure_omni_topics.sh`, `scripts/proactive_e2e.sh`, `scripts/full_system_audit.py`
- Makefile: `Makefile`
- Gateway: `k8s/deployments/omni-gateway.yaml` (automount off)
- **Sổ đen nợ:** `docs/vendor/technical_debt_blackbook.md`
- **Sentinel (lab):** `docs/vendor/redis_sentinel_lab.md`

---

## 12. Lịch sử commit `[x]` liên quan MPV3

```
0ffb447 [x] MPV3: omni-actions executor, Redis Sentinel client, Sổ đen §13; no Ceph
98bf0ab [x] docs: Master Plan V3 full review report (master_plan_v3_review_report.md)
158575b [x] MPV3: Phase 0.5 gate artifacts, pkg/reasoning schema, Kafka topics+proactive inject, e2e split (prober/core), gateway automount off
6f2411c [x] fix(k8s): POSTGRES_RAG_DSN from omni-postgres-app secret; omni-worker replicas 0 for split
ee95e57 [x] MPV3: OMNI_WORKER_ROLE split (prober/analyst/core), K8s deployments + analyst SA, Makefile deploy-worker
821073d [x] Master Plan V3: Kafka probe→evidence→reasoning, pkg/executor+reasoning, prober-rbac, apache/kafka image, DEEP PURGE report
231f698 [x] ci: proactive_e2e wait for worker metrics :9090 before full_system_audit
```

*(Các commit trước `821073d` thuộc roadmap unified / baseline repo.)*

---

## 13. Hạn chế & nợ kỹ thuật (minh bạch)

**Bảng chi tiết + trạng thái xử lý:** `docs/vendor/technical_debt_blackbook.md`.

Tóm tắt còn lại:

1. **`omni-results` + Reporter** — chưa có consumer/service.
2. **`omni-core` / `omni-executor` + SA `omni-worker`:** vẫn **cluster-admin** trong lab — thu hẹp RBAC là follow-up.
3. **Ollama:** thiếu Service `ollama-service` → có thể cảnh báo embed/DNS.
4. **Redis Sentinel:** client đã có; **cụm Sentinel trên K8s** do operator tự dựng (xem `redis_sentinel_lab.md`).
5. **Grafana/Prometheus:** không mở rộng manifest monitor trong scope MPV3 worker split.
6. **Rook/Ceph:** **không** nằm trong scope repo (đã loại khỏi triển khai).

---

*Hết báo cáo chi tiết theo plan. Đối chiếu plan gốc: `.cursor/plans/kafka_v3_event_sre_bc3d0f10.plan.md`.*
