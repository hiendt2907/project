"""Provider Audit — /audit phải không còn là stub, chiếu CRAT hash-chain thật.

Không tạo nguồn sự thật thứ hai: chỉ đọc audit_chain:*:blocks mà
services.audit_ledger.chain_writer.write_audit_block() đã ghi.
"""
from __future__ import annotations

import json
import time

import fakeredis.aioredis as aioredis
import httpx

from aoip.console import identity
from aoip.console.app import create_provider_app
from aoip.console.audit import build_provider_audit


def _redis():
    return aioredis.FakeRedis(decode_responses=True)


def _block(seq: int, event_type: str, tenant_id: str, ts: str, signed: bool = False) -> str:
    return json.dumps({
        "seq": seq, "event_type": event_type, "trace_id": f"trace-{seq}",
        "tenant_id": tenant_id, "timestamp_utc": ts,
        "block_hash": f"hash-{seq}", "signature_hex": "deadbeef" if signed else None,
    })


async def test_build_provider_audit_merges_default_and_named_tenants():
    r = _redis()
    await r.rpush("audit_chain:blocks", _block(1, "ADVISORY_DECISION", "default", "2026-07-08T10:00:00Z"))
    await r.rpush("audit_chain:acme:blocks",
                  _block(1, "MUTATION_TRAPPED", "acme", "2026-07-08T11:00:00Z", signed=True))

    result = await build_provider_audit(r)

    assert result["total"] == 2
    assert result["signed"] == 1
    assert result["event_counts"] == {"ADVISORY_DECISION": 1, "MUTATION_TRAPPED": 1}
    # newest first
    assert result["blocks"][0]["tenant_id"] == "acme"


async def test_build_provider_audit_filters_by_tenant():
    r = _redis()
    await r.rpush("audit_chain:blocks", _block(1, "ADVISORY_DECISION", "default", "2026-07-08T10:00:00Z"))
    await r.rpush("audit_chain:acme:blocks",
                  _block(1, "MUTATION_TRAPPED", "acme", "2026-07-08T11:00:00Z"))

    result = await build_provider_audit(r, tenant_id="acme")

    assert result["total"] == 1
    assert result["blocks"][0]["tenant_id"] == "acme"


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c")


async def test_audit_endpoint_enforces_raw_evidence_permission():
    r = _redis()
    await identity.upsert_user(r, subject="owner@aoip", email="owner@aoip")
    await identity.grant_provider_role(r, subject="owner@aoip", role="platform_owner")
    await identity.upsert_user(r, subject="operator@aoip", email="operator@aoip")
    await identity.grant_provider_role(r, subject="operator@aoip", role="platform_operator")
    await r.rpush("audit_chain:blocks", _block(1, "ADVISORY_DECISION", "default", "2026-07-08T10:00:00Z"))

    app = create_provider_app(r)
    owner_p = await identity.resolve_provider_principal(r, "owner@aoip")
    owner_sid = (await identity.issue_session(r, principal=owner_p, now=time.time())).sid
    operator_p = await identity.resolve_provider_principal(r, "operator@aoip")
    operator_sid = (await identity.issue_session(r, principal=operator_p, now=time.time())).sid

    async with _client(app) as c:
        denied = await c.get(
            "/api/provider/v1/audit", headers={"Authorization": f"Bearer {operator_sid}"},
        )
        assert denied.status_code == 403

        allowed = await c.get(
            "/api/provider/v1/audit", headers={"Authorization": f"Bearer {owner_sid}"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["total"] == 1
