"""Tests EPIC 1: LiveHostDiscoveryBackend chạm MÁY THẬT (không mock).

Theo chỉ thị "EVERYTHING MUST TOUCH A REAL MACHINE": probe cổng thật bằng TCP
connect, đọc tiến trình thật. Test mở một socket LISTEN thật trên cổng ephemeral
và xác minh backend phát hiện được — đây là integration thật, không AsyncMock.
"""
from __future__ import annotations

import socket

import pytest

from aoip.live_backend import LiveHostDiscoveryBackend


def _open_listen_socket() -> tuple[socket.socket, int]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


async def test_probe_port_detects_real_open_and_closed_ports():
    sock, port = _open_listen_socket()
    try:
        backend = LiveHostDiscoveryBackend()
        assert await backend.probe_port("127.0.0.1", port) is True
    finally:
        sock.close()
    # cổng vừa đóng → không còn reachable.
    assert await backend.probe_port("127.0.0.1", port) is False


async def test_discover_returns_real_host_structure():
    backend = LiveHostDiscoveryBackend()
    inv = await backend.discover(socket.gethostname())

    # host thật, đúng shape understand_host cần.
    assert inv["host"]
    assert isinstance(inv["services"], list)
    assert isinstance(inv["unknowns"], list)
    # mỗi service phát hiện được phải có cổng THẬT đang mở.
    for svc in inv["services"]:
        assert "name" in svc and "port" in svc
        assert await backend.probe_port("127.0.0.1", svc["port"]) is True


async def test_open_port_without_matching_process_is_unknown_owner():
    # Never assume: cổng thật mở nhưng KHÔNG có tiến trình 'testsvc' → port_owner Unknown,
    # KHÔNG tự gán tên service.
    sock, port = _open_listen_socket()
    try:
        backend = LiveHostDiscoveryBackend(extra_service_ports={"testsvc": [port]})
        inv = await backend.discover("127.0.0.1")
        names = {s["name"] for s in inv["services"]}
        assert "testsvc" not in names
        assert f"port_owner:{port}" in inv["unknowns"]
    finally:
        sock.close()
