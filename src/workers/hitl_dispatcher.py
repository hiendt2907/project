"""
HITL Dispatcher — reads omni-hitl-pending, calls FinGuard HITL API, routes on decision.

Flow:
  omni-hitl-pending (Kafka)
    → POST  /v1/hitl/register           register as "pending"
    → GET   /v1/hitl/decisions/{id}     poll until approved|rejected|timeout
    → APPROVED  → omni-actions          executor picks up
    → REJECTED  → omni-action-feedback  analyst gets rejection context

Design invariants:
  - NO cross-namespace Redis writes: decisions are polled over HTTP, not via Redis.
  - Manual Kafka offset commit: offset committed only after the action is routed.
  - Bounded concurrency: at most HITL_MAX_CONCURRENT pending approvals in-flight.
  - Exponential backoff: API/network failures back off with full jitter, not busy-loop.
  - Strict types: all internal data passed as frozen dataclasses, not raw dicts.

Environment:
  OMNI_KAFKA_BOOTSTRAP_SERVERS          kafka:9092
  OMNI_KAFKA_TOPIC_HITL_PENDING         omni-hitl-pending
  OMNI_KAFKA_TOPIC_ACTIONS              omni-actions
  OMNI_KAFKA_TOPIC_ACTION_FEEDBACK      omni-action-feedback
  OMNI_REDIS_URL                        redis://redis:6379/0   (state audit only — no decision polling)
  HITL_API_BASE_URL                     http://hitl-api.finguard-customer.svc.cluster.local:8080
  HITL_API_TOKEN                        (from secret)
  HITL_APPROVAL_TIMEOUT_SEC             1800
  HITL_POLL_INTERVAL_SEC                10      (base; doubles with jitter on failures)
  HITL_MAX_CONCURRENT                   32      (max parallel pending approvals)
  HITL_API_REGISTER_MAX_RETRIES         5
  HITL_API_REGISTER_BACKOFF_BASE_SEC    1.0
  OMNI_HITL_FALLBACK_CHANNEL            none    (slack | none)
  OMNI_HITL_FALLBACK_WEBHOOK_URL               (Slack incoming webhook URL)
  OMNI_HITL_ESCALATION_TIMEOUT_SEC      900    (seconds before fallback emit)
  OMNI_HITL_DEAD_LETTER_TTL_SEC         86400  (dead-letter Redis key TTL)

Escalation timeline (S1.3):
  T=0:                       Register HITL → Telegram notification (existing)
  T=ESCALATION_TIMEOUT_SEC:  Emit to fallback channel (Slack webhook)
  T=APPROVAL_TIMEOUT_SEC:    Auto-reject with HITL_TIMEOUT + store dead-letter in Redis
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Final

import httpx
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.structs import TopicPartition
from redis.asyncio import Redis

from workers.hitl_fallback import emit_slack_fallback, store_dead_letter
from workers.pipeline_stages import mark_stage

log = logging.getLogger("hitl_dispatcher")
logging.basicConfig(
    level=logging.INFO,
    format='{"ts": "%(asctime)s", "lvl": "%(levelname)s", "logger": "%(name)s", "msg": %(message)s}',
)

# ---------------------------------------------------------------------------
# Config — all values loaded once at module import time.
# ---------------------------------------------------------------------------
_KAFKA_SERVERS: Final[str] = os.environ.get("OMNI_KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
_TOPIC_PENDING: Final[str] = os.environ.get("OMNI_KAFKA_TOPIC_HITL_PENDING", "omni-hitl-pending")
_TOPIC_ACTIONS: Final[str] = os.environ.get("OMNI_KAFKA_TOPIC_ACTIONS", "omni-actions")
_TOPIC_FEEDBACK: Final[str] = os.environ.get("OMNI_KAFKA_TOPIC_ACTION_FEEDBACK", "omni-action-feedback")
_REDIS_URL: Final[str] = os.environ.get("OMNI_REDIS_URL", "redis://redis:6379/0")
_HITL_API_BASE: Final[str] = os.environ.get("HITL_API_BASE_URL", "http://hitl-api.finguard-customer.svc.cluster.local:8080").rstrip("/")
_HITL_API_TOKEN: Final[str] = os.environ.get("HITL_API_TOKEN", "")
_APPROVAL_TIMEOUT_SEC: Final[int] = int(os.environ.get("HITL_APPROVAL_TIMEOUT_SEC", "1800"))
_POLL_INTERVAL_BASE_SEC: Final[float] = float(os.environ.get("HITL_POLL_INTERVAL_SEC", "10"))
_MAX_CONCURRENT: Final[int] = max(1, int(os.environ.get("HITL_MAX_CONCURRENT", "32")))
_REGISTER_MAX_RETRIES: Final[int] = max(1, int(os.environ.get("HITL_API_REGISTER_MAX_RETRIES", "5")))
_REGISTER_BACKOFF_BASE: Final[float] = float(os.environ.get("HITL_API_REGISTER_BACKOFF_BASE_SEC", "1.0"))
_CONSUMER_GROUP: Final[str] = os.environ.get("HITL_DISPATCHER_GROUP", "omni-hitl-dispatcher")

# S1.3 — Fallback channel + dead-letter (bounds mirror WorkerSettings Pydantic validators)
_ESCALATION_TIMEOUT_SEC: Final[int] = min(
    _APPROVAL_TIMEOUT_SEC - 1,  # must be strictly less than approval window
    max(60, int(os.environ.get("OMNI_HITL_ESCALATION_TIMEOUT_SEC", "900"))),
)
_FALLBACK_CHANNEL: Final[str] = os.environ.get("OMNI_HITL_FALLBACK_CHANNEL", "none").lower().strip()
_FALLBACK_WEBHOOK_URL: Final[str] = os.environ.get("OMNI_HITL_FALLBACK_WEBHOOK_URL", "")
_DEAD_LETTER_TTL_SEC: Final[int] = min(
    604800,  # 7 days max (WorkerSettings le=604800)
    max(3600, int(os.environ.get("OMNI_HITL_DEAD_LETTER_TTL_SEC", "86400"))),
)

# Redis key for internal audit state (same-namespace Omni Redis — no cross-namespace writes).
_STATE_KEY_FMT: Final[str] = "omni:hitl:state:{trace}"


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

class _Decision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT  = "timeout"


@dataclass(frozen=True)
class _PendingAction:
    """Typed representation of an omni-hitl-pending Kafka message."""
    trace_id:      str
    tool_name:     str
    args:          dict                  # kept as dict for Kafka forwarding
    reasoning_chain: dict | None
    siem_incident_id: str
    siem_tenant:   str
    siem_category: str
    siem_playbook_id: str
    explain:       str
    advise:        str
    hitl_reason:   str                   # original hitl_reason from the Kafka message (audit trail)
    raw_body:      dict                  # full original body stripped of hitl_ keys for forwarding


@dataclass(frozen=True)
class _RejectionFeedback:
    action:         str = "execute_mutate_feedback"
    trace_id:       str = ""
    tool_name:      str = ""
    exit_code:      int = -1
    stdout:         str = ""
    stderr:         str = ""
    hitl_rejected:  bool = True
    siem_incident_id: str = ""
    siem_tenant:    str = ""
    hitl_reason:    str = ""            # preserved for analyst audit trail


# ---------------------------------------------------------------------------
# Partition-safe watermark tracker
# ---------------------------------------------------------------------------

class _PartitionTracker:
    """Per-partition watermark tracker for safe manual Kafka offset commits.

    Invariant: the commit offset only advances when there are no in-flight
    offsets below the watermark, so a pod restart cannot skip un-processed
    messages regardless of completion order.
    """

    __slots__ = ("_in_flight", "_committed_up_to", "_max_seen")

    def __init__(self) -> None:
        self._in_flight: set[int] = set()
        self._committed_up_to: int = -1
        self._max_seen: int = -1

    def start(self, offset: int) -> None:
        """Register an offset as in-flight before handing it to a worker."""
        self._in_flight.add(offset)
        if offset > self._max_seen:
            self._max_seen = offset

    def done(self, offset: int) -> int | None:
        """Mark offset complete.

        Returns the Kafka commit offset (= next-to-read) when the watermark
        advances, or None when it does not (earlier offsets still in-flight).
        """
        self._in_flight.discard(offset)
        if not self._in_flight:
            new_watermark = self._max_seen
        else:
            new_watermark = min(self._in_flight) - 1

        if new_watermark > self._committed_up_to:
            self._committed_up_to = new_watermark
            return new_watermark + 1   # Kafka: committed offset = next offset to fetch
        return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class _ParseError(ValueError):
    pass


def _parse_pending(raw: bytes) -> _PendingAction:
    """Deserialise a Kafka message into a _PendingAction.  Raises _ParseError on malformed input."""
    try:
        outer: dict = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise _ParseError(f"outer json decode: {e}") from e

    data_str = outer.get("data") or "{}"
    try:
        body: dict = json.loads(data_str) if isinstance(data_str, str) else data_str
    except json.JSONDecodeError as e:
        raise _ParseError(f"inner data json decode: {e}") from e

    if not isinstance(body, dict):
        raise _ParseError("body must be a JSON object")

    trace = str(body.get("trace_id") or body.get("id") or "").strip()
    if not trace:
        raise _ParseError("missing trace_id / id")

    # Strip hitl_ metadata keys but keep hitl_reason for audit trail on rejection.
    hitl_reason = str(body.get("hitl_reason") or "siem_critical_action")
    raw_body = {k: v for k, v in body.items() if k not in {
        "hitl_pending", "hitl_reason",
        "siem_incident_id", "siem_tenant", "siem_category", "siem_playbook_id",
        "explain", "advise",
    }}

    return _PendingAction(
        trace_id=trace,
        tool_name=str(body.get("tool_name") or "").strip(),
        args=body.get("args") if isinstance(body.get("args"), dict) else {},
        reasoning_chain=body.get("reasoning_chain") if isinstance(body.get("reasoning_chain"), dict) else None,
        siem_incident_id=str(body.get("siem_incident_id") or trace).strip(),
        siem_tenant=str(body.get("siem_tenant") or "omni").strip(),
        siem_category=str(body.get("siem_category") or "").strip(),
        siem_playbook_id=str(body.get("siem_playbook_id") or "").strip(),
        explain=str(body.get("explain") or "").strip()[:500],
        advise=str(body.get("advise") or "").strip()[:500],
        hitl_reason=hitl_reason,
        raw_body=raw_body,
    )


# ---------------------------------------------------------------------------
# Exponential backoff helper
# ---------------------------------------------------------------------------

def _jitter_sleep_sec(attempt: int, base: float, cap: float = 60.0) -> float:
    """Full-jitter exponential backoff: sleep = random(0, min(cap, base * 2^attempt))."""
    return random.uniform(0.0, min(cap, base * math.pow(2, attempt)))


# ---------------------------------------------------------------------------
# HITL API client
# ---------------------------------------------------------------------------

async def _register_pending(
    client: httpx.AsyncClient,
    action: _PendingAction,
) -> bool:
    """
    POST /v1/hitl/decisions with decision="pending" — registers the action for operator review.
    Retries with exponential backoff on network/5xx failures.
    Returns False (hard fail) only after exhausting all retries or receiving 4xx.
    """
    if not _HITL_API_TOKEN:
        log.error(
            '"hitl_register_skipped" trace="%s" reason="HITL_API_TOKEN not configured — '
            'operator will NOT see this pending action"',
            action.trace_id,
        )
        return False

    payload = {
        "incident_id": action.siem_incident_id,
        "tenant_id":   action.siem_tenant,
        "action_type": action.tool_name or "omni_mutation",
        "reason": (
            f"omni:awaiting_approval trace={action.trace_id} tool={action.tool_name}"
            + (f" | explain: {action.explain}" if action.explain else "")
            + (f" | advise: {action.advise}" if action.advise else "")
        )[:1000],
        "actor": "omni-hitl-dispatcher",
    }

    for attempt in range(_REGISTER_MAX_RETRIES):
        try:
            resp = await client.post(
                f"{_HITL_API_BASE}/v1/hitl/register",
                json=payload,
                headers={"Authorization": f"Bearer {_HITL_API_TOKEN}"},
                timeout=10.0,
            )
            if resp.status_code in (200, 201):
                log.info(
                    '"hitl_registered" trace="%s" incident_id="%s"',
                    action.trace_id,
                    action.siem_incident_id,
                )
                return True

            # 4xx — bad request/auth; no point retrying.
            if 400 <= resp.status_code < 500:
                log.error(
                    '"hitl_register_client_error" trace="%s" status=%d body="%s"',
                    action.trace_id,
                    resp.status_code,
                    resp.text[:300],
                )
                return False

            # 5xx — retry after backoff.
            log.warning(
                '"hitl_register_server_error" trace="%s" status=%d attempt=%d',
                action.trace_id,
                resp.status_code,
                attempt,
            )

        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            log.warning(
                '"hitl_register_network_error" trace="%s" attempt=%d err="%s"',
                action.trace_id,
                attempt,
                exc,
            )

        sleep = _jitter_sleep_sec(attempt, _REGISTER_BACKOFF_BASE)
        await asyncio.sleep(sleep)

    log.error(
        '"hitl_register_exhausted" trace="%s" retries=%d — operator cannot approve this action',
        action.trace_id,
        _REGISTER_MAX_RETRIES,
    )
    return False


async def _poll_api_for_decision(
    client: httpx.AsyncClient,
    action: _PendingAction,
) -> _Decision:
    """Poll GET /v1/hitl/decisions/{incident_id} until approved|rejected|timeout.

    S1.3 escalation: emits Slack fallback at T=_ESCALATION_TIMEOUT_SEC if still pending.
    Uses adaptive backoff: healthy polling at _POLL_INTERVAL_BASE_SEC; backs off on failures.
    Returns _Decision.TIMEOUT if the approval window expires.
    """
    start = time.monotonic()
    deadline = start + _APPROVAL_TIMEOUT_SEC
    escalation_deadline = start + _ESCALATION_TIMEOUT_SEC
    escalated = False

    url = f"{_HITL_API_BASE}/v1/hitl/decisions/{action.siem_incident_id}"
    headers = {"Authorization": f"Bearer {_HITL_API_TOKEN}"}
    consecutive_errors = 0

    while time.monotonic() < deadline:
        # S1.3: emit fallback when escalation threshold crossed for the first time.
        if not escalated and time.monotonic() >= escalation_deadline:
            escalated = True
            elapsed = int(time.monotonic() - start)
            log.warning(
                '"hitl_escalation_threshold" trace="%s" elapsed_sec=%d channel="%s"',
                action.trace_id, elapsed, _FALLBACK_CHANNEL,
            )
            if _FALLBACK_CHANNEL == "slack" and _FALLBACK_WEBHOOK_URL:
                await emit_slack_fallback(
                    client=client,
                    webhook_url=_FALLBACK_WEBHOOK_URL,
                    trace_id=action.trace_id,
                    tool_name=action.tool_name,
                    incident_id=action.siem_incident_id,
                    explain=action.explain,
                    elapsed_sec=elapsed,
                )

        try:
            resp = await client.get(url, headers=headers, timeout=8.0)

            if resp.status_code == 200:
                consecutive_errors = 0
                try:
                    data: dict = resp.json()
                except Exception:
                    data = {}
                status = str(data.get("status") or "").lower().strip()
                if status == "approved":
                    return _Decision.APPROVED
                if status == "rejected":
                    return _Decision.REJECTED
                # Still "pending" — continue polling.

            elif resp.status_code == 404:
                # Not yet registered in the store — wait and retry.
                consecutive_errors = 0

            else:
                consecutive_errors += 1
                log.warning(
                    '"hitl_poll_unexpected_status" trace="%s" status=%d consecutive_errors=%d',
                    action.trace_id,
                    resp.status_code,
                    consecutive_errors,
                )

        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            consecutive_errors += 1
            log.warning(
                '"hitl_poll_network_error" trace="%s" err="%s" consecutive_errors=%d',
                action.trace_id,
                exc,
                consecutive_errors,
            )

        # Adaptive sleep: exponential backoff capped at 5× base on repeated failures,
        # full-jitter so concurrent pollers don't stampede on recovery.
        if consecutive_errors > 0:
            sleep = _jitter_sleep_sec(min(consecutive_errors, 5), _POLL_INTERVAL_BASE_SEC)
        else:
            sleep = _POLL_INTERVAL_BASE_SEC

        # Clamp to remaining deadline so we don't overshoot.
        remaining = deadline - time.monotonic()
        await asyncio.sleep(min(sleep, max(remaining, 0)))

    return _Decision.TIMEOUT


# ---------------------------------------------------------------------------
# Kafka helpers
# ---------------------------------------------------------------------------

async def _emit(producer: AIOKafkaProducer, topic: str, body: dict) -> None:
    payload = json.dumps({"data": json.dumps(body, ensure_ascii=False)}, ensure_ascii=False).encode()
    await producer.send_and_wait(topic, value=payload)


def _build_rejection_feedback(action: _PendingAction, reason: str) -> dict:
    fb = _RejectionFeedback(
        trace_id=action.trace_id,
        tool_name=action.tool_name,
        stderr=reason,
        hitl_rejected=True,
        siem_incident_id=action.siem_incident_id,
        siem_tenant=action.siem_tenant,
        hitl_reason=action.hitl_reason,   # original categorisation from Kafka message
    )
    import dataclasses
    return dataclasses.asdict(fb)


# ---------------------------------------------------------------------------
# Audit state (same-namespace Redis only — no cross-namespace writes)
# ---------------------------------------------------------------------------

async def _set_audit_state(redis: Redis, trace: str, status: str, tool: str, ttl: int) -> None:
    """Write internal audit state to Omni Redis (multi-agent namespace only)."""
    try:
        await redis.setex(
            _STATE_KEY_FMT.format(trace=trace),
            ttl,
            json.dumps({"status": status, "tool_name": tool}, ensure_ascii=False),
        )
    except Exception as exc:
        # Audit state write failure is non-fatal — routing decision is not affected.
        log.warning('"audit_state_write_failed" trace="%s" err="%s"', trace, exc)


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

async def _process(
    action:   _PendingAction,
    producer: AIOKafkaProducer,
    redis:    Redis,
    client:   httpx.AsyncClient,
) -> None:
    log.info(
        '"hitl_pending_received" trace="%s" tool="%s" incident_id="%s" explain="%s"',
        action.trace_id,
        action.tool_name,
        action.siem_incident_id,
        action.explain[:120] if action.explain else "",
    )

    await _set_audit_state(
        redis, action.trace_id, "AWAITING_HITL", action.tool_name,
        ttl=_APPROVAL_TIMEOUT_SEC + 300,
    )
    await mark_stage(redis, action.trace_id, "HITL", "pending", detail="awaiting operator")

    # Register with FinGuard HITL API — operators must see this.
    registered = await _register_pending(client, action)
    if not registered:
        # Fail closed: cannot register → auto-reject so the analyst is unblocked.
        log.error(
            '"hitl_register_failed_auto_reject" trace="%s" tool="%s"',
            action.trace_id,
            action.tool_name,
        )
        feedback = _build_rejection_feedback(
            action,
            "HITL_REGISTER_FAILED: could not register with HITL API; action auto-rejected for safety",
        )
        await _emit(producer, _TOPIC_FEEDBACK, feedback)
        await _set_audit_state(
            redis, action.trace_id, "REGISTER_FAILED_AUTO_REJECTED", action.tool_name, ttl=3600,
        )
        await mark_stage(redis, action.trace_id, "HITL", "fail", detail="register_failed_auto_reject")
        return

    # Poll for operator decision (HTTP only — no cross-namespace Redis polling).
    decision = await _poll_api_for_decision(client, action)

    if decision is _Decision.APPROVED:
        log.info('"hitl_approved" trace="%s" tool="%s"', action.trace_id, action.tool_name)
        await _emit(producer, _TOPIC_ACTIONS, action.raw_body)
        await _set_audit_state(
            redis, action.trace_id, "APPROVED_FORWARDED", action.tool_name, ttl=3600,
        )
        await mark_stage(redis, action.trace_id, "HITL", "ok", detail="approved")

    elif decision is _Decision.REJECTED:
        log.info('"hitl_rejected" trace="%s" tool="%s"', action.trace_id, action.tool_name)
        await mark_stage(redis, action.trace_id, "HITL", "fail", detail="rejected")
        feedback = _build_rejection_feedback(
            action, "HITL_REJECTED: operator declined this action",
        )
        await _emit(producer, _TOPIC_FEEDBACK, feedback)
        await _set_audit_state(
            redis, action.trace_id, "REJECTED", action.tool_name, ttl=3600,
        )

    else:  # TIMEOUT
        log.warning('"hitl_timeout" trace="%s" tool="%s"', action.trace_id, action.tool_name)
        await mark_stage(redis, action.trace_id, "HITL", "fail", detail="timeout")
        feedback = _build_rejection_feedback(
            action,
            f"HITL_TIMEOUT: no decision within {_APPROVAL_TIMEOUT_SEC}s approval window",
        )
        await _emit(producer, _TOPIC_FEEDBACK, feedback)
        await _set_audit_state(
            redis, action.trace_id, "EXPIRED", action.tool_name, ttl=3600,
        )
        # S1.3: store dead-letter so admin can replay
        await store_dead_letter(
            redis=redis,
            trace_id=action.trace_id,
            incident_id=action.siem_incident_id,
            tool_name=action.tool_name,
            raw_body=action.raw_body,
            reason=f"HITL_TIMEOUT after {_APPROVAL_TIMEOUT_SEC}s",
            ttl=_DEAD_LETTER_TTL_SEC,
        )


# ---------------------------------------------------------------------------
# Consumer loop
# ---------------------------------------------------------------------------

async def run(redis: Redis, producer: AIOKafkaProducer) -> None:
    """Main consumer loop.

    Bounded worker pool (size = _MAX_CONCURRENT) drains an asyncio.Queue.
    The consumer blocks on queue.put() when the queue is full — natural back-pressure.
    Each worker calls _PartitionTracker.done() and commits only when the per-partition
    watermark advances, so offset ordering is preserved across concurrent workers.
    """
    consumer = AIOKafkaConsumer(
        _TOPIC_PENDING,
        bootstrap_servers=_KAFKA_SERVERS,
        group_id=_CONSUMER_GROUP,
        auto_offset_reset="earliest",
        enable_auto_commit=False,            # manual watermark commit only
    )
    backoff = 2
    while True:
        try:
            await consumer.start()
            break
        except Exception as exc:
            log.warning('"hitl_consumer_bootstrap_retry" err="%s" backoff_s=%d', exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
    log.info('"hitl_dispatcher_started" topic="%s" group="%s"', _TOPIC_PENDING, _CONSUMER_GROUP)

    # queue capacity = 2× worker count so the consumer can read ahead without unbounded growth.
    queue: asyncio.Queue[tuple[bytes, object, int]] = asyncio.Queue(maxsize=_MAX_CONCURRENT * 2)
    trackers: dict[object, _PartitionTracker] = {}

    async def _worker() -> None:
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    raw, tp, offset = await queue.get()
                except asyncio.CancelledError:
                    return
                try:
                    try:
                        action = _parse_pending(raw)
                    except _ParseError as exc:
                        log.error('"hitl_parse_error" err="%s"', exc)
                        action = None

                    if action is not None:
                        await _process(action, producer, redis, client)

                finally:
                    commit_offset = trackers[tp].done(offset)
                    if commit_offset is not None:
                        try:
                            await consumer.commit({tp: commit_offset})
                        except Exception as exc:
                            log.error(
                                '"hitl_commit_error" tp="%s" commit_offset=%d err="%s"',
                                tp, commit_offset, exc,
                            )
                    queue.task_done()

    workers = [asyncio.create_task(_worker()) for _ in range(_MAX_CONCURRENT)]

    try:
        async for msg in consumer:
            tp = TopicPartition(msg.topic, msg.partition)
            if tp not in trackers:
                trackers[tp] = _PartitionTracker()
            trackers[tp].start(msg.offset)
            await queue.put((msg.value or b"{}", tp, msg.offset))   # blocks when full (back-pressure)

    except asyncio.CancelledError:
        log.info('"hitl_dispatcher_shutting_down"')
    finally:
        # Drain all in-flight items before tearing down workers.
        await queue.join()
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await consumer.stop()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main() -> None:
    redis = Redis.from_url(_REDIS_URL, decode_responses=False)
    producer = AIOKafkaProducer(bootstrap_servers=_KAFKA_SERVERS)
    backoff = 2
    while True:
        try:
            await producer.start()
            break
        except Exception as exc:
            log.warning('"hitl_producer_bootstrap_retry" err="%s" backoff_s=%d', exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
    try:
        await run(redis, producer)
    finally:
        await producer.stop()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
