"""Coverage-gap tests for src/workers/health_server.py (28.9% → raise to ~85%).

Tests cover:
- record_message_processed
- update_check_state
- _read_check_states (startup grace, no messages, stalled, active messages)
- _build_health (ok / degraded / unhealthy)
- _HealthHandler.do_GET (/healthz, /readyz, /health, 404)
- start_health_server (thread started, port already in use graceful fail)
- configure (no-op)
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("OMNI_ENV_MODE", "dev")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379")

# Reset module-level globals before every test to avoid cross-test contamination
import workers.health_server as hs


@pytest.fixture(autouse=True)
def reset_health_server_state():
    """Reset module-level state before each test."""
    with hs._lock:
        hs._last_message_ts = 0.0
        hs._startup_ts = time.time()
        hs._check_states = {
            "kafka_lag": ("ok", "not polled yet"),
            "redis_ping": ("ok", "not polled yet"),
            "llm_up": ("ok", "not polled yet"),
            "last_message_age": ("ok", "not started yet"),
        }
    yield


# ---------------------------------------------------------------------------
# configure (no-op)
# ---------------------------------------------------------------------------

def test_configure_noop():
    """configure() is a no-op but should not raise."""
    hs.configure(redis=MagicMock(), llm_base_url="http://ollama:11434")


# ---------------------------------------------------------------------------
# record_message_processed
# ---------------------------------------------------------------------------

def test_record_message_processed_sets_timestamp():
    before = time.time()
    hs.record_message_processed()
    with hs._lock:
        ts = hs._last_message_ts
    assert ts >= before
    assert ts <= time.time()


def test_record_message_processed_updates_on_second_call():
    hs.record_message_processed()
    with hs._lock:
        first = hs._last_message_ts
    time.sleep(0.01)
    hs.record_message_processed()
    with hs._lock:
        second = hs._last_message_ts
    assert second > first


# ---------------------------------------------------------------------------
# update_check_state
# ---------------------------------------------------------------------------

def test_update_check_state_sets_known():
    hs.update_check_state("redis_ping", "unhealthy", "connection refused")
    with hs._lock:
        assert hs._check_states["redis_ping"] == ("unhealthy", "connection refused")


def test_update_check_state_adds_new_key():
    hs.update_check_state("custom_check", "degraded", "partial fail")
    with hs._lock:
        assert hs._check_states["custom_check"] == ("degraded", "partial fail")


def test_update_check_state_overwrites():
    hs.update_check_state("kafka_lag", "ok", "lag=0")
    hs.update_check_state("kafka_lag", "unhealthy", "lag=9999")
    with hs._lock:
        assert hs._check_states["kafka_lag"] == ("unhealthy", "lag=9999")


# ---------------------------------------------------------------------------
# _read_check_states
# ---------------------------------------------------------------------------

def test_read_check_states_startup_grace():
    """No messages received, but within grace period → last_message_age ok."""
    with hs._lock:
        hs._last_message_ts = 0.0
        hs._startup_ts = time.time()  # just started

    states = hs._read_check_states()
    status, detail = states["last_message_age"]
    assert status == "ok"
    assert "startup grace" in detail


def test_read_check_states_no_messages_past_grace():
    """No messages, past grace period → degraded."""
    with hs._lock:
        hs._last_message_ts = 0.0
        hs._startup_ts = time.time() - (hs._STARTUP_GRACE_SECONDS + 10)

    states = hs._read_check_states()
    status, detail = states["last_message_age"]
    assert status == "degraded"
    assert "no messages" in detail


def test_read_check_states_recent_message():
    """Recent message → last_message_age ok."""
    hs.record_message_processed()
    states = hs._read_check_states()
    status, detail = states["last_message_age"]
    assert status == "ok"
    assert "age=" in detail


def test_read_check_states_stalled_message():
    """Message received long ago → unhealthy."""
    with hs._lock:
        hs._last_message_ts = time.time() - (hs._MESSAGE_STALL_SECONDS + 30)

    states = hs._read_check_states()
    status, detail = states["last_message_age"]
    assert status == "unhealthy"
    assert "stalled" in detail


def test_read_check_states_propagates_check_states():
    """Custom check state is included in output."""
    hs.update_check_state("redis_ping", "unhealthy", "no connection")
    states = hs._read_check_states()
    assert states["redis_ping"] == ("unhealthy", "no connection")


# ---------------------------------------------------------------------------
# _build_health
# ---------------------------------------------------------------------------

def test_build_health_all_ok():
    for key in list(hs._check_states.keys()):
        hs.update_check_state(key, "ok", "fine")
    hs.record_message_processed()
    health = hs._build_health()
    assert health["status"] == "ok"
    assert "checks" in health
    assert health["uptime_s"] >= 0


def test_build_health_degraded():
    hs.update_check_state("llm_up", "degraded", "llm_up=0")
    hs.record_message_processed()
    health = hs._build_health()
    assert health["status"] == "degraded"


def test_build_health_unhealthy():
    hs.update_check_state("kafka_lag", "unhealthy", "lag=9999")
    hs.record_message_processed()
    health = hs._build_health()
    assert health["status"] == "unhealthy"


def test_build_health_unhealthy_wins_over_degraded():
    hs.update_check_state("llm_up", "degraded", "llm down")
    hs.update_check_state("kafka_lag", "unhealthy", "lag huge")
    hs.record_message_processed()
    health = hs._build_health()
    assert health["status"] == "unhealthy"


def test_build_health_structure():
    hs.record_message_processed()
    health = hs._build_health()
    assert isinstance(health["checks"], dict)
    assert isinstance(health["uptime_s"], float)
    assert isinstance(health["ts"], float)
    for k, v in health["checks"].items():
        assert "status" in v
        assert "detail" in v


# ---------------------------------------------------------------------------
# _HealthHandler.do_GET via simulated HTTP requests
# ---------------------------------------------------------------------------

class _FakeSocket:
    """Minimal socket-like object for BaseHTTPRequestHandler."""
    def __init__(self):
        self._data = b""

    def makefile(self, mode, bufsize=-1):
        if "r" in mode:
            return io.BufferedReader(io.BytesIO(self._data))
        return io.BufferedWriter(io.BytesIO())

    def sendall(self, data: bytes) -> None:
        pass


def _make_handler(path: str) -> hs._HealthHandler:
    """Create a _HealthHandler instance for the given path."""
    request_line = f"GET {path} HTTP/1.0\r\n\r\n".encode()

    class _FakeSock:
        def makefile(self, mode, bufsize=-1):
            if "r" in mode:
                return io.BufferedReader(io.BytesIO(request_line))
            return io.BytesIO()

        def sendall(self, data: bytes) -> None:
            pass

    output = io.BytesIO()

    # Patch wfile to capture output
    handler = hs._HealthHandler.__new__(hs._HealthHandler)
    handler.path = path
    handler.wfile = output
    handler.rfile = io.BytesIO(request_line)

    captured = []

    def fake_send_response(code):
        captured.append(("response", code))

    def fake_send_header(k, v):
        captured.append(("header", k, v))

    def fake_end_headers():
        captured.append(("end_headers",))

    def fake_write(data):
        captured.append(("body", data))

    handler.send_response = fake_send_response
    handler.send_header = fake_send_header
    handler.end_headers = fake_end_headers
    handler.wfile = MagicMock()
    handler.wfile.write = fake_write

    handler._captured = captured
    return handler


def test_health_handler_healthz_ok():
    hs.record_message_processed()
    for key in list(hs._check_states.keys()):
        hs.update_check_state(key, "ok", "fine")

    handler = _make_handler("/healthz")
    handler.do_GET()

    response_codes = [x[1] for x in handler._captured if x[0] == "response"]
    assert response_codes == [200]
    bodies = [x[1] for x in handler._captured if x[0] == "body"]
    assert len(bodies) == 1
    data = json.loads(bodies[0])
    assert "status" in data
    assert data["status"] == "ok"


def test_health_handler_healthz_unhealthy():
    hs.update_check_state("kafka_lag", "unhealthy", "lag=99999")
    hs.record_message_processed()

    handler = _make_handler("/healthz")
    handler.do_GET()

    response_codes = [x[1] for x in handler._captured if x[0] == "response"]
    assert response_codes == [503]
    bodies = [x[1] for x in handler._captured if x[0] == "body"]
    data = json.loads(bodies[0])
    assert data["status"] == "unhealthy"


def test_health_handler_health_alias():
    hs.record_message_processed()
    for key in list(hs._check_states.keys()):
        hs.update_check_state(key, "ok", "fine")

    handler = _make_handler("/health")
    handler.do_GET()

    response_codes = [x[1] for x in handler._captured if x[0] == "response"]
    assert response_codes == [200]


def test_health_handler_readyz_ok():
    hs.record_message_processed()
    for key in list(hs._check_states.keys()):
        hs.update_check_state(key, "ok", "fine")

    handler = _make_handler("/readyz")
    handler.do_GET()

    response_codes = [x[1] for x in handler._captured if x[0] == "response"]
    assert response_codes == [200]
    bodies = [x[1] for x in handler._captured if x[0] == "body"]
    data = json.loads(bodies[0])
    assert data["ready"] is True


def test_health_handler_readyz_unhealthy():
    hs.update_check_state("kafka_lag", "unhealthy", "lag=99999")
    hs.record_message_processed()

    handler = _make_handler("/readyz")
    handler.do_GET()

    response_codes = [x[1] for x in handler._captured if x[0] == "response"]
    assert response_codes == [503]
    bodies = [x[1] for x in handler._captured if x[0] == "body"]
    data = json.loads(bodies[0])
    assert data["ready"] is False
    assert data["status"] == "unhealthy"


def test_health_handler_readyz_degraded_is_ready():
    """readyz returns 200 even when degraded (not unhealthy)."""
    hs.record_message_processed()
    for key in list(hs._check_states.keys()):
        hs.update_check_state(key, "ok", "fine")
    hs.update_check_state("llm_up", "degraded", "llm_up=0")

    handler = _make_handler("/readyz")
    handler.do_GET()

    response_codes = [x[1] for x in handler._captured if x[0] == "response"]
    assert response_codes == [200]
    bodies = [x[1] for x in handler._captured if x[0] == "body"]
    data = json.loads(bodies[0])
    assert data["ready"] is True
    assert data["status"] == "degraded"


def test_health_handler_not_found():
    handler = _make_handler("/metrics")
    handler.do_GET()

    response_codes = [x[1] for x in handler._captured if x[0] == "response"]
    assert response_codes == [404]


def test_health_handler_log_message_suppressed():
    """log_message should not raise (it's a no-op)."""
    handler = hs._HealthHandler.__new__(hs._HealthHandler)
    handler.log_message("format %s", "arg")  # Should not raise


# ---------------------------------------------------------------------------
# start_health_server
# ---------------------------------------------------------------------------

def test_start_health_server_starts_daemon_thread():
    """Verify thread is created (but use a port that will fail immediately)."""
    # Use a port that's in use by finding an available one
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    # Server should start without raising in caller's thread
    hs.start_health_server(host="127.0.0.1", port=port)

    # Give thread time to start
    time.sleep(0.05)

    # Check the server thread is alive
    threads = [t for t in threading.enumerate() if t.name == "health-server"]
    assert len(threads) >= 1
    assert threads[0].daemon is True


def test_start_health_server_port_in_use():
    """Even if port binding fails, the calling thread should not raise."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    # Keep s open so the port is in use

    try:
        # This should start a thread that silently fails
        hs.start_health_server(host="127.0.0.1", port=port)
        time.sleep(0.05)
        # No exception should propagate to caller
    finally:
        s.close()
