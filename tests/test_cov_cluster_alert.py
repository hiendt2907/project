"""Coverage tests for:
  - pkg/clustering/incident_cluster.py
  - workers/alert_to_event.py (_stringify_labels + build_anomaly_event_from_evidence_batch)
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fakeredis.aioredis import FakeRedis


# ── incident_cluster.py ────────────────────────────────────────────────────────

class TestIncidentCluster:
    @pytest.mark.asyncio
    async def test_assign_fallback_no_vector_store(self):
        from pkg.clustering.incident_cluster import assign_to_cluster
        ctx = SimpleNamespace(vector_store=None, llm=None, redis=None, settings=None)
        result = await assign_to_cluster(ctx, alert_fp="fp-abc", error_hint="OOM", namespace="ns")
        assert "cls-nollm-" in result

    @pytest.mark.asyncio
    async def test_assign_fallback_on_exception(self):
        from pkg.clustering.incident_cluster import assign_to_cluster
        ctx = SimpleNamespace(vector_store="bad", llm=AsyncMock(), redis=None, settings=None)
        result = await assign_to_cluster(ctx, alert_fp="fp-xyz", error_hint="err", namespace="ns")
        assert "fallback" in result or "nollm" in result or "cls-" in result

    @pytest.mark.asyncio
    async def test_assign_creates_new_cluster(self):
        from pkg.clustering.incident_cluster import assign_to_cluster

        mock_embed = {"embedding": [0.1] * 768}
        mock_llm = AsyncMock()
        mock_llm.embed = AsyncMock(return_value=mock_embed)

        empty_result = MagicMock()
        empty_result.points = []

        mock_vs = AsyncMock()
        mock_vs.similarity_search_raw = AsyncMock(return_value=empty_result)
        mock_vs.upsert = AsyncMock()

        r = FakeRedis(decode_responses=True)
        ws = SimpleNamespace(embed_model="nomic-embed-text")
        ctx = SimpleNamespace(vector_store=mock_vs, llm=mock_llm, redis=r, settings=ws)

        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "pkg.clustering.incident_cluster._update_centroid", new=AsyncMock()
        ):
            result = await assign_to_cluster(ctx, alert_fp="fp-new", error_hint="CrashLoop nginx",
                                              namespace="prod")
        assert result.startswith("cls-")

    @pytest.mark.asyncio
    async def test_assign_joins_existing_cluster(self):
        from pkg.clustering.incident_cluster import assign_to_cluster

        mock_embed = {"embedding": [0.5] * 768}
        mock_llm = AsyncMock()
        mock_llm.embed = AsyncMock(return_value=mock_embed)

        mock_point = MagicMock()
        mock_point.payload = {"cluster_id": "cls-existing-abc"}
        mock_point.id = "cls-existing-abc"
        mock_point.score = 0.95

        hit_result = MagicMock()
        hit_result.points = [mock_point]

        mock_vs = AsyncMock()
        mock_vs.similarity_search_raw = AsyncMock(return_value=hit_result)
        mock_vs.upsert = AsyncMock()

        r = FakeRedis(decode_responses=True)
        ws = SimpleNamespace(embed_model="nomic-embed-text")
        ctx = SimpleNamespace(vector_store=mock_vs, llm=mock_llm, redis=r, settings=ws)

        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "pkg.clustering.incident_cluster._update_centroid", new=AsyncMock()
        ):
            result = await assign_to_cluster(ctx, alert_fp="fp-repeat", error_hint="same err",
                                              namespace="prod")
        assert result == "cls-existing-abc"

    @pytest.mark.asyncio
    async def test_assign_embedding_wrong_dim_padded(self):
        from pkg.clustering.incident_cluster import assign_to_cluster

        # Return short embedding — should be padded
        mock_llm = AsyncMock()
        mock_llm.embed = AsyncMock(return_value={"embedding": [0.1, 0.2, 0.3]})

        empty_result = MagicMock()
        empty_result.points = []

        mock_vs = AsyncMock()
        mock_vs.similarity_search_raw = AsyncMock(return_value=empty_result)
        mock_vs.upsert = AsyncMock()

        ws = SimpleNamespace(embed_model="nomic-embed-text")
        ctx = SimpleNamespace(vector_store=mock_vs, llm=mock_llm, redis=None, settings=ws)

        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "pkg.clustering.incident_cluster._update_centroid", new=AsyncMock()
        ):
            result = await assign_to_cluster(ctx, alert_fp="fp-short-emb", error_hint="err",
                                              namespace="ns")
        assert result.startswith("cls-")

    @pytest.mark.asyncio
    async def test_assign_uses_embeddings_field(self):
        from pkg.clustering.incident_cluster import assign_to_cluster

        # Some models return 'embeddings' instead of 'embedding'
        mock_llm = AsyncMock()
        mock_llm.embed = AsyncMock(return_value={"embeddings": [[0.1] * 768]})

        empty_result = MagicMock()
        empty_result.points = []

        mock_vs = AsyncMock()
        mock_vs.similarity_search_raw = AsyncMock(return_value=empty_result)
        mock_vs.upsert = AsyncMock()

        ws = SimpleNamespace(embed_model="nomic-embed-text")
        ctx = SimpleNamespace(vector_store=mock_vs, llm=mock_llm, redis=None, settings=ws)

        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "pkg.clustering.incident_cluster._update_centroid", new=AsyncMock()
        ):
            result = await assign_to_cluster(ctx, alert_fp="fp-embeddings", error_hint="e",
                                              namespace="ns")
        assert result.startswith("cls-")

    @pytest.mark.asyncio
    async def test_update_centroid_no_existing(self):
        from pkg.clustering.incident_cluster import _update_centroid

        empty_result = MagicMock()
        empty_result.points = []

        mock_vs = AsyncMock()
        mock_vs.similarity_search_raw = AsyncMock(return_value=empty_result)

        await _update_centroid(mock_vs, None, "cls-abc", [0.1] * 768)  # no error

    @pytest.mark.asyncio
    async def test_update_centroid_with_existing_vec(self):
        from pkg.clustering.incident_cluster import _update_centroid

        existing_point = MagicMock()
        existing_point.vector = [0.2] * 768
        existing_point.payload = {"cluster_id": "cls-abc"}

        hit_result = MagicMock()
        hit_result.points = [existing_point]

        mock_vs = AsyncMock()
        mock_vs.similarity_search_raw = AsyncMock(return_value=hit_result)
        mock_vs.upsert = AsyncMock()

        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "rag.pgvector_store.PointStruct", MagicMock()
        ):
            await _update_centroid(mock_vs, None, "cls-abc", [0.5] * 768)
        mock_vs.upsert.assert_awaited()

    @pytest.mark.asyncio
    async def test_update_centroid_none_vs(self):
        from pkg.clustering.incident_cluster import _update_centroid
        await _update_centroid(None, None, "cls", [0.1])  # returns early, no error


# ── alert_to_event.py ─────────────────────────────────────────────────────────

class TestStringifyLabels:
    def test_non_dict_input(self):
        from workers.alert_to_event import _stringify_labels
        assert _stringify_labels(None) == {}
        assert _stringify_labels("string") == {}

    def test_skips_none_values(self):
        from workers.alert_to_event import _stringify_labels
        result = _stringify_labels({"key": None, "valid": "v"})
        assert "key" not in result
        assert result["valid"] == "v"

    def test_skips_empty_keys(self):
        from workers.alert_to_event import _stringify_labels
        result = _stringify_labels({"": "val", "  ": "val2", "good": "g"})
        assert "" not in result
        assert "good" in result

    def test_skips_dict_and_list_values(self):
        from workers.alert_to_event import _stringify_labels
        result = _stringify_labels({"nested": {"k": "v"}, "arr": [1, 2], "ok": "yes"})
        assert "nested" not in result
        assert "arr" not in result
        assert result["ok"] == "yes"


class TestAnomalyEventDictFromEvidenceBatch:
    def test_empty_batch(self):
        from workers.alert_to_event import anomaly_event_dict_from_evidence_batch
        result = anomaly_event_dict_from_evidence_batch([], trace="t-001")
        assert result["trace_id"] == "t-001"
        assert result["namespace"] == ""

    def test_empty_trace_id(self):
        from workers.alert_to_event import anomaly_event_dict_from_evidence_batch
        result = anomaly_event_dict_from_evidence_batch([], trace="")
        assert result["trace_id"] == "evidence-unknown"

    def test_with_evidence_batch(self):
        from workers.alert_to_event import anomaly_event_dict_from_evidence_batch
        batch = [{
            "alert_hint": "CrashLoop in nginx",
            "alert_rule": "KubePodCrashLooping",
            "namespace": "prod",
            "extracted_fact": {"ns": "prod"},
            "canonical_query_snippet": "",
        }]
        result = anomaly_event_dict_from_evidence_batch(batch, trace="t-abc")
        assert result["trace_id"] == "t-abc"
        assert result["rule_name"] == "KubePodCrashLooping"

    def test_with_canonical_query_json(self):
        from workers.alert_to_event import anomaly_event_dict_from_evidence_batch
        cq = json.dumps({"labels": {"alertname": "OOMKill", "namespace": "prod"},
                         "annotations": {"summary": "OOM occurred"}})
        batch = [{
            "alert_hint": "OOM kill detected",
            "alert_rule": "KubeOOMKill",
            "namespace": "prod",
            "extracted_fact": {},
            "canonical_query_snippet": cq,
        }]
        result = anomaly_event_dict_from_evidence_batch(batch, trace="t-oom")
        assert result["trace_id"] == "t-oom"
