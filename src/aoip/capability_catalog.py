"""Capability catalog — Service → Capability → Failure Modes (tri thức mở rộng).

Reviewer North Star: KHÔNG classify theo Redis/Postgres/Kafka mà theo CAPABILITY
(Cache/Database/HTTP/Queue/Proxy/Storage). Service chỉ là implementation của một
capability; tất cả implementation cùng capability reuse cùng tập failure mode +
planner. Thêm service mới = 1 dòng map (hoặc tự suy từ cổng) — KHÔNG file mới.

  Cache    ← redis, memcached, dragonfly, hazelcast …
  Database ← postgres, mysql, mariadb, mongo, oracle …
  HTTP     ← nginx, apache, traefik, envoy …
  Queue    ← kafka, rabbitmq, nats …

Mỗi capability → tập failure mode khả dĩ. Bước tiếp (reviewer): AI tự CLASSIFY từ
tín hiệu discovery (cổng/binary/config/api) thay vì bảng tay — đã có fallback suy
theo cổng ở đây.
"""
from __future__ import annotations

# Capability → các failure mode khả dĩ (tên trong FAILURE_MODES).
_UNIVERSAL = ("process_down", "oom_killed", "disk_full", "network_unreachable")
CAPABILITY_MODES: dict[str, tuple[str, ...]] = {
    "cache": _UNIVERSAL,
    "database": _UNIVERSAL,
    "queue": _UNIVERSAL,
    "http": ("process_down", "network_unreachable", "cpu_starvation"),
    "proxy": ("process_down", "network_unreachable", "cpu_starvation"),
    "storage": _UNIVERSAL,
    # fallback: service chưa phân loại vẫn chẩn đoán được tối thiểu.
    "generic": ("process_down", "network_unreachable"),
}

# Service name (base) → capability. Thêm dòng = hỗ trợ service mới, KHÔNG file mới.
SERVICE_CAPABILITY: dict[str, str] = {
    "redis": "cache", "redis-server": "cache", "memcached": "cache", "dragonfly": "cache",
    "hazelcast": "cache", "keydb": "cache",
    "postgres": "database", "postgresql": "database", "mysql": "database", "mysqld": "database",
    "mariadb": "database", "mariadbd": "database", "mongod": "database", "mongodb": "database",
    "oracle": "database", "clickhouse": "database",
    "nginx": "http", "apache2": "http", "httpd": "http", "tomcat": "http", "caddy": "http",
    "haproxy": "proxy", "traefik": "proxy", "envoy": "proxy",
    "kafka": "queue", "rabbitmq": "queue", "nats": "queue", "redpanda": "queue",
    "minio": "storage", "ceph": "storage",
}

# Cổng quy ước → capability (suy luận khi tên lạ — service chưa từng gặp).
_PORT_CAPABILITY: dict[int, str] = {
    6379: "cache", 11211: "cache",
    5432: "database", 3306: "database", 27017: "database", 9000: "database",
    80: "http", 443: "http", 8080: "http",
    9092: "queue", 5672: "queue", 4222: "queue",
}


def classify_service(name: str, *, port: int | None = None) -> str:
    """Service → capability. Ưu tiên tên đã biết; nếu lạ, suy từ cổng; cuối cùng generic.

    Đây là điểm khiến AI xử lý được service CHƯA TỪNG GẶP (vd DragonflyDB nghe 6379
    → cache) — tri thức ở mức capability, không phải catalog service đóng.
    """
    base = (name or "").split("@")[0].strip().lower()
    if base in SERVICE_CAPABILITY:
        return SERVICE_CAPABILITY[base]
    if port is not None and port in _PORT_CAPABILITY:
        return _PORT_CAPABILITY[port]
    return "generic"


def failure_modes_for(capability: str) -> tuple[str, ...]:
    return CAPABILITY_MODES.get(capability, CAPABILITY_MODES["generic"])
