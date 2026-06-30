"""SRE diagnosis planner — DOMAIN candidate generator cho Diagnosis Engine.

Đây là tầng DOMAIN (discipline SRE), tách khỏi lõi ``diagnosis.py`` tổng quát. Nó
biết các nguyên nhân hỏng dịch vụ hay gặp trong production và CÁCH probe chúng thật
trên host. Lõi engine không biết gì về redis/disk/oom — chỉ chạy falsification.

Mỗi candidate = (Hypothesis có predicted_evidence, probe THẬT qua transport). Probe
trả True nếu evidence của giả thuyết CÓ MẶT (giả thuyết được chứng thực). Read-only.
"""
from __future__ import annotations

from aoip.diagnosis import Candidate
from aoip.objects import Hypothesis


def _hyp(claim: str, evidence: str, prior: float) -> Hypothesis:
    return Hypothesis(claim=claim, predicted_evidence=(evidence,), prior=prior, origin="DIAGNOSIS")


def sre_root_cause_candidates(node: str, host: str, transport, *, port: int | None = None,
                              service: str | None = None) -> list[Candidate]:
    """Sinh giả thuyết root-cause + probe THẬT cho một service hỏng trên host.

    ``service`` = tên unit (vd 'redis-server'); ``port`` = cổng dịch vụ. Probe đọc
    trạng thái thật: systemctl, df, journalctl OOM, ss listen. Không đọc nội dung.
    """
    svc = service or node.split(":", 1)[-1]

    async def _probe_process_dead() -> bool:
        out, _ = await transport.run(["systemctl", "is-active", svc])
        state = out.strip().lower()
        return state in ("inactive", "failed", "deactivating")

    async def _probe_disk_full() -> bool:
        out, _ = await transport.run(["df", "--output=pcent", "/"])
        return any(tok.strip().rstrip("%").isdigit() and int(tok.strip().rstrip("%")) >= 95
                   for tok in out.split())

    async def _probe_oom() -> bool:
        out, _ = await transport.run(
            ["bash", "-c", f"journalctl -k --since '-10 min' 2>/dev/null | grep -i 'killed process.*{svc}\\|out of memory' | tail -1"])
        return bool(out.strip())

    async def _probe_network_partition() -> bool:
        # Network partition: tiến trình CÒN chạy nhưng cổng không reachable.
        if port is None:
            return False
        active, _ = await transport.run(["systemctl", "is-active", svc])
        if active.strip().lower() != "active":
            return False  # tiến trình chết → không phải network
        out, _ = await transport.run(
            ["bash", "-c", f'timeout 1 bash -c "exec 3<>/dev/tcp/127.0.0.1/{port}" && echo OPEN || echo CLOSED'])
        return "CLOSED" in out

    candidates: list[Candidate] = [
        (_hyp(f"{node}: process dead/crashed", f"systemctl {svc} inactive/failed", 0.45),
         _probe_process_dead),
        (_hyp(f"{node}: disk full on host", "df / ≥95%", 0.30), _probe_disk_full),
        (_hyp(f"{node}: OOM-killed", "kernel OOM log for service", 0.30), _probe_oom),
        (_hyp(f"{node}: network/port unreachable (process up)", "process active but port closed", 0.30),
         _probe_network_partition),
    ]
    return candidates
