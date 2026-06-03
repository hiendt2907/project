"""LogCluster: groups evidence items sharing the same fingerprint.

Cluster state is persisted in Redis with two TTL scopes:
  - Per-agent window  (5 min):  omni:evcluster:{agent_id}:{fingerprint}
  - Cross-agent long  (7 days): omni:evcluster:seen:{fingerprint}

The long-lived key lets the triage layer answer "has ANY agent ever seen
this pattern before?" without querying RAG on every item.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# Redis TTLs
_WINDOW_TTL_S = 300        # 5-min per-agent cluster window
_SEEN_TTL_S = 7 * 86400    # 7-day cross-agent pattern memory

_KEY_WINDOW = "omni:evcluster:{agent_id}:{fingerprint}"
_KEY_SEEN = "omni:evcluster:seen:{fingerprint}"

# After this many identical items in a 5-min window we declare a log storm.
STORM_THRESHOLD = 20


@dataclass
class LogCluster:
    fingerprint: str
    probe: str
    domain: str                   # from domain_signals.detect_domain()
    representative: dict[str, Any]  # richest evidence item in cluster
    count: int                    # occurrences in current window
    first_seen: float             # unix epoch of first item
    last_seen: float
    results: Counter              # {"FAILED":5,"PASSED":2,...}
    agent_ids: set[str]           # all agents that sent this pattern
    lane: str
    is_new: bool                  # True if never seen before (cross-agent, 7 days)
    is_storm: bool = False        # True if count > STORM_THRESHOLD


async def upsert_cluster(
    redis: Any,
    agent_id: str,
    fingerprint: str,
    item: dict[str, Any],
    domain: str,
) -> LogCluster:
    """
    Record a new occurrence of ``fingerprint`` from ``agent_id``.

    Returns the updated LogCluster. Does NOT decide whether to publish
    to Kafka — caller makes that decision based on count / is_storm.
    """
    now = time.time()
    probe = item.get("probe", "unknown")
    lane = item.get("lane", "SYS_RESOURCE")
    result = item.get("result", "PASSED")

    window_key = _KEY_WINDOW.format(agent_id=agent_id, fingerprint=fingerprint)
    seen_key = _KEY_SEEN.format(fingerprint=fingerprint)

    # ── per-agent window ──────────────────────────────────────────────────
    raw_window = await redis.get(window_key)
    if raw_window:
        state = json.loads(raw_window)
        state["count"] += 1
        state["last_seen"] = now
        state["results"][result] = state["results"].get(result, 0) + 1
        if agent_id not in state["agent_ids"]:
            state["agent_ids"].append(agent_id)
        # Keep representative with most content
        old_rep = state["representative"]
        old_len = len(old_rep.get("alert_hint","")) + len(old_rep.get("raw",""))
        new_len = len(item.get("alert_hint","")) + len(item.get("raw",""))
        if new_len > old_len:
            state["representative"] = item
    else:
        state = {
            "fingerprint": fingerprint,
            "probe": probe,
            "domain": domain,
            "representative": item,
            "count": 1,
            "first_seen": now,
            "last_seen": now,
            "results": {result: 1},
            "agent_ids": [agent_id],
            "lane": lane,
        }

    await redis.set(window_key, json.dumps(state), ex=_WINDOW_TTL_S)

    # ── cross-agent seen record ───────────────────────────────────────────
    raw_seen = await redis.get(seen_key)
    is_new = raw_seen is None
    if raw_seen:
        seen_state = json.loads(raw_seen)
        seen_state["total_count"] += 1
        seen_state["last_seen"] = now
        if agent_id not in seen_state["agents"]:
            seen_state["agents"].append(agent_id)
    else:
        seen_state = {
            "total_count": 1,
            "first_seen_ever": now,
            "last_seen": now,
            "agents": [agent_id],
            "last_diagnosis": None,
        }
    await redis.set(seen_key, json.dumps(seen_state), ex=_SEEN_TTL_S)

    return LogCluster(
        fingerprint=fingerprint,
        probe=probe,
        domain=domain,
        representative=state["representative"],
        count=state["count"],
        first_seen=state["first_seen"],
        last_seen=now,
        results=Counter(state["results"]),
        agent_ids=set(state["agent_ids"]),
        lane=lane,
        is_new=is_new,
        is_storm=state["count"] > STORM_THRESHOLD,
    )


async def mark_cluster_diagnosed(
    redis: Any,
    fingerprint: str,
    verdict: str,
    root_cause: str,
) -> None:
    """Update the cross-agent seen record with the LLM diagnosis result."""
    seen_key = _KEY_SEEN.format(fingerprint=fingerprint)
    raw = await redis.get(seen_key)
    if not raw:
        return
    state = json.loads(raw)
    state["last_diagnosis"] = {
        "verdict": verdict,
        "root_cause": root_cause[:200],
        "ts": time.time(),
    }
    # Refresh TTL on diagnosis to keep hot patterns in memory longer
    await redis.set(seen_key, json.dumps(state), ex=_SEEN_TTL_S)


async def get_seen_state(redis: Any, fingerprint: str) -> dict[str, Any] | None:
    """Return the cross-agent seen record, or None if not yet seen."""
    raw = await redis.get(_KEY_SEEN.format(fingerprint=fingerprint))
    return json.loads(raw) if raw else None
