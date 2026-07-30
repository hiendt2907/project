"""Tests for knowledge pipeline — signal routing, confidence score, change detection."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis

from anomaly.remote_host_baseline import (
    ConfidenceLevel,
    score_to_level,
    add_confidence,
    get_confidence_score,
    decay_confidence,
)
from remote_agent.discovery import diff_discovery, is_snapshot_suspect, load_discovery_snapshot
from workers.knowledge_pipeline import (
    handle_knowledge_evidence,
    handle_telegram_doc_upload,
    _LOG_STORE_MAX,
    _CHANGE_PENDING_PREFIX,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def redis():
    return FakeRedis(decode_responses=True)


def _ctx(redis_client):
    return SimpleNamespace(
        redis=redis_client,
        telegram=None,
        telegram_chat_id=None,
        kafka=None,
        settings=SimpleNamespace(),
        ledger=SimpleNamespace(record_exception=AsyncMock()),
    )


# ---------------------------------------------------------------------------
# Phase 1: signal_type routing (INV_KNOWLEDGE_NOT_ALERT)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_signal_type_log_sample_stored(redis):
    ctx = _ctx(redis)
    ev = {
        "signal_type": "LOG_SAMPLE",
        "tenant_id": "t1",
        "extracted_fact": {"agent_id": "agent-001"},
        "ts": "1700000000",
        "alert_hint": "",
        "raw": "2024-01-01 info ok",
    }
    await handle_knowledge_evidence(ctx, ev)
    key = "omni:knowledge:logs:agent-001:rolling"
    entries = await redis.lrange(key, 0, -1)
    assert len(entries) == 1
    record = json.loads(entries[0])
    assert "ts" in record


@pytest.mark.asyncio
async def test_log_sample_rolling_trim(redis):
    ctx = _ctx(redis)
    agent_id = "agent-trim"
    for i in range(_LOG_STORE_MAX + 5):
        ev = {
            "signal_type": "LOG_SAMPLE",
            "tenant_id": "t1",
            "extracted_fact": {"agent_id": agent_id},
            "ts": str(1700000000 + i),
            "alert_hint": "",
            "raw": f"line {i}",
        }
        await handle_knowledge_evidence(ctx, ev)
    key = f"omni:knowledge:logs:{agent_id}:rolling"
    length = await redis.llen(key)
    assert length == _LOG_STORE_MAX


@pytest.mark.asyncio
async def test_unknown_signal_type_noop(redis):
    ctx = _ctx(redis)
    ev = {"signal_type": "BOGUS", "tenant_id": "t1", "extracted_fact": {}}
    # Should not raise
    await handle_knowledge_evidence(ctx, ev)


# ---------------------------------------------------------------------------
# Phase 3: Confidence Score
# ---------------------------------------------------------------------------

def test_score_to_level():
    assert score_to_level(0) == ConfidenceLevel.STATIC_GUARD
    assert score_to_level(24) == ConfidenceLevel.STATIC_GUARD
    assert score_to_level(25) == ConfidenceLevel.LEARNING
    assert score_to_level(49) == ConfidenceLevel.LEARNING
    assert score_to_level(50) == ConfidenceLevel.ASSISTED
    assert score_to_level(74) == ConfidenceLevel.ASSISTED
    assert score_to_level(75) == ConfidenceLevel.AUTONOMOUS
    assert score_to_level(100) == ConfidenceLevel.AUTONOMOUS


@pytest.mark.asyncio
async def test_add_confidence_clamps_to_100(redis):
    await add_confidence(redis, tenant_id="t1", host="h1", delta=90)
    await add_confidence(redis, tenant_id="t1", host="h1", delta=90)
    score = await get_confidence_score(redis, tenant_id="t1", host="h1")
    assert score == 100


@pytest.mark.asyncio
async def test_add_confidence_floor_at_zero(redis):
    score = await add_confidence(redis, tenant_id="t1", host="h1", delta=-50)
    assert score == 0


@pytest.mark.asyncio
async def test_decay_confidence(redis):
    await add_confidence(redis, tenant_id="t1", host="h1", delta=30)
    after = await decay_confidence(redis, tenant_id="t1", host="h1", decay=5)
    assert after == 25


@pytest.mark.asyncio
async def test_add_confidence_notify_on_level_change(redis):
    called: list[tuple] = []

    async def notify(old_level, new_level, tid, host):
        called.append((old_level, new_level))

    # Start at 20 (STATIC_GUARD), bump to 30 (LEARNING)
    await add_confidence(redis, tenant_id="t2", host="h2", delta=20)
    await add_confidence(redis, tenant_id="t2", host="h2", delta=10, notify_fn=notify)
    assert len(called) == 1
    assert called[0] == (ConfidenceLevel.STATIC_GUARD, ConfidenceLevel.LEARNING)


# ---------------------------------------------------------------------------
# Phase 4: Change detection — diff_discovery
# ---------------------------------------------------------------------------

def test_diff_discovery_service_added():
    old = {"services": [{"name": "nginx"}], "network_listeners": []}
    new = {"services": [{"name": "nginx"}, {"name": "mysql"}], "network_listeners": []}
    changes = diff_discovery(old, new)
    types = [c["change_type"] for c in changes]
    assert "SERVICE_ADDED" in types
    names = [c["entity_name"] for c in changes]
    assert "mysql" in names


def test_diff_discovery_service_removed():
    old = {"services": [{"name": "nginx"}, {"name": "redis"}], "network_listeners": []}
    new = {"services": [{"name": "nginx"}], "network_listeners": []}
    changes = diff_discovery(old, new)
    types = [c["change_type"] for c in changes]
    assert "SERVICE_REMOVED" in types
    names = [c["entity_name"] for c in changes]
    assert "redis" in names


def test_diff_discovery_port_opened():
    old = {"services": [], "network_listeners": [{"proto": "tcp", "port": "80"}]}
    new = {"services": [], "network_listeners": [{"proto": "tcp", "port": "80"}, {"proto": "tcp", "port": "443"}]}
    changes = diff_discovery(old, new)
    types = [c["change_type"] for c in changes]
    assert "PORT_OPENED" in types


def test_diff_discovery_no_changes():
    snap = {"services": [{"name": "nginx"}], "network_listeners": [{"proto": "tcp", "port": "80"}]}
    changes = diff_discovery(snap, snap)
    assert changes == []


# ---------------------------------------------------------------------------
# Phase 8: chaos hardening — suspect (implausibly empty) discovery snapshot
# must not silently corrupt the baseline on a single transient collector blip.
# ---------------------------------------------------------------------------

def test_is_snapshot_suspect_true_when_new_empty_and_old_nonempty():
    old = {"services": [{"name": "nginx"}, {"name": "mysql"}]}
    new = {"services": []}
    assert is_snapshot_suspect(old, new) is True


def test_is_snapshot_suspect_false_for_partial_real_change():
    old = {"services": [{"name": "nginx"}, {"name": "mysql"}]}
    new = {"services": [{"name": "nginx"}]}
    assert is_snapshot_suspect(old, new) is False


def test_is_snapshot_suspect_false_when_both_empty():
    assert is_snapshot_suspect({"services": []}, {"services": []}) is False


# ---------------------------------------------------------------------------
# suspect_confirm_threshold / streak TTL must be env-driven, never hardcoded
# (no code path may bake in a fixed number that ops can't tune per-tenant).
# ---------------------------------------------------------------------------

def test_suspect_confirm_threshold_default_when_unset(monkeypatch):
    from remote_agent.discovery import suspect_confirm_threshold
    monkeypatch.delenv("OMNI_DISCOVERY_SUSPECT_CONFIRM_THRESHOLD", raising=False)
    assert suspect_confirm_threshold() == 2


def test_suspect_confirm_threshold_reads_env_override(monkeypatch):
    from remote_agent.discovery import suspect_confirm_threshold
    monkeypatch.setenv("OMNI_DISCOVERY_SUSPECT_CONFIRM_THRESHOLD", "5")
    assert suspect_confirm_threshold() == 5


def test_suspect_confirm_threshold_falls_back_on_invalid_or_nonpositive(monkeypatch):
    from remote_agent.discovery import suspect_confirm_threshold
    monkeypatch.setenv("OMNI_DISCOVERY_SUSPECT_CONFIRM_THRESHOLD", "not-a-number")
    assert suspect_confirm_threshold() == 2
    monkeypatch.setenv("OMNI_DISCOVERY_SUSPECT_CONFIRM_THRESHOLD", "0")
    assert suspect_confirm_threshold() == 2


def test_suspect_streak_ttl_s_reads_env_override(monkeypatch):
    from remote_agent.discovery import _suspect_streak_ttl_s
    monkeypatch.setenv("OMNI_DISCOVERY_SUSPECT_STREAK_TTL_S", "600")
    assert _suspect_streak_ttl_s() == 600


@pytest.mark.asyncio
async def test_discovery_suspect_confirm_threshold_1_accepts_on_first_cycle(redis, monkeypatch):
    """Env override actually changes runtime behavior, not just the getter."""
    from remote_agent.discovery import save_discovery_snapshot
    monkeypatch.setenv("OMNI_DISCOVERY_SUSPECT_CONFIRM_THRESHOLD", "1")
    ctx = _ctx(redis)
    baseline = {"services": [{"name": "nginx"}, {"name": "mysql"}]}
    await save_discovery_snapshot(redis, tenant_id="t1", agent_id="a1", snapshot=baseline)

    await handle_knowledge_evidence(ctx, _discovery_ev("t1", "a1", []))

    reloaded = await load_discovery_snapshot(redis, tenant_id="t1", agent_id="a1")
    assert reloaded == {"services": []}, "threshold=1 must accept a suspect snapshot on the FIRST cycle"
    assert await _change_pending_count(redis, "t1") == 2


def _discovery_ev(tenant_id: str, agent_id: str, services: list[dict[str, str]], hostname: str = "host-1") -> dict:
    return {
        "signal_type": "DISCOVERY",
        "tenant_id": tenant_id,
        "probe": "service_topology",
        "namespace": hostname,
        "extracted_fact": {"agent_id": agent_id, "discovery_data": {"services": services}},
    }


async def _change_pending_count(redis_client, tenant_id: str) -> int:
    keys = await redis_client.keys(f"{_CHANGE_PENDING_PREFIX}{tenant_id}:*")
    return len(keys)


@pytest.mark.asyncio
async def test_discovery_suspect_snapshot_skips_baseline_overwrite_first_cycle(redis):
    from remote_agent.discovery import save_discovery_snapshot
    ctx = _ctx(redis)
    baseline = {"services": [{"name": "nginx"}, {"name": "mysql"}], "network_listeners": []}
    await save_discovery_snapshot(redis, tenant_id="t1", agent_id="a1", snapshot=baseline)

    # 1 chu kỳ collector-blip: systemctl fail -> services=[] toàn bộ
    await handle_knowledge_evidence(ctx, _discovery_ev("t1", "a1", []))

    reloaded = await load_discovery_snapshot(redis, tenant_id="t1", agent_id="a1")
    assert reloaded == baseline, "baseline must NOT be overwritten by a single suspect (empty) cycle"
    assert await _change_pending_count(redis, "t1") == 0, "no spurious SERVICE_REMOVED events on a suspect cycle"


@pytest.mark.asyncio
async def test_discovery_suspect_snapshot_confirmed_after_two_consecutive_cycles(redis):
    from remote_agent.discovery import save_discovery_snapshot
    ctx = _ctx(redis)
    baseline = {"services": [{"name": "nginx"}, {"name": "mysql"}], "network_listeners": []}
    await save_discovery_snapshot(redis, tenant_id="t1", agent_id="a1", snapshot=baseline)

    await handle_knowledge_evidence(ctx, _discovery_ev("t1", "a1", []))  # cycle 1: suspect, skipped
    await handle_knowledge_evidence(ctx, _discovery_ev("t1", "a1", []))  # cycle 2: confirmed

    reloaded = await load_discovery_snapshot(redis, tenant_id="t1", agent_id="a1")
    assert reloaded == {"services": []}, (
        "2 consecutive suspect cycles must be accepted as a real outage, not skipped forever"
    )
    assert await _change_pending_count(redis, "t1") == 2, "SERVICE_REMOVED for both services once confirmed"


@pytest.mark.asyncio
async def test_discovery_partial_change_not_treated_as_suspect(redis):
    from remote_agent.discovery import save_discovery_snapshot
    ctx = _ctx(redis)
    baseline = {"services": [{"name": "nginx"}, {"name": "mysql"}], "network_listeners": []}
    await save_discovery_snapshot(redis, tenant_id="t1", agent_id="a1", snapshot=baseline)

    # 1 service thật sự rớt (không phải toàn bộ rỗng) -> xử lý ngay, không cần streak
    await handle_knowledge_evidence(ctx, _discovery_ev("t1", "a1", [{"name": "nginx"}]))

    reloaded = await load_discovery_snapshot(redis, tenant_id="t1", agent_id="a1")
    assert reloaded == {"services": [{"name": "nginx"}]}
    assert await _change_pending_count(redis, "t1") == 1


# ---------------------------------------------------------------------------
# Phase 8 (#2, #3): a genuine infra failure (Redis read, Kafka forward) must
# propagate to the caller's existing retry+poison-ack instead of being
# silently swallowed into "nothing happened this cycle".
# ---------------------------------------------------------------------------

class _RaisingRedisGet:
    """Simulates a real Redis read failure -- distinct from FakeRedis's
    legitimate 'key missing' None."""

    async def get(self, key):
        raise ConnectionError("redis blip (simulated)")


class _RaisingKafka:
    async def send_dict(self, *args, **kwargs):
        raise ConnectionError("kafka blip (simulated)")


@pytest.mark.asyncio
async def test_load_discovery_snapshot_propagates_real_read_failure():
    with pytest.raises(ConnectionError):
        await load_discovery_snapshot(_RaisingRedisGet(), tenant_id="t1", agent_id="a1")


@pytest.mark.asyncio
async def test_discovery_redis_read_failure_propagates_not_swallowed():
    ctx = _ctx(_RaisingRedisGet())
    with pytest.raises(ConnectionError):
        await handle_knowledge_evidence(ctx, _discovery_ev("t1", "a1", [{"name": "nginx"}]))


@pytest.mark.asyncio
async def test_discovery_kafka_forward_failure_propagates_not_swallowed(redis):
    from remote_agent.discovery import save_discovery_snapshot
    ctx = _ctx(redis)
    ctx.kafka = _RaisingKafka()
    baseline = {"services": [{"name": "nginx"}]}
    await save_discovery_snapshot(redis, tenant_id="t1", agent_id="a1", snapshot=baseline)

    with pytest.raises(ConnectionError):
        await handle_knowledge_evidence(
            ctx, _discovery_ev("t1", "a1", [{"name": "nginx"}, {"name": "mysql"}])
        )

    # Diff+save (and change-detected emit) must already have completed BEFORE
    # the forward step raised -- a retry re-runs handle_knowledge_evidence and
    # sees old==new, so it won't duplicate the ADDED event, only re-attempt
    # the forward.
    reloaded = await load_discovery_snapshot(redis, tenant_id="t1", agent_id="a1")
    assert reloaded == {"services": [{"name": "nginx"}, {"name": "mysql"}]}
    assert await _change_pending_count(redis, "t1") == 1


@pytest.mark.asyncio
async def test_discovery_snapshot_save_and_load(redis):
    from remote_agent.discovery import save_discovery_snapshot, load_discovery_snapshot
    snap = {"services": [{"name": "nginx"}], "network_listeners": []}
    await save_discovery_snapshot(redis, tenant_id="t1", agent_id="a1", snapshot=snap)
    loaded = await load_discovery_snapshot(redis, tenant_id="t1", agent_id="a1")
    assert loaded == snap


@pytest.mark.asyncio
async def test_load_snapshot_returns_none_when_missing(redis):
    from remote_agent.discovery import load_discovery_snapshot
    result = await load_discovery_snapshot(redis, tenant_id="no-tenant", agent_id="no-agent")
    assert result is None


# ---------------------------------------------------------------------------
# Phase 5: Telegram doc-upload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_doc_upload_ignored_without_reply(redis):
    ctx = _ctx(redis)
    update = {
        "update_id": 1,
        "message": {
            "message_id": 100,
            "chat": {"id": 999},
            "document": {"file_id": "abc", "file_name": "test.pdf"},
            # No reply_to_message
        },
    }
    result = await handle_telegram_doc_upload(ctx, update)
    assert result is False


@pytest.mark.asyncio
async def test_doc_upload_ignored_without_pending_q(redis):
    ctx = _ctx(redis)
    update = {
        "update_id": 1,
        "message": {
            "message_id": 100,
            "chat": {"id": 999},
            "document": {"file_id": "abc", "file_name": "test.pdf"},
            "reply_to_message": {"message_id": 77},
        },
    }
    result = await handle_telegram_doc_upload(ctx, update)
    # No pending_q key in Redis → skip
    assert result is False


@pytest.mark.asyncio
async def test_doc_upload_returns_false_for_text_message(redis):
    ctx = _ctx(redis)
    update = {
        "update_id": 1,
        "message": {
            "message_id": 100,
            "chat": {"id": 999},
            "text": "hello",
        },
    }
    result = await handle_telegram_doc_upload(ctx, update)
    assert result is False


@pytest.mark.asyncio
async def test_doc_upload_processes_when_pending_q_exists(redis):
    from services.knowledge.document_store import get_doc

    # Pre-populate pending_q
    chat_id = 888
    bot_msg_id = 42
    q_key = "omni:knowledge:pending_q:t3:abc123"
    q_data = {
        "tenant_id": "t3",
        "agent_id": "agent-xyz",
        "hostname": "myhost",
        "entity_type": "process",
        "entity_name": "unknown_process",
    }
    await redis.set(q_key, json.dumps(q_data), ex=3600)
    await redis.set(f"omni:knowledge:pending_q_by_msgid:{chat_id}:{bot_msg_id}", q_key, ex=3600)

    ctx = _ctx(redis)
    update = {
        "update_id": 1,
        "message": {
            "message_id": 200,
            "chat": {"id": chat_id},
            "document": {
                "file_id": "TG-FILE-ID-001",
                "file_name": "runbook.pdf",
                "mime_type": "application/pdf",
            },
            "caption": "This is the nginx runbook",
            "reply_to_message": {"message_id": bot_msg_id},
        },
    }
    result = await handle_telegram_doc_upload(ctx, update)
    assert result is True

    # pending_q should be cleaned up
    remaining = await redis.get(q_key)
    assert remaining is None

    # Confidence should have increased
    score = await get_confidence_score(redis, tenant_id="t3", host="myhost")
    assert score >= 20


# ---------------------------------------------------------------------------
# Phase 6: INV_KNOWLEDGE_NOT_ALERT nới có kiểm soát — Omni tự phán trên
# METRIC_SAMPLE (thuần số) và nâng thành ANOMALY khi lệch.
# ---------------------------------------------------------------------------

class _CaptureKafka:
    def __init__(self):
        self.sent: list[tuple[str, dict, bytes | None]] = []

    async def send_dict(self, topic, value, key=None):
        self.sent.append((topic, value, key))

    def envelopes(self, topic="omni-diagnostic-evidence"):
        return [json.loads(v["data"]) for t, v, _ in self.sent if t == topic]


def _metric_ev(tenant: str, host: str, **metrics):
    return {
        "signal_type": "METRIC_SAMPLE",
        "tenant_id": tenant,
        "namespace": host,
        "lane": "SYS_RESOURCE",
        "probe": "remote_system_metrics",
        "trace_id": f"ra-{host}",
        "extracted_fact": {"agent_id": f"agent-{host}", "hostname": host, **metrics},
    }


async def _metric_ctx(redis_client, confidence: int):
    ctx = _ctx(redis_client)
    ctx.kafka = _CaptureKafka()
    ctx.settings = SimpleNamespace(kafka_topic_diagnostic_evidence="omni-diagnostic-evidence")
    if confidence:
        await add_confidence(redis_client, tenant_id="t1", host="h1", delta=confidence)
    return ctx


@pytest.mark.asyncio
async def test_metric_sample_normal_emits_no_anomaly(redis):
    """(a) Mẫu bình thường: KHÔNG có message nào vào omni-diagnostic-evidence."""
    ctx = await _metric_ctx(redis, confidence=90)
    from anomaly.remote_host_baseline import update_remote_host_baseline

    for v in (10.0, 10.5, 10.2, 10.4, 10.1):
        await update_remote_host_baseline(
            redis, tenant_id="t1", host="h1", fact={"cpu_percent": v}
        )

    await handle_knowledge_evidence(ctx, _metric_ev("t1", "h1", cpu_percent=10.3))

    assert ctx.kafka.envelopes() == []


@pytest.mark.asyncio
async def test_metric_sample_static_guard_promotes_on_threshold(redis):
    """(b) Confidence thấp + vượt ngưỡng tĩnh ⇒ ANOMALY decided_by=omni_static_guard."""
    ctx = await _metric_ctx(redis, confidence=0)

    await handle_knowledge_evidence(ctx, _metric_ev("t1", "h1", cpu_percent=95.0))

    envs = ctx.kafka.envelopes()
    assert len(envs) == 1
    env = envs[0]
    assert env["signal_type"] == "ANOMALY"
    assert env["decided_by"] == "omni_static_guard"
    assert env["omni_decision"]["metric"] == "cpu_percent"
    assert env["omni_decision"]["confidence_level"] == ConfidenceLevel.STATIC_GUARD.value
    assert env["omni_decision"]["static_threshold"] == 80.0
    assert env["domain"] == "os_host"
    assert env["extracted_fact"]["omni_decision"]["promoted_from"] == "METRIC_SAMPLE"


@pytest.mark.asyncio
async def test_promoted_result_is_failed_so_stage4_diagnosis_loop_runs(redis):
    """`result` PHẢI là đúng chuỗi "FAILED" — ở cả top-level và extracted_fact.

    Đây là một liên kết ngầm giữa ba file: `assess_domain_severity` Priority 1 so
    `extracted_fact.result == "FAILED"` để nâng urgency lên high/critical, và
    `remote_agent_pipeline` Stage 4 chỉ chạy vòng chẩn đoán nhiều lượt khi urgency
    đạt `_NOTIFY_TIERS`. Đổi chuỗi này thành thứ "trung thực hơn" (ví dụ "ANOMALY")
    làm chính cảnh báo Omni vừa tự phán rơi xuống medium: không vòng chẩn đoán,
    không Telegram, chết lặng — và không có lỗi nào bật ra.

    Ai phán và bằng bằng chứng gì thì đọc `omni_decision`, không mã hoá vào `result`.
    """
    from pkg.reasoning.domain_signals import assess_domain_severity

    ctx = await _metric_ctx(redis, confidence=0)
    await handle_knowledge_evidence(ctx, _metric_ev("t1", "h1", cpu_percent=95.0))

    env = ctx.kafka.envelopes()[0]
    assert env["result"] == "FAILED"
    assert env["extracted_fact"]["result"] == "FAILED"

    # Và khẳng định hệ quả thật, không chỉ chuỗi: severity phải đạt tier được thông báo.
    severity = assess_domain_severity(
        env["domain"], env.get("alert_hint", ""), env.get("raw", ""), env["extracted_fact"]
    )
    assert severity in ("critical", "high"), (
        f"severity={severity} — Stage 4 se KHONG chay vong chan doan nhieu luot"
    )

    # Nguồn phán vẫn phải truy được, không bị `result` che mất.
    assert env["omni_decision"]["decided_by"] == "omni_static_guard"


@pytest.mark.asyncio
async def test_metric_sample_autonomous_promotes_on_zscore(redis):
    """(c) Confidence cao + z vượt 3σ (dưới ngưỡng tĩnh) ⇒ decided_by=omni_baseline."""
    ctx = await _metric_ctx(redis, confidence=90)
    from anomaly.remote_host_baseline import update_remote_host_baseline

    # >=8 mẫu lịch sử để qua gate cold-start _MIN_BASELINE (2026-07-31: mẫu đang chấm
    # KHÔNG còn nằm trong baseline nên z phản ánh độ lệch thật, không bị trần √(n-1)).
    for v in [10.0, 10.2, 10.1, 10.3] * 4:
        await update_remote_host_baseline(
            redis, tenant_id="t1", host="h1", fact={"cpu_percent": v}
        )

    # 72%: trên sàn biên độ 60 và << ngưỡng tĩnh 80% nhưng lệch rất xa baseline ~10%.
    await handle_knowledge_evidence(ctx, _metric_ev("t1", "h1", cpu_percent=72.0))

    envs = ctx.kafka.envelopes()
    assert len(envs) == 1
    env = envs[0]
    assert env["decided_by"] == "omni_baseline"
    assert env["omni_decision"]["z_score"] >= 3.0
    assert env["omni_decision"]["confidence_level"] == ConfidenceLevel.AUTONOMOUS.value


@pytest.mark.asyncio
async def test_metric_sample_learning_records_zdev_without_promoting(redis):
    """LEARNING: z lệch chỉ ghi sổ đối chiếu, KHÔNG nâng ANOMALY."""
    ctx = await _metric_ctx(redis, confidence=30)
    from anomaly.remote_host_baseline import update_remote_host_baseline

    # >=8 mẫu lịch sử qua gate cold-start; 72% trên sàn biên độ 60 để z_breach có nghĩa.
    for v in [10.0, 10.2, 10.1, 10.3] * 4:
        await update_remote_host_baseline(
            redis, tenant_id="t1", host="h1", fact={"cpu_percent": v}
        )

    await handle_knowledge_evidence(ctx, _metric_ev("t1", "h1", cpu_percent=72.0))

    assert ctx.kafka.envelopes() == []
    entries = await redis.lrange("omni:knowledge:zdev:t1:h1", 0, -1)
    assert len(entries) == 1
    assert json.loads(entries[0])["metric"] == "cpu_percent"


@pytest.mark.asyncio
async def test_flat_host_small_spike_not_promoted_at_autonomous(redis):
    """Chống báo giả 2026-07-31: host phẳng (σ nhỏ) làm z bung dù CPU chỉ ~5%.

    Sàn biên độ 60% chặn: 5.2% dù z=8 vẫn KHÔNG nâng ANOMALY, kể cả AUTONOMOUS
    (nơi hàng rào tĩnh trước đây bị tắt). Không sự cố tài nguyên nào ở 5% CPU.
    """
    ctx = await _metric_ctx(redis, confidence=90)
    from anomaly.remote_host_baseline import update_remote_host_baseline

    for v in [5.0, 5.05, 4.95, 5.0, 5.02, 4.98, 5.0, 5.01, 4.99, 5.0]:
        await update_remote_host_baseline(
            redis, tenant_id="t1", host="h1", fact={"cpu_percent": v}
        )
    await handle_knowledge_evidence(ctx, _metric_ev("t1", "h1", cpu_percent=5.2))

    assert ctx.kafka.envelopes() == []


@pytest.mark.asyncio
async def test_disk_full_still_alarms_at_autonomous_via_static(redis):
    """Chống mù-đĩa 2026-07-31: đĩa tăng đơn điệu nên z phẳng (~1.7) khi bò lên 99%.

    disk_percent bị loại khỏi z-score; hàng rào tĩnh là cận trên ở MỌI bậc, nên đĩa
    99% ở AUTONOMOUS PHẢI báo qua static (trước đây use_static tắt ở AUTONOMOUS ⇒ mù).
    """
    ctx = await _metric_ctx(redis, confidence=90)
    await handle_knowledge_evidence(ctx, _metric_ev("t1", "h1", disk_percent=99.0))

    envs = ctx.kafka.envelopes()
    assert len(envs) == 1
    assert envs[0]["decided_by"] == "omni_static_guard"


@pytest.mark.asyncio
async def test_metric_sample_promotion_deduped_within_ttl(redis):
    """(d) Hai mẫu lệch liên tiếp trong 600s ⇒ chỉ MỘT ANOMALY."""
    ctx = await _metric_ctx(redis, confidence=0)

    await handle_knowledge_evidence(ctx, _metric_ev("t1", "h1", cpu_percent=95.0))
    await handle_knowledge_evidence(ctx, _metric_ev("t1", "h1", cpu_percent=96.0))

    assert len(ctx.kafka.envelopes()) == 1
    assert await redis.ttl("omni:knowledge:promoted:t1:h1:cpu_percent") > 0


@pytest.mark.asyncio
async def test_metric_promotion_kafka_failure_releases_dedup_and_propagates(redis):
    """Kafka lỗi ⇒ lỗi văng ra VÀ khoá dedup được nhả (không mất cảnh báo 10 phút)."""
    ctx = await _metric_ctx(redis, confidence=0)
    ctx.kafka = _RaisingKafka()

    with pytest.raises(ConnectionError):
        await handle_knowledge_evidence(ctx, _metric_ev("t1", "h1", cpu_percent=95.0))

    assert await redis.get("omni:knowledge:promoted:t1:h1:cpu_percent") is None


@pytest.mark.asyncio
async def test_disk_anomaly_is_promoted_as_storage_not_os_host(redis) -> None:
    """Sự cố ĐĨA phải mang `domain=storage`, dù envelope là `os_host`.

    `remote_system_metrics` gộp CPU/RAM/đĩa vào MỘT envelope mang `domain=os_host`. Lấy
    domain của envelope làm domain sự cố thì đĩa đầy bị gán `os_host` ⇒ Omni gọi bộ chẩn
    đoán os_host (tải, tiến trình, dmesg) thay vì storage (df, du, lsblk, inode) ⇒ điều
    tra sai chỗ rồi kết luận "không thấy gì". Đo được trên VM thật 2026-07-30.
    """
    ctx = await _metric_ctx(redis, confidence=0)
    await handle_knowledge_evidence(ctx, _metric_ev("t1", "h1", disk_percent=97.0))

    envs = ctx.kafka.envelopes()
    assert len(envs) == 1
    env = envs[0]
    assert env["omni_decision"]["metric"] == "disk_percent"
    assert env["domain"] == "storage", (
        f"domain={env['domain']} — su co dia phai la storage, khong phai domain envelope"
    )


@pytest.mark.asyncio
async def test_cpu_anomaly_stays_os_host(redis) -> None:
    """Đối chứng: CPU vẫn là `os_host` — bản sửa không đổi phân loại đúng sẵn có."""
    ctx = await _metric_ctx(redis, confidence=0)
    await handle_knowledge_evidence(ctx, _metric_ev("t1", "h1", cpu_percent=95.0))
    assert ctx.kafka.envelopes()[0]["domain"] == "os_host"


def test_every_baseline_metric_has_a_domain() -> None:
    """Thêm metric vào baseline mà quên xếp lĩnh vực ⇒ nó thừa hưởng domain envelope
    trong im lặng. Fail ở đây để lỗi bật ra lúc thêm, không phải lúc có sự cố thật."""
    from anomaly.remote_host_baseline import REMOTE_METRIC_DOMAIN, REMOTE_METRIC_SPECS
    from pkg.domain.taxonomy import CANONICAL_DOMAINS

    for fact_key, _z, _t in REMOTE_METRIC_SPECS:
        assert fact_key in REMOTE_METRIC_DOMAIN, f"metric {fact_key} chua xep linh vuc"
        assert REMOTE_METRIC_DOMAIN[fact_key] in CANONICAL_DOMAINS
