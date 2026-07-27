"""TDD: _resolve_prometheus_time / _prometheus_get_json convert VictoriaMetrics-style
relative literals ("now", "now-1h") sang Unix epoch — vanilla Prometheus lab từ chối
"now-1h" cho /api/v1/query_range (xác nhận thật qua curl, 2026-07-23: 400 bad_data)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workers.sdk_service_tools import _prometheus_get_json, _resolve_prometheus_time


class TestResolvePrometheusTime:
    def test_now_resolves_to_epoch(self):
        assert _resolve_prometheus_time("now", 1000.0) == "1000"

    def test_now_minus_hours_resolves_to_epoch(self):
        assert _resolve_prometheus_time("now-1h", 10_000.0) == "6400"

    def test_now_minus_minutes(self):
        assert _resolve_prometheus_time("now-30m", 10_000.0) == "8200"

    def test_now_minus_days(self):
        assert _resolve_prometheus_time("now-2d", 200_000.0) == "27200"

    def test_already_epoch_passthrough(self):
        assert _resolve_prometheus_time("1700000000", 1000.0) == "1700000000"

    def test_non_string_passthrough(self):
        assert _resolve_prometheus_time(1700000000, 1000.0) == 1700000000

    def test_unrelated_string_passthrough(self):
        assert _resolve_prometheus_time("2026-01-01T00:00:00Z", 1000.0) == "2026-01-01T00:00:00Z"


@pytest.mark.asyncio
class TestPrometheusGetJsonResolvesStartEnd:
    async def test_query_range_start_end_converted_before_request(self):
        ctx = MagicMock()
        ctx.settings.prometheus_url = "http://prom.test"
        captured = {}

        class _FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"status": "success"}

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params):
                captured["params"] = params
                return _FakeResponse()

        with patch("workers.sdk_service_tools.httpx.AsyncClient", return_value=_FakeClient()), \
             patch("workers.sdk_service_tools.time.time", return_value=10_000.0):
            await _prometheus_get_json(
                ctx, "/api/v1/query_range",
                {"query": "up", "start": "now-1h", "end": "now", "step": "30s"},
            )

        assert captured["params"]["start"] == "6400"
        assert captured["params"]["end"] == "10000"
        assert captured["params"]["query"] == "up"
        assert captured["params"]["step"] == "30s"

    async def test_query_endpoint_without_start_end_unaffected(self):
        ctx = MagicMock()
        ctx.settings.prometheus_url = "http://prom.test"
        captured = {}

        class _FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"status": "success"}

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params):
                captured["params"] = params
                return _FakeResponse()

        with patch("workers.sdk_service_tools.httpx.AsyncClient", return_value=_FakeClient()):
            await _prometheus_get_json(ctx, "/api/v1/query", {"query": "up"})

        assert captured["params"] == {"query": "up"}
