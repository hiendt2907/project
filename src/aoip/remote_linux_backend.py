"""RemoteLinuxBackend — Linux discovery adapter, độc lập môi trường (EPIC 2).

Vì sao tồn tại: đây là "Linux Adapter" trong sơ đồ Discovery Interface. Cùng một
logic (ss/systemctl/env/nginx) chạy trên BẤT KỲ Linux nào — EC2, bare-metal,
VMware, hay OrbStack lab — vì mọi I/O đi qua ``CommandTransport``. Backend KHÔNG
biết môi trường: không có 'orb', không 'ssh' hard-code. Đổi nơi deploy = đổi
transport, không sửa backend.

Cài đặt HostDiscoveryBackend Protocol → cắm thẳng Mission understand_host. Read-
only, không đọc nội dung file (INV_NO_DATA_EXFIL): chỉ tên service, cổng, và tham
chiếu topology dạng cấu trúc (env *_HOST, nginx proxy_pass).
"""
from __future__ import annotations

import re

from aoip.transport import CommandTransport


class RemoteLinuxBackend:
    def __init__(self, transport: CommandTransport) -> None:
        self._t = transport

    async def _listeners(self) -> list[dict]:
        """Parse ``ss -Htlnp``: mỗi LISTEN → {port, service}. sudo để thấy process."""
        out, rc = await self._t.run(["sudo", "ss", "-Htlnp"])
        if rc != 0 or not out.strip():
            out, _ = await self._t.run(["ss", "-Htlnp"])
        listeners: list[dict] = []
        seen: set[int] = set()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            raw_port = parts[3].rsplit(":", 1)[-1]
            if not raw_port.isdigit():
                continue
            port = int(raw_port)
            if port in seen:
                continue
            seen.add(port)
            m = re.search(r'users:\(\("([^"]+)"', line)
            listeners.append({"port": port, "service": m.group(1) if m else ""})
        return listeners

    async def _relationships(self) -> list[dict]:
        """Topology THẬT từ cấu hình cấu trúc (metadata, không exfil nội dung)."""
        rels: list[dict] = []
        out, _ = await self._t.run([
            "bash", "-c",
            "for f in /etc/systemd/system/*.service; do "
            "u=$(basename \"$f\" .service); "
            "grep -h 'Environment=' \"$f\" 2>/dev/null | sed \"s|^|$u |\"; done",
        ])
        for line in out.splitlines():
            m = re.search(r"^(\S+).*?(\w+)_HOST=([A-Za-z0-9_.-]+)", line)
            if m:
                rels.append({
                    "source": m.group(1), "relation": "depends_on",
                    "target": m.group(3), "evidence": f"systemd.env.{m.group(2)}_HOST",
                })
        out, _ = await self._t.run([
            "bash", "-c",
            "grep -rhoE 'proxy_pass[[:space:]]+[^;]+' /etc/nginx 2>/dev/null",
        ])
        for line in out.splitlines():
            m = re.search(r"proxy_pass\s+https?://([A-Za-z0-9_.:-]+)", line)
            if m:
                rels.append({
                    "source": "nginx", "relation": "proxies_to",
                    "target": m.group(1), "evidence": "nginx.proxy_pass",
                })
        return rels

    async def discover(self, host: str) -> dict:
        listeners = await self._listeners()
        services: list[dict] = []
        unknowns: list[str] = []
        for lsn in listeners:
            svc = (lsn.get("service") or "").strip()
            if svc:
                services.append({"name": svc, "port": lsn["port"]})
            else:
                unknowns.append(f"port_owner:{lsn['port']}")
        return {
            "host": host,
            "services": services,
            "unknowns": sorted(set(unknowns)),
            "relationships": await self._relationships(),
        }

    async def probe_port(self, host: str, port: int) -> bool:
        """Probe THẬT trên host qua transport (/dev/tcp, timeout 1s)."""
        out, _ = await self._t.run(
            ["bash", "-c",
             f'timeout 1 bash -c "exec 3<>/dev/tcp/127.0.0.1/{port}" && echo OPEN'],
            timeout=5.0,
        )
        return "OPEN" in out
