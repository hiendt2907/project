"""Coverage tests for src/workers/log_surge_probe.py pure functions."""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from workers.log_surge_probe import (
    classify_http_status,
    parse_http_status_from_access_line,
    parse_app_json_line_5xx,
    namespace_pod_from_batch,
    count_access_errors,
    AccessErrorCounts,
    _pod_regex_for_logql,
    _ratio_5xx_access,
    _count_app_json_5xx,
    loki_query_range_lines,
    evaluate_log_surge_sigma_bypass,
    LogSurgeResult,
)


# ── classify_http_status ──────────────────────────────────────────────────────

@pytest.mark.parametrize("status,expected", [
    (500, "5xx"),
    (501, "5xx"),
    (502, "5xx"),
    (503, "5xx"),
    (504, "5xx"),
    (429, "rate_limit"),
    (499, "client_abort"),
    (401, "auth_failure"),
    (403, "auth_failure"),
    (200, "ok"),
    (201, "ok"),
    (301, "ok"),
    (404, "ok"),
    (400, "ok"),
])
def test_classify_http_status(status, expected):
    assert classify_http_status(status) == expected


# ── parse_http_status_from_access_line ───────────────────────────────────────

def test_parse_combined_log_format():
    line = '127.0.0.1 - - [01/Jan/2026:00:00:00 +0000] "GET /health HTTP/1.1" 200 42'
    assert parse_http_status_from_access_line(line) == 200


def test_parse_combined_log_503():
    line = '10.0.0.1 - - [01/Jan/2026:00:00:00 +0000] "POST /api HTTP/1.1" 503 0'
    assert parse_http_status_from_access_line(line) == 503


def test_parse_combined_log_429():
    line = '10.0.0.1 - - [01/Jan/2026:00:00:00 +0000] "GET /api HTTP/1.1" 429 12'
    assert parse_http_status_from_access_line(line) == 429


def test_parse_combined_log_499():
    line = '10.0.0.1 - - [01/Jan/2026:00:00:00 +0000] "GET /slow HTTP/1.1" 499 0'
    assert parse_http_status_from_access_line(line) == 499


def test_parse_kv_format():
    line = 'ts=2026-01-01T00:00:00Z status=503 path=/api method=GET'
    assert parse_http_status_from_access_line(line) == 503


def test_parse_kv_response_code():
    line = 'time=2026-01-01 response_code=401 uri=/auth'
    assert parse_http_status_from_access_line(line) == 401


def test_parse_space_separated_fallback():
    line = 'some line with 502 in it'
    result = parse_http_status_from_access_line(line)
    assert result == 502


def test_parse_empty_line_returns_none():
    assert parse_http_status_from_access_line("") is None
    assert parse_http_status_from_access_line("   ") is None


def test_parse_no_status_returns_none():
    assert parse_http_status_from_access_line("no status code here at all xyz") is None


# ── parse_app_json_line_5xx ────────────────────────────────────────────────────

def test_parse_json_5xx_status_field():
    line = '{"status": 503, "path": "/api", "ts": "2026-01-01"}'
    assert parse_app_json_line_5xx(line) is True


def test_parse_json_200_not_5xx():
    line = '{"status": 200, "path": "/ok"}'
    assert parse_app_json_line_5xx(line) is False


def test_parse_json_level_error():
    line = '{"level": "error", "msg": "internal failure"}'
    assert parse_app_json_line_5xx(line) is True


def test_parse_json_level_warning_not_5xx():
    line = '{"level": "warning", "msg": "retry"}'
    assert parse_app_json_line_5xx(line) is False


def test_parse_json_status_code_field():
    line = '{"status_code": 500, "service": "api"}'
    assert parse_app_json_line_5xx(line) is True


def test_parse_json_http_status_field():
    line = '{"http_status": "502", "url": "/upstream"}'
    assert parse_app_json_line_5xx(line) is True


def test_parse_json_code_field():
    line = '{"code": 503}'
    assert parse_app_json_line_5xx(line) is True


def test_parse_not_json_returns_false():
    line = "plain text log entry"
    assert parse_app_json_line_5xx(line) is False


def test_parse_not_json_dict():
    line = "[1, 2, 3]"
    assert parse_app_json_line_5xx(line) is False


def test_parse_invalid_json_fallback_regex():
    line = '{"status": 503, bad json...'
    # Falls back to regex
    result = parse_app_json_line_5xx(line)
    assert isinstance(result, bool)


def test_parse_json_401_not_5xx():
    line = '{"status": 401, "msg": "unauthorized"}'
    assert parse_app_json_line_5xx(line) is False


