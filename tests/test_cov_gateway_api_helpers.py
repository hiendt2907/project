"""Coverage for src/gateway/api.py helpers and env parsing — no mocks."""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from starlette.requests import Request


def _http_scope(
    *,
    method: str = "GET",
    path: str = "/",
    query_string: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"spec_version": "2.3", "version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query_string,
        "headers": headers or [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }


@pytest.fixture(autouse=True)
def _restore_kafka_env():
    saved_alert = os.environ.pop("OMNI_KAFKA_TOPIC_ALERTS", None)
    saved_stream = os.environ.pop("OMNI_STREAM_INBOUND", None)
    yield
    if saved_alert is not None:
        os.environ["OMNI_KAFKA_TOPIC_ALERTS"] = saved_alert
    else:
        os.environ.pop("OMNI_KAFKA_TOPIC_ALERTS", None)
    if saved_stream is not None:
        os.environ["OMNI_STREAM_INBOUND"] = saved_stream
    else:
        os.environ.pop("OMNI_STREAM_INBOUND", None)


def test_kafka_topic_from_env_defaults():
    from gateway import api as gw

    os.environ.pop("OMNI_KAFKA_TOPIC_ALERTS", None)
    os.environ.pop("OMNI_STREAM_INBOUND", None)
    assert gw._kafka_topic_from_env() == "omni-alerts"


def test_kafka_topic_from_env_valid_custom():
    from gateway import api as gw

    os.environ["OMNI_KAFKA_TOPIC_ALERTS"] = "alerts.prod_v2"
    assert gw._kafka_topic_from_env() == "alerts.prod_v2"


def test_kafka_topic_from_env_invalid_falls_back():
    from gateway import api as gw

    os.environ["OMNI_KAFKA_TOPIC_ALERTS"] = "bad topic!"
    assert gw._kafka_topic_from_env() == "omni-alerts"


def test_kafka_topic_stream_inbound_fallback():
    from gateway import api as gw

    os.environ.pop("OMNI_KAFKA_TOPIC_ALERTS", None)
    os.environ["OMNI_STREAM_INBOUND"] = "stream-in-01"
    assert gw._kafka_topic_from_env() == "stream-in-01"


def test_str_header_and_query_real_request():
    from gateway import api as gw

    req = Request(
        _http_scope(
            headers=[(b"x-custom", b"  trimmed  ")],
            query_string=b"foo=bar&trace_id=querytrace12",
        )
    )
    assert gw._str_header(req, "x-custom") == "trimmed"
    assert gw._str_query(req, "foo") == "bar"
    assert gw._str_query(req, "missing") is None


def test_pick_valid_client_trace_id_order():
    from gateway import api as gw

    assert gw._pick_valid_client_trace_id("headerid12", "queryid1234") == "headerid12"
    assert gw._pick_valid_client_trace_id(None, "secondid12") == "secondid12"
    assert gw._pick_valid_client_trace_id("short", None) is None
    assert gw._pick_valid_client_trace_id("a" * 129, None) is None


def test_resolve_prometheus_trace_id_generates_when_invalid():
    from gateway import api as gw

    req = Request(_http_scope(query_string=b"trace_id=bad"))
    tid = gw._resolve_prometheus_trace_id(req)
    assert tid.startswith("gw-prom-")


def test_resolve_prometheus_trace_id_honors_header():
    from gateway import api as gw

    req = Request(
        _http_scope(
            headers=[(b"x-omni-trace-id", b"clienttid123456")],
        )
    )
    assert gw._resolve_prometheus_trace_id(req) == "clienttid123456"


def test_is_chaos_lab_prometheus_webhook():
    from gateway import api as gw

    assert gw._is_chaos_lab_prometheus_webhook({"receiver": "omni-chaos-validation"}) is True
    assert gw._is_chaos_lab_prometheus_webhook(
        {"alerts": [{"labels": {"alertname": "ChaosLabAlert"}}]}
    ) is True
    assert gw._is_chaos_lab_prometheus_webhook({"alerts": "not-a-list"}) is False
    assert gw._is_chaos_lab_prometheus_webhook({"alerts": [None, {"labels": None}]}) is False
    assert gw._is_chaos_lab_prometheus_webhook({"receiver": "other"}) is False


def test_verify_hmac_when_lab_no_secret():
    from gateway import api as gw

    req = Request(_http_scope())
    assert gw._verify_webhook_auth(req, b"any-body") is True


def test_linear_forecast_single_point_and_horizon():
    from gateway import api as gw

    pred, meta = gw._linear_forecast([10.0], horizon_steps=4)
    assert len(pred) == 4
    assert "slope" in meta and "intercept" in meta


def test_linear_forecast_perfect_line():
    from gateway import api as gw

    vals = [0.0, 1.0, 2.0, 3.0]
    pred, meta = gw._linear_forecast(vals, horizon_steps=2)
    assert abs(meta["r_squared"] - 1.0) < 1e-9
    assert len(pred) == 2


def test_prometheus_webhook_body_model():
    from gateway import api as gw

    raw = json.dumps(
        {
            "receiver": "r1",
            "status": "firing",
            "alerts": [],
        }
    )
    body = gw.PrometheusWebhookBody.model_validate_json(raw)
    assert body.receiver == "r1"
    assert body.alerts == []
