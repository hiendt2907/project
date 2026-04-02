"""proactive_observer: kill switch + consumer group helper."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from workers.proactive_observer import (
    _quick_verify_output,
    _result_status,
    ensure_consumer_group,
    proactive_kill_switch_engaged,
)


@pytest.mark.asyncio
async def test_proactive_kill_switch_engaged() -> None:
    r = AsyncMock()
    r.get = AsyncMock(return_value=None)
    assert await proactive_kill_switch_engaged(r, "omni:proactive:kill_switch") is False
    r.get = AsyncMock(return_value="0")
    assert await proactive_kill_switch_engaged(r, "k") is False
    r.get = AsyncMock(return_value="1")
    assert await proactive_kill_switch_engaged(r, "k") is True


@pytest.mark.asyncio
async def test_ensure_consumer_group_busygroup_ignored() -> None:
    from redis.exceptions import ResponseError

    r = AsyncMock()

    async def _create(*a, **k):
        raise ResponseError("BUSYGROUP")

    r.xgroup_create = AsyncMock(side_effect=_create)
    await ensure_consumer_group(r, "s", "g")
    r.xgroup_create.assert_awaited()


def test_quick_verify_output_respects_status_tag() -> None:
    assert _quick_verify_output("[STATUS] business_hit\nok", "error,failed") is True
    assert _quick_verify_output("[STATUS] empty_result\nno rows", "error,failed") is False
    assert _quick_verify_output("[STATUS] error\nhttp timeout", "error,failed") is False


def test_result_status_parse() -> None:
    assert _result_status("[STATUS] business_hit\n...") == "business_hit"
    assert _result_status("[STATUS] empty_result\n...") == "empty_result"
    assert _result_status("[STATUS] error\n...") == "error"