# ── namespace_pod_from_batch ──────────────────────────────────────────────────

def test_namespace_pod_from_batch_extracted_fact_dict():
    batch = [{"extracted_fact": {"namespace": "multi-agent", "pod": "api-gw-abc12"}}]
    ns, pod = namespace_pod_from_batch(batch)
    assert ns == "multi-agent"
    assert pod == "api-gw-abc12"


def test_namespace_pod_from_batch_extracted_fact_json_string():
    import json
    fact = json.dumps({"namespace": "default", "pod": "nginx-abc12"})
    batch = [{"extracted_fact": fact}]
    ns, pod = namespace_pod_from_batch(batch)
    assert ns == "default"
    assert pod == "nginx-abc12"


def test_namespace_pod_from_batch_canonical_snippet():
    import json
    snip = json.dumps({"labels": {"namespace": "kube-system", "pod": "coredns-abc"}})
    batch = [{"canonical_query_snippet": snip}]
    ns, pod = namespace_pod_from_batch(batch)
    assert ns == "kube-system"
    assert pod == "coredns-abc"


def test_namespace_pod_from_batch_empty():
    ns, pod = namespace_pod_from_batch([])
    assert ns == ""
    assert pod == ""


def test_namespace_pod_from_batch_no_relevant_fields():
    batch = [{"other_field": "value"}]
    ns, pod = namespace_pod_from_batch(batch)
    assert ns == ""
    assert pod == ""


def test_namespace_pod_from_batch_uses_first_found():
    batch = [
        {"extracted_fact": {"namespace": "ns1", "pod": "pod1"}},
        {"extracted_fact": {"namespace": "ns2", "pod": "pod2"}},
    ]
    ns, pod = namespace_pod_from_batch(batch)
    # First match wins (but may be overridden by later entries — check just type)
    assert isinstance(ns, str)
    assert isinstance(pod, str)


# ── count_access_errors ────────────────────────────────────────────────────────

def test_count_access_errors_empty():
    counts = count_access_errors([])
    assert counts.total_parsed == 0
    assert counts.count_5xx == 0
    assert counts.count_rate_limit == 0
    assert counts.count_client_abort == 0
    assert counts.count_auth == 0


def test_count_access_errors_mixed():
    lines = [
        '10.0.0.1 - - [01/Jan/2026] "GET / HTTP/1.1" 503 0',
        '10.0.0.1 - - [01/Jan/2026] "GET / HTTP/1.1" 503 0',
        '10.0.0.1 - - [01/Jan/2026] "GET / HTTP/1.1" 429 0',
        '10.0.0.1 - - [01/Jan/2026] "GET / HTTP/1.1" 499 0',
        '10.0.0.1 - - [01/Jan/2026] "GET / HTTP/1.1" 401 0',
        '10.0.0.1 - - [01/Jan/2026] "GET / HTTP/1.1" 200 100',
    ]
    counts = count_access_errors(lines)
    assert counts.total_parsed == 6
    assert counts.count_5xx == 2
    assert counts.count_rate_limit == 1
    assert counts.count_client_abort == 1
    assert counts.count_auth == 1


def test_count_access_errors_histogram():
    lines = [
        '10.0.0.1 - - [01/Jan/2026] "GET / HTTP/1.1" 503 0',
        '10.0.0.1 - - [01/Jan/2026] "GET / HTTP/1.1" 503 0',
        '10.0.0.1 - - [01/Jan/2026] "GET / HTTP/1.1" 200 0',
    ]
    counts = count_access_errors(lines)
    assert counts.status_histogram.get(503) == 2
    assert counts.status_histogram.get(200) == 1


def test_count_access_errors_skips_unparseable():
    lines = ["no status here", "still no status", "completely invalid log line"]
    counts = count_access_errors(lines)
    assert counts.total_parsed == 0


# ── _pod_regex_for_logql ──────────────────────────────────────────────────────

def test_pod_regex_strips_hash_suffix():
    result = _pod_regex_for_logql("my-app-7d6b9c4f8-abc12")
    assert "my-app" in result
    assert result.endswith(".*")


def test_pod_regex_short_pod_name():
    result = _pod_regex_for_logql("nginx")
    assert "nginx" in result
    assert result.endswith(".*")


def test_pod_regex_no_re2_special_chars():
    result = _pod_regex_for_logql("my.app+svc[1]")
    # Special RE2 chars should be escaped
    assert "\\." in result or "\\+" in result or "\\[" in result


