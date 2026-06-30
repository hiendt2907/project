"""Failure Mode catalog — tri thức chẩn đoán ở mức CƠ CHẾ, không theo service.

Reviewer: Senior SRE không nghĩ "redis timeout → redis playbook". Họ nghĩ theo
FAILURE MODE: ProcessDown, OOM, DiskFull, NetworkUnreachable, CPUStarvation… Mỗi
failure mode là một cơ chế hỏng PHỔ QUÁT — đúng cho mọi service. Một mode = một
``Hypothesis`` (predicted_evidence) + cách probe THẬT trên host.

Đây là tri thức tái dùng: thêm service mới KHÔNG cần file mới; thêm một cơ chế hỏng
mới = 1 entry ở đây. Service cụ thể chỉ map sang Capability (catalog riêng) rồi
Capability map sang tập failure mode. Probe read-only (INV_NO_DATA_EXFIL).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from aoip.diagnosis import Candidate
from aoip.objects import Hypothesis

# Builder: (host, transport, params) → probe async trả True nếu evidence CÓ MẶT.
ProbeBuilder = Callable[[str, object, dict], Callable]


@dataclass(frozen=True)
class FailureMode:
    name: str
    evidence: str
    prior: float
    build: ProbeBuilder

    def candidate(self, node: str, host: str, transport, params: dict) -> Candidate:
        hyp = Hypothesis(
            claim=f"{node}: {self.name}",
            predicted_evidence=(self.evidence,),
            prior=self.prior,
            origin="DIAGNOSIS",
        )
        return (hyp, self.build(host, transport, params))


# ── Probe builders (cơ chế phổ quát, không biết service là gì) ────────────────
def _process_down(host, transport, params):
    svc = params.get("service", "")

    async def probe():
        out, _ = await transport.run(["systemctl", "is-active", svc])
        return out.strip().lower() in ("inactive", "failed", "deactivating")
    return probe


def _oom_killed(host, transport, params):
    svc = params.get("service", "")

    async def probe():
        out, _ = await transport.run(
            ["bash", "-c",
             f"journalctl -k --since '-10 min' 2>/dev/null | "
             f"grep -i 'out of memory\\|killed process.*{svc}' | tail -1"])
        return bool(out.strip())
    return probe


def _disk_full(host, transport, params):
    async def probe():
        out, _ = await transport.run(["df", "--output=pcent", "/"])
        return any(t.strip().rstrip("%").isdigit() and int(t.strip().rstrip("%")) >= 95
                   for t in out.split())
    return probe


def _network_unreachable(host, transport, params):
    svc = params.get("service", "")
    port = params.get("port")

    async def probe():
        if port is None:
            return False
        active, _ = await transport.run(["systemctl", "is-active", svc])
        if active.strip().lower() != "active":
            return False  # tiến trình chết → là ProcessDown, không phải network
        out, _ = await transport.run(
            ["bash", "-c",
             f'timeout 1 bash -c "exec 3<>/dev/tcp/127.0.0.1/{port}" && echo OPEN || echo CLOSED'])
        return "CLOSED" in out
    return probe


def _cpu_starvation(host, transport, params):
    async def probe():
        out, _ = await transport.run(["cat", "/proc/loadavg"])
        try:
            load1 = float(out.split()[0])
        except (ValueError, IndexError):
            return False
        return load1 >= 32.0  # tải rất cao (ngưỡng bảo thủ, tránh false positive)
    return probe


# Catalog phổ quát — khóa = tên failure mode.
FAILURE_MODES: dict[str, FailureMode] = {
    "process_down": FailureMode("process_down", "systemctl unit inactive/failed", 0.45, _process_down),
    "oom_killed": FailureMode("oom_killed", "kernel OOM log for service", 0.30, _oom_killed),
    "disk_full": FailureMode("disk_full", "root filesystem ≥95%", 0.30, _disk_full),
    "network_unreachable": FailureMode(
        "network_unreachable", "process active but port closed", 0.30, _network_unreachable),
    "cpu_starvation": FailureMode("cpu_starvation", "1-min load average very high", 0.20, _cpu_starvation),
}
