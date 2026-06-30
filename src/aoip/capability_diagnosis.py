"""Capability diagnosis planner — sinh candidate theo CAPABILITY, không theo service.

Thay cho mọi ``<service>_diagnosis.py``: planner này phân loại service → capability,
tra tập failure mode của capability, rồi để Failure Mode catalog sinh (Hypothesis,
probe) thật. Diagnosis Engine (core) chạy falsification trên đó.

Luồng: Service (metadata) → classify → CAPABILITY TAGS → Failure Modes → Probe → Candidate.
Một service mang NHIỀU tag (cache + session_store) → HỢP (union) failure mode mọi tag.
Thêm Redis-killer mới (DragonflyDB…) = 0 dòng nếu cổng đã biết, hoặc 1 dòng map tên.
"""
from __future__ import annotations

from aoip.capability_catalog import classify_capability_tags, failure_modes_for
from aoip.diagnosis import Candidate
from aoip.failure_modes import FAILURE_MODES


def capability_root_cause_candidates(
    node: str, host: str, transport, *, service: str | None = None, port: int | None = None,
) -> list[Candidate]:
    """Sinh giả thuyết root-cause + probe THẬT cho service hỏng, qua capability tags.

    Nhiều tag → union failure mode (giữ thứ tự xuất hiện, khử trùng). Probe ba trạng
    thái sẽ tự loại mode không áp dụng (UNAVAILABLE) thay vì bịa counter-evidence.
    """
    svc = service or node.split(":", 1)[-1]
    tags = classify_capability_tags(svc, port=port)
    mode_names: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        for mode_name in failure_modes_for(tag.tag):
            if mode_name not in seen:
                seen.add(mode_name)
                mode_names.append(mode_name)
    params = {"service": svc, "port": port}
    candidates: list[Candidate] = []
    for mode_name in mode_names:
        mode = FAILURE_MODES.get(mode_name)
        if mode is not None:
            candidates.append(mode.candidate(node, host, transport, params))
    return candidates
