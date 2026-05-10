"""Independent health/readiness HTTP server (thread-based, không block asyncio).

Design: passive — đọc state từ metrics registry và shared state được set bởi
observability_metrics_loop(). Không chạy async code từ thread.
"""

from __future__ import annotations

import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

logger = logging.getLogger(__name__)

_MESSAGE_STALL_SECONDS = 600  # 10 phút
_STARTUP_GRACE_SECONDS = 60   # 1 phút — giảm từ 120s để phát hiện sớm hơn

_lock = threading.Lock()
_last_message_ts: float = 0.0
_startup_ts: float = time.time()
# Cached check states — được set bởi observability_metrics_loop hoặc health_state_update()
_check_states: dict[str, tuple[str, str]] = {
    "kafka_lag": ("ok", "not polled yet"),
    "redis_ping": ("ok", "not polled yet"),
    "llm_up": ("ok", "not polled yet"),
    "last_message_age": ("ok", "not started yet"),
}


def configure(*, redis: Any = None, llm_base_url: str = "") -> None:
    """Called once at worker startup. redis/llm_base_url not needed in passive mode."""
    pass


def record_message_processed() -> None:
    global _last_message_ts
    with _lock:
        _last_message_ts = time.time()


def update_check_state(check_name: str, status: str, detail: str) -> None:
    """Called from the observability loop to push health state into health server."""
    with _lock:
        _check_states[check_name] = (status, detail)


def _read_check_states() -> dict[str, tuple[str, str]]:
    with _lock:
        now = time.time()
        ts = _last_message_ts
        uptime = now - _startup_ts
        states = dict(_check_states)

    # last_message_age is always computed live
    if ts == 0.0:
        if uptime < _STARTUP_GRACE_SECONDS:
            states["last_message_age"] = ("ok", f"startup grace {uptime:.0f}s")
        else:
            states["last_message_age"] = ("degraded", f"no messages in {uptime:.0f}s")
    else:
        age = now - ts
        if age > _MESSAGE_STALL_SECONDS:
            states["last_message_age"] = ("unhealthy", f"stalled {age:.0f}s")
        else:
            states["last_message_age"] = ("ok", f"age={age:.0f}s")

    return states


def _build_health() -> dict:
    checks = _read_check_states()
    statuses = [s for s, _ in checks.values()]
    if "unhealthy" in statuses:
        overall = "unhealthy"
    elif "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "ok"
    return {
        "status": overall,
        "checks": {k: {"status": s, "detail": d} for k, (s, d) in checks.items()},
        "uptime_s": round(time.time() - _startup_ts, 1),
        "ts": time.time(),
    }


class _HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, *_: Any) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        import json

        if self.path == "/readyz":
            # Ready = health checks not "unhealthy" (connectivity OK).
            # Message staleness is a liveness/degraded concern, not readiness.
            health = _build_health()
            ready = health["status"] != "unhealthy"
            code = 200 if ready else 503
            body = json.dumps({"ready": ready, "status": health["status"]}).encode()
        elif self.path in ("/healthz", "/health"):
            health = _build_health()
            code = 200 if health["status"] != "unhealthy" else 503
            body = json.dumps(health).encode()
        else:
            code = 404
            body = b'{"error":"not found"}'

        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_health_server(host: str = "0.0.0.0", port: int = 8090) -> None:
    def _run() -> None:
        try:
            server = HTTPServer((host, port), _HealthHandler)
            logger.info("health server listening on %s:%d", host, port)
            server.serve_forever()
        except Exception as e:
            logger.warning("health server failed to start: %s", e)

    t = threading.Thread(target=_run, daemon=True, name="health-server")
    t.start()
