# Redis Sentinel (lab) — biến môi trường

**Tài liệu gốc:** [Redis Sentinel](https://redis.io/docs/management/sentinel/) (high availability, monitoring, failover).

## Worker / Gateway

- **`OMNI_REDIS_SENTINEL_HOSTS`** — CSV `host:port`, ví dụ `redis-sentinel-0.redis-sentinel:26379,redis-sentinel-1.redis-sentinel:26379,redis-sentinel-2.redis-sentinel:26379`.
- **`OMNI_REDIS_SENTINEL_MASTER_NAME`** — tên master trong cấu hình Sentinel (mặc định `mymaster`).
- Khi `OMNI_REDIS_SENTINEL_HOSTS` **rỗng**, code dùng **`OMNI_REDIS_URL`** (standalone) như cũ.

## Triển khai K8s

Manifest Sentinel **không** cố định trong repo (tùy cluster — số node, storage). Áp operator/Helm hoặc StatefulSet theo doc Redis; sau đó set hai biến trên trong ConfigMap `omni-worker-config` / env gateway.
