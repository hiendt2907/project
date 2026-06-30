"""Tri thức tiên nghiệm của Senior SRE: service → cổng thường mở.

Đây là "kinh nghiệm" (Experience/Pattern theo LEARNING_MODEL) được vật-chất-hóa
thành dữ liệu, KHÔNG phải noun mới. Nó cấp prior cho Expectation (= Hypothesis):
"thấy nginx thì kỳ vọng 80/443". Bảng này sẽ được học/bồi đắp về sau; hiện seed
tay đủ để đóng vòng Observe→Expect→Compare→Finding.
"""
from __future__ import annotations

# Khóa = base name đã chuẩn hóa (bỏ '@instance', hậu tố 'd', lowercase).
_EXPECTED_PORTS: dict[str, tuple[int, ...]] = {
    "nginx": (80, 443),
    "apache2": (80, 443),
    "httpd": (80, 443),
    "haproxy": (80, 443),
    "redis": (6379,),
    "redis-server": (6379,),
    "mariadb": (3306,),
    "mysql": (3306,),
    "postgres": (5432,),
    "postgresql": (5432,),
    "mongodb": (27017,),
    "mongod": (27017,),
    "kafka": (9092,),
    "rabbitmq": (5672,),
    "proxysql": (6033, 6032),
}


def _normalize(service_name: str) -> str:
    base = service_name.split("@")[0].strip().lower()
    # 'mariadbd' → 'mariadb', 'mongod' đã có khóa riêng; chỉ tỉa 'd' khi giúp khớp.
    if base not in _EXPECTED_PORTS and base.endswith("d") and base[:-1] in _EXPECTED_PORTS:
        return base[:-1]
    return base


def expected_ports(service_name: str) -> tuple[int, ...]:
    """Cổng kỳ vọng cho một service. () nếu chưa có tri thức (→ KHÔNG kỳ vọng)."""
    return _EXPECTED_PORTS.get(_normalize(service_name), ())
