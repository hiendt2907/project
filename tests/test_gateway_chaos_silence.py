"""Gateway: OMNI_GATEWAY_SILENCE_CHAOS_LAB drops chaos-lab Prometheus webhooks (no Redis)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_API = _ROOT / "src" / "gateway" / "api.py"
_spec = importlib.util.spec_from_file_location("gateway_api_chaos", _API)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
sys.modules["gateway_api_chaos"] = _mod
_spec.loader.exec_module(_mod)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({}, False),
        ({"receiver": "omni-telegram"}, False),
        ({"receiver": "omni-chaos-validation"}, True),
        (
            {
                "receiver": "other",
                "alerts": [{"labels": {"alertname": "ChaosLabAlert"}}],
            },
            True,
        ),
        ({"alerts": [{"labels": {"alertname": "OmniWorkerDown"}}]}, False),
    ],
)
def test_is_chaos_lab_prometheus_webhook(body: dict, expected: bool) -> None:
    assert _mod._is_chaos_lab_prometheus_webhook(body) is expected


@pytest.mark.asyncio
async def test_prometheus_webhook_drops_when_silence_enabled() -> None:
    body = {
        "status": "firing",
        "receiver": "omni-chaos-validation",
        "alerts": [],
    }
    req = MagicMock()
    req.json = AsyncMock(return_value=body)

    mock_redis = AsyncMock()
    mock_kafka = AsyncMock()
    sem = MagicMock()
    sem._value = 10
    sem.acquire = AsyncMock()

    with (
        patch.object(_mod, "SILENCE_CHAOS_LAB", True),
        patch.object(_mod, "_rate_semaphore", sem),
        patch.object(_mod, "_redis", mock_redis),
        patch.object(_mod, "_kafka", mock_kafka),
    ):
        resp = await _mod.prometheus_webhook(req)

    assert resp.status_code == 200
    assert resp.body
    import json

    assert json.loads(resp.body.decode())["status"] == "dropped"
    mock_kafka.send_and_wait.assert_not_called()
