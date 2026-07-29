"""Backfill `omni:rag:sop:{tenant}` hash → HNSW COLLECTION_SOP.

Gap đóng ở đây: `advisory_ingest` chỉ ghi Redis hash phẳng nên 1019+ advisory
KHÔNG bao giờ được vector search thấy (`itops_sop_ledger` num_docs=0). Backfill
ingest vào HNSW với ``auto_execute=False`` — an toàn với fast path
(`resolve_remediation_from_memory` trả miss ở nhánh ``sop_no_auto``), chỉ phục vụ
`redis_brain` làm context read-only.
"""

from __future__ import annotations

import json

import pytest

from rag.sop_ledger import SOP_COLLECTION
from training.advisory_sop_vector import (
    advisory_match_text,
    advisory_sop_payload,
    backfill_sop_vectors,
)

SAMPLE = {
    "alert_id": "sop-OS-NET-0005",
    "lane": "SYS_HARD_FAIL",
    "alert_context": {
        "alertname": "TCPTimeWaitAccumulation",
        "namespace": "multi-agent",
        "severity": "critical",
        "annotations": {"summary": "TCP TIME_WAIT connections exceeding 50000"},
    },
    "evidence": ["tcp_connections probe: time_wait_count=62400"],
    "root_cause": "Short-lived HTTP connections not reusing TCP sessions.",
    "proposed_remediation": [
        {"step": "sysctl -w net.ipv4.tcp_tw_reuse=1", "approval_required": False}
    ],
}


class _FakeHashRedis:
    """Chỉ phục vụ HGETALL — đủ cho backfill."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    async def hgetall(self, key: str) -> dict[str, str]:  # noqa: ARG002
        return dict(self._mapping)

    async def aclose(self) -> None:
        return None


class _CaptureStore:
    def __init__(self) -> None:
        self.upserts: list[tuple[str, list, str]] = []
        self.ready = False

    async def ensure_ready(self) -> None:
        self.ready = True

    async def upsert(self, collection_name, points, *, tenant_id="default", **_kw) -> None:
        self.upserts.append((collection_name, list(points), tenant_id))

    async def close(self) -> None:
        return None


def test_match_text_carries_alertname_and_root_cause():
    text = advisory_match_text(SAMPLE)

    assert "TCPTimeWaitAccumulation" in text
    assert "Short-lived HTTP connections" in text
    assert "SYS_HARD_FAIL" in text


def test_payload_never_auto_executes():
    payload = advisory_sop_payload(SAMPLE)

    assert payload["auto_execute"] is False
    assert payload.get("tool") in (None, "")


def test_payload_exposes_root_cause_for_brain_summary():
    payload = advisory_sop_payload(SAMPLE)

    assert payload["root_cause"] == SAMPLE["root_cause"]
    assert payload["sop_id"] == "sop-OS-NET-0005"


@pytest.mark.asyncio
async def test_backfill_upserts_into_sop_collection():
    redis = _FakeHashRedis({"sop-OS-NET-0005": json.dumps(SAMPLE)})
    store = _CaptureStore()

    async def embed_fn(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 768 for _ in texts]

    n = await backfill_sop_vectors(
        redis=redis, store=store, embed_fn=embed_fn, tenant_id="default"
    )

    assert n == 1
    assert store.ready is True
    collection, points, tenant = store.upserts[0]
    assert collection == SOP_COLLECTION
    assert tenant == "default"
    assert points[0].payload["auto_execute"] is False


@pytest.mark.asyncio
async def test_backfill_skips_malformed_json_without_failing():
    redis = _FakeHashRedis(
        {"good": json.dumps(SAMPLE), "bad": "{not-json", "empty": json.dumps({})}
    )
    store = _CaptureStore()

    async def embed_fn(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 768 for _ in texts]

    n = await backfill_sop_vectors(
        redis=redis, store=store, embed_fn=embed_fn, tenant_id="default"
    )

    assert n == 1


@pytest.mark.asyncio
async def test_backfill_dry_run_does_not_upsert():
    redis = _FakeHashRedis({"sop-OS-NET-0005": json.dumps(SAMPLE)})
    store = _CaptureStore()

    async def embed_fn(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 768 for _ in texts]

    n = await backfill_sop_vectors(
        redis=redis, store=store, embed_fn=embed_fn, tenant_id="default", dry_run=True
    )

    assert n == 1
    assert store.upserts == []


@pytest.mark.asyncio
async def test_backfill_empty_hash_returns_zero():
    store = _CaptureStore()

    async def embed_fn(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 768 for _ in texts]

    n = await backfill_sop_vectors(
        redis=_FakeHashRedis({}), store=store, embed_fn=embed_fn, tenant_id="default"
    )

    assert n == 0
    assert store.upserts == []
