"""Phase 3 (0-6 roadmap): durable agent_id ownership.

Confirmed live 2026-07-21 that 2 of 3 real fleet hosts (cust-edge, cust-db)
still authenticate with a tenant-shared key, not an IT-3 per-agent
credential — for those, ownership of an agent_id was ENTIRELY determined by
whichever tenant's key currently has a live entry in the ephemeral Redis
registry (TTL=120s). A network gap longer than the TTL let any tenant
re-claim the same agent_id string. migrations/omni_admin/0010 +
AdminConfigRepo.get_or_claim_agent_owner() close this with a durable,
no-TTL, first-claim-wins PG record — tested here at the repo layer (real
query shapes against a fake PG that mimics INSERT...ON CONFLICT semantics)
and at the require_agent_tenant() wiring layer (real FakeRedis simulating an
expired registry).
"""
from __future__ import annotations

import pytest
from fakeredis.aioredis import FakeRedis

from services.admin_config.repo import AdminConfigRepo


class _FakeConn:
    def __init__(self, claims: dict[str, str]) -> None:
        self._claims = claims

    async def fetchrow(self, query: str, *args):
        if "INSERT INTO omni_admin.agent_identity_claim" in query:
            agent_id, tenant_id = args
            if agent_id in self._claims:
                return None  # ON CONFLICT DO NOTHING -> no RETURNING row
            self._claims[agent_id] = tenant_id
            return {"tenant_id": tenant_id}
        if "SELECT tenant_id FROM omni_admin.agent_identity_claim" in query:
            (agent_id,) = args
            owner = self._claims.get(agent_id)
            return {"tenant_id": owner} if owner else None
        raise AssertionError(f"unexpected query: {query}")


class _FakeConnCtx:
    def __init__(self, claims: dict[str, str]) -> None:
        self._claims = claims

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._claims)

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self) -> None:
        self.claims: dict[str, str] = {}

    def acquire(self) -> _FakeConnCtx:
        return _FakeConnCtx(self.claims)


@pytest.fixture
def repo() -> AdminConfigRepo:
    return AdminConfigRepo(_FakePool())


class TestGetOrClaimAgentOwnerRepo:
    async def test_first_claim_returns_own_tenant(self, repo):
        owner = await repo.get_or_claim_agent_owner("agent-x", "tenant-a")
        assert owner == "tenant-a"

    async def test_second_claim_by_different_tenant_returns_original_owner(self, repo):
        await repo.get_or_claim_agent_owner("agent-x", "tenant-a")
        # tenant-b tries to claim the SAME agent_id later (simulates a
        # registry TTL expiry window where tenant-b's shared key attempts
        # to register as agent-x)
        owner = await repo.get_or_claim_agent_owner("agent-x", "tenant-b")
        assert owner == "tenant-a"  # NOT tenant-b — durable, first-claim-wins

    async def test_repeated_claim_by_original_owner_is_idempotent(self, repo):
        await repo.get_or_claim_agent_owner("agent-x", "tenant-a")
        owner = await repo.get_or_claim_agent_owner("agent-x", "tenant-a")
        assert owner == "tenant-a"

    async def test_different_agent_ids_claim_independently(self, repo):
        owner_x = await repo.get_or_claim_agent_owner("agent-x", "tenant-a")
        owner_y = await repo.get_or_claim_agent_owner("agent-y", "tenant-b")
        assert owner_x == "tenant-a"
        assert owner_y == "tenant-b"


class TestRequireAgentTenantDurableFallback:
    """Wiring: require_agent_tenant() falls back to the durable claim ONLY
    when the ephemeral Redis registry has no live record — simulating a
    registry TTL expiry, not mocking resolve behavior directly."""

    async def test_registry_expired_durable_claim_blocks_different_tenant(self, repo):
        from gateway.tenant_context import TenantContext, require_agent_tenant

        redis = FakeRedis(decode_responses=True)
        # tenant-a durably claimed agent-x on an earlier request (registry
        # since expired — no key in `redis` for it at all, simulating TTL=120s
        # having elapsed with no re-registration).
        await repo.get_or_claim_agent_owner("agent-x", "tenant-a")

        ctx_b = TenantContext(tenant_id="tenant-b", is_admin=False)
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await require_agent_tenant(redis, "agent-x", ctx_b, repo=repo)
        assert exc.value.status_code == 403

    async def test_registry_expired_durable_claim_allows_original_owner(self, repo):
        from gateway.tenant_context import TenantContext, require_agent_tenant

        redis = FakeRedis(decode_responses=True)
        await repo.get_or_claim_agent_owner("agent-x", "tenant-a")

        ctx_a = TenantContext(tenant_id="tenant-a", is_admin=False)
        await require_agent_tenant(redis, "agent-x", ctx_a, repo=repo)  # no raise

    async def test_no_repo_skips_durable_layer_backward_compatible(self):
        """repo=None (lightweight ASGI/unit harnesses, or a gateway pod
        without OMNI_ADMIN_PG_DSN configured) must not break — same
        fail-open-on-missing-dependency pattern as every other repo-gated
        check in this module."""
        from gateway.tenant_context import TenantContext, require_agent_tenant

        redis = FakeRedis(decode_responses=True)
        ctx = TenantContext(tenant_id="tenant-a", is_admin=False)
        await require_agent_tenant(redis, "agent-x", ctx, repo=None)  # no raise

    async def test_live_registry_record_takes_priority_over_durable_claim(self, repo):
        """If the Redis registry DOES have a live record, that is checked
        (fast path) and the durable layer is not consulted at all — matches
        the existing (pre-Phase-3) registry-based ownership semantics for
        the common case where nothing has expired."""
        import json
        import time

        from gateway.tenant_context import TenantContext, require_agent_tenant

        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:agent-x",
                        json.dumps({"agent_id": "agent-x", "tenant_id": "tenant-a",
                                    "last_seen": int(time.time())}))
        # durable claim says tenant-b (e.g. stale from a past incident) —
        # should NOT be consulted since the registry has a live record.
        await repo.get_or_claim_agent_owner("agent-x", "tenant-b")

        ctx_a = TenantContext(tenant_id="tenant-a", is_admin=False)
        await require_agent_tenant(redis, "agent-x", ctx_a, repo=repo)  # no raise — registry wins
