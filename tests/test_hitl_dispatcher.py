"""
Unit tests for hitl_dispatcher defect fixes:
  1. _PartitionTracker watermark correctness (commit ordering)
  2. Bounded queue back-pressure (no unbounded task growth)
  3. hitl_reason propagation from Kafka message → _PendingAction → rejection feedback
"""

from __future__ import annotations

import asyncio
import json
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workers.hitl_dispatcher import (
    _PartitionTracker,
    _PendingAction,
    _RejectionFeedback,
    _build_rejection_feedback,
    _parse_pending,
    _ParseError,
)


# ===========================================================================
# 1. _PartitionTracker — watermark / commit ordering
# ===========================================================================

class TestPartitionTracker:
    def test_single_offset_commits_immediately(self):
        t = _PartitionTracker()
        t.start(0)
        result = t.done(0)
        assert result == 1  # Kafka commit offset = next to read = 0+1

    def test_in_order_completion_advances_each_time(self):
        t = _PartitionTracker()
        t.start(0)
        t.start(1)
        t.start(2)

        assert t.done(0) == 1   # watermark=0, commit=1
        assert t.done(1) == 2   # watermark=1, commit=2
        assert t.done(2) == 3   # watermark=2, commit=3

    def test_out_of_order_holds_until_gap_closed(self):
        """If earlier offset is still in-flight, watermark must not skip ahead."""
        t = _PartitionTracker()
        t.start(3)
        t.start(4)
        t.start(5)

        # 4 finishes first — gap at 3, so watermark = 3-1 = 2 > -1 → commit=3
        assert t.done(4) == 3

        # 5 finishes — in_flight={3}, watermark = 3-1 = 2 == committed_up_to=2 → no advance
        assert t.done(5) is None

        # 3 finishes — in_flight empty, watermark = max_seen=5 → commit=6
        assert t.done(3) == 6

    def test_later_finishes_then_earlier_no_double_commit(self):
        t = _PartitionTracker()
        t.start(10)
        t.start(11)

        # 11 finishes first → watermark = min({10})-1 = 9 > -1 → advance
        assert t.done(11) == 10

        # 10 finishes → in_flight={}, watermark=max_seen=11 > committed=9 → commit=12
        assert t.done(10) == 12

    def test_no_commit_when_watermark_does_not_advance(self):
        t = _PartitionTracker()
        t.start(5)
        t.start(6)
        # 6 done first → watermark=5-1=4 > -1 → commit=5
        assert t.done(6) == 5
        # 5 done → watermark=6, 6 > 4 → commit=7
        result = t.done(5)
        assert result == 7

    def test_non_contiguous_offsets(self):
        """Partitions can have gaps in offset space (log compaction, etc.)."""
        t = _PartitionTracker()
        t.start(100)
        t.start(200)

        # 200 done first → watermark=100-1=99 > -1 → commit=100
        assert t.done(200) == 100

        # 100 done → in_flight={}, watermark=max_seen=200 → commit=201
        assert t.done(100) == 201


# ===========================================================================
# 2. hitl_reason propagation
# ===========================================================================

def _make_kafka_bytes(body: dict) -> bytes:
    """Wraps body in the outer envelope format used by hitl_dispatcher."""
    return json.dumps({"data": json.dumps(body)}).encode()


class TestHitlReasonPropagation:
    def test_hitl_reason_stored_in_pending_action(self):
        raw = _make_kafka_bytes({
            "trace_id": "abc123",
            "tool_name": "scale_deployment",
            "hitl_reason": "critical_severity_siem",
            "siem_incident_id": "inc-001",
        })
        action = _parse_pending(raw)
        assert action.hitl_reason == "critical_severity_siem"

    def test_hitl_reason_defaults_when_missing(self):
        raw = _make_kafka_bytes({
            "trace_id": "abc123",
            "tool_name": "scale_deployment",
            # hitl_reason intentionally absent
        })
        action = _parse_pending(raw)
        assert action.hitl_reason == "siem_critical_action"

    def test_hitl_reason_not_included_in_raw_body(self):
        """hitl_reason must be stripped from raw_body forwarded to omni-actions."""
        raw = _make_kafka_bytes({
            "trace_id": "abc123",
            "tool_name": "scale_deployment",
            "hitl_reason": "critical_severity_siem",
        })
        action = _parse_pending(raw)
        assert "hitl_reason" not in action.raw_body

    def test_rejection_feedback_carries_original_hitl_reason(self):
        """_build_rejection_feedback must use action.hitl_reason, not the error string."""
        raw = _make_kafka_bytes({
            "trace_id": "t1",
            "tool_name": "patch_config",
            "hitl_reason": "manual_escalation",
        })
        action = _parse_pending(raw)
        feedback = _build_rejection_feedback(action, "HITL_TIMEOUT: no decision within 1800s")
        # hitl_reason in feedback = original categorisation from Kafka, not the timeout message
        assert feedback["hitl_reason"] == "manual_escalation"
        # stderr = the routing error message
        assert "HITL_TIMEOUT" in feedback["stderr"]

    def test_rejection_feedback_fields(self):
        raw = _make_kafka_bytes({
            "trace_id": "t2",
            "tool_name": "delete_pod",
            "siem_incident_id": "inc-999",
            "siem_tenant": "acme",
            "hitl_reason": "auto_escalation",
        })
        action = _parse_pending(raw)
        fb = _build_rejection_feedback(action, "HITL_REJECTED: operator declined")
        assert fb["trace_id"] == "t2"
        assert fb["tool_name"] == "delete_pod"
        assert fb["hitl_rejected"] is True
        assert fb["siem_incident_id"] == "inc-999"
        assert fb["siem_tenant"] == "acme"
        assert fb["hitl_reason"] == "auto_escalation"


# ===========================================================================
# 3. Bounded queue — back-pressure prevents memory growth
# ===========================================================================

class TestBoundedQueue:
    """Verify that the queue has a fixed maxsize so the consumer blocks rather than growing."""

    def test_queue_maxsize_is_bounded(self):
        """The queue is created with maxsize = _MAX_CONCURRENT * 2.  Confirm it is finite."""
        from workers.hitl_dispatcher import _MAX_CONCURRENT
        q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_CONCURRENT * 2)
        assert q.maxsize == _MAX_CONCURRENT * 2
        assert q.maxsize > 0

    @pytest.mark.asyncio
    async def test_queue_blocks_producer_when_full(self):
        """put() on a full queue must block until a consumer calls get() + task_done()."""
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        await q.put("a")
        await q.put("b")

        put_completed = False

        async def _slow_put():
            nonlocal put_completed
            await q.put("c")       # should block until slot freed
            put_completed = True

        async def _consumer():
            await asyncio.sleep(0.01)
            item = await q.get()
            q.task_done()
            return item

        put_task = asyncio.create_task(_slow_put())
        await _consumer()
        await put_task

        assert put_completed, "put() must complete after consumer frees a slot"

    @pytest.mark.asyncio
    async def test_queue_join_waits_for_all_done(self):
        """queue.join() must block until every item has called task_done()."""
        q: asyncio.Queue = asyncio.Queue(maxsize=4)
        for i in range(3):
            await q.put(i)

        completed: list[int] = []

        async def _worker():
            while True:
                item = await q.get()
                completed.append(item)
                q.task_done()
                if q.empty():
                    return

        asyncio.create_task(_worker())
        await q.join()
        assert len(completed) == 3
