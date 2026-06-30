"""Capability catalog — Service → CAPABILITY TAGS → Failure Modes (tri thức mở rộng).

Reviewer North Star: KHÔNG classify theo Redis/Postgres/Kafka mà theo CAPABILITY
(Cache/Database/HTTP/Queue/Proxy/Storage). Service chỉ là implementation của một
capability; tất cả implementation cùng capability reuse cùng tập failure mode.

Hardening (review production):
  1. Tên gọi "Capability" trùng với ``CapabilityState`` (product K×R×E…) → ở đây
     dùng ``capability_tags`` cho rõ: đây là NHÃN năng lực hạ tầng của service.
  2. MỘT service mang NHIỀU tag, mỗi tag có confidence + provenance (redis vừa là
     cache vừa là session_store). Diagnosis hợp (union) failure mode của mọi tag.
  3. Port/name chỉ tạo GIẢ THUYẾT (tag confidence thấp), KHÔNG phải Fact: tên đã
     biết → tin cao; suy từ cổng → tin vừa; không gì → generic tin thấp.

  Cache    ← redis, memcached, dragonfly, hazelcast …
  Database ← postgres, mysql, mariadb, mongo, oracle …
  HTTP     ← nginx, apache, traefik, envoy …
  Queue    ← kafka, rabbitmq, nats …
"""
from __future__ import annotations

from dataclasses import dataclass

# Provenance — vì sao gán tag này (đi vào explainability của Hypothesis).
PROV_NAME = "service-name"      # khớp tên binary đã biết → tin cao
PROV_PORT = "port-signature"    # suy từ cổng quy ước → tin vừa
PROV_FALLBACK = "fallback"      # không tín hiệu nào → generic, tin thấp

_CONF_NAME = 0.7
_CONF_PORT = 0.4
_CONF_SECONDARY = 0.3
_CONF_FALLBACK = 0.2


@dataclass(frozen=True)
class CapabilityTag:
    """Derived (không persist): nhãn năng lực + độ tin + nguồn gốc suy luận.

    KHÔNG phải noun mới của ontology — đây là giá trị suy diễn (như ScoredAction),
    đầu vào để sinh ``Hypothesis``. Port/name → tag confidence thấp, KHÔNG thành Fact.
    """

    tag: str
    confidence: float
    provenance: str


# Capability tag → các failure mode khả dĩ (tên trong FAILURE_MODES).
_UNIVERSAL = ("process_down", "oom_killed", "disk_full", "network_unreachable")
CAPABILITY_MODES: dict[str, tuple[str, ...]] = {
    "cache": _UNIVERSAL,
    "session_store": ("process_down", "network_unreachable"),
    "database": _UNIVERSAL,
    "queue": _UNIVERSAL,
    "http": ("process_down", "network_unreachable", "cpu_starvation"),
    "proxy": ("process_down", "network_unreachable", "cpu_starvation"),
    "storage": _UNIVERSAL,
    # fallback: service chưa phân loại vẫn chẩn đoán được tối thiểu.
    "generic": ("process_down", "network_unreachable"),
}

# Service name (base) → tag chính. Thêm dòng = hỗ trợ service mới, KHÔNG file mới.
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

# Tag phụ theo tên (một service mang nhiều năng lực) — confidence thấp hơn tag chính.
SERVICE_SECONDARY: dict[str, tuple[str, ...]] = {
    "redis": ("session_store",), "redis-server": ("session_store",),
    "keydb": ("session_store",), "dragonfly": ("session_store",),
}

# Cổng quy ước → tag (suy luận khi tên lạ — service chưa từng gặp).
_PORT_CAPABILITY: dict[int, str] = {
    6379: "cache", 11211: "cache",
    5432: "database", 3306: "database", 27017: "database", 9000: "database",
    80: "http", 443: "http", 8080: "http",
    9092: "queue", 5672: "queue", 4222: "queue",
}


def classify_capability_tags(name: str, *, port: int | None = None) -> list[CapabilityTag]:
    """Service → danh sách CapabilityTag (giả thuyết năng lực), sắp theo confidence giảm.

    Đây là điểm khiến AI xử lý được service CHƯA TỪNG GẶP (DragonflyDB nghe 6379 →
    cache). Tag chỉ là GIẢ THUYẾT: tên đã biết tin cao, cổng tin vừa, không gì →
    generic tin thấp. Nhiều tag cùng lúc (redis = cache + session_store).
    """
    base = (name or "").split("@")[0].strip().lower()
    by_tag: dict[str, CapabilityTag] = {}

    def offer(tag: str, conf: float, prov: str) -> None:
        cur = by_tag.get(tag)
        if cur is None or conf > cur.confidence:
            by_tag[tag] = CapabilityTag(tag=tag, confidence=conf, provenance=prov)

    if base in SERVICE_CAPABILITY:
        offer(SERVICE_CAPABILITY[base], _CONF_NAME, PROV_NAME)
        for sec in SERVICE_SECONDARY.get(base, ()):
            offer(sec, _CONF_SECONDARY, PROV_NAME)
    if port is not None and port in _PORT_CAPABILITY:
        offer(_PORT_CAPABILITY[port], _CONF_PORT, PROV_PORT)
    if not by_tag:
        offer("generic", _CONF_FALLBACK, PROV_FALLBACK)

    return sorted(by_tag.values(), key=lambda t: t.confidence, reverse=True)


def failure_modes_for(tag: str) -> tuple[str, ...]:
    return CAPABILITY_MODES.get(tag, CAPABILITY_MODES["generic"])
