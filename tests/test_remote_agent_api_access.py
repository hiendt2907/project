from __future__ import annotations

import json

from remote_agent.collectors.logs import normalize_api_route, parse_api_access_lines
from remote_agent.collectors.api_contract import parse_api_contract


def test_parse_common_access_log_aggregates_and_redacts_query_and_ids():
    lines = [
        '10.0.0.1 - - [14/Jul/2026:10:00:00 +0000] "GET /api/orders/123?token=secret HTTP/1.1" 200 42',
        '10.0.0.2 - - [14/Jul/2026:10:00:01 +0000] "GET /api/orders/456?token=other HTTP/1.1" 200 42',
        '10.0.0.3 - - [14/Jul/2026:10:00:02 +0000] "POST /api/orders HTTP/1.1" 502 0 upstream=app:8080',
    ]

    records = parse_api_access_lines(lines, "/var/log/nginx/access.log")

    assert records[0]["route"] == "/api/orders/:id"
    assert records[0]["count"] == 2
    assert records[0]["status_class"] == "2xx"
    assert records[1]["upstream"] == "app:8080"
    serialized = json.dumps(records)
    assert "token" not in serialized
    assert "secret" not in serialized


def test_json_access_log_accepts_route_metadata_only():
    records = parse_api_access_lines([
        '{"method":"GET","path":"/v1/users/abc123456789?x=secret","status":204,"upstream":"app"}',
    ], "/var/log/app/access.json")

    assert records == [{
        "method": "GET",
        "route": "/v1/users/:id",
        "status_class": "2xx",
        "upstream": "app",
        "count": 1,
        "source_path": "/var/log/app/access.json",
    }]


def test_route_normalization_bounds_and_preserves_business_shape():
    assert normalize_api_route("/checkout/987/items/abcdef1234567890?session=secret") == "/checkout/:id/items/:id"


def test_openapi_parser_emits_route_metadata_without_description_or_schema_content():
    contract = parse_api_contract(
        '{"openapi":"3.0.0","info":{"title":"Orders"},"paths":{"/orders/{id}":{"get":{"operationId":"getOrder","tags":["orders"],"responses":{"200":{"description":"ok"}}}}}}',
        "/app/openapi.json",
    )
    assert contract is not None
    assert contract["title"] == "Orders"
    assert contract["routes"] == [{
        "method": "GET", "route": "/orders/{id}", "operation_id": "getOrder",
        "tags": ["orders"], "response_statuses": ["200"],
    }]
    assert "description" not in json.dumps(contract)
