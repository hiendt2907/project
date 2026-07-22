"""Slice O1 Bước 0: role=full (canonical lab deployment) must register the
discovery-evidence consumer — otherwise onboarding accumulation silently never
runs against production (single omni-fullstack pod, role=full)."""
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


class TestRoleFullRegistersDiscoveryConsumer:
    @pytest.mark.asyncio
    async def test_full_role_task_names_include_discovery_loop(self):
        from workers.omni_worker import _worker_background_tasks

        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _ctx(r)
        ctx.scout_ready.set()
        stop = asyncio.Event()

        tasks = _worker_background_tasks(ctx, stop)
        try:
            names = {t.get_name() for t in tasks}
            assert "kafka_discovery_evidence_loop" in names
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
