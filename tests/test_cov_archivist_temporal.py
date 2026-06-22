"""Coverage tests for workers/archivist.py and prober/temporal_evidence.py."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fakeredis.aioredis import FakeRedis


# ── archivist._writable_postmortem_dir ───────────────────────────────────────

def test_writable_postmortem_dir_uses_env():
    """Line 34: OMNI_POSTMORTEM_DIR is appended first."""
    from workers.archivist import _writable_postmortem_dir

    with patch.dict("os.environ", {"OMNI_POSTMORTEM_DIR": "/tmp/omni_test_pm"}):
        with patch("os.makedirs", return_value=None):
            result = _writable_postmortem_dir()
    assert result == "/tmp/omni_test_pm"


def test_writable_postmortem_dir_oserror_falls_through():
    """Lines 43-45: OSError on first candidate → try next."""
    from workers.archivist import _writable_postmortem_dir

    call_count = [0]
    original_makedirs = os.makedirs

    def fake_makedirs(path, exist_ok=False):
        call_count[0] += 1
        if call_count[0] == 1:
            raise OSError("read-only filesystem")
        return original_makedirs(path, exist_ok=True)

    with patch("os.makedirs", side_effect=fake_makedirs):
        result = _writable_postmortem_dir()
    assert result is not None


def test_writable_postmortem_dir_all_fail_returns_tempdir():
    """Line 45: all makedirs fail → return fallback tempdir path."""
    from workers.archivist import _writable_postmortem_dir

    with patch("os.makedirs", side_effect=OSError("all fail")):
        result = _writable_postmortem_dir()
    assert "omni-postmortems" in result


# ── write_incident_postmortem ─────────────────────────────────────────────────

def test_write_incident_postmortem_success():
    """Lines 94-103: success path writes file, returns path."""
    from workers.archivist import write_incident_postmortem

    with patch("workers.archivist._writable_postmortem_dir", return_value="/tmp"):
        path = write_incident_postmortem(
            "trace-success",
            tool_name="kubectl_rollout_restart",
            arg_keys=["namespace", "deployment"],
            alertname="KubeCPUHigh",
            namespace="prod",
            workload="api",
        )
    assert "trace-success" in path or "/tmp" in path


def test_write_incident_postmortem_file_error():
    """Lines 102-103: file write fails → logs warning, still returns path."""
    from workers.archivist import write_incident_postmortem

    with patch("workers.archivist._writable_postmortem_dir", return_value="/tmp"):
        with patch("builtins.open", side_effect=IOError("disk full")):
            path = write_incident_postmortem(
                "trace-fail",
                tool_name="restart",
                arg_keys=[],
                alertname="OOMKill",
                namespace="prod",
                workload="redis",
            )
    assert path.endswith(".md")


# ── recall_playbook_advisory ──────────────────────────────────────────────────

def _make_ws():
    return SimpleNamespace(embed_model="nomic-embed-text")


@pytest.mark.asyncio
async def test_recall_no_vector_store():
    from workers.archivist import recall_playbook_advisory

    ctx = SimpleNamespace(vector_store=None, llm=AsyncMock(), settings=_make_ws(), redis=None)
    result = await recall_playbook_advisory(ctx, query_text="crash", trace="t")
    assert result is None


@pytest.mark.asyncio
async def test_recall_search_exception():
    """Lines 157-159: similarity_search raises → return None."""
    from workers.archivist import recall_playbook_advisory

    mock_vs = AsyncMock()
    mock_vs.similarity_search = AsyncMock(side_effect=RuntimeError("embed timeout"))
    ctx = SimpleNamespace(vector_store=mock_vs, llm=AsyncMock(), settings=_make_ws(), redis=None)
    result = await recall_playbook_advisory(ctx, query_text="CrashLoop nginx pod", trace="t-001")
    assert result is None


@pytest.mark.asyncio
async def test_recall_no_points():
    from workers.archivist import recall_playbook_advisory

    mock_result = MagicMock()
    mock_result.points = []
    mock_vs = AsyncMock()
    mock_vs.similarity_search = AsyncMock(return_value=mock_result)
    ctx = SimpleNamespace(vector_store=mock_vs, llm=AsyncMock(), settings=_make_ws(), redis=None)
    result = await recall_playbook_advisory(ctx, query_text="CrashLoop nginx pod", trace="t")
    assert result is None


@pytest.mark.asyncio
async def test_recall_negative_set_filters_point():
    """Lines 169-175: smembers loads negative set; 182-183: point filtered."""
    from workers.archivist import recall_playbook_advisory

    r = FakeRedis(decode_responses=True)
    await r.sadd("omni:recall:negative_set", "pid-bad")

    pt_bad = MagicMock()
    pt_bad.id = "pid-bad"
    pt_bad.score = 0.92
    pt_bad.payload = {"tool": "restart", "arg_keys": ["ns"]}

    pt_good = MagicMock()
    pt_good.id = "pid-good"
    pt_good.score = 0.80
    pt_good.payload = {"tool": "describe", "arg_keys": ["pod"]}

    mock_result = MagicMock()
    mock_result.points = [pt_bad, pt_good]
    mock_vs = AsyncMock()
    mock_vs.similarity_search = AsyncMock(return_value=mock_result)
    ctx = SimpleNamespace(vector_store=mock_vs, llm=AsyncMock(), settings=_make_ws(), redis=r)

    result = await recall_playbook_advisory(ctx, query_text="CrashLoop nginx", trace="t-002")
    # pt_bad filtered; pt_good survives with score 0.80 >= threshold 0.70
    assert result is not None
    assert result.top_score >= 0.70


@pytest.mark.asyncio
async def test_recall_smembers_exception_ignored():
    """Lines 174-175: smembers raises → negative_set stays empty, proceeds."""
    from workers.archivist import recall_playbook_advisory

    r = AsyncMock()
    r.smembers = AsyncMock(side_effect=RuntimeError("redis down"))
    r.get = AsyncMock(return_value=None)

    pt = MagicMock()
    pt.id = "pid-1"
    pt.score = 0.80
    pt.payload = {"tool": "restart", "arg_keys": ["ns"]}

    mock_result = MagicMock()
    mock_result.points = [pt]
    mock_vs = AsyncMock()
    mock_vs.similarity_search = AsyncMock(return_value=mock_result)
    ctx = SimpleNamespace(vector_store=mock_vs, llm=AsyncMock(), settings=_make_ws(), redis=r)
    result = await recall_playbook_advisory(ctx, query_text="crash", trace="t-smembers")
    assert result is not None


@pytest.mark.asyncio
async def test_recall_redis_get_exception_ignored():
    """Lines 190-191: redis.get raises → neg_count=0, point not decayed."""
    from workers.archivist import recall_playbook_advisory

    r = AsyncMock()
    r.smembers = AsyncMock(return_value=set())
    r.get = AsyncMock(side_effect=RuntimeError("timeout"))

    pt = MagicMock()
    pt.id = "pid-1"
    pt.score = 0.80
    pt.payload = {"tool": "restart", "arg_keys": ["ns"]}

    mock_result = MagicMock()
    mock_result.points = [pt]
    mock_vs = AsyncMock()
    mock_vs.similarity_search = AsyncMock(return_value=mock_result)
    ctx = SimpleNamespace(vector_store=mock_vs, llm=AsyncMock(), settings=_make_ws(), redis=r)
    result = await recall_playbook_advisory(ctx, query_text="crash", trace="t-get-exc")
    assert result is not None


@pytest.mark.asyncio
async def test_recall_all_points_negative_returns_none():
    """Lines 193-194: all points filtered by negative_set → return None."""
    from workers.archivist import recall_playbook_advisory

    r = FakeRedis(decode_responses=True)
    await r.sadd("omni:recall:negative_set", "pid-1", "pid-2")

    pt1 = MagicMock()
    pt1.id = "pid-1"
    pt1.score = 0.92

    pt2 = MagicMock()
    pt2.id = "pid-2"
    pt2.score = 0.88

    mock_result = MagicMock()
    mock_result.points = [pt1, pt2]
    mock_vs = AsyncMock()
    mock_vs.similarity_search = AsyncMock(return_value=mock_result)
    ctx = SimpleNamespace(vector_store=mock_vs, llm=AsyncMock(), settings=_make_ws(), redis=r)

    result = await recall_playbook_advisory(ctx, query_text="CrashLoop crash", trace="t-003")
    assert result is None


@pytest.mark.asyncio
async def test_recall_decay_applied_and_resort():
    """Lines 186-191: neg_count > 0 → decay; line 198: re-sort after decay."""
    from workers.archivist import recall_playbook_advisory

    r = FakeRedis(decode_responses=True)
    await r.set("omni:recall:negative:pid-weak", "1")  # 1 negative feedback → decay 0.85

    pt_weak = MagicMock()
    pt_weak.id = "pid-weak"
    pt_weak.score = 0.95
    pt_weak.payload = {"tool": "restart", "arg_keys": ["ns"]}

    pt_strong = MagicMock()
    pt_strong.id = "pid-strong"
    pt_strong.score = 0.80
    pt_strong.payload = {"tool": "describe", "arg_keys": ["pod"]}

    mock_result = MagicMock()
    mock_result.points = [pt_weak, pt_strong]
    mock_vs = AsyncMock()
    mock_vs.similarity_search = AsyncMock(return_value=mock_result)
    ctx = SimpleNamespace(vector_store=mock_vs, llm=AsyncMock(), settings=_make_ws(), redis=r)

    result = await recall_playbook_advisory(ctx, query_text="CrashLoop crash", trace="t-004")
    assert result is not None
    # After decay: pt_weak.score ≈ 0.95*0.85 = 0.8075; pt_strong = 0.80; re-sorted pt_weak still top


@pytest.mark.asyncio
async def test_recall_strong_threshold():
    """Covers strong=True path (score >= 0.85)."""
    from workers.archivist import recall_playbook_advisory

    r = FakeRedis(decode_responses=True)

    pt = MagicMock()
    pt.id = "pid-strong"
    pt.score = 0.90
    pt.payload = {"tool": "kubectl_rollout_restart", "arg_keys": ["namespace", "deployment"]}

    mock_result = MagicMock()
    mock_result.points = [pt]
    mock_vs = AsyncMock()
    mock_vs.similarity_search = AsyncMock(return_value=mock_result)
    ctx = SimpleNamespace(vector_store=mock_vs, llm=AsyncMock(), settings=_make_ws(), redis=r)

    result = await recall_playbook_advisory(ctx, query_text="OOMKill redis restart", trace="t-005")
    assert result is not None
    assert result.strong is True


# ── temporal_evidence.py ─────────────────────────────────────────────────────

class TestTemporalMetric:
    def test_rate_of_change_single_value(self):
        from prober.temporal_evidence import TemporalMetric

        m = TemporalMetric("cpu", [(1000.0, 50.0)])
        assert m.rate_of_change() is None

    def test_rate_of_change_same_timestamp(self):
        """Covers t1 <= t0 branch."""
        from prober.temporal_evidence import TemporalMetric

        m = TemporalMetric("cpu", [(1000.0, 50.0), (1000.0, 60.0)])
        assert m.rate_of_change() is None

    def test_rate_of_change_normal(self):
        from prober.temporal_evidence import TemporalMetric

        m = TemporalMetric("cpu", [(0.0, 10.0), (60.0, 20.0)])
        rate = m.rate_of_change()
        assert rate is not None
        assert abs(rate - 10.0) < 0.001  # (20-10) / 1 minute


class TestFetchFromPrometheus:
    @pytest.mark.asyncio
    async def test_http_error_returns_none(self):
        """Lines 194-200: httpx.HTTPError → return None."""
        import httpx
        from prober.temporal_evidence import TemporalEvidenceBlock

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection failed"))

            result = await TemporalEvidenceBlock.fetch_from_prometheus(
                "http://prometheus:9090", "up", "test_metric"
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_generic_exception_returns_none(self):
        """Lines 201-207: generic Exception → return None."""
        from prober.temporal_evidence import TemporalEvidenceBlock

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=Exception("unexpected error"))

            result = await TemporalEvidenceBlock.fetch_from_prometheus(
                "http://prometheus:9090", "up", "test_metric"
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_non_success_status_returns_none(self):
        """Lines 138-144: status != 'success' → return None."""
        from prober.temporal_evidence import TemporalEvidenceBlock

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"status": "error", "data": {}})

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)

            result = await TemporalEvidenceBlock.fetch_from_prometheus(
                "http://prometheus:9090", "up", "test_metric"
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_result_returns_none(self):
        """Lines 146-152: empty result list → return None."""
        from prober.temporal_evidence import TemporalEvidenceBlock

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"status": "success", "data": {"result": []}})

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)

            result = await TemporalEvidenceBlock.fetch_from_prometheus(
                "http://prometheus:9090", "up", "test_metric"
            )
        assert result is None
