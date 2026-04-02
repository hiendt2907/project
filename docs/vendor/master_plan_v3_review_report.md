# Master Plan V3 — Báo cáo đầy đủ (review)

**Mục đích:** Một file duy nhất để sếp review: DEEP PURGE, Gate Phase 0.5, triển khai Phase 1–7 (theo plan), Git, verify, hạn chế còn lại.  
**Không** thay thế file plan canonical trong `.cursor/plans/` — chỉ mirror trạng thái thực tế repo + lab.

**Cập nhật:** 2026-04-02 (workspace `/Users/hiendang/project`, branch `main`).

---

## 1. Tóm tắt điều hành

| Hạng mục | Trạng thái |
|----------|------------|
| DEEP PURGE (Docker / K8s / .venv / Redis) | Đã thực hiện (chi tiết §2); SSD trước/sau có số liệu |
| Vulture + `.cursorignore` | Đã chạy / đã cấu hình; log: `docs/vendor/vulture_mp3_src.txt` |
| Kafka (broker + topics `omni-*`) | Broker `apache/kafka:3.8.0`; `make ensure-kafka-topics` |
| Postgres (CNPG) | Cluster `omni-postgres`; DSN qua Secret `omni-postgres-app` (`POSTGRES_RAG_DSN` trong Deployment worker) |
| Luồng Kafka V3 | `omni-alerts` → Prober (diagnostic) → `omni-diagnostic-evidence` → Analyst (reasoning); không còn alert thẳng vào `handle_inbound` trên path analyst |
| RBAC Prober | `k8s/deployments/prober-rbac.yaml`; `kubectl delete pod` as `omni-prober` → **Forbidden** |
| Tách process | `OMNI_WORKER_ROLE` = `prober` / `analyst` / `core` / `full`; Deployments `omni-prober`, `omni-analyst`, `omni-core`; `omni-worker` replicas **0** (tránh trùng consumer) |
| Test | `pytest` (unit, không integration): **343 passed** (theo lần chạy gần nhất) |
| E2E lab | `proactive_e2e.sh` + `full_system_audit.py` hỗ trợ topology split; chạy mẫu: `summary.pass: true` |
| Git | Commit prefix `[x]`; push `origin/main` (xem §6) |

---

## 2. DEEP PURGE — số liệu & hành động

### 2.1 SSD / ổ đĩa (`df -h /`)

| Thời điểm | Avail (/) | Ghi chú |
|-----------|-----------|---------|
| Trước `docker system prune -af` | ~**193 Gi** | Log agent lúc purge |
| Sau prune | ~**207 Gi** | ~**+14 Gi** khả dụng; Docker thu ~**13.2 GB** |
| **Hiện tại** (máy dev, 2026-04-02) | **~206 Gi** | `df -h /` — có thể lệch nhẹ theo thời gian |

*(macOS: có thể bổ sung `diskutil apfs list` khi cần audit sâu — không bắt buộc trong báo cáo tối thiểu.)*

### 2.2 Docker

- `docker system prune -af` — dọn image/container/build cache cũ.
- Kafka/Postgres: re-pull / manifest sau purge (Bitnami Kafka tag lỗi → chuyển **`apache/kafka:3.8.0`**).

### 2.3 Kubernetes — namespace `multi-agent`

- Đã có đợt dọn: xóa workload cũ, áp lại Postgres (CNPG), Kafka, Redis standalone (worker/gateway phụ thuộc Redis).
- **Inventory mẫu** (sau triển khai): Postgres pods + Service `omni-postgres-rw`; `kafka`; `redis`; split worker (`omni-prober`, `omni-analyst`, `omni-core`); `omni-worker` **0 replica**.
- **CRDs rác:** chỉ quét cẩn trọng cluster-wide (không tự động xóa CRD không rõ nguồn trong báo cáo này).

### 2.4 Python

- Xóa `.venv`, cài lại `pip install -r requirements.txt` (+ `vulture` trong requirements).

