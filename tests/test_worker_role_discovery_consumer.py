"""role=onboarding is the sole owner of the discovery-evidence consumer group.

Originally (Slice O1 Bước 0) role=full also registered this loop, reasoned as
"otherwise onboarding accumulation silently never runs against production
(single omni-fullstack pod, role=full)" — true only while no dedicated
onboarding deployment existed. A dedicated `omni-onboarding` Deployment
(role=onboarding) now runs permanently alongside `omni-fullstack` (see
CLAUDE.md "Declared target topology"), so role=full also joining the same
fixed `consumer_group_onboarding` group created two independent group members
competing over a single-partition topic — confirmed live 2026-08-03 as the
root cause of `omni-onboarding` crash-looping (exit 137, 15 restarts/3h27m)
via repeated Kafka rebalances on group `omni-onboarding-discovery` logged by
BOTH pods. role=full no longer registers this loop."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import fakeredis.aioredis
import pytest


def _settings(**overrides):
    base = dict(
        worker_role="full",
        autonomous_decider_enabled=False,
        proactive_enabled=False,
        telegram_polling_enabled=False,
        siem_chain_consumer_enabled=False,
        siem_correlation_enabled=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _ctx(redis):
    ledger = SimpleNamespace(record_exception=lambda *a, **k: None)
    return SimpleNamespace(
        redis=redis,
        kafka=None,
        telegram=None,
        settings=_settings(),
        scout_ready=asyncio.Event(),
        ledger=ledger,
    )


class TestRoleFullDoesNotDuplicateDiscoveryConsumer:
    @pytest.mark.asyncio
    async def test_full_role_task_names_exclude_discovery_loop(self):
        """role=full must NOT join `consumer_group_onboarding` — the dedicated
        `omni-onboarding` deployment is the sole owner (see module docstring)."""
        from workers.omni_worker import _worker_background_tasks

        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _ctx(r)
        ctx.scout_ready.set()
        stop = asyncio.Event()

        tasks = _worker_background_tasks(ctx, stop)
        try:
            names = {t.get_name() for t in tasks}
            assert "kafka_discovery_evidence_loop" not in names
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_onboarding_role_still_registers_discovery_loop_no_duplicate_within_role(self):
        """role=onboarding keeps working (dual-role support, additive change) and does
        not accidentally register the loop twice for the same role."""
        from workers.omni_worker import _worker_background_tasks

        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _ctx(r)
        ctx.settings = _settings(worker_role="onboarding")
        ctx.scout_ready.set()
        stop = asyncio.Event()

        tasks = _worker_background_tasks(ctx, stop)
        try:
            names = [t.get_name() for t in tasks]
            assert names.count("kafka_discovery_evidence_loop") == 1
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_executor_role_does_not_register_discovery_loop(self):
        from workers.omni_worker import _worker_background_tasks

        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _ctx(r)
        ctx.settings = _settings(worker_role="executor")
        ctx.scout_ready.set()
        stop = asyncio.Event()

        tasks = _worker_background_tasks(ctx, stop)
        try:
            names = {t.get_name() for t in tasks}
            assert "kafka_discovery_evidence_loop" not in names
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
