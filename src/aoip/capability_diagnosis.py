"""Capability diagnosis planner — sinh candidate theo CAPABILITY, không theo service.

Thay cho mọi ``<service>_diagnosis.py``: planner này phân loại service → capability,
tra tập failure mode của capability, rồi để Failure Mode catalog sinh (Hypothesis,
probe) thật. Diagnosis Engine (core) chạy falsification trên đó.

Luồng: Service (metadata) → classify → Capability → Failure Modes → Probe → Candidate.
Thêm Redis-killer mới (DragonflyDB…) = 0 dòng nếu cổng đã biết, hoặc 1 dòng map tên.
"""
from __future__ import annotations

from aoip.capability_catalog import classify_service, failure_modes_for
from aoip.diagnosis import Candidate
from aoip.failure_modes import FAILURE_MODES


def capability_root_cause_candidates(
    node: str, host: str, transport, *, service: str | None = None, port: int | None = None,
) -> list[Candidate]:
    """Sinh giả thuyết root-cause + probe THẬT cho service hỏng, qua capability."""
    svc = service or node.split(":", 1)[-1]
    capability = classify_service(svc, port=port)
    params = {"service": svc, "port": port}
    candidates: list[Candidate] = []
    for mode_name in failure_modes_for(capability):
        mode = FAILURE_MODES.get(mode_name)
        if mode is not None:
            candidates.append(mode.candidate(node, host, transport, params))
    return candidates
