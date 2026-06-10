"""Backfill core SIEM playbooks into Redis (idempotent).

Usage:
    python scripts/backfill_playbooks.py

Reads OMNI_REDIS_URL from env (default: redis://localhost:16379/0).
Ensures idx:playbooks exists, then upserts pre-approved SIEM playbooks.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import redis.asyncio as redis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from services.playbook.models import Playbook, PlaybookStep  # noqa: E402
from services.playbook.store import PlaybookStore  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


CORE_PLAYBOOKS: tuple[Playbook, ...] = (
    Playbook(
        playbook_id="ddos-edge-ingress-mitigation",
        version="1",
        name="DDoS Edge Ingress Mitigation",
        severity_filter="critical",
        approved_by="sre-lead",
        siem_categories=("ddos",),
        steps=(
            PlaybookStep(
                step_order=1,
                action_type="ingress_rate_limit",
                target="ingress-nginx/edge",
                params={"rps": 50, "burst": 100, "window_sec": 60},
                timeout_sec=30,
                requires_hitl=True,
            ),
            PlaybookStep(
                step_order=2,
                action_type="block_source_ip_set",
                target="cloud-armor/edge-policy",
                params={"source": "siem.suspect_ips", "ttl_sec": 3600},
                timeout_sec=30,
                requires_hitl=True,
            ),
            PlaybookStep(
                step_order=3,
                action_type="scale_replicas",
                target="deploy/edge-proxy",
                params={"min_replicas": 6, "max_replicas": 20},
                timeout_sec=60,
                requires_hitl=False,
            ),
        ),
    ),
)


async def main() -> None:
    url = os.environ.get("OMNI_REDIS_URL", "redis://localhost:16379/0")
    r = redis.from_url(url, decode_responses=True)
    store = PlaybookStore(r)
    await store.ensure_ready()
    for pb in CORE_PLAYBOOKS:
        await store.upsert(pb)
        logger.info("upserted playbook=%s", pb.playbook_id)
    await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())
