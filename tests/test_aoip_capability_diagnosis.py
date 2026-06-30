"""Tests: Diagnosis planner theo CAPABILITY, không hardcode từng service.

Reviewer: đừng viết redis_diagnosis/postgres_diagnosis. Tri thức ở mức Failure Mode
(ProcessDown/OOM/Disk/Network…); Capability (Cache/Database/HTTP/Queue) map sang
tập failure mode; Service chỉ là metadata map sang Capability (hoặc suy từ cổng).
Thêm service mới = 1 dòng / hoặc tự suy — KHÔNG file mới. Core Diagnosis bất biến.
"""
from __future__ import annotations

from aoip.capability_catalog import classify_service
from aoip.capability_diagnosis import capability_root_cause_candidates


class FakeTransport:
    target = "h"

    def __init__(self, active="inactive"):
        self._active = active

    async def run(self, argv, *, timeout=15.0):
        if "is-active" in " ".join(argv):
            return (self._active + "\n", 0)
        return ("", 0)


def test_classify_by_known_name():
    assert classify_service("redis-server") == "cache"
    assert classify_service("postgres") == "database"
    assert classify_service("mariadbd") == "database"
    assert classify_service("nginx") == "http"
    assert classify_service("kafka") == "queue"


def test_classify_unknown_name_by_port_signature():
    # DragonflyDB chưa từng biết, nhưng nghe 6379 → suy ra Cache.
    assert classify_service("dragonfly", port=6379) == "cache"
    assert classify_service("yugabyte", port=5432) == "database"
    # không tên, không cổng gợi ý → generic (vẫn chẩn đoán được, không vỡ).
    assert classify_service("totally-unknown") == "generic"


def test_cache_and_database_share_universal_modes_no_per_service_file():
    cache = capability_root_cause_candidates("svc:redis", "h", FakeTransport(), service="redis", port=6379)
    db = capability_root_cause_candidates("svc:pg", "h", FakeTransport(), service="postgres", port=5432)
    cache_modes = {h.claim.split(": ", 1)[1] for h, _ in cache}
    db_modes = {h.claim.split(": ", 1)[1] for h, _ in db}
    # cùng tập failure-mode nền (process/oom/disk/network) — reuse, không trùng lặp code.
    assert {"process_down", "oom_killed", "disk_full", "network_unreachable"} <= cache_modes
    assert {"process_down", "oom_killed", "disk_full", "network_unreachable"} <= db_modes
    assert all(h.predicted_evidence for h, _ in cache)


def test_unseen_service_still_diagnosable_via_port():
    # Dragonfly (chưa biết) trên 6379 → phân loại Cache → vẫn sinh candidate.
    cands = capability_root_cause_candidates("svc:dragonfly", "h", FakeTransport(),
                                             service="dragonfly", port=6379)
    assert cands  # không rỗng — AI xử lý được service chưa từng gặp
    assert any("process_down" in h.claim for h, _ in cands)


async def test_candidate_probe_runs_real_falsification():
    from aoip.diagnosis import diagnose
    # process inactive → process_down sống; còn lại bị bác bỏ.
    cands = capability_root_cause_candidates("svc:redis", "h", FakeTransport(active="inactive"),
                                             service="redis", port=6379)
    result = await diagnose(cands)
    assert any("process_down" in f.claim for f in result.findings)
