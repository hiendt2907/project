"""#27 wiring — quyết định tier_gate=HITL trong ``_handle_execute_mutate`` phải
mở pending thật (không chỉ skip + feedback như trước bản vá).

Trước bản vá: khi ``gate_decision_for_tool`` trả "HITL", code chỉ publish
action_feedback status="skipped" rồi return — không có cách nào cho người vận
hành thấy/duyệt hành động đó. Test này khoá lại: ``open_hitl_pending_for_mutate``
phải được gọi đúng 1 lần với tool/args/risk/tier đúng, và hành vi skip cũ (mutate
không tự chạy) vẫn giữ nguyên — đây là bản vá CỘNG THÊM, không đổi default.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest

from workers.kafka_actions_consumer import _handle_execute_mutate


def _feedback_body(sent_entry: tuple[str, dict]) -> dict:
    """``publish_action_feedback`` gửi ``{"data": json.dumps(body)}`` — giải mã lại."""
    return json.loads(sent_entry[1]["data"])


class _FakeKafka:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_dict(self, topic, body, key=None):
        self.sent.append((topic, body))


def _ctx(redis):
    return SimpleNamespace(
        redis=redis,
        kafka=_FakeKafka(),
        telegram=None,
        admin_repo=None,
        settings=SimpleNamespace(
            omni_shadow_os_mode=False,
            omni_auto_execute_enabled=True,
            omni_autonomy_tier="auto",
            kafka_topic_action_feedback="omni-action-feedback",
            kafka_topic_audit_chain="omni-audit-chain",
            telegram_admin_chat_id=None,
        ),
    )


@pytest.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


async def test_hitl_decision_opens_pending_and_still_skips_execution(redis):
    ctx = _ctx(redis)
    open_pending_mock = AsyncMock()

    with (
        patch("workers.tier_gate.resolve_tier", AsyncMock(return_value="auto")),
        patch("workers.hitl_telegram.open_hitl_pending_for_mutate", open_pending_mock),
    ):
        await _handle_execute_mutate(
            ctx, "trace-hitl-1",
            {
                "tool_name": "k8s_delete_pod",  # HIGH risk → HITL ở mọi tier
                "args": {"namespace": "multi-agent", "pod": "flaky"},
                "tenant_id": "acme",
            },
        )

    # Hành vi skip cũ giữ nguyên: mutate không tự chạy, feedback status=skipped.
    assert len(ctx.kafka.sent) == 1
    topic, _ = ctx.kafka.sent[0]
    assert topic == "omni-action-feedback"
    assert _feedback_body(ctx.kafka.sent[0])["status"] == "skipped"

    # Bản vá cộng thêm: pending thật được mở đúng 1 lần, đúng tham số.
    open_pending_mock.assert_awaited_once()
    _, kwargs = open_pending_mock.call_args
    assert kwargs["trace"] == "trace-hitl-1"
    assert kwargs["tenant_id"] == "acme"
    assert kwargs["tool_name"] == "k8s_delete_pod"
    assert kwargs["risk_class"] == "HIGH"
    assert kwargs["tier"] == "auto"


async def test_suggest_decision_does_not_open_pending(redis):
    """SUGGEST (vd tier=shadow) không phải HITL — không được mở pending, giữ
    nguyên hành vi advisory-only hiện tại."""
    ctx = _ctx(redis)
    ctx.settings.omni_autonomy_tier = "shadow"
    open_pending_mock = AsyncMock()

    with (
        patch("workers.tier_gate.resolve_tier", AsyncMock(return_value="shadow")),
        patch("workers.hitl_telegram.open_hitl_pending_for_mutate", open_pending_mock),
    ):
        await _handle_execute_mutate(
            ctx, "trace-suggest-1",
            {
                "tool_name": "k8s_scale_deployment",
                "args": {"namespace": "multi-agent", "deployment": "x", "replicas": 3},
                "tenant_id": "acme",
            },
        )

    open_pending_mock.assert_not_awaited()


async def test_open_pending_failure_does_not_break_skip_feedback(redis):
    """open_hitl_pending_for_mutate lỗi (vd Redis down) KHÔNG được làm hỏng
    đường skip+feedback fail-safe đã có — best-effort, không phải đường quyết định."""
    ctx = _ctx(redis)

    with (
        patch("workers.tier_gate.resolve_tier", AsyncMock(return_value="auto")),
        patch(
            "workers.hitl_telegram.open_hitl_pending_for_mutate",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        await _handle_execute_mutate(
            ctx, "trace-hitl-2",
            {
                "tool_name": "k8s_delete_pod",
                "args": {"namespace": "multi-agent", "pod": "flaky"},
                "tenant_id": "acme",
            },
        )

    assert len(ctx.kafka.sent) == 1
    assert _feedback_body(ctx.kafka.sent[0])["status"] == "skipped"
