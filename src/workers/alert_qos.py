"""Alert QoS — ingress storm control via priority classification + atomic sliding-window admission.

Gemini-sấy lesson (plan step 1): MAXLEN-trimming the main stream is DATA LOSS, not
backpressure — a 10k-alert storm would silently drop the 9k tail where the root cause
often lives. Instead this module splits admission by priority:

- ``CRITICAL`` (severity critical/high/page/emergency, or security/SIEM-critical alerts)
  → NEVER shed. Always admitted.
- ``NORMAL`` (warning/info/none) → subject to an **atomic sliding-window** admission cap.
  Excess in the window is shed (the system stays responsive for criticals).
- ``MALFORMED`` (unparseable, or missing all of ns/pod/alertname identity) → routed to DLQ,
  never fed to the diagnostic/mutate pipeline.

The sliding-window counter is a single Redis Lua script (one atomic round-trip): under a
storm thousands of concurrent admissions race on the same key, and a Python read-modify-write
(ZCARD → branch → ZADD) would over-admit. The Lua script makes count-and-claim atomic.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class AlertPriority(StrEnum):
    CRITICAL = "critical"
    NORMAL = "normal"
    MALFORMED = "malformed"


class AdmissionDecision(StrEnum):
    ADMIT = "admit"
    SHED = "shed"


# Severity labels that must never be shed under load.
_CRITICAL_SEVERITIES = frozenset(
    {"critical", "high", "page", "emergency", "fatal", "sev1", "p1"}
)

# Sliding-window admission for NORMAL-priority alerts. One atomic round-trip:
#   1. drop entries older than the window
#   2. read current count
#   3. admit (ZADD + bump TTL) only if under cap, else shed
# Returns 1 (admit) or 0 (shed). KEYS[1]=window zset; ARGV=now, window_sec, cap, member.
_ADMIT_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local cap = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local cnt = redis.call('ZCARD', key)
if cnt < cap then
  redis.call('ZADD', key, now, member)
  redis.call('EXPIRE', key, window)
  return 1
end
return 0
"""


def _first_alert_labels(payload: dict[str, Any]) -> dict[str, Any] | None:
    body = payload.get("data") or {}
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            return None
    if not isinstance(body, dict):
        return None
    alerts = body.get("alerts") or []
    if not alerts or not isinstance(alerts[0], dict):
        return None
    labels = alerts[0].get("labels") or {}
    return labels if isinstance(labels, dict) else None


def classify_alert_priority(payload: dict[str, Any]) -> AlertPriority:
    """Classify an ingress alert envelope into a QoS priority class.

    Non-alert sources (telegram, callbacks, meta) are treated as NORMAL — they bypass
    the alert-shaped checks but still get fair-share admission so a noisy non-alert
    source cannot starve the pipeline.
    """
    source = str(payload.get("source") or "").strip()
    if source not in ("prometheus", "siem"):
        # Not an external alert storm vector; admit under the normal lane.
        return AlertPriority.NORMAL

    labels = _first_alert_labels(payload)
    if labels is None:
        return AlertPriority.MALFORMED

    alertname = str(labels.get("alertname") or "").strip()
    namespace = str(labels.get("namespace") or labels.get("exported_namespace") or "").strip()
    pod = str(labels.get("pod") or "").strip()
    # Identity floor: an alert with no name AND no ns/pod target cannot be diagnosed
    # or remediated — it is malformed for our purposes (drop to DLQ, not the pipeline).
    if not alertname and not namespace and not pod:
        return AlertPriority.MALFORMED

    severity = str(labels.get("severity") or "").strip().lower()
    if severity in _CRITICAL_SEVERITIES:
        return AlertPriority.CRITICAL
    # SIEM incidents default to critical handling — security can't wait behind warnings.
    if source == "siem":
        return AlertPriority.CRITICAL
    return AlertPriority.NORMAL


async def admit_alert(
    redis: Any,
    priority: AlertPriority,
    *,
    now: float,
    member: str,
    normal_cap: int,
    window_sec: int,
) -> AdmissionDecision:
    """Atomic sliding-window admission. CRITICAL is never shed; NORMAL is capped per window.

    Fail-OPEN on Redis error: if the admission counter is unavailable we admit rather than
    drop, since losing a real incident is worse than a transient over-admit.
    """
    if priority is AlertPriority.CRITICAL:
        return AdmissionDecision.ADMIT
    if normal_cap <= 0 or window_sec <= 0:
        return AdmissionDecision.ADMIT
    key = "omni:qos:adm:normal"
    try:
        admitted = await redis.eval(
            _ADMIT_LUA, 1, key, str(now), str(window_sec), str(normal_cap), member
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("event=qos_admit_eval_failed err=%s — failing open", e)
        return AdmissionDecision.ADMIT
    return AdmissionDecision.ADMIT if int(admitted or 0) == 1 else AdmissionDecision.SHED
