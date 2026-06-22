"""Tests for business error lane: 5xx, 429, 499, 401, 403 classification."""

from __future__ import annotations

import pytest

from workers.log_surge_probe import (
    AccessErrorCounts,
    ErrorClass,
    classify_http_status,
    count_access_errors,
    parse_http_status_from_access_line,
)


# ---------------------------------------------------------------------------
# classify_http_status
# ---------------------------------------------------------------------------

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
    (301, "ok"),
    (404, "ok"),
])
def test_classify_http_status(status: int, expected: ErrorClass) -> None:
    assert classify_http_status(status) == expected


# ---------------------------------------------------------------------------
# parse_http_status_from_access_line — new statuses
# ---------------------------------------------------------------------------

def test_parse_combined_log_429() -> None:
    line = '127.0.0.1 - - [01/Jan/2024:00:00:00 +0000] "GET /api HTTP/1.1" 429 512 "-" "-"'
    assert parse_http_status_from_access_line(line) == 429


def test_parse_combined_log_499() -> None:
    line = '10.0.0.1 - - [01/Jan/2024:00:00:00 +0000] "POST /upload HTTP/1.1" 499 0 "-" "-"'
    assert parse_http_status_from_access_line(line) == 499


def test_parse_combined_log_401() -> None:
    line = '192.168.1.1 - - [01/Jan/2024:00:00:00 +0000] "GET /admin HTTP/1.1" 401 123'
    assert parse_http_status_from_access_line(line) == 401


def test_parse_combined_log_403() -> None:
    line = '10.0.0.2 - - [01/Jan/2024] "DELETE /secret HTTP/1.1" 403 0'
    assert parse_http_status_from_access_line(line) == 403


def test_parse_kv_style_429() -> None:
    line = "ts=2024-01 status=429 path=/api method=GET"
    assert parse_http_status_from_access_line(line) == 429


# ---------------------------------------------------------------------------
# count_access_errors — mixed error class lines
# ---------------------------------------------------------------------------

def make_access_lines(entries: list[tuple[int, int]]) -> list[str]:
    """Generate count lines for status code."""
    lines = []
    for status, count in entries:
        for _ in range(count):
            lines.append(f'10.0.0.1 - - [01/Jan/2024] "GET /x HTTP/1.1" {status} 100')
    return lines


def test_count_access_errors_5xx_dominant() -> None:
    lines = make_access_lines([(500, 8), (200, 2)])
    counts = count_access_errors(lines)
    assert counts.count_5xx == 8
    assert counts.total_parsed == 10
    assert counts.count_rate_limit == 0
    assert counts.count_auth == 0


def test_count_access_errors_429_dominant() -> None:
    lines = make_access_lines([(429, 7), (200, 3)])
    counts = count_access_errors(lines)
    assert counts.count_rate_limit == 7
    assert counts.count_5xx == 0


def test_count_access_errors_499_detected() -> None:
    lines = make_access_lines([(499, 6), (200, 4)])
    counts = count_access_errors(lines)
    assert counts.count_client_abort == 6


def test_count_access_errors_auth_dominant() -> None:
    lines = make_access_lines([(401, 4), (403, 3), (200, 3)])
    counts = count_access_errors(lines)
    assert counts.count_auth == 7


def test_count_access_errors_mixed_classes() -> None:
    lines = make_access_lines([(500, 3), (429, 2), (499, 1), (403, 2), (200, 2)])
    counts = count_access_errors(lines)
    assert counts.total_parsed == 10
    assert counts.count_5xx == 3
    assert counts.count_rate_limit == 2
    assert counts.count_client_abort == 1
    assert counts.count_auth == 2
    # histogram tracks all
    assert counts.status_histogram[500] == 3
    assert counts.status_histogram[429] == 2


def test_count_access_errors_empty() -> None:
    counts = count_access_errors([])
    assert counts.total_parsed == 0
    assert counts.count_5xx == 0


def test_count_access_errors_no_parseable() -> None:
    lines = ["not a log line at all", "another garbage line"]
    counts = count_access_errors(lines)
    assert counts.total_parsed == 0
