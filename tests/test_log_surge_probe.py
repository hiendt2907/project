"""log_surge_probe parsers and ratio helpers."""

from __future__ import annotations

from workers.log_surge_probe import (
    parse_app_json_line_5xx,
    parse_http_status_from_access_line,
    _ratio_5xx_access,
)


def test_parse_nginx_combined() -> None:
    ln = '192.168.1.1 - - [08/Apr/2026:10:00:00 +0000] "GET / HTTP/1.1" 503 42 "-" "-"'
    assert parse_http_status_from_access_line(ln) == 503


def test_parse_json_status() -> None:
    assert parse_app_json_line_5xx('{"level":"info","status":200}') is False
    assert parse_app_json_line_5xx('{"status":503,"msg":"x"}') is True
    assert parse_app_json_line_5xx('{"level":"error"}') is True


def test_ratio_access() -> None:
    lines = [
        '"GET /a HTTP/1.1" 200 1',
        '"GET /b HTTP/1.1" 503 2',
        '"GET /c HTTP/1.1" 503 3',
        '"GET /d HTTP/1.1" 500 4',
        '"GET /e HTTP/1.1" 200 5',
    ]
    bad, parsed = _ratio_5xx_access(lines)
    assert parsed == 5
    assert bad == 3
