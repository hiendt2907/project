"""OrbVMDiscoveryBackend — discovery chạm VM Linux THẬT qua OrbStack (EPIC 1).

Vì sao tồn tại: chỉ thị "EVERYTHING MUST TOUCH A REAL MACHINE". Backend này shell
vào một VM Ubuntu thật (`orb -m <vm>`), chạy `ss`/`systemctl` THẬT để lấy service
+ cổng đang nghe, và probe cổng bằng /dev/tcp THẬT trên chính VM đó. Cài đặt
HostDiscoveryBackend Protocol nên cắm thẳng vào Mission understand_host.

Read-only, không đọc nội dung file (INV_NO_DATA_EXFIL). Đây là tiền đề của agent
thật: cùng các lệnh Linux-native mà `src/remote_agent/discovery.py` dùng, nhưng
chạy end-to-end qua Mission Runtime.
"""
from __future__ import annotations

import asyncio
import re


async def _orb(vm: str, *argv: str, timeout: float = 15.0) -> tuple[str, int]:
    """Chạy một lệnh trên VM qua orb. Trả (stdout, returncode). Không raise."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "orb", "-m", vm, *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode(errors="replace"), proc.returncode or 0
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        return "", 1


class OrbVMDiscoveryBackend:
    """Sensor thật trên một VM OrbStack: ss + systemctl + /dev/tcp probe."""

    def __init__(self, vm: str) -> None:
        self.vm = vm

    async def _listeners(self) -> list[dict]:
        """Parse `ss -Htlnp`: mỗi LISTEN → {port, service}. Cần sudo để thấy proc."""
        out, rc = await _orb(self.vm, "sudo", "ss", "-Htlnp")
        if rc != 0 or not out.strip():
            out, _ = await _orb(self.vm, "ss", "-Htlnp")
        listeners: list[dict] = []
        seen: set[int] = set()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            local = parts[3]
            raw_port = local.rsplit(":", 1)[-1]
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
        """Topology THẬT từ cấu hình cấu trúc (metadata, không exfil nội dung).

        Hai nguồn read-only:
          - systemd unit ``Environment=*_HOST=`` → service phụ thuộc host backend.
          - nginx ``proxy_pass`` → reverse-proxy tới upstream.
        """
        rels: list[dict] = []

        # systemd Environment *_HOST → edge depends_on.
        out, _ = await _orb(
            self.vm, "bash", "-c",
            "for f in /etc/systemd/system/*.service; do "
            "u=$(basename \"$f\" .service); "
            "grep -h 'Environment=' \"$f\" 2>/dev/null | sed \"s|^|$u |\"; done",
        )
        for line in out.splitlines():
            m = re.search(r"^(\S+).*?(\w+)_HOST=([A-Za-z0-9_.-]+)", line)
            if m:
                rels.append({
                    "source": m.group(1), "relation": "depends_on",
                    "target": m.group(3), "evidence": f"systemd.env.{m.group(2)}_HOST",
                })

        # nginx proxy_pass → edge proxies_to.
        out, _ = await _orb(
            self.vm, "bash", "-c",
            "grep -rhoE 'proxy_pass[[:space:]]+[^;]+' /etc/nginx 2>/dev/null",
        )
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
        """Probe THẬT trên VM bằng /dev/tcp (timeout 1s)."""
        out, rc = await _orb(
            self.vm, "bash", "-c",
            f'timeout 1 bash -c "exec 3<>/dev/tcp/127.0.0.1/{port}" && echo OPEN',
            timeout=5.0,
        )
        return "OPEN" in out
