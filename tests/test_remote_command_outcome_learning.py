"""TDD: đóng vòng học remote-agent (Đ8 next-step #2).

`reconcile_one` (remote_command_outcome_loop.py) trước đây chỉ ghi CRAT audit
+ publish `omni-action-feedback` khi một lệnh remote (systemd.*) qua kênh lệnh
bền thành công — KHÔNG bao giờ upsert `action_experience`. Reflex mới
(`_try_known_fix_reflex` trong knowledge_pipeline.py, qua
`remote_known_fix.try_remote_known_fix` -> `known_fix_resolver.find_known_fix_candidate`)
đọc đúng collection đó nhưng collection trống cho capability class
`systemd.*` — không có gì để nhớ lại. Test này khoá lại: một remote recovery
verified thành công PHẢI ghi một điểm mà `find_known_fix_candidate` đọc lại
được nguyên vẹn (round-trip write→read).

Cập nhật 2026-08-04: thất bại nay CŨNG phải ghi (``exec_outcome="fail"``,
``auto_execute=False``) — hệ thống chỉ học từ lần đúng thì không bao giờ biết
cái gì KHÔNG dùng được. Tính an toàn "không lặp lại cách sửa đã sai" được chứng
minh trực tiếp bằng `test_failed_experience_is_never_returned_as_a_fix_candidate`
thay vì bằng việc không ghi gì cả.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from pkg.reasoning.known_fix_resolver import find_known_fix_candidate
from workers import auto_recovery_bridge as arb
from workers import remote_command_outcome_loop as rcol


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, ex=None, **kw):
        self.kv[key] = value
        return True

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def zrange(self, key, start, end):
        items = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
        return [m for m, _ in items[start:end + 1 if end >= 0 else None]]

    async def zscore(self, key, member):
        return self.zsets.get(key, {}).get(member)

    async def zrem(self, key, member):
        return 1 if self.zsets.get(key, {}).pop(member, None) is not None else 0


class FakeKafka:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_dict(self, topic, msg, key=None):
        self.sent.append((topic, msg))


class FakeLLM:
    async def embed(self, model: str, input: str) -> dict:
        return {"embedding": [0.2] * 768}


class _FakePoint:
    def __init__(self, id, vector, payload):
        self.id = id
        self.vector = vector
        self.payload = payload
        self.score = 0.99


class _FakeQueryResponse:
    def __init__(self, points):
        self.points = points


class FakeVectorStore:
    """Records every upsert; `query_points` replays the last upsert so the
    round-trip test can feed it straight into the reader."""

    def __init__(self) -> None:
        self.upserts: list[dict[str, Any]] = []

    async def upsert(self, *, collection_name: str, points: list[Any]) -> None:
        self.upserts.append({"collection_name": collection_name, "points": points})

    async def query_points(self, *, collection_name, query, limit, score_threshold, with_payload):
        last_points = self.upserts[-1]["points"] if self.upserts else []
        return _FakeQueryResponse([_FakePoint(p.id, p.vector, p.payload) for p in last_points])


def _settings():
    return SimpleNamespace(
        omni_gateway_api_key="test-key",
        omni_gateway_internal_url="http://gw.local",
        kafka_topic_audit_chain="omni-audit-chain",
        kafka_topic_action_feedback="omni-action-feedback",
        embed_model="nomic-embed-text:latest",
        memory_canonical_strip_pods=True,
    )


@pytest.fixture
def ctx():
    return SimpleNamespace(
        redis=FakeRedis(), kafka=FakeKafka(), settings=_settings(),
        llm=FakeLLM(), vector_store=FakeVectorStore(),
    )


@pytest.fixture
def audit_ok(monkeypatch):
    async def _write(**kwargs):
        return {"block_hash": "deadbeef"}

    monkeypatch.setattr("services.audit_ledger.chain_writer.write_audit_block", _write)
    return []


def _seed(ctx, *, state="COMPLETED", rc=0, tenant="t1", cid="cmd-1", trace="tr-1",
          unit="payment-api.service", capability="systemd.restart_unit"):
    ctx.redis.kv[f"omni:cmd:rec:{tenant}:{cid}"] = __import__("json").dumps({
        "command_id": cid, "agent_id": "lab_agent", "state": state,
        "delivery_attempt": 1, "action_id": "act-1", "canonical_scope": f"{tenant}:svc:x",
        "incident_id": trace,
        "outcome": {"status": "recovered" if rc == 0 else "aborted", "rc": rc,
                    "reason": "service + dependents verified",
                    "evidence": ["before=inactive", "service_health=ok"],
                    "verified": rc == 0},
    })
    ctx.redis.kv[f"omni:autorecovery:meta:{tenant}:{cid}"] = __import__("json").dumps({
        "trace_id": trace, "agent_id": "lab_agent", "unit": unit, "capability": capability,
    })
    ctx.redis.zsets.setdefault(arb.PENDING_KEY, {})[arb.pending_member(tenant, cid)] = time.time()


# ── 1. Success writes a reader-compatible point ─────────────────────────────

async def test_completed_command_upserts_action_experience(ctx, audit_ok):
    _seed(ctx)
    assert await rcol.reconcile_one(ctx, "t1", "cmd-1") == "done"

    assert len(ctx.vector_store.upserts) == 1
    call = ctx.vector_store.upserts[0]
    # Đ55: action_experience nay scope theo tenant ("t1" != default tenant) —
    # trước fix này mọi tenant ghi chung 1 collection, xem docstring
    # _upsert_action_experience cho sự cố thật đã xảy ra vì thiếu cách ly.
    assert call["collection_name"] == "action_experience:t1"
    payload = call["points"][0].payload
    assert payload["exec_outcome"] == "success"
    assert payload["auto_execute"] is True
    assert payload["tool"] == "systemd.restart_unit"
    assert payload["args"] == {"unit": "payment-api.service"}
    assert payload["trace_id"] == "tr-1"


async def test_upserted_point_round_trips_through_known_fix_resolver(ctx, audit_ok):
    """Prove vòng học thực sự khép: điểm ghi ra phải là điểm mà chính cơ chế
    reflex (find_known_fix_candidate) đọc lại được và coi là ứng viên hợp lệ."""
    _seed(ctx)
    assert await rcol.reconcile_one(ctx, "t1", "cmd-1") == "done"

    candidate, reason = await find_known_fix_candidate(
        ctx,
        query_text="payment-api service down on host cust-app",
        score_threshold=0.5,
        host_scope=frozenset({"payment-api.service"}),
        valid_tools=arb._SUPPORTED_CAPABILITIES,
        tenant_id="t1",
    )
    assert reason == "ok"
    assert candidate is not None
    assert candidate.tool == "systemd.restart_unit"
    assert candidate.args == {"unit": "payment-api.service"}


# ── 2. Failure PHẢI được ghi lại — nhưng không bao giờ được đem ra áp dụng ───
#
# Đổi hợp đồng 2026-08-04. Bản cũ khẳng định thất bại "KHÔNG được ghi gì", với
# lý do đúng: không dạy hệ thống lặp lại một cách sửa đã biết là sai. Nhưng cách
# bảo đảm đó quá tay — nó cũng vứt luôn bài học. Đo thật: 3/15 outcome gần nhất
# là FAILED với lý do rất giàu thông tin ("không có operator cho
# ('failed_state_stale','systemd')") và tất cả biến mất không dấu vết, nên hệ
# thống sẽ thử lại đúng cách sai đó mãi mãi.
#
# Tính an toàn nay được chứng minh TRỰC TIẾP thay vì bằng sự vắng mặt:
# `find_known_fix_candidate` lọc `exec_outcome != "success"` (known_fix_resolver
# ~dòng 146), nên điểm thất bại KHÔNG BAO GIỜ trở thành ứng viên sửa lỗi. Nó chỉ
# nằm trong kho cho LLM đọc: "đã thử cách này, hỏng vì X".


async def test_failed_command_upserts_experience_marked_as_failure(ctx, audit_ok):
    _seed(ctx, state="FAILED", rc=1)
    assert await rcol.reconcile_one(ctx, "t1", "cmd-1") == "done"

    assert len(ctx.vector_store.upserts) == 1, "thất bại phải để lại bài học"
    payload = ctx.vector_store.upserts[0]["points"][0].payload
    assert payload["exec_outcome"] == "fail"
    assert payload["auto_execute"] is False, "bài học hỏng không được phép tự chạy lại"
    assert payload["verification_result"] == "fail"
    assert payload["failure_reason"], "phải giữ lý do hỏng để lần sau tránh"


async def test_failed_experience_is_never_returned_as_a_fix_candidate(ctx, audit_ok):
    """Bằng chứng an toàn cốt lõi: ghi bài học thất bại KHÔNG mở đường cho reflex
    đem chính cách sửa đã hỏng ra dùng lại."""
    _seed(ctx, state="FAILED", rc=1)
    assert await rcol.reconcile_one(ctx, "t1", "cmd-1") == "done"

    candidate, reason = await find_known_fix_candidate(
        ctx,
        query_text="payment-api service down on host cust-app",
        score_threshold=0.5,
        host_scope=frozenset({"payment-api.service"}),
        valid_tools=arb._SUPPORTED_CAPABILITIES,
        tenant_id="t1",
    )
    assert candidate is None, "cách sửa đã thất bại bị đem ra dùng lại"
    assert reason != "ok"


async def test_expired_command_upserts_experience_marked_as_failure(ctx, audit_ok):
    _seed(ctx, state="EXPIRED", rc=1)
    assert await rcol.reconcile_one(ctx, "t1", "cmd-1") == "done"

    assert len(ctx.vector_store.upserts) == 1
    payload = ctx.vector_store.upserts[0]["points"][0].payload
    assert payload["exec_outcome"] == "fail"
    assert payload["auto_execute"] is False


# ── 3. Learning write is best-effort, never breaks the outcome path ─────────

async def test_upsert_failure_does_not_turn_success_into_retry(ctx, audit_ok, monkeypatch):
    _seed(ctx)

    async def _boom(*a, **kw):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(ctx.vector_store, "upsert", _boom)
    assert await rcol.reconcile_one(ctx, "t1", "cmd-1") == "done"
    topics = [t for t, _ in ctx.kafka.sent]
    assert "omni-action-feedback" in topics, "feedback must still publish despite learning-write failure"


async def test_reconcile_one_unaffected_when_ctx_has_no_vector_store(audit_ok):
    """Regression: existing callers that build a bare ctx (no llm/vector_store,
    as in test_remote_auto_execute_loop.py) must keep working unchanged."""
    ctx = SimpleNamespace(redis=FakeRedis(), kafka=FakeKafka(), settings=_settings())
    _seed(ctx)
    assert await rcol.reconcile_one(ctx, "t1", "cmd-1") == "done"
    topics = [t for t, _ in ctx.kafka.sent]
    assert "omni-action-feedback" in topics