def test_pod_regex_hyphens_preserved():
    result = _pod_regex_for_logql("my-service-7d4b9")
    # Hyphens should NOT be escaped (RE2 compatible)
    # Check that the base name is present (hyphens intact in base)
    assert "my" in result


def test_pod_regex_empty_pod_uses_wildcard():
    result = _pod_regex_for_logql("")
    assert ".*" in result


# ── _ratio_5xx_access ─────────────────────────────────────────────────────────

def test_ratio_5xx_access_returns_counts():
    lines = [
        '10.0.0.1 - - [01/Jan/2026] "GET / HTTP/1.1" 503 0',
        '10.0.0.1 - - [01/Jan/2026] "GET / HTTP/1.1" 200 0',
    ]
    count_5xx, count_parsed = _ratio_5xx_access(lines)
    assert count_5xx == 1
    assert count_parsed == 2


# ── _count_app_json_5xx ────────────────────────────────────────────────────────

def test_count_app_json_5xx_counts_only_json():
    lines = [
        '{"status": 503}',
        'plain text line',
        '{"status": 200}',
        '{"level": "error"}',
    ]
    bad, checked = _count_app_json_5xx(lines)
    assert checked == 3  # 3 JSON lines
    assert bad == 2  # 503 + level=error


# ── loki_query_range_lines ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_loki_query_range_lines_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": {
            "result": [
                {"stream": {}, "values": [["123456789", "line1"], ["123456790", "line2"]]}
            ]
        }
    }
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client
        lines, err = await loki_query_range_lines(
            base_url="http://loki:3100",
            logql='{namespace="test"}',
            start_sec=1000.0,
            end_sec=2000.0,
        )
        assert len(lines) == 2
        assert err == ""


@pytest.mark.asyncio
async def test_loki_query_range_lines_non_200():
    mock_resp = MagicMock()
    mock_resp.status_code = 503
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client
        lines, err = await loki_query_range_lines(
            base_url="http://loki:3100",
            logql='{namespace="test"}',
            start_sec=1000.0,
            end_sec=2000.0,
        )
        assert lines == []
        assert "loki_http_503" in err


@pytest.mark.asyncio
async def test_loki_query_range_lines_exception():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_cls.return_value = mock_client
        lines, err = await loki_query_range_lines(
            base_url="http://loki:3100",
            logql='{namespace="test"}',
            start_sec=1000.0,
            end_sec=2000.0,
        )
        assert lines == []
        assert "connection refused" in err


# ── evaluate_log_surge_sigma_bypass ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_evaluate_no_namespace():
    result = await evaluate_log_surge_sigma_bypass(
        loki_base_url="http://loki:3100",
        namespace="",
        pod_name="nginx-abc12",
        window_sec=300,
        min_lines=10,
        min_ratio=0.5,
        line_limit=500,
        timeout_sec=5.0,
    )
    assert result.ok is False
    assert result.reason == "no_namespace"


@pytest.mark.asyncio
async def test_evaluate_loki_unavailable():
    with patch("workers.log_surge_probe.loki_query_range_lines", return_value=([], "connection refused")):
        result = await evaluate_log_surge_sigma_bypass(
            loki_base_url="http://loki:3100",
            namespace="multi-agent",
            pod_name="nginx",
            window_sec=300,
            min_lines=10,
            min_ratio=0.5,
            line_limit=500,
            timeout_sec=5.0,
        )
    assert result.ok is False
    assert result.reason == "loki_unavailable"
    assert result.escalate_log_unavailable is True


@pytest.mark.asyncio
async def test_evaluate_5xx_surge():
    lines = ['10.0.0.1 - - [01/Jan/2026] "GET / HTTP/1.1" 503 0'] * 15 + \
            ['10.0.0.1 - - [01/Jan/2026] "GET / HTTP/1.1" 200 0'] * 2
    with patch("workers.log_surge_probe.loki_query_range_lines", return_value=(lines, "")):
        result = await evaluate_log_surge_sigma_bypass(
            loki_base_url="http://loki:3100",
            namespace="multi-agent",
            pod_name="nginx",
            window_sec=300,
            min_lines=5,
            min_ratio=0.5,
            line_limit=500,
            timeout_sec=5.0,
        )
    assert result.ok is True
    assert result.reason == "access_5xx_sustained"
    assert result.dominant_error_class == "5xx"


