"""WorkerSettings — Prometheus URL scheme coercion."""

from __future__ import annotations

import pytest

from workers.settings import WorkerSettings, default_prometheus_http_base


def test_prometheus_default_hostport_coerced_to_http() -> None:
    ws = WorkerSettings()
    expect = default_prometheus_http_base()
    assert ws.prometheus_url == expect
    assert ws.vmagent_url == expect
    assert expect == "http://prometheus.monitor.svc.cluster.local:9090"


def test_prometheus_explicit_hostport_coerced() -> None:
    ws = WorkerSettings(prometheus_url="other.svc:9090", vmagent_url="other.svc:9090")
    assert ws.prometheus_url == "http://other.svc:9090"
    assert ws.vmagent_url == "http://other.svc:9090"


def test_agentic_debug_io_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_AGENTIC_DEBUG_IO", "true")
    ws = WorkerSettings()
    assert ws.agentic_debug_io is True


def test_agentic_debug_io_env_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_AGENTIC_DEBUG_IO", "1")
    ws = WorkerSettings()
    assert ws.agentic_debug_io is True


def test_prometheus_full_url_unchanged() -> None:
    ws = WorkerSettings(
        prometheus_url="https://prom.example:9090",
        vmagent_url="http://prom:9090",
    )
    assert ws.prometheus_url == "https://prom.example:9090"
    assert ws.vmagent_url == "http://prom:9090"
