"""Tests: Diagnosis planner theo CAPABILITY TAGS, không hardcode từng service.

Reviewer (hardening): đừng viết redis_diagnosis/postgres_diagnosis. Tri thức ở mức
Failure Mode (ProcessDown/OOM/Disk/Network…); capability_tags (cache/database/http/
queue) map sang tập failure mode; Service map sang nhiều tag (mỗi tag có confidence
+ provenance). Port/name chỉ tạo GIẢ THUYẾT tag, KHÔNG thành Fact. Thêm service mới
= 1 dòng / hoặc tự suy từ cổng — KHÔNG file mới. Core Diagnosis bất biến.
"""
from __future__ import annotations

from aoip.capability_catalog import (
    PROV_NAME,
    PROV_PORT,
    classify_capability_tags,
)
from aoip.capability_diagnosis import capability_root_cause_candidates


class FakeTransport:
    target = "h"

    def __init__(self, active="inactive"):
        self._active = active

    async def run(self, argv, *, timeout=15.0):
        if "is-active" in " ".join(argv):
            return (self._active + "\n", 0)
        return ("", 0)


def _tags(name, **kw):
    return {t.tag for t in classify_capability_tags(name, **kw)}


def test_classify_by_known_name_high_confidence():
    assert "cache" in _tags("redis-server")
    assert "database" in _tags("postgres")
    assert "database" in _tags("mariadbd")
    assert "http" in _tags("nginx")
    assert "queue" in _tags("kafka")
    # tên đã biết → provenance service-name, confidence cao.
    primary = classify_capability_tags("redis-server")[0]
    assert primary.provenance == PROV_NAME and primary.confidence >= 0.7


def test_service_carries_multiple_tags_with_confidence():
    # redis vừa là cache (chính) vừa là session_store (phụ, confidence thấp hơn).
    tags = classify_capability_tags("redis")
    by = {t.tag: t for t in tags}
    assert {"cache", "session_store"} <= set(by)
    assert by["cache"].confidence > by["session_store"].confidence


def test_classify_unknown_name_by_port_is_hypothesis_not_fact():
    # DragonflyDB chưa từng biết, nghe 6379 → giả thuyết Cache, provenance port, tin VỪA.
    tags = classify_capability_tags("dragonfly-x", port=6379)
    cache = next(t for t in tags if t.tag == "cache")
    assert cache.provenance == PROV_PORT
    assert cache.confidence < 0.7  # suy từ cổng < tên đã biết (chỉ là giả thuyết)
    # không tên, không cổng gợi ý → generic (vẫn chẩn đoán được, không vỡ).
    assert _tags("totally-unknown") == {"generic"}


def test_cache_and_database_share_universal_modes_no_per_service_file():
    cache = capability_root_cause_candidates("svc:redis", "h", FakeTransport(), service="redis", port=6379)
    db = capability_root_cause_candidates("svc:pg", "h", FakeTransport(), service="postgres", port=5432)
    cache_modes = {h.claim.split(": ", 1)[1] for h, _ in cache}
    db_modes = {h.claim.split(": ", 1)[1] for h, _ in db}
    base = {"process_down", "oom_killed", "disk_full", "network_unreachable"}
    assert base <= cache_modes
    assert base <= db_modes
    assert all(h.predicted_evidence for h, _ in cache)


def test_unseen_service_still_diagnosable_via_port():
    # Dragonfly (chưa biết) trên 6379 → phân loại Cache → vẫn sinh candidate.
    cands = capability_root_cause_candidates("svc:dragonfly", "h", FakeTransport(),
                                             service="dragonfly-x", port=6379)
    assert cands  # không rỗng — AI xử lý được service chưa từng gặp
    assert any("process_down" in h.claim for h, _ in cands)


def test_modes_unioned_across_tags_no_duplicates():
    # GUARD dedup: redis = cache + session_store; cả hai cùng có process_down,
    # network_unreachable. Failure mode TRÙNG chỉ được sinh 1 candidate (1 probe) —
    # nếu không coverage/confidence sẽ bị đếm hai lần và sai.
    from aoip.capability_catalog import classify_capability_tags, failure_modes_for
    cands = capability_root_cause_candidates("svc:redis", "h", FakeTransport(),
                                             service="redis", port=6379)
    modes = [h.claim.split(": ", 1)[1] for h, _ in cands]
    assert len(modes) == len(set(modes))  # khử trùng union
    # số candidate đúng bằng kích thước HỢP các tag (không nhân đôi).
    union = set()
    for t in classify_capability_tags("redis", port=6379):
        union |= set(failure_modes_for(t.tag))
    assert len(cands) == len(union)


async def test_dedup_keeps_coverage_correct():
    # GUARD: tổng (findings+rejected+untested) == số mode DUY NHẤT, không phình do trùng tag.
    from aoip.diagnosis import diagnose
    cands = capability_root_cause_candidates("svc:redis", "h", FakeTransport(active="active"),
                                             service="redis", port=6379)
    result = await diagnose(cands)
    total = len(result.findings) + len(result.rejected) + len(result.untested)
    assert total == len(cands) == len({h.claim for h, _ in cands})


async def test_candidate_probe_runs_three_valued_falsification():
    from aoip.diagnosis import diagnose
    # process inactive → process_down PRESENT; df/journalctl không chạy trên fake →
    # UNAVAILABLE (KHÔNG bị bác bỏ sai), không phải counter-evidence.
    cands = capability_root_cause_candidates("svc:redis", "h", FakeTransport(active="inactive"),
                                             service="redis", port=6379)
    result = await diagnose(cands)
    assert any("process_down" in f.claim for f in result.findings)


async def test_unavailable_probe_is_not_counter_evidence():
    from aoip.diagnosis import diagnose
    # service active → process_down ABSENT; network probe mở/đóng tuỳ; nhưng disk
    # (df rỗng trên fake) → UNAVAILABLE chứ KHÔNG bị tính là "đã loại".
    cands = capability_root_cause_candidates("svc:redis", "h", FakeTransport(active="active"),
                                             service="redis", port=6379)
    result = await diagnose(cands)
    assert any("disk_full" in u[0] for u in result.untested)  # disk không kiểm được
