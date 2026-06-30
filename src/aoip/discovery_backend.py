"""Discovery backend — seam thu thập bằng chứng từ host (Stage 1 roadmap).

Remote Agent thật (``src/remote_agent/``) là sensor read-only trên host khách. Ở
walking-skeleton dùng ``MockHostDiscoveryBackend`` (chạy không cần host thật) để
ép pipeline understand_host chạy end-to-end. Backend thật wire sau — runtime ép,
không suy diễn trước.

``discover`` trả raw inventory; ``probe_port`` xác minh một cổng có mở thật không
(đẩy Hypothesis→Fact). Read-only: KHÔNG đọc nội dung/exfil (omni-remote-agent).
"""
from __future__ import annotations

from typing import Protocol


class HostDiscoveryBackend(Protocol):
    async def discover(self, host: str) -> dict: ...
    async def probe_port(self, host: str, port: int) -> bool: ...


class MockHostDiscoveryBackend:
    """Deterministic backend cho skeleton/test.

    ``inventory`` mô phỏng kết quả khám phá: services + ports. ``open_ports`` mô
    phỏng cổng thật mở (verify). ``unknowns`` = vùng runtime không tự quyết được
    → sẽ sinh Communication (interview).
    """

    def __init__(
        self,
        *,
        inventory: dict | None = None,
        open_ports: set[int] | None = None,
    ) -> None:
        self._inventory = inventory or {
            "services": [
                {"name": "redis", "port": 6379},
                {"name": "nginx", "port": 80},
                {"name": "postgres", "port": 5432},
            ],
            # owner của redis không xác định được từ host → Unknown (interview).
            "unknowns": ["service_owner:redis"],
        }
        # Mặc định: redis/nginx mở thật; postgres KHÔNG reachable (claim sai).
        self._open = open_ports if open_ports is not None else {6379, 80}

    async def discover(self, host: str) -> dict:
        return {"host": host, **self._inventory}

    async def probe_port(self, host: str, port: int) -> bool:
        return port in self._open


class VMProfileDiscoveryBackend:
    """Adapter: VMProfile THẬT (``run_vm_discovery``) → contract understand_host.

    Vì sao tồn tại: nối Stage-1 Discovery đã có (``src/remote_agent/discovery.py``)
    vào vòng Understanding/SystemModel/Interview của AOIP — integration over
    isolation, real behavior over mock. KHÔNG verb/noun mới.

    Ngữ nghĩa SRE (join listeners↔services):
      - ``listeners``/``open_ports`` = cổng QUAN SÁT được listen → nguồn Fact
        (probe_port = True khi cổng nằm trong tập listener).
      - mỗi listener có ``service`` → một service hypothesis ``{name, port}``.
      - service systemctl chạy nhưng KHÔNG có listener → ``service_port:<name>``
        Unknown (không tự bịa cổng).
      - listener không rõ ``service`` → ``port_owner:<port>`` Unknown.
    Never assume: vùng không suy được thành Communication, không thành Fact.
    """

    def __init__(self, profile: dict) -> None:
        self._profile = profile or {}

    def _listeners(self) -> list[dict]:
        prof = self._profile
        return prof.get("listeners") or prof.get("open_ports") or []

    def _inventory(self) -> dict:
        listeners = self._listeners()
        services: list[dict] = []
        unknowns: list[str] = []
        listening_procs: set[str] = set()

        for lsn in listeners:
            port = lsn.get("port")
            if not isinstance(port, int):
                continue
            svc = (lsn.get("service") or "").strip()
            if svc:
                services.append({"name": svc, "port": port})
                listening_procs.add(svc.lower())
            else:
                unknowns.append(f"port_owner:{port}")

        # Running service (systemctl) mà không khớp listener nào → cổng chưa rõ.
        for s in self._profile.get("services", []):
            name = (s.get("name") or "").strip()
            if not name:
                continue
            base = name.split("@")[0].lower()
            matched = any(base in proc or proc in base for proc in listening_procs)
            if not matched:
                unknowns.append(f"service_port:{name}")

        return {"services": services, "unknowns": unknowns}

    async def discover(self, host: str) -> dict:
        return {"host": host, **self._inventory()}

    async def probe_port(self, host: str, port: int) -> bool:
        return any(lsn.get("port") == port for lsn in self._listeners())