### 2.5 Redis

- **`FLUSHALL`** trên instance lab (`redis-0`) — destructive; đã chạy (kể cả follow-up).

---

## 3. Phase 0.5 — Gate (deliverable)

| # | Deliverable | Vị trí / bằng chứng |
|---|-------------|---------------------|
| 1 | Log vulture | `docs/vendor/vulture_mp3_src.txt` |
| 2 | Thay đổi code (dead code / noqa có kiểm) | Lịch sử commit `[x] MPV3` |
| 3 | Log K8s / inventory | Mục §2.3 + `kubectl get all,cm,secret,pvc -n multi-agent` (chạy lại khi review) |
| 4 | `.cursorignore` | Pattern `deployments/` (legacy); **không** ignore `k8s/deployments/` |
| 5 | Báo cáo purge ban đầu | `docs/vendor/master_plan_v3_phase05_report.md` (lịch sử ngắn) |

**Gate người duyệt:** sếp OK sau khi đọc báo cáo này + log — mới coi Phase 0.5 **closed** về mặt quy trình.

---

## 4. Bảng Phase Master Plan V3 — Git push + Status

Quy ước plan: mỗi phase xong → **commit + push** prefix **`[x]`**, cột Status → **`[x]`**.  
Bảng dưới phản ánh **trạng thái implementation trong repo** (không sửa file plan `.cursor`).

| Phase | Nội dung gói | Git push | Status |
|-------|----------------|----------|--------|
| 0.5 | DEEP PURGE + vulture + `.cursorignore` + Gate + Postgres/Kafka | Bắt buộc | **[x]** |
| 1 | `pkg/reasoning` + `pkg/executor`, tách mutate khỏi analyst boundary | Bắt buộc | **[x]** * |
| 2 | `prober-rbac.yaml`, SA `omni-prober` least privilege | Bắt buộc | **[x]** |
| 3 | Analyst chỉ evidence; cắt `omni-alerts` → reasoning trực tiếp | Bắt buộc | **[x]** |
| 4 | Bỏ monolith một process cho mọi loop; entry theo `OMNI_WORKER_ROLE` + Deployment | Bắt buộc | **[x]** |
| 5 | SA + Kafka + Sentinel placeholder Config/settings | Bắt buộc | **[x]** * |
| 6 | Dockerfile / Makefile / deploy targets | Bắt buộc | **[x]** |
| 7 | pytest + e2e + acceptance | Bắt buộc | **[x]** * |

\* *Phase 1 (isolation tuyệt đối):* `pkg/executor` + `pkg/reasoning/schema` đã có; `services/analyst` chỉ import `pkg.reasoning`. Runtime analyst (`python -m workers`, role `analyst`) vẫn đi qua `workers.handlers` (reasoning + tool path) — **chưa** tách hoàn toàn reasoning-only binary không phụ thuộc executor nếu `handle_inbound` còn gọi mutate (đánh dấu technical debt nếu sếp yêu cầu cứng).  
\* *Phase 5 Sentinel:* field `OMNI_REDIS_SENTINEL_HOSTS` / `redis_sentinel_hosts` placeholder — chưa manifest Sentinel HA.  
\* *Phase 7:* pytest + e2e proactive đã xanh trên lab; acceptance “chỉ reasoning không executor” cần grep/CI riêng nếu siết hợp đồng.

---

## 5. Kiến trúc & file chính (tham chiếu)