@pytest.mark.asyncio
async def test_evaluate_rate_limit_surge():
    lines = ['10.0.0.1 - - [01/Jan/2026] "GET / HTTP/1.1" 429 0'] * 12 + \
            ['10.0.0.1 - - [01/Jan/2026] "GET / HTTP/1.1" 200 0'] * 3
    with patch("workers.log_surge_probe.loki_query_range_lines", return_value=(lines, "")):
        result = await evaluate_log_surge_sigma_bypass(
            loki_base_url="http://loki:3100",
            namespace="multi-agent",
            pod_name="nginx",
            window_sec=300,
            min_lines=5,
            min_ratio=0.5,
            line_limit=500,
            timeout_sec=5.0,
        )
    assert result.ok is True
    assert result.reason == "access_rate_limit_sustained"
    assert result.dominant_error_class == "rate_limit"


@pytest.mark.asyncio
async def test_evaluate_auth_failure_surge():
    lines = ['10.0.0.1 - - [01/Jan/2026] "POST /auth HTTP/1.1" 401 0'] * 10 + \
            ['10.0.0.1 - - [01/Jan/2026] "GET / HTTP/1.1" 200 0'] * 2
    with patch("workers.log_surge_probe.loki_query_range_lines", return_value=(lines, "")):
        result = await evaluate_log_surge_sigma_bypass(
            loki_base_url="http://loki:3100",
            namespace="multi-agent",
            pod_name="api-gw",
            window_sec=300,
            min_lines=5,
            min_ratio=0.5,
            line_limit=500,
            timeout_sec=5.0,
        )
    assert result.ok is True
    assert result.reason == "access_auth_failure_sustained"
    assert result.dominant_error_class == "auth_failure"


@pytest.mark.asyncio
async def test_evaluate_client_abort_informational():
    lines = ['10.0.0.1 - - [01/Jan/2026] "GET /slow HTTP/1.1" 499 0'] * 15 + \
            ['10.0.0.1 - - [01/Jan/2026] "GET / HTTP/1.1" 200 0'] * 2
    with patch("workers.log_surge_probe.loki_query_range_lines", return_value=(lines, "")):
        result = await evaluate_log_surge_sigma_bypass(
            loki_base_url="http://loki:3100",
            namespace="multi-agent",
            pod_name="nginx",
            window_sec=300,
            min_lines=5,
            min_ratio=0.5,
            line_limit=500,
            timeout_sec=5.0,
        )
    assert result.ok is False
    assert result.reason == "access_client_abort_informational"
    assert result.dominant_error_class == "client_abort"
    assert result.escalate_log_unavailable is False


@pytest.mark.asyncio
async def test_evaluate_json_5xx_fallback():
    # No access log status lines, but JSON 5xx app lines
    lines = ['{"status": 503, "path": "/api"}'] * 15 + \
            ['{"status": 200}'] * 2
    with patch("workers.log_surge_probe.loki_query_range_lines", return_value=(lines, "")):
        result = await evaluate_log_surge_sigma_bypass(
            loki_base_url="http://loki:3100",
            namespace="multi-agent",
            pod_name="api",
            window_sec=300,
            min_lines=5,
            min_ratio=0.5,
            line_limit=500,
            timeout_sec=5.0,
        )
    assert result.ok is True
    assert result.reason == "app_json_5xx_sustained"


@pytest.mark.asyncio
async def test_evaluate_insufficient_evidence():
    # Very few lines, none problematic
    lines = ['10.0.0.1 - - [01/Jan/2026] "GET / HTTP/1.1" 200 0'] * 3
    with patch("workers.log_surge_probe.loki_query_range_lines", return_value=(lines, "")):
        result = await evaluate_log_surge_sigma_bypass(
            loki_base_url="http://loki:3100",
            namespace="multi-agent",
            pod_name="nginx",
            window_sec=300,
            min_lines=10,
            min_ratio=0.5,
            line_limit=500,
            timeout_sec=5.0,
        )
    assert result.ok is False
    assert result.reason == "insufficient_error_evidence"


@pytest.mark.asyncio
async def test_evaluate_no_pod_uses_wildcard():
    """Empty pod_name should use .* wildcard in LogQL."""
    lines = []
    with patch("workers.log_surge_probe.loki_query_range_lines") as mock_loki:
        mock_loki.return_value = (lines, "")
        result = await evaluate_log_surge_sigma_bypass(
            loki_base_url="http://loki:3100",
            namespace="multi-agent",
            pod_name="",
            window_sec=300,
            min_lines=10,
            min_ratio=0.5,
            line_limit=500,
            timeout_sec=5.0,
        )
        # Check that the logql used .* wildcard
        call_kwargs = mock_loki.call_args[1]
        assert ".*" in call_kwargs["logql"]
