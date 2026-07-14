"""Sprint NV-SRE IT-7 — Evidence outbox: không mất evidence khi mất mạng.

Metric #6: cắt mạng 10 phút → agent spool evidence ra disk, mạng về → flush
theo thứ tự cũ→mới, không duplicate (chỉ spool khi emit fail TOÀN PHẦN — batch
đã được gateway nhận thì không bao giờ vào outbox).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from remote_agent.outbox import EvidenceOutbox


def _batch(i: int) -> list[dict]:
    return [{"source": "remote_agent", "seq": i, "hostname": "cust-app"}]


class TestSpool:
    def test_spool_writes_batch_and_pending_ordered(self, tmp_path: Path):
        ob = EvidenceOutbox(tmp_path)
        p1 = ob.spool(_batch(1))
        p2 = ob.spool(_batch(2))
        assert p1 is not None and p2 is not None
        pending = ob.pending()
        assert pending == sorted(pending)
        assert len(pending) == 2
        assert json.loads(pending[0].read_text())[0]["seq"] == 1

    def test_spool_best_effort_on_unwritable_root(self, tmp_path: Path):
        root = tmp_path / "ro"
        root.mkdir()
        root.chmod(0o500)
        ob = EvidenceOutbox(root / "outbox")
        assert ob.spool(_batch(1)) is None  # không raise — agent không được crash
        root.chmod(0o700)

    def test_prune_caps_batch_count_dropping_oldest(self, tmp_path: Path):
        ob = EvidenceOutbox(tmp_path, max_batches=3)
        for i in range(5):
            ob.spool(_batch(i))
        pending = ob.pending()
        assert len(pending) == 3
        assert json.loads(pending[0].read_text())[0]["seq"] == 2  # 0,1 bị drop
        health = ob.health()
        assert health["dropped_batches"] == 2
        assert health["pressure"] == 1.0


class TestFlush:
    async def test_flush_drains_in_order_no_duplicate(self, tmp_path: Path):
        ob = EvidenceOutbox(tmp_path)
        for i in range(3):
            ob.spool(_batch(i))
        sent: list[int] = []

        async def emit(evidence):
            sent.append(evidence[0]["seq"])
            return len(evidence)

        result = await ob.flush(emit)
        assert sent == [0, 1, 2]
        assert result["flushed"] == 3
        assert ob.pending() == []

    async def test_flush_stops_on_transport_failure_keeps_rest(self, tmp_path: Path):
        ob = EvidenceOutbox(tmp_path)
        for i in range(3):
            ob.spool(_batch(i))
        calls: list[int] = []

        async def emit(evidence):
            calls.append(evidence[0]["seq"])
            return None if evidence[0]["seq"] == 1 else 1  # batch 1: mạng vẫn down

        result = await ob.flush(emit)
        assert calls == [0, 1]
        assert result["flushed"] == 1
        remaining = ob.pending()
        assert len(remaining) == 2  # batch 1 giữ nguyên chờ lần sau, batch 2 chưa đụng
        assert json.loads(remaining[0].read_text())[0]["seq"] == 1

    async def test_flush_skips_corrupted_file(self, tmp_path: Path):
        ob = EvidenceOutbox(tmp_path)
        ob.spool(_batch(1))
        bad = tmp_path / "0000000000000000-bad.json"
        bad.write_text("{not json")

        async def emit(evidence):
            return len(evidence)

        result = await ob.flush(emit)
        assert result["flushed"] == 1
        assert result["corrupted"] == 1
        assert ob.pending() == []


class TestEmitterFailureSignal:
    async def test_emit_returns_none_on_transport_failure(self, monkeypatch):
        """emit() phải phân biệt fail (None) vs enqueued=0 — outbox chỉ spool khi None."""
        from remote_agent import emitter as em

        async def boom(client, url, payload):
            return None  # _with_retry đã cạn retry

        monkeypatch.setattr(em, "_with_retry", boom)
        e = em.OmniEmitter("http://gw", "k", "a1", "h1")
        assert await e.emit([{"x": 1}]) is None
        assert await e.emit([]) == 0