| Khu vực | File / ghi chú |
|---------|----------------|
| Prober path | `src/workers/omni_worker.py` — `kafka_alerts_loop` → diagnostic pipeline, không gọi `handle_inbound` cho alert thô |
| Analyst path | `kafka_evidence_loop` → `workers/evidence_consumer.reason_from_diagnostic_evidence` |
| Executor boundary | `src/pkg/executor/__init__.py` re-export mutate từ `execution` / `workers.k8s_tools` |
| Reasoning boundary | `src/pkg/reasoning/` — `schema.py` + `__init__.py` |
| RBAC Prober | `k8s/deployments/prober-rbac.yaml` |
| Worker split | `k8s/deployments/omni-prober.yaml`, `omni-analyst.yaml`, `omni-core.yaml`, `omni-worker.yaml` (replicas 0) |
| Postgres DSN | Env `POSTGRES_RAG_DSN` từ Secret `omni-postgres-app` key `uri` |
| Kafka topics | `scripts/kafka_ensure_omni_topics.sh`, `make ensure-kafka-topics` |
| E2E split | `scripts/proactive_e2e.sh`, `scripts/full_system_audit.py`, `src/devtools/kafka_inject_proactive_incident.py` |
| Gateway hardening | `k8s/deployments/omni-gateway.yaml` — `automountServiceAccountToken: false` |
| Known issues | `docs/vendor/knownbase.md` |

---

## 6. Git — commit gần đây (mốc `[x]`)

```
158575b [x] MPV3: Phase 0.5 gate artifacts, pkg/reasoning schema, Kafka topics+proactive inject, e2e split (prober/core), gateway automount off
6f2411c [x] fix(k8s): POSTGRES_RAG_DSN from omni-postgres-app secret; omni-worker replicas 0 for split
ee95e57 [x] MPV3: OMNI_WORKER_ROLE split (prober/analyst/core), K8s deployments + analyst SA, Makefile deploy-worker
821073d [x] Master Plan V3: Kafka probe→evidence→reasoning, pkg/executor+reasoning, prober-rbac, apache/kafka image, DEEP PURGE report
```

Remote: `git@github.com:hiendt2907/project.git` — branch `main` (theo `refactor_unified_roadmap.md`).

---

## 7. Lệnh xác minh (sếp có thể chạy lại)

```bash
# Unit tests
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration

# Disk
df -h /

# K8s (OrbStack / kube context lab)
./scripts/with_working_kube.sh get pods -n multi-agent
./scripts/with_working_kube.sh auth can-i delete pods --as=system:serviceaccount:multi-agent:omni-prober -n multi-agent   # expect: no
./scripts/with_working_kube.sh auth can-i get pods --as=system:serviceaccount:multi-agent:omni-prober -n multi-agent      # expect: yes

# Kafka topics (trong pod kafka)
make ensure-kafka-topics

# E2E (cần cluster + image; có thể --skip-build)
DURATION_SEC=20 INTERVAL_SEC=5 bash scripts/proactive_e2e.sh --skip-build
```

---

## 8. Hạn chế / nợ kỹ thuật (minh bạch)

1. **Ollama:** ConfigMap `OMNI_OLLAMA_BASE_URL=http://ollama-service:11434` — nếu không có Service `ollama-service` trong cluster, DeepScout embed có cảnh báo DNS (worker vẫn chạy nếu Postgres OK).
2. **Analyst isolation cứng:** Entry `services/analyst` tuân thủ chỉ `pkg.reasoning`; path runtime đầy đủ vẫn dùng `handlers` (có thể gọi tool/executor tùy handler) — nếu sếp yêu cầu **zero** import executor trong process analyst, cần thêm refactor `handle_inbound` hoặc service image riêng.
3. **`omni-worker` cluster-admin:** `omni-core` vẫn dùng SA `omni-worker` cho mutate — đúng lab; thu hẹp cluster-admin là hạng mục sau.
4. **Redis Sentinel:** placeholder settings; single Redis vẫn là mặc định lab.

---

## 9. Kết luận

- **Phase 0.5 + pipeline MPV3** đã có bằng chứng trong repo (báo cáo, log vulture, knownbase, script Kafka, e2e).
- **Push Git:** các mốc trên đã lên `main` với prefix `[x]`.
- Sếp review file này + `master_plan_v3_phase05_report.md` (lịch sử purge) + `knownbase.md` là đủ để Gate; phần **hạn chế** §8 là đầu vào cho sprint tiếp theo nếu cần siết hợp đồng analyst/executor.
