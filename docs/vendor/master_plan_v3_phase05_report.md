# Master Plan V3 — Phase 0.5 DEEP PURGE report

Generated: 2026-04-02 (implement step).

**Báo cáo review đầy đủ (Phase 0.5 → 7, Git, verify, hạn chế):** xem [`master_plan_v3_review_report.md`](master_plan_v3_review_report.md).

## SSD / disk (`df -h /`)

**Before (pre-Docker prune):** Avail ~193Gi on `/` (see agent log).

**After Docker `system prune -af`:** Reclaimed ~13.2GB Docker data; Avail ~207Gi on `/`.

## Docker

- `docker system prune -af` — reclaimed ~13.2GB (images/containers/build cache).

## Kubernetes `multi-agent`

- Deleted CNPG `Cluster/omni-postgres`, `kubectl delete all --all`, PVCs, jobs.
- Re-applied: `deployments/postgres-cluster.yaml`, `k8s/kafka/kafka-single.yaml` (image `apache/kafka:3.8.0`), `k8s/deployments/redis-standalone.yaml` (worker dependency).
- **Kafka manifest** switched from unavailable Bitnami tag to **apache/kafka:3.8.0** (KRaft env).

## Redis

- `FLUSHALL` on `redis-0` before workload delete (lab).

## Python `.venv`

- Removed and recreated; `pip install -r requirements.txt` + `vulture`.

## Vulture

- Ran `vulture src/ --min-confidence 80`; fixed unused imports / `model_post_init` params in telegram, ollama_client, watchdog, tools.

## `.cursorignore`

- Appended `deployments/` (legacy; canonical under `k8s/deployments/`).

## Gate

- This report + code changes ready for review. Apply worker/gateway images after rebuild.

## 2026-04-02 follow-up (automated)

- **Redis:** `FLUSHALL` on `redis-0` (lab) — OK.
- **Vulture:** `docs/vendor/vulture_mp3_src.txt` (clean run, `--min-confidence 80`).
- **K8s inventory:** `kubectl get all,cm,secret,pvc -n multi-agent` — Postgres + Kafka + Redis + split worker deployments; `omni-worker` replicas 0.
- **Kafka topics:** `make ensure-kafka-topics` / `scripts/kafka_ensure_omni_topics.sh`.
- **E2E:** `DURATION_SEC=20 INTERVAL_SEC=5 bash scripts/proactive_e2e.sh --skip-build` — `summary.pass: true` (split topology + optional gateway).

