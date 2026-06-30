"""LiveHostDiscoveryBackend — discovery chạm MÁY THẬT (EPIC 1, no mock).

Vì sao tồn tại: chỉ thị "EVERYTHING MUST TOUCH A REAL MACHINE". Backend này KHÔNG
nhận profile bịa — nó đọc tiến trình thật (psutil) và probe cổng thật (TCP connect)
trên chính host đang chạy. Cài đặt HostDiscoveryBackend Protocol nên cắm thẳng vào
Mission understand_host mà không đổi gì khác.

Read-only, unprivileged, an toàn (INV_NO_DATA_EXFIL): chỉ tên tiến trình + trạng
thái cổng, KHÔNG đọc nội dung. Cross-platform (macOS/Linux) vì chỉ dùng socket
connect + psutil.process_iter — không phụ thuộc systemctl/ss (sẽ bổ sung collector
Linux-native ở bước sau khi deploy thật yêu cầu).
"""
from __future__ import annotations

import asyncio
import socket

try:
    import psutil
except ImportError:  # pragma: no cover - psutil là dependency runtime
    psutil = None

# Service phổ biến → cổng quy ước. Probe THẬT mới xác nhận đang mở.
_SERVICE_PORTS: dict[str, list[int]] = {
    "nginx": [80, 443],
    "apache2": [80, 443],
    "httpd": [80, 443],
    "haproxy": [80, 443],
    "redis": [6379],
    "redis-server": [6379],
    "postgres": [5432],
    "postgresql": [5432],
    "mysql": [3306],
    "mysqld": [3306],
    "mariadb": [3306],
    "mongod": [27017],
    "mongodb": [27017],
    "rabbitmq": [5672],
    "kafka": [9092],
    "elasticsearch": [9200],
    "memcached": [11211],
    "prometheus": [9090],
    "grafana": [3000],
    "ollama": [11434],
}

# Cổng "ứng dụng" hay gặp; mở mà không thuộc service nào đã biết → port_owner Unknown.
_EXTRA_SCAN_PORTS: list[int] = [8080, 8000, 5000, 9000, 8443, 3001]

_PROBE_TIMEOUT = 0.3


class LiveHostDiscoveryBackend:
    """Sensor thật trên host: tiến trình thật + cổng thật."""

    def __init__(self, *, extra_service_ports: dict[str, list[int]] | None = None) -> None:
        self._service_ports = {**_SERVICE_PORTS, **(extra_service_ports or {})}

    async def probe_port(self, host: str, port: int) -> bool:
        """TCP connect THẬT tới (host, port). True nếu chấp nhận kết nối."""
        target = "127.0.0.1" if host in ("localhost", socket.gethostname()) else host
        try:
            fut = asyncio.open_connection(target, port)
            reader, writer = await asyncio.wait_for(fut, timeout=_PROBE_TIMEOUT)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    def _running_process_names(self) -> set[str]:
        if psutil is None:
            return set()
        names: set[str] = set()
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if name:
                names.add(name)
        return names

    async def discover(self, host: str) -> dict:
        """Khám phá THẬT: service nào đang nghe cổng nào trên host này."""
        proc_names = self._running_process_names()

        # Probe đồng thời mọi cổng quan tâm (service ports + extra scan).
        candidate_ports = sorted(
            {p for ports in self._service_ports.values() for p in ports}
            | set(_EXTRA_SCAN_PORTS)
        )
        results = await asyncio.gather(
            *(self.probe_port("127.0.0.1", p) for p in candidate_ports)
        )
        open_ports = {port for port, is_open in zip(candidate_ports, results) if is_open}

        def _proc_present(svc: str) -> bool:
            base = svc.split("-")[0]
            return any(base in n for n in proc_names)

        services: list[dict] = []
        claimed_ports: set[int] = set()
        unknowns: list[str] = []

        # Never assume: cổng mở CHỈ được gán tên service khi có tiến trình thật khớp.
        # Một cổng → một service (tiến trình khớp đầu tiên), tránh bịa nhiều daemon.
        for port in sorted(open_ports):
            matched = next(
                (name for name, ports in self._service_ports.items()
                 if port in ports and _proc_present(name)),
                None,
            )
            if matched:
                services.append({"name": matched, "port": port})
                claimed_ports.add(port)
            else:
                # Có thứ đang nghe nhưng không định danh được chủ → hỏi/điều tra.
                unknowns.append(f"port_owner:{port}")

        # Tiến trình service-like đang chạy nhưng cổng quy ước đóng → gap thật.
        for name, ports in self._service_ports.items():
            if _proc_present(name) and not any(p in open_ports for p in ports):
                unknowns.append(f"service_port:{name}")

        return {
            "host": host,
            "services": services,
            "unknowns": sorted(set(unknowns)),
            "relationships": [],  # topology hint từ config: epic sau (cần collector thật)
        }
