"""no_data on Victoria query triggers observability audit (mocked)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workers import sdk_service_tools as sst


@pytest.mark.asyncio
async def test_query_timeseries_no_data_appends_audit_stack() -> None:
    ctx = MagicMock()
    ctx.settings = MagicMock()

    empty_vm = {"status": "success", "data": {"resultType": "matrix", "result": []}}

    with patch.object(sst, "_prometheus_get_json", new_callable=AsyncMock, return_value=empty_vm):
        with patch.object(sst, "resolve_promql_for_args", return_value=("up{}", "")):
            with patch(
                "workers.observability_audit.tool_audit_observability_stack",
                new_callable=AsyncMock,
                return_value="[DATA] audit_observability_stack\nmock_audit_body",
            ) as audit_mock:
                out = await sst._query_timeseries_impl(
                    ctx,
                    {"duration": "1h", "target_type": "host"},
                )

    assert "[DATA] no_data" in out
    assert "[AUDIT_STACK]" in out
    assert "mock_audit_body" in out
    audit_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_query_timeseries_no_data_audit_failure_still_returns_diagnosis() -> None:
    ctx = MagicMock()
    ctx.settings = MagicMock()
    empty_vm = {"status": "success", "data": {"resultType": "matrix", "result": []}}

    with patch.object(sst, "_prometheus_get_json", new_callable=AsyncMock, return_value=empty_vm):
        with patch.object(sst, "resolve_promql_for_args", return_value=("up{}", "")):
            with patch(
                "workers.observability_audit.tool_audit_observability_stack",
                new_callable=AsyncMock,
                side_effect=RuntimeError("k8s down"),
            ):
                out = await sst._query_timeseries_impl(ctx, {"duration": "30m"})

    assert "[DATA] no_data" in out
    assert "[AUDIT_STACK]" not in out
