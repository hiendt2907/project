import json

import pytest
from fakeredis.aioredis import FakeRedis

from rag.kb_feedback import apply_kb_feedback

_KEY = "doc:vendor_knowledge:kb-1"


@pytest.fixture
async def redis():
    r = FakeRedis(decode_responses=True)
    await r.hset(_KEY, mapping={"omni_payload": json.dumps({"title": "x", "score": 60})})
    yield r
    await r.flushall()


async def _payload(redis):
    return json.loads(await redis.hget(_KEY, "omni_payload"))


async def test_confirmed_raises_score(redis):
    summary = await apply_kb_feedback(
        redis,
        trace="t1",
        assessments=[
            {"kb_id": "kb-1", "collection": "vendor_knowledge",
             "verdict": "confirmed", "applicable": True, "reason": "probe ok"}
        ],
    )
    p = await _payload(redis)
    assert p["score"] == 65
    assert p["confirmed_count"] == 1
    assert summary["confirmed"] == 1


async def test_refuted_three_times_marks_stale(redis):
    for i in range(3):
        await apply_kb_feedback(
            redis,
            trace=f"t{i}",
            assessments=[
                {"kb_id": "kb-1", "collection": "vendor_knowledge",
                 "verdict": "refuted", "applicable": True,
                 "reason": "outdated config path"}
            ],
            stale_threshold=3,
        )
    p = await _payload(redis)
    assert p["contradicted_count"] == 3
    assert p["stale"] is True
    assert "outdated config path" in p["stale_for"]
    # 60 - 8*3 = 36, clamped >= 0
    assert p["score"] >= 0
    assert p["score"] == 36


async def test_refuted_clamps_at_zero(redis):
    await redis.hset(_KEY, mapping={"omni_payload": json.dumps({"score": 5})})
    for i in range(3):
        await apply_kb_feedback(
            redis, trace=f"c{i}",
            assessments=[{"kb_id": "kb-1", "collection": "vendor_knowledge",
                          "verdict": "refuted", "applicable": True, "reason": "no"}],
        )
    p = await _payload(redis)
    assert p["score"] == 0


async def test_unverifiable_no_change(redis):
    await apply_kb_feedback(
        redis, trace="t1",
        assessments=[{"kb_id": "kb-1", "collection": "vendor_knowledge",
                      "verdict": "unverifiable", "applicable": True, "reason": "?"}],
    )
    p = await _payload(redis)
    assert p["score"] == 60
    assert "confirmed_count" not in p


async def test_missing_kb(redis):
    summary = await apply_kb_feedback(
        redis, trace="t1",
        assessments=[{"kb_id": "kb-nope", "collection": "vendor_knowledge",
                      "verdict": "confirmed", "applicable": True, "reason": "x"}],
    )
    assert summary["missing"] == 1


async def test_audit_log_written(redis):
    await apply_kb_feedback(
        redis, trace="audit-trace",
        assessments=[{"kb_id": "kb-1", "collection": "vendor_knowledge",
                      "verdict": "confirmed", "applicable": True, "reason": "ok"}],
    )
    entries = await redis.lrange("omni:kb:feedback:log", 0, -1)
    assert len(entries) == 1
    rec = json.loads(entries[0])
    assert rec["trace"] == "audit-trace"
    assert rec["kb_id"] == "kb-1"
    assert rec["new_score"] == 65


async def test_bad_kb_id_skipped(redis):
    summary = await apply_kb_feedback(
        redis, trace="t1",
        assessments=[{"kb_id": "a/b", "collection": "vendor_knowledge",
                      "verdict": "confirmed", "applicable": True, "reason": "x"}],
    )
    assert summary["skipped"] == 1
